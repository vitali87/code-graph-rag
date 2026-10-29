import importlib
import os
import posixpath
import re
from collections.abc import Mapping
from functools import cache, lru_cache
from pathlib import Path
from typing import NamedTuple

from loguru import logger
from tree_sitter import Language, Node, Parser, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..language_spec import (
    LANGUAGE_SPECS,
    LanguageSpec,
    get_language_for_extension,
    language_family,
    own_extension_module_qn,
)
from ..node_text import safe_decode_text, safe_decode_with_fallback, sorted_captures
from ..services import IngestorProtocol
from ..types_defs import (
    DeferredImportEdge,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageFamily,
    LanguageQueries,
    PropertyDict,
    StemSiblingModules,
)
from ..utils.path_utils import (
    base_module_qn,
    declaration_extension,
    module_extension,
    should_skip_rel_file,
)
from .cpp.qn import build_module_qn_map
from .csharp.imports import CSharpImporter
from .dart.imports import DartImporter
from .go import discover_go_module_paths, resolve_go_import_path
from .java.imports import JavaImporter
from .js_ts.imports import JsTsImporter
from .js_ts.reexports import js_ts_module_qn
from .lua import utils as lua_utils
from .php.imports import PhpImporter
from .python.source_roots import discover_python_source_roots, resolve_via_source_roots
from .rust.imports import RustImporter
from .scala.imports import ScalaImporter
from .stdlib_extractor import (
    StdlibCacheStats,
    StdlibExtractor,
    clear_stdlib_cache,
    flush_stdlib_cache,
    get_stdlib_cache_stats,
)
from .utils import follow_reexports, import_site_props, is_project_qn, shared_or_new

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


def _names_js_module(site: PropertyDict | None) -> bool:
    # `export * from` and `import * as ns` record `*` as their imported name,
    # and `const m = require("./m")` records none: each names the module
    # itself, never a name inside it (issues #2464, #2934).
    if site is None:
        return False
    imported = site.get(cs.KEY_IMPORTED_NAME)
    return imported is None or imported == cs.IMPORTED_NAME_WILDCARD


def _is_conditional_import_node(import_node: Node) -> bool:
    # An import nested under an if/try (click's platform-conditional
    # `if WIN: from ._winconsole import X ... else: def X(...)`) binds its
    # name only on some runtime paths; a same-named local def is then a
    # mutually-exclusive fallback variant, not shadowed dead code.
    current = import_node.parent
    while current is not None:
        if current.type in (cs.TS_PY_IF_STATEMENT, cs.TS_PY_TRY_STATEMENT):
            return True
        current = current.parent
    return False


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


def _external_module_name(module_path: str) -> str:
    """The display name for an ExternalModule qn.

    The last DOTTED segment names a package path (`os.path` -> `path`,
    `std.fmt` -> `fmt`), but a C/C++ header carries its EXTENSION in that
    position, so every `.h` include minted an ExternalModule called `h`:
    `std.stdio.h`, `std.signal.h` and `std.sys.types.h` alike (issue #1758).
    A header is named by its own stem instead, the same name
    `_cpp_include_local_name` binds it under, so the node and the local name
    agree.

    The header rule is deliberately confined to the `std.` prefix that
    `_cpp_include_full_name` puts on an include, AND to qns of three or more
    segments. This function serves EVERY language's external imports, and a
    package whose last segment is literally `h` is a real thing (the Python
    HTTP/2 library), so an unconditional rule would rename `mypkg.h` to
    `mypkg` for languages that have no header extensions at all.

    The three-segment floor leaves one known inconsistency: `<std.h>` has the
    two-segment qn `std.h`, indistinguishable here from a package `h` under
    `std`, so its node is named `h` while `_cpp_include_local_name` binds it
    as `std`. Lowering the floor to two would rename every `<pkg.h>`-shaped
    external of every language; `<std.h>` is not a real header, so the
    inconsistency is the cheaper of the two errors.

    A second, unfixable-here case: this sees only the qn, and slash
    segmentation has already erased the difference between a PATH and dots
    inside one basename. `<foo/bar.h>` and `<foo.bar.h>` both become
    `std.foo.bar.h`, yet `_cpp_include_local_name` binds the first as `bar`
    (basename stem) and the second as `foo` (first stem segment). One name
    must therefore disagree with its binding, and no rule at this layer can
    tell which. It is `<foo/bar.h>` -- a real path, the common shape -- that
    is served correctly; `<foo.bar.h>` gets `bar` where its binding says
    `foo`. Fixing that means passing the ORIGINAL include path down to node
    creation rather than deriving from the qn, which is a wider change than
    the naming defect warrants (#1758).
    """
    segments = module_path.split(cs.SEPARATOR_DOT)
    # `stdio.h` occupies the last TWO dotted segments, stem and extension.
    if (
        len(segments) >= 3
        and module_path.startswith(cs.IMPORT_STD_PREFIX)
        and f"{cs.SEPARATOR_DOT}{segments[-1]}" in (cs.EXT_H, cs.EXT_HPP)
    ):
        return segments[-2]
    return segments[-1]


def _cpp_include_local_name(include_path: str) -> str:
    """The name the include binds locally: the header's stem for `.h`/`.hpp`,
    the bare last path segment otherwise (`<vector>`)."""
    header_name = include_path.rsplit(cs.SEPARATOR_SLASH, maxsplit=1)[-1]
    if header_name.endswith(cs.EXT_H) or header_name.endswith(cs.EXT_HPP):
        return header_name.split(cs.SEPARATOR_DOT)[0]
    return header_name


def _lua_relative_module(import_path: str, current_module: str) -> str:
    # Resolve `./x` / `../x` against the importing module's package.
    parts = current_module.split(cs.SEPARATOR_DOT)[:-1]
    for p in import_path.replace("\\", cs.SEPARATOR_SLASH).split(cs.SEPARATOR_SLASH):
        if p == cs.PATH_PARENT_DIR:
            if parts:
                parts.pop()
        elif p and p != cs.PATH_CURRENT_DIR:
            parts.append(p)
    return cs.SEPARATOR_DOT.join(parts)


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


