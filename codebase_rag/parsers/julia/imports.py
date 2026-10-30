"""Julia's import pass: `using`/`import` statements into the shared import
map, keyed under a nested `module`'s own qn where one encloses the statement,
and the file, stem and declared-module indices a target resolves through.

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a Julia
file's imports to the `JuliaImporter` it builds."""

import importlib
import re
from functools import cache
from pathlib import Path
from typing import NamedTuple, Protocol

from tree_sitter import Language, Node, Parser

from ... import constants as cs
from ...node_text import safe_decode_text
from ...types_defs import PropertyDict
from ...utils.path_utils import should_skip_rel_file

_JULIA_SNAKE_RE_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_JULIA_SNAKE_RE_WORD = re.compile(r"([a-z0-9])([A-Z])")


class _JuliaModuleIndex(NamedTuple):
    # declared: module name -> the files that declare it. scoped: (file,
    # dotted enclosing-module chain) -> {module name: qn suffix relative to
    # the file module}.
    declared: dict[str, set[str]]
    scoped: dict[tuple[str, str], dict[str, str]]


@cache
def _julia_parser() -> Parser | None:
    """A tree-sitter Parser for Julia, or None without the grammar.

    The declared-module fallback re-parses files the main pipeline already
    parsed, but the import processor owns no parser, so it loads one on
    demand. tree-sitter-julia ships with the treesitter-full extra; without
    it the fallback degrades to the path/stem lookups.
    """
    try:
        grammar = importlib.import_module(f"{cs.TREE_SITTER_MODULE_PREFIX}julia")
        return Parser(Language(grammar.language()))
    except Exception:  # noqa: BLE001 -- optional component: any failure reads as unavailable
        return None


def _julia_module_name(node: Node) -> str | None:
    return next(
        (
            safe_decode_text(child)
            for child in node.named_children
            if child.type == cs.TS_JULIA_IDENTIFIER
        ),
        None,
    )


def _julia_unmacro(node: Node | None) -> str:
    # `@foo` binds and is invoked as `foo`, as `julia_utils.call_name` reads it.
    return (safe_decode_text(node) or "").lstrip("@")


def _julia_snake_case(name: str) -> str:
    # Julia convention pairs CamelCase modules with snake_case files
    # (`SharedStructs` -> `shared_structs.jl`); acronyms keep their grouping.
    s = _JULIA_SNAKE_RE_ACRONYM.sub(r"\1_\2", name)
    return _JULIA_SNAKE_RE_WORD.sub(r"\1_\2", s).lower()


class JuliaImportHost(Protocol):
    """What the Julia importer reads and calls on the import processor that
    builds it: the indexer's walk policy, and the import-site record every
    language's bindings go through.

    The policy is read here rather than copied in: the factory re-resolves
    `unignore_paths` on processors it has already built
    (`propagate_unignore_paths`), and a stale copy would let an excluded file
    win import resolution, or keep a rescued one from it (issue #1088)."""

    exclude_paths: frozenset[str] | None
    unignore_paths: frozenset[str] | None

    def _record_import_site(
        self,
        scope_qn: str,
        local_name: str,
        node: Node | None,
        imported_name: str | None = None,
        owner_qn: str | None = None,
    ) -> PropertyDict | None: ...


