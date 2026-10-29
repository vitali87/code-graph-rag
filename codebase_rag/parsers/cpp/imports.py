"""C and C++'s import pass: `#include` directives and C++20 module imports and
declarations into the shared import map, a quoted include resolving to the
repo file it names by the indexer's own walk, and the includes a later
binding displaced or a module declaration wrote, which the deferral treats
apart.

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a C or
C++ file's imports to the `CppImporter` it builds."""

import os
import posixpath
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...node_text import safe_decode_text, safe_decode_with_fallback
from ...types_defs import PropertyDict
from .qn import build_module_qn_map


def _cpp_include_spec(include_node: Node) -> tuple[str, bool] | None:
    """(include path, is a system include) from a `preproc_include`, or None.

    The LAST string child wins, as it always did; a quoted path is stripped
    of its quotes and an angle-bracket path of its brackets.
    """
    spec: tuple[str, bool] | None = None
    for child in include_node.children:
        if child.type == cs.TS_STRING_LITERAL:
            spec = (safe_decode_with_fallback(child).strip('"'), False)
        elif child.type == cs.TS_SYSTEM_LIB_STRING:
            spec = (safe_decode_with_fallback(child).strip("<>"), True)
    return spec if spec is not None and spec[0] else None


def _include_path_suffix(include_path: str) -> str:
    """A written include path as the repository path suffix it names:
    normalised, with every `.` and `..` segment dropped wherever it falls
    (after `normpath`, only leading `..` segments can remain)."""
    parts = [
        part
        for part in posixpath.normpath(include_path.replace("\\", "/")).split("/")
        if part not in ("", ".", "..")
    ]
    return "/".join(parts)


def _dotted_include_path(include_path: str) -> str:
    """An include path as dotted module segments: `sys/types.h` -> `sys.types.h`.

    The qualified name is a dotted path everywhere else in the graph, so a
    slash left in it (`std.sys/types.h`) is not addressable by any dotted
    lookup and does not segment (issue #1758).
    """
    return include_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)


def _cpp_include_local_name(include_path: str) -> str:
    """The name the include binds locally: the header's stem for `.h`/`.hpp`,
    the bare last path segment otherwise (`<vector>`)."""
    header_name = include_path.rsplit(cs.SEPARATOR_SLASH, maxsplit=1)[-1]
    if header_name.endswith(cs.EXT_H) or header_name.endswith(cs.EXT_HPP):
        return header_name.split(cs.SEPARATOR_DOT)[0]
    return header_name


def _cpp_import_module_name(template_args: Node) -> str | None:
    # The module a C++ `import<...>` names; the last type identifier wins,
    # reading a type_descriptor's first type identifier.
    module_name = None
    for child in template_args.children:
        if child.type == cs.TS_TYPE_DESCRIPTOR:
            inner = next(
                (c for c in child.children if c.type == cs.TS_TYPE_IDENTIFIER), None
            )
            if inner is not None:
                module_name = safe_decode_with_fallback(inner)
        elif child.type == cs.TS_TYPE_IDENTIFIER:
            module_name = safe_decode_with_fallback(child)
    return module_name


class CppImportHost(Protocol):
    """What the C/C++ importer reads and calls on the import processor that
    builds it: the indexer's walk policy, the unresolved-reference record, and
    the import-site record every language's bindings go through.

    The policy is read here rather than copied in: the factory re-resolves
    `unignore_paths` on processors it has already built
    (`propagate_unignore_paths`), and a stale copy would resolve an include
    against files the indexer skips, or miss a rescued tree (issue #1088)."""

    exclude_paths: frozenset[str] | None
    unignore_paths: frozenset[str] | None

    def note_unresolved(self, module_qn: str, name: str) -> None: ...

    def _record_import_site(
        self,
        scope_qn: str,
        local_name: str,
        node: Node | None,
        imported_name: str | None = None,
        owner_qn: str | None = None,
    ) -> PropertyDict | None: ...


