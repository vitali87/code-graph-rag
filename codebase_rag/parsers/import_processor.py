from collections.abc import Mapping
from pathlib import Path

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..language_spec import (
    LANGUAGE_SPECS,
    LanguageSpec,
    get_language_for_extension,
    language_family,
    own_extension_module_qn,
)
from ..node_text import sorted_captures
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
)
from .cpp.imports import CppImporter
from .csharp.imports import CSharpImporter
from .dart.imports import DartImporter
from .go.imports import GoImporter
from .java.imports import JavaImporter
from .js_ts.imports import JsTsImporter
from .js_ts.reexports import js_ts_module_qn
from .julia.imports import JuliaImporter
from .lua.imports import LuaImporter
from .php.imports import PhpImporter
from .python.imports import PythonImporter
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


def _names_js_module(site: PropertyDict | None) -> bool:
    # `export * from` and `import * as ns` record `*` as their imported name,
    # and `const m = require("./m")` records none: each names the module
    # itself, never a name inside it (issues #2464, #2934).
    if site is None:
        return False
    imported = site.get(cs.KEY_IMPORTED_NAME)
    return imported is None or imported == cs.IMPORTED_NAME_WILDCARD


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
        "stdlib_extractor",
        "_deferred_import_edges",
        "unresolved_references",
        "_import_sites",
        "_import_site_owners",
        "_import_site_writers",
        "rust",
        "js_ts",
        "java",
        "scala",
        "csharp",
        "dart",
        "php",
        "lua",
        "julia",
        "cpp",
        "go",
        "python",
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
        # Julia's using/import parsing and the file, stem and declared-module
        # indices its targets resolve through: the importer writes the import
        # map above, under a nested module's own key where one encloses the
        # statement, and records its sites through this processor.
        self.julia = JuliaImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
        )
        # C and C++ `#include` and C++20 module parsing, the repo walk an
        # include resolves against, and the includes and declarations the
        # deferral below treats apart: the importer writes the import map
        # above and records its sites and unresolved includes through this
        # processor.
        self.cpp = CppImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
        )
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
        # Lua's `require` and `pcall(require, ...)` parsing: the importer
        # writes the import map above and records its sites through this
        # processor.
        self.lua = LuaImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
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

        # Python's import and from-import parsing and the package layout a
        # written module name resolves against: the importer writes the import
        # map above and records its sites through this processor. Built
        # first of the layouts, so its source-root discovery logs first.
        self.python = PythonImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
        )

        # Go's import parsing and the go.mod layout that rewrites a local
        # import path to its package's qn: the importer writes the import map
        # above and records its sites through this processor. Built after
        # the Python layout above, whose discovery logs first.
        self.go = GoImporter(
            self,
            repo_path=repo_path,
            project_name=project_name,
            import_mapping=self.import_mapping,
        )

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
        self.julia.drop_scope_map_keys(module_qn)
        self.cpp.drop_module_state(module_qn)
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
        map_keys = {module_qn, *self.julia.scope_map_keys(module_qn)}
        for map_key in map_keys:
            bindings = self.import_mapping.get(map_key)
            if not bindings:
                continue
            for local_name, full_name in bindings.items():
                if self.cpp.is_declaration(module_qn, full_name):
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
        for shadowed in self.cpp.shadowed_include_targets(module_qn):
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
        self.python.python_import_rebinds.pop(module_qn, None)
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
                    self.python.parse_imports(captures, module_qn)
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
                    self.go.parse_imports(captures, module_qn)
                # C shares the include grammar and the resolver: without its
                # own arm here a `.c` file's `#include "foo.h"` never reached
                # the import map, so it emitted no IMPORTS edge while a `.h`
                # including the same header (parsed as C++) did (issue #1654).
                case cs.SupportedLanguage.CPP | cs.SupportedLanguage.C:
                    self.cpp.parse_imports(captures, module_qn)
                case cs.SupportedLanguage.LUA:
                    self.lua.parse_imports(captures, module_qn)
                case cs.SupportedLanguage.JULIA:
                    self.julia.parse_imports(captures, module_qn)
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
        self.cpp.forget_module_qn_map()

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
                importer_pkg = self.julia.julia_nested_pkg_root(
                    known_module_paths.get(entry.module_qn, "")
                )
                target = None
                while True:
                    target = self._verify_internal_import_target(
                        candidate, known_module_paths, module_aliases, entry.language
                    )
                    target_pkg = (
                        self.julia.julia_nested_pkg_root(
                            known_module_paths.get(target, "")
                        )
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

    def _parse_generic_imports(
        self, captures: dict[str, list[Node]], lang_config: LanguageSpec
    ) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            logger.debug(
                ls.IMP_GENERIC,
                language=lang_config.language,
                node_type=import_node.type,
            )
