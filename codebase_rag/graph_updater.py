"""Orchestrate parsing a repository into graph nodes and edges and ingest them."""

import hashlib
import json
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path

from loguru import logger
from rich.progress import Progress, SpinnerColumn, TextColumn
from tree_sitter import Node, Parser, QueryCursor

from . import constants as cs
from . import logs as ls
from .analyzers import FindingAnalyzer
from .ast_cache import BoundedASTCache
from .capture import CaptureSelection, default_capture
from .config import settings
from .function_registry import FunctionRegistryTrie
from .language_spec import (
    LANGUAGE_FQN_SPECS,
    get_language_for_extension,
    get_language_spec,
)
from .parser_fingerprint import compute_parser_fingerprint
from .parser_loader import COMBINED_FUNC_CLASS_IMPORT_QUERIES
from .parsers.ast_grep_tier import AstGrepTier
from .parsers.cpp.preproc_recovery import parse_with_preproc_recovery
from .parsers.cpp_frontend import (
    cpp_frontend_available,
    find_compile_commands,
    run_cpp_frontend,
    run_cpp_frontend_hybrid,
)
from .parsers.csharp_frontend import (
    CSharpQueryCall,
    csharp_frontend_available,
    find_csharp_project,
    run_csharp_frontend,
)
from .parsers.endpoints import link_endpoints
from .parsers.factory import ProcessorFactory
from .parsers.utils import sorted_captures
from .services import FilteringIngestor, IngestorProtocol, QueryProtocol
from .services.resource_cleanup import prune_unanchored_resources
from .types_defs import (
    CppDefinitionSpan,
    EmbeddingQueryResult,
    FunctionLocation,
    LanguageQueries,
    NodeType,
    PendingExpansionCall,
    PendingMacroCall,
    ResultRow,
    SimpleNameLookup,
)
from .utils.dependencies import has_semantic_dependencies
from .utils.fqn_resolver import find_function_source_by_fqn
from .utils.path_utils import (
    cached_relative_path,
    matches_ignore_patterns,
    should_skip_path,
    should_skip_rel_file,
    unignore_could_match_within,
)
from .utils.source_extraction import extract_source_with_fallback

type FileHashCache = dict[str, str]
type DirMtimesCache = dict[str, float]


_CPP_SPAN_FILE_EXTENSIONS = frozenset(cs.CPP_EXTENSIONS) | frozenset(cs.C_EXTENSIONS)


def _hash_file(filepath: Path) -> str:
    data = filepath.read_bytes()
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _hash_file_with_bytes(filepath: Path) -> tuple[str, bytes] | None:
    try:
        with open(filepath, "rb") as f:
            data = f.read()
    except OSError as e:
        logger.warning(ls.FILE_UNREADABLE, path=filepath, error=e)
        return None
    return hashlib.md5(data, usedforsecurity=False).hexdigest(), data


def _load_hash_cache(cache_path: Path) -> FileHashCache:
    if not cache_path.is_file():
        return {}
    try:
        with cache_path.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            logger.info(ls.HASH_CACHE_LOADED, count=len(data), path=cache_path)
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(ls.HASH_CACHE_LOAD_FAILED, path=cache_path, error=e)
    return {}


def _save_hash_cache(cache_path: Path, hashes: FileHashCache) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(hashes, f, indent=2)
        logger.info(ls.HASH_CACHE_SAVED, count=len(hashes), path=cache_path)
    except OSError as e:
        logger.warning(ls.HASH_CACHE_SAVE_FAILED, path=cache_path, error=e)


def _load_parser_fingerprint(stamp_path: Path) -> str | None:
    try:
        return stamp_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _save_parser_fingerprint(stamp_path: Path, fingerprint: str) -> None:
    try:
        stamp_path.write_text(fingerprint, encoding="utf-8")
    except OSError as e:
        logger.warning(ls.PARSER_FINGERPRINT_SAVE_FAILED, path=stamp_path, error=e)


def _load_dir_mtimes(cache_path: Path) -> DirMtimesCache:
    if not cache_path.is_file():
        return {}
    try:
        with cache_path.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: float(v) for k, v in data.items() if isinstance(v, int | float)}
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return {}


def _save_dir_mtimes(cache_path: Path, mtimes: DirMtimesCache) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(mtimes, f)
    except OSError:
        pass


def _touch_empty_json(cache_path: Path) -> None:
    if cache_path.exists():
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as f:
            f.write(cs.JSON_EMPTY_OBJECT)
    except OSError:
        pass