class ImportProcessor:
    __slots__ = (
        "repo_path",
        "project_name",
        "ingestor",
        "function_registry",
        "exclude_paths",
        "unignore_paths",
        "import_mapping",
        "commonjs_direct_exports",
        "conditional_imports",
        "python_import_rebinds",
        "stdlib_extractor",
        "_is_local_module_cached",
        "_project_named_package",
        "_map_py_source_root",
        "_map_go_import_path",
        "_cpp_module_qn_map",
        "_cpp_qn_to_rel",
        "_julia_file_stems",
        "_julia_modules",
        "_julia_scope_map_keys",
        "_deferred_import_edges",
        "unresolved_references",
        "_import_sites",
        "_import_site_owners",
        "_import_site_writers",
        "_cpp_declaration_mappings",
        "_cpp_shadowed_include_targets",
        "rust",
        "js_ts",
        "java",
        "scala",
        "csharp",
        "dart",
        "php",
    )

    def __init__(
        self,
        repo_path: Path,
        project_name: str,
        ingestor: IngestorProtocol | None = None,
        function_registry: FunctionRegistryTrieProtocol | None = None,
        exclude_paths: frozenset[str] | None = None,
        unignore_paths: frozenset[str] | None = None,
        module_qn_to_file_path: dict[str, Path] | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.project_name = project_name
        self.ingestor = ingestor
        self.function_registry = function_registry
        # The same sets the indexer walks with, so the redirect sweep sees
        # exactly the files the graph holds (issue #1088).
        self.exclude_paths = exclude_paths
        self.unignore_paths = unignore_paths
        self.import_mapping: dict[str, dict[str, str]] = {}
        # CommonJS modules whose ENTIRE export is one function
        # (`module.exports = function (...) {...}`): module qn -> the
        # exported function's qn, so a whole-module require alias called
        # directly (`const f = require('./m'); f(x)`) resolves to it.
        self.commonjs_direct_exports: dict[str, str] = {}
        # Names bound by a CONDITIONAL Python import (nested under if/try --
        # click's `if WIN: from ._winconsole import X`): the dead-code fan-out
        # treats a same-named local def as the mutually-exclusive fallback
        # variant ONLY for these; an unconditional import is plain shadowing.
        self.conditional_imports: dict[str, set[str]] = {}
        # Every target a Python name was bound to, for names its module's
        # imports bind more than once (`try: import tomllib` / `except
        # ImportError: import tomli as tomllib`). `import_mapping` keeps one
        # binding, and plain imports are handled before from-imports whatever
        # their source order, so the resolver needs all of them to tell a
        # module that is external on every path from one a branch binds to
        # first-party code (issue #2360).
        self.python_import_rebinds: dict[str, dict[str, set[str]]] = {}
        # Lazy: replayed walk of every eligible repo file, built on the first C++
        # include so non-C++ projects never pay for it.
        self._cpp_module_qn_map: dict[str, str] | None = None
        self._cpp_qn_to_rel: dict[str, str] = {}
        # File module -> its nested-scope map keys; read unguarded on the
        # re-ingest path, which never runs the per-run cache resets.
        self._julia_scope_map_keys: dict[str, set[str]] = {}
        # Filesystem-derived, built on the first Julia import that needs them
        # and dropped by reset_julia_path_caches.
        self._julia_file_stems: dict[str, set[str]] | None = None
        self._julia_modules: _JuliaModuleIndex | None = None
        # IMPORTS edges held back until every file is parsed, so internal
        # targets verify against the full module registry (issue #652).
        self._deferred_import_edges: list[DeferredImportEdge] = []
        # Per module qn: every name the module referenced and could not
        # resolve, from any pass (a dropped import, an include naming no
        # file, a base class resolved nowhere, a call with no callee). The
        # class and call processors record into the same dict; the updater
        # writes it onto the Module node after Pass 3, and matches an added
        # file against it to find the modules that waited (issue #1568).
        self.unresolved_references: dict[str, set[str]] = {}
        # Per scope qn: the site props of each bound import name (#1522),
        # attached to the IMPORTS edge when the deferred edge flushes.
        self._import_sites: dict[str, dict[str, PropertyDict]] = {}
        # Sub-scope site entries a file wrote under OTHER scope keys (Rust
        # fn/inline-mod uses), so its re-parse can retract them (#1522).
        self._import_site_owners: dict[str, set[tuple[str, str]]] = {}
        # (scope, name) -> the file module that wrote the CURRENT entry, so a
        # re-parse retracts only entries it still owns: a fn-local inline mod
        # in src/a.rs and src/a/b/mod.rs can share a scope key and a name.
        self._import_site_writers: dict[tuple[str, str], str] = {}
        # Rust's `use` parsing, its Cargo crate, workspace and target layout,
        # and the per-scope maps the call resolver reads: the importer writes
        # the import map and sites above and queues its edges through this
        # processor, whose flush verifies them like every language's.
        self.rust = RustImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
            import_sites=self._import_sites,
        )
        # Java's import and `import static` parsing and the source-root
        # layout a local import's qn carries: the importer writes the import
        # map above and records its sites through this processor.
        self.java = JavaImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
        )
        # Scala's import parsing and the package index its imports resolve
        # through once every module is known: the importer writes the import
        # map and reads the sites above, and queues its edges through this
        # processor, whose flush lands the project's own on their modules.
        # C#'s `using` parsing, each file's declared namespaces and the
        # evidence that pins a namespace `using` to the modules it uses: the
        # importer writes the import map above and records its sites and
        # requeued edges through this processor, whose flush emits them.
        self.csharp = CSharpImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
        )
        self.scala = ScalaImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
            import_sites=self._import_sites,
        )
        # Dart's directive parsing, its `as` prefixes and barrel exports, and
        # the repo's own pubspec packages: the importer writes the import map
        # above and records its sites through this processor.
        self.dart = DartImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
        )
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
        # PHP's `use` and include/require parsing and each module's declared
        # namespace: the importer writes the import map above and records its
        # sites through this processor.
        self.php = PhpImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
        )
        self.stdlib_extractor = StdlibExtractor(
            function_registry, repo_path, project_name
        )

        repo_is_package = (repo_path / cs.INIT_PY).is_file()

        # A repo whose top-level package shares the repo's name (a django
        # clone is django/django, celery is celery/celery) makes written
        # absolute imports collide with the project prefix: `django.http`
        # names the package dir and needs the project prefix on top
        # (django.django.http), while in a flat layout (repo root doubles
        # as the installed package) the written path already IS the qn.
        self._project_named_package = (
            not repo_is_package and (repo_path / project_name).is_dir()
        )

        # Python packages under nested source roots (src-layout, monorepo packages,
        # pyproject package-dir remaps) are importable by a name that differs from
        # their repo-relative path, so absolute imports of them cannot resolve by the
        # import-name == path assumption. Discover the name -> dotted-path map once
        # so those imports resolve first-party.
        py_source_roots = discover_python_source_roots(repo_path)

        @lru_cache(maxsize=4096)
        def _map_py_source_root_cached(module_name: str) -> str | None:
            return resolve_via_source_roots(repo_path, py_source_roots, module_name)

        self._map_py_source_root = _map_py_source_root_cached

        # Go import paths are module-path-prefixed (github.com/acme/tool/pkg), never
        # repo-relative, so no local Go import resolves by the name == path
        # assumption. Map each go.mod module directive to its directory once so local
        # imports rewrite to project-prefixed qns and unmapped (external) paths stay
        # recognisably slash-separated.
        go_module_paths = discover_go_module_paths(repo_path)

        @lru_cache(maxsize=4096)
        def _map_go_import_path_cached(import_path: str) -> str | None:
            return resolve_go_import_path(go_module_paths, import_path)

        self._map_go_import_path = _map_go_import_path_cached

        # JS/TS's import and `require` parsing, its tsconfig and workspace
        # layout, and the per-module export tables and package names the call
        # resolver reads: the importer writes the import map above and records
        # its sites through this processor, whose flush verifies its edges
        # like every language's. Built after the Python and Go layouts above,
        # so their discoveries log before its workspace discovery does.
        self.js_ts = JsTsImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
            module_qn_to_file_path=shared_or_new(module_qn_to_file_path, dict),
        )

        @lru_cache(maxsize=4096)
        def _is_local_module_cached(module_name: str) -> bool:
            # When the repo root is itself a package, its children are importable
            # only under the package name (project_name.child), never as bare
            # top-level names, so a bare top-level import resolves externally.
            if repo_is_package:
                return module_name == project_name
            # A top-level stub with no `.py` is a local module too: it is what
            # a compiled extension's definitions are indexed from (#2445).
            return (repo_path / module_name).is_dir() or any(
                (repo_path / f"{module_name}{ext}").is_file()
                for ext in cs.PY_EXTENSIONS
            )

        self._is_local_module_cached = _is_local_module_cached

    @staticmethod
    def flush_stdlib_cache() -> None:
        flush_stdlib_cache()

    @staticmethod
    def clear_stdlib_cache() -> None:
        clear_stdlib_cache()

    @staticmethod
    def get_stdlib_cache_stats() -> StdlibCacheStats:
        return get_stdlib_cache_stats()

    def _clear_module_import_state(self, module_qn: str) -> None:
        """Drop everything a previous parse of this module recorded.

        Extracted from `parse_imports` to keep it under the cognitive
        complexity limit (S3776); the grouping is also the honest one, since
        these four writes share a single invariant: a re-parse must carry
        nothing the edited file no longer says.
        """
        self.import_mapping[module_qn] = {}
        self.scala.drop_module_imports(module_qn)
        self.csharp.csharp_static_imports.pop(module_qn, None)
        self.csharp.csharp_global_static_imports.pop(module_qn, None)
        self.java.java_static_imports.pop(module_qn, None)
        self._julia_scope_map_keys.pop(module_qn, None)
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
        # Re-derived from the export statements the re-parse finds.
        self.js_ts.js_export_bindings.pop(module_qn, None)
        # The Dart prefix maps share the same invariant. Both are written
        # only when the file HAS prefixed imports, so removing the last one
        # left the previous parse's entries in place: a stale alias kept
        # resolving a name the file no longer binds, and a stale shadow span
        # kept suppressing a fold at a line that had moved (Copilot,
        # PR #2040).
        self.dart.dart_prefix_shadows.pop(module_qn, None)
        self.dart.dart_import_aliases.pop(module_qn, None)
        self.dart.dart_exposed_libraries.pop(module_qn, None)
        self._retract_import_sites(module_qn)

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

    def _defer_module_import_edges(
        self, module_qn: str, language: cs.SupportedLanguage
    ) -> None:
        """Queue this module's IMPORTS edges for the post-parse flush.

        Extracted from `parse_imports` to keep it under the cognitive
        complexity limit (S3776). The two loops here -- the mapping's own
        bindings and the includes a later binding displaced -- are the
        densest branching in that function, and nesting multiplies the
        cost, so moving them out is what actually reduces it; the earlier
        extraction of straight-line assignments did not.
        """
        # Hold the edges back: an internal target is only real if some file
        # yields that module qn, known only after every file is parsed
        # (flush_deferred_import_edges).
        sites = self._import_sites.get(module_qn, {})
        # Nested Julia `module` bindings are keyed under `module_qn.scope`,
        # but the IMPORTS edge stays on the file module (#652). The explicit
        # registry keeps a sibling FILE module whose qn is a prefix
        # (`proj.a.b` beside `proj.a`) from leaking in.
        map_keys = {module_qn, *self._julia_scope_map_keys.get(module_qn, ())}
        for map_key in map_keys:
            bindings = self.import_mapping.get(map_key)
            if not bindings:
                continue
            for local_name, full_name in bindings.items():
                if (module_qn, full_name) in self._cpp_declaration_mappings:
                    continue
                if full_name == cs.RUST_UNRESOLVABLE_QN:
                    # The unrepresentable-#[path] sentinel names no module;
                    # never let it become a phantom IMPORTS edge (#1082).
                    continue
                self._deferred_import_edges.append(
                    DeferredImportEdge(
                        module_qn=module_qn,
                        full_name=full_name,
                        language=language,
                        site=sites.get(local_name),
                    )
                )
        # Includes whose local binding a later include took over: the
        # file includes them, so they keep their edge even though the
        # map no longer names them (issue #1758). No site: the binding
        # that carried it belongs to the include that won the name.
        for module_key, shadowed in self._cpp_shadowed_include_targets:
            if module_key != module_qn:
                continue
            if (module_qn, shadowed) in self._cpp_declaration_mappings:
                # The displaced binding can be one `export module X;`
                # wrote, not an include: re-emitting it here would
                # rebuild the self-import the main loop above filters
                # out, which `test_cpp_module_declarations_emit_no_
                # self_import` forbids (#1758).
                continue
            self._deferred_import_edges.append(
                DeferredImportEdge(
                    module_qn=module_qn,
                    full_name=shadowed,
                    language=language,
                    site=None,
                )
            )

    def parse_imports(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        pre_captures: dict[str, list[Node]] | None = None,
        *,
        defer_edges: bool = True,
    ) -> None:
        if language not in queries:
            return
        imports_query = queries[language]["imports"]
        if not imports_query:
            return

        lang_config = queries[language]["config"]

        self._clear_module_import_state(module_qn)
        self.csharp.drop_resolved_cross_module_uses(module_qn)
        if language == cs.SupportedLanguage.CSHARP:
            self.csharp.scan_module(module_qn, root_node)
        elif language == cs.SupportedLanguage.SCALA:
            self.scala.record_package_scan(module_qn, root_node)
        # A re-parsed module that no longer directly exports one function
        # must not leave the stale whole-module alias mapping behind.
        self.commonjs_direct_exports.pop(module_qn, None)
        # Reset per-module PHP use-function state too, so a re-index that drops a
        # `use function` import does not leave a stale exemption behind.
        self.php.php_function_imports.pop(module_qn, None)
        # A re-index that removes or edits the `namespace` declaration must not
        # leave the old namespace bound to this module.
        self.php.php_module_namespaces.pop(module_qn, None)
        self.js_ts.js_ts_bare_imports.pop(module_qn, None)
        self.js_ts.js_ts_package_imports.pop(module_qn, None)
        # A re-parse that drops a fallback import must not leave the binding
        # it removed standing beside the one that remains.
        self.python_import_rebinds.pop(module_qn, None)
        # A re-parse re-derives these from the current source, so the previous
        # run's specifiers must not survive: an import the edit removed, or one
        # whose target now exists, would otherwise keep nominating this file
        # for ever (issue #1714).
        self.js_ts.unresolved_specifiers.pop(module_qn, None)
        self.unresolved_references.pop(module_qn, None)

        try:
            if pre_captures is not None:
                captures = pre_captures
            else:
                cursor = QueryCursor(imports_query)
                captures = sorted_captures(cursor, root_node)

            match language:
                case cs.SupportedLanguage.PYTHON:
                    self._parse_python_imports(captures, module_qn)
                case (
                    cs.SupportedLanguage.JS
                    | cs.SupportedLanguage.TS
                    | cs.SupportedLanguage.TSX
                ):
                    self.js_ts.parse_imports(captures, module_qn, root_node)
                case cs.SupportedLanguage.JAVA:
                    self.java.parse_imports(captures, module_qn)
                case cs.SupportedLanguage.RUST:
                    self.rust.parse_imports(captures, module_qn)
                case cs.SupportedLanguage.GO:
                    self._parse_go_imports(captures, module_qn)
                # C shares the include grammar and the resolver: without its
                # own arm here a `.c` file's `#include "foo.h"` never reached
                # the import map, so it emitted no IMPORTS edge while a `.h`
                # including the same header (parsed as C++) did (issue #1654).
                case cs.SupportedLanguage.CPP | cs.SupportedLanguage.C:
                    self._parse_cpp_imports(captures, module_qn)
                case cs.SupportedLanguage.LUA:
                    self._parse_lua_imports(captures, module_qn)
                case cs.SupportedLanguage.JULIA:
                    self._parse_julia_imports(captures, module_qn)
                case cs.SupportedLanguage.PHP:
                    self.php.parse_imports(captures, module_qn, root_node)
                case cs.SupportedLanguage.CSHARP:
                    self.csharp.parse_imports(captures, module_qn)
                case cs.SupportedLanguage.DART:
                    self.dart.parse_imports(captures, module_qn, root_node)
                case cs.SupportedLanguage.SCALA:
                    self.scala.parse_imports(captures, module_qn)
                case _:
                    self._parse_generic_imports(captures, lang_config)

            logger.debug(
                ls.IMP_PARSED_COUNT,
                count=len(self.import_mapping[module_qn]),
                module=module_qn,
            )

            if self.ingestor and defer_edges:
                self._defer_module_import_edges(module_qn, language)
        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            logger.warning(ls.IMP_PARSE_FAILED, module=module_qn, error=e)

    def _retract_import_sites(self, module_qn: str) -> None:
        # A re-parse must not keep the sub-scope site entries the previous
        # parse of this file wrote under other scope keys (issue #1522).
        for scope_qn, name in self._import_site_owners.pop(module_qn, ()):
            if self._import_site_writers.get((scope_qn, name)) != module_qn:
                # Another file wrote the live entry since; it is theirs.
                continue
            self._import_site_writers.pop((scope_qn, name), None)
            scope_sites = self._import_sites.get(scope_qn)
            if scope_sites is None:
                continue
            scope_sites.pop(name, None)
            if not scope_sites:
                del self._import_sites[scope_qn]
        # The file's own scope key may also hold entries an inline module of
        # ANOTHER file wrote (a file-backed `a/b/mod.rs` and an inline `mod b`
        # in `a.rs` share `p.src.a.b`): keep those, drop only this file's.
        own = self._import_sites.get(module_qn, {})
        mine = [
            name
            for name in own
            if self._import_site_writers.get((module_qn, name), module_qn) == module_qn
        ]
        for name in mine:
            del own[name]
            self._import_site_writers.pop((module_qn, name), None)
        self._import_sites[module_qn] = own

    def _record_import_site(
        self,
        scope_qn: str,
        local_name: str,
        node: Node | None,
        imported_name: str | None = None,
        owner_qn: str | None = None,
    ) -> PropertyDict | None:
        """Remember where an import binding was written (issue #1522).

        `node` is the import statement (for a grouped Go block, the spec line)
        whose span the IMPORTS edge will carry. Sentinel keys (`*pkg`
        wildcards, Go `.pkg` dot-imports) bind no name and record no alias.
        `owner_qn` names the FILE module when `scope_qn` is one of its
        sub-scopes, so the file's re-parse retracts the entry.
        """
        if node is None:
            return None
        if owner_qn is not None and owner_qn != scope_qn:
            self._import_site_owners.setdefault(owner_qn, set()).add(
                (scope_qn, local_name)
            )
        # A file-level record is written by the scope's own file.
        self._import_site_writers[(scope_qn, local_name)] = (
            owner_qn if owner_qn is not None else scope_qn
        )
        site = import_site_props(node, local_name, imported_name)
        self._import_sites.setdefault(scope_qn, {})[local_name] = site
        return site

    def defer_import_edge(
        self,
        module_qn: str,
        full_name: str,
        language: cs.SupportedLanguage,
        site: PropertyDict | None = None,
    ) -> None:
        # Entry point for import shapes discovered outside parse_imports (the
        # CommonJS destructuring fallback); every IMPORTS edge goes through the same
        # deferred verification.
        if full_name == cs.RUST_UNRESOLVABLE_QN:
            # The sentinel names a module the qn scheme cannot key (an
            # unrepresentable #[path] target): it has no referent, so it must
            # never become a phantom ExternalModule IMPORTS edge (issue #1082).
            return
        self._deferred_import_edges.append(
            DeferredImportEdge(
                module_qn=module_qn, full_name=full_name, language=language, site=site
            )
        )

    def drop_cpp_module_import_state(self, module_qn: str) -> None:
        """Retract a deleted C/C++ module without leaving an empty import mapping."""
        self._clear_module_import_state(module_qn)
        self.import_mapping.pop(module_qn, None)
        self._cpp_module_qn_map = None
        self._cpp_qn_to_rel.clear()

    def restore_unparsed_imports(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        function_locations: Mapping[FunctionSpanKey, FunctionLocation],
    ) -> None:
        """Rebuild the import state of a module this run did not parse.

        A re-parsed file types `from pkg import Client` by following the
        re-export through `pkg`'s own import map, and an incremental run
        parses only CHANGED files, so an unchanged `pkg` had no map and the
        receiver fell to a name-only guess (issue #2559). The module's IMPORTS
        edges are still in the graph, so none are queued for the flush.
        """
        self.parse_imports(root_node, module_qn, language, queries, defer_edges=False)
        if language == cs.SupportedLanguage.RUST:
            # The tail a parsed Rust file gets from the definition pass: body
            # `use`s key on their functions' registered qns, and inline-mod
            # maps wait for the flush-time arbitration like every file's.
            self.rust.finalise_rust_function_scope_uses(module_qn, function_locations)
            self.rust.retract_rust_mod_scope_uses(module_qn)

    def _emit_csharp_internal_imports(
        self,
        entry: DeferredImportEdge,
        declaring: dict[str, set[str]],
        known_module_qns: set[str],
    ) -> int:
        """IMPORTS edges for a `using` of a namespace declared in the repo.

        A `using` names a NAMESPACE. When indexed modules declare it, the edge
        lands on the specific modules this file actually uses: either a
        resolved call/reference/inherit crossed into the module, or the file
        mentions a type name the module declares under that namespace. A
        namespace-keyed ExternalModule would be a dead end no edge ever
        connects to the defining files (issue #1347).
        """
        if self.ingestor is None:
            return 0
        emitted = 0
        for target_module_qn in self.csharp.using_targets(entry.module_qn, declaring):
            if target_module_qn not in known_module_qns:
                continue
            self._emit_import_edge(entry, cs.NodeLabel.MODULE, target_module_qn)
            emitted += 1
        return emitted

    def _emit_import_edge(
        self, entry: DeferredImportEdge, target_label: str, target_qn: str
    ) -> None:
        if self.ingestor is None:
            return
        from_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, entry.module_qn)
        to_spec = (target_label, cs.KEY_QUALIFIED_NAME, target_qn)
        if entry.site:
            self.ingestor.ensure_relationship_batch(
                from_spec, cs.RelationshipType.IMPORTS, to_spec, properties=entry.site
            )
        else:
            self.ingestor.ensure_relationship_batch(
                from_spec, cs.RelationshipType.IMPORTS, to_spec
            )

    def drop_deferred_import_edges(
        self, module_qns: set[str], language: cs.SupportedLanguage
    ) -> None:
        # Unflushed `language` edges queued by these modules, for an
        # importer that queues them again per binding (Scala's requeue).
        self._deferred_import_edges = [
            entry
            for entry in self._deferred_import_edges
            if entry.module_qn not in module_qns or entry.language != language
        ]

    def flush_deferred_import_edges(self, known_module_paths: dict[str, str]) -> int:
        """Emit IMPORTS edges now that every file is parsed.

        An external target gets its ExternalModule node as before. An internal
        target must verify against the real module qns (with their file paths
        for language tie-breaking); a guess that resolves nowhere (a broken
        import, a directory with no index module, a crate path resolved from
        the wrong root) emits no edge, because the phantom endpoint is
        silently dropped by the database anyway.
        """
        # Rust sub-scope maps normally commit earlier (run() finalises
        # before the deferred inheritance pass reads them); flushing
        # drains any pendings a direct caller queued without that pass.
        self.rust.finalise_rust_mod_scope_uses(known_module_paths)
        deferred = self._deferred_import_edges
        if not deferred or self.ingestor is None:
            return 0
        self._deferred_import_edges = []
        known_module_qns = set(known_module_paths)
        module_aliases = self._module_alias_map(known_module_qns)
        siblings = self.stem_sibling_modules(known_module_paths)
        # namespace -> the indexed modules DECLARING it, from the per-file
        # scan done at parse time: C# namespaces need not mirror directory
        # names, so path-based guessing cannot answer "is this internal". An
        # incremental run only re-parses CHANGED files, so unchanged C#
        # modules recover their namespaces here from a targeted parse of
        # their source, or the internal detection would silently fail in
        # watch mode and resurrect the dead-end ExternalModule.
        self.csharp.recover_unparsed_csharp_state(known_module_paths)
        csharp_namespace_modules = self.csharp.csharp_namespace_modules(
            known_module_qns
        )
        emitted = 0
        for entry in deferred:
            # `from pkg.transport import TTransport` is ambiguous in
            # Python: TTransport may be an item OR a submodule. The stdlib
            # extractor strips it as an item, anchoring the edge at the
            # package; when the FULL dotted name verifies as a real module,
            # the submodule is the true target.
            if entry.language == cs.SupportedLanguage.PYTHON and (
                full_target := self._verify_internal_import_target(
                    entry.full_name,
                    known_module_paths,
                    module_aliases,
                    entry.language,
                    siblings,
                )
            ):
                self._emit_import_edge(entry, cs.NodeLabel.MODULE, full_target)
                emitted += 1
                continue
            if self._is_rust_project_import(entry):
                emitted += self._flush_rust_project_import(
                    entry, known_module_paths, module_aliases, siblings
                )
                continue
            if entry.language == cs.SupportedLanguage.JULIA and (
                entry.full_name == self.project_name
                or entry.full_name.startswith(f"{self.project_name}{cs.SEPARATOR_DOT}")
            ):
                # A Julia `using`/`import` target resolved to a file-based
                # project qn at parse time: verify it against the real module
                # qns, not the generic resolver (which would externalise the
                # bare project name).
                candidate = entry.full_name
                importer_pkg = self._julia_nested_pkg_root(
                    known_module_paths.get(entry.module_qn, "")
                )
                target = None
                while True:
                    target = self._verify_internal_import_target(
                        candidate, known_module_paths, module_aliases, entry.language
                    )
                    target_pkg = (
                        self._julia_nested_pkg_root(known_module_paths.get(target, ""))
                        if target is not None
                        else None
                    )
                    if target_pkg is not None and target_pkg != importer_pkg:
                        # A target in ANOTHER nested package is unreachable
                        # from here; one in the SAME nested package is a
                        # normal sibling import (issue #1882).
                        target = None
                    if target is not None:
                        break
                    trimmed = candidate.rsplit(cs.SEPARATOR_DOT, 1)[0]
                    if trimmed in (candidate, self.project_name):
                        break
                    candidate = trimmed
                if target == entry.module_qn:
                    # `using .SelfModule` inside the file's own module: a
                    # self-import edge is meaningless.
                    continue
                if target is None:
                    logger.debug(
                        ls.IMP_DROPPED_PHANTOM_TARGET,
                        from_module=entry.module_qn,
                        to_module=entry.full_name,
                    )
                    continue
                self._emit_import_edge(entry, cs.NodeLabel.MODULE, target)
                emitted += 1
                continue
            if entry.language == cs.SupportedLanguage.CSHARP and (
                declaring := csharp_namespace_modules.get(entry.full_name)
            ):
                emitted += self._emit_csharp_internal_imports(
                    entry, declaring, known_module_qns
                )
                continue
            if self.scala.is_scala_project_import(entry):
                emitted += self._flush_scala_project_import(entry, known_module_qns)
                continue
            emitted += self._flush_resolved_import(
                entry, known_module_paths, module_aliases, siblings
            )
        return emitted

    def _flush_scala_project_import(
        self, entry: DeferredImportEdge, known_module_qns: set[str]
    ) -> int:
        # A path into the project's own packages lands on the module
        # declaring its member; never on an ExternalModule.
        if entry.full_name not in known_module_qns:
            return 0
        self._emit_import_edge(entry, cs.NodeLabel.MODULE, entry.full_name)
        return 1

    def _is_rust_project_import(self, entry: DeferredImportEdge) -> bool:
        return entry.language == cs.SupportedLanguage.RUST and is_project_qn(
            entry.full_name, self.project_name
        )

    def _flush_rust_project_import(
        self,
        entry: DeferredImportEdge,
        known_module_paths: dict[str, str],
        module_aliases: dict[str, str],
        siblings: StemSiblingModules,
    ) -> int:
        # A crate::/super::/self:: use path was rewritten to a project
        # qn at parse time. The module target is the longest prefix
        # that verifies: the full path for a module import, minus the
        # item for item imports, further for enum variants and
        # associated items (use crate::color::Color::Red). Never
        # externalise a local path (the phantom ExternalModule would
        # orphan; issue #1007).
        candidate = entry.full_name
        target = None
        while True:
            target = self._verify_internal_import_target(
                candidate, known_module_paths, module_aliases, entry.language, siblings
            )
            if target is not None:
                break
            trimmed = candidate.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if trimmed in (candidate, self.project_name):
                break
            candidate = trimmed
        if target == entry.module_qn:
            # `use super::*` in an inline mod resolves to the file's
            # own module; a self-import edge is meaningless.
            return 0
        if target is None:
            logger.debug(
                ls.IMP_DROPPED_PHANTOM_TARGET,
                from_module=entry.module_qn,
                to_module=entry.full_name,
            )
            self.note_unresolved(entry.module_qn, entry.full_name)
            return 0
        self._emit_import_edge(entry, cs.NodeLabel.MODULE, target)
        return 1

    def _flush_resolved_import(
        self,
        entry: DeferredImportEdge,
        known_module_paths: dict[str, str],
        module_aliases: dict[str, str],
        siblings: StemSiblingModules,
    ) -> int:
        module_path = self._resolve_module_path(
            entry.full_name, entry.language, known_module_paths, entry.site
        )
        target_label = self._module_label(module_path)
        if target_label == cs.NodeLabel.EXTERNAL_MODULE:
            # An external import target has no file pass to create its
            # node; without one here the IMPORTS edge MERGEs against
            # nothing and is silently dropped (issue #652).
            self._ensure_external_module_node(module_path, entry.full_name)
        else:
            verified = self._verify_internal_import_target(
                module_path,
                known_module_paths,
                module_aliases,
                entry.language,
                siblings,
            )
            if verified is None and entry.language == cs.SupportedLanguage.PYTHON:
                # A package-anchored guess that names no sibling module is an
                # ABSOLUTE import in Python semantics (`import sys` inside a
                # package); re-resolve it as one.
                if absolute := self._python_absolute_fallback(
                    module_path, entry.module_qn
                ):
                    self._ensure_external_module_node(absolute, entry.full_name)
                    target_label = cs.NodeLabel.EXTERNAL_MODULE
                    verified = absolute
            if verified is None:
                logger.debug(
                    ls.IMP_DROPPED_PHANTOM_TARGET,
                    from_module=entry.module_qn,
                    to_module=module_path,
                )
                self.note_unresolved(entry.module_qn, module_path)
                self.note_unresolved(entry.module_qn, entry.full_name)
                return 0
            module_path = verified
        if entry.language in cs.JS_TS_LANGUAGES and _names_js_module(entry.site):
            # `export * from "./add"` in `math/index.ts` stores the module
            # `math.add`, which the resolution above reads as the name `add`
            # of the `math` barrel: the barrel itself (issue #2464). A
            # namespace import or whole-module `require` beside a directory's
            # `index.ts` or a same-named file read the same way, so
            # `import * as m from "./utils/math"` imported `utils.index`
            # (issue #2934).
            module_path = (
                self._verify_internal_import_target(
                    entry.full_name,
                    known_module_paths,
                    module_aliases,
                    entry.language,
                    siblings,
                )
                or module_path
            )
        if module_path == entry.module_qn and entry.language in cs.JS_TS_LANGUAGES:
            # A module importing itself is no edge.
            return 0
        self._emit_import_edge(entry, target_label, module_path)
        logger.debug(
            ls.IMP_CREATED_RELATIONSHIP,
            from_module=entry.module_qn,
            to_module=module_path,
            full_name=entry.full_name,
        )
        return 1

    def stem_sibling_modules(
        self, module_paths: Mapping[str, Path | str]
    ) -> StemSiblingModules:
        """Each family's module on a stem whose files carry their extension.

        A module qn that is its path-derived name plus its own extension was
        suffixed by the disambiguator, and importers of its language still
        write the bare name. Where one family has several such files (`util.js`
        and `util.ts` beside `util.py`), the one the in-family rule would have
        named bare is taken: an implementation over a declaration, then the
        first in walk order.
        """
        found: list[tuple[tuple[bool, str], str, LanguageFamily, str]] = []
        for qn, raw_path in module_paths.items():
            if not raw_path:
                continue
            path = Path(raw_path)
            language = get_language_for_extension(path.suffix)
            # Most modules carry no extension segment; skip them before paying
            # for the path arithmetic below.
            if language is None or not qn.endswith(module_extension(path.name)):
                continue
            try:
                rel = path.relative_to(self.repo_path)
            except ValueError:
                continue
            base = base_module_qn(rel, self.project_name)
            if qn == own_extension_module_qn(base, path.name):
                rank = (declaration_extension(path.name) is not None, rel.as_posix())
                found.append((rank, base, language_family(language), qn))
        siblings: StemSiblingModules = {}
        for _rank, base, family, qn in sorted(found, key=lambda item: item[0]):
            siblings.setdefault(base, {}).setdefault(family, qn)
        return siblings

    def _own_language_target(
        self,
        name: str,
        family: LanguageFamily,
        module_paths: Mapping[str, Path | str],
        siblings: StemSiblingModules,
    ) -> str:
        """`name` re-rooted on its stem's module of the importer's family.

        The longest prefix that names a module decides: one the importer can
        reach keeps the name as written, and a stem whose module is missing or
        in another family is swapped for the family's own sibling. A name
        under a real submodule (`proj.util.sub.f`) therefore stays put.
        """
        parts = name.split(cs.SEPARATOR_DOT)
        for end in range(len(parts), 1, -1):
            prefix = cs.SEPARATOR_DOT.join(parts[:end])
            holder = module_paths.get(prefix)
            if holder is not None and self._reachable_module(holder, family):
                return name
            own = siblings.get(prefix, {}).get(family)
            if own is not None:
                return f"{own}{name[len(prefix) :]}"
            if holder is not None:
                return name
        return name

    @staticmethod
    def _reachable_module(path: Path | str, family: LanguageFamily) -> bool:
        # A module with no file (an inline Rust `mod`, a rehydrated qn) or of
        # an unknown language is given the benefit of the doubt.
        if not path:
            return True
        language = get_language_for_extension(Path(path).suffix)
        return language is None or language in family

    def point_imports_at_own_language_siblings(
        self, module_paths: Mapping[str, Path]
    ) -> None:
        """Re-point imports of a stem shared across languages.

        `util.py` beside `util.js` leaves no `proj.util` module (each carries
        its extension, issue #2586), yet `from util import helper` and
        `require('./util')` both record the bare name. Each import lands on the
        file of its own language family, the only one its import system can
        reach, so CALLS and the edges resolved through the map follow it.
        """
        siblings = self.stem_sibling_modules(module_paths)
        if not siblings:
            return
        for importer in self.import_mapping:
            self.point_module_imports_at_own_language_siblings(
                importer, module_paths, siblings
            )

    def point_module_imports_at_own_language_siblings(
        self,
        importer: str,
        module_paths: Mapping[str, Path],
        siblings: StemSiblingModules,
    ) -> None:
        """`point_imports_at_own_language_siblings` for one importer's map."""
        mapping = self.import_mapping.get(importer)
        importer_path = module_paths.get(importer)
        if not mapping or not siblings or importer_path is None:
            return
        language = get_language_for_extension(importer_path.suffix)
        if language is None:
            return
        family = language_family(language)
        for local_name, full_name in mapping.items():
            target = self._own_language_target(
                full_name, family, module_paths, siblings
            )
            if target != full_name:
                mapping[local_name] = target

    def _module_alias_map(self, known_module_qns: set[str]) -> dict[str, str]:
        # A module reached through its container's name: pkg/__init__.py,
        # shared/index.js, utils/mod.rs. Importers write the container qn; the real
        # Module node lives at the index-file leaf.
        aliases: dict[str, str] = {}
        for qn in known_module_qns:
            base, _, leaf = qn.rpartition(cs.SEPARATOR_DOT)
            if base and leaf in cs.MODULE_INDEX_FILE_STEMS:
                aliases[base] = qn
        return aliases

    def _python_absolute_fallback(self, module_path: str, module_qn: str) -> str | None:
        package_qn = module_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        prefix = f"{package_qn}{cs.SEPARATOR_DOT}"
        if not module_path.startswith(prefix):
            return None
        written = module_path[len(prefix) :]
        if not written:
            return None
        absolute = self.stdlib_extractor.extract_module_path(
            written, cs.SupportedLanguage.PYTHON
        )
        project_prefix = f"{self.project_name}{cs.SEPARATOR_DOT}"
        if not absolute or absolute.startswith(project_prefix):
            return None
        return absolute

    def _verify_internal_import_target(
        self,
        module_path: str,
        known_module_paths: dict[str, str],
        module_aliases: dict[str, str],
        language: cs.SupportedLanguage,
        siblings: StemSiblingModules | None = None,
    ) -> str | None:
        if siblings:
            module_path = self._own_language_target(
                module_path, language_family(language), known_module_paths, siblings
            )
        if module_path in known_module_paths:
            return module_path
        if alias := module_aliases.get(module_path):
            return alias
        # A path resolved from the wrong root (`use crate::utils` written outside
        # src/) still names a unique real module; a whole-segment suffix match
        # recovers it. Ambiguity means no edge, not a guess.
        prefix = f"{self.project_name}{cs.SEPARATOR_DOT}"
        if not module_path.startswith(prefix):
            return None
        tail = module_path[len(prefix) :]
        if not tail:
            return None
        suffix = f"{cs.SEPARATOR_DOT}{tail}"
        matches = {qn for qn in known_module_paths if qn.endswith(suffix)}
        matches.update(
            real for base, real in module_aliases.items() if base.endswith(suffix)
        )
        if len(matches) > 1:
            # A polyglot repo can hold same-named modules in several
            # languages (thrift: src/protocol/__init__.py vs
            # src/ext/protocol.h); the importing language can only target
            # its own modules, so tie-break by file extension. Inline
            # modules (no file) stay eligible.
            matches = {
                qn
                for qn in matches
                if self._path_matches_language(known_module_paths.get(qn, ""), language)
            }
        if len(matches) == 1:
            return matches.pop()
        return None

    @staticmethod
    def _path_matches_language(path: str, language: cs.SupportedLanguage) -> bool:
        if not path:
            return True
        spec = LANGUAGE_SPECS.get(language)
        if spec is None:
            return True
        return path.endswith(tuple(spec.file_extensions))

    def _parse_python_imports(
        self, captures: dict[str, list[Node]], module_qn: str
    ) -> None:
        all_imports = captures.get(cs.CAPTURE_IMPORT, []) + captures.get(
            cs.CAPTURE_IMPORT_FROM, []
        )
        for import_node in all_imports:
            before = dict(self.import_mapping[module_qn])
            if import_node.type == cs.TS_PY_IMPORT_STATEMENT:
                self._handle_python_import_statement(import_node, module_qn)
            elif import_node.type == cs.TS_PY_IMPORT_FROM_STATEMENT:
                self._handle_python_import_from_statement(import_node, module_qn)
            if _is_conditional_import_node(import_node):
                new_names = self.import_mapping[module_qn].keys() - before.keys()
                if new_names:
                    self.conditional_imports.setdefault(module_qn, set()).update(
                        new_names
                    )
            self._record_python_rebinds(module_qn, before)

    def _record_python_rebinds(self, module_qn: str, before: dict[str, str]) -> None:
        mapping = self.import_mapping[module_qn]
        for name, previous in before.items():
            current = mapping.get(name)
            if current is not None and current != previous:
                self.python_import_rebinds.setdefault(module_qn, {}).setdefault(
                    name, {previous}
                ).add(current)

    def _handle_python_import_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        for child in import_node.named_children:
            match child.type:
                case cs.TS_DOTTED_NAME:
                    self._handle_dotted_name_import(child, module_qn)
                case cs.TS_ALIASED_IMPORT:
                    self._handle_aliased_import(child, module_qn)

    def _handle_dotted_name_import(self, child: Node, module_qn: str) -> None:
        module_name = safe_decode_text(child) or ""
        local_name = module_name.split(cs.SEPARATOR_DOT)[0]
        full_name = self._resolve_import_full_name(module_name, local_name)
        self.import_mapping[module_qn][local_name] = full_name
        self._record_import_site(module_qn, local_name, child.parent)
        logger.debug(ls.IMP_IMPORT, local=local_name, full=full_name)

    def _handle_aliased_import(self, child: Node, module_qn: str) -> None:
        module_name_node = child.child_by_field_name(cs.FIELD_NAME)
        alias_node = child.child_by_field_name(cs.FIELD_ALIAS)
        if not module_name_node or not alias_node:
            return

        module_name = safe_decode_text(module_name_node)
        alias = safe_decode_text(alias_node)
        if not module_name or not alias:
            return

        top_level = module_name.split(cs.SEPARATOR_DOT)[0]
        full_name = self._resolve_import_full_name(module_name, top_level)
        self.import_mapping[module_qn][alias] = full_name
        self._record_import_site(module_qn, alias, child.parent)
        logger.debug(ls.IMP_ALIASED_IMPORT, alias=alias, full=full_name)

    def _resolve_import_full_name(self, module_name: str, top_level: str) -> str:
        if module_name == self.project_name or module_name.startswith(
            self.project_name + cs.SEPARATOR_DOT
        ):
            if self._project_named_package:
                return f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
            return module_name
        if self._is_local_module(top_level):
            return f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
        if mapped := self._map_py_source_root(module_name):
            return f"{self.project_name}{cs.SEPARATOR_DOT}{mapped}"
        return module_name

    def _is_local_module(self, module_name: str) -> bool:
        return self._is_local_module_cached(module_name)

    def _is_local_js_import(self, full_name: str) -> bool:
        return full_name.startswith(self.project_name + cs.SEPARATOR_DOT)

    def _module_label(self, module_path: str) -> cs.NodeLabel:
        # #498: import targets outside the project prefix live under the
        # dedicated ExternalModule label (mirroring Package/ExternalPackage).
        if module_path == self.project_name or module_path.startswith(
            self.project_name + cs.SEPARATOR_DOT
        ):
            return cs.NodeLabel.MODULE
        return cs.NodeLabel.EXTERNAL_MODULE

    def ensure_external_module_node(self, module_path: str) -> None:
        # Public entry for non-import callers (deferred inheritance): an external
        # base keeps its edge by targeting the same ExternalModule node an import of
        # it would mint.
        self._ensure_external_module_node(module_path, module_path)

    def _ensure_external_module_node(self, module_path: str, full_name: str) -> None:
        if not self.ingestor or not module_path:
            return
        if cs.SEPARATOR_DOUBLE_COLON in module_path:
            name = module_path.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1]
        else:
            name = _external_module_name(module_path)
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.EXTERNAL_MODULE,
            {
                cs.KEY_NAME: name,
                cs.KEY_QUALIFIED_NAME: module_path,
                cs.KEY_PATH: full_name,
            },
        )

    def _resolve_rust_import_path(self, import_path: str) -> str:
        # Local (crate::/super::/self::) paths never reach this point: they
        # are rewritten to project qns at parse time and short-circuited in
        # flush_deferred_import_edges. Only external paths (std::fmt) remain.
        parts = import_path.split(cs.SEPARATOR_DOUBLE_COLON)
        module_path = (
            cs.SEPARATOR_DOUBLE_COLON.join(parts[:-1]) if len(parts) > 1 else parts[0]
        )

        self._ensure_external_module_node(module_path, import_path)
        return module_path

    def _resolve_module_path(
        self,
        full_name: str,
        language: cs.SupportedLanguage,
        known_module_paths: Mapping[str, str] | None = None,
        site: PropertyDict | None = None,
    ) -> str:
        project_prefix = self.project_name + cs.SEPARATOR_DOT
        match language:
            # Java MODULE semantics: internal imports point to file-level MODULE
            # nodes (project.utils.StringUtils) because Java files are named after
            # their primary class. External imports point to package-level
            # (java.util) because we lack source code for file-level nodes. This
            # asymmetry is intentional.
            case cs.SupportedLanguage.JAVA:
                if full_name.startswith(project_prefix):
                    return self.java.java_owning_module_qn(full_name)
            case (
                cs.SupportedLanguage.JS
                | cs.SupportedLanguage.TS
                | cs.SupportedLanguage.TSX
            ):
                if self._is_local_js_import(full_name):
                    return self.js_ts.resolve_js_internal_module(
                        full_name, known_module_paths, site
                    )
            case cs.SupportedLanguage.RUST:
                return self._resolve_rust_import_path(full_name)

        module_path = self.stdlib_extractor.extract_module_path(full_name, language)
        # The bare project name is first-party too: a Go import of the module
        # path itself binds the module's root package, whose qn is exactly it.
        if self._module_label(module_path) == cs.NodeLabel.EXTERNAL_MODULE:
            self._ensure_external_module_node(module_path, full_name)
        return module_path

    def _handle_python_import_from_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        module_name = self._extract_python_from_module_name(import_node, module_qn)
        if not module_name:
            return

        imported_items = self._extract_python_imported_items(import_node)
        is_wildcard = any(
            child.type == cs.TS_WILDCARD_IMPORT for child in import_node.children
        )

        if not imported_items and not is_wildcard:
            return

        self._register_python_from_imports(
            module_qn, module_name, imported_items, is_wildcard, import_node
        )

    def _extract_python_from_module_name(
        self, import_node: Node, module_qn: str
    ) -> str | None:
        module_name_node = import_node.child_by_field_name(cs.FIELD_MODULE_NAME)
        if not module_name_node:
            return None

        if module_name_node.type == cs.TS_DOTTED_NAME:
            # A written absolute path resolves through the same collision-aware
            # mapping as plain imports; a relative import is already project-prefixed
            # by construction and must NOT re-enter it (a second prefixing would
            # double the project segment).
            if written := safe_decode_text(module_name_node):
                return self._resolve_python_base_module(written)
            return None
        if module_name_node.type == cs.TS_RELATIVE_IMPORT:
            return self._resolve_relative_import(module_name_node, module_qn)
        return None

    def _extract_python_imported_items(
        self, import_node: Node
    ) -> list[tuple[str, str]]:
        imported_items: list[tuple[str, str]] = []

        for name_node in import_node.children_by_field_name(cs.FIELD_NAME):
            if item := self._extract_single_python_import(name_node):
                imported_items.append(item)

        return imported_items

    def _extract_single_python_import(self, name_node: Node) -> tuple[str, str] | None:
        if name_node.type == cs.TS_DOTTED_NAME:
            if name := safe_decode_text(name_node):
                return (name, name)
        elif name_node.type == cs.TS_ALIASED_IMPORT:
            original_node = name_node.child_by_field_name(cs.FIELD_NAME)
            alias_node = name_node.child_by_field_name(cs.FIELD_ALIAS)
            if original_node and alias_node:
                original = safe_decode_text(original_node)
                alias = safe_decode_text(alias_node)
                if original and alias:
                    return (alias, original)
        return None

    def _resolve_python_base_module(self, module_name: str) -> str:
        # The old `startswith(project_name)` as-is shortcut is subsumed by
        # _resolve_import_full_name's first branch, which also handles the
        # project-named-package collision.
        top_level = module_name.split(cs.SEPARATOR_DOT, maxsplit=1)[0]
        return self._resolve_import_full_name(module_name, top_level)

    def _register_python_from_imports(
        self,
        module_qn: str,
        base_module: str,
        imported_items: list[tuple[str, str]],
        is_wildcard: bool,
        import_node: Node | None = None,
    ) -> None:
        if is_wildcard:
            wildcard_key = f"*{base_module}"
            self.import_mapping[module_qn][wildcard_key] = base_module
            self._record_import_site(
                module_qn, wildcard_key, import_node, cs.IMPORTED_NAME_WILDCARD
            )
            logger.debug(ls.IMP_WILDCARD_IMPORT, module=base_module)
            return

        for local_name, original_name in imported_items:
            full_name = f"{base_module}{cs.SEPARATOR_DOT}{original_name}"
            self.import_mapping[module_qn][local_name] = full_name
            self._record_import_site(module_qn, local_name, import_node, original_name)
            logger.debug(ls.IMP_FROM_IMPORT, local=local_name, full=full_name)

    def _is_package_qn(self, module_qn: str) -> bool:
        prefix = self.project_name + cs.SEPARATOR_DOT
        if not module_qn.startswith(prefix):
            return False
        rel = module_qn[len(prefix) :].replace(cs.SEPARATOR_DOT, cs.SEPARATOR_SLASH)
        # A stub-only package's `__init__.pyi` names the package too (#2445).
        return any(
            (self.repo_path / rel / init).is_file() for init in cs.PY_PACKAGE_INIT_FILES
        )

    def _resolve_relative_import(self, relative_node: Node, module_qn: str) -> str:
        # Relative imports are always internal; resolve to the full project-prefixed
        # qn so resolution does not depend on bare-name locality checks (which treat
        # package children as external).
        module_parts = module_qn.split(cs.SEPARATOR_DOT)

        dots = 0
        module_name = ""

        for child in relative_node.children:
            if child.type == cs.TS_IMPORT_PREFIX:
                if decoded_text := safe_decode_text(child):
                    dots = len(decoded_text)
            elif child.type == cs.TS_DOTTED_NAME:
                if decoded_name := safe_decode_text(child):
                    module_name = decoded_name

        # A package's qn already IS the package, so `from .` inside an __init__.py
        # drops one fewer level than inside a regular module.
        drop = dots - 1 if self._is_package_qn(module_qn) else dots
        keep = max(len(module_parts) - drop, 0)
        target_parts = module_parts[:keep]

        if module_name:
            target_parts.extend(module_name.split(cs.SEPARATOR_DOT))

        # A relative climb that lands at the project root (`from . import x` in a
        # top-level module) leaves no parts; resolve it to the project root so the
        # import is not silently dropped.
        if not target_parts:
            return self.project_name

        return cs.SEPARATOR_DOT.join(target_parts)

    def note_unresolved(self, module_qn: str, name: str) -> None:
        """Record that `module_qn` referenced `name` and could not resolve it
        (issue #1568). Any pass may call this; the set is cleared when the
        module is parsed again, so it always describes the current source."""
        if name:
            self.unresolved_references.setdefault(module_qn, set()).add(name)

    def follow_module_reexports(
        self,
        qn: str,
        module_paths: Mapping[str, Path],
        function_registry: FunctionRegistryTrieProtocol,
    ) -> str:
        """Where `qn` leads through the modules re-exporting it, by the rule
        of the language its module is written in.

        A JS/TS module passes on only what it exports: its own imports are
        not its exports (issue #2464), so a name a barrel only imports must
        not lead anywhere (#2560 follow-up). Every other module is followed
        through its import map.
        """
        module_qn, separator, _name = qn.rpartition(cs.SEPARATOR_DOT)
        if separator and js_ts_module_qn(module_qn, module_paths) is not None:
            return self.js_ts.follow_js_reexports(qn, module_paths, function_registry)
        return follow_reexports(qn, self.import_mapping, function_registry)

    def reset_julia_path_caches(self) -> None:
        # Same contract as reset_rust_path_caches: the stem and declared-
        # module indices are filesystem-derived, so files added or removed
        # between runs must not keep resolving against the old layout.
        self._julia_file_stems = None
        self._julia_modules = None
        self._julia_scope_map_keys = {}

    def _parse_go_imports(
        self, captures: dict[str, list[Node]], module_qn: str
    ) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_GO_IMPORT_DECLARATION:
                self._parse_go_import_declaration(import_node, module_qn)

    def _parse_go_import_declaration(self, import_node: Node, module_qn: str) -> None:
        for child in import_node.children:
            if child.type == cs.TS_IMPORT_SPEC:
                self._parse_go_import_spec(child, module_qn)
            elif child.type == cs.TS_IMPORT_SPEC_LIST:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IMPORT_SPEC:
                        self._parse_go_import_spec(grandchild, module_qn)

    def _parse_go_import_spec(self, spec_node: Node, module_qn: str) -> None:
        alias_name = None
        import_path = None
        is_dot_import = False

        for child in spec_node.children:
            if child.type == cs.TS_PACKAGE_IDENTIFIER:
                alias_name = safe_decode_with_fallback(child)
            elif child.type == cs.TS_GO_DOT:
                is_dot_import = True
            elif child.type == cs.TS_INTERPRETED_STRING_LITERAL:
                import_path = safe_decode_with_fallback(child).strip('"')

        if import_path:
            package_name = alias_name or import_path.split(cs.SEPARATOR_SLASH)[-1]
            # A path under a local go.mod module rewrites to the package dir's
            # project qn ('' remainder = the module root package itself), so both the
            # IMPORTS edge and call resolution bind first-party. External paths stay
            # raw.
            if (mapped := self._map_go_import_path(import_path)) is not None:
                import_path = (
                    f"{self.project_name}{cs.SEPARATOR_DOT}{mapped}"
                    if mapped
                    else self.project_name
                )
            if is_dot_import:
                # `import . "fmt"` binds the package's exported names, not the
                # package identifier; a `.`-prefixed sentinel key (no identifier
                # can contain a dot) lets bare-callee lookups re-qualify.
                dot_key = f"{cs.SEPARATOR_DOT}{package_name}"
                self.import_mapping[module_qn][dot_key] = import_path
                self._record_import_site(module_qn, dot_key, spec_node)
            else:
                self.import_mapping[module_qn][package_name] = import_path
                self._record_import_site(module_qn, package_name, spec_node)
            logger.debug(ls.IMP_GO, package=package_name, path=import_path)

    def _parse_cpp_imports(
        self, captures: dict[str, list[Node]], module_qn: str
    ) -> None:
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
                self.exclude_paths,
                self.unignore_paths,
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
        self._record_import_site(module_qn, local_name, include_node, include_path)
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
        self.note_unresolved(module_qn, _include_path_suffix(include_path))
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
            self._record_import_site(module_qn, local_name, import_node, module_name)
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

    def _parse_generic_imports(
        self, captures: dict[str, list[Node]], lang_config: LanguageSpec
    ) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            logger.debug(
                ls.IMP_GENERIC,
                language=lang_config.language,
                node_type=import_node.type,
            )

    def _record_lua_require(
        self,
        module_qn: str,
        call_node: Node,
        module_path: str,
        assigned_name: str | None,
    ) -> None:
        local_name = (
            assigned_name or module_path.rsplit(cs.SEPARATOR_DOT, maxsplit=1)[-1]
        )
        resolved = self._resolve_lua_module_path(module_path, module_qn)
        self.import_mapping[module_qn][local_name] = resolved
        self._record_import_site(module_qn, local_name, call_node, module_path)

    def _parse_lua_imports(
        self, captures: dict[str, list[Node]], module_qn: str
    ) -> None:
        for call_node in captures.get(cs.CAPTURE_IMPORT, []):
            if self._lua_is_require_call(call_node):
                if module_path := self._lua_extract_require_arg(call_node):
                    self._record_lua_require(
                        module_qn,
                        call_node,
                        module_path,
                        self._lua_extract_assignment_lhs(call_node),
                    )
            elif self._lua_is_pcall_require(call_node):
                if module_path := self._lua_extract_pcall_require_arg(call_node):
                    self._record_lua_require(
                        module_qn,
                        call_node,
                        module_path,
                        lua_utils.extract_pcall_second_identifier(call_node),
                    )

            elif self._lua_is_stdlib_call(call_node):
                if stdlib_module := self._lua_extract_stdlib_module(call_node):
                    self.import_mapping[module_qn][stdlib_module] = stdlib_module
                    self._record_import_site(
                        module_qn, stdlib_module, call_node, stdlib_module
                    )

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

    def _parse_julia_imports(
        self, captures: dict[str, list[Node]], module_qn: str
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
                self._record_import_site(module_qn, local_name, stmt_node, dotted)

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

    def _julia_nested_pkg_root(self, path: str) -> str | None:
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
                hit for hit in hits if self._julia_nested_pkg_root(hit) == importer_root
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
                importer_root is None or self._julia_nested_pkg_root(c) == importer_root
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
            exclude_paths=self.exclude_paths,
            unignore_paths=self.unignore_paths,
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
                    if self._julia_nested_pkg_root(hit) == importer_root
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
            self._julia_nested_pkg_root(
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

    def _lua_is_require_call(self, call_node: Node) -> bool:
        first_child = call_node.children[0] if call_node.children else None
        if first_child and first_child.type == cs.TS_IDENTIFIER:
            return safe_decode_text(first_child) == cs.IMPORT_REQUIRE
        return False

    def _lua_is_pcall_require(self, call_node: Node) -> bool:
        first_child = call_node.children[0] if call_node.children else None
        if not (
            first_child
            and first_child.type == cs.TS_IDENTIFIER
            and safe_decode_text(first_child) == cs.IMPORT_PCALL
        ):
            return False

        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if not args:
            return False

        first_arg_node = next(
            (
                child
                for child in args.children
                if child.type not in cs.PUNCTUATION_TYPES
            ),
            None,
        )

        return (
            first_arg_node is not None
            and first_arg_node.type == cs.TS_IDENTIFIER
            and safe_decode_text(first_arg_node) == cs.IMPORT_REQUIRE
        )

    def _lua_extract_require_arg(self, call_node: Node) -> str | None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        candidates = args.children if args else call_node.children
        for node in candidates:
            if node.type in cs.LUA_STRING_TYPES:
                if decoded := safe_decode_text(node):
                    return decoded.strip("'\"")
        return None

    def _lua_extract_pcall_require_arg(self, call_node: Node) -> str | None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if not args:
            return None
        found_require = False
        for child in args.children:
            if found_require and child.type in cs.LUA_STRING_TYPES:
                if decoded := safe_decode_text(child):
                    return decoded.strip("'\"")
            if (
                child.type == cs.TS_IDENTIFIER
                and safe_decode_text(child) == cs.IMPORT_REQUIRE
            ):
                found_require = True
        return None

    def _lua_extract_assignment_lhs(self, call_node: Node) -> str | None:
        return lua_utils.extract_assigned_name(
            call_node, accepted_var_types=(cs.TS_IDENTIFIER,)
        )

    def _resolve_lua_module_path(self, import_path: str, current_module: str) -> str:
        if import_path.startswith(cs.PATH_RELATIVE_PREFIX) or import_path.startswith(
            cs.PATH_PARENT_PREFIX
        ):
            return _lua_relative_module(import_path, current_module)
        dotted = import_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)

        try:
            relative_file = (
                dotted.replace(cs.SEPARATOR_DOT, cs.SEPARATOR_SLASH) + cs.EXT_LUA
            )
            if (self.repo_path / relative_file).is_file():
                return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
            if (self.repo_path / f"{dotted}{cs.EXT_LUA}").is_file():
                return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
        except OSError:
            pass

        return dotted

    def _lua_is_stdlib_call(self, call_node: Node) -> bool:
        if not call_node.children:
            return False

        first_child = call_node.children[0]
        if first_child.type == cs.TS_DOT_INDEX_EXPRESSION and (
            first_child.children and first_child.children[0].type == cs.TS_IDENTIFIER
        ):
            module_name = safe_decode_text(first_child.children[0])
            return module_name in cs.LUA_STDLIB_MODULES

        return False

    def _lua_extract_stdlib_module(self, call_node: Node) -> str | None:
        if not call_node.children:
            return None

        first_child = call_node.children[0]
        if first_child.type == cs.TS_DOT_INDEX_EXPRESSION and (
            first_child.children and first_child.children[0].type == cs.TS_IDENTIFIER
        ):
            return safe_decode_text(first_child.children[0])

        return None