class JuliaImporter:
    __slots__ = (
        "_host",
        "repo_path",
        "project_name",
        "import_mapping",
        "_julia_scope_map_keys",
        "_julia_file_stems",
        "_julia_modules",
    )

    def __init__(
        self,
        host: JuliaImportHost,
        repo_path: Path,
        project_name: str,
        import_mapping: dict[str, dict[str, str]],
    ) -> None:
        self._host = host
        self.repo_path = repo_path
        self.project_name = project_name
        # The processor's own map, shared rather than copied: a `using` binds
        # its names in the map every later pass reads.
        self.import_mapping = import_mapping
        # File module -> its nested-scope map keys; read unguarded on the
        # re-ingest path, which never runs the per-run cache resets.
        self._julia_scope_map_keys: dict[str, set[str]] = {}
        # Filesystem-derived, built on the first Julia import that needs them
        # and dropped by reset_julia_path_caches.
        self._julia_file_stems: dict[str, set[str]] | None = None
        self._julia_modules: _JuliaModuleIndex | None = None

    def reset_julia_path_caches(self) -> None:
        # Same contract as reset_rust_path_caches: the stem and declared-
        # module indices are filesystem-derived, so files added or removed
        # between runs must not keep resolving against the old layout.
        self._julia_file_stems = None
        self._julia_modules = None
        self._julia_scope_map_keys = {}

    def scope_map_keys(self, module_qn: str) -> set[str]:
        # The nested-`module` keys this file's bindings sit under, beside its
        # own, for the deferral that keeps their IMPORTS edges on the file.
        return self._julia_scope_map_keys.get(module_qn, set())

    def drop_scope_map_keys(self, module_qn: str) -> None:
        self._julia_scope_map_keys.pop(module_qn, None)

    def _julia_import_scope_name(self, stmt_node: Node) -> str | None:
        # Dotted chain of every enclosing `module`, outermost first (None
        # at file scope), so `Outer.Inner` matches the caller qn chain.
        node = stmt_node.parent
        names: list[str] = []
        while node is not None:
            if node.type == cs.TS_JULIA_MODULE_DEFINITION:
                name = next(
                    (
                        safe_decode_text(c)
                        for c in node.named_children
                        if c.type == cs.TS_JULIA_IDENTIFIER
                    ),
                    None,
                )
                if name is None:
                    return None
                names.append(name)
            node = node.parent
        return cs.SEPARATOR_DOT.join(reversed(names)) if names else None

    def parse_imports(
        self,
        captures: dict[str, list[Node]],
        module_qn: str,
        root_node: Node | None = None,
    ) -> None:
        # `using A.B` / `using A.B as C` / `import A: b, c as d` / `using .S`:
        # the local name is the alias / selected member / last path segment,
        # the target the dotted path. Relative `.`/`..` stay in the
        # import_path's text; nested-`module` bindings key under that
        # module's qn, the IMPORTS edge on the file module (#652).
        for stmt_node in captures.get(cs.CAPTURE_IMPORT, []):
            scope = self._julia_import_scope_name(stmt_node)
            map_qn = f"{module_qn}{cs.SEPARATOR_DOT}{scope}" if scope else module_qn
            if scope:
                self._julia_scope_map_keys.setdefault(module_qn, set()).add(map_qn)
            for local_name, dotted, depth in self._julia_import_pairs(stmt_node):
                target = self._julia_resolve_module_path(
                    dotted, depth, module_qn, scope
                )
                self.import_mapping.setdefault(map_qn, {})[local_name] = target
                self._host._record_import_site(module_qn, local_name, stmt_node, dotted)

    def _julia_import_pairs(self, stmt_node: Node):
        # Yields (local_name, dotted_target, depth) for every binding a
        # using/import statement creates in the current scope.
        for child in stmt_node.children:
            t = child.type
            if t == cs.TS_JULIA_SELECTED_IMPORT:
                # `A: b, c as d` — head is the module, the rest are members.
                named = child.named_children
                if not named or not named[0].text:
                    continue
                dotted, relative = self._julia_path_dotted(named[0])
                for name_node in named[1:]:
                    if name_node.type == cs.TS_JULIA_IMPORT_ALIAS:
                        alias = (
                            name_node.named_children[-1]
                            if name_node.named_children
                            else None
                        )
                        local = (
                            _julia_unmacro(alias)
                            if alias is not None
                            and alias.type == cs.TS_JULIA_MACRO_IDENTIFIER
                            else safe_decode_text(alias)
                        )
                    elif name_node.type == cs.TS_JULIA_IDENTIFIER:
                        local = safe_decode_text(name_node)
                    elif name_node.type == cs.TS_JULIA_MACRO_IDENTIFIER:
                        local = _julia_unmacro(name_node)
                    else:
                        continue
                    if local:
                        yield local, dotted, relative
            elif t == cs.TS_JULIA_IMPORT_ALIAS:
                # `using A.B as C` — local is the `as` side.
                named = child.named_children
                if len(named) < 2:
                    continue
                if named[0].type == cs.TS_JULIA_MACRO_IDENTIFIER:
                    dotted, relative = _julia_unmacro(named[0]), 0
                else:
                    dotted, relative = self._julia_path_dotted(named[0])
                # `import Base: @view as @v`: the alias may be a macro too.
                local = (
                    _julia_unmacro(named[-1])
                    if named[-1].type == cs.TS_JULIA_MACRO_IDENTIFIER
                    else safe_decode_text(named[-1])
                )
                if local:
                    yield local, dotted, relative
            elif t in (
                cs.TS_JULIA_IMPORT_PATH,
                cs.TS_JULIA_IDENTIFIER,
                cs.TS_JULIA_SCOPED_IDENTIFIER,
            ):
                # `using A.B` (and the `using A.B.*` recovery, whose
                # scoped_identifier is a direct child) — local is the last
                # path segment.
                dotted, relative = self._julia_path_dotted(child)
                if dotted:
                    yield dotted.split(cs.SEPARATOR_DOT)[-1], dotted, relative

    @staticmethod
    def _julia_path_dotted(node: Node) -> tuple[str, int]:
        # (dotted path, depth). depth is the count of leading dots: `Sibling`
        # -> 0 (project-relative), `.Sibling` -> 1 (the current module),
        # `..Sibling` -> 2 (the parent's children), and so on.
        raw = safe_decode_text(node) or ""
        depth = 0
        for ch in raw:
            if ch != cs.PATH_CURRENT_DIR:
                break
            depth += 1
        dotted = raw[depth:].replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)
        return dotted, depth

    def _julia_dir_parts(self, module_qn: str) -> list[str]:
        # The module qn is rooted at the FULL project name, which may itself
        # be dotted (a Julia repo dir is conventionally `Pkg.jl`): strip the
        # project's N segments, not just the first.
        prefix = len(self.project_name.split(cs.SEPARATOR_DOT))
        parts = module_qn.split(cs.SEPARATOR_DOT)
        return parts[prefix:-1]

    @staticmethod
    def _julia_dir_prefix_score(base: list[str], rel_file: str) -> int:
        """How many of the importer's dir segments a candidate file shares.

        A same-named module in several files resolves to the one closest to
        the IMPORTER (the nearest file is the one the import most plausibly
        means); zero shared segments means no preference, not a guess.
        """
        parts = rel_file.split(cs.SEPARATOR_SLASH)[:-1]
        score = 0
        for a, b in zip(base, parts):
            if a != b:
                break
            score += 1
        return score

    def julia_nested_pkg_root(self, path: str) -> str | None:
        # The nearest enclosing directory with its own Project.toml (the
        # nested package root), or None if the path is in the main package.
        if not path:
            return None
        p = Path(path)
        if p.is_absolute():
            try:
                p = p.relative_to(self.repo_path)
            except ValueError:
                return None
        parts = p.as_posix().split(cs.SEPARATOR_SLASH)
        for i in range(1, len(parts) + 1):
            if (self.repo_path.joinpath(*parts[:i]) / "Project.toml").is_file():
                return cs.SEPARATOR_SLASH.join(parts[:i])
        return None

    def _julia_module_index(self) -> _JuliaModuleIndex:
        """Every module declaration in the repo's Julia files, by name and
        by enclosing scope, from one parse of each file.

        A submodule need not be named after its file, so the declared name
        is often the only link to it. Nested packages are indexed too; the
        package-root boundary is enforced at lookup time.
        """
        index = self._julia_modules
        if index is None:
            index = self._julia_modules = self._scan_julia_modules()
        return index

    def _scan_julia_modules(self) -> _JuliaModuleIndex:
        index = _JuliaModuleIndex({}, {})
        parser = _julia_parser()
        if parser is None:
            return index
        try:
            for path in sorted(self.repo_path.rglob("*" + cs.EXT_JL)):
                if not path.is_file():
                    continue
                rel = path.relative_to(self.repo_path).as_posix()
                if self._julia_candidate_excluded(rel):
                    continue
                try:
                    tree = parser.parse(path.read_bytes())
                except OSError:
                    # A file that vanished or cannot be read declares no
                    # module this index could point an import at.
                    continue
                stack: list[tuple[Node, list[str]]] = [(tree.root_node, [])]
                while stack:
                    node, chain = stack.pop()
                    if node.type == cs.TS_JULIA_MODULE_DEFINITION and (
                        name := _julia_module_name(node)
                    ):
                        index.declared.setdefault(name, set()).add(rel)
                        index.scoped.setdefault(
                            (rel, cs.SEPARATOR_DOT.join(chain)), {}
                        )[name] = cs.SEPARATOR_DOT.join([*chain, name])
                        chain = [*chain, name]
                    stack.extend((child, chain) for child in node.children)
        except OSError:
            # The walk itself failed (a directory went away under it): what
            # it indexed so far stands, and an import it never reached stays
            # unresolved, as one into a file outside the repo does.
            pass
        return index

    def _julia_find_in_scope(
        self, segs: list[str], module_qn: str, scope: str
    ) -> str | None:
        # `using .X` in `module S` names a submodule of S declared in this
        # same file; the leading path segments extend the scope chain.
        if not segs:
            return None
        chain = cs.SEPARATOR_DOT.join([scope, *segs[:-1]])
        suffix = (
            self._julia_module_index()
            .scoped.get((self._julia_module_rel_file(module_qn), chain), {})
            .get(segs[-1])
        )
        return cs.SEPARATOR_DOT.join([module_qn, suffix]) if suffix else None

    def julia_member_qn_candidates(self, module_qn: str, name: str) -> list[str]:
        # A Julia function qn carries the file's DECLARED module segment
        # (`api.jl` declaring `module api` registers `proj.api.api.alpha`).
        candidates = [f"{module_qn}{cs.SEPARATOR_DOT}{name}"]
        rel_file = self._julia_module_rel_file(module_qn)
        for declared, files in self._julia_module_index().declared.items():
            if rel_file in files:
                candidates.append(
                    f"{module_qn}{cs.SEPARATOR_DOT}{declared}{cs.SEPARATOR_DOT}{name}"
                )
        return candidates

    def _julia_module_rel_file(self, module_qn: str) -> str:
        dir_parts = self._julia_dir_parts(module_qn)
        stem = module_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        return cs.SEPARATOR_SLASH.join([*dir_parts, stem + cs.EXT_JL])

    def _julia_find_by_declared_module(
        self, name: str, module_qn: str, importer_root: str | None = None
    ) -> str | None:
        if not name:
            return None
        hits = self._julia_module_index().declared.get(name)
        if hits and importer_root is not None:
            hits = {
                hit for hit in hits if self.julia_nested_pkg_root(hit) == importer_root
            }
        if not hits:
            return None
        if len(hits) == 1:
            return next(iter(hits))
        base = self._julia_dir_parts(module_qn)
        best = max(sorted(hits), key=lambda f: self._julia_dir_prefix_score(base, f))
        if self._julia_dir_prefix_score(base, best) > 0:
            return best
        return None

    def _julia_find_by_path(
        self, dotted: str, depth: int, module_qn: str, importer_root: str | None = None
    ) -> str | None:
        segs = dotted.split(cs.SEPARATOR_DOT)
        if not segs or not segs[0]:
            return None
        if depth > 0:
            # `.S` resolves from the importer's dir; each `..` climbs one.
            # Climbing above the project root names no file in this repo.
            dir_parts = self._julia_dir_parts(module_qn)
            climb = depth - 1
            if climb > len(dir_parts):
                return None
            bases = (dir_parts[: len(dir_parts) - climb],)
        else:
            bases = ((), ("src",))
        candidates: set[str] = set()
        for base in bases:
            variants = [segs, [s.lower() for s in segs]]
            if len(segs) > 1:
                variants.append(segs[:-1] + [segs[-1].lower()])
            for variant in variants:
                candidates.add("/".join(list(base) + variant) + cs.EXT_JL)
        found = sorted(
            c
            for c in candidates
            if (self.repo_path / c).is_file()
            and not self._julia_candidate_excluded(c)
            and (
                importer_root is None or self.julia_nested_pkg_root(c) == importer_root
            )
        )
        return found[0] if found else None

    def _julia_candidate_excluded(self, rel_file: str) -> bool:
        # The same exclude/unignore predicate the indexer walks with, so an
        # excluded generated module cannot win import resolution.
        parts = rel_file.split(cs.SEPARATOR_SLASH)
        return should_skip_rel_file(
            rel_file,
            tuple(parts[:-1]),
            exclude_paths=self._host.exclude_paths,
            unignore_paths=self._host.unignore_paths,
        )

    def _julia_find_by_name(
        self, name: str, module_qn: str | None = None, importer_root: str | None = None
    ) -> str | None:
        if not name:
            return None
        index = self._julia_file_stem_index()
        base = self._julia_dir_parts(module_qn) if module_qn else ()

        for key in dict.fromkeys((name, name.lower(), _julia_snake_case(name))):
            hits = index.get(key)
            if hits and importer_root is not None:
                hits = {
                    hit
                    for hit in hits
                    if self.julia_nested_pkg_root(hit) == importer_root
                }
            if not hits:
                continue
            if len(hits) == 1:
                return next(iter(hits))
            if base:
                best = max(
                    sorted(hits),
                    key=lambda f: self._julia_dir_prefix_score(base, f),
                )
                if self._julia_dir_prefix_score(base, best) > 0:
                    return best
        return None

    def _julia_file_stem_index(self) -> dict[str, set[str]]:
        if (cached := self._julia_file_stems) is not None:
            return cached
        index: dict[str, set[str]] = {}
        try:
            for path in self.repo_path.rglob("*" + cs.EXT_JL):
                if not path.is_file():
                    continue
                rel = path.relative_to(self.repo_path).as_posix()
                if self._julia_candidate_excluded(rel):
                    continue
                stem = path.stem
                for key in dict.fromkeys((stem, stem.lower(), _julia_snake_case(stem))):
                    index.setdefault(key, set()).add(rel)
        except OSError:
            # As in `_scan_julia_modules`: the stems seen before the walk
            # failed stand, and a file it never reached matches no import.
            pass
        self._julia_file_stems = index
        return index

    def _julia_resolve_module_path(
        self,
        dotted: str,
        depth: int,
        module_qn: str,
        scope: str | None = None,
    ) -> str:
        # Module qns are rooted at the FULL project name (a dotted repo dir
        # like `Symbolics.jl` keeps both segments).
        project_root = self.project_name
        # `using .First` in `module Outer` names a submodule of `Outer`,
        # not a sibling file (in-file submodules have no path of their own).
        if depth > 0 and scope is not None:
            segs = dotted.split(cs.SEPARATOR_DOT)
            if target_qn := self._julia_find_in_scope(segs, module_qn, scope):
                return target_qn
        # Relative imports stay inside the importer's package root.
        importer_root = (
            self.julia_nested_pkg_root(
                cs.SEPARATOR_SLASH.join(self._julia_dir_parts(module_qn))
            )
            if depth > 0
            else None
        )
        # Climbing above the project root names no file in this repo.
        over_climb = depth > 1 and depth - 1 > len(self._julia_dir_parts(module_qn))
        # An absolute dotted import names an external package unless it
        # starts with this project's own name (a self-reference).
        self_ref = cs.SEPARATOR_DOT in dotted and self._julia_current_package_prefix(
            dotted
        )
        # The exact-path lookup is a first-party link only: an absolute
        # dotted import names an external package unless it starts with
        # this project's own name (issue #1882).
        rel_file = (
            self._julia_find_by_path(dotted, depth, module_qn, importer_root)
            if depth > 0 or self_ref
            else None
        )
        if rel_file is None and not over_climb and (depth > 0 or self_ref):
            rel_file = self._julia_find_by_name(
                dotted.rsplit(cs.SEPARATOR_DOT, maxsplit=1)[-1],
                module_qn,
                importer_root,
            )
        if (
            rel_file is None
            and not over_climb
            and (depth > 0 or cs.SEPARATOR_DOT not in dotted or self_ref)
        ):
            # The declared name may match neither the file path nor the
            # file stem: the declared-module index is the last first-party
            # link before externalising.
            rel_file = self._julia_find_by_declared_module(
                dotted.rsplit(cs.SEPARATOR_DOT, maxsplit=1)[-1],
                module_qn,
                importer_root,
            )
        if rel_file is not None:
            rel_module = rel_file[: -len(cs.EXT_JL)].replace(
                cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT
            )
            return f"{project_root}{cs.SEPARATOR_DOT}{rel_module}"
        if depth > 0 and not over_climb:
            # No file matched: build the qn the relative path points at,
            # climbing the importer's dir tree by the path's depth.
            dir_parts = self._julia_dir_parts(module_qn)
            climb = depth - 1
            base = dir_parts[: len(dir_parts) - climb]
            parts = base + dotted.split(cs.SEPARATOR_DOT)
            return f"{project_root}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(parts)}"
        return dotted  # external (stdlib / package)

    def _julia_current_package_prefix(self, dotted: str) -> bool:
        # An absolute dotted import starts with this package's own name: the
        # project name, or the checkout directory's (conventionally `Pkg.jl`,
        # the `.jl` optional). A dotted directory no longer shares its name
        # with the project, which gets a derived one instead (#2412).
        names = {self.project_name.lower(), self.repo_path.name.lower()} - {""}
        suffix = cs.EXT_JL.lower()
        prefixes = {p for name in names for p in (name, name.removesuffix(suffix))}
        dotted_l = dotted.lower()
        return any(
            dotted_l == prefix or dotted_l.startswith(prefix + cs.SEPARATOR_DOT)
            for prefix in prefixes
        )
