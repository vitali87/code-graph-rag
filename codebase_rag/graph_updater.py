"""Orchestrate parsing a repository into graph nodes and edges and ingest them."""

import time
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from tree_sitter import Parser

from . import constants as cs
from . import cypher_queries as cq
from . import exceptions as ex
from . import logs as ls
from .analyzers import FindingAnalyzer
from .ast_cache import BoundedASTCache
from .capture import CaptureSelection, default_capture
from .config import settings
from .function_registry import FunctionRegistryTrie
from .graph_pruning import GraphPruner
from .language_spec import (
    get_language_spec,
)
from .parsers.ast_grep_tier import AstGrepTier
from .parsers.csharp import graph_joins as csharp_joins
from .parsers.document_tier import DocumentTier
from .parsers.endpoint_linker import EndpointLinker
from .parsers.factory import ProcessorFactory
from .parsers.go import graph_joins as go_joins
from .parsers.stdlib_extractor import flush_stdlib_cache
from .services import (
    IngestorProtocol,
    NodeUpdateProtocol,
    QueryProtocol,
)
from .services.filtering import filtering_ingestor
from .sync.checkout_state import prepare_state_dir, state_dir
from .sync.lock import holds_sync_lock
from .sync.state import (
    DirMtimesCache,
    FileHashCache,
    load_hash_cache,
)
from .trace.carry import CapturedTraceEdge
from .types_defs import (
    LanguageQueries,
    PropertyParams,
    ReingestReport,
    ResultRow,
    SimpleNameLookup,
)
from .updater.container_flips import ContainerFlips
from .updater.deferred import DeferredResolution
from .updater.dependents import DependentLookup
from .updater.edge_carry import EdgeCarry
from .updater.file_parse import FileParser
from .updater.file_pass import FilePass
from .updater.file_state import FileStateSweep
from .updater.frontends import FrontendRunner
from .updater.generated_sources import GeneratedSources
from .updater.gloss_reanchor import GlossReanchor
from .updater.import_closure import ImportClosure
from .updater.legacy_retirement import LegacyRetirement
from .updater.module_seeding import ModuleQnSeeding
from .updater.rehydration import GraphRehydrator
from .updater.reingest_planner import ReingestPlanner
from .updater.sync_keeper import SyncStateKeeper
from .updater.tree_scan import TreeScan
from .utils.json_io import load_json
from .utils.path_utils import (
    base_module_qn,
    cached_file_identity_posix,
    is_inline_module_path,
    normalize_project_name,
    walk_position,
)
from .vectors.embedding_sync import EmbeddingSync


def _project_root_for_single_file(target: Path) -> Path:
    """The project root owning `target`, or its parent when none is found.

    A single-file run must key the file as the directory run that indexed it
    did, or delete-before-reingest misses the node and writes a duplicate
    under the wrong key (#1775). The nearest ancestor whose hash cache lists
    the target wins, since that cache is evidence of the root that indexed
    it; only without one does the nearest `.git` apply, which is a guess. A
    cache outranks a nearer `.git` only when it lists a file inside that
    nested checkout: the cache lives under CGR_HOME and can outlive the tree
    it described (PR #2557). A target under neither marker keys against its
    parent, as before.
    """
    git_root: Path | None = None
    for ancestor in target.parents:
        cache = _hash_cache_marking(ancestor)
        if cache is not None and (
            git_root is None or _cache_lists_under(cache, ancestor, git_root)
        ):
            return ancestor
        if git_root is None and (ancestor / cs.GIT_DIR_NAME).exists():
            git_root = ancestor
    return git_root if git_root is not None else target.parent


def _hash_cache_marking(ancestor: Path) -> Path | None:
    """The hash cache a directory run at `ancestor` wrote, if there is one.

    In its state directory, or in the tree itself where an older cgr wrote
    it. `state_dir`, not `prepare_state_dir`: a lookup must not create a
    state directory for every ancestor it asks about (issue #2427).
    """
    for cache in (
        state_dir(ancestor) / cs.HASH_CACHE_FILENAME,
        ancestor / cs.HASH_CACHE_FILENAME,
    ):
        if cache.is_file():
            return cache
    return None


def _cache_lists_under(cache: Path, root: Path, checkout: Path) -> bool:
    """Whether the run at `root` that wrote `cache` indexed a file in
    `checkout`. Keys are POSIX paths relative to `root`; one that cannot be
    read lists nothing."""
    prefix = f"{checkout.relative_to(root).as_posix()}{cs.SEPARATOR_SLASH}"
    try:
        with cache.open(encoding=cs.ENCODING_UTF8) as f:
            entries = load_json(f)
    except (OSError, ValueError):
        return False
    return isinstance(entries, dict) and any(key.startswith(prefix) for key in entries)


@dataclass(slots=True)
class _SyncRun:
    """What one `run()` or `reingest()` call learns and still owes.

    Replaced whole at the start of each call, so an updater reused across
    calls (the MCP server's, the watcher's) never answers from a previous
    call's state and a new field cannot be left out of a reset.
    """

    # The call re-parses EVERY file (no cache, or force): it re-resolves
    # all edges itself, so graph reads may degrade on failure; an
    # incremental run's correctness depends on them.
    is_full_build: bool = False
    # Registered project names, read once per call (issue #1970), and
    # whether that read failed: the list then holds this project alone,
    # which cannot tell a nested project's rows from this one's.
    registered_projects: list[str] | None = None
    registry_unread: bool = False
    exposes_cleanup_skipped: bool = False
    prune_settled: bool = False
    # An orphaned cache could not be deleted: the file is still on disk,
    # so this call ignores it in memory rather than trust it (issue #1647).
    cache_discarded_in_memory: bool = False
    # A parser input changed since the graph was built (issue #1977): the
    # hash cache describes files parsed by other inputs, so the call
    # ignores it and re-parses every file once.
    parser_changed: bool = False
    # Whether the exclusion set matches the last completed run's stamp.
    # Project provenance can match while the exclusion set changes; the
    # old cache is kept then so newly excluded files can be removed.
    exclusion_match: bool | None = None
    cache_project_match: bool | None = None
    # The graph's module paths could not be read: the reconciliation they
    # were needed for may not have happened, so the exclusion set must not
    # be stamped as reconciled.
    graph_state_unknown: bool = False
    # Sync state written at the post-flush commit point in `run`, never
    # inside `_process_files` (issues #1615, #1634): a build that dies
    # before its final flush must not leave state claiming it finished.
    pending_hash_cache: tuple[Path, FileHashCache] | None = None
    pending_dir_mtimes: tuple[Path, DirMtimesCache] | None = None
    pending_cache_observed_at: float | None = None
    pending_parser_fingerprint: str | None = None
    # The trace-derived CALLS edges read before the re-parsed subtrees are
    # deleted, re-applied once Pass 3 rebuilt the static edges (issue
    # #2429), and the files deleted, whose edges must not follow their
    # qualified names onto a same-stem survivor.
    captured_trace_edges: list[CapturedTraceEdge] = field(default_factory=list)
    trace_gone_paths: frozenset[str] = frozenset()
    # The edges into re-indexed files that `_process_files` captured, held
    # until the deferred stages registered every re-parsed definition
    # (issue #3270).
    pending_inbound_edges: list[ResultRow] = field(default_factory=list)
    embeddings_interrupted: bool = False
    # The bytes a scoped re-ingest judged a file's interface on, which its
    # re-parse then reads instead of the disk (issue #2531).
    reingest_pinned_sources: dict[str, bytes] = field(default_factory=dict)