class GraphUpdater:
    """Drive a full or incremental ingest of a repository into the graph.

    Parses each supported source file into definitions, imports, and calls,
    resolves them across files, and streams the resulting nodes and edges to
    the configured ingestor.
    """

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        parsers: Mapping[cs.SupportedLanguage, Parser],
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        unignore_paths: frozenset[str] | None = None,
        exclude_paths: frozenset[str] | None = None,
        project_name: str | None = None,
        capture: CaptureSelection | None = None,
        skip_embeddings: bool | None = None,
    ):
        self.capture = capture if capture is not None else default_capture()
        # `ingestor` stays the raw object for DB queries (QueryProtocol),
        # flushes, and test introspection. `_sink` is a filtering wrapper that
        # drops disabled relationships/nodes at one choke point, so the ~20
        # parser emission sites stay untouched. All emission goes through
        # `_sink`; everything else uses `ingestor`.
        self.ingestor = ingestor
        self._sink: IngestorProtocol = FilteringIngestor(ingestor, self.capture)
        self._single_file: Path | None = None
        if repo_path.is_file():
            resolved = repo_path.resolve()
            self._single_file = resolved
            repo_path = resolved.parent
        self.repo_path = repo_path
        self.parsers = parsers
        self.queries = queries
        self.project_name = (
            project_name and project_name.strip()
        ) or repo_path.resolve().name
        self.simple_name_lookup: SimpleNameLookup = defaultdict(set)
        self.function_registry = FunctionRegistryTrie(
            simple_name_lookup=self.simple_name_lookup
        )
        self.ast_cache = BoundedASTCache(loader=self._load_ast_from_disk)
        # Every file parsed this run, in parse order. The AST cache is bounded
        # and evicts on large repos, so Pass 3 must iterate this full list (not
        # the cache) and re-parse evicted files, or their calls are dropped.
        self._parsed_files: list[tuple[Path, cs.SupportedLanguage]] = []
        self.unignore_paths = unignore_paths
        self.exclude_paths = exclude_paths
        # None defers to the CGR_SKIP_EMBEDDINGS setting so env-configured
        # callers (MCP, workspace sync) opt out without a CLI flag.
        self.skip_embeddings = (
            settings.SKIP_EMBEDDINGS if skip_embeddings is None else skip_embeddings
        )
        self.skipped_because_in_sync = False
        self._collected_dir_mtimes: DirMtimesCache = {}
        self._cpp_frontend_covered: frozenset[str] = frozenset()
        # Hybrid-mode macro uses awaiting a caller: attribution needs the
        # tree-sitter definition spans, which exist only after Pass 2.
        self._pending_cpp_macro_calls: list[PendingMacroCall] = []
        # Hybrid-mode expansion-produced calls (call text lives inside a
        # macro body): BOTH ends join to tree-sitter spans after Pass 2.
        self._pending_cpp_expansion_calls: list[PendingExpansionCall] = []
        # Definition spans read back from the graph on incremental runs:
        # an expansion call's CALLEE may live in an UNCHANGED file whose
        # spans Pass 2 never recorded this run.
        self._rehydrated_cpp_spans: dict[str, list[CppDefinitionSpan]] = {}
        # C# Roslyn hybrid facts awaiting their join point: partial
        # declaration groups join to Class qns after Pass 2, and LINQ
        # query-operator calls join to function locations after Pass 3.
        self._csharp_partial_decls: list[list[tuple[str, int]]] = []
        self._csharp_query_calls: list[CSharpQueryCall] = []
        # Files (re)parsed by Pass 2 this run: the only files whose
        # definition spans exist for hybrid macro-call attribution.
        self._reparsed_file_keys: set[str] = set()
        # Module qns read back from the graph on incremental runs; deferred
        # import verification counts them as real internal targets.
        self._rehydrated_module_qns: set[str] = set()

        self.factory = ProcessorFactory(
            ingestor=self._sink,
            repo_path=self.repo_path,
            project_name=self.project_name,
            queries=self.queries,
            function_registry=self.function_registry,
            simple_name_lookup=self.simple_name_lookup,
            ast_cache=self.ast_cache,
            unignore_paths=self.unignore_paths,
            exclude_paths=self.exclude_paths,
            capture=self.capture,
        )
        # Fallback structural tier for languages with no tree-sitter
        # LanguageSpec (e.g. Ruby), driven by ast-grep pattern configs.
        self.ast_grep_tier = AstGrepTier(self._sink, self.repo_path, self.project_name)
        # Opt-in ast-grep finding analyzer (issue #413): Pattern/CodeSmell/
        # SecurityIssue nodes from categorized YAML rules, run as a post-pass.
        self.finding_analyzer = FindingAnalyzer(
            self._sink, self.repo_path, self.capture
        )

    def _run_cpp_frontend(self) -> None:
        # Optional libclang C++ pre-pass when a compile_commands.json is
        # discoverable. LIBCLANG: emit macro-accurate C/C++ nodes/edges
        # directly (tree-sitter cannot expand macros) and skip covered files
        # in the definition pass. HYBRID: tree-sitter stays the backbone
        # (nothing skipped); libclang layers on only macro Function nodes and
        # #include IMPORTS, whose qns are scheme-identical, and hands back
        # macro uses for span attribution after Pass 2. Missing either
        # condition falls back to tree-sitter.
        self._cpp_frontend_covered = frozenset()
        self._pending_cpp_macro_calls = []
        if settings.CPP_FRONTEND not in (
            cs.CppFrontend.LIBCLANG,
            cs.CppFrontend.HYBRID,
        ):
            return
        if not self._repo_has_c_or_cpp_files():
            # HYBRID is the default, so a repo with no C/C++ sources must
            # skip silently instead of warning about libclang or a missing
            # compile_commands.json on every index of a Python/Go project.
            return
        if not cpp_frontend_available():
            logger.warning(ls.CPP_FRONTEND_UNAVAILABLE)
            return
        compdb_dir = find_compile_commands(self.repo_path)
        if compdb_dir is None:
            logger.warning(ls.CPP_FRONTEND_NO_COMPDB)
            return
        logger.info(ls.CPP_FRONTEND_RUNNING.format(path=compdb_dir))
        if settings.CPP_FRONTEND == cs.CppFrontend.HYBRID:
            (
                self._pending_cpp_macro_calls,
                self._pending_cpp_expansion_calls,
            ) = run_cpp_frontend_hybrid(
                self._sink,
                self.repo_path,
                self.project_name,
                compdb_dir,
                function_registry=self.function_registry,
                simple_name_lookup=self.simple_name_lookup,
                structural_elements=(
                    self.factory.structure_processor.structural_elements
                ),
            )
            logger.info(
                ls.CPP_FRONTEND_HYBRID_PENDING.format(
                    count=len(self._pending_cpp_macro_calls)
                    + len(self._pending_cpp_expansion_calls)
                )
            )
            return
        self._cpp_frontend_covered = run_cpp_frontend(
            self._sink,
            self.repo_path,
            self.project_name,
            compdb_dir,
            function_registry=self.function_registry,
            simple_name_lookup=self.simple_name_lookup,
            structural_elements=self.factory.structure_processor.structural_elements,
        )
        logger.info(
            ls.CPP_FRONTEND_COVERED.format(count=len(self._cpp_frontend_covered))
        )

    def _run_csharp_frontend(self) -> None:
        # Optional Roslyn semantic pre-pass. ROSLYN/HYBRID: load the repo's
        # real .csproj/.sln via MSBuildWorkspace and collect facts syntax
        # alone cannot derive: exact INHERITS-vs-IMPLEMENTS base kinds (Pass
        # 2), exact per-invocation call targets (Pass 3), partial-type
        # identity groups (joined after Pass 2), and LINQ query-operator
        # calls (after Pass 3). Missing dotnet, project, or a build/restore
        # failure all fall back to pure tree-sitter (empty facts). Reset first
        # so a reused updater (watch mode) that previously ran hybrid does not
        # keep applying stale facts on a later run with the frontend off.
        # csharp_call_sites is mutated in place because the type-inference
        # engine holds a reference.
        dp = self.factory.definition_processor
        dp.csharp_base_kinds = {}
        dp.csharp_call_sites.clear()
        dp.csharp_external_sites.clear()
        self._csharp_partial_decls = []
        self._csharp_query_calls = []
        if settings.CSHARP_FRONTEND == cs.CSharpFrontend.TREESITTER:
            return
        project = find_csharp_project(self.repo_path)
        if project is None:
            # Skip silently when there is no C# project: nothing to augment,
            # and building the net tool for a non-C# repo would be wasteful.
            return
        if not csharp_frontend_available():
            # AUTO promises hybrid only where the toolchain exists, so a
            # missing dotnet is the expected fallback (info); an EXPLICIT
            # hybrid/roslyn request that cannot run stays a warning.
            if settings.CSHARP_FRONTEND == cs.CSharpFrontend.AUTO:
                logger.info(ls.CSHARP_FRONTEND_AUTO_FALLBACK)
            else:
                logger.warning(ls.CSHARP_FRONTEND_UNAVAILABLE)
            return
        logger.info(ls.CSHARP_FRONTEND_RUNNING.format(path=project))
        facts = run_csharp_frontend(self.repo_path)
        dp.csharp_base_kinds = facts.base_kinds
        dp.csharp_call_sites.update(facts.call_sites)
        dp.csharp_external_sites.update(facts.external_sites)
        self._csharp_partial_decls = facts.partial_groups
        self._csharp_query_calls = facts.query_calls
        logger.info(ls.CSHARP_FRONTEND_TYPES.format(count=len(facts.base_kinds)))
        logger.info(
            ls.CSHARP_FRONTEND_FACTS.format(
                calls=len(facts.call_sites),
                partials=len(facts.partial_groups),
                queries=len(facts.query_calls),
                externals=len(facts.external_sites),
            )
        )

    def _join_csharp_partials(self) -> None:
        # Replace the directory-keyed syntactic partial grouping with the
        # Roslyn symbol-identity groups wherever Roslyn saw the type: parts
        # in DIFFERENT directories of one project merge (the syntactic rule
        # deliberately under-merges there), and unrelated same-name types a
        # syntactic merge would conflate split apart (each arrives as its
        # own group). Group lists stay SHARED objects because the resolver
        # compares them by identity.
        if not self._csharp_partial_decls:
            return
        dp = self.factory.definition_processor
        groups: list[list[str]] = []
        covered: set[str] = set()
        for decls in self._csharp_partial_decls:
            qns = sorted({qn for d in decls if (qn := dp.csharp_type_locations.get(d))})
            covered.update(qns)
            if len(qns) > 1:
                groups.append(qns)
        for qn in covered:
            old = dp.csharp_partial_groups.pop(qn, None)
            if old is not None and qn in old:
                # Also shrink the shared syntactic list so members NOT
                # covered by Roslyn stop spanning to this part.
                old.remove(qn)
        for group in groups:
            for qn in group:
                dp.csharp_partial_groups[qn] = group
        if groups:
            logger.info(ls.CSHARP_FRONTEND_PARTIALS_JOINED.format(count=len(groups)))

    def _emit_csharp_query_calls(self) -> None:
        # LINQ query syntax has no invocation nodes for tree-sitter to see;
        # each Roslyn query-operator fact becomes a direct CALLS edge, both
        # ends resolved through the Pass-2 function-location registry (a miss
        # on either end drops the fact rather than risk a dangling edge).
        if not self._csharp_query_calls:
            return
        dp = self.factory.definition_processor
        rel_to_module = {
            cached_relative_path(path, self.repo_path).as_posix(): qn
            for qn, path in dp.module_qn_to_file_path.items()
        }

        def located(rel_file: str, line: int, col: int) -> FunctionLocation | None:
            module_qn = rel_to_module.get(rel_file)
            if module_qn is None:
                return None
            return dp.function_locations.get((module_qn, line, col))

        emitted = 0
        for fact in self._csharp_query_calls:
            caller = located(fact.caller_file, fact.caller_line, fact.caller_col)
            target = located(fact.target_file, fact.target_line, fact.target_col)
            if caller is None or target is None:
                continue
            self.ingestor.ensure_relationship_batch(
                (caller.label, cs.KEY_QUALIFIED_NAME, caller.qualified_name),
                cs.RelationshipType.CALLS,
                (target.label, cs.KEY_QUALIFIED_NAME, target.qualified_name),
            )
            emitted += 1
        if emitted:
            logger.info(ls.CSHARP_FRONTEND_QUERY_EDGES.format(count=emitted))

    def _tightest_containing_span(
        self, rel_path: str, line: int
    ) -> CppDefinitionSpan | None:
        spans = self.factory.definition_processor.cpp_definition_spans
        candidates = spans.get(rel_path)
        if candidates is None and rel_path not in self._reparsed_file_keys:
            # An unchanged file on an incremental run has no fresh spans;
            # its definitions (and their lines) are unchanged too, so the
            # graph-rehydrated spans are exact.
            candidates = self._rehydrated_cpp_spans.get(rel_path)
        containing = [s for s in candidates or () if s.start_line <= line <= s.end_line]
        if not containing:
            return None
        return min(containing, key=lambda s: s.end_line - s.start_line)

    def _resolve_hybrid_macro_calls(self) -> None:
        # Attribute each hybrid macro use to the tightest enclosing
        # TREE-SITTER definition span (recorded during Pass 2), falling back
        # to the use site's Module: the mirror of the libclang frontend's own
        # span resolution, but against the qn scheme the rest of the graph
        # actually uses.
        if not self._pending_cpp_macro_calls:
            return
        spans = self.factory.definition_processor.cpp_definition_spans
        emitted = 0
        for call in self._pending_cpp_macro_calls:
            # The frontend parses every TU each run, but an incremental run
            # records spans only for re-parsed files. An unchanged file has no
            # spans here AND already carries its caller->macro edges, so
            # resolving it would re-attribute in-function uses to the Module.
            if call.rel_path not in self._reparsed_file_keys:
                continue
            containing = [
                s
                for s in spans.get(call.rel_path, ())
                if s.start_line <= call.line <= s.end_line
                and s.qualified_name != call.callee_qn
            ]
            if containing:
                tightest = min(containing, key=lambda s: s.end_line - s.start_line)
                caller_label, caller_qn = tightest.label, tightest.qualified_name
            else:
                caller_label = cs.NodeLabel.MODULE.value
                caller_qn = call.fallback_module_qn
            self._sink.ensure_relationship_batch(
                (caller_label, cs.KEY_QUALIFIED_NAME, caller_qn),
                cs.RelationshipType.CALLS,
                (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, call.callee_qn),
            )
            emitted += 1
        logger.info(ls.CPP_FRONTEND_MACRO_CALLS.format(count=emitted))

    def _resolve_hybrid_expansion_calls(self) -> None:
        # A call whose text lives inside a macro body exists only after
        # expansion, so tree-sitter never emits it. Join BOTH ends to
        # tree-sitter definition spans: the caller by expansion site (Module
        # fallback, like macro uses), the callee by its referenced
        # definition's location (dropped when no span contains it, since an
        # unindexed or template-only definition has no tree-sitter node to
        # target).
        if not self._pending_cpp_expansion_calls:
            return
        emitted = 0
        for call in self._pending_cpp_expansion_calls:
            if call.caller_rel_path not in self._reparsed_file_keys:
                continue
            callee = self._tightest_containing_span(
                call.callee_rel_path, call.callee_line
            )
            if callee is None:
                continue
            caller = self._tightest_containing_span(
                call.caller_rel_path, call.caller_line
            )
            if caller is not None:
                caller_label: str = caller.label
                caller_qn = caller.qualified_name
            else:
                caller_label = cs.NodeLabel.MODULE.value
                caller_qn = call.fallback_module_qn
            self._sink.ensure_relationship_batch(
                (caller_label, cs.KEY_QUALIFIED_NAME, caller_qn),
                cs.RelationshipType.CALLS,
                (callee.label, cs.KEY_QUALIFIED_NAME, callee.qualified_name),
            )
            emitted += 1
        logger.info(ls.CPP_FRONTEND_EXPANSION_CALLS.format(count=emitted))

    def _repo_has_c_or_cpp_files(self) -> bool:
        # Cheap early-exit scan: the frontend (and its warnings) only make
        # sense when there is C/C++ to index.
        extensions = set(cs.CPP_EXTENSIONS) | set(cs.C_EXTENSIONS)
        for _root, dirs, files in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if not d.startswith(cs.SEPARATOR_DOT)]
            # splitext, not Path(): no object allocation per repo file.
            if any(os.path.splitext(f)[1].lower() in extensions for f in files):
                return True
        return False

    def _is_dependency_file(self, file_name: str, filepath: Path) -> bool:
        return (
            file_name.lower() in cs.DEPENDENCY_FILES
            or filepath.suffix.lower() == cs.CSPROJ_SUFFIX
        )

    def run(self, force: bool = False) -> None:
        """Ingest the repository; ``force`` rebuilds instead of updating incrementally."""
        py_engine = self.factory.type_inference._python_type_inference
        if py_engine is not None:
            py_engine._available_classes_cache.clear()
            py_engine._return_stmt_cache.clear()
            py_engine._method_return_type_cache.clear()
            py_engine._self_assignment_cache.clear()
        # Reset per-run parse tracking so a reused updater does not reprocess
        # a previous run's files in Pass 3.
        self._parsed_files.clear()
        self._sink.ensure_node_batch(
            cs.NODE_PROJECT,
            {
                cs.KEY_NAME: self.project_name,
                cs.KEY_ROOT_PATH: str(self.repo_path.resolve()),
            },
        )
        logger.info(ls.ENSURING_PROJECT, name=self.project_name)

        if not force and self._single_file is None:
            self._drop_cache_if_graph_lost()
            self._warn_if_parser_changed()

        if not force and self._is_already_in_sync():
            logger.info(ls.GRAPH_ALREADY_IN_SYNC)
            self.skipped_because_in_sync = True
            self.ingestor.flush_all()
            return

        logger.info(ls.PASS_1_STRUCTURE)
        self.factory.structure_processor.identify_structure()

        # LIBCLANG must run before Pass 2: _process_files consumes the
        # covered-file set to skip those files.
        if settings.CPP_FRONTEND != cs.CppFrontend.HYBRID:
            self._run_cpp_frontend()

        # The C# Roslyn frontend must run before Pass 2: it produces a
        # base-classification oracle that split_csharp_bases consults while
        # ingesting each type's INHERITS/IMPLEMENTS edges during Pass 2.
        self._run_csharp_frontend()

        logger.info(ls.PASS_2_FILES)
        self._process_files(force=force)

        # Partial groups join AFTER Pass 2: the Roslyn declaration
        # locations resolve against the Class qns Pass 2 just registered.
        self._join_csharp_partials()

        # HYBRID must run after Pass 2: an incremental run deletes each
        # changed file's Module subtree before re-parsing it, so macro
        # nodes and include IMPORTS emitted earlier would be deleted with
        # it and vanish until a forced rebuild.
        if settings.CPP_FRONTEND == cs.CppFrontend.HYBRID:
            self._run_cpp_frontend()

        corrected = self.factory.definition_processor.resolve_deferred_cpp_methods()
        if corrected:
            logger.info("Resolved {} deferred C++ out-of-class methods", corrected)

        contained = self.factory.definition_processor.resolve_deferred_cpp_containment()
        if contained:
            logger.info("Resolved {} deferred C++ nested containments", contained)

        # After resolve_deferred_cpp_methods: an out-of-class method's span
        # is recorded only once its class binding resolves, and a macro use
        # inside such a method must attribute to it, not the Module.
        self._resolve_hybrid_macro_calls()

        go_methods = self.factory.definition_processor.resolve_deferred_go_methods()
        if go_methods:
            logger.info("Resolved {} Go receiver methods", go_methods)

        if not force:
            self._rehydrate_registry_from_graph()

        # After rehydration: an expansion call's callee join needs spans
        # for unchanged files too.
        self._resolve_hybrid_expansion_calls()

        # After rehydration so the "does a real definition exist?" check sees
        # definitions in files an incremental run did not re-parse; otherwise a
        # forward declaration whose definition lives in an unchanged file is
        # kept as a phantom and re-fragments the class.
        kept_forwards = (
            self.factory.definition_processor.resolve_deferred_forward_declarations()
        )
        if kept_forwards:
            logger.info(
                "Registered {} forward-declared C/C++ types with no definition",
                kept_forwards,
            )

        # After rehydration (an incremental run's class may live in an
        # unchanged header) and after forward declarations (a kept
        # forward-declared TYPE also proves the name is a class, not a
        # macro).
        orphan_ctors = (
            self.factory.definition_processor.resolve_deferred_cpp_artifacts()
        )
        if orphan_ctors:
            logger.info("Registered {} recovery-orphaned C++ ctors", orphan_ctors)

        # After forward declarations so a base whose only representation is
        # a kept forward declaration still resolves to a real node.
        inherits = self.factory.definition_processor.resolve_deferred_cpp_inherits()
        if inherits:
            logger.info("Resolved {} deferred C++ inheritance bases", inherits)

        # Same reasoning for every other language: parents resolve against
        # the full registry (including rehydrated definitions), and an
        # unresolvable parent emits no edge instead of a phantom.
        generic_inherits = self.factory.definition_processor.resolve_deferred_inherits()
        if generic_inherits:
            logger.info(
                "Resolved {} deferred inheritance/implements parents",
                generic_inherits,
            )

        module_impls = (
            self.factory.definition_processor.resolve_deferred_cpp_module_impls()
        )
        if module_impls:
            logger.info("Resolved {} C++20 module implementation links", module_impls)

        # IMPORTS edges verify against every module qn this run produced
        # (files, inline modules, rehydrated unchanged files); an internal
        # target that resolves nowhere emits no edge.
        known_module_paths: dict[str, str] = {
            str(qn): path.as_posix()
            for qn, path in (
                self.factory.definition_processor.module_qn_to_file_path.items()
            )
        }
        for qn in self.factory.definition_processor.declared_module_qns:
            known_module_paths.setdefault(qn, "")
        for qn in self._rehydrated_module_qns:
            known_module_paths.setdefault(qn, "")
        imports_emitted = self.factory.import_processor.flush_deferred_import_edges(
            known_module_paths
        )
        if imports_emitted:
            logger.info("Emitted {} verified IMPORTS edges", imports_emitted)

        # Last containment step: every node-registering pass above (deferred
        # C++ methods, Go receivers, kept forward declarations) must finish
        # before parent qns are verified against the registry.
        linked = self.factory.definition_processor.resolve_deferred_parent_links()
        if linked:
            logger.info("Resolved {} deferred containment parents", linked)

        logger.info(ls.FOUND_FUNCTIONS, count=len(self.function_registry))
        logger.info(ls.PASS_3_CALLS)
        self._process_function_calls()

        # LINQ query-operator edges join AFTER Pass 3 with the complete
        # function-location registry (both ends must be registered nodes).
        self._emit_csharp_query_calls()

        self.factory.definition_processor.process_all_method_overrides()

        # ast-grep findings post-pass (opt-in FINDINGS group). Links to the
        # Modules the definition pass already emitted, so no dangling edges.
        self.finding_analyzer.analyze(
            self.factory.definition_processor.module_qn_to_file_path
        )

        logger.info(ls.ANALYSIS_COMPLETE)
        self.ingestor.flush_all()

        self._link_endpoint_resources()

        self._prune_orphan_nodes()

        self._generate_semantic_embeddings()

    def _link_endpoint_resources(self) -> None:
        # After flush_all so this run's Resource nodes are queryable; NETWORK
        # resources of previously indexed projects join here too, which is
        # what makes client-URL-to-endpoint edges cross-project (issue #425).
        # The raw ingestor bypasses the capture filter, so gate explicitly.
        if not self.capture.rel_enabled(cs.RelationshipType.RESOLVES_TO):
            return
        if not isinstance(self.ingestor, QueryProtocol):
            return
        created = link_endpoints(self.ingestor)
        if created:
            logger.info("Resolved {} client request URLs to endpoints", created)
            self.ingestor.flush_all()

    def _rehydrate_registry_from_graph(self) -> None:
        # Incremental runs populate the function registry only from re-parsed
        # files. Read every definition's qualified name back from the graph and
        # re-register the ones missing locally, so calls and instantiations
        # into files that were not re-parsed still resolve and their edges are
        # re-emitted. Without this, editing one file drops cross-file CALLS /
        # INSTANTIATES into any unchanged file (issue #532, outbound half).
        if not isinstance(self.ingestor, QueryProtocol):
            return
        added = 0
        project_params = {cs.KEY_PROJECT_PREFIX: self.project_name + "."}
        for row in self.ingestor.fetch_all(
            cs.CYPHER_ALL_DEFINITION_QNS, project_params
        ):
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            label = row.get(cs.KEY_LABEL)
            if not isinstance(qn, str) or not isinstance(label, str):
                continue
            if qn in self.function_registry:
                continue
            try:
                node_type = NodeType(label)
            except ValueError:
                continue
            self.function_registry[qn] = node_type
            # Restore the property-name set for unchanged files: property-dispatch
            # resolution (`obj.prop`) consults it, so a re-parsed file's call to a
            # @property defined elsewhere would otherwise drop.
            if row.get(cs.KEY_IS_PROPERTY):
                self.function_registry.mark_property(qn)
            # Restore the macro-namespace set for unchanged files: the Rust
            # macro/fn gate consults it, so a re-parsed file's invocation of a
            # macro defined elsewhere would otherwise drop.
            if row.get(cs.KEY_IS_MACRO):
                self.factory.definition_processor.macro_qns.add(qn)
            # Record the defining file so _is_cpp_defined can language-check
            # rehydrated candidates (deferred C++ INHERITS resolution runs
            # after this and must reach bases in UNCHANGED headers).
            if isinstance(path := row.get(cs.KEY_PATH), str):
                self.factory.definition_processor.rehydrated_definition_paths[qn] = path
                # Spans for hybrid expansion-call callee joins: only C/C++
                # Function/Method rows carry a usable span.
                start = row.get(cs.KEY_START_LINE)
                end = row.get(cs.KEY_END_LINE)
                if (
                    node_type in (NodeType.FUNCTION, NodeType.METHOD)
                    and isinstance(start, int)
                    and isinstance(end, int)
                    and os.path.splitext(path)[1].lower() in _CPP_SPAN_FILE_EXTENSIONS
                ):
                    self._rehydrated_cpp_spans.setdefault(path, []).append(
                        CppDefinitionSpan(start, end, node_type.value, qn)
                    )
            added += 1
        if added:
            logger.info(ls.REGISTRY_REHYDRATED, count=added)
        # Module qns from unchanged files: deferred import verification and
        # C++20 module-impl resolution must count them as real targets, or
        # an incremental run would drop edges a clean index emits.
        for row in self.ingestor.fetch_all(cs.CYPHER_ALL_MODULE_QNS, project_params):
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            label = row.get(cs.KEY_LABEL)
            if not isinstance(qn, str) or not isinstance(label, str):
                continue
            if label == cs.NodeLabel.MODULE_INTERFACE.value:
                self.factory.definition_processor.cpp_module_interfaces.add(qn)
            else:
                self._rehydrated_module_qns.add(qn)
        self._rehydrate_class_inheritance_from_graph()

    def _seed_module_qns_from_graph(self, eligible_paths: set[str]) -> None:
        # Cross-language module-qn disambiguation (definition_processor.
        # _disambiguate_module_qn) only sees files processed this run. On an
        # incremental ADD of a file whose basename collides with an already-
        # indexed sibling of another language (shapes.rs owns proj.shapes,
        # then shapes.cpp is added), the added file would re-claim the bare qn
        # and overwrite the existing module under the qualified_name
        # constraint. Seed the qn->file map from the graph BEFORE processing so
        # the disambiguator sees taken qns; a re-parsed file keeps its bare qn.
        if not isinstance(self.ingestor, QueryProtocol):
            return
        module_map = self.factory.definition_processor.module_qn_to_file_path
        for row in self.ingestor.fetch_all(cs.CYPHER_ALL_MODULE_PATHS_INTERNAL):
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            path = row.get(cs.KEY_PATH)
            if not isinstance(qn, str) or not isinstance(path, str) or not path:
                continue
            if path.startswith(cs.INLINE_MODULE_PATH_PREFIX):
                continue
            # Only seed modules whose file survives this run (still eligible).
            # A file deleted OR newly excluded this cycle is gone from
            # eligible_paths, so a same-basename ADD (delete shapes.rs + add
            # shapes.cpp) takes the bare qn a clean index would give it.
            if path not in eligible_paths:
                continue
            module_map.setdefault(qn, self.repo_path / path)

    def _rehydrate_class_inheritance_from_graph(self) -> None:
        # Incremental runs rebuild class_inheritance only from re-parsed files.
        # Restore the child->bases map for classes defined in files that were
        # not re-parsed, so protocol dispatch and inherited-method resolution
        # work in Pass 3 (issue #532 residual). Only fill entries missing
        # locally: a re-parsed class already has its fresh, correctly ordered
        # bases, so we must not overwrite or duplicate them. CYPHER_ALL_INHERITS
        # is ordered by base_index, so a rehydrated class's bases keep their
        # original source order (multiple inheritance resolves the same base a
        # clean index would).
        if not isinstance(self.ingestor, QueryProtocol):
            return
        class_inheritance = self.factory.definition_processor.class_inheritance
        rows = self.ingestor.fetch_all(
            cs.CYPHER_ALL_INHERITS,
            {cs.KEY_PROJECT_PREFIX: self.project_name + "."},
        )
        for child, bases in self._rehydrated_bases_by_child(
            rows, class_inheritance
        ).items():
            class_inheritance[child] = bases

    @staticmethod
    def _rehydrated_bases_by_child(
        rows: list[ResultRow], existing: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        # Group persisted INHERITS rows into child -> ordered bases, restoring
        # source order from base_index. Skip children already present locally
        # (freshly re-parsed). A class with more than one base needs a reliable
        # order (method resolution / override attribution are first-match-wins
        # over the base list); if any of its edges lacks a base_index (e.g. an
        # INHERITS written by an older index before base_index existed) the
        # order cannot be trusted, so that class is NOT rehydrated and falls
        # back to name-based resolution rather than risk the wrong base.
        # Single-base classes are order-independent and always safe.
        collected: dict[str, list[tuple[int | None, str]]] = {}
        for row in rows:
            child = row.get(cs.KEY_CHILD_QN)
            base = row.get(cs.KEY_BASE_QN)
            if not isinstance(child, str) or not isinstance(base, str):
                continue
            if child in existing:
                continue
            raw_index = row.get(cs.KEY_BASE_INDEX)
            index = raw_index if isinstance(raw_index, int) else None
            collected.setdefault(child, []).append((index, base))
        result: dict[str, list[str]] = {}
        for child, pairs in collected.items():
            if len(pairs) > 1 and any(index is None for index, _ in pairs):
                continue
            pairs.sort(key=lambda pair: (pair[0] is None, pair[0] or 0))
            result[child] = [base for _index, base in pairs]
        return result

    def _capture_inbound_edges(self, reindexed_keys: list[str]) -> list[ResultRow]:
        # Record the reference edges unchanged files point at the re-indexed
        # files, BEFORE those files' subtrees (and thus the inbound edges) are
        # deleted. Restoring the exact edges avoids re-resolving the callers,
        # whose resolution would diverge from a clean index (cgr resolution is
        # context-sensitive).
        if not reindexed_keys or not isinstance(self.ingestor, QueryProtocol):
            return []
        return self.ingestor.fetch_all(
            cs.CYPHER_INBOUND_EDGES, {cs.CYPHER_PARAM_PATHS: reindexed_keys}
        )

    def _restore_inbound_edges(self, captured: list[ResultRow]) -> None:
        # Re-emit each captured inbound edge whose target still exists after the
        # re-index. A target that was renamed or removed is correctly left
        # without its stale inbound edge, matching a clean re-index.
        if not captured:
            return
        module_label = cs.NodeLabel.MODULE.value
        restored = 0
        for row in captured:
            caller_label = row.get(cs.KEY_CALLER_LABEL)
            caller_qn = row.get(cs.KEY_CALLER_QN)
            rel = row.get(cs.KEY_REL)
            target_label = row.get(cs.KEY_TARGET_LABEL)
            target_qn = row.get(cs.KEY_TARGET_QN)
            if not (
                isinstance(caller_label, str)
                and isinstance(caller_qn, str)
                and isinstance(rel, str)
                and isinstance(target_label, str)
                and isinstance(target_qn, str)
            ):
                continue
            if target_label != module_label and target_qn not in self.function_registry:
                continue
            caller_key = cs.NODE_UNIQUE_CONSTRAINTS.get(caller_label)
            target_key = cs.NODE_UNIQUE_CONSTRAINTS.get(target_label)
            if caller_key is None or target_key is None:
                continue
            self._sink.ensure_relationship_batch(
                (caller_label, caller_key, caller_qn),
                rel,
                (target_label, target_key, target_qn),
            )
            restored += 1
        if restored:
            logger.info(ls.INCREMENTAL_REBUILD_INBOUND, count=restored)

    def remove_file_from_state(self, file_path: Path) -> None:
        logger.debug(ls.REMOVING_STATE, path=file_path)

        if file_path in self.ast_cache:
            del self.ast_cache[file_path]
            logger.debug(ls.REMOVED_FROM_CACHE)

        relative_path = cached_relative_path(file_path, self.repo_path)
        path_parts = (
            relative_path.parent.parts
            if file_path.name == cs.INIT_PY
            else relative_path.with_suffix("").parts
        )
        module_qn_prefix = cs.SEPARATOR_DOT.join([self.project_name, *path_parts])

        qns_to_remove = set()

        for qn in list(self.function_registry.keys()):
            if qn.startswith(f"{module_qn_prefix}.") or qn == module_qn_prefix:
                qns_to_remove.add(qn)
                del self.function_registry[qn]

        if qns_to_remove:
            logger.debug(ls.REMOVING_QNS, count=len(qns_to_remove))

        for simple_name, qn_set in self.simple_name_lookup.items():
            original_count = len(qn_set)
            new_qn_set = qn_set - qns_to_remove
            if len(new_qn_set) < original_count:
                self.simple_name_lookup[simple_name] = new_qn_set
                logger.debug(ls.CLEANED_SIMPLE_NAME, name=simple_name)

    def _delete_module_entities(self, file_key: str) -> None:
        """Remove a changed/deleted file's Module subtree from the graph.

        The incremental path re-parses a changed file and re-adds its
        entities, but the entities the previous parse contributed (the
        Module and everything it DEFINES, plus their IMPORTS/CALLS edges via
        DETACH) must be removed first; otherwise renamed-away Function/Class/
        Method nodes and their edges linger alongside the new ones.
        """
        if isinstance(self.ingestor, QueryProtocol):
            self.ingestor.execute_write(
                cs.CYPHER_DELETE_MODULE, {cs.KEY_PATH: file_key}
            )

    def _diff_dir_against_cache(
        self,
        dir_path_str: str,
        dir_key: str,
        old_hashes: FileHashCache,
        old_dir_mtimes: DirMtimesCache,
    ) -> tuple[str | None, str | None]:
        prefix = "" if dir_key == cs.ROOT_DIR_KEY else f"{dir_key}/"
        expected_files: set[str] = set()
        expected_dirs: set[str] = set()
        for fk in old_hashes:
            if fk.startswith(prefix):
                rest = fk[len(prefix) :]
                if "/" not in rest:
                    expected_files.add(rest)
        for dk in old_dir_mtimes:
            if dk == cs.ROOT_DIR_KEY or not dk.startswith(prefix):
                continue
            rest = dk[len(prefix) :]
            if "/" not in rest:
                expected_dirs.add(rest)

        actual_files: set[str] = set()
        actual_dirs: set[str] = set()
        try:
            with os.scandir(dir_path_str) as it:
                for entry in it:
                    name = entry.name
                    if name in cs.CGR_STATE_FILENAMES:
                        continue
                    try:
                        is_symlink = entry.is_symlink()
                    except OSError:
                        is_symlink = False
                    try:
                        is_dir_following = entry.is_dir()
                    except OSError:
                        is_dir_following = False
                    if is_symlink and is_dir_following:
                        continue
                    if is_dir_following:
                        actual_dirs.add(name)
                    else:
                        actual_files.add(name)
        except OSError:
            return None, dir_key

        dir_parts: tuple[str, ...] = (
            () if dir_key == cs.ROOT_DIR_KEY else tuple(dir_key.split("/"))
        )
        dir_prefix_for_keep = "" if dir_key == cs.ROOT_DIR_KEY else f"{dir_key}/"

        for name in actual_dirs - expected_dirs:
            if not self._should_keep_dir(name, dir_prefix_for_keep):
                continue
            return f"{prefix}{name}", None
        for name in actual_files - expected_files:
            dot = name.rfind(".")
            suffix = name[dot:] if dot != -1 else ""
            if should_skip_rel_file(
                f"{prefix}{name}",
                dir_parts,
                suffix,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
            ):
                continue
            return f"{prefix}{name}", None

        for name in expected_files - actual_files:
            return None, f"{prefix}{name}"
        for name in expected_dirs - actual_dirs:
            return None, f"{prefix}{name}"

        return None, None

    def _should_keep_dir(self, dirname: str, dir_prefix: str) -> bool:
        rel_dir = f"{dir_prefix}{dirname}"
        # an explicit exclude can never be rescued by unignore (excludes win
        # at the file level too), so prune the subtree outright.
        if self.exclude_paths and matches_ignore_patterns(
            f"{rel_dir}/", self.exclude_paths
        ):
            return False
        if dirname not in cs.IGNORE_PATTERNS:
            return True
        # Cargo's src/bin/ holds first-party binaries, not build output;
        # mirrors has_ignored_dir_part.
        if (
            dirname == cs.DIR_BIN
            and dir_prefix.rstrip(cs.SEPARATOR_SLASH).rsplit(cs.SEPARATOR_SLASH, 1)[-1]
            == cs.DIR_SRC
        ):
            return True
        return bool(
            self.unignore_paths
            and any(
                unignore_could_match_within(u, rel_dir) for u in self.unignore_paths
            )
        )

    def _drop_cache_if_graph_lost(self) -> None:
        """Discard the hash cache when the graph no longer holds this project.

        The cache lives inside the repo, but the database is shared: cleaning
        the database while indexing another repo, an MCP wipe_database, or a
        fresh Memgraph instance voids the cache without touching it, and an
        incremental sync that trusts it would skip every file and leave the
        project silently empty.
        """
        cache_path = self.repo_path / cs.HASH_CACHE_FILENAME
        if not cache_path.is_file():
            return
        fetch_all = getattr(self.ingestor, "fetch_all", None)
        if fetch_all is None:
            return
        rows = fetch_all(
            cs.CYPHER_COUNT_PROJECT_MODULES,
            {cs.KEY_PROJECT_PREFIX: self.project_name + "."},
        )
        try:
            # count() always yields exactly one row; anything else means the
            # sink did not really answer and cannot invalidate the cache.
            count = int(rows[0]["count"])
        except (KeyError, IndexError, TypeError, ValueError):
            return
        if count:
            return
        logger.warning(ls.HASH_CACHE_ORPHANED.format(project=self.project_name))
        cache_path.unlink(missing_ok=True)
        (self.repo_path / cs.DIR_MTIMES_FILENAME).unlink(missing_ok=True)

    def _warn_if_parser_changed(self) -> None:
        # No hash cache means a full build is coming: nothing to compare.
        if not (self.repo_path / cs.HASH_CACHE_FILENAME).is_file():
            return
        stored = _load_parser_fingerprint(
            self.repo_path / cs.PARSER_FINGERPRINT_FILENAME
        )
        # A missing stamp on an existing graph means it was built by an
        # unknown (pre-fingerprint) parser: treat it as stale too, without
        # paying for a fingerprint computation that cannot match.
        if stored is None or stored != compute_parser_fingerprint():
            logger.warning(ls.PARSER_FINGERPRINT_MISMATCH)

    def _is_already_in_sync(self) -> bool:
        if self._single_file is not None:
            return False
        cache_path = self.repo_path / cs.HASH_CACHE_FILENAME
        if not cache_path.is_file():
            return False
        cache_mtime = cache_path.stat().st_mtime
        dir_mtimes_path = self.repo_path / cs.DIR_MTIMES_FILENAME
        old_hashes = _load_hash_cache(cache_path)
        old_dir_mtimes = _load_dir_mtimes(dir_mtimes_path)
        if not old_hashes or not old_dir_mtimes:
            return False

        repo_str = str(self.repo_path)
        for dir_key, cached_mtime in old_dir_mtimes.items():
            dir_path_str = (
                repo_str if dir_key == cs.ROOT_DIR_KEY else f"{repo_str}/{dir_key}"
            )
            try:
                current_mtime = os.stat(dir_path_str).st_mtime
            except OSError:
                return False
            if current_mtime != cached_mtime:
                addition, removal = self._diff_dir_against_cache(
                    dir_path_str, dir_key, old_hashes, old_dir_mtimes
                )
                if addition is not None or removal is not None:
                    return False

        for file_key, old_hash in old_hashes.items():
            file_path_str = f"{repo_str}/{file_key}"
            try:
                stat = os.stat(file_path_str)
            except OSError:
                return False
            if stat.st_mtime <= cache_mtime:
                continue
            if _hash_file(Path(file_path_str)) != old_hash:
                return False
        return True

    def _collect_eligible_files(self) -> list[tuple[Path, str]]:
        if self._single_file is not None:
            if not should_skip_path(
                self._single_file,
                self.repo_path,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
            ):
                file_key = cached_relative_path(
                    self._single_file, self.repo_path
                ).as_posix()
                return [(self._single_file, file_key)]
            return []

        eligible: list[tuple[Path, str]] = []
        state_filenames = cs.CGR_STATE_FILENAMES
        repo_str = str(self.repo_path)
        repo_prefix_len = len(repo_str) + 1
        exclude_paths = self.exclude_paths
        unignore_paths = self.unignore_paths
        self._collected_dir_mtimes = {}
        for dirpath, dirnames, filenames in os.walk(repo_str):
            if len(dirpath) < repo_prefix_len:
                rel_dir = ""
                dir_parts: tuple[str, ...] = ()
                dir_key = cs.ROOT_DIR_KEY
            else:
                rel_dir = dirpath[repo_prefix_len:].replace(os.sep, "/")
                dir_parts = tuple(rel_dir.split("/")) if rel_dir else ()
                dir_key = rel_dir or cs.ROOT_DIR_KEY
            dir_prefix = f"{rel_dir}/" if rel_dir else ""
            try:
                self._collected_dir_mtimes[dir_key] = os.stat(dirpath).st_mtime
            except OSError:
                pass
            dirnames[:] = sorted(
                d for d in dirnames if self._should_keep_dir(d, dir_prefix)
            )
            for fname in sorted(filenames):
                if fname in state_filenames:
                    continue
                dot = fname.rfind(".")
                suffix = fname[dot:] if dot != -1 else ""
                rel_path_str = f"{dir_prefix}{fname}"
                if not should_skip_rel_file(
                    rel_path_str,
                    dir_parts,
                    suffix,
                    exclude_paths=exclude_paths,
                    unignore_paths=unignore_paths,
                ):
                    eligible.append((Path(f"{dirpath}/{fname}"), rel_path_str))
        return eligible

    def _process_files(self, force: bool = False) -> None:
        cache_path = self.repo_path / cs.HASH_CACHE_FILENAME
        dir_mtimes_path = self.repo_path / cs.DIR_MTIMES_FILENAME
        old_hashes = _load_hash_cache(cache_path) if not force else {}
        is_full_build = (force or not old_hashes) and self._single_file is None
        cache_mtime = cache_path.stat().st_mtime if cache_path.is_file() else 0.0
        if force:
            logger.info(ls.INCREMENTAL_FORCE)

        _touch_empty_json(cache_path)
        _touch_empty_json(dir_mtimes_path)

        eligible_files = self._collect_eligible_files()

        if not is_full_build:
            self._seed_module_qns_from_graph({key for _fp, key in eligible_files})
        new_hashes: FileHashCache = {}
        skipped_count = 0
        changed_count = 0
        unreadable_count = 0

        current_file_keys: set[str] = set()

        processed_since_flush = 0

        changed_entries: list[tuple[Path, str, bool, bytes]] = []
        for filepath, file_key in eligible_files:
            if not force and file_key in old_hashes:
                try:
                    file_mtime = filepath.stat().st_mtime
                except OSError:
                    unreadable_count += 1
                    continue
                if file_mtime <= cache_mtime:
                    new_hashes[file_key] = old_hashes[file_key]
                    current_file_keys.add(file_key)
                    skipped_count += 1
                    continue

            hashed = _hash_file_with_bytes(filepath)
            if hashed is None:
                unreadable_count += 1
                continue
            current_hash, file_bytes = hashed

            current_file_keys.add(file_key)
            new_hashes[file_key] = current_hash

            if (
                not force
                and file_key in old_hashes
                and old_hashes[file_key] == current_hash
            ):
                logger.debug(ls.FILE_HASH_UNCHANGED, path=file_key)
                skipped_count += 1
                continue

            is_new = file_key not in old_hashes
            if not is_new:
                logger.debug(ls.FILE_HASH_CHANGED, path=file_key)
            else:
                logger.debug(ls.FILE_HASH_NEW, path=file_key)
            changed_entries.append((filepath, file_key, is_new, file_bytes))

        # Before deleting any changed file's subtree (which removes the inbound
        # CALLS/IMPORTS/INSTANTIATES edges incident on it), capture those edges
        # so they can be restored verbatim afterwards (issue #532, inbound
        # half). New files have no prior inbound edges, so only re-indexed
        # (changed, non-new) files matter.
        reindexed_keys = sorted(
            file_key for _fp, file_key, is_new, _b in changed_entries if not is_new
        )
        captured_inbound = self._capture_inbound_edges(reindexed_keys)
        self._reparsed_file_keys = {
            file_key for _fp, file_key, _new, _b in changed_entries
        }

        pre_parsed = self._pre_parse_changed_files(changed_entries)

        with Progress(
            SpinnerColumn(),
            TextColumn(ls.PROGRESS_INDEXING_LABEL),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
            disable=not sys.stderr.isatty(),
        ) as progress:
            task = progress.add_task("", total=len(eligible_files))
            if skipped_count or unreadable_count:
                progress.advance(task, skipped_count + unreadable_count)

            for filepath, file_key, is_new, file_bytes in changed_entries:
                if not is_new:
                    self.remove_file_from_state(filepath)
                    self._delete_module_entities(file_key)

                changed_count += 1
                self._process_single_file(
                    filepath,
                    file_bytes=file_bytes,
                    pre_parsed=pre_parsed.get(filepath),
                )

                processed_since_flush += 1
                if processed_since_flush >= settings.FILE_FLUSH_INTERVAL:
                    logger.info(ls.PERIODIC_FLUSH.format(count=processed_since_flush))
                    self.ingestor.flush_all()
                    processed_since_flush = 0

                progress.update(
                    task,
                    advance=1,
                    description=ls.PROGRESS_FILES_PROCESSED.format(count=changed_count),
                )

        deleted_keys = set(old_hashes.keys()) - current_file_keys
        if deleted_keys:
            logger.info(ls.INCREMENTAL_DELETED, count=len(deleted_keys))
            for deleted_key in deleted_keys:
                deleted_path = self.repo_path / deleted_key
                self.remove_file_from_state(deleted_path)
                self._delete_module_entities(deleted_key)
                if isinstance(self.ingestor, QueryProtocol):
                    self.ingestor.execute_write(
                        cs.CYPHER_DELETE_FILE, {cs.KEY_PATH: deleted_key}
                    )

        self._restore_inbound_edges(captured_inbound)

        if skipped_count > 0:
            logger.info(ls.INCREMENTAL_SKIPPED, count=skipped_count)
        if changed_count > 0:
            logger.info(ls.INCREMENTAL_CHANGED, count=changed_count)
        if unreadable_count > 0:
            logger.info(ls.INCREMENTAL_UNREADABLE, count=unreadable_count)

        _save_hash_cache(cache_path, new_hashes)
        _save_dir_mtimes(dir_mtimes_path, self._collected_dir_mtimes)
        # Stamp only full builds: re-stamping an incremental run would
        # silence the staleness warning while unchanged files still carry
        # the old parser's edges.
        if is_full_build:
            _save_parser_fingerprint(
                self.repo_path / cs.PARSER_FINGERPRINT_FILENAME,
                compute_parser_fingerprint(),
            )

    def _pre_parse_changed_files(
        self,
        changed_entries: list[tuple[Path, str, bool, bytes]],
    ) -> dict[Path, tuple[Node, dict[str, list] | None]]:
        result: dict[Path, tuple[Node, dict[str, list] | None]] = {}
        for filepath, _file_key, _is_new, file_bytes in changed_entries:
            lang_config = get_language_spec(filepath.suffix)
            if not (
                lang_config
                and isinstance(lang_config.language, cs.SupportedLanguage)
                and lang_config.language in self.parsers
            ):
                continue
            language = lang_config.language
            parser = self.queries[language].get(cs.KEY_PARSER)
            if not parser:
                continue
            tree = parse_with_preproc_recovery(parser, file_bytes, language)
            root_node = tree.root_node
            combined_query = COMBINED_FUNC_CLASS_IMPORT_QUERIES.get(language)
            combined_captures: dict[str, list] | None = None
            if combined_query:
                cursor = QueryCursor(combined_query)
                combined_captures = sorted_captures(cursor, root_node)
            result[filepath] = (root_node, combined_captures)
        return result

    def _process_single_file(
        self,
        filepath: Path,
        file_bytes: bytes | None = None,
        pre_parsed: tuple[Node, dict[str, list] | None] | None = None,
    ) -> None:
        if self._cpp_frontend_covered:
            rel = cached_relative_path(filepath, self.repo_path).as_posix()
            if rel in self._cpp_frontend_covered:
                # The libclang frontend already emitted this file's
                # definitions; keep only the generic File node.
                self.factory.structure_processor.process_generic_file(
                    filepath, filepath.name
                )
                return

        lang_config = get_language_spec(filepath.suffix)
        if (
            lang_config
            and isinstance(lang_config.language, cs.SupportedLanguage)
            and lang_config.language in self.parsers
        ):
            result = self.factory.definition_processor.process_file(
                filepath,
                lang_config.language,
                self.queries,
                self.factory.structure_processor.structural_elements,
                source_bytes=file_bytes,
                pre_parsed=pre_parsed,
            )
            if result:
                root_node, language = result
                self.ast_cache[filepath] = (root_node, language)
                self._parsed_files.append((filepath, language))
        elif self._is_dependency_file(filepath.name, filepath):
            self.factory.definition_processor.process_dependencies(filepath)
        elif self.ast_grep_tier.handles(filepath.suffix):
            self.ast_grep_tier.process_file(
                filepath, self.factory.structure_processor.structural_elements
            )

        self.factory.structure_processor.process_generic_file(filepath, filepath.name)

    def _ast_for(self, file_path: Path) -> Node | None:
        entry = self.ast_cache.load(file_path)
        return entry[0] if entry else None

    def _load_ast_from_disk(
        self, file_path: Path
    ) -> tuple[Node, cs.SupportedLanguage] | None:
        # BoundedASTCache loader: re-parse an evicted file. Evicted files carry
        # stale captures (nodes from the discarded tree), so drop them:
        # downstream recomputes captures from the fresh tree.
        language = get_language_for_extension(file_path.suffix)
        if language is None or language not in self.parsers:
            return None
        parser = self.queries[language].get(cs.KEY_PARSER)
        if parser is None:
            return None
        try:
            file_bytes = file_path.read_bytes()
        except OSError as e:
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)
            return None
        root_node = parse_with_preproc_recovery(parser, file_bytes, language).root_node
        self.factory._func_class_captures_cache.pop(file_path, None)
        return (root_node, language)

    def _process_function_calls(self) -> None:
        captures_cache = self.factory._func_class_captures_cache
        # Iterate every file parsed this run, not the bounded AST cache: on a
        # large repo the cache evicts most files, and iterating it drops their
        # calls (a whole module ends up with zero CALLS edges).
        for file_path, language in self._parsed_files:
            root_node = self._ast_for(file_path)
            if root_node is None:
                continue
            self.factory.call_processor.collect_callable_field_bindings(
                file_path,
                root_node,
                language,
                self.queries,
                func_class_captures_cache=captures_cache,
            )
        # Bindings are pending until every file's ctor metadata (param order,
        # param->attribute renames) is in: a construction site may be scanned
        # before the file defining its class.
        self.factory.call_processor.finalize_callable_field_bindings()
        for file_path, language in self._parsed_files:
            root_node = self._ast_for(file_path)
            if root_node is None:
                continue
            if captures_cache is not None and file_path in captures_cache:
                cached = captures_cache[file_path]
                if not cached.get(cs.CAPTURE_CALL) and not cached.get(
                    cs.CAPTURE_FUNCTION
                ):
                    continue
            self.factory.call_processor.process_calls_in_file(
                file_path,
                root_node,
                language,
                self.queries,
                func_class_captures_cache=captures_cache,
            )
        self.factory.call_processor.finalize_callable_param_flow()
        self.factory.call_processor.finalize_flow()

    def _prune_orphan_nodes(self) -> None:
        """Remove graph nodes whose files/folders no longer exist on disk."""
        if not isinstance(self.ingestor, QueryProtocol):
            return

        logger.info(ls.PRUNE_START)
        total_pruned = 0

        project_prefix = self.project_name + "."
        repo_abs = self.repo_path.resolve().as_posix()
        prune_specs: list[tuple[str, str, str]] = [
            (cs.CYPHER_ALL_FILE_PATHS, cs.CYPHER_DELETE_FILE, "File"),
            (
                cs.CYPHER_ALL_MODULE_PATHS_INTERNAL,
                cs.CYPHER_DELETE_MODULE,
                "Module",
            ),
            (cs.CYPHER_ALL_FOLDER_PATHS, cs.CYPHER_DELETE_FOLDER, "Folder"),
        ]

        for query_all, delete_query, label in prune_specs:
            rows = self.ingestor.fetch_all(query_all)
            orphans = []
            for r in rows:
                path = r.get("path")
                if not isinstance(path, str) or not path:
                    continue
                if path.startswith(cs.INLINE_MODULE_PATH_PREFIX):
                    continue
                abs_path = r.get("absolute_path")
                qn = r.get("qualified_name", "")
                if isinstance(abs_path, str) and not abs_path.startswith(repo_abs):
                    continue
                if isinstance(qn, str) and qn and not qn.startswith(project_prefix):
                    continue
                if not (self.repo_path / path).exists():
                    orphans.append(path)

            if orphans:
                logger.info(ls.PRUNE_FOUND, count=len(orphans), label=label)
                for orphan_path in orphans:
                    logger.debug(ls.PRUNE_DELETING, label=label, path=orphan_path)
                    self.ingestor.execute_write(
                        delete_query, {cs.KEY_PATH: orphan_path}
                    )
                total_pruned += len(orphans)

        # Drop external import-target modules that no module imports anymore,
        # e.g. an imported name renamed/removed on an incremental rebuild.
        self.ingestor.execute_write(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)

        # Drop shared Resource nodes whose component no longer reaches any
        # code node, e.g. an endpoint whose route changed on a rebuild.
        prune_unanchored_resources(self.ingestor)

        if total_pruned:
            logger.info(ls.PRUNE_COMPLETE, count=total_pruned)
        else:
            logger.info(ls.PRUNE_SKIP)

    def _generate_semantic_embeddings(self) -> None:
        if self.skip_embeddings:
            logger.info(ls.EMBEDDINGS_SKIPPED)
            return

        if not has_semantic_dependencies():
            logger.info(ls.SEMANTIC_NOT_AVAILABLE)
            return

        if not isinstance(self.ingestor, QueryProtocol):
            logger.info(ls.INGESTOR_NO_QUERY)
            return

        try:
            from .embedder import embed_code_batch, get_embedding_cache
            from .vector_store import (
                close_qdrant_client,
                store_embedding_batch,
                verify_stored_ids,
            )

            logger.info(ls.PASS_4_EMBEDDINGS)

            results = self.ingestor.fetch_all(
                cs.CYPHER_QUERY_EMBEDDINGS, {"project_name": self.project_name}
            )

            if not results:
                logger.info(ls.NO_FUNCTIONS_FOR_EMBEDDING)
                return

            logger.info(ls.GENERATING_EMBEDDINGS, count=len(results))

            embedded_count = 0
            expected_ids: set[int] = set()
            pending: list[tuple[int, str, str]] = []
            flush_at = settings.QDRANT_BATCH_SIZE

            def flush() -> int:
                nonlocal pending
                if not pending:
                    return 0
                snippets = [item[2] for item in pending]
                try:
                    embeddings = embed_code_batch(snippets)
                except Exception as e:
                    logger.warning(
                        ls.EMBEDDING_BATCH_COMPUTE_FAILED,
                        count=len(pending),
                        error=e,
                    )
                    pending = []
                    return 0
                points: list[tuple[int, list[float], str]] = [
                    (node_id, emb, qname)
                    for (node_id, qname, _), emb in zip(pending, embeddings)
                ]
                for node_id, _qname, _src in pending:
                    expected_ids.add(node_id)
                stored = store_embedding_batch(points)
                pending = []
                return stored

            for row in results:
                parsed = self._parse_embedding_result(row)
                if parsed is None:
                    continue

                node_id = parsed[cs.KEY_NODE_ID]
                qualified_name = parsed[cs.KEY_QUALIFIED_NAME]
                start_line = parsed.get(cs.KEY_START_LINE)
                end_line = parsed.get(cs.KEY_END_LINE)
                file_path = parsed.get(cs.KEY_PATH)

                if start_line is None or end_line is None or file_path is None:
                    logger.debug(ls.NO_SOURCE_FOR, name=qualified_name)
                    continue

                if source_code := self._extract_source_code(
                    qualified_name, file_path, start_line, end_line
                ):
                    pending.append((node_id, qualified_name, source_code))
                    if len(pending) >= flush_at:
                        embedded_count += flush()
                        if (
                            embedded_count % settings.EMBEDDING_PROGRESS_INTERVAL == 0
                            and embedded_count > 0
                        ):
                            logger.debug(
                                ls.EMBEDDING_PROGRESS,
                                done=embedded_count,
                                total=len(results),
                            )
                else:
                    logger.debug(ls.NO_SOURCE_FOR, name=qualified_name)

            embedded_count += flush()

            logger.info(ls.EMBEDDINGS_COMPLETE, count=embedded_count)

            self._reconcile_embeddings(expected_ids, verify_stored_ids)

            get_embedding_cache().save()
            close_qdrant_client()

        except Exception as e:
            logger.warning(ls.EMBEDDING_GENERATION_FAILED, error=e)

    def _reconcile_embeddings(
        self,
        expected_ids: set[int],
        verify_fn: Callable[[set[int]], set[int]],
    ) -> None:
        if not expected_ids:
            return
        try:
            stored_ids = verify_fn(expected_ids)
            missing = expected_ids - stored_ids
            if missing:
                sample = sorted(missing)[:10]
                logger.warning(
                    ls.EMBEDDING_RECONCILE_MISSING.format(
                        missing=len(missing),
                        expected=len(expected_ids),
                        sample_ids=sample,
                    )
                )
            else:
                logger.info(ls.EMBEDDING_RECONCILE_OK.format(count=len(expected_ids)))
        except Exception as e:
            logger.warning(ls.EMBEDDING_RECONCILE_FAILED.format(error=e))

    def _extract_source_code(
        self, qualified_name: str, file_path: str, start_line: int, end_line: int
    ) -> str | None:
        if not file_path or not start_line or not end_line:
            return None

        file_path_obj = self.repo_path / file_path

        ast_extractor = None
        if file_path_obj in self.ast_cache:
            root_node, language = self.ast_cache[file_path_obj]
            fqn_config = LANGUAGE_FQN_SPECS.get(language)

            if fqn_config:

                def ast_extractor_func(qname: str, path: Path) -> str | None:
                    return find_function_source_by_fqn(
                        root_node,
                        qname,
                        path,
                        self.repo_path,
                        self.project_name,
                        fqn_config,
                    )

                ast_extractor = ast_extractor_func

        return extract_source_with_fallback(
            file_path_obj, start_line, end_line, qualified_name, ast_extractor
        )

    def _parse_embedding_result(self, row: ResultRow) -> EmbeddingQueryResult | None:
        node_id = row.get(cs.KEY_NODE_ID)
        qualified_name = row.get(cs.KEY_QUALIFIED_NAME)

        if not isinstance(node_id, int) or not isinstance(qualified_name, str):
            return None

        start_line = row.get(cs.KEY_START_LINE)
        end_line = row.get(cs.KEY_END_LINE)
        file_path = row.get(cs.KEY_PATH)

        return EmbeddingQueryResult(
            node_id=node_id,
            qualified_name=qualified_name,
            start_line=start_line if isinstance(start_line, int) else None,
            end_line=end_line if isinstance(end_line, int) else None,
            path=file_path if isinstance(file_path, str) else None,
        )
