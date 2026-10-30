"""The import pass: each file's import map, the sites its bindings were
written at, and the IMPORTS edges they make.

`ImportProcessor` builds one importer per language package and looks a
file's language up to hand its importer the file's import captures; the
importer writes the shared import map and records its sites and edges
through this processor. It keeps what every language shares: the import
map, the per-module state a re-parse clears, the import sites (issue
#1522), the names a module could not resolve (issue #1568) and the queue of
IMPORTS edges held until every file is parsed, which the `ImportEdges` it
owns lands on their targets (`import_edges`, with the module lookups in
`import_targets`)."""

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LanguageSpec, get_language_for_extension, language_family
from ..node_text import sorted_captures
from ..services import IngestorProtocol
from ..types_defs import (
    DeferredImportEdge,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageQueries,
    PropertyDict,
    StemSiblingModules,
)
from . import import_targets
from .cpp.imports import CppImporter
from .csharp.imports import CSharpImporter
from .dart.imports import DartImporter
from .go.imports import GoImporter
from .import_edges import ImportEdges
from .import_host import ImporterContext
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
from .utils import follow_reexports, import_site_props, shared_or_new


class LanguageImporter(Protocol):
    """What the per-language dispatch calls on the importer a language's
    files go to: the parse of one file's import captures. `root_node` is the
    whole file, which JS/TS, PHP and Dart read beyond their captures."""

    def parse_imports(
        self,
        captures: dict[str, list[Node]],
        module_qn: str,
        root_node: Node,
        /,
    ) -> None: ...


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
        "_importers",
        "_edges",
    )

    def __init__(
        self,
        repo_path: Path,
        project_name: str,
        *,
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
        self.stdlib_extractor = StdlibExtractor(
            function_registry, repo_path, project_name
        )
        self._build_importers(shared_or_new(module_qn_to_file_path, dict))
        self._importers = self._language_importers()
        # Where a deferred IMPORTS edge lands, by the layouts of the
        # importers whose languages decide it.
        self._edges = ImportEdges(
            self,
            java=self.java,
            js_ts=self.js_ts,
            julia=self.julia,
            csharp=self.csharp,
            scala=self.scala,
        )

    def _build_importers(self, module_qn_to_file_path: dict[str, Path]) -> None:
        # Each language's importer, which writes the import map above and
        # records its sites through this processor (each module's docstring
        # says what else its importer keeps). Python's, Go's and JS/TS's
        # layouts are discovered as they are built, so they are built in that
        # order and their discoveries log in it.
        context = ImporterContext(
            self.repo_path, self.project_name, self.import_mapping
        )
        self.julia = JuliaImporter(self, context)
        self.cpp = CppImporter(self, context)
        self.rust = RustImporter(self, context, import_sites=self._import_sites)
        self.java = JavaImporter(self, context)
        self.csharp = CSharpImporter(self, context)
        self.scala = ScalaImporter(self, context, import_sites=self._import_sites)
        self.lua = LuaImporter(self, context)
        self.dart = DartImporter(self, context)
        self.php = PhpImporter(self, context)
        self.python = PythonImporter(self, context)
        self.go = GoImporter(self, context)
        self.js_ts = JsTsImporter(
            self, context, module_qn_to_file_path=module_qn_to_file_path
        )

    def _language_importers(self) -> dict[cs.SupportedLanguage, LanguageImporter]:
        # The importer each language's files go to; a language with none
        # binds nothing, its import nodes only logged.
        return {
            cs.SupportedLanguage.PYTHON: self.python,
            cs.SupportedLanguage.JS: self.js_ts,
            cs.SupportedLanguage.TS: self.js_ts,
            cs.SupportedLanguage.TSX: self.js_ts,
            cs.SupportedLanguage.JAVA: self.java,
            cs.SupportedLanguage.RUST: self.rust,
            cs.SupportedLanguage.GO: self.go,
            cs.SupportedLanguage.CPP: self.cpp,
            # C shares the include grammar and the resolver: without its
            # own entry here a `.c` file's `#include "foo.h"` never reached
            # the import map, so it emitted no IMPORTS edge while a `.h`
            # including the same header (parsed as C++) did (issue #1654).
            cs.SupportedLanguage.C: self.cpp,
            cs.SupportedLanguage.LUA: self.lua,
            cs.SupportedLanguage.JULIA: self.julia,
            cs.SupportedLanguage.PHP: self.php,
            cs.SupportedLanguage.CSHARP: self.csharp,
            cs.SupportedLanguage.DART: self.dart,
            cs.SupportedLanguage.SCALA: self.scala,
        }

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

            if (importer := self._importers.get(language)) is not None:
                importer.parse_imports(captures, module_qn, root_node)
            else:
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

    def record_import_site(
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
        return self._edges.flush(deferred, known_module_paths)

    def stem_sibling_modules(
        self, module_paths: Mapping[str, Path | str]
    ) -> StemSiblingModules:
        """Each family's module on a stem whose files carry their extension
        (`import_targets.stem_sibling_modules`)."""
        return import_targets.stem_sibling_modules(
            module_paths, self.repo_path, self.project_name
        )

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
            target = import_targets.own_language_target(
                full_name, family, module_paths, siblings
            )
            if target != full_name:
                mapping[local_name] = target

    def ensure_external_module_node(self, module_path: str) -> None:
        # Public entry for non-import callers (deferred inheritance): an external
        # base keeps its edge by targeting the same ExternalModule node an import of
        # it would mint.
        self._edges.ensure_external_module_node(module_path, module_path)

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