@dataclass(slots=True)
class _ReingestPlan:
    """What a `reingest` call's read-only prologue found, for its writes.

    `survivors` are the same-stem and package-flip siblings the call
    re-parses unnamed, `affected` the dependents, and `flipped_dirs` the
    directories whose container kind changes; `captured` and
    `captured_trace` are the edges into every file in `all_keys`, read
    before the delete.
    """

    cache_path: Path
    hashes: FileHashCache
    survivors: dict[str, Path]
    affected: dict[str, Path]
    flipped_dirs: set[str]
    all_keys: list[str]
    captured: list[ResultRow]
    captured_trace: list[CapturedTraceEdge]


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
        project_named: bool | None = None,
        state_dir: Path | None = None,
    ):
        self.capture = capture if capture is not None else default_capture()
        self._run = _SyncRun()
        # Where the incremental-sync state (hash cache, dir mtimes, exclusion
        # stamp, parser fingerprint, pending markers) is read and written.
        # None resolves on first use to this checkout's directory under
        # CGR_HOME (issue #2427), keeping construction free of side effects.
        # A snapshot run points it at a throwaway directory so it neither
        # builds incrementally from a sync's state nor leaves state claiming
        # a live graph is current (issue #2401).
        self._state_dir: Path | None = state_dir
        # `ingestor` stays the raw object for DB queries (QueryProtocol),
        # flushes, and test introspection. `_sink` is a filtering wrapper that
        # drops disabled relationships/nodes at one choke point, so the ~20
        # parser emission sites stay untouched. All emission goes through
        # `_sink`; everything else uses `ingestor`.
        self.ingestor = ingestor
        self._sink: IngestorProtocol = filtering_ingestor(ingestor, self.capture)
        self._single_file: Path | None = None
        if repo_path.is_file():
            resolved = repo_path.resolve()
            self._single_file = resolved
            repo_path = _project_root_for_single_file(resolved)
        self.repo_path = repo_path
        self.parsers = parsers
        self.queries = queries
        self.project_named = (
            bool(project_name and project_name.strip())
            if project_named is None
            else project_named
        )
        self.project_name = normalize_project_name(project_name, repo_path)
        self.simple_name_lookup: SimpleNameLookup = defaultdict(set)
        self.function_registry = FunctionRegistryTrie(
            simple_name_lookup=self.simple_name_lookup
        )
        self.ast_cache = BoundedASTCache()
        # Every file parsed this run, in parse order. The AST cache is bounded
        # and evicts on large repos, so Pass 3 must iterate this full list (not
        # the cache) and re-parse evicted files, or their calls are dropped.
        self._parsed_files: list[tuple[Path, cs.SupportedLanguage]] = []
        # Rel-path -> registered qns for FRONTEND registrations, which have
        # no tree-sitter span records; the watch prefix sweep reads this to
        # spare foreign files' entries (issue #1025).
        self._frontend_owned_qns: dict[str, set[str]] = {}
        self.unignore_paths = unignore_paths
        # Whether the LAST `reingest` call got past its read-only prologue
        # into deletes and writes. Callers persisting recovery state use it to
        # tell "nothing changed, retry freely" from "partial, needs recovery"
        # (#1705).
        self.reingest_mutated = False
        self.exclude_paths = exclude_paths
        # None defers to the CGR_SKIP_EMBEDDINGS setting so env-configured
        # callers (MCP, workspace sync) opt out without a CLI flag.
        self.skip_embeddings = (
            settings.SKIP_EMBEDDINGS if skip_embeddings is None else skip_embeddings
        )
        self.skipped_because_in_sync = False
        # Whether the current run has saved its graph and sync state; see
        # `run`. A caller interrupted mid-run reads it to tell a partial graph
        # from a whole one (issue #2442).
        self.committed = False
        # Files (re)parsed by Pass 2 this run: the only files whose
        # definition spans exist for hybrid macro-call attribution.
        self._reparsed_file_keys: set[str] = set()
        # The file keys the current `reingest` call will delete and re-parse
        # (changed, deleted, dependents, same-stem survivors), published right
        # before `before_write` runs so a caller can capture exactly that set
        # while nothing has been written yet (issue #1718). Set only while
        # `before_write` runs; empty before and after, whether the hook
        # returned, refused or was never given.
        self.reingest_scope: tuple[str, ...] = ()
        # Package paths read from the graph before Pass 1 of an incremental
        # run; None on a full build or when the graph could not be read.
        self._packages_before_run: set[str] | None = None
        # reingest() reads the registry back from the graph once on a fresh
        # updater; a reused one already holds live state (issue #1524).
        self._reingest_hydrated = False

        self._build_parse_stages(configured_unignore_paths=unignore_paths)
        self._build_sync_stages()
        self._build_closing_stages()

    def _build_parse_stages(
        self, configured_unignore_paths: frozenset[str] | None
    ) -> None:
        """The passes that turn source into graph nodes and edges, the
        compiler frontends layered over them, and the definition-level stages
        they defer until every file is parsed. `configured_unignore_paths` is
        the caller's set, before a run adds its generated-source roots."""
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
        # Annotation-processor output and the delombok overlay, recomputed
        # per run.
        self.generated_sources = GeneratedSources(
            factory=self.factory,
            repo_path=self.repo_path,
            configured_unignore_paths=configured_unignore_paths,
        )
        # What an incremental sync reads back from the graph for the files it
        # did not re-parse.
        self.graph_rehydrator = GraphRehydrator(
            graph=self._graph,
            factory=self.factory,
            function_registry=self.function_registry,
            parsers=self.parsers,
            repo_path=self.repo_path,
            project_name=self.project_name,
            capture=self.capture,
            owns=self._owns,
            owned_rows=self._owned_rows,
        )
        # The compiler frontends layered over Tree-sitter, and the hybrid
        # C/C++ macro uses they leave for after the definition pass.
        self.frontend_runner = FrontendRunner(
            sink=self._sink,
            factory=self.factory,
            function_registry=self.function_registry,
            simple_name_lookup=self.simple_name_lookup,
            repo_path=self.repo_path,
            project_name=self.project_name,
            scope=self,
            owned_qns=self._frontend_owned_qns,
            rehydrated_spans=self.graph_rehydrator.cpp_spans,
        )
        # Fallback structural tier for languages with no tree-sitter
        # LanguageSpec (e.g. Ruby), driven by ast-grep pattern configs.
        self.ast_grep_tier = AstGrepTier(self._sink, self.repo_path, self.project_name)
        # Heading structure for documents (issue #1426): Section nodes nested
        # by heading level. Documents have no calls, so they get neither of
        # the code tiers above.
        self.document_tier = DocumentTier(self._sink, self.repo_path, self.project_name)
        # Each file of a pass parsed through the first tier that claims it.
        self.file_parser = FileParser(
            graph=self._graph,
            factory=self.factory,
            ast_cache=self.ast_cache,
            parsers=self.parsers,
            queries=self.queries,
            repo_path=self.repo_path,
            project_name=self.project_name,
            scope=self,
            parsed_files=self._parsed_files,
            frontend_runner=self.frontend_runner,
            generated_sources=self.generated_sources,
            ast_grep_tier=self.ast_grep_tier,
            document_tier=self.document_tier,
        )
        # The parser re-reads what the bounded cache evicts; it needs the
        # factory, which needs the cache, so it is attached once both exist.
        self.ast_cache.loader = self.file_parser.load_ast_from_disk
        # The import maps of the unchanged modules a re-parsed file reaches.
        self.import_closure = ImportClosure(
            factory=self.factory,
            ast_cache=self.ast_cache,
            parsers=self.parsers,
            queries=self.queries,
            repo_path=self.repo_path,
            frontend_runner=self.frontend_runner,
        )
        # The definition-level stages a sync defers until Pass 2 is done.
        self.deferred_resolution = DeferredResolution(
            factory=self.factory,
            document_tier=self.document_tier,
            frontend_runner=self.frontend_runner,
            graph_rehydrator=self.graph_rehydrator,
            import_closure=self.import_closure,
            known_module_paths=self.known_module_paths,
        )

    def _build_sync_stages(self) -> None:
        """What lets a sync re-parse only what changed: the lookups that widen
        a re-parse, the state it carries across the delete, the post-passes
        and caches a finished sync records, and the walk and Pass 2 built on
        them."""
        # The files a change sends back for a re-parse beside the changed ones.
        self.dependent_lookup = DependentLookup(
            graph=self._graph,
            graph_rows=self._graph_rows,
            factory=self.factory,
            parsers=self.parsers,
            queries=self.queries,
            repo_path=self.repo_path,
            project_name=self.project_name,
            capture=self.capture,
            registered_project_names=self._registered_project_names,
            tree_sitter_language=self.file_parser.tree_sitter_language,
        )
        # The module-qn claims an incremental run starts from, and the stems
        # in flux whose bare qn goes to the file a clean index gives it.
        self.module_seeding = ModuleQnSeeding(
            graph=self._graph,
            factory=self.factory,
            repo_path=self.repo_path,
            project_name=self.project_name,
            owns=self._owns,
            owned_rows=self._owned_rows,
            parsed_files=self._parsed_files,
            rehydrated_module_qns=self.graph_rehydrator.module_qns,
            frontend_runner=self.frontend_runner,
            remover=self,
        )
        # The registry side of removing a file from the updater's state.
        self.file_state_sweep = FileStateSweep(
            factory=self.factory,
            function_registry=self.function_registry,
            simple_name_lookup=self.simple_name_lookup,
            repo_path=self.repo_path,
            project_name=self.project_name,
            frontend_owned_qns=self._frontend_owned_qns,
            rehydrated_module_qns=self.graph_rehydrator.module_qns,
        )
        # The inbound and trace edges into re-parsed files, read before their
        # subtrees are deleted and written back after the re-parse.
        self.edge_carry = EdgeCarry(
            graph=self._graph,
            ingestor=self.ingestor,
            sink=self._sink,
            factory=self.factory,
            document_tier=self.document_tier,
            function_registry=self.function_registry,
            repo_path=self.repo_path,
            project_name=self.project_name,
        )
        # Opt-in ast-grep finding analyzer (issue #413): Pattern/CodeSmell/
        # SecurityIssue nodes from categorized YAML rules, run as a post-pass.
        self.finding_analyzer = FindingAnalyzer(
            self._sink, self.repo_path, self.capture
        )
        # The embeddings pass that closes a sync, and the debt it leaves in
        # the state directory when it cannot finish.
        self.embedding_sync = EmbeddingSync(
            graph=self._graph,
            ast_cache=self.ast_cache,
            repo_path=self.repo_path,
            project_name=self.project_name,
            skip_embeddings=self.skip_embeddings,
            nested_project_names=self._nested_project_names,
        )
        # The sync state that lets a run skip what the last one did, and the
        # commit that records a finished run.
        self.sync_keeper = SyncStateKeeper(
            graph=self._graph,
            graph_rows=self._graph_rows,
            ingestor=self.ingestor,
            repo_path=self.repo_path,
            project_name=self.project_name,
            project_named=self.project_named,
            capture=self.capture,
            single_file=self._single_file,
            scope=self,
            embedding_sync=self.embedding_sync,
            generated_sources=self.generated_sources,
        )
        # The walk of the checkout, held against the hash cache the last run
        # left.
        self.tree_scan = TreeScan(
            repo_path=self.repo_path,
            single_file=self._single_file,
            scope=self,
            generated_sources=self.generated_sources,
            sync_keeper=self.sync_keeper,
            existing_module_paths=self._existing_module_paths,
        )
        # Pass 2: the definitions of every file the sync re-parses.
        self.file_pass = FilePass(
            graph=self._graph,
            ingestor=self.ingestor,
            factory=self.factory,
            repo_path=self.repo_path,
            single_file=self._single_file,
            tree_scan=self.tree_scan,
            module_seeding=self.module_seeding,
            dependent_lookup=self.dependent_lookup,
            edge_carry=self.edge_carry,
            file_parser=self.file_parser,
            frontend_runner=self.frontend_runner,
            generated_sources=self.generated_sources,
            remover=self,
            delete_module_entities=self._delete_module_entities,
            package_flip_dirs=self._package_flip_dirs,
        )

    def _build_closing_stages(self) -> None:
        """The scoped re-ingest's planners, and the passes that close a sync."""
        # The container kind of each directory a scoped re-ingest touches.
        self.container_flips = ContainerFlips(
            graph=self._graph,
            factory=self.factory,
            repo_path=self.repo_path,
            tree_scan=self.tree_scan,
        )
        # The files a scoped re-ingest deletes and re-parses.
        self.reingest_planner = ReingestPlanner(
            repo_path=self.repo_path,
            tree_scan=self.tree_scan,
            dependent_lookup=self.dependent_lookup,
            generated_sources=self.generated_sources,
        )
        # HTTP endpoints from route decorators and route registrations, and
        # the client-URL and contract links into them (issues #877, #886).
        self.endpoint_linker = EndpointLinker(
            ingestor=self.ingestor,
            graph=self._graph,
            sink=self._sink,
            capture=self.capture,
            factory=self.factory,
            ast_cache=self.ast_cache,
            repo_path=self.repo_path,
            project_name=self.project_name,
            owns=self._owns,
        )
        # The gloss re-anchor that closes every sync and re-ingest.
        self.gloss_reanchor = GlossReanchor(
            graph=self._graph,
            parsers=self.parsers,
            repo_path=self.repo_path,
            project_name=self.project_name,
        )
        # The retirement of what an older cgr wrote for this checkout.
        self.legacy_retirement = LegacyRetirement(
            graph=self._graph,
            graph_rows=self._graph_rows,
            repo_path=self.repo_path,
            project_name=self.project_name,
            project_named=self.project_named,
            single_file=self._single_file,
        )
        # The orphan prune that closes a sync, and the prune it owes the next
        # run when cut short.
        self.graph_pruner = GraphPruner(
            graph=self._graph,
            factory=self.factory,
            repo_path=self.repo_path,
            project_name=self.project_name,
            single_file=self._single_file,
            owns=self._owns,
            delete_module_entities=self._delete_module_entities,
            legacy_retirement=self.legacy_retirement,
        )

    @property
    def prepared_state_dir(self) -> Path:
        """Where this checkout's sync state is read and written.

        Under CGR_HOME rather than in the working tree, where it showed up in
        `git status` and a read-only checkout could not keep it (issue
        #2427). Resolving it moves in what an older cgr left in the tree, so
        the first sync after an upgrade reads the hash cache it had.
        """
        if self._state_dir is None:
            self._state_dir = prepare_state_dir(self.repo_path)
        return self._state_dir

    @property
    def call_pass_failures(self) -> list[Path]:
        """Files the last call pass failed in, whole or in one caller (#3173).

        Each one logged its error and kept every edge it could, so the run
        still completes; the sync summary names them, since an exit-0 "done"
        was the only other signal that a file had lost its calls.
        """
        processor = self.factory.built_call_processor
        if processor is None:
            return []
        return sorted(processor.failed_call_files)

    def _require_repo_dir(self) -> None:
        """Raise `FileNotFoundError` unless `self.repo_path` is a directory.

        `run` checks this before `holds_sync_lock` takes the lock, so a
        missing root is reported as missing, and no state directory is made
        for it under CGR_HOME (issues #1651, #2441).
        """
        if not self.repo_path.is_dir():
            raise FileNotFoundError(ls.REPO_PATH_MISSING.format(path=self.repo_path))

    @holds_sync_lock(require=_require_repo_dir)
    def run(self, force: bool = False) -> None:
        """Ingest the repository; ``force`` rebuilds instead of updating incrementally.

        Raises `FileNotFoundError` when `self.repo_path` -- the target
        directory, or the project root of a single-file target -- is not a
        directory. The constructor recognises a single-file target only
        while the file exists, so a deleted or mistyped path fell through as
        a directory run rooted at a non-directory: the walk yielded nothing
        and the run reported success having indexed nothing (issue #1651).
        Raising here rather than in the constructor keeps construction cheap
        and side-effect free for callers that never run. A single-file
        target deleted AFTER construction passes this check (its root
        exists) and is a separate decision (#1737). `holds_sync_lock` runs
        the check (`_require_repo_dir`) before it opens the lock, so the
        missing path is what the caller is told about.

        Raises `SyncInProgressError`, before anything is written, while
        another writer holds the checkout's sync lock, which
        `holds_sync_lock` takes around the whole run (issue #2441),
        `UnsafeSyncLockError` when that lock file is a symbolic link, and
        `SyncLockUnavailableError` when it is there but cannot be opened
        even read-only. The lock lives in the checkout's state directory;
        when there is none and CGR_HOME cannot be written to create it, the
        run goes ahead without it, after a warning.

        `committed` turns True once the run has nothing left that could leave
        the graph partial: at the commit point, or on the in-sync fast path.
        A Ctrl+C can land after that and before the return (#2442), so where
        the interrupt surfaced does not say whether the graph is whole.
        """
        if self._start_run(force):
            self._finish_in_sync_run()
            self.legacy_retirement.retire_dotted_project()
            return
        self._prepare_pass_2(force)
        known_module_paths = self._run_definition_passes(force)
        self._run_call_pass(known_module_paths)
        self._run_post_passes()
        self._close_run()

    def _start_run(self, force: bool) -> bool:
        """Reset the per-run state and settle what the caches can still be
        trusted for; whether the graph is already in sync with the tree."""
        # First, so an interrupt anywhere below reads this run's answer, not
        # a reused updater's previous one.
        self.committed = False
        self._run = _SyncRun()
        # The built passes drop what they recorded about the previous run.
        self.factory.begin_run()
        # Reset per-run parse tracking so a reused updater does not reprocess
        # a previous run's files in Pass 3.
        self._parsed_files.clear()
        # Per-run for the same reason: the warning names THIS run's files.
        self.ast_grep_tier.reset_unparsed()
        # Per-run for the same reason: set on the in-sync early return below
        # and previously cleared only in `__init__`, so a reused instance kept
        # reporting a previous run's skip. `cli.py` reads it to decide what to
        # report, so a stale True describes a run that did real work as
        # already in sync (#1620).
        self.skipped_because_in_sync = False
        # Read before the write below replaces it: the root the graph's
        # project was last indexed from decides whether this repo's cache
        # still describes it (#2411).
        previous_root = (
            self.sync_keeper.stored_project_root()
            if not force
            and self._single_file is None
            and (self.prepared_state_dir / cs.HASH_CACHE_FILENAME).is_file()
            else None
        )
        self._sink.ensure_node_batch(
            cs.NODE_PROJECT,
            {
                cs.KEY_NAME: self.project_name,
                cs.KEY_ROOT_PATH: str(self.repo_path.resolve()),
                # The capture is part of the parser fingerprint, so a run that
                # returns early as in sync captured this same selection.
                cs.KEY_CAPTURED_RELATIONSHIPS: sorted(
                    rel.value for rel in self.capture.enabled_rels
                ),
            },
        )
        logger.info(ls.ENSURING_PROJECT, name=self.project_name)

        if not force and self._single_file is None:
            self.sync_keeper.drop_cache_if_graph_lost(
                self._run, self.prepared_state_dir, previous_root
            )
            self.sync_keeper.reparse_all_if_parser_changed(
                self._run, self.prepared_state_dir
            )

        # Discovery must precede the in-sync check: a build that appeared
        # since the cached run changes the eligible set, and the check walks
        # with the same unignore patterns the indexing pass will use.
        self._register_generated_sources()
        # The checks above may already have answered these against the
        # unignore set discovery just replaced, so they are asked again.
        self._run.exclusion_match = None
        self._run.cache_project_match = None
        self._run.graph_state_unknown = False
        return not force and self._is_already_in_sync()

    def _prepare_pass_2(self, force: bool) -> None:
        """Pass 1, and the compiler frontends whose facts Pass 2 reads."""
        # Cleared only when a REAL indexing run begins: an in-sync no-op above
        # must keep the last run's ownership map, which a later watch-event
        # prefix sweep still reads (issue #1025).
        self._frontend_owned_qns.clear()

        logger.info(ls.PASS_1_STRUCTURE)
        # Read BEFORE Pass 1: identify_structure emits this run's Package
        # nodes, so a read after it could not tell a new package from an old
        # one (issue #1570).
        self._packages_before_run = None if force else self._package_paths()
        structure = self.factory.structure_processor.identify_structure()
        logger.info(
            ls.STRUCTURE_IDENTIFIED.format(
                packages=structure.packages, folders=structure.folders
            )
        )

        # Cleared here, not only in run_cpp_frontend: in HYBRID that method
        # runs AFTER Pass 2, so its reset is too late for a REUSED updater
        # whose previous run was LIBCLANG. _process_files consumes this set to
        # skip files, and a stale entry makes it skip a file whose Module
        # subtree was just deleted -- leaving only a generic File node that the
        # later hybrid pass cannot repair, because it never produced the
        # tree-sitter definitions to restore.
        self.frontend_runner.cpp_covered = frozenset()

        # LIBCLANG must run before Pass 2: _process_files consumes the
        # covered-file set to skip those files.
        if settings.CPP_FRONTEND != cs.CppFrontend.HYBRID:
            self.frontend_runner.run_cpp_frontend()

        # The C# Roslyn frontend must run before Pass 2: it produces a
        # base-classification oracle that split_csharp_bases consults while
        # ingesting each type's INHERITS/IMPLEMENTS edges during Pass 2.
        self.frontend_runner.run_csharp_frontend()
        # Go facts are read at Pass 3 and the name-alias target index is filled
        # during Pass 2, so loading them here (before Pass 2) is correct.
        self.frontend_runner.run_go_frontend()

    def _run_definition_passes(self, force: bool) -> dict[str, str]:
        """Pass 2, the joins that need its registrations, and the deferred
        definition-level stages; the module qn -> path map they produced."""
        logger.info(ls.PASS_2_FILES)
        try:
            self._process_files(force=force)
            # Before the partial join on an incremental run: rebuild the type
            # locations for unchanged .cs files so a partial part living in one
            # still joins its group (issue #1229). Pass-2 entries for re-parsed
            # files are already present and take precedence. A cacheless full build
            # (force=False but _is_full_build) already re-parsed every file, so the
            # project-wide query would be wasted work -- skip it.
            if not force and not self._run.is_full_build:
                self.graph_rehydrator.rehydrate_csharp_type_locations(self._run)
                # Same posture for the col-keyed indexes (issue #1240): the Go
                # IMPLEMENTS and semantic-call joins below resolve against
                # locations Pass 2 filled only for re-parsed files.
                self.graph_rehydrator.rehydrate_go_type_locations(self._run)
                self.graph_rehydrator.rehydrate_function_locations(self._run)

            # Partial groups join AFTER Pass 2: the Roslyn declaration
            # locations resolve against the Class qns Pass 2 just registered.
            csharp_joins.join_partials(self.factory.definition_processor)

            # Go IMPLEMENTS pairs join AFTER Pass 2 for the same reason: both ends
            # resolve against the go_type_locations index Pass 2 just registered.
            go_joins.join_implements(self.factory.definition_processor, self._sink)

            # The Jedi Python frontend runs AFTER Pass 2 (its facts join Pass 3
            # calls against the function_locations Pass 2 just filled) and needs
            # the parsed-file list, which Pass 2 produced (issue #1183).
            self.frontend_runner.run_python_frontend(self._parsed_files)

            # Same posture for Java (issue #1181): the facts resolve against the
            # method name-token locations Pass 2 registered.
            self.frontend_runner.run_java_frontend()

            # Before known_module_paths is built from the map: the seed pass only
            # ever adds, so a reused updater carries qns whose Module another
            # writer has since deleted.
            #
            # A forced run prunes too. It re-parses every file but does NOT clear
            # the map, so on a reused updater a qn whose Module and file are both
            # gone survives a full rebuild -- measured, and the reason this is not
            # gated on `not force`. Nothing this run parsed can be dropped
            # (`_parsed_files` exempts it) and an unreadable or empty read is
            # already a no-op, so the first full build of an empty graph is
            # unaffected.
            self.module_seeding.prune_stale_seeded_module_qns()

            known_module_paths = self.deferred_resolution.resolve_deferred_definitions(
                self._run,
                rehydrate=not force,
                reparsed_keys=self._reparsed_file_keys,
            )
        finally:
            # After the deferred stages, as `_reingest_resolve` does, so an
            # edge into a re-parsed Go receiver method finds it registered
            # (#3270). In `finally`, with Pass 2 inside the `try`: a run
            # stopped by a failing file, by the cache stash that closes Pass 2
            # or by a later stage cannot capture these edges again, since
            # their targets' subtrees are already gone, so it restores them
            # on the way out, Go receiver methods included.
            self.edge_carry.restore_pending_inbound_edges(self._run)
        return known_module_paths

    def _run_call_pass(self, known_module_paths: dict[str, str]) -> None:
        """Pass 3, and the edges that join against what it resolved."""
        logger.info(ls.FOUND_FUNCTIONS, count=len(self.function_registry))
        # The resolver memoises name -> qn answers and module languages
        # across files; on a reused updater (any caller that holds one across
        # `run()` calls, as the tests do) those answers can name definitions
        # this run deleted or renamed, so every run starts Pass 3 with empty
        # caches, as the watcher's per-event pass already does (issue #1575).
        self.factory.call_processor.reset_resolution_caches()
        # A run that re-parses every file re-walks every file, so the
        # callable-flow records start empty: a reused updater's records for a
        # file deleted since would otherwise keep emitting its call sites'
        # edges. An incremental run keeps them, since finalize still needs the
        # seeds of files it does not re-walk.
        if self._run.is_full_build:
            self.factory.call_processor.reset_callable_flow()
        logger.info(ls.PASS_3_CALLS)
        self.factory.import_processor.csharp.recover_unparsed_csharp_state(
            known_module_paths
        )
        self._process_function_calls()

        # IMPORTS flush AFTER Pass 3: a C# namespace import lands on the
        # modules the file actually resolved entities from, and those
        # resolutions are recorded during call processing (issue #1347).
        imports_emitted = self.factory.import_processor.flush_deferred_import_edges(
            known_module_paths
        )
        if imports_emitted:
            logger.info(ls.VERIFIED_IMPORTS_EMITTED.format(count=imports_emitted))

        # LINQ query-operator edges join AFTER Pass 3 with the complete
        # function-location registry (both ends must be registered nodes).
        csharp_joins.emit_query_calls(
            self.factory.definition_processor, self.repo_path, self._sink
        )
        self._write_unresolved_references()

        self.factory.definition_processor.process_all_method_overrides(
            self.known_module_paths()
        )

    def _run_post_passes(self) -> None:
        """The passes that need every module parsed and every call resolved."""
        # Deferred endpoint emission: every module is parsed now, so router
        # mount prefixes (possibly cross-module) can resolve (issue #877).
        self.endpoint_linker.emit_pending_endpoints(self._run)

        # Call-registered routes (Express, net/http, echo, gin) become
        # endpoints too, so JS and Go servers are linkable (issue #886).
        self.endpoint_linker.emit_route_call_endpoints(self._run)
        self.sync_keeper.record_exposes_cleanup(
            self._run, self.prepared_state_dir, cleared_by_this_run=True
        )

        # ast-grep findings post-pass (opt-in FINDINGS group). Links to the
        # Modules the definition pass already emitted, so no dangling edges.
        self.finding_analyzer.analyze(
            self.factory.definition_processor.module_qn_to_file_path
        )

        # Once per sync, after every file was seen: files the ast-grep tier
        # would parse but could not, for want of its extra (issue #2634).
        self.ast_grep_tier.warn_unparsed()
        logger.info(ls.ANALYSIS_COMPLETE)
        self.ingestor.flush_all()
        # After that flush: the carry reads the static edges Pass 3 just
        # wrote to decide which observations confirm one (issue #2429).
        self.edge_carry.carry_trace_edges(
            self._run.captured_trace_edges, self._run.trace_gone_paths
        )
        self._run.captured_trace_edges = []

        self.endpoint_linker.link_endpoint_resources(
            self.exclude_paths, self.unignore_paths
        )

    def _close_run(self) -> None:
        """Prune, embed and commit: the steps that make the run's graph whole."""
        self.graph_pruner.prune_orphan_nodes(self._run, self.prepared_state_dir)
        self.gloss_reanchor.reanchor()
        # The prune issues its own deletes and has no flush of its own, so the
        # flush above cannot cover them (issue #1645). Without this they are
        # still queued when the caches commit below, and a run that stops in
        # between tells its successor the tree was reconciled while the
        # deletes never became durable: the successor takes the in-sync fast
        # path, never re-issues them, and the orphan survives until a full
        # rebuild. Same invariant as #1615, on the writes that pass fixed.
        self.ingestor.flush_all()
        # A complete prune settles an owed one only once its deletes are
        # durable: cleared before this flush, a failed flush would leave the
        # orphan AND an unmarked tree the next run skips (#2125).
        self.graph_pruner.settle_prune_marker(self._run, self.prepared_state_dir)

        self.embedding_sync.generate_semantic_embeddings(
            self._run, self.prepared_state_dir
        )

        self._commit_run_state()
        if self._run.embeddings_interrupted:
            raise ex.EmbeddingsInterrupted
        self.legacy_retirement.retire_dotted_project()

    def _finish_in_sync_run(self) -> None:
        logger.info(ls.GRAPH_ALREADY_IN_SYNC)
        self.skipped_because_in_sync = True
        # The re-anchor runs here too: a run whose re-anchor failed still
        # published its cache, so the next unchanged run takes this path,
        # and "re-attached by the next run" (the re-anchor's own contract)
        # would otherwise be false for exactly the run that needs it. Two
        # statements, no-ops when every note is attached (issue #1808).
        self.gloss_reanchor.reanchor()
        self.ingestor.flush_all()
        if self._single_file is None and not self._run.graph_state_unknown:
            self.sync_keeper.stamp_exclusion_state(
                self.prepared_state_dir, only_if_changed=True
            )
        self.sync_keeper.record_sync_time()
        # Nothing to save on this path, and nothing left half-written.
        self.committed = True

    def _commit_run_state(self) -> None:
        state_dir = self.prepared_state_dir
        # The delombok state commits ONLY here, after every pass and the
        # graph flush succeeded: a run that dies mid-way must not convince
        # its successor that the overlay-affected files were reprocessed.
        # Single-file runs never commit it.
        if self._single_file is None:
            self.generated_sources.commit(state_dir)
        self.sync_keeper.commit_caches(self._run, state_dir)
        flush_stdlib_cache()
        self.sync_keeper.record_sync_time()
        # Last, so an interrupt anywhere above still reads as a partial run.
        self.committed = True

    @property
    def _graph(self) -> QueryProtocol | None:
        """The sink as a graph this run can read back, or None for a
        write-only sink (the protobuf exporter): every read then takes its
        safe answer instead of querying."""
        ingestor = self.ingestor
        return ingestor if isinstance(ingestor, QueryProtocol) else None

    def _graph_rows(self, query: str, params: PropertyParams | None) -> list[ResultRow]:
        # A write-only sink (the protobuf exporter) has no graph to read, and
        # every caller already handles a failed read with its safe answer.
        graph = self._graph
        if graph is None:
            raise TypeError(
                ex.INGESTOR_NOT_QUERYABLE.format(kind=type(self.ingestor).__name__)
            )
        return graph.fetch_all(query, params)

    def _registered_project_names(self) -> list[str]:
        """Every project the graph holds, longest name first, read once per
        run. Degrades to this project alone, which keeps the prefix rule."""
        if self._run.registered_projects is not None:
            return self._run.registered_projects
        names: set[str] = {self.project_name}
        try:
            rows = self._graph_rows(cq.CYPHER_LIST_PROJECTS, None)
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.warning(
                ls.PRUNE_QUERY_FAILED, label="registered projects", error=exc
            )
            self._run.registry_unread = True
            rows = []
        for row in rows:
            name = row.get(cs.KEY_NAME)
            if isinstance(name, str) and name:
                names.add(name)
        registered = sorted(names, key=str.__len__, reverse=True)
        self._run.registered_projects = registered
        return registered

    def _owns(self, qn: str) -> bool:
        """Whether this project, not another whose name it is a dotted prefix
        of, owns `qn`. Every rehydration read is scoped by `STARTS WITH
        $project_prefix`, and `svc.` selects `svc.v2`'s rows too; the owner
        is the LONGEST registered project name the qn sits under, the way
        `gloss.repair._projects_of` decides it (issue #1970)."""
        if qn != self.project_name and not qn.startswith(
            f"{self.project_name}{cs.SEPARATOR_DOT}"
        ):
            return False
        for name in self._registered_project_names():
            if qn == name or qn.startswith(f"{name}{cs.SEPARATOR_DOT}"):
                return name == self.project_name
        return True

    def _owned_rows(self, rows: list[ResultRow], key: str) -> list[ResultRow]:
        """`rows` whose `key` names a qualified name this project owns."""
        return [
            row
            for row in rows
            if isinstance(qn := row.get(key), str) and self._owns(qn)
        ]

    def _write_unresolved_references(self) -> None:
        """Write every re-parsed module's unresolved references onto its node,
        the empty list included, so a name that now resolves is gone (issue
        #1568). A property SET rather than a second node write: a node write
        carrying two keys reads as the whole node to every store that does
        not merge. Flushed first so the Module nodes of a first build exist
        to match; the modules with nothing recorded (most) are cleared in
        one statement, the rest set one by one. A store that takes no query
        but can update a node it holds (the protobuf index) has the list set
        in place, or the exported field stays empty (#3206); any other store
        is left as it was, which reads as "nothing waited"."""
        recorded = self.factory.import_processor.unresolved_references
        cleared: list[str] = []
        pending: list[tuple[str, list[str]]] = []
        for (
            module_qn,
            path,
        ) in self.factory.definition_processor.module_qn_to_file_path.items():
            # A module recorded outside this checkout has no key a re-parse
            # could produce, so it was not re-parsed.
            if not path.is_relative_to(self.repo_path):
                continue
            key = path.relative_to(self.repo_path).as_posix()
            if key not in self._reparsed_file_keys:
                continue
            names = sorted(recorded.get(module_qn, ()))
            if names:
                pending.append((module_qn, names))
            else:
                cleared.append(module_qn)
        if not cleared and not pending:
            return
        graph = self._graph
        if graph is None:
            self._update_unresolved_reference_nodes(cleared, pending)
            return
        self.ingestor.flush_all()
        try:
            if cleared:
                graph.execute_write(
                    cs.CYPHER_CLEAR_UNRESOLVED_REFERENCES, {cs.KEY_QNS: cleared}
                )
            for module_qn, names in pending:
                graph.execute_write(
                    cs.CYPHER_SET_UNRESOLVED_REFERENCES,
                    {cs.KEY_QN: module_qn, cs.CYPHER_PARAM_NAMES: names},
                )
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the write is retried later
            logger.warning(ls.UNRESOLVED_REFERENCES_WRITE_FAILED, error=exc)

    def _update_unresolved_reference_nodes(
        self, cleared: list[str], pending: list[tuple[str, list[str]]]
    ) -> None:
        # Never a second node write: one carrying only these two keys reads
        # as the whole Module node to every store that does not merge.
        if not isinstance(self.ingestor, NodeUpdateProtocol):
            return
        for module_qn, names in (*((qn, []) for qn in cleared), *pending):
            self.ingestor.update_node_properties(
                cs.NodeLabel.MODULE,
                {
                    cs.KEY_QUALIFIED_NAME: module_qn,
                    cs.KEY_UNRESOLVED_REFERENCES: names,
                },
            )

    def _package_paths(self) -> set[str] | None:
        """Relative paths of this project's Package nodes, None if unreadable."""
        graph = self._graph
        if graph is None:
            return set()
        try:
            # Scoped: every sync read every project's Packages (issue #2405).
            rows = graph.fetch_all(
                cs.CYPHER_PROJECT_PACKAGE_PATHS,
                {
                    cs.KEY_PROJECT_NAME: self.project_name,
                    cs.KEY_PROJECT_PREFIX: f"{self.project_name}{cs.SEPARATOR_DOT}",
                },
            )
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.warning(ls.PRUNE_QUERY_FAILED, label="Package", error=exc)
            return None
        paths: set[str] = set()
        for row in rows:
            path = row.get(cs.KEY_PATH)
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            if not isinstance(path, str) or not isinstance(qn, str):
                continue
            # Ownership, not the prefix: a package only `svc.v2` holds would
            # otherwise count as one of `svc`'s (issue #2126).
            if self._owns(qn):
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
        for qn in self.factory.facts.declared_module_qns:
            known.setdefault(qn, "")
        for qn in self.graph_rehydrator.module_qns:
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
        #
        # This removal is ALSO what keeps the list free of duplicates. The
        # appender in `FileParser.process_single_file` appends unconditionally, so
        # every re-parse route has to strip the old entry first -- the
        # incremental path calls this method, and `reingest` gets there via
        # `_reingest_delete`. A deduplicating appender is deliberately not
        # the mechanism: one existed (`register_parsed_file`, added for
        # #1028) and was left callerless when #1524 moved the watcher onto
        # `reingest`, where it read as the guarantee's source while never
        # running. Removed in #1784; the invariant lives here.
        self._parsed_files[:] = [
            entry for entry in self._parsed_files if entry[0] != file_path
        ]
        # Same for its callable-flow records: a re-parse re-records them on
        # the walk, a deletion never does. Through `_call_processor`, not the
        # property: the property builds the processor on first access, and
        # building it here, before the definition pass, snapshots half-built
        # state (a C# method then ingested under a bare, unnamespaced name).
        # With no processor yet there are no records to drop.
        if self.factory.built_call_processor is not None:
            self.factory.built_call_processor.forget_callable_flow(file_path)

        self.file_state_sweep.sweep(file_path, frontend_current)

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
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.debug(ls.GRAPH_READ_UNAVAILABLE, label="module paths", error=exc)
            return None
        try:
            # A row without a qualified name (an older reader's shape) keeps
            # the prefix rule the query itself applied.
            return frozenset(
                path
                for row in rows
                if isinstance(path := row.get(cs.KEY_PATH), str)
                and not is_inline_module_path(path)
                and (
                    not isinstance(qn := row.get(cs.KEY_QUALIFIED_NAME), str)
                    or self._owns(qn)
                )
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
        graph = self._graph
        if graph is not None:
            graph.execute_write(
                cs.CYPHER_DELETE_MODULE,
                {
                    cs.KEY_PATH: file_key,
                    cs.KEY_PROJECT_NAME: self.project_name,
                    cs.KEY_PROJECT_PREFIX: self.project_name + ".",
                    cs.KEY_NESTED_PROJECTS: self._nested_project_names(),
                },
            )

    def _nested_project_names(self) -> list[str]:
        """Registered projects whose names extend this one, whose modules
        the prefix-scoped module delete must leave alone (issue #1985)."""
        prefix = f"{self.project_name}{cs.SEPARATOR_DOT}"
        return [
            name for name in self._registered_project_names() if name.startswith(prefix)
        ]

    def _is_already_in_sync(self) -> bool:
        state_dir = self.prepared_state_dir
        return not self.sync_keeper.forces_reparse(
            self._run, state_dir
        ) and self.sync_keeper.cache_matches_disk(self._run, state_dir)

    def _register_generated_sources(self) -> None:
        # The walk keeps what this run's generated sources rescue, and so
        # does every reader of the walk's scope (issue #1088).
        self.unignore_paths = self.generated_sources.refresh(self.prepared_state_dir)

    def _process_files(self, force: bool = False) -> None:
        self.file_pass.process_files(
            self._run, self.prepared_state_dir, force, self._reparsed_file_keys
        )

    def process_with_secondary_tier(self, filepath: Path) -> bool:
        """Parse a file with whichever non-tree-sitter tier claims it.

        The updater's own entry to `FileParser.process_with_secondary_tier`:
        True when a tier took the file.
        """
        return self.file_parser.process_with_secondary_tier(filepath)

    def _process_function_calls(self, only: Collection[Path] | None = None) -> None:
        # `only` scopes the pass to a re-ingested subset (issue #1524); every
        # other file keeps the edges it already has.
        # A reused updater (watch mode, a second run) re-parses files; the
        # JS receiver-binding index holds nodes from the PREVIOUS parse,
        # whose spans would resolve against the refreshed registry onto
        # whatever now sits at the old line/column.
        self.factory.call_processor.reset_js_receiver_bindings()
        self.factory.call_processor.failed_call_files.clear()
        captures_cache = self.factory.func_class_captures_cache
        # Iterate every file parsed this run, not the bounded AST cache: on a
        # large repo the cache evicts most files, and iterating it drops their
        # calls (a whole module ends up with zero CALLS edges).
        files = (
            self._parsed_files
            if only is None
            else [(fp, lang) for fp, lang in self._parsed_files if fp in only]
        )
        for file_path, language in files:
            root_node = self.file_parser.ast_for(file_path)
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
            root_node = self.file_parser.ast_for(file_path)
            if root_node is None:
                continue
            # Issue #1837: there is deliberately no capture-based skip here.
            # The old one ("no calls and no functions, so nothing to walk")
            # was unsound in both directions. A MODULE-LEVEL reference -- a
            # dispatch table holding an imported handler, a bare
            # `x = handler`, a JSX element -- is a real CALLS edge that lives
            # under no capture kind at all, and process_calls_in_file runs
            # those passes ahead of its own no-calls early return precisely
            # so such a file is covered. A captured class is positive
            # evidence of work too: the Python decorator pass takes class
            # nodes as its targets.
            #
            # An all-absent entry IS possible on this branch: the populator
            # writes {} whenever the query RAN, to keep the cache honest
            # about a file that now parses to nothing (#1794). So the
            # narrowed guard is not unreachable-and-therefore-harmless, as
            # the #1837 reasoning had it on a tree without that change -- it
            # is unsound. An all-absent entry is precisely the module-level
            # reference case: a file whose only CALLS edge lives under no
            # capture kind. Measured by reinstating it: the guard fires on
            # 19 files and reddens test_a_module_level_reference_survives_
            # the_re_parse, test_class_only_file_still_emits_its_module_
            # level_call and test_every_parsed_file_reaches_the_call_walk.
            # Do not re-add it.
            #
            # Uncached files were always walked, so the walk is now uniform.
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
        #
        # `emit=False`: the MAP is what hydration needs, and it needs all of
        # it -- a parent lookup can reach any directory, so `only` is not an
        # option here. Emitting is a different matter. The unrestricted walk
        # writes a node for every directory as it is on disk NOW, so a
        # directory whose package-ness changed since the last index gained a
        # node of the new kind beside its surviving old one -- two
        # contradictory container identities, for a directory this scoped
        # call never named (issue #1872). The scoped re-derivation inside
        # `reingest` is what legitimately emits, and it restricts itself to
        # the flipped directories and their children.
        self.factory.structure_processor.identify_structure(emit=False)
        self.graph_rehydrator.rehydrate_registry_from_graph(
            self._run, self._reparsed_file_keys
        )
        self.graph_rehydrator.rehydrate_function_locations(self._run)
        self._reingest_hydrated = True

    def _rerun_frontends_for(self, languages: set[cs.SupportedLanguage]) -> None:
        # Semantic facts are location-keyed against the compiler's view of
        # the whole module, so a change in one file can rebind calls in
        # unchanged files (issue #1229 phase 3): the applicable frontend
        # re-runs and its joins re-emit before the scoped call pass.
        if cs.SupportedLanguage.GO in languages:
            self.frontend_runner.run_go_frontend()
            self.graph_rehydrator.rehydrate_go_type_locations(self._run)
            self.graph_rehydrator.rehydrate_function_locations(self._run)
            go_joins.join_implements(self.factory.definition_processor, self._sink)
        if cs.SupportedLanguage.CSHARP in languages:
            self.frontend_runner.run_csharp_frontend()
            self.graph_rehydrator.rehydrate_csharp_type_locations(self._run)
            self.graph_rehydrator.rehydrate_function_locations(self._run)
            csharp_joins.join_partials(self.factory.definition_processor)
        if cs.SupportedLanguage.JAVA in languages:
            self.frontend_runner.run_java_frontend()
            self.graph_rehydrator.rehydrate_function_locations(self._run)
        if cs.SupportedLanguage.PYTHON in languages:
            self.frontend_runner.run_python_frontend(self._parsed_files)
            self.graph_rehydrator.rehydrate_function_locations(self._run)

    def _reingest_delete(
        self, reparse: dict[str, Path], gone: dict[str, Path], hashes: FileHashCache
    ) -> None:
        import_processor = self.factory.import_processor
        # A fresh updater read every definition's annotations back from the
        # graph into the pending type facts BEFORE this delete; the facts of
        # the files re-parsed or dropped here describe the old source, and
        # the re-parse queues the new ones, so without this both the stale
        # and the fresh RETURNS/ACCEPTS edge would be emitted (issue #1527).
        # Keyed on the FILE, not the module qn: `foo.py` and `foo/__init__.py`
        # share `proj.foo`, and a module-qn filter dropped the unchanged
        # file's facts with the re-parsed one's, leaving its detached
        # RETURNS/ACCEPTS unbuilt (issue #1892). A fact ingested without a
        # file (no path) falls back to the module-qn key.
        stale_keys = {*reparse, *gone}
        qn_to_path = self.factory.definition_processor.module_qn_to_file_path
        stale_paths = {*reparse.values(), *gone.values()}
        stale_modules = {
            base_module_qn(Path(key), self.project_name) for key in stale_keys
        } | {qn for qn, path in qn_to_path.items() if path in stale_paths}
        pending = self.factory.definition_processor.pending_type_facts
        pending[:] = [
            fact
            for fact in pending
            if (
                fact.path not in stale_keys
                if fact.path is not None
                else fact.module_qn not in stale_modules
            )
        ]
        # The same for parameter annotations: the scoped prologue rehydrates
        # them from the graph before this delete, so a changed annotation
        # would otherwise emit OF_TYPE to both the old and the new type.
        pending_params = self.factory.definition_processor.pending_parameter_types
        pending_params[:] = [
            fact for fact in pending_params if fact.path not in stale_keys
        ]
        # Fields the same, keyed on the file for the same reason.
        pending_fields = self.factory.definition_processor.pending_field_types
        pending_fields[:] = [
            fact for fact in pending_fields if fact.path not in stale_keys
        ]
        for key, path in reparse.items():
            self.remove_file_from_state(path)
            self._delete_module_entities(key)
            # The Rust path caches were filled by the last run; a created or
            # modified file must be re-observed before crate::/super:: paths
            # resolve against them.
            import_processor.rust.refresh_rust_path_caches_for(
                path, created=key not in hashes
            )
            # Julia's stem/declared-module indices are the same kind of
            # per-layout cache: any .jl change invalidates the whole set.
            if path.suffix == cs.EXT_JL:
                import_processor.julia.reset_julia_path_caches()
            # A pubspec's `name:` is what Dart `package:` imports resolve by.
            if path.name == cs.DEP_FILE_PUBSPEC:
                import_processor.dart.reset_dart_path_caches()
        graph = self._graph
        for key, path in gone.items():
            self.remove_file_from_state(path)
            self._delete_module_entities(key)
            # A deleted .jl file invalidates the same path caches reparse does.
            if path.suffix == cs.EXT_JL:
                import_processor.julia.reset_julia_path_caches()
            if path.name == cs.DEP_FILE_PUBSPEC:
                import_processor.dart.reset_dart_path_caches()
            if graph is not None:
                # Keyed on the absolute path: a sibling project's File node
                # can share the relative path (issue #897). The entry's own
                # identity, as the File was indexed: a file replaced by a
                # link resolves to the link's target (#2451).
                graph.execute_write(
                    cs.CYPHER_DELETE_FILE,
                    {cs.KEY_PATH: cached_file_identity_posix(path)},
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
            self.frontend_runner.cpp_covered = frozenset()
            self.frontend_runner.run_cpp_frontend()
        parsed: dict[str, bytes] = {}
        for key, path in reparse.items():
            # Read once and hash those same bytes later: an edit landing
            # between the parse and a second read would otherwise be
            # recorded in the cache as already indexed.
            try:
                parsed[key] = self.reingest_planner.source_for(self._run, key, path)
            except OSError as exc:
                # The file was classified from disk and has since become
                # unreadable -- deleted, or replaced by a directory, between
                # the split and this read. Its old entities are already gone
                # (`_reingest_delete` ran above), so staying silent here
                # reports the path as re-parsed while the graph no longer
                # holds it. `parsed` is what the report is built from, so
                # omitting the key is what makes the answer honest; the log
                # says why the caller sees one fewer file than it named.
                logger.warning(ls.REINGEST_UNREADABLE, path=str(path), error=str(exc))
                continue
            # The delombok overlay stands in for the checked-in bytes exactly
            # as the batch path does (issue #1140).
            self.file_parser.process_single_file(
                path, file_bytes=self.generated_sources.overlay.get(key, parsed[key])
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
        # hydrated above, so no registry read-back here.
        known_module_paths = self.deferred_resolution.resolve_deferred_definitions(
            self._run, rehydrate=False, reparsed_keys=self._reparsed_file_keys
        )
        # A fresh updater read the implementer pairs back BEFORE the delete,
        # so a class the edit stopped implementing still counted as the sole
        # implementer and the re-parsed caller kept its edge (#2403).
        # Read back again now that the re-parse registered the traits, as
        # run() does. The inbound edges are not restored yet, and an
        # unchanged impl of a re-parsed trait that no edge makes a dependent
        # (`impl crate::Trait for u64`) is recorded only by its OVERRIDES
        # edge, so the captured ones count as read. The parsed pairs stay:
        # the re-parse records the deleted files' pairs, and the unchanged
        # files' include impls the graph cannot show.
        self.graph_rehydrator.rehydrate_interface_implementers(
            self._run,
            {cs.KEY_PROJECT_PREFIX: self.project_name + cs.SEPARATOR_DOT},
            carried=captured,
        )
        # The re-parsed files' own CALLS edges went with their Module
        # subtrees; a moved use must not serve last pass's cached answer.
        self.factory.call_processor.reset_resolution_caches()
        self.factory.import_processor.csharp.recover_unparsed_csharp_state(
            known_module_paths
        )
        self._process_function_calls(only=set(reparse.values()))
        # Editing a PROVIDER recreated its Module node and severed the
        # unchanged C# importers' edges (issue #1347).
        import_processor = self.factory.import_processor
        import_processor.csharp.requeue_csharp_import_edges()
        import_processor.flush_deferred_import_edges(known_module_paths)
        csharp_joins.emit_query_calls(
            self.factory.definition_processor, self.repo_path, self._sink
        )
        self._write_unresolved_references()
        self.factory.definition_processor.process_all_method_overrides(
            self.known_module_paths()
        )
        # Endpoints and route registrations, scoped to the re-parsed modules:
        # the project-wide passes would load every route-capable module's
        # AST, which on a fresh updater means re-parsing most of the repo.
        reparsed_paths = set(reparse.values())
        touched_modules = {
            qn
            for qn, module_path in self.factory.definition_processor.module_qn_to_file_path.items()
            if module_path in reparsed_paths
        }
        self.endpoint_linker.emit_pending_endpoints(self._run, only=touched_modules)
        self.endpoint_linker.emit_route_call_endpoints(self._run, only=touched_modules)
        # A scoped pass covers only the touched modules, so it can owe a
        # cleanup but never settle one owed from elsewhere.
        self.sync_keeper.record_exposes_cleanup(
            self._run, self.prepared_state_dir, cleared_by_this_run=False
        )
        self.edge_carry.restore_inbound_edges(captured)
        graph = self._graph
        if graph is not None:
            graph.execute_write(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)
        # Flush FIRST: the re-parsed definitions are still buffered in the
        # ingestor here, and the grading statement compares a note against
        # its subject's current `anchor_hash` in the store. Graded before the
        # flush it finds no subject (the old node is already deleted, the new
        # one not yet written) and the note on the very file that was just
        # edited stays EXACT until the following sync (issue #1808).
        self.ingestor.flush_all()
        self.gloss_reanchor.reanchor()

    def has_indexed(self, path: Path) -> bool:
        """Whether the last run or re-ingest indexed the file at `path`.

        Asked by the watcher about a path that is a link now (issue #2451):
        the link is not indexed, but the file it replaced was, and its nodes
        must go.
        """
        try:
            key = path.relative_to(self.repo_path).as_posix()
        except ValueError:
            return False
        return key in load_hash_cache(self.prepared_state_dir / cs.HASH_CACHE_FILENAME)

    def indexed_files_under(self, directory: Path) -> list[Path]:
        """Files the last run or re-ingest indexed beneath `directory`.

        A deleted or moved-away directory arrives from the watcher as one
        event and its files are no longer on disk to list. The hash cache is
        the record every run and `reingest` keep of what they indexed, so it
        names exactly the files whose nodes must now go.
        """
        try:
            prefix = directory.relative_to(self.repo_path).as_posix()
        except ValueError:
            return []
        if prefix == ".":
            return []
        hashes = load_hash_cache(self.prepared_state_dir / cs.HASH_CACHE_FILENAME)
        return [
            self.repo_path / key
            for key in sorted(hashes)
            if key.startswith(prefix + "/")
        ]

    @holds_sync_lock(wait=True)
    def reingest(
        self,
        paths: Iterable[Path | str],
        deleted: Iterable[Path | str] = (),
        before_write: Callable[[], None] | None = None,
    ) -> ReingestReport:
        """Re-ingest the given files with file-scoped call resolution.

        `before_write`, when given, runs once at the exact point the prologue
        ends and the first DELETE or content write is about to be issued. A
        caller persisting recovery state uses it to record that the graph is
        about to change; if it raises, the run aborts as `ex.ReingestAborted`
        with the graph as it was, the same as any other prologue failure
        (#1705). "As it was" rather than "untouched": on a fresh
        updater the prologue's structure hydration re-emits Package and
        Folder upserts, which are idempotent and change nothing a later run
        would not write identically; nothing is deleted and no definition is
        written before the hook.

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
        same-named file has since reappeared (a watch DELETE event). The old
        file's definitions go in that case, but a reappeared path that is
        still a file is re-parsed rather than retracted, so the graph ends up
        describing what is on disk (issue #1799). Paths the project's ignore
        rules exclude are reported as ``skipped`` and left out of the graph,
        as the walk would leave them.

        Waits while another writer holds the checkout's sync lock, then
        applies the change to the graph that sync finished; the lock is
        taken by `holds_sync_lock` (issue #2441).
        """
        started = time.perf_counter()
        # The Python engine memoises per pass, as run() knows: on the
        # long-lived updater the MCP server, the watcher and rename hold, an
        # edited function's old return type would re-resolve its dependents,
        # and the Node-keyed entries would pin every re-parsed tree (#2927).
        self.factory.clear_inference_caches()
        # Fresh per-call state: a scoped re-ingest is never a full build,
        # whatever the previous run() was (the flag decides whether a failed
        # inbound-edge capture aborts, and whether unchanged handlers
        # rehydrate), and a project registered since must not read as owned.
        self._run = _SyncRun()
        # Per call: a caller holding this updater across many events must see
        # THIS call's answer, not the last one's.
        self.reingest_mutated = False
        self.reingest_scope = ()
        self.ast_grep_tier.reset_unparsed()
        present, gone, skipped = self.reingest_planner.split(paths, deleted)
        if skipped:
            logger.warning(ls.REINGEST_SKIPPED_IGNORED, paths=sorted(skipped))
        if not present and not gone:
            return ReingestReport((), (), (), tuple(sorted(skipped)), 0.0)

        plan = self._plan_reingest(present, gone)
        # The caller's last word before the first write. Still inside the
        # read-only prologue: a refusal here leaves the graph exactly as it
        # was, and `reingest_mutated` stays False so the caller classifies
        # it as "nothing changed". The scope is published first so the hook
        # can read what this call is about to replace (issue #1718).
        self.reingest_scope = tuple(plan.all_keys)
        try:
            if before_write is not None:
                before_write()
        except Exception as exc:
            raise ex.ReingestAborted(str(exc)) from exc
        finally:
            # Published for the hook only: the declaration promises an empty
            # scope outside a call, and a long-lived updater (the watcher,
            # the MCP server) would otherwise carry the last call's keys into
            # whatever reads it next (#1718).
            self.reingest_scope = ()
        # Past this point the run WILL issue deletes and writes. Callers that
        # persist recovery state need to know whether a failure left the graph
        # untouched or partial, and classifying by exception TYPE is not
        # enough: a future abort raised mid-run would be misread as "nothing
        # changed" and clear a marker that is protecting a partial graph
        # (#1705). This flag records what actually happened.
        self.reingest_mutated = True
        self._reparsed_file_keys = set(plan.all_keys)

        self._rederive_containers(present, plan.flipped_dirs)
        parsed = self._apply_reingest(plan, present, gone)

        report = ReingestReport(
            # `parsed`, not `present`: a file that became unreadable between
            # the split and the read never re-entered the graph, and its old
            # entities were already deleted. Reporting it as re-parsed would
            # tell the caller the graph holds something it does not.
            reparsed=tuple(sorted(present.keys() & parsed.keys())),
            # Survivors of a stem in flux re-parsed with the dependents and
            # are reported with them: the caller sees every file this call
            # touched.
            affected=tuple(sorted({*plan.affected, *plan.survivors})),
            removed=tuple(sorted(gone)),
            skipped=tuple(sorted(skipped)),
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        self.ast_grep_tier.warn_unparsed()
        logger.info(
            ls.REINGEST_DONE,
            reparsed=len(report.reparsed),
            affected=len(report.affected),
            removed=len(report.removed),
            ms=round(report.elapsed_ms, 1),
        )
        return report

    def _plan_reingest(
        self, present: dict[str, Path], gone: dict[str, Path]
    ) -> _ReingestPlan:
        """The read-only prologue: every file the call re-parses, and the
        edges into them it must carry across the delete."""
        # Everything up to the inbound-edge capture issues no delete and no
        # content write (a fresh updater's structure hydration
        # derives the package map and emits nothing, #1872). A failure
        # there leaves the graph as it was and says so with ex.ReingestAborted,
        # so a caller holding the updater (the MCP tool, the watcher) keeps
        # it rather than treating the graph as partially rewritten.
        try:
            return self._read_reingest_plan(present, gone)
        except Exception as exc:
            raise ex.ReingestAborted(str(exc)) from exc

    def _read_reingest_plan(
        self, present: dict[str, Path], gone: dict[str, Path]
    ) -> _ReingestPlan:
        """`_plan_reingest`'s reads, from the hydration to the edge capture."""
        self._hydrate_for_reingest()
        # Generated-source roots and the delombok overlay are per-run inputs;
        # a watcher's updater lives across many events, so refresh them per
        # call (the batch path does the same through
        # `GeneratedSources.stale_keys`).
        self._register_generated_sources()
        cache_path = self.prepared_state_dir / cs.HASH_CACHE_FILENAME
        hashes = load_hash_cache(cache_path)

        # Same-stem reconciliation, as the batch path does it (issue #1569):
        # a stem with a sibling added or deleted by this call is in flux, its
        # on-disk survivors re-parse unseeded, and the module-qn map is
        # seeded from the graph for everything else so the disambiguator
        # sees the taken qns on a fresh updater.
        flux_stems, survivors = self.reingest_planner.flux_survivors(
            present, gone, hashes
        )
        existing_paths = self._existing_module_paths()
        if existing_paths is None:
            # The sink claims a query surface but the read failed: the taken
            # qns are unknown, and seeding nothing would let a re-parsed
            # file claim a qn the graph already holds. Abort before any
            # delete, as the inbound-edge capture does.
            raise RuntimeError(ls.REINGEST_MODULE_PATHS_UNKNOWN)
        graph_paths = set(existing_paths)
        self.module_seeding.seed_module_qns_from_graph(
            graph_paths - set(gone), flux_stems
        )
        # A gone file seeds regardless: its map entry is what the state drop
        # below keys on to free its qn and forget its rehydrated form.
        self.module_seeding.seed_module_qns_from_graph(
            set(gone) & graph_paths, frozenset()
        )

        # A directory that gained or lost a package indicator changes the
        # container every file under it hangs off, so those files re-parse
        # too and the stale node of the other kind is pruned below.
        flipped_dirs, flip_siblings = self.container_flips.package_flip(present, gone)
        survivors.update(flip_siblings)

        affected = self.reingest_planner.dependents(
            self._run,
            {**present, **survivors},
            gone,
            created={key for key in present if key not in hashes},
            stable=self.reingest_planner.interface_stable(
                self._run, present, hashes, flux_stems, flipped_dirs
            ),
        )
        affected.update(
            self.reingest_planner.go_package_files(
                {*present, *gone, *survivors, *affected}
            )
        )
        affected.update(
            self.reingest_planner.foreign_members(
                self._run, {*present, *gone, *survivors, *affected}
            )
        )
        all_keys = sorted({*present, *gone, *survivors, *affected})
        return _ReingestPlan(
            cache_path=cache_path,
            hashes=hashes,
            survivors=survivors,
            affected=affected,
            flipped_dirs=flipped_dirs,
            all_keys=all_keys,
            captured=self.edge_carry.capture_inbound_edges(self._run, all_keys),
            captured_trace=self.edge_carry.capture_trace_edges(self._run, all_keys),
        )

    def _rederive_containers(
        self, present: dict[str, Path], flipped_dirs: set[str]
    ) -> None:
        """Derive the containers this call's directories now need."""
        # Past the caller's refusal point, so this may finally WRITE. Re-deriving
        # the structure updates `structural_elements` and emits the container
        # node of the new kind; the sibling re-parse below re-points the
        # containment edges onto it, and `ContainerFlips.prune_flipped`
        # removes the node of the old kind once both have happened.
        # New directories first: their container has to exist before the
        # re-parse writes containment edges out of it.
        # A directory with no container yet only needs deriving. One whose
        # recorded kind DIVERGED from disk also needs its stale node pruned,
        # or the derivation emits the new kind beside the old one -- the very
        # duplicate identity this path exists to prevent. Pruning is what
        # `flipped_dirs` already means, so it joins that set rather than
        # getting a second, parallel mechanism to keep in step.
        new_dirs, diverged_dirs = self.container_flips.uncontained_dirs(present)
        flipped_dirs.update(diverged_dirs)
        if scope := (new_dirs | diverged_dirs):
            self.factory.structure_processor.identify_structure(only=scope)
        if flipped_dirs:
            # Scoped to the flipped directories and their CHILDREN, never
            # their ancestors -- see `ContainerFlips.derivation_scope` for both
            # halves of that rule.
            #
            # Scoped at all because the unrestricted walk emits a node for
            # every directory that changed on disk since the last derivation,
            # so an unrelated directory whose indicator was removed elsewhere
            # gained a second container identity beside the one it already
            # had (PR #1835).
            self.factory.structure_processor.identify_structure(
                only=self.container_flips.derivation_scope(flipped_dirs)
            )

    def _apply_reingest(
        self, plan: _ReingestPlan, present: dict[str, Path], gone: dict[str, Path]
    ) -> dict[str, bytes]:
        """Delete, re-parse and resolve the planned files; the bytes parsed."""
        # Walk order, as the batch path re-parses (issue #1569): the first
        # same-stem sibling parsed claims the bare module qn, so a header
        # found before its dependent source file would take the qn a clean
        # index gives the source.
        reparse = dict(
            sorted(
                {**present, **plan.survivors, **plan.affected}.items(),
                key=lambda item: walk_position(item[0]),
            )
        )
        self._reingest_delete(reparse, gone, plan.hashes)
        self.file_parser.resync_dependencies((*reparse, *gone), reparse)
        parsed = self._reingest_reparse(reparse, gone)
        self.file_parser.log_unparsable_manifests()
        # After BOTH seed calls and after the re-parse, so a re-parsed file's
        # own entry is exempt while its Module node is still unflushed
        # (issue #1712). The exemption is the paths THIS call re-parsed, not
        # `_parsed_files`: that field accumulates here on purpose, since
        # `_hydrate_for_reingest` reads it as a liveness proxy, so the default
        # would exempt everything the process has ever parsed and the map
        # would never shrink on the retained updater this fix exists for.
        self.module_seeding.prune_stale_seeded_module_qns(set(reparse.values()))
        self._reingest_resolve(reparse, plan.captured)
        # `_reingest_resolve` ends on a flush, so the static edges the carry
        # may confirm are in the store (issue #2429).
        self.edge_carry.carry_trace_edges(plan.captured_trace, gone.keys())
        # AFTER the re-parse, never before: the surviving node of the correct
        # kind and its re-pointed containment edges are written by the parse
        # above, so pruning first would delete the old node while every edge
        # still hung off it and leave the files unparented in between.
        self.container_flips.prune_flipped(plan.flipped_dirs)
        # BEFORE the hash commit, matching `run()`, which does both passes
        # ahead of its cache write. `publish_reingest_hashes` saves the hash
        # cache to disk, which records these files as indexed; queueing the
        # rebuilt finding and link writes after it meant an interruption in
        # between left caches claiming the files were done while those writes
        # never landed -- and the next run would skip them as unchanged
        # (#1852).
        self._reingest_rebuild_findings(reparse)
        self.endpoint_linker.link_endpoint_resources(
            self.exclude_paths, self.unignore_paths
        )
        # Unconditionally, because neither post-pass guarantees one:
        # `_reingest_rebuild_findings` only QUEUES its nodes and edges, and
        # `link_endpoint_resources` returns early when RESOLVES_TO is
        # disabled or the ingestor cannot query, so on those paths nothing
        # flushes what the rebuild queued. The hash cache below records these
        # files as current, and a later run would take the in-sync path and
        # skip the post-passes entirely -- so an unflushed queue here is lost
        # for good rather than retried (#1852).
        self.ingestor.flush_all()
        self.sync_keeper.publish_reingest_hashes(
            plan.cache_path, plan.hashes, reparse, parsed, gone
        )
        flush_stdlib_cache()
        return parsed

    def _reingest_rebuild_findings(self, reparse: dict[str, Path]) -> None:
        """Re-run the finding analysis for the RE-PARSED modules only.

        `CYPHER_DELETE_MODULE` takes a re-parsed Module's finding nodes
        (`HAS_SMELL`, `HAS_VULNERABILITY`, `IMPLEMENTS_PATTERN`) with it
        (issue #2536), and without this pass nothing wrote them again until
        the next full `update_repository` (issue #1670). A re-ingested file
        therefore lost its findings and stayed lost, which reads as "this
        file is clean".

        Scoped rather than repo-wide, because the full pass is not cheap
        enough to run on every re-ingest -- the other option the issue
        offers. Measured over this repo's own parser tree: 1.001s for 125
        modules against 0.001s for one, so the full pass would dominate a
        scoped re-ingest and grow with the repository rather than with the
        change. The analyzer already takes a module map, so the scope is the
        argument and needs no new machinery.
        """
        processor = self.factory.definition_processor
        touched = set(reparse.values())
        scoped = {
            module_qn: path
            for module_qn, path in processor.module_qn_to_file_path.items()
            if path in touched
        }
        if scoped:
            self.finding_analyzer.analyze(scoped)