class CppImporter:
    __slots__ = (
        "_host",
        "repo_path",
        "project_name",
        "import_mapping",
        "_cpp_module_qn_map",
        "_cpp_qn_to_rel",
        "_cpp_declaration_mappings",
        "_cpp_shadowed_include_targets",
    )

    def __init__(
        self,
        host: CppImportHost,
        repo_path: Path,
        project_name: str,
        import_mapping: dict[str, dict[str, str]],
    ) -> None:
        self._host = host
        self.repo_path = repo_path
        self.project_name = project_name
        # The processor's own map, shared rather than copied: an include
        # binds its name in the map every later pass reads.
        self.import_mapping = import_mapping
        # Lazy: replayed walk of every eligible repo file, built on the first C++
        # include so non-C++ projects never pay for it.
        self._cpp_module_qn_map: dict[str, str] | None = None
        self._cpp_qn_to_rel: dict[str, str] = {}
        # Import-map entries registered by C++20 module DECLARATIONS (`module X;`,
        # `export module X;`, `import :partition;`). They exist for name resolution
        # only; a declaration is not an import, so no IMPORTS edge is emitted.
        self._cpp_declaration_mappings: set[tuple[str, str]] = set()
        # Include targets whose LOCAL name was taken by a later include in
        # the same file. `<std>` and `<std.h>` both bind `std`, so the second
        # overwrote the first in `import_mapping` and only one IMPORTS edge
        # survived (issue #1758). The binding can hold one name, but the file
        # really does include both headers, so the edge is kept here.
        self._cpp_shadowed_include_targets: set[tuple[str, str]] = set()

    def parse_imports(self, captures: dict[str, list[Node]], module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_PREPROC_INCLUDE:
                self._parse_cpp_include(import_node, module_qn)
            elif import_node.type == cs.TS_TEMPLATE_FUNCTION:
                self._parse_cpp_module_import(import_node, module_qn)
            elif import_node.type == cs.TS_DECLARATION:
                self._parse_cpp_module_declaration(import_node, module_qn)

    def _resolve_cpp_include_target(
        self, include_path: str, module_qn: str
    ) -> str | None:
        """Resolve a quoted #include to the module qn of a real repo file.

        Tries the includer's directory, then the repo root, then a unique
        path-suffix match (covers -I style includes written relative to a
        source root). Returns None for headers outside the repo.
        """
        if self._cpp_module_qn_map is None:
            self._cpp_module_qn_map = build_module_qn_map(
                self.repo_path,
                self.project_name,
                self._host.exclude_paths,
                self._host.unignore_paths,
            )
            self._cpp_qn_to_rel = {
                qn: rel for rel, qn in self._cpp_module_qn_map.items()
            }
        normalized = os.path.normpath(include_path).replace(os.sep, cs.SEPARATOR_SLASH)

        includer_rel = self._cpp_qn_to_rel.get(module_qn)
        if includer_rel is not None:
            candidate = os.path.normpath(
                str(Path(includer_rel).parent / normalized)
            ).replace(os.sep, cs.SEPARATOR_SLASH)
            if qn := self._cpp_module_qn_map.get(candidate):
                return qn

        if qn := self._cpp_module_qn_map.get(normalized):
            return qn

        suffix = f"{cs.SEPARATOR_SLASH}{normalized}"
        matches = sorted(rel for rel in self._cpp_module_qn_map if rel.endswith(suffix))
        if not matches:
            return None
        if len(matches) > 1 and includer_rel is not None:
            # Prefer, deterministically, the header sharing the longest path prefix
            # with the includer (the same source tree). commonpath (not
            # commonprefix) so sibling dirs with a shared name prefix (src/ast vs
            # src/ast_new) rank by whole components.
            matches.sort(
                key=lambda rel: (
                    -len(os.path.commonpath([rel, includer_rel])),
                    rel,
                )
            )
        return self._cpp_module_qn_map[matches[0]]

    def _parse_cpp_include(self, include_node: Node, module_qn: str) -> None:
        spec = _cpp_include_spec(include_node)
        if spec is None:
            return
        include_path, is_system_include = spec
        local_name = _cpp_include_local_name(include_path)
        full_name = self._cpp_include_full_name(
            include_path, is_system_include, module_qn
        )
        # A local name already bound by an earlier include in this file names
        # a DIFFERENT header (`<std>` then `<std.h>`, both binding `std`).
        # The map holds one binding, so remember the loser's target: it is a
        # real include of a real header and must keep its IMPORTS edge (#1758).
        displaced = self.import_mapping[module_qn].get(local_name)
        if displaced is not None and displaced != full_name:
            self._cpp_shadowed_include_targets.add((module_qn, displaced))
        self.import_mapping[module_qn][local_name] = full_name
        self._host._record_import_site(
            module_qn, local_name, include_node, include_path
        )
        logger.debug(
            ls.IMP_CPP_INCLUDE,
            local=local_name,
            full=full_name,
            system=is_system_include,
        )

    def _cpp_include_full_name(
        self, include_path: str, is_system_include: bool, module_qn: str
    ) -> str:
        """The module qn an include names: `std.<path>` for a system header or
        an unresolved quoted one, else the repo file the include resolves to."""
        if is_system_include:
            # The token, not the substring: `startswith("std")` took
            # `<stdio.h>`, `<stdlib.h>`, `<stdint.h>` and the rest of that
            # family as already prefixed, so they became ExternalModule
            # `stdio.h` while `<signal.h>` in the same file became
            # `std.signal.h` (issue #1744).
            if include_path == cs.CPP_STD_PREFIX or include_path.startswith(
                cs.IMPORT_STD_PREFIX
            ):
                return include_path
            # A slashed include path is segmented like any other module path:
            # `<sys/types.h>` is the module `types.h` under `sys`, and leaving
            # the slash in produced the qn `std.sys/types.h`, which no dotted
            # lookup can address (issue #1758).
            #
            # Accepted collision, measured: this maps `<sys/types.h>` and a
            # literal `<sys.types.h>` onto the same qn, which `main` kept
            # distinct by leaving the slash in. C and C++ use `/` as the
            # include separator and a dot only inside the final component, so
            # the colliding spelling is not a form real code takes; and the
            # qn `main` gave the slashed one was unaddressable anyway, so
            # nothing could resolve against it. A conflated pair of headers
            # that both exist is the cheaper error than a target no dotted
            # lookup can name at all. `test_two_spellings_of_one_header_path`
            # pins the behaviour so a later reader sees a decision rather
            # than an oversight (#1758).
            return f"{cs.IMPORT_STD_PREFIX}{_dotted_include_path(include_path)}"
        if resolved := self._resolve_cpp_include_target(include_path, module_qn):
            # The include resolves to a real repo file; use that file's
            # actual (collision-disambiguated) module qn. The old
            # project-rooted, extension-stripped guess produced phantom
            # module qns (self-imports for same-stem header/source pairs,
            # wrong roots for -I style includes), which poisoned both the
            # IMPORTS edges and class resolution via the import map
            # (issue #652).
            return resolved
        # A quoted include matching no repo file is a third-party header; a
        # project-rooted qn would be a phantom. Segmented like the system
        # branch above, for the same reason (issue #1758). Recorded for the
        # day the header is added (issue #1568), normalised to the repo path
        # suffix an added file offers: `./include/base.h` and
        # `../include/base.h` both wait on `include/base.h`.
        self._host.note_unresolved(module_qn, _include_path_suffix(include_path))
        return f"{cs.IMPORT_STD_PREFIX}{_dotted_include_path(include_path)}"

    def _parse_cpp_module_import(self, import_node: Node, module_qn: str) -> None:
        identifier_child = None
        template_args_child = None

        for child in import_node.children:
            if child.type == cs.TS_IDENTIFIER:
                identifier_child = child
            elif child.type == cs.TS_TEMPLATE_ARGUMENT_LIST:
                template_args_child = child

        if not (
            identifier_child
            and safe_decode_text(identifier_child) == cs.IMPORT_IMPORT
            and template_args_child
        ):
            return
        if module_name := _cpp_import_module_name(template_args_child):
            local_name = module_name
            full_name = f"{cs.IMPORT_STD_PREFIX}{module_name}"
            self.import_mapping[module_qn][local_name] = full_name
            self._host._record_import_site(
                module_qn, local_name, import_node, module_name
            )
            logger.debug(ls.IMP_CPP_MODULE, local=local_name, full=full_name)

    def _parse_cpp_module_declaration(self, decl_node: Node, module_qn: str) -> None:
        decoded_text = safe_decode_text(decl_node)
        if not decoded_text:
            return
        decl_text = decoded_text.strip()

        if decl_text.startswith(cs.CPP_MODULE_PREFIX) and not decl_text.startswith(
            cs.CPP_MODULE_PRIVATE_PREFIX
        ):
            parts = decl_text.split()
            if len(parts) >= 2:
                self._register_cpp_module_mapping(
                    parts, 1, module_qn, ls.IMP_CPP_MODULE_IMPL
                )
        elif decl_text.startswith(cs.CPP_EXPORT_MODULE_PREFIX):
            parts = decl_text.split()
            if len(parts) >= 3:
                self._register_cpp_module_mapping(
                    parts, 2, module_qn, ls.IMP_CPP_MODULE_IFACE
                )
        elif cs.CPP_IMPORT_PARTITION_PREFIX in decl_text:
            colon_pos = decl_text.find(cs.CHAR_COLON)
            if colon_pos != -1:
                if partition_part := decl_text[colon_pos + 1 :].split(";")[0].strip():
                    partition_name = f"{cs.CPP_PARTITION_PREFIX}{partition_part}"
                    full_name = f"{self.project_name}{cs.SEPARATOR_DOT}{partition_part}"
                    self.import_mapping[module_qn][partition_name] = full_name
                    # A partition lives inside the same named module; no graph node
                    # models it, so never emit an IMPORTS edge.
                    self._cpp_declaration_mappings.add((module_qn, full_name))
                    logger.debug(
                        ls.IMP_CPP_PARTITION,
                        partition=partition_name,
                        full=full_name,
                    )

    def _register_cpp_module_mapping(
        self, parts: list[str], name_index: int, module_qn: str, log_template: str
    ) -> None:
        module_name = parts[name_index].rstrip(";")
        full_name = f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
        # A global module fragment puts includes BEFORE the declaration:
        #
        #     module;
        #     #include <foo.h>
        #     export module foo;
        #
        # so the declaration displaces the include's binding, exactly as a
        # later include displaces an earlier one. Record it the same way or
        # the header's IMPORTS edge is lost -- the declaration's own target is
        # then skipped by the deferred loop as a self-import, and nothing is
        # emitted for the include at all (#1758).
        displaced = self.import_mapping[module_qn].get(module_name)
        if displaced is not None and displaced != full_name:
            self._cpp_shadowed_include_targets.add((module_qn, displaced))
        self.import_mapping[module_qn][module_name] = full_name
        # `module X;` / `export module X;` DECLARE this file's module; the mapping
        # exists for name resolution only, never as an IMPORTS edge.
        self._cpp_declaration_mappings.add((module_qn, full_name))
        logger.debug(log_template, name=module_name)

    def displaced_include_targets(self) -> dict[str, frozenset[str]]:
        """The headers each file includes under a local name a later include
        took over (#1758), by including module.

        The mapping holds one binding per local name, so these are the rest
        of what a file includes: `a/util.h` and `b/util.h` both bind `util`,
        and both are compiled into the file (PR #2593).
        """
        displaced: dict[str, set[str]] = {}
        for module_qn, target in self._cpp_shadowed_include_targets:
            if (module_qn, target) not in self._cpp_declaration_mappings:
                displaced.setdefault(module_qn, set()).add(target)
        return {module: frozenset(targets) for module, targets in displaced.items()}

    def drop_module_state(self, module_qn: str) -> None:
        # Cleared with the mapping it shadows: these entries ADD edges, so a
        # stale one would resurrect an include the edited file has removed
        # (issue #1758).
        self._cpp_shadowed_include_targets = {
            entry
            for entry in self._cpp_shadowed_include_targets
            if entry[0] != module_qn
        }
        # Cleared here too, now that the shadowed sweep READS it. These
        # entries SUPPRESS edges, so a stale one is the opposite hazard: a
        # re-parse that removes an `export module X;` used to leave an
        # exemption that only ever matched the declaration binding it came
        # from -- already gone, so inert. The sweep matches on the resolved
        # qn instead, so the same stale entry can now suppress a real
        # include's edge (#1758).
        self._cpp_declaration_mappings = {
            entry for entry in self._cpp_declaration_mappings if entry[0] != module_qn
        }

    def is_declaration(self, module_qn: str, full_name: str) -> bool:
        # A binding a module declaration (`module X;`, `export module X;`,
        # `import :part;`) wrote: name resolution only, never an IMPORTS edge.
        return (module_qn, full_name) in self._cpp_declaration_mappings

    def shadowed_include_targets(self, module_qn: str) -> Iterator[str]:
        """The includes of `module_qn` whose local binding a later include took
        over, in the order the deferral queues their edges."""
        for module_key, shadowed in self._cpp_shadowed_include_targets:
            if module_key != module_qn:
                continue
            if (module_qn, shadowed) in self._cpp_declaration_mappings:
                # The displaced binding can be one `export module X;`
                # wrote, not an include: re-emitting it would rebuild
                # the self-import the deferral's binding loop filters
                # out, which `test_cpp_module_declarations_emit_no_
                # self_import` forbids (#1758).
                continue
            yield shadowed

    def forget_module_qn_map(self) -> None:
        # A deleted file leaves the replayed walk; the next include rebuilds it.
        self._cpp_module_qn_map = None
        self._cpp_qn_to_rel.clear()
