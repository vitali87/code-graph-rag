"""Orchestrate parsing a repository into graph nodes and edges and ingest them."""

import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path
from typing import cast

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
from .parsers.contract_linking import link_contracts
from .parsers.cpp.preproc_recovery import parse_with_preproc_recovery
from .parsers.cpp_frontend import (
    cpp_frontend_available,
    find_compile_commands,
)
from .parsers.csharp_frontend import find_csharp_project
from .parsers.document_tier import DocumentTier
from .parsers.endpoint_prefixes import (
    CYPHER_DELETE_HANDLER_EXPOSES,
    CYPHER_PROJECT_PY_MODULES,
    CYPHER_PROJECT_ROUTE_HANDLERS,
    build_router_registry,
    imported_module_qns,
)
from .parsers.endpoint_routes import (
    CYPHER_DELETE_MODULE_EXPOSES,
    CYPHER_PROJECT_MODULES,
    ROUTE_CALL_LANGUAGES,
    ROUTE_MODULE_EXTENSIONS,
    RouteRegistration,
    collect_route_registrations,
)
from .parsers.endpoints import (
    _emit_endpoint,
    emit_endpoints,
    link_endpoints,
    parse_route_decorator,
)
from .parsers.factory import ProcessorFactory
from .parsers.frontends import (
    EMITTING_FRONTENDS,
    FRONTENDS,
    FrontendEmitContext,
    FrontendPhase,
    SemanticFacts,
)
from .parsers.frontends.protocol import QueryCall
from .parsers.go_frontend import find_go_module
from .parsers.java_generated import (
    discover_generated_source_roots,
    generated_prefixes_for,
    unignore_patterns_for,
)
from .parsers.java_lombok import (
    build_delombok_overlay,
    current_lombok_identity,
    overlay_identity,
)
from .parsers.utils import sorted_captures
from .path_filters import matches_test_path
from .services import FilteringIngestor, IngestorProtocol, QueryProtocol
from .services.resource_cleanup import prune_unanchored_resources
from .types_defs import (
    CppDefinitionSpan,
    EmbeddingQueryResult,
    FunctionLocation,
    FunctionLocations,
    LanguageQueries,
    NodeType,
    PendingExpansionCall,
    PendingMacroCall,
    PendingTypeFact,
    PropertyDict,
    ReingestReport,
    ResultRow,
    SimpleNameLookup,
)
from .utils.dependencies import has_semantic_dependencies
from .utils.fqn_resolver import find_function_source_by_fqn
from .utils.path_utils import (
    base_module_qn,
    cached_file_identity_posix,
    cached_relative_path,
    should_keep_dir,
    should_skip_path,
    should_skip_rel_file,
    walk_eligible_files,
)
from .utils.source_extraction import extract_source_with_fallback


def _persisted_int(value: object) -> int | None:
    # bool is an int subclass; a stray True would key as 1 and shadow a real
    # entry, so it is rejected explicitly (same discipline as the C# path).
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _owning_module_qn(qn: str, module_qns: set[str]) -> str | None:
    owner: str | None = None
    for candidate in module_qns:
        if qn.startswith(candidate + cs.SEPARATOR_DOT) and (
            owner is None or len(candidate) > len(owner)
        ):
            owner = candidate
    return owner


def _route_handler_entry(
    row: Mapping[str, object], already_pending: set[str], module_qns: set[str]
) -> tuple[cs.NodeLabel, str, list[str], str | None] | None:
    qn = row.get(cs.KEY_QUALIFIED_NAME)
    decorators = row.get(cs.KEY_DECORATORS)
    if not isinstance(qn, str) or qn in already_pending:
        return None
    if not isinstance(decorators, list):
        return None
    routes = [d for d in decorators if isinstance(d, str) and parse_route_decorator(d)]
    if not routes:
        return None
    labels = row.get("labels")
    label = (
        cs.NodeLabel.METHOD
        if isinstance(labels, list) and cs.NodeLabel.METHOD.value in labels
        else cs.NodeLabel.FUNCTION
    )
    return (label, qn, routes, _owning_module_qn(qn, module_qns))


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


_EMPTY_DELOMBOK_STATE: dict = {"identity": "", "keys": [], "lombok": ""}


def _load_delombok_state(state_path: Path) -> dict:
    try:
        loaded = json.loads(state_path.read_text(encoding=cs.ENCODING_UTF8))
    except (OSError, json.JSONDecodeError):
        return _EMPTY_DELOMBOK_STATE.copy()
    if not isinstance(loaded, dict):
        return _EMPTY_DELOMBOK_STATE.copy()
    identity = loaded.get("identity")
    keys = loaded.get("keys")
    lombok = loaded.get("lombok")
    return {
        "identity": identity if isinstance(identity, str) else "",
        "keys": (
            sorted(key for key in keys if isinstance(key, str))
            if isinstance(keys, list)
            else []
        ),
        "lombok": lombok if isinstance(lombok, str) else "",
    }


def _save_delombok_state(state_path: Path, state: dict) -> None:
    try:
        state_path.write_text(
            json.dumps(state, sort_keys=True), encoding=cs.ENCODING_UTF8
        )
    except OSError:
        return None


def _exclusion_state(
    exclude_paths: frozenset[str] | None,
    unignore_paths: frozenset[str] | None,
) -> dict[str, list[str]]:
    return {
        "exclude": sorted(exclude_paths or ()),
        "unignore": sorted(unignore_paths or ()),
    }


def _load_exclusion_state(state_path: Path) -> dict[str, list[str]] | None:
    """The exclusion set the last completed run indexed under, or None.

    None means "cannot be established" and must be read as changed by the
    caller: an index written before this stamp existed carries an unknown
    exclusion set, exactly like a missing parser fingerprint.
    """
    try:
        loaded = json.loads(state_path.read_text(encoding=cs.ENCODING_UTF8))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    result: dict[str, list[str]] = {}
    for key in ("exclude", "unignore"):
        values = loaded.get(key)
        if not isinstance(values, list):
            return None
        result[key] = sorted(v for v in values if isinstance(v, str))
    return result


def _save_exclusion_state(state_path: Path, state: dict[str, list[str]]) -> None:
    try:
        state_path.write_text(
            json.dumps(state, sort_keys=True), encoding=cs.ENCODING_UTF8
        )
    except OSError:
        return None


def _save_hash_cache(cache_path: Path, hashes: FileHashCache) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(hashes, f, indent=2)
        logger.info(ls.HASH_CACHE_SAVED, count=len(hashes), path=cache_path)
    except OSError as e:
        logger.warning(ls.HASH_CACHE_SAVE_FAILED, path=cache_path, error=e)


def _publish_hash_cache(
    cache_path: Path, hashes: FileHashCache, observed_at: float | None
) -> bool:
    """Write the hash cache and its timestamp as one visible step.

    Returns True when the cache is published, False when it is not. The
    caller needs that answer: the directory-mtime map must not advance past
    a hash cache that failed to publish, or the pair on disk becomes the
    fresh-map/stale-cache combination that hides an addition entirely.

    `_is_already_in_sync` skips any file whose mtime is at or below this
    file's, so a cache carrying its own write time hides every edit made
    while the run was hashing. Writing then stamping leaves that exact state
    on disk between the two calls, and a run that stops there hands it to its
    successor. Building the whole thing on a temporary path and renaming it
    into place closes that window: `os.replace` is atomic within a
    filesystem, so a successor sees either the previous cache or a fully
    stamped new one, never an unstamped new one.

    On any failure the temporary file is removed and the PREVIOUS cache is
    left untouched, which is the safe direction: a cache that is one run out
    of date costs a rescan, while a mis-stamped one loses an edit.
    """
    tmp_path = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(hashes, f, indent=2)
        if observed_at is not None:
            os.utime(tmp_path, (observed_at, observed_at))
        os.replace(tmp_path, cache_path)
        logger.info(ls.HASH_CACHE_SAVED, count=len(hashes), path=cache_path)
        return True
    except OSError as e:
        logger.warning(ls.HASH_CACHE_SAVE_FAILED, path=cache_path, error=e)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            # Bound separately: `e` is why the PUBLISH failed, and reporting
            # that here would name a reason unrelated to the removal.
            # A filesystem too read-only to write the cache is too read-only
            # to have created the temporary, so there is normally nothing to
            # remove; if there is and it cannot go, the stale temporary is
            # inert (no reader looks for `.tmp`) and must not end the run.
            logger.warning(
                ls.CACHE_STAMP_CLEANUP_FAILED, path=tmp_path, error=cleanup_error
            )
        return False


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


class ReingestAborted(RuntimeError):
    """`reingest()` gave up before its first delete or content write.

    Raised for a graph read that failed while the call was still gathering
    what it needs (the module paths, the inbound edges). Nothing has been
    deleted and no definition has been rewritten; the only writes a fresh
    updater may have issued by then are the idempotent Package and Folder
    upserts of its structure hydration, which leave the graph as it was.
    A caller keeps its updater and may simply retry, unlike a failure
    after the deletes began.
    """


def _stem_key(file_key: str) -> str:
    """The relative path without its extension: what same-stem siblings share."""
    return Path(file_key).with_suffix("").as_posix()


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
        # True while the current sync re-parses EVERY file (no cache/force):
        # such a run re-resolves all edges itself, so graph reads may degrade
        # on failure; an incremental run's correctness depends on them.
        self._is_full_build = False
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
        # Rel-path -> registered qns for FRONTEND registrations, which have
        # no tree-sitter span records; the watch prefix sweep reads this to
        # spare foreign files' entries (issue #1025).
        self._frontend_owned_qns: dict[str, set[str]] = {}
        self.unignore_paths = unignore_paths
        self._configured_unignore_paths = unignore_paths
        self._delombok_overlay: dict[str, bytes] = {}
        self._delombok_state_changed = False
        self._delombok_stale_keys: set[str] = set()
        self._delombok_state_candidate: dict = _EMPTY_DELOMBOK_STATE.copy()
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
        self._csharp_query_calls: list[QueryCall] = []
        # Files (re)parsed by Pass 2 this run: the only files whose
        # definition spans exist for hybrid macro-call attribution.
        self._reparsed_file_keys: set[str] = set()
        self._exclusion_match: bool | None = None
        # Set when a run that needed the graph's module paths could not read
        # them: the reconciliation it was meant to do may not have happened,
        # so that run must not stamp its exclusion set as reconciled.
        self._graph_state_unknown: bool = False
        # Written at the post-flush commit point in `run`, never inside
        # `_process_files` (issue #1615).
        self._pending_hash_cache: tuple[Path, FileHashCache] | None = None
        self._pending_dir_mtimes: tuple[Path, DirMtimesCache] | None = None
        self._pending_cache_observed_at: float | None = None
        # Package paths read from the graph before Pass 1 of an incremental
        # run; None on a full build or when the graph could not be read.
        self._packages_before_run: set[str] | None = None
        # reingest() reads the registry back from the graph once on a fresh
        # updater; a reused one already holds live state (issue #1524).
        self._reingest_hydrated = False
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
        # Heading structure for documents (issue #1426): Section nodes nested
        # by heading level. Documents have no calls, so they get neither of
        # the code tiers above.
        self.document_tier = DocumentTier(self._sink, self.repo_path, self.project_name)
        # Opt-in ast-grep finding analyzer (issue #413): Pattern/CodeSmell/
        # SecurityIssue nodes from categorized YAML rules, run as a post-pass.
        self.finding_analyzer = FindingAnalyzer(
            self._sink, self.repo_path, self.capture
        )

    def _run_emitting_frontends(self, phase: FrontendPhase) -> None:
        """Run every registered emitting frontend declaring this phase.

        C++ is dispatched by `_run_cpp_frontend`, which owns the mode gating
        and the compile-database discovery its two modes need, so it is skipped
        here to avoid running it twice (issue #1460).

        A frontend that is unavailable or does not apply is skipped rather than
        failing the run: a missing toolchain must degrade to the tree-sitter
        backbone, which covers every file anyway.
        """
        for language, frontend in EMITTING_FRONTENDS.items():
            if language == cs.SupportedLanguage.CPP:
                continue
            if frontend.phase != phase:
                continue
            try:
                if not frontend.available() or not frontend.applies(self.repo_path):
                    continue
            except Exception:
                logger.warning(ls.EMITTING_FRONTEND_PROBE_FAILED.format(lang=language))
                continue
            result = frontend.emit(
                FrontendEmitContext(
                    ingestor=self._sink,
                    repo_path=self.repo_path,
                    project_name=self.project_name,
                    compdb_dir=self.repo_path,
                    function_registry=self.function_registry,
                    simple_name_lookup=self.simple_name_lookup,
                    structural_elements=(
                        self.factory.structure_processor.structural_elements
                    ),
                    exclude_paths=self.exclude_paths,
                    unignore_paths=self.unignore_paths,
                    owned_qns=self._frontend_owned_qns,
                )
            )
            # Covered files union across frontends: each one reports the files
            # it fully owns, and the definition pass must skip all of them.
            if result.covered_files:
                self._cpp_frontend_covered |= result.covered_files

    def _run_cpp_frontend(self) -> None:
        # Optional libclang C++ pre-pass when a compile_commands.json is
        # discoverable. LIBCLANG: emit macro-accurate C/C++ nodes/edges
        # directly (tree-sitter cannot expand macros) and skip covered files
        # in the definition pass. HYBRID: tree-sitter stays the backbone
        # (nothing skipped); libclang layers on only macro Function nodes and
        # #include IMPORTS, whose qns are scheme-identical, and hands back
        # macro uses for span attribution after Pass 2. Missing either
        # condition falls back to tree-sitter.
        # Both pending lists, not just the macro one: an early return below
        # (mode off, libclang absent, no compile_commands.json) would otherwise
        # leave a previous run's expansion calls in place, and the Pass-3
        # consumer would emit CALLS edges for expansions that no longer apply.
        self._cpp_frontend_covered = frozenset()
        self._pending_cpp_macro_calls = []
        self._pending_cpp_expansion_calls = []
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
        # Dispatched through the EmittingFrontend registry rather than called
        # directly (issue #1178). The mode-to-phase mapping now lives on the
        # frontend, so the two call sites above stay the single place that
        # decides WHEN each phase runs.
        frontend = EMITTING_FRONTENDS.get(cs.SupportedLanguage.CPP)
        if frontend is None:
            return
        result = frontend.emit(
            FrontendEmitContext(
                ingestor=self._sink,
                repo_path=self.repo_path,
                project_name=self.project_name,
                compdb_dir=compdb_dir,
                function_registry=self.function_registry,
                simple_name_lookup=self.simple_name_lookup,
                structural_elements=(
                    self.factory.structure_processor.structural_elements
                ),
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
                owned_qns=self._frontend_owned_qns,
            )
        )
        if settings.CPP_FRONTEND == cs.CppFrontend.HYBRID:
            # FrontendEmitResult keeps these as list[object] so the protocol
            # need not import cpp_frontend internals; narrowing here is the
            # "the C++ frontend adapter narrows them" its docstring describes.
            self._pending_cpp_macro_calls = cast(
                "list[PendingMacroCall]", result.pending_macro_calls
            )
            self._pending_cpp_expansion_calls = cast(
                "list[PendingExpansionCall]", result.pending_expansion_calls
            )
            logger.info(
                ls.CPP_FRONTEND_HYBRID_PENDING.format(
                    count=len(self._pending_cpp_macro_calls)
                    + len(self._pending_cpp_expansion_calls)
                )
            )
            return
        self._cpp_frontend_covered = result.covered_files
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
        self._reset_semantic_facts()
        if settings.CSHARP_FRONTEND == cs.CSharpFrontend.TREESITTER:
            return
        frontend = FRONTENDS.get(cs.SupportedLanguage.CSHARP)
        if frontend is None or not frontend.applies(self.repo_path):
            # Skip silently when the frontend has nothing to augment (no C#
            # project): applicability is the frontend's own rule (issue #1178), so
            # a registered replacement can define its own.
            return
        if not frontend.available():
            # AUTO promises hybrid only where the toolchain exists, so a
            # missing dotnet is the expected fallback (info); an EXPLICIT
            # hybrid/roslyn request that cannot run stays a warning.
            if settings.CSHARP_FRONTEND == cs.CSharpFrontend.AUTO:
                logger.info(ls.CSHARP_FRONTEND_AUTO_FALLBACK)
            else:
                logger.warning(ls.CSHARP_FRONTEND_UNAVAILABLE)
            return
        logger.info(
            ls.CSHARP_FRONTEND_RUNNING.format(path=find_csharp_project(self.repo_path))
        )
        facts = frontend.run(self.repo_path, ())
        self._apply_semantic_facts(facts)
        logger.info(ls.CSHARP_FRONTEND_TYPES.format(count=len(facts.base_kinds)))
        logger.info(
            ls.CSHARP_FRONTEND_FACTS.format(
                calls=len(facts.resolved_call_sites),
                partials=len(facts.partial_groups),
                queries=len(facts.query_calls),
                externals=len(facts.external_sites),
            )
        )

    def _reset_semantic_facts(self) -> None:
        # A reused updater (watch mode) that previously ran a frontend must not
        # keep applying stale facts on a later run with it off. csharp_call_sites
        # is mutated in place because the type-inference engine holds a reference.
        dp = self.factory.definition_processor
        dp.csharp_base_kinds = {}
        dp.csharp_call_sites.clear()
        dp.csharp_arg_flows.clear()
        dp.csharp_bind_flows.clear()
        dp.csharp_out_writes.clear()
        dp.csharp_external_sites.clear()
        self._csharp_partial_decls = []
        self._csharp_query_calls = []

    def _apply_semantic_facts(self, facts: SemanticFacts) -> None:
        # Copy each fact family into the processor state the existing consumers
        # read, with per-miss fallback to the tree-sitter heuristics (issue #1178).
        dp = self.factory.definition_processor
        dp.csharp_base_kinds = facts.base_kinds
        dp.csharp_call_sites.update(facts.resolved_call_sites)
        dp.csharp_arg_flows.update(facts.arg_flows)
        dp.csharp_bind_flows.update(facts.bind_flows)
        dp.csharp_out_writes.update(facts.out_writes)
        dp.csharp_external_sites.update(facts.external_sites)
        self._csharp_partial_decls = facts.partial_groups
        self._csharp_query_calls = facts.query_calls

    def _run_go_frontend(self) -> None:
        # Optional go/types semantic pre-pass (issue #1179). GOTYPES/AUTO: load
        # the module with go/packages and collect exact first-party call targets
        # (Pass 3) and external sites the tree-sitter name trie cannot derive.
        # Missing go, no go.mod, or a build failure all fall back to pure
        # tree-sitter (empty facts). Reset first so a reused updater (watch mode)
        # that previously ran the frontend does not keep applying stale facts on
        # a later run with it off; the maps are mutated in place because the
        # type-inference engine holds a reference.
        self._reset_go_semantic_facts()
        if settings.GO_FRONTEND == cs.GoFrontend.TREESITTER:
            return
        frontend = FRONTENDS.get(cs.SupportedLanguage.GO)
        if frontend is None or not frontend.applies(self.repo_path):
            return
        if not frontend.available():
            if settings.GO_FRONTEND == cs.GoFrontend.AUTO:
                logger.info(ls.GO_FRONTEND_AUTO_FALLBACK)
            else:
                logger.warning(ls.GO_FRONTEND_UNAVAILABLE)
            return
        logger.info(ls.GO_FRONTEND_RUNNING.format(path=find_go_module(self.repo_path)))
        facts = frontend.run(self.repo_path, ())
        self._apply_go_semantic_facts(facts)
        logger.info(
            ls.GO_FRONTEND_FACTS.format(
                calls=len(facts.resolved_call_sites),
                externals=len(facts.external_sites),
            )
        )

    def _run_java_frontend(self) -> None:
        # The javac frontend (issue #1181) runs AFTER Pass 2, like the Jedi one:
        # its call facts join against the function_locations Pass 2 just filled,
        # including the name-token alias the Java method registration adds.
        # Reset first so a reused updater (watch mode) that previously ran the
        # frontend does not keep applying stale facts on a later run with it
        # off; the maps are mutated in place because the type-inference engine
        # holds a reference.
        dp = self.factory.definition_processor
        dp.java_call_sites.clear()
        dp.java_external_sites.clear()
        if settings.JAVA_FRONTEND == cs.JavaFrontend.HEURISTIC:
            return
        frontend = FRONTENDS.get(cs.SupportedLanguage.JAVA)
        if frontend is None or not frontend.applies(self.repo_path):
            return
        if not frontend.available():
            logger.warning(ls.JAVA_FRONTEND_UNAVAILABLE)
            return
        facts = frontend.run(self.repo_path, ())
        dp.java_call_sites.update(facts.resolved_call_sites)
        dp.java_external_sites.update(facts.external_sites)
        logger.info(
            ls.JAVA_FRONTEND_FACTS.format(
                calls=len(facts.resolved_call_sites),
                externals=len(facts.external_sites),
            )
        )

    def _run_python_frontend(self) -> None:
        # In-process Jedi facts (issue #1183): exact first-party callees
        # through re-exports/decorators and external proofs for calls leaving
        # the repo. Off (HEURISTIC) or unavailable degrades to the tree-sitter
        # heuristics; reset first so a reused updater does not keep stale
        # facts when the setting flips between runs.
        dp = self.factory.definition_processor
        dp.python_call_sites.clear()
        dp.python_external_sites.clear()
        if settings.PYTHON_FRONTEND == cs.PythonFrontend.HEURISTIC:
            return
        frontend = FRONTENDS.get(cs.SupportedLanguage.PYTHON)
        if frontend is None or not frontend.available():
            logger.warning(ls.PY_FRONTEND_UNAVAILABLE)
            return
        files = [
            fp for fp, lang in self._parsed_files if lang == cs.SupportedLanguage.PYTHON
        ]
        if not files:
            return
        logger.info(ls.PY_FRONTEND_RUNNING)
        facts = frontend.run(self.repo_path, files)
        dp.python_call_sites.update(facts.resolved_call_sites)
        dp.python_external_sites.update(facts.external_sites)
        logger.info(
            ls.PY_FRONTEND_FACTS.format(
                calls=len(facts.resolved_call_sites),
                externals=len(facts.external_sites),
            )
        )

    def _reset_go_semantic_facts(self) -> None:
        dp = self.factory.definition_processor
        dp.go_call_sites.clear()
        dp.go_external_sites.clear()
        dp.go_implements.clear()

    def _apply_go_semantic_facts(self, facts: SemanticFacts) -> None:
        dp = self.factory.definition_processor
        dp.go_call_sites.update(facts.resolved_call_sites)
        dp.go_external_sites.update(facts.external_sites)
        dp.go_implements.extend(facts.implements_pairs)

    def _join_go_implements(self) -> None:
        # go/types proved each implementer->interface pair structurally; both
        # ends carry a declaring-identifier position that resolves to the Pass-2
        # type node through go_type_locations. On a two-sided hit, emit the
        # IMPLEMENTS edge and record the implementer so a call through the
        # interface with a SOLE implementer also edges to the concrete method
        # (resolver.interface_sole_impl_targets, no extra wiring). A miss on
        # either end drops the pair rather than risk a dangling edge.
        dp = self.factory.definition_processor
        if not dp.go_implements:
            return
        emitted = 0
        for pair in dp.go_implements:
            impl = dp.go_type_locations.get(
                (pair.impl_file, pair.impl_line, pair.impl_col)
            )
            iface = dp.go_type_locations.get(
                (pair.iface_file, pair.iface_line, pair.iface_col)
            )
            if impl is None or iface is None:
                continue
            impl_qn, impl_label = impl
            iface_qn, iface_label = iface
            # Through _sink (the capture-filtering wrapper), not the raw
            # ingestor, so a capture that disables IMPLEMENTS suppresses this
            # edge too -- graph-element emission must honour the capture contract.
            self._sink.ensure_relationship_batch(
                (impl_label, cs.KEY_QUALIFIED_NAME, impl_qn),
                cs.RelationshipType.IMPLEMENTS,
                (iface_label, cs.KEY_QUALIFIED_NAME, iface_qn),
            )
            dp.interface_implementers.setdefault(iface_qn, set()).add(impl_qn)
            emitted += 1
        if emitted:
            logger.info(ls.GO_FRONTEND_IMPLEMENTS_JOINED.format(count=emitted))

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
            # Through _sink (the capture-filtering wrapper), not the raw
            # ingestor, so a capture that disables CALLS suppresses these
            # synthesized LINQ query-operator edges too (issue #1236).
            self._sink.ensure_relationship_batch(
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

    def _resolve_deferred_definitions(self, rehydrate: bool) -> dict[str, str]:
        """Every deferred definition-level resolution that must follow Pass 2.

        Shared by run() and reingest() (issue #1524): a scoped re-ingest
        deletes a file's Module subtree like an incremental run does, so the
        C/C++ macro, containment, forward-declaration, inheritance and
        module-implementation relationships it held have to be rebuilt by
        the same stages, or they vanish until a full update. Returns the
        module qn -> path map the IMPORTS flush verifies against.
        """
        # HYBRID must run after Pass 2: an incremental run deletes each
        # changed file's Module subtree before re-parsing it, so macro
        # nodes and include IMPORTS emitted earlier would be deleted with
        # it and vanish until a forced rebuild.
        if settings.CPP_FRONTEND == cs.CppFrontend.HYBRID:
            self._run_cpp_frontend()
        self._run_emitting_frontends(FrontendPhase.AFTER_DEFINITIONS)

        go_methods = self.factory.definition_processor.resolve_deferred_go_methods()
        if go_methods:
            logger.info("Resolved {} Go receiver methods", go_methods)

        if rehydrate:
            self._rehydrate_registry_from_graph()

        # After rehydration: an incremental run re-parses the `.cpp` holding
        # an out-of-class method while its class stays in an unchanged
        # header, and the class is only known once the registry is read back
        # from the graph. Resolving earlier bound such a method to a
        # module-anchored fallback qn beside its class-anchored node, in a
        # parse-order-dependent way (issue #1552).
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

        # After artifact resolution so a recovery-registered definition also
        # counts; a prototype duplicating any bodied definition is dropped.
        kept_prototypes = (
            self.factory.definition_processor.resolve_deferred_cpp_prototypes()
        )
        if kept_prototypes:
            logger.info(
                "Registered {} C/C++ function prototypes with no definition",
                kept_prototypes,
            )

        # After forward declarations so a base whose only representation is
        # a kept forward declaration still resolves to a real node.
        inherits = self.factory.definition_processor.resolve_deferred_cpp_inherits()
        if inherits:
            logger.info("Resolved {} deferred C++ inheritance bases", inherits)

        # IMPORTS edges and the Rust sub-scope arbitration verify against
        # every module qn this run produced (files, inline modules,
        # rehydrated unchanged files); an internal target that resolves
        # nowhere emits no edge.
        known_module_paths = self.known_module_paths()

        # Inline-mod import maps commit BEFORE the deferred inheritance
        # pass below: it re-resolves module-anchored trait guesses through
        # them.
        self.factory.import_processor.finalise_rust_mod_scope_uses(known_module_paths)

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

        # Last containment step: every node-registering pass above (deferred
        # C++ methods, Go receivers, kept forward declarations) must finish
        # before parent qns are verified against the registry.
        linked = self.factory.definition_processor.resolve_deferred_parent_links()
        if linked:
            logger.info("Resolved {} deferred containment parents", linked)
        # Return/parameter annotations resolve against the complete registry
        # (issue #1527), so a type defined in a later file still gets its edge.
        typed = self.factory.definition_processor.emit_type_edges()
        if typed:
            logger.info(ls.TYPE_EDGES_EMITTED, count=typed)
        return known_module_paths

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
        # Per-run for the same reason: set on the in-sync early return below
        # and previously cleared only in `__init__`, so a reused instance kept
        # reporting a previous run's skip. `cli.py` reads it to decide what to
        # report, so a stale True describes a run that did real work as
        # already in sync (#1620).
        self.skipped_because_in_sync = False
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

        # Discovery must precede the in-sync check: a build that appeared
        # since the cached run changes the eligible set, and the check walks
        # with the same unignore patterns the indexing pass will use.
        self._register_generated_sources()
        # Per-run, and reset here rather than in __init__: the memo answers
        # "did the set move since the last COMPLETED run", and this run writes
        # a new stamp at the end, so an instance used for a second run must
        # re-read rather than reuse an answer about the stamp its own previous
        # run replaced. Reset after _register_generated_sources so the answer
        # is always computed against the effective unignore set.
        self._exclusion_match = None
        self._graph_state_unknown = False
        # Per-run for the same reason (issue #1620's shape): a run that raised
        # between `_process_files` and the commit point leaves its cache
        # stashed on the instance. `_process_files` has exactly one raise, the
        # deferred first parse failure, and it sits AFTER the observed-at
        # stash and BEFORE the two cache stashes, so a raising run leaves
        # `_pending_cache_observed_at` set and the other two None; `run`
        # returns only at the in-sync fast path below, above the commit
        # point. This reset is what makes that leftover harmless on a reused
        # instance, and the first early `return` added to either function
        # is covered by it too.
        self._pending_hash_cache = None
        self._pending_dir_mtimes = None
        self._pending_cache_observed_at = None
        if not force and self._is_already_in_sync():
            logger.info(ls.GRAPH_ALREADY_IN_SYNC)
            self.skipped_because_in_sync = True
            self.ingestor.flush_all()
            return

        # Cleared only when a REAL indexing run begins: an in-sync no-op above
        # must keep the last run's ownership map, which a later watch-event
        # prefix sweep still reads (issue #1025 review).
        self._frontend_owned_qns.clear()

        logger.info(ls.PASS_1_STRUCTURE)
        # Read BEFORE Pass 1: identify_structure emits this run's Package
        # nodes, so a read after it could not tell a new package from an old
        # one (issue #1570).
        self._packages_before_run = None if force else self._package_paths()
        self.factory.structure_processor.identify_structure()

        # Cleared here, not only in _run_cpp_frontend: in HYBRID that method
        # runs AFTER Pass 2, so its reset is too late for a REUSED updater
        # whose previous run was LIBCLANG. _process_files consumes this set to
        # skip files, and a stale entry makes it skip a file whose Module
        # subtree was just deleted -- leaving only a generic File node that the
        # later hybrid pass cannot repair, because it never produced the
        # tree-sitter definitions to restore.
        self._cpp_frontend_covered = frozenset()

        # LIBCLANG must run before Pass 2: _process_files consumes the
        # covered-file set to skip those files.
        if settings.CPP_FRONTEND != cs.CppFrontend.HYBRID:
            self._run_cpp_frontend()
        self._run_emitting_frontends(FrontendPhase.BEFORE_DEFINITIONS)

        # The C# Roslyn frontend must run before Pass 2: it produces a
        # base-classification oracle that split_csharp_bases consults while
        # ingesting each type's INHERITS/IMPLEMENTS edges during Pass 2.
        self._run_csharp_frontend()
        # Go facts are read at Pass 3 and the name-alias target index is filled
        # during Pass 2, so loading them here (before Pass 2) is correct.
        self._run_go_frontend()

        logger.info(ls.PASS_2_FILES)
        self._process_files(force=force)

        # Before the partial join on an incremental run: rebuild the type
        # locations for unchanged .cs files so a partial part living in one
        # still joins its group (issue #1229). Pass-2 entries for re-parsed
        # files are already present and take precedence. A cacheless full build
        # (force=False but _is_full_build) already re-parsed every file, so the
        # project-wide query would be wasted work -- skip it.
        if not force and not self._is_full_build:
            self._rehydrate_csharp_type_locations()
            # Same posture for the col-keyed indexes (issue #1240): the Go
            # IMPLEMENTS and semantic-call joins below resolve against
            # locations Pass 2 filled only for re-parsed files.
            self._rehydrate_go_type_locations()
            self._rehydrate_function_locations()

        # Partial groups join AFTER Pass 2: the Roslyn declaration
        # locations resolve against the Class qns Pass 2 just registered.
        self._join_csharp_partials()

        # Go IMPLEMENTS pairs join AFTER Pass 2 for the same reason: both ends
        # resolve against the go_type_locations index Pass 2 just registered.
        self._join_go_implements()

        # The Jedi Python frontend runs AFTER Pass 2 (its facts join Pass 3
        # calls against the function_locations Pass 2 just filled) and needs
        # the parsed-file list, which Pass 2 produced (issue #1183).
        self._run_python_frontend()

        # Same posture for Java (issue #1181): the facts resolve against the
        # method name-token locations Pass 2 registered.
        self._run_java_frontend()

        known_module_paths = self._resolve_deferred_definitions(rehydrate=not force)

        logger.info(ls.FOUND_FUNCTIONS, count=len(self.function_registry))
        # The resolver memoises name -> qn answers and module languages
        # across files; on a reused updater (any caller that holds one across
        # `run()` calls, as the tests do) those answers can name definitions
        # this run deleted or renamed, so every run starts Pass 3 with empty
        # caches, as the watcher's per-event pass already does (issue #1575).
        self.factory.call_processor.reset_resolution_caches()
        logger.info(ls.PASS_3_CALLS)
        self._process_function_calls()

        # IMPORTS flush AFTER Pass 3: a C# namespace import lands on the
        # modules the file actually resolved entities from, and those
        # resolutions are recorded during call processing (issue #1347).
        imports_emitted = self.factory.import_processor.flush_deferred_import_edges(
            known_module_paths
        )
        if imports_emitted:
            logger.info("Emitted {} verified IMPORTS edges", imports_emitted)

        # LINQ query-operator edges join AFTER Pass 3 with the complete
        # function-location registry (both ends must be registered nodes).
        self._emit_csharp_query_calls()

        self.factory.definition_processor.process_all_method_overrides()

        # Deferred endpoint emission: every module is parsed now, so router
        # mount prefixes (possibly cross-module) can resolve (issue #877).
        self._emit_pending_endpoints()

        # Call-registered routes (Express, net/http, echo, gin) become
        # endpoints too, so JS and Go servers are linkable (issue #886).
        self._emit_route_call_endpoints()

        # ast-grep findings post-pass (opt-in FINDINGS group). Links to the
        # Modules the definition pass already emitted, so no dangling edges.
        self.finding_analyzer.analyze(
            self.factory.definition_processor.module_qn_to_file_path
        )

        logger.info(ls.ANALYSIS_COMPLETE)
        self.ingestor.flush_all()

        self._link_endpoint_resources()

        self._prune_orphan_nodes()
        # The prune issues its own deletes and has no flush of its own, so the
        # flush above cannot cover them (issue #1645). Without this they are
        # still queued when the caches commit below, and a run that stops in
        # between tells its successor the tree was reconciled while the
        # deletes never became durable: the successor takes the in-sync fast
        # path, never re-issues them, and the orphan survives until a full
        # rebuild. Same invariant as #1615, on the writes that pass fixed.
        self.ingestor.flush_all()

        self._generate_semantic_embeddings()

        # The delombok state commits ONLY here, after every pass and the
        # graph flush succeeded: a run that dies mid-way must not convince
        # its successor that the overlay-affected files were reprocessed.
        # Single-file runs never commit it.
        if self._delombok_state_changed and self._single_file is None:
            _save_delombok_state(
                self.repo_path / cs.DELOMBOK_STATE_FILENAME,
                self._delombok_state_candidate,
            )

        # Same commit point, for the same reason. The module deletions a newly
        # excluded file triggers are execute_write calls that only become
        # durable at the flush above; stamping back in _process_files would let
        # a run that died in between tell its successor the exclusion was
        # already reconciled, and the successor would fast-path over a subtree
        # still in the graph. Deferring the stamp buys only that: the successor
        # RUNS. It does not on its own make the successor delete anything:
        # the cache commits below, after this flush (issue #1615), so a run
        # that died earlier leaves no cache at all and its successor re-hashes
        # from scratch. What names a file whose exclusion predates the cache
        # is the graph-backed reconciliation in _process_files, which the
        # changed exclusion set turns on; the two are needed together.
        # Recorded on EVERY completed project run, not full builds only: an
        # incremental run that narrowed the set must record it, or the next run
        # reads that run's own result as a change. A single-file run walks one
        # file and cannot speak for the project's exclusion set.
        # Committed here, not in `_process_files` (issue #1615), so a run that
        # dies before the flush leaves a cache that still names the deleted
        # file and its successor re-issues the delete. Deliberately NOT inside
        # the single-file guard below: a single-file run writes the cache today
        # and must keep doing so. Written even when `_graph_state_unknown` is
        # set, because the cache only claims which files were parsed at which
        # hashes, which stays true; the withheld exclusion stamp is what tells
        # the next run that deletions were not reconciled, and that alone both
        # fails the sync check and turns graph-backed reconciliation on.
        observed_at = self._pending_cache_observed_at
        # Hash cache FIRST, directory mtimes LAST, because a stop between the
        # two leaves a mismatched pair that is only detected in one direction.
        # `_is_already_in_sync` iterates the entries the map RECORDS, so it
        # never reaches a directory the map has no entry for. What catches an
        # addition is the recorded entry for the directory that DIRECTLY
        # CONTAINS it, whose stored mtime no longer matches disk: that sends
        # `_diff_dir_against_cache` into it to find the new child. For a new
        # subdirectory that containing entry is the parent; for a file added
        # to a directory already in the map it is that directory's own entry,
        # and the parent contributes nothing. Measured both shapes by
        # refreshing one entry at a time: refreshing the containing entry
        # alone hides the addition, refreshing any other leaves it visible.
        # Checked on all three shapes: a new subdirectory, a file added to a
        # directory already recorded, and a file added at the repo root.
        # A stale map holds every entry's old mtime, so the addition surfaces
        # either way. A FRESH map records them all as current, nothing is
        # compared, and the file loop only walks keys the hash cache already
        # names, so a file added in the gap is never indexed. Publishing the
        # hash cache first leaves the harmless window.
        # A failed publish leaves the PREVIOUS hash cache on disk, so advancing
        # the map anyway would build exactly the fresh-map/stale-cache pair the
        # ordering above exists to avoid: nothing would be compared, and a file
        # added during the run would never be indexed. Withholding the map
        # keeps both artefacts one run behind, which only costs a rescan.
        hash_cache_published = True
        if self._pending_hash_cache is not None:
            cache_path, new_hashes = self._pending_hash_cache
            hash_cache_published = _publish_hash_cache(
                cache_path, new_hashes, observed_at
            )
            self._pending_hash_cache = None
        if self._pending_dir_mtimes is not None and hash_cache_published:
            _save_dir_mtimes(*self._pending_dir_mtimes)
        self._pending_dir_mtimes = None
        self._pending_cache_observed_at = None

        if self._single_file is None:
            if self._graph_state_unknown:
                logger.warning(ls.EXCLUSION_STATE_NOT_RECORDED)
            else:
                _save_exclusion_state(
                    self.repo_path / cs.EXCLUSION_STATE_FILENAME,
                    _exclusion_state(self.exclude_paths, self.unignore_paths),
                )

    def _emit_pending_endpoints(self, only: set[str] | None = None) -> None:
        # `only` (a scoped re-ingest, issue #1524) limits the AST loads to
        # those modules plus the router modules their mounts need; the
        # mount-prefix registry is still built from every Python module the
        # graph knows, but through the disk-backed cache only for the
        # scoped set.
        if not self.capture.rel_enabled(cs.RelationshipType.EXPOSES):
            return
        dp = self.factory.definition_processor
        known_files = self._python_module_files()
        if only is None:
            module_files = known_files
            module_asts = self._python_module_asts(module_files)
        else:
            # The router modules a mounting module's mounts need are the
            # ones it imports, transitively (a mount chain main -> api ->
            # routes needs all three); they load through the cache, and
            # their handlers rehydrate below so a mount-prefix edit
            # re-emits them.
            module_files, module_asts = self._scoped_module_closure(only, known_files)
        entries = list(dp.pending_endpoints)
        # A mount-only incremental change re-parses just the mounting module;
        # the unchanged handlers must still re-emit under the new prefix, so
        # they come back from the graph. A full build queued them all already.
        if not self._is_full_build:
            if only is None:
                rehydrated = self._rehydrated_route_handlers(
                    {qn for _label, qn, _decorators, _module in entries},
                    set(module_asts),
                )
            else:
                # Scoped: only the handlers of the router modules pulled in
                # above; a module in `only` re-parsed and queued its own.
                # Handlers are attributed against every module the graph
                # knows, never the partial scoped set, or a package
                # `__init__` pulled in by an import would stand in for its
                # unloaded children and claim unrelated routers' handlers.
                rehydrated = [
                    entry
                    for entry in self._rehydrated_route_handlers(
                        {qn for _label, qn, _decorators, _module in entries},
                        set(known_files),
                    )
                    if entry[3] in module_asts and entry[3] not in only
                ]
            entries.extend(rehydrated)
        if not entries:
            return
        self._drop_stale_handler_exposes(
            [qn for _label, qn, _decorators, _module in entries]
        )
        registry = build_router_registry(module_asts)
        for label, qn, decorators, module_qn in entries:
            # Test modules stay in the stale-EXPOSES drop above (so a
            # legacy graph sheds their endpoints) but emit nothing: a route
            # spun up inside a test is not a production endpoint (#910).
            # Rehydrated handlers live in graph-backed modules absent from
            # the re-parse map, so the merged python-files map goes first.
            path = module_files.get(
                module_qn or ""
            ) or self.factory.definition_processor.module_qn_to_file_path.get(
                module_qn or ""
            )
            if self._is_test_module(path):
                continue
            emit_endpoints(
                self._sink,
                label,
                qn,
                decorators,
                module_qn=module_qn,
                prefix_resolver=registry.mount_prefixes,
            )
        dp.pending_endpoints.clear()

    def _scoped_module_closure(
        self, only: set[str], known: Mapping[str, Path]
    ) -> tuple[dict[str, Path], dict[str, Node]]:
        # `only` plus every known module reachable through their imports,
        # resolved from the ASTs the way the router registry resolves them.
        files: dict[str, Path] = {}
        asts: dict[str, Node] = {}
        pending = [qn for qn in only if qn in known]
        module_qns = set(known)
        while pending:
            qn = pending.pop()
            if qn in files:
                continue
            files[qn] = known[qn]
            loaded = self._python_module_asts({qn: known[qn]})
            if qn not in loaded:
                continue
            asts[qn] = loaded[qn]
            pending.extend(
                target
                for target in imported_module_qns(qn, loaded[qn], module_qns)
                if target not in files
            )
        return files, asts

    def _drop_stale_handler_exposes(self, handler_qns: list[str]) -> None:
        # Re-emitted handlers drop their previous EXPOSES first, so a
        # template that changed prefix loses its stale anchor and the orphan
        # cleanup prunes the outdated endpoint node.
        if not isinstance(self.ingestor, QueryProtocol):
            return
        try:
            self.ingestor.execute_write(
                CYPHER_DELETE_HANDLER_EXPOSES, {"qns": handler_qns}
            )
        except Exception:
            logger.debug("Stale EXPOSES cleanup unavailable; emission continues")

    def _emit_route_call_endpoints(self, only: set[str] | None = None) -> None:
        if not self.capture.rel_enabled(cs.RelationshipType.EXPOSES):
            return
        modules = self._route_module_asts(only=only)
        if not modules:
            return
        # Cleanup keyed on every scanned module, BEFORE emission:
        # a module whose last route disappeared (or whose attribution moved)
        # still sheds its old EXPOSES edges even though it contributes no new
        # registrations.
        self._drop_stale_module_exposes(sorted(modules))
        for module_qn, (root, language, path) in modules.items():
            # Kept in the cleanup above, skipped for emission (#910).
            if self._is_test_module(path):
                continue
            for registration in collect_route_registrations(root, language):
                label, source_qn = self._route_source(module_qn, registration)
                identity = f"{registration.method} {registration.path}"
                _emit_endpoint(self._sink, label, source_qn, identity)

    def _drop_stale_module_exposes(self, module_qns: list[str]) -> None:
        # Ownership is the DEFINES containment closure from each Module
        # node, so prefix-sharing sibling modules keep their endpoints.
        if not isinstance(self.ingestor, QueryProtocol):
            return
        try:
            self.ingestor.execute_write(
                CYPHER_DELETE_MODULE_EXPOSES, {"module_qns": module_qns}
            )
        except Exception:
            logger.debug("Stale EXPOSES cleanup unavailable; emission continues")

    def _route_source(
        self, module_qn: str, registration: RouteRegistration
    ) -> tuple[cs.NodeLabel, str]:
        # Attribution ladder: identifier handler, else the registering call's
        # enclosing function, else the module, so the endpoint stays anchored
        # and the trace lands at the wiring site.
        registry = self.factory.definition_processor.function_registry
        candidates = []
        if registration.handler_name:
            candidates.append(
                f"{module_qn}{cs.SEPARATOR_DOT}{registration.handler_name}"
            )
        if registration.scope:
            candidates.append(f"{module_qn}{cs.SEPARATOR_DOT}{registration.scope}")
        for qn in candidates:
            node_type = registry.get(qn)
            if node_type is not None:
                label = (
                    cs.NodeLabel.METHOD
                    if node_type == NodeType.METHOD
                    else cs.NodeLabel.FUNCTION
                )
                return label, qn
        return cs.NodeLabel.MODULE, module_qn

    def _route_module_asts(
        self, only: set[str] | None = None
    ) -> dict[str, tuple[Node, cs.SupportedLanguage, Path]]:
        dp = self.factory.definition_processor
        files: dict[str, Path] = dict(dp.module_qn_to_file_path)
        if only is None:
            for qn, path in self._graph_route_module_paths():
                files.setdefault(qn, path)
        else:
            files = {qn: p for qn, p in files.items() if qn in only}
        out: dict[str, tuple[Node, cs.SupportedLanguage, Path]] = {}
        for qn, path in files.items():
            entry = self.ast_cache.load(path)
            if entry is not None and entry[1] in ROUTE_CALL_LANGUAGES:
                out[qn] = (entry[0], entry[1], path)
        return out

    def _is_test_module(self, path: Path | None) -> bool:
        # Judged on the repo-relative POSIX path; an unknown path (a
        # rehydrated legacy handler) stays permissive. The repo-relative cut
        # keeps a `/tmp/pytest-*/` parent from classifying the whole run.
        if path is None:
            return False
        try:
            relative = path.relative_to(self.repo_path)
        except ValueError:
            # Outside the repo means the repo-relative judgement is
            # impossible; stay permissive rather than matching absolute
            # segments like a `/tmp/tests/` parent.
            return False
        return matches_test_path(relative.as_posix())

    def _graph_route_module_paths(self) -> list[tuple[str, Path]]:
        # Unchanged modules come back from the graph on an incremental run,
        # already narrowed to route-capable extensions at the query.
        if not isinstance(self.ingestor, QueryProtocol):
            return []
        params = {
            cs.KEY_PROJECT_PREFIX: self.project_name + cs.SEPARATOR_DOT,
            "extensions": list(ROUTE_MODULE_EXTENSIONS),
        }
        try:
            rows = list(self.ingestor.fetch_all(CYPHER_PROJECT_MODULES, params))
        except Exception:
            return []
        out: list[tuple[str, Path]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            qn, rel_path = row.get(cs.KEY_QUALIFIED_NAME), row.get(cs.KEY_PATH)
            if isinstance(qn, str) and isinstance(rel_path, str):
                out.append((qn, self.repo_path / rel_path))
        return out

    def _rehydrated_route_handlers(
        self, already_pending: set[str], module_qns: set[str]
    ) -> list[tuple[cs.NodeLabel, str, list[str], str | None]]:
        if not isinstance(self.ingestor, QueryProtocol):
            return []
        params = {cs.KEY_PROJECT_PREFIX: self.project_name + cs.SEPARATOR_DOT}
        try:
            rows = list(self.ingestor.fetch_all(CYPHER_PROJECT_ROUTE_HANDLERS, params))
        except Exception:
            return []
        out: list[tuple[cs.NodeLabel, str, list[str], str | None]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            entry = _route_handler_entry(row, already_pending, module_qns)
            if entry is not None:
                out.append(entry)
        return out

    def _python_module_files(self) -> dict[str, Path]:
        # Re-parsed files first; on an incremental run the unchanged modules
        # come back from the graph's Module nodes.
        dp = self.factory.definition_processor
        files: dict[str, Path] = {
            qn: path
            for qn, path in dp.module_qn_to_file_path.items()
            if path.suffix == ".py"
        }
        for qn, path in self._graph_python_modules():
            files.setdefault(qn, path)
        return files

    def _python_module_asts(self, files: Mapping[str, Path]) -> dict[str, Node]:
        # Loaded through the disk-backed AST cache.
        asts: dict[str, Node] = {}
        for qn, path in files.items():
            entry = self.ast_cache.load(path)
            if entry is not None and entry[1] == cs.SupportedLanguage.PYTHON:
                asts[qn] = entry[0]
        return asts

    def _graph_python_modules(self) -> list[tuple[str, Path]]:
        if not isinstance(self.ingestor, QueryProtocol):
            return []
        params = {cs.KEY_PROJECT_PREFIX: self.project_name + cs.SEPARATOR_DOT}
        try:
            rows = list(self.ingestor.fetch_all(CYPHER_PROJECT_PY_MODULES, params))
        except Exception:
            return []
        out: list[tuple[str, Path]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            qn, rel_path = row.get(cs.KEY_QUALIFIED_NAME), row.get(cs.KEY_PATH)
            if isinstance(qn, str) and isinstance(rel_path, str):
                out.append((qn, self.repo_path / rel_path))
        return out

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
        # Contract files name the operations the generated artefacts on both
        # sides implement, so they anchor after the artefacts exist.
        anchored = link_contracts(
            self.ingestor,
            self.repo_path,
            self.project_name,
            self.exclude_paths,
            self.unignore_paths,
        )
        if anchored:
            logger.info("Anchored {} artefacts to contract operations", anchored)
            self.ingestor.flush_all()

    def _requeue_type_facts(
        self, node_type: NodeType, qn: str, path: str, row: ResultRow
    ) -> None:
        if node_type not in (NodeType.FUNCTION, NodeType.METHOD):
            return
        return_type = row.get(cs.KEY_RETURN_TYPE)
        param_types = row.get(cs.KEY_PARAM_TYPES)
        if not return_type and not param_types:
            return
        self.factory.definition_processor.pending_type_facts.append(
            PendingTypeFact(
                node_type.value,
                qn,
                base_module_qn(Path(path), self.project_name),
                return_type if isinstance(return_type, str) else None,
                [str(p) for p in param_types]
                if isinstance(param_types, list)
                else None,
            )
        )

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
        try:
            rows = self.ingestor.fetch_all(cs.CYPHER_ALL_DEFINITION_QNS, project_params)
        except Exception:
            # Rehydration completes cross-file resolution for files this run
            # did not re-parse: a FULL build parsed them all, so it degrades
            # to the freshly parsed registry; an incremental run would drop
            # cross-file edges, so the outage aborts it.
            if not self._is_full_build:
                raise
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return
        for row in rows:
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
                # Persisted annotations of UNCHANGED definitions rejoin the
                # type-edge queue (issue #1527): a changed file can add the
                # first resolvable type an old annotation names, and MERGE
                # makes re-emitting the already-known edges harmless.
                self._requeue_type_facts(node_type, qn, path, row)
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
        try:
            module_rows = self.ingestor.fetch_all(
                cs.CYPHER_ALL_MODULE_QNS, project_params
            )
        except Exception:
            if not self._is_full_build:
                raise
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return
        for row in module_rows:
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            label = row.get(cs.KEY_LABEL)
            if not isinstance(qn, str) or not isinstance(label, str):
                continue
            if label == cs.NodeLabel.MODULE_INTERFACE.value:
                self.factory.definition_processor.cpp_module_interfaces.add(qn)
            else:
                self._rehydrated_module_qns.add(qn)
        self._rehydrate_class_inheritance_from_graph()

    def _seed_module_qns_from_graph(
        self,
        eligible_paths: set[str],
        flux_stems: frozenset[str] | set[str] = frozenset(),
    ) -> None:
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
        # Only incremental runs seed (full builds process every file), so a
        # failed read here always aborts: a missing seed can silently let an
        # added file overwrite a sibling module's qn.
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
            # A survivor of a stem in flux re-parses unseeded (issue #1569).
            if _stem_key(path) in flux_stems:
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
        try:
            rows = self.ingestor.fetch_all(
                cs.CYPHER_ALL_INHERITS,
                {cs.KEY_PROJECT_PREFIX: self.project_name + "."},
            )
        except Exception:
            if not self._is_full_build:
                raise
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return
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

    def _rehydrate_csharp_type_locations(self) -> None:
        # Incremental runs fill csharp_type_locations only from re-parsed files,
        # but _join_csharp_partials resolves every partial-declaration location
        # against it. Restore the (path, start_line) -> qn entries for types in
        # UNCHANGED .cs files from the persisted graph so a partial part in an
        # unchanged file still joins its group (issue #1229). A re-parsed file's
        # fresh Pass-2 entry is kept (not overwritten); a stale rehydrated
        # position for a type that moved is simply never queried by the join.
        if not isinstance(self.ingestor, QueryProtocol):
            return
        locations = self.factory.definition_processor.csharp_type_locations
        try:
            rows = self.ingestor.fetch_all(
                cs.CYPHER_ALL_CSHARP_TYPE_LOCATIONS,
                {cs.KEY_PROJECT_PREFIX: self.project_name + "."},
            )
        except Exception:
            if not self._is_full_build:
                raise
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return
        restored = 0
        for row in rows:
            path = row.get(cs.KEY_PATH)
            start_line = row.get(cs.KEY_START_LINE)
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            if not (
                isinstance(path, str)
                and isinstance(start_line, int)
                and not isinstance(start_line, bool)
                and isinstance(qn, str)
            ):
                # bool is an int subclass; a stray True would key as line 1 and
                # shadow a real line-1 type, so reject it explicitly.
                continue
            key = (path, start_line)
            if key not in locations:
                locations[key] = qn
                restored += 1
        if restored:
            logger.info(ls.CSHARP_TYPE_LOCATIONS_REHYDRATED.format(count=restored))

    def _rehydrate_go_type_locations(self) -> None:
        # Incremental runs fill go_type_locations only from re-parsed files,
        # but _join_go_implements resolves BOTH ends of each pair against it.
        # Restore (path, line, col) -> (qn, label) for types in unchanged .go
        # files from the persisted graph (issue #1240). A pre-#1240 graph has
        # no start_col: those rows are skipped and behavior degrades to
        # today's until a re-index.
        if not isinstance(self.ingestor, QueryProtocol):
            return
        locations = self.factory.definition_processor.go_type_locations
        try:
            rows = self.ingestor.fetch_all(
                cs.CYPHER_ALL_GO_TYPE_LOCATIONS,
                {cs.KEY_PROJECT_PREFIX: self.project_name + "."},
            )
        except Exception:
            if not self._is_full_build:
                raise
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return
        restored = 0
        for row in rows:
            path = row.get(cs.KEY_PATH)
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            label = row.get(cs.KEY_LABEL)
            start_line = _persisted_int(row.get(cs.KEY_START_LINE))
            start_col = _persisted_int(row.get(cs.KEY_START_COL))
            if not (
                isinstance(path, str)
                and path.endswith(cs.EXT_GO)
                and isinstance(qn, str)
                and isinstance(label, str)
                and start_line is not None
                and start_col is not None
            ):
                continue
            key = (path, start_line, start_col)
            if key not in locations:
                locations[key] = (qn, label)
                restored += 1
        if restored:
            logger.info(ls.GO_TYPE_LOCATIONS_REHYDRATED.format(count=restored))

    def _rehydrate_function_locations(self) -> None:
        # The C#/Go semantic call + LINQ joins resolve targets against
        # function_locations, keyed (module_qn, line, col) at the definition
        # START; Go's join additionally keys at the NAME token, whose column
        # is persisted separately (issue #1240). Fresh Pass-2 entries win;
        # rehydrated records serve the joins only (Pass 3 touches re-parsed
        # files exclusively), so a METHOD's container comes from the query
        # and is_named stays True (generated qns never persist under Module
        # DEFINES).
        if not isinstance(self.ingestor, QueryProtocol):
            return
        locations = self.factory.definition_processor.function_locations
        params = {cs.KEY_PROJECT_PREFIX: self.project_name + "."}
        restored = 0
        for query in (
            cs.CYPHER_ALL_FUNCTION_LOCATIONS,
            cs.CYPHER_ALL_METHOD_LOCATIONS,
        ):
            try:
                rows = self.ingestor.fetch_all(query, params)
            except Exception:
                if not self._is_full_build:
                    raise
                logger.warning(ls.REHYDRATE_QUERY_FAILED)
                return
            restored += self._restore_function_rows(rows, locations)
        if restored:
            logger.info(ls.FUNCTION_LOCATIONS_REHYDRATED.format(count=restored))

    @staticmethod
    def _restore_function_rows(
        rows: list[ResultRow], locations: FunctionLocations
    ) -> int:
        restored = 0
        for row in rows:
            module_qn = row.get("module_qn")
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            label = row.get(cs.KEY_LABEL)
            container = row.get("container_qn")
            start_line = _persisted_int(row.get(cs.KEY_START_LINE))
            start_col = _persisted_int(row.get(cs.KEY_START_COL))
            name_line = _persisted_int(row.get(cs.KEY_NAME_START_LINE))
            name_col = _persisted_int(row.get(cs.KEY_NAME_START_COL))
            if not (
                isinstance(module_qn, str)
                and isinstance(qn, str)
                and isinstance(label, str)
                and start_line is not None
                and start_col is not None
            ):
                continue
            record = FunctionLocation(
                label=label,
                qualified_name=qn,
                container_qn=container if isinstance(container, str) else None,
            )
            keys = {(module_qn, start_line, start_col)}
            if name_col is not None:
                # The NAME token can sit on a LATER line than the declaration
                # start (a multiline Go receiver), so the alias keys at its
                # own persisted line, never the declaration's.
                keys.add((module_qn, name_line or start_line, name_col))
            for key in keys:
                if key not in locations:
                    locations[key] = record
                    restored += 1
        return restored

    def _affected_caller_keys(self, reindexed_keys: list[str]) -> list[str]:
        """Relative paths of files whose code depends on a re-indexed file.

        One level deep is complete: an affected caller's own DEFINITIONS are
        unchanged, so bindings in ITS callers cannot move. A failed read
        degrades to today's verbatim restore, never worse.
        """
        if not reindexed_keys or not isinstance(self.ingestor, QueryProtocol):
            return []
        try:
            rows = self.ingestor.fetch_all(
                cs.CYPHER_AFFECTED_CALLER_PATHS,
                {
                    cs.CYPHER_PARAM_PATHS: reindexed_keys,
                    cs.KEY_PROJECT_PREFIX: self.project_name + cs.SEPARATOR_DOT,
                },
            )
        except Exception:
            logger.warning(ls.PRUNE_QUERY_FAILED, label="affected callers")
            return []
        return sorted(
            {
                path
                for row in rows
                if isinstance(path := row.get(cs.KEY_CALLER_PATH), str) and path
            }
        )

    def _package_paths(self) -> set[str] | None:
        """Relative paths of this project's Package nodes, None if unreadable."""
        if not isinstance(self.ingestor, QueryProtocol):
            return set()
        try:
            rows = self.ingestor.fetch_all(cs.CYPHER_ALL_PACKAGE_PATHS)
        except Exception:
            logger.warning(ls.PRUNE_QUERY_FAILED, label="Package")
            return None
        prefix = self.project_name + cs.SEPARATOR_DOT
        paths: set[str] = set()
        for row in rows:
            path = row.get(cs.KEY_PATH)
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            if not isinstance(path, str) or not isinstance(qn, str):
                continue
            if qn == self.project_name or qn.startswith(prefix):
                paths.add(path)
        return paths

    def _package_flip_dirs(self) -> set[str]:
        """Directories that are a package now and were not, or the reverse."""
        was = self._packages_before_run
        if was is None:
            return set()
        now = {
            rel.as_posix()
            for rel, qn in self.factory.structure_processor.structural_elements.items()
            if qn
        }
        return was ^ now

    def _capture_inbound_edges(self, reindexed_keys: list[str]) -> list[ResultRow]:
        # Record the reference edges unchanged files point at the re-indexed
        # files, BEFORE those files' subtrees (and thus the inbound edges) are
        # deleted. Restoring the exact edges avoids re-resolving the callers,
        # whose resolution would diverge from a clean index (cgr resolution is
        # context-sensitive).
        if not reindexed_keys or not isinstance(self.ingestor, QueryProtocol):
            return []
        try:
            return self.ingestor.fetch_all(
                cs.CYPHER_INBOUND_EDGES, {cs.CYPHER_PARAM_PATHS: reindexed_keys}
            )
        except Exception:
            # A FULL build re-parses every caller, so nothing is lost and the
            # sync may continue; an incremental run cannot re-resolve edges
            # from files it will not parse, so the outage must abort it
            # rather than silently drop them.
            if not self._is_full_build:
                raise
            logger.warning(ls.INBOUND_CAPTURE_FAILED)
            return []

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
            # The edge's own properties (its site, issue #1522) come back with
            # it: per-site edges are keyed by them, so a bare re-emission
            # would land beside the original instead of restoring it.
            props = row.get(cs.KEY_PROPS)
            self._sink.ensure_relationship_batch(
                (caller_label, caller_key, caller_qn),
                rel,
                (target_label, target_key, target_qn),
                properties=cast(PropertyDict, props)
                if isinstance(props, dict) and props
                else None,
            )
            restored += 1
        if restored:
            logger.info(ls.INCREMENTAL_REBUILD_INBOUND, count=restored)

    def known_module_paths(self) -> dict[str, str]:
        # Module qns this updater has produced, mapped to file paths;
        # declared and rehydrated qns carry no path and never win the
        # Rust sub-scope ownership arbitration.
        known: dict[str, str] = {
            str(qn): path.as_posix()
            for qn, path in (
                self.factory.definition_processor.module_qn_to_file_path.items()
            )
        }
        for qn in self.factory.definition_processor.declared_module_qns:
            known.setdefault(qn, "")
        for qn in self._rehydrated_module_qns:
            known.setdefault(qn, "")
        return known

    def remove_file_from_state(
        self, file_path: Path, frontend_current: bool = False
    ) -> None:
        logger.debug(ls.REMOVING_STATE, path=file_path)

        if file_path in self.ast_cache:
            del self.ast_cache[file_path]
            logger.debug(ls.REMOVED_FROM_CACHE)

        # The call pass iterates _parsed_files; a removed file must leave it
        # (a re-parse re-registers), or deleted files keep contributing and
        # created files never do (issue #1028).
        self._parsed_files = [
            entry for entry in self._parsed_files if entry[0] != file_path
        ]

        relative_path = cached_relative_path(file_path, self.repo_path)
        path_parts = (
            relative_path.parent.parts
            if file_path.name == cs.INIT_PY
            else relative_path.with_suffix("").parts
        )
        path_derived_qn = cs.SEPARATOR_DOT.join([self.project_name, *path_parts])
        # Sweep on the qns the parse RECORDED for this path, not on the
        # path-derived form. The two differ whenever `base_module_qn` applies
        # its mod.rs collapse (src/a/mod.rs records as proj.src.a, while the
        # path yields proj.src.a.mod) or `_disambiguate_module_qn` suffixes a
        # qn an earlier-parsed sibling already owns. Keying on the path-derived
        # form swept a qn nothing ever recorded, so a mod.rs deletion left its
        # registry and simple-name entries behind and Pass 3 kept resolving
        # calls into definitions that no longer exist. The Rust and C# import
        # blocks below already re-derive the recorded qn for this reason.
        # A file that was never parsed recorded nothing; the path-derived form
        # is the only prefix available for it, and it owns no recorded qn that
        # this could wipe.
        recorded_qns = {
            qn
            for qn, path in (
                self.factory.definition_processor.module_qn_to_file_path.items()
            )
            if path == file_path
        }
        module_qn_prefixes = recorded_qns or {path_derived_qn}
        for prefix in module_qn_prefixes:
            self.factory.import_processor.commonjs_direct_exports.pop(prefix, None)
        # A deleted file is no writer: its mod-scope registry entries must
        # not weigh in the next arbitration (a modified file re-drops this
        # in its own re-parse, harmlessly twice). The drop keys on the qn
        # the parse actually RECORDED, which can differ from any path-derived
        # form twice over: mod.rs collapses to its directory, and
        # _disambiguate_module_qn suffixes a qn whose bare form an
        # earlier-parsed file already owns (src/a/mod.rs beside src/a.rs or
        # src/a.py records as ...a.rs). Recomputing from the path would wipe
        # the bare qn's real owner, which this event does not re-parse, while
        # the deleted file's own writers kept voting forever. A Rust file
        # with no recorded qn was never parsed and owns no import state.
        if file_path.suffix == cs.EXT_RS:
            qn_to_path = self.factory.definition_processor.module_qn_to_file_path
            for qn, path in qn_to_path.items():
                if path == file_path:
                    self.factory.import_processor.drop_rust_module_import_state(qn)

        # Same discipline for C#: a deleted file's namespaces, identifier
        # evidence, and using entries must not survive into the next flush
        # (issue #1347).
        if file_path.suffix == cs.EXT_CS:
            qn_to_path = self.factory.definition_processor.module_qn_to_file_path
            for qn, path in qn_to_path.items():
                if path == file_path:
                    self.factory.import_processor.drop_csharp_module_import_state(qn)

        # Ownership guard for the prefix sweep: another file's inline-mod
        # chain can share this file's qn prefix (the #1017 shape), and a
        # sweep by prefix alone deregisters functions that file still owns —
        # they are never re-registered because it is not re-parsed. The span
        # records key by the REGISTERING file's module qn, so a qn recorded
        # under a module this event does not touch survives (issue #1025).
        touched_modules = recorded_qns
        foreign_qns = {
            location.qualified_name
            for (
                span_module,
                _line,
                _col,
            ), location in self.factory.definition_processor.function_locations.items()
            if span_module not in touched_modules
        }
        # Frontend registrations (libclang C/C++) have no span records; their
        # ownership is tracked per rel path at registration time.
        touched_rel = relative_path.as_posix()
        for owner_rel, owner_qns in self._frontend_owned_qns.items():
            if owner_rel != touched_rel:
                foreign_qns |= owner_qns
        if frontend_current:
            # Incremental full run in LIBCLANG mode: the frontend ALREADY
            # re-registered this file's current state before this cleanup,
            # and the tree-sitter pass will skip it as covered — sweeping
            # here would wipe registrations nothing restores (issue #1025
            # review). Watch events keep the default: the frontend does not
            # rerun there, so its stale entries must go.
            foreign_qns |= self._frontend_owned_qns.get(touched_rel, set())

        qns_to_remove = set()

        for qn in list(self.function_registry.keys()):
            if (
                any(
                    qn.startswith(f"{prefix}.") or qn == prefix
                    for prefix in module_qn_prefixes
                )
                and qn not in foreign_qns
            ):
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

        # The file no longer owns any module qn: a replacement with the same
        # stem (util.js -> util.ts) must take the bare qn a clean index gives
        # it, and a re-parse re-registers its own entry (issue #1575).
        qn_to_path = self.factory.definition_processor.module_qn_to_file_path
        # The qn read back from the graph goes with the map entry, keyed on
        # the qn the graph RECORDED (a disambiguated `util.py` beside
        # `util.js`, a `mod.rs` directory), never on the path-derived prefix,
        # which for a same-stem sibling names the SURVIVOR's qn.
        # `known_module_paths` would otherwise keep offering the deleted
        # module to deferred resolution as a live internal target on a
        # reused updater (issue #1575).
        for qn in [qn for qn, path in qn_to_path.items() if path == file_path]:
            del qn_to_path[qn]
            self._rehydrated_module_qns.discard(qn)
        # The paths read back from the graph for an earlier incremental run
        # go with the registry entries: `_module_language` falls through to
        # them for a definition qn, and a replacement in another language
        # (util.rs -> util.py) would otherwise still answer RUST for the qn
        # its new file re-registers (issue #1575).
        rehydrated = self.factory.definition_processor.rehydrated_definition_paths
        for qn in qns_to_remove:
            rehydrated.pop(qn, None)

    def _existing_module_paths(self) -> frozenset[str] | None:
        """Paths of this project's Module nodes already in the graph.

        Empty when the sink has no query surface (offline writers, unit
        fakes) or answers with something that is not rows: such a sink holds
        no readable state worth deleting first. None when the sink CLAIMS
        readability but the query itself failed: the graph state is unknown,
        and treating it as empty would skip every delete and recreate the
        stale accumulation this probe exists to prevent.
        """
        fetch_all = getattr(self.ingestor, "fetch_all", None)
        if fetch_all is None:
            return frozenset()
        try:
            rows = fetch_all(
                cs.CYPHER_PROJECT_MODULE_PATHS,
                {
                    cs.KEY_PROJECT_NAME: self.project_name,
                    cs.KEY_PROJECT_PREFIX: self.project_name + ".",
                },
            )
        except Exception:
            return None
        try:
            return frozenset(
                path
                for row in rows
                if isinstance(path := row.get(cs.KEY_PATH), str)
                and not path.startswith(cs.INLINE_MODULE_PATH_PREFIX)
            )
        except (TypeError, AttributeError):
            return frozenset()

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
                cs.CYPHER_DELETE_MODULE,
                {
                    cs.KEY_PATH: file_key,
                    cs.KEY_PROJECT_NAME: self.project_name,
                    cs.KEY_PROJECT_PREFIX: self.project_name + ".",
                },
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
            if should_skip_rel_file(
                f"{prefix}{name}",
                dir_parts,
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
        return should_keep_dir(
            dirname, dir_prefix, self.exclude_paths, self.unignore_paths
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
        try:
            rows = fetch_all(
                cs.CYPHER_COUNT_PROJECT_MODULES,
                {
                    cs.KEY_PROJECT_NAME: self.project_name,
                    cs.KEY_PROJECT_PREFIX: self.project_name + ".",
                },
            )
        except Exception:
            # A graph that cannot answer (connection refused, a sink that
            # rejects reads) cannot invalidate the cache: fail open.
            return
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
        if stored is None or stored != compute_parser_fingerprint(
            repo_path=self.repo_path, capture=self.capture
        ):
            logger.warning(ls.PARSER_FINGERPRINT_MISMATCH)

    def _exclusions_match_last_run(self) -> bool:
        # Memoised: `_process_files` asks again to decide whether the graph's
        # own module paths must join reconciliation, and the answer must be
        # the same one the sync check acted on, logged once.
        if self._exclusion_match is not None:
            return self._exclusion_match
        stored = _load_exclusion_state(self.repo_path / cs.EXCLUSION_STATE_FILENAME)
        current = _exclusion_state(self.exclude_paths, self.unignore_paths)
        if stored == current:
            self._exclusion_match = True
            return True
        # A missing stamp means an index built before it existed, whose
        # exclusion set is unknown rather than known-equal -- the posture
        # `_warn_if_parser_changed` takes for a missing fingerprint. Reported
        # apart from a real change so a user seeing an unexpected full pass
        # can tell which of the two they are looking at.
        if stored is None:
            logger.info(ls.EXCLUSION_STATE_MISSING)
        else:
            logger.info(
                ls.EXCLUSION_SET_CHANGED.format(previous=stored, current=current)
            )
        self._exclusion_match = False
        return False

    def _is_already_in_sync(self) -> bool:
        if self._delombok_state_changed:
            # The overlay's effect changed while the checked-in bytes did
            # not; the affected files must reparse.
            return False
        if self._single_file is not None:
            return False
        cache_path = self.repo_path / cs.HASH_CACHE_FILENAME
        if not cache_path.is_file():
            return False
        # Nothing on disk changes when only the exclusion set does, so no
        # hash or directory mtime below can see it: a file excluded by a CLI
        # flag alone would keep the Module, Class and Method nodes the last
        # run gave it (issue #1606). `.cgrignore` was covered only
        # incidentally, by being an indexed and hashed file itself.
        if not self._exclusions_match_last_run():
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

    def _register_generated_sources(self) -> None:
        # Annotation-processor output next to a build file (issue #1140):
        # carve those exact subtrees out of the target/build prune, register
        # them as Java import-probe roots, and stamp their modules generated.
        # Recomputed per run so a build that appears between watch runs is
        # picked up; no roots leaves everything exactly as before.
        roots = discover_generated_source_roots(self.repo_path)
        dp = self.factory.definition_processor
        dp.generated_source_prefixes = generated_prefixes_for(roots)
        self.factory.import_processor.set_java_generated_roots(roots)
        # Rebuilt from the CONFIGURED base every run, never accumulated: a
        # root that vanished (target/ cleaned) must stop rescuing its subtree.
        patterns = unignore_patterns_for(roots)
        base = (
            frozenset(self._configured_unignore_paths)
            if self._configured_unignore_paths
            else frozenset()
        )
        combined = base | patterns
        resolved = combined if combined else None
        self.unignore_paths = resolved
        # The factory-owned processors hold their own copies for structure
        # traversal and import-time walks; they must prune identically or a
        # sweep could read files the indexer skipped (issue #1088 invariant).
        self.factory.import_processor.unignore_paths = resolved
        self.factory.structure_processor.unignore_paths = resolved
        if roots:
            logger.info(ls.GENERATED_SOURCES_REGISTERED, count=len(roots))
        # Delombok overlay (issue #1140 tier 1): rebuilt per run so a jar or
        # annotation appearing between watch runs is picked up; empty means
        # raw parsing everywhere, exactly as before. The persisted identity
        # detects overlay CHANGES (jar appears/vanishes, version bump): the
        # affected files' cached hashes still match the checked-in source,
        # so without forcing them through a reparse the graph would keep
        # stale (or never gain) generated members.
        self._delombok_overlay = build_delombok_overlay(self.repo_path)
        previous = _load_delombok_state(self.repo_path / cs.DELOMBOK_STATE_FILENAME)
        current = {
            "identity": overlay_identity(self._delombok_overlay),
            "keys": sorted(self._delombok_overlay),
            # Two Lombok versions can expand identically today and diverge on
            # the next annotation edit; the version keeps the state honest.
            "lombok": current_lombok_identity(),
        }
        self._delombok_state_changed = previous != current
        self._delombok_stale_keys = (
            set(previous.get("keys", ())) | set(current["keys"])
            if self._delombok_state_changed
            else set()
        )
        # Held in memory until the run SUCCEEDS: persisting now would let a
        # crashed run convince its successor that the stale files were
        # already reprocessed. Single-file runs never commit it either.
        self._delombok_state_candidate = current

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
        self._collected_dir_mtimes = {}

        def _record_dir_mtime(dir_key: str, dirpath: str) -> None:
            try:
                self._collected_dir_mtimes[dir_key] = os.stat(dirpath).st_mtime
            except OSError:
                pass

        # One shared walk with the C++ module-qn map, so the two cannot drift
        # in ORDER as well as in membership (issues #1025, #1099).
        for dirpath, fname, rel_path_str in walk_eligible_files(
            self.repo_path,
            self.exclude_paths,
            self.unignore_paths,
            on_dir=_record_dir_mtime,
        ):
            eligible.append((Path(f"{dirpath}/{fname}"), rel_path_str))
        return eligible

    def _process_files(self, force: bool = False) -> None:
        self.factory.import_processor.reset_rust_path_caches()
        self.factory.import_processor.reset_java_path_caches()
        cache_path = self.repo_path / cs.HASH_CACHE_FILENAME
        dir_mtimes_path = self.repo_path / cs.DIR_MTIMES_FILENAME
        old_hashes = _load_hash_cache(cache_path) if not force else {}
        # Snapshot for the single-file merge, read from DISK and independently
        # of `force`. Two distinct reasons it cannot reuse `old_hashes`:
        #
        # 1. the stale-key pop below -- a single-file run does not commit the
        #    delombok state (see `run`), so it must not act on that pop
        #    either. Merging over the popped dict would DROP every
        #    Lombok-affected sibling, which then takes the `is_new` path next
        #    run and skips delete-before-reingest, leaving the previous
        #    parse's entities beside the fresh ones (#1619 review).
        # 2. `force` -- which empties `old_hashes` to make this run RE-PARSE
        #    what it walked. A single-file run walked one file, so `force`
        #    says nothing about the siblings it never looked at. Reusing the
        #    emptied dict would persist a cache holding only the target and
        #    erase every sibling's hash, which is the same data loss the
        #    unforced path already guards (#1619 review, round 2).
        #
        # Only the single-file commit reads this; a project run replaces the
        # cache wholesale from its own complete walk, as it always has.
        pristine_hashes = (
            _load_hash_cache(cache_path) if self._single_file is not None else {}
        )
        for stale_key in self._delombok_stale_keys:
            old_hashes.pop(stale_key, None)
        is_full_build = (force or not old_hashes) and self._single_file is None
        self._is_full_build = is_full_build
        cache_mtime = cache_path.stat().st_mtime if cache_path.is_file() else 0.0
        if force:
            logger.info(ls.INCREMENTAL_FORCE)

        _touch_empty_json(cache_path)
        _touch_empty_json(dir_mtimes_path)

        eligible_files = self._collect_eligible_files()

        # Stems with a same-stem sibling added or deleted this run: their
        # survivors are not seeded and re-parse below, so the bare qn goes to
        # whichever file the clean rule (first in walk order) gives it,
        # rather than to whichever file was indexed first (issue #1569).
        eligible_keys = {key for _fp, key in eligible_files}
        flux_stems = (
            set()
            if is_full_build
            else {
                _stem_key(key)
                for key in (eligible_keys - old_hashes.keys())
                | (old_hashes.keys() - eligible_keys)
            }
        )
        if not is_full_build:
            self._seed_module_qns_from_graph(eligible_keys, flux_stems)
        # A full build can still land on a graph that already holds this
        # project: the cache lives in the repo working tree, the graph does
        # not, so a fresh clone of an indexed repo (or a force run) parses
        # every file as "new" while the previous parse's state is still
        # there. A file whose Module already exists must take the
        # delete-before-reingest path or renamed-away symbols and their
        # CALLS/REFERENCES edges accumulate alongside the fresh parse.
        # A changed exclusion set has to reconcile against the GRAPH, not just
        # against the hash cache. Since #1615 the cache commits only after the
        # flush, so a crashed run cannot leave one that has already forgotten
        # the file. The graph query is still required for a different reason:
        # the cache records only the files eligible on the run that wrote it,
        # so a file excluded by an EARLIER completed run was already dropped
        # from it and no on-disk hash names it any more. Only the graph still
        # does. It costs a query only on the runs where the set actually
        # moved.
        # A single-file run whose cache cannot name the target needs this too,
        # for the same reason a full build does. `is_new` would otherwise fall
        # through to True on a file the previous parse already indexed, skip
        # delete-before-reingest, and stack a second parse's entities on the
        # first -- renamed-away symbols and their CALLS/REFERENCES edges
        # surviving beside the fresh ones (#1619 review, round 3).
        #
        # The condition mirrors `is_full_build` above deliberately: `force`
        # empties `old_hashes`, and a missing or evicted cache leaves it empty
        # the same way, so both must ask the graph. Guarding on `force` alone
        # left the identical defect on a fresh clone of an already-indexed
        # repo -- the cache lives in the working tree, the graph does not --
        # and after any cache eviction (#1619 review, round 4).
        cacheless_single_file = (force or not old_hashes) and (
            self._single_file is not None
        )
        preexisting_paths = (
            self._existing_module_paths()
            if is_full_build
            or cacheless_single_file
            or not self._exclusions_match_last_run()
            else frozenset()
        )
        # None is "the sink claims readability and the query failed", not
        # "nothing there": below it falls back to the hash cache alone, which
        # cannot name a file whose exclusion predates it. That run completes
        # and would stamp, and every later run would fast-path over the
        # surviving subtree. Remembering the failure keeps the stamp back.
        if preexisting_paths is None:
            self._graph_state_unknown = True
        new_hashes: FileHashCache = {}
        skipped_count = 0
        changed_count = 0
        unreadable_count = 0
        unreadable_keys: set[str] = set()

        current_file_keys: set[str] = set()

        processed_since_flush = 0

        # Taken BEFORE the loop below, so it is at or earlier than every hash
        # it describes. `_is_already_in_sync` skips rehashing any file whose
        # mtime is at or below the CACHE FILE's own mtime, so deferring the
        # write must not defer that stamp: a file edited after its hash was
        # taken but before the commit point would otherwise be stamped newer
        # than its own edit and skipped for good (issue #1615 review).
        # Capturing here rather than after the loop also covers a file edited
        # between its own hash and the end of the loop. Erring early only ever
        # costs a redundant rehash; erring late loses an edit.
        self._pending_cache_observed_at = time.time()

        changed_entries: list[tuple[Path, str, bool, bytes]] = []
        for filepath, file_key in eligible_files:
            if not force and file_key in old_hashes:
                try:
                    file_mtime = filepath.stat().st_mtime
                except OSError:
                    unreadable_count += 1
                    unreadable_keys.add(file_key)
                    continue
                if file_mtime <= cache_mtime:
                    new_hashes[file_key] = old_hashes[file_key]
                    current_file_keys.add(file_key)
                    skipped_count += 1
                    continue

            hashed = _hash_file_with_bytes(filepath)
            if hashed is None:
                unreadable_count += 1
                unreadable_keys.add(file_key)
                continue
            current_hash, file_bytes = hashed
            # The hash keys the CHECKED-IN source (cache invalidation follows
            # edits); the PARSE may consume the delomboked expansion instead,
            # so Lombok-generated members become real graph nodes (#1140).
            file_bytes = self._delombok_overlay.get(file_key, file_bytes)

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

            is_new = (
                file_key not in old_hashes
                and preexisting_paths is not None
                and file_key not in preexisting_paths
            )
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
        # A file depending on a re-indexed one can have its bindings moved by
        # the change (a new override shadowing an inherited method); restoring
        # its old edges verbatim would freeze the stale binding, so it joins
        # the re-parse set BEFORE capture: the capture query then excludes
        # edges among the expanded set and Pass 3 recomputes them, while its
        # own inbound edges are captured and restored like any re-indexed
        # file's (issue #1229 phase 4).
        present = {file_key for _fp, file_key, _new, _b in changed_entries}
        eligible_by_key = {file_key: fp for fp, file_key in eligible_files}
        # A dependent of a DELETED file is in the same position: its edges
        # into that file go with the subtree and cannot be restored, and a
        # clean index would re-derive them from what remains (a phantom
        # external parent, a fallback, or nothing). The deleted set is what
        # the cache remembers and the walk no longer finds; it joins the
        # dependents query now, while the subtrees still exist to match
        # against (issue #1567).
        # `old_hashes` has already had the Delombok-stale keys popped (so a
        # changed overlay forces those files through a reparse), which would
        # otherwise hide a file that is BOTH stale and deleted: it would be
        # absent from the cache set here and never join the dependents query.
        # Union them back in; the walk-eligible subtraction below drops the
        # ones that still exist (issue #1567).
        # A file the graph still holds but neither the cache nor the walk
        # names (a cacheless or forced rebuild over an existing graph) must
        # go before the parse too, for the same-stem reason as above: a
        # surviving sibling parsed first would otherwise claim its qn and
        # the post-parse delete by path would find nothing.
        graph_only_gone = (
            preexisting_paths - eligible_by_key.keys() - unreadable_keys
            if preexisting_paths
            else frozenset()
        )
        # Empty on a single-file run, for the same reason `deleted_keys`
        # below is: every term here is PROJECT-WIDE (the whole hash cache, the
        # whole Delombok-stale set, every module the graph holds), and the
        # only thing subtracted is `eligible_by_key` -- which on a single-file
        # run names just the walked file. So each sibling falls straight
        # through and is deleted before the parse, which is the sweep #1619
        # exists to stop. It was previously bounded only by accident, because
        # nothing on this path populated the set (issue #1671); that is not a
        # property to rely on, so the scope rule is stated here.
        deleted_before_parse = (
            sorted(
                (set(old_hashes) | self._delombok_stale_keys | graph_only_gone)
                - eligible_by_key.keys()
                - unreadable_keys
            )
            if self._single_file is None
            else []
        )
        # A directory whose package-ness flipped since the last run (an
        # __init__.py or Cargo.toml added or deleted) changes the container
        # every module directly under it hangs off; that edge is emitted only
        # when the module is parsed, so the siblings re-parse too. The stale
        # Package or Folder node itself is pruned at the end of the run
        # (issue #1570).
        flipped_dirs = self._package_flip_dirs() if not is_full_build else set()
        siblings = {
            key
            for key in eligible_by_key
            if (flipped_dirs and Path(key).parent.as_posix() in flipped_dirs)
            or (flux_stems and _stem_key(key) in flux_stems)
        }
        # The siblings join the dependents query: a flux-stem survivor's
        # module qn changes on this run, so a file that includes or calls it
        # must re-parse too, or its captured inbound edges are restored
        # against the old qn (a wrong IMPORTS edge, a dropped CALLS edge).
        affected = 0
        for caller_key in sorted(
            {
                *self._affected_caller_keys(
                    sorted({*reindexed_keys, *deleted_before_parse, *siblings})
                ),
                *siblings,
            }
        ):
            if caller_key in present:
                continue
            caller_path = eligible_by_key.get(caller_key)
            if caller_path is None or not caller_path.is_file():
                continue
            try:
                caller_bytes = caller_path.read_bytes()
            except OSError:
                continue
            caller_bytes = self._delombok_overlay.get(caller_key, caller_bytes)
            changed_entries.append((caller_path, caller_key, False, caller_bytes))
            present.add(caller_key)
            affected += 1
        if affected:
            logger.info(ls.INCREMENTAL_AFFECTED_CALLERS, count=affected)
            reindexed_keys = sorted(
                file_key for _fp, file_key, is_new, _b in changed_entries if not is_new
            )
        # Pass 2 order decides which same-stem file claims the bare module
        # qn; a clean build processes files in walk order, so the re-parse
        # set follows it too instead of the order the dependents were found.
        eligible_order = {key: i for i, (_fp, key) in enumerate(eligible_files)}
        changed_entries.sort(key=lambda entry: eligible_order.get(entry[1], -1))
        # A caller that lives in a DELETED file must not be restored: its
        # module is gone, and if a surviving same-stem sibling has just taken
        # over its qn the restore would hang the dead file's edge on the
        # survivor (issue #1569).
        deleted_set = set(deleted_before_parse)
        captured_inbound = [
            row
            for row in self._capture_inbound_edges(reindexed_keys)
            if row.get(cs.KEY_CALLER_PATH) not in deleted_set
        ]
        self._reparsed_file_keys = {
            file_key for _fp, file_key, _new, _b in changed_entries
        }

        # Every old subtree goes BEFORE any file of this run is parsed, not
        # one by one as each file is reached: a same-stem sibling parsed
        # earlier claims the survivor's old module qn, the MERGE lands on the
        # old node and rewrites its path, and the per-file delete by path
        # then finds nothing, leaving the old definitions beside the new
        # ones (issue #1569). This loop is the only graph delete on the
        # re-parse path: every non-new entry is in reindexed_keys, so the
        # per-file loop below only clears local state. It runs only once
        # every changed file has pre-parsed: a parse failure then leaves
        # the old subtrees in place instead of an emptied graph.
        pre_parsed = self._pre_parse_changed_files(changed_entries)
        for stale_key in (*reindexed_keys, *deleted_before_parse):
            self._delete_module_entities(stale_key)
        # The in-memory side of a deleted file goes now as well: a reused
        # updater (the watcher's `remove_file_from_state` path, or any caller
        # holding one across `run()` calls) otherwise resolves this
        # run's re-parsed importers against the deleted definitions and
        # suffixes a same-stem replacement's qn (issue #1575). The late
        # deletion block repeats this harmlessly.
        for deleted_key in deleted_before_parse:
            self.remove_file_from_state(self.repo_path / deleted_key)
        first_failure: Exception | None = None

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
                    self.remove_file_from_state(
                        filepath,
                        frontend_current=bool(self._cpp_frontend_covered)
                        and cached_relative_path(filepath, self.repo_path).as_posix()
                        in self._cpp_frontend_covered,
                    )

                changed_count += 1
                # Every stale subtree went in the loop above, so a failure
                # here must not abort this loop: every module after it would
                # stay deleted and never be rebuilt. The remaining files are
                # processed, the deletion reconciliation and inbound-edge
                # restore below still run, and the first error is re-raised
                # before the cache commit so the next run retries the lot.
                try:
                    self._process_single_file(
                        filepath,
                        file_bytes=file_bytes,
                        pre_parsed=pre_parsed.get(filepath),
                    )
                except Exception as exc:
                    logger.error(ls.INCREMENTAL_FILE_FAILED, path=filepath, error=exc)
                    if first_failure is None:
                        first_failure = exc

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

        # On a cacheless rebuild old_hashes cannot name what disappeared, so
        # the graph's own module paths join the reconciliation: a file that
        # was deleted OR newly excluded since the previous index is absent
        # from current_file_keys and its subtree must go. (Disk-deleted files
        # are also swept by orphan pruning; excluded ones still exist on disk
        # and only this reconciliation catches them.) A file that merely
        # could not be READ this run still exists: sweeping it would erase
        # live state over a transient error, so unreadable keys are exempt.
        graph_paths = (
            preexisting_paths if preexisting_paths is not None else frozenset()
        )
        # A single-file run walked exactly one file, so `current_file_keys`
        # names only that file and every sibling looks deleted. It cannot
        # speak for the project's file set, for the same reason `run` guards
        # the exclusion stamp with `self._single_file is None` (#1619).
        deleted_keys = (
            (set(old_hashes.keys()) | graph_paths) - current_file_keys - unreadable_keys
            if self._single_file is None
            else set()
        )
        if deleted_keys:
            logger.info(ls.INCREMENTAL_DELETED, count=len(deleted_keys))
            for deleted_key in deleted_keys:
                deleted_path = self.repo_path / deleted_key
                # A key cleared before the parse gets neither a second state
                # drop nor a second module delete: the state drop keys on the
                # module qn, which a same-stem replacement parsed since has
                # taken over (its CommonJS export entry would go with it).
                # Only the File node is still to go.
                if deleted_key not in deleted_set:
                    self.remove_file_from_state(deleted_path)
                    self._delete_module_entities(deleted_key)
                if isinstance(self.ingestor, QueryProtocol):
                    # Keyed on the absolute path: a sibling project's File
                    # node can share the relative path (issue #897).
                    self.ingestor.execute_write(
                        cs.CYPHER_DELETE_FILE,
                        {cs.KEY_PATH: deleted_path.resolve().as_posix()},
                    )

        self._restore_inbound_edges(captured_inbound)

        if skipped_count > 0:
            logger.info(ls.INCREMENTAL_SKIPPED, count=skipped_count)
        if changed_count > 0:
            logger.info(ls.INCREMENTAL_CHANGED, count=changed_count)
        if unreadable_count > 0:
            logger.info(ls.INCREMENTAL_UNREADABLE, count=unreadable_count)
        if first_failure is not None:
            raise first_failure

        # Deferred to the post-flush commit point in `run` (issue #1615). The
        # deletions above are execute_write calls that only become durable at
        # that flush; committing the cache here would let a run that died in
        # between tell its successor the file was already reconciled, and the
        # successor computes an empty `deleted_keys` and never re-issues the
        # delete. The subtree then stays in the graph until a full rebuild.
        if self._single_file is None:
            self._pending_hash_cache = (cache_path, new_hashes)
            self._pending_dir_mtimes = (dir_mtimes_path, self._collected_dir_mtimes)
        else:
            # Two different remedies, because the run has different standing
            # on each (#1619). It DID hash its one file, so that hash is worth
            # recording -- merged over the previous run's entries, never
            # replacing them, or the next full run treats every sibling as
            # new. It collected NO directory mtimes at all
            # (`_collect_eligible_files` returns above the walk that populates
            # them), so there is nothing to merge and writing the empty
            # collection would wipe the previous walk's record wholesale.
            # ...but only when there IS a previous run's cache to merge over.
            # With none (missing, empty or unreadable), publishing the one
            # hash this run took would create a cache naming a single file,
            # which the next PROJECT run reads as authoritative: every
            # sibling is then absent from `old_hashes`, takes the `is_new`
            # path, skips delete-before-reingest, and an edited sibling's
            # previous entities survive beside the fresh parse. Writing
            # nothing keeps the next project run a full build, which
            # reconciles properly (#1619 review, round 5). Measured: with the
            # cache removed, a single-file run then a project run left
            # `module_b.py` undeleted after an edit.
            self._pending_hash_cache = (
                (cache_path, {**pristine_hashes, **new_hashes})
                if pristine_hashes
                else None
            )
            # And it must carry the PREVIOUS cache's mtime forward rather
            # than stamping this run's instant. The merged entries are mostly
            # the previous run's hashes, so stamping now would assert every
            # sibling was observed just now: a sibling edited before this run
            # then satisfies `file_mtime <= cache_mtime` with a hash that
            # still matches, and the next project run reports "already in
            # sync" and never indexes the edit (#1619 review).
            #
            # Note this must be the previous mtime, NOT None. None skips the
            # `os.utime` in `run` altogether, which leaves the file its own
            # write time -- LATER than this run's instant, so it makes the
            # trap worse rather than closing it. `cache_mtime` was read at the
            # top of this method, before anything wrote to the cache.
            self._pending_cache_observed_at = cache_mtime or None
        # Stamp only full builds: re-stamping an incremental run would
        # silence the staleness warning while unchanged files still carry
        # the old parser's edges.
        if is_full_build:
            _save_parser_fingerprint(
                self.repo_path / cs.PARSER_FINGERPRINT_FILENAME,
                compute_parser_fingerprint(
                    repo_path=self.repo_path, capture=self.capture
                ),
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
        """Ingest one file through the first tier that claims it.

        Tries tree-sitter, then dependency manifests, then the secondary
        tiers; every file gets a File node regardless, so an unclaimed
        extension is still represented in the graph.
        """
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
        else:
            self.process_with_secondary_tier(filepath)

        self.factory.structure_processor.process_generic_file(filepath, filepath.name)

    def process_with_secondary_tier(self, filepath: Path) -> bool:
        """Parse a file with whichever non-tree-sitter tier claims it.

        Shared with the file watcher, which re-parses one changed file and
        would otherwise handle only tree-sitter languages: it deletes a
        file's Module (and everything hanging off it) before re-parsing, so
        a tier it does not know about loses its symbols entirely rather than
        merely going stale (issue #1427).

        Returns True when a tier took the file, so the batch path can keep
        its if/elif chain.
        """
        structural_elements = self.factory.structure_processor.structural_elements
        if self.ast_grep_tier.handles(filepath.suffix):
            self.ast_grep_tier.process_file(filepath, structural_elements)
            return True
        if self.document_tier.handles(filepath.suffix):
            self.document_tier.process_file(filepath, structural_elements)
            return True
        return False

    def _ast_for(self, file_path: Path) -> Node | None:
        """The cached AST root for a file, or None when it is not cached."""
        entry = self.ast_cache.load(file_path)
        return entry[0] if entry else None

    def _load_ast_from_disk(
        self, file_path: Path
    ) -> tuple[Node, cs.SupportedLanguage] | None:
        """Re-parse a file the AST cache evicted, for BoundedASTCache.

        Evicted files carry stale captures (nodes from the discarded tree),
        so those are dropped: downstream recomputes them from the fresh tree.
        """
        language = get_language_for_extension(file_path.suffix)
        if language is None or language not in self.parsers:
            return None
        parser = self.queries[language].get(cs.KEY_PARSER)
        if parser is None:
            return None
        try:
            file_bytes = file_path.read_bytes()
            overlay_key = cached_relative_path(file_path, self.repo_path).as_posix()
            file_bytes = self._delombok_overlay.get(overlay_key, file_bytes)
        except OSError as e:
            logger.error(ls.AST_RELOAD_FAILED, path=file_path, error=e)
            return None
        root_node = parse_with_preproc_recovery(parser, file_bytes, language).root_node
        self.factory._func_class_captures_cache.pop(file_path, None)
        return (root_node, language)

    def register_parsed_file(
        self, file_path: Path, language: cs.SupportedLanguage
    ) -> None:
        # Watch-mode events parse outside run(): the file must join the
        # call pass's iteration set or its outgoing CALLS edges are never
        # emitted (issue #1028).
        if all(existing != file_path for existing, _ in self._parsed_files):
            self._parsed_files.append((file_path, language))

    def _process_function_calls(self, only: Collection[Path] | None = None) -> None:
        # `only` scopes the pass to a re-ingested subset (issue #1524); every
        # other file keeps the edges it already has.
        # A reused updater (watch mode, a second run) re-parses files; the
        # JS receiver-binding index holds nodes from the PREVIOUS parse,
        # whose spans would resolve against the refreshed registry onto
        # whatever now sits at the old line/column.
        self.factory.call_processor.reset_js_receiver_bindings()
        captures_cache = self.factory._func_class_captures_cache
        # Iterate every file parsed this run, not the bounded AST cache: on a
        # large repo the cache evicts most files, and iterating it drops their
        # calls (a whole module ends up with zero CALLS edges).
        files = (
            self._parsed_files
            if only is None
            else [(fp, lang) for fp, lang in self._parsed_files if fp in only]
        )
        for file_path, language in files:
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
            self.factory.call_processor.collect_js_symbol_bindings(
                file_path, root_node, language
            )
        # Bindings are pending until every file's ctor metadata (param order,
        # param->attribute renames) is in: a construction site may be scanned
        # before the file defining its class. Symbol-keyed installations wait
        # the same way: a `[kX]: value` site may be swept before the file
        # defining kX.
        self.factory.call_processor.finalize_callable_field_bindings()
        self.factory.call_processor.finalize_js_symbol_bindings()
        for file_path, language in files:
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
        self.factory.call_processor.finalize_dispatch()

    # --- Scoped re-ingest (issue #1524) --------------------------------------

    def _reingest_target(self, raw: Path | str) -> tuple[str, Path]:
        """(relative posix key, absolute path) for a file inside the repo.

        Relative inputs resolve against the repo root; anything escaping it
        (``..`` segments, symlinks out) is refused rather than silently
        clamped, since the caller is an agent naming files it just wrote.
        """
        path = Path(raw)
        if not path.is_absolute():
            path = self.repo_path / path
        resolved = path.resolve()
        root = self.repo_path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(cs.REINGEST_OUTSIDE_REPO.format(path=raw))
        key = resolved.relative_to(root).as_posix()
        return key, self.repo_path / key

    def _hydrate_for_reingest(self) -> None:
        # A fresh updater (the MCP tool builds one per project) holds no
        # registry, so every cross-file call from the re-parsed files would
        # go unresolved; read the definitions back once, the way an
        # incremental run() does. A reused updater already has live state.
        if self._reingest_hydrated or self._parsed_files:
            self._reingest_hydrated = True
            return
        # Pass 1 of run(): without the package map a re-parsed module hangs
        # off a Folder instead of its Package. Generated-source roots and the
        # delombok overlay are per-run inputs run() computes before Pass 2;
        # a fresh updater must compute them too or a Lombok class re-parses
        # without its generated members.
        self.factory.structure_processor.identify_structure()
        self._rehydrate_registry_from_graph()
        self._rehydrate_function_locations()
        self._reingest_hydrated = True

    def _rerun_frontends_for(self, languages: set[cs.SupportedLanguage]) -> None:
        # Semantic facts are location-keyed against the compiler's view of
        # the whole module, so a change in one file can rebind calls in
        # unchanged files (issue #1229 phase 3): the applicable frontend
        # re-runs and its joins re-emit before the scoped call pass.
        if cs.SupportedLanguage.GO in languages:
            self._run_go_frontend()
            self._rehydrate_go_type_locations()
            self._rehydrate_function_locations()
            self._join_go_implements()
        if cs.SupportedLanguage.CSHARP in languages:
            self._run_csharp_frontend()
            self._rehydrate_csharp_type_locations()
            self._rehydrate_function_locations()
            self._join_csharp_partials()
        if cs.SupportedLanguage.JAVA in languages:
            self._run_java_frontend()
            self._rehydrate_function_locations()
        if cs.SupportedLanguage.PYTHON in languages:
            self._run_python_frontend()
            self._rehydrate_function_locations()

    def _reingest_split(
        self, paths: Iterable[Path | str], deleted: Iterable[Path | str]
    ) -> tuple[dict[str, Path], dict[str, Path], set[str]]:
        # (present, gone, skipped): a listed path missing on disk counts as
        # gone, an explicit deletion wins over a same-named file that
        # reappeared, and a path the ignore rules exclude is reported back
        # rather than indexed. A directory in `paths` is refused: present or
        # gone is inferred from disk there, and the delete queries match
        # nothing for a directory, so it would be reported as removed while
        # the graph stayed untouched. `deleted` is an instruction, not an
        # inference, and a directory may now sit where the deleted file was
        # (the watcher sees exactly that race), so its delete goes ahead.
        present: dict[str, Path] = {}
        gone: dict[str, Path] = {}
        skipped: set[str] = set()
        for raw in paths:
            key, path = self._reingest_file_target(raw)
            if self._reingest_ignored(path):
                skipped.add(key)
                continue
            (present if path.is_file() else gone)[key] = path
        for raw in deleted:
            key, path = self._reingest_target(raw)
            present.pop(key, None)
            if self._reingest_ignored(path):
                skipped.add(key)
                continue
            gone[key] = path
        return present, gone, skipped

    def _reingest_file_target(self, raw: Path | str) -> tuple[str, Path]:
        key, path = self._reingest_target(raw)
        if path.is_dir():
            raise ValueError(cs.REINGEST_IS_DIRECTORY.format(path=raw))
        return key, path

    def _reingest_ignored(self, path: Path) -> bool:
        # The caller is an agent naming files it wrote, so the project's
        # ignore rules apply here exactly as they do to the walk: a path
        # under `node_modules`, a generated `.min.js`, or a CLI-excluded
        # directory must not become a Module the next update then keeps,
        # because its hash matches and the walk never revisits it.
        return should_skip_path(
            path,
            self.repo_path,
            exclude_paths=self.exclude_paths,
            unignore_paths=self.unignore_paths,
            is_file=True,
        )

    def _reingest_flux_survivors(
        self, present: dict[str, Path], gone: dict[str, Path], hashes: FileHashCache
    ) -> tuple[set[str], dict[str, Path]]:
        # Stems with a same-stem sibling added (a present key the cache has
        # never seen) or deleted by this call, and the on-disk files that
        # share them and are not named in the call. Those survivors re-parse
        # in walk order so the bare module qn goes to the file a clean index
        # gives it rather than to the file indexed first (issue #1569).
        flux_stems = {_stem_key(key) for key in present if key not in hashes}
        flux_stems |= {_stem_key(key) for key in gone}
        survivors: dict[str, Path] = {}
        for stem in flux_stems:
            parent = (self.repo_path / stem).parent
            if not parent.is_dir():
                continue
            for candidate in sorted(parent.iterdir()):
                if not candidate.is_file():
                    continue
                key = cached_relative_path(candidate, self.repo_path).as_posix()
                if _stem_key(key) != stem or key in present or key in gone:
                    continue
                if self._reingest_ignored(candidate):
                    continue
                survivors[key] = candidate
        return flux_stems, survivors

    def _reingest_dependents(
        self, present: dict[str, Path], gone: dict[str, Path]
    ) -> dict[str, Path]:
        # Files whose bindings the change can move (one level, from the
        # graph's own edges) plus any file whose delombok overlay changed.
        keys = sorted({*present, *gone})
        affected: dict[str, Path] = {}
        for caller_key in self._affected_caller_keys(keys):
            caller_path = self.repo_path / caller_key
            if (
                caller_key not in present
                and caller_key not in gone
                and caller_path.is_file()
            ):
                affected[caller_key] = caller_path
        for key in self._delombok_stale_keys:
            if (
                key not in present
                and key not in gone
                and (self.repo_path / key).is_file()
            ):
                affected[key] = self.repo_path / key
        return affected

    def _reingest_delete(
        self, reparse: dict[str, Path], gone: dict[str, Path], hashes: FileHashCache
    ) -> None:
        import_processor = self.factory.import_processor
        # A fresh updater read every definition's annotations back from the
        # graph into the pending type facts BEFORE this delete; the facts of
        # the files re-parsed or dropped here describe the old source, and
        # the re-parse queues the new ones, so without this both the stale
        # and the fresh RETURNS/ACCEPTS edge would be emitted (issue #1527).
        qn_to_path = self.factory.definition_processor.module_qn_to_file_path
        stale_paths = {*reparse.values(), *gone.values()}
        stale_modules = {
            base_module_qn(Path(key), self.project_name) for key in (*reparse, *gone)
        } | {qn for qn, path in qn_to_path.items() if path in stale_paths}
        pending = self.factory.definition_processor.pending_type_facts
        pending[:] = [fact for fact in pending if fact.module_qn not in stale_modules]
        for key, path in reparse.items():
            self.remove_file_from_state(path)
            self._delete_module_entities(key)
            # The Rust path caches were filled by the last run; a created or
            # modified file must be re-observed before crate::/super:: paths
            # resolve against them.
            import_processor.refresh_rust_path_caches_for(
                path, created=key not in hashes
            )
        for key, path in gone.items():
            self.remove_file_from_state(path)
            self._delete_module_entities(key)
            if isinstance(self.ingestor, QueryProtocol):
                # Keyed on the absolute path: a sibling project's File node
                # can share the relative path (issue #897).
                self.ingestor.execute_write(
                    cs.CYPHER_DELETE_FILE, {cs.KEY_PATH: path.resolve().as_posix()}
                )

    def _reingest_reparse(
        self, reparse: dict[str, Path], gone: dict[str, Path]
    ) -> dict[str, bytes]:
        touched_languages: set[cs.SupportedLanguage] = set()
        for path in (*reparse.values(), *gone.values()):
            spec = get_language_spec(path.suffix)
            if spec is not None and isinstance(spec.language, cs.SupportedLanguage):
                touched_languages.add(spec.language)
        if touched_languages & cs.C_FAMILY_LANGUAGES and (
            settings.CPP_FRONTEND != cs.CppFrontend.HYBRID
        ):
            # LIBCLANG emits C/C++ definitions itself and marks the files it
            # covered so the tree-sitter pass skips them; the deleted subtrees
            # need it to run again BEFORE the re-parse, as run() does. (HYBRID
            # runs after Pass 2, inside the deferred stages.)
            self._cpp_frontend_covered = frozenset()
            self._run_cpp_frontend()
        parsed: dict[str, bytes] = {}
        for key, path in reparse.items():
            # Read once and hash those same bytes later: an edit landing
            # between the parse and a second read would otherwise be
            # recorded in the cache as already indexed.
            try:
                parsed[key] = path.read_bytes()
            except OSError:
                continue
            # The delombok overlay stands in for the checked-in bytes exactly
            # as the batch path does (issue #1140).
            self._process_single_file(
                path, file_bytes=self._delombok_overlay.get(key, parsed[key])
            )
        self._rerun_frontends_for(touched_languages)
        return parsed

    def _reingest_resolve(
        self, reparse: dict[str, Path], captured: list[ResultRow]
    ) -> None:
        # The deferred definition-level stages run() applies after Pass 2
        # (C/C++ macros and includes, containment, forward declarations,
        # inheritance, Rust inline-mod arbitration): the deleted subtrees
        # held those relationships too. The registry is already live or was
        # hydrated above, so no graph read-back here.
        known_module_paths = self._resolve_deferred_definitions(rehydrate=False)
        # The re-parsed files' own CALLS edges went with their Module
        # subtrees; a moved use must not serve last pass's cached answer.
        self.factory.call_processor.reset_resolution_caches()
        self._process_function_calls(only=set(reparse.values()))
        # Editing a PROVIDER recreated its Module node and severed the
        # unchanged C# importers' edges (issue #1347).
        import_processor = self.factory.import_processor
        import_processor.requeue_csharp_import_edges()
        import_processor.flush_deferred_import_edges(known_module_paths)
        self._emit_csharp_query_calls()
        self.factory.definition_processor.process_all_method_overrides()
        # Endpoints and route registrations, scoped to the re-parsed modules:
        # the project-wide passes would load every route-capable module's
        # AST, which on a fresh updater means re-parsing most of the repo.
        reparsed_paths = set(reparse.values())
        touched_modules = {
            qn
            for qn, module_path in self.factory.definition_processor.module_qn_to_file_path.items()
            if module_path in reparsed_paths
        }
        self._emit_pending_endpoints(only=touched_modules)
        self._emit_route_call_endpoints(only=touched_modules)
        self._restore_inbound_edges(captured)
        if isinstance(self.ingestor, QueryProtocol):
            self.ingestor.execute_write(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)
        self.ingestor.flush_all()

    def _reingest_update_hashes(
        self,
        cache_path: Path,
        hashes: FileHashCache,
        reparse: dict[str, Path],
        parsed: dict[str, bytes],
        gone: dict[str, Path],
    ) -> None:
        # Keep the hash cache honest so the next update_repository does not
        # re-parse what this call already applied.
        if not cache_path.is_file():
            return
        try:
            observed_at = cache_path.stat().st_mtime
        except OSError:
            return
        for key in reparse:
            data = parsed.get(key)
            if data is None:
                hashes.pop(key, None)
            else:
                hashes[key] = hashlib.md5(data, usedforsecurity=False).hexdigest()
        for key in gone:
            hashes.pop(key, None)
        _save_hash_cache(cache_path, hashes)
        # The cache's mtime is the instant every file NOT in it was last
        # judged (`_is_already_in_sync` and the walk skip files no newer
        # than it). Writing moves it to now, which would hide an edit made
        # before this call to a file this call was not told about: the next
        # update would read it as older than the cache and keep the stale
        # hash. Put the previous instant back; the files re-ingested here
        # are newer than it, so the next run re-hashes them and finds them
        # already current. Fail closed like `run()`: a cache stamped with
        # the wrong time is worse than no cache.
        try:
            os.utime(cache_path, (observed_at, observed_at))
        except OSError as e:
            logger.warning(ls.REINGEST_CACHE_STAMP_FAILED, path=cache_path, error=e)
            try:
                cache_path.unlink(missing_ok=True)
            except OSError as unlink_error:
                logger.warning(
                    ls.REINGEST_CACHE_STAMP_CLEANUP_FAILED,
                    path=cache_path,
                    error=unlink_error,
                )

    def reingest(
        self,
        paths: Iterable[Path | str],
        deleted: Iterable[Path | str] = (),
    ) -> ReingestReport:
        """Re-ingest the given files with file-scoped call resolution.

        The batch incremental path already knows how to do this for the
        files a hash walk finds changed: re-parse them plus the files that
        depend on them (one level, via the graph's edges), restore every
        other inbound edge verbatim, and resolve calls only in the re-parsed
        set. This is the same recipe cut down to an explicit file list, so
        an agent's edit reaches the graph in the time it takes to parse
        those files rather than to walk and re-resolve the project (issue
        #1524). The watcher and the MCP ``reingest`` tool both run through
        here.

        ``paths`` that no longer exist on disk are treated as deleted;
        ``deleted`` names files whose removal should be applied even if a
        same-named file has since reappeared (a watch DELETE event). Paths
        the project's ignore rules exclude are reported as ``skipped`` and
        left out of the graph, as the walk would leave them.
        """
        started = time.perf_counter()
        # A scoped re-ingest is never a full build, whatever the previous
        # run() was: the flag decides whether a failed inbound-edge capture
        # aborts (it must, or the run drops cross-file edges under a
        # success log) and whether unchanged handlers rehydrate.
        self._is_full_build = False
        present, gone, skipped = self._reingest_split(paths, deleted)
        if skipped:
            logger.warning(ls.REINGEST_SKIPPED_IGNORED, paths=sorted(skipped))
        if not present and not gone:
            return ReingestReport((), (), (), tuple(sorted(skipped)), 0.0)

        # Everything up to the inbound-edge capture issues no delete and no
        # content write (a fresh updater's structure hydration re-emits
        # idempotent Package and Folder upserts, nothing more). A failure
        # there leaves the graph as it was and says so with ReingestAborted,
        # so a caller holding the updater (the MCP tool, the watcher) keeps
        # it rather than treating the graph as partially rewritten.
        try:
            self._hydrate_for_reingest()
            # Generated-source roots and the delombok overlay are per-run inputs;
            # a watcher's updater lives across many events, so refresh them per
            # call (the batch path does the same through _delombok_stale_keys).
            self._register_generated_sources()
            cache_path = self.repo_path / cs.HASH_CACHE_FILENAME
            hashes = _load_hash_cache(cache_path)

            # Same-stem reconciliation, as the batch path does it (issue #1569):
            # a stem with a sibling added or deleted by this call is in flux, its
            # on-disk survivors re-parse unseeded, and the module-qn map is
            # seeded from the graph for everything else so the disambiguator
            # sees the taken qns on a fresh updater.
            flux_stems, survivors = self._reingest_flux_survivors(present, gone, hashes)
            existing_paths = self._existing_module_paths()
            if existing_paths is None:
                # The sink claims a query surface but the read failed: the taken
                # qns are unknown, and seeding nothing would let a re-parsed
                # file claim a qn the graph already holds. Abort before any
                # delete, as the inbound-edge capture does.
                raise RuntimeError(ls.REINGEST_MODULE_PATHS_UNKNOWN)
            graph_paths = set(existing_paths)
            self._seed_module_qns_from_graph(graph_paths - set(gone), flux_stems)
            # A gone file seeds regardless: its map entry is what the state drop
            # below keys on to free its qn and forget its rehydrated form.
            self._seed_module_qns_from_graph(set(gone) & graph_paths, frozenset())

            affected = self._reingest_dependents({**present, **survivors}, gone)
            all_keys = sorted({*present, *gone, *survivors, *affected})
            captured = self._capture_inbound_edges(all_keys)
        except Exception as exc:
            raise ReingestAborted(str(exc)) from exc
        self._reparsed_file_keys = set(all_keys)

        # Walk order, as the batch path re-parses (issue #1569): the first
        # same-stem sibling parsed claims the bare module qn, so a header
        # found before its dependent source file would take the qn a clean
        # index gives the source.
        reparse = dict(
            sorted(
                {**present, **survivors, **affected}.items(),
                key=lambda item: (Path(item[0]).parent.parts, Path(item[0]).name),
            )
        )
        self._reingest_delete(reparse, gone, hashes)
        parsed = self._reingest_reparse(reparse, gone)
        self._reingest_resolve(reparse, captured)
        self._reingest_update_hashes(cache_path, hashes, reparse, parsed, gone)

        report = ReingestReport(
            reparsed=tuple(sorted(present)),
            # Survivors of a stem in flux re-parsed with the dependents and
            # are reported with them: the caller sees every file this call
            # touched.
            affected=tuple(sorted({*affected, *survivors})),
            removed=tuple(sorted(gone)),
            skipped=tuple(sorted(skipped)),
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        logger.info(
            ls.REINGEST_DONE,
            reparsed=len(report.reparsed),
            affected=len(report.affected),
            removed=len(report.removed),
            ms=round(report.elapsed_ms, 1),
        )
        return report

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
            (cs.CYPHER_ALL_PACKAGE_PATHS, cs.CYPHER_DELETE_PACKAGE, "Package"),
        ]
        # A directory is represented by exactly one of Folder and Package,
        # decided by identify_structure this run; the node of the other kind
        # is stale even though the directory exists (issue #1570).
        packages_now = {
            rel.as_posix()
            for rel, qn in self.factory.structure_processor.structural_elements.items()
            if qn
        }

        read_failed = False
        legacy_file_keys: list[tuple[str, str]] = []
        for query_all, delete_query, label in prune_specs:
            try:
                rows = self.ingestor.fetch_all(query_all)
            except Exception:
                # A graph that cannot be read cannot be pruned safely; the
                # next healthy run sweeps whatever this one left behind.
                logger.warning(ls.PRUNE_QUERY_FAILED, label=label)
                read_failed = True
                continue
            orphans = []
            for r in rows:
                path = r.get("path")
                if not isinstance(path, str) or not path:
                    continue
                if path.startswith(cs.INLINE_MODULE_PATH_PREFIX):
                    continue
                abs_path = r.get("absolute_path")
                qn = r.get("qualified_name", "")
                # Component-aware containment: a bare prefix test would also
                # match a sibling root such as <repo>-old (issue #897).
                if isinstance(abs_path, str) and not (
                    abs_path == repo_abs or abs_path.startswith(repo_abs + "/")
                ):
                    # An out-of-repo File key the containment gate would leak
                    # forever: legacy pre-GHSA-85gg nodes were keyed on a
                    # symlink's dereferenced TARGET. A key that disagrees with
                    # the identity derivable from the relative path is such a
                    # record; one that agrees is a file under a symlinked
                    # ancestor directory, legitimate under the current scheme
                    # (issue #1156).
                    if label == "File" and abs_path != (
                        cached_file_identity_posix(self.repo_path / path)
                    ):
                        legacy_file_keys.append((path, abs_path))
                    continue
                # The root directory's own node is qualified as exactly the
                # project name, with no dot: testing only the dotted prefix
                # dropped it here and let a stale root Package survive beside
                # the Folder that replaced it. `_package_paths` reads these
                # same rows and admits both forms; the two must agree.
                if (
                    isinstance(qn, str)
                    and qn
                    and qn != self.project_name
                    and not qn.startswith(project_prefix)
                ):
                    continue
                stale_kind = (label == "Folder" and path in packages_now) or (
                    label == "Package" and path not in packages_now
                )
                if stale_kind or not (self.repo_path / path).exists():
                    # File/Folder deletes key on the absolute path: a sibling
                    # project's node can share the relative path (issue #897).
                    key = (
                        abs_path
                        if isinstance(abs_path, str) and abs_path
                        else (self.repo_path / path).resolve().as_posix()
                    )
                    orphans.append((path, key))

            if orphans:
                logger.info(ls.PRUNE_FOUND, count=len(orphans), label=label)
                for orphan_path, orphan_key in orphans:
                    logger.debug(ls.PRUNE_DELETING, label=label, path=orphan_path)
                    if delete_query == cs.CYPHER_DELETE_MODULE:
                        # Module deletes are project-scoped; a sibling
                        # project's module can share the relative path.
                        self._delete_module_entities(orphan_path)
                    else:
                        self.ingestor.execute_write(
                            delete_query, {cs.KEY_PATH: orphan_key}
                        )
                total_pruned += len(orphans)

        if read_failed:
            # The same outage that broke the path reads would break (or act
            # on stale state through) the cleanup below; leave everything
            # for the next healthy run.
            return

        total_pruned += self._sweep_legacy_file_identities(legacy_file_keys)

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

    def _sweep_legacy_file_identities(self, candidates: list[tuple[str, str]]) -> int:
        """Delete legacy target-resolved File records, sparing shared keys.

        File nodes MERGE globally on absolute_path, so the stale key can be
        the very node another project legitimately owns (its repo contains
        the old link's target); any container from outside this repository
        vetoes the delete (issue #1156).
        """
        if not isinstance(self.ingestor, QueryProtocol):
            return 0
        swept = 0
        for path, abs_path in candidates:
            if not self._file_key_owned_only_by_this_project(abs_path):
                continue
            logger.debug(ls.PRUNE_DELETING, label="File", path=path)
            self.ingestor.execute_write(cs.CYPHER_DELETE_FILE, {cs.KEY_PATH: abs_path})
            swept += 1
        if swept:
            logger.info(ls.PRUNE_LEGACY_IDENTITIES, count=swept)
        return swept

    def _file_key_owned_only_by_this_project(self, abs_path: str) -> bool:
        """Positive attribution: every container is this project's, and at
        least one exists. A sibling project's node can share both the key
        and the relative path (issue #897), and a node whose containers are
        missing or unreadable offers no evidence of sole ownership, so both
        veto the sweep.
        """
        try:
            rows = self.ingestor.fetch_all(
                cs.CYPHER_FILE_CONTAINERS, {cs.KEY_PATH: abs_path}
            )
        except Exception:
            # Unreadable ownership is unknown ownership: never delete a
            # globally merged key on a failed read, and never let the
            # failure escape mid-update.
            logger.warning(ls.PRUNE_QUERY_FAILED, label="File containers")
            return False
        repo_abs = self.repo_path.resolve().as_posix()
        if not all(self._container_is_ours(row, repo_abs) for row in rows):
            return False
        return bool(rows)

    def _container_is_ours(self, row: ResultRow, repo_abs: str) -> bool:
        """Whether one container row proves THIS project's ownership.

        A foreign Project or Folder, and equally a container with no usable
        identity, reads as not-ours: partial evidence must never delete a
        globally merged key.
        """
        labels = row.get("labels")
        if isinstance(labels, list) and cs.NodeLabel.PROJECT.value in labels:
            return row.get("name") == self.project_name
        container_abs = row.get("absolute_path")
        if isinstance(container_abs, str) and container_abs:
            return container_abs == repo_abs or container_abs.startswith(repo_abs + "/")
        return False

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
