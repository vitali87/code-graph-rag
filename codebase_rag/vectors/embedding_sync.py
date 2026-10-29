"""Keep the vector store in step with the graph's functions and methods."""

from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..config import settings
from ..language_spec import LANGUAGE_FQN_SPECS
from ..services import QueryProtocol
from ..sync.state import (
    embeddings_marker,
    embeddings_store_stamp,
    load_project_stamps,
    load_shared_embeddings_owed,
    save_embeddings_owed,
    save_embeddings_store,
    stamp_names_selected_store,
)
from ..types_defs import (
    ASTCacheProtocol,
    EmbeddingPoint,
    EmbeddingQueryResult,
    EmbeddingSymbol,
    ResultRow,
    ResultValue,
)
from ..utils.dependencies import has_semantic_dependencies
from ..utils.fqn_resolver import find_function_source_by_fqn
from ..utils.source_extraction import extract_source_with_fallback


class EmbeddingRun(Protocol):
    """What the embeddings pass reads and sets on the sync run it is part of:
    whether the project registry could be read, and whether a Ctrl+C stopped
    the pass, which `GraphUpdater.run` raises once it has committed."""

    registry_unread: bool
    embeddings_interrupted: bool


def _flush_embedding_batch(
    pending: list[tuple[int, EmbeddingSymbol, str]],
    expected: dict[int, EmbeddingSymbol],
    stored_ids: set[int],
    embed_code_batch: Callable[[list[str]], list[list[float]]],
    store_embedding_batch: Callable[[list[EmbeddingPoint]], int],
) -> int:
    # Embeds and stores the pending batch, then empties it in place so the
    # caller keeps appending to the same list. `stored_ids` gains the nodes
    # whose points the store confirmed, which `expected` cannot vouch for: it
    # is filled before the embeddings are even computed, so the check after
    # the pass counts a batch the model failed on as missing, and the sync
    # owes it to the next one like a batch the store refused.
    if not pending:
        return 0
    for node_id, symbol, _src in pending:
        expected[node_id] = symbol
    snippets = [item[2] for item in pending]
    try:
        embeddings = embed_code_batch(snippets)
    except Exception as e:  # noqa: BLE001 -- any embedding model error skips this batch, retried next sync
        logger.warning(
            ls.EMBEDDING_BATCH_COMPUTE_FAILED,
            count=len(pending),
            error=e,
        )
        pending.clear()
        return 0
    points: list[EmbeddingPoint] = [
        (node_id, emb, symbol) for (node_id, symbol, _), emb in zip(pending, embeddings)
    ]
    stored = store_embedding_batch(points)
    if stored:
        stored_ids.update(node_id for node_id, _emb, _symbol in points)
    pending.clear()
    return stored


def _embedded_label(label: ResultValue | None) -> cs.NodeLabel | None:
    if isinstance(label, str) and label in cs.EMBEDDED_NODE_LABELS:
        return cs.NodeLabel(label)
    return None


def _embedding_symbol(parsed: EmbeddingQueryResult) -> EmbeddingSymbol:
    return EmbeddingSymbol(parsed[cs.KEY_LABEL], parsed[cs.KEY_QUALIFIED_NAME])


def _under_any_project(qualified_name: object, projects: Sequence[str]) -> bool:
    return isinstance(qualified_name, str) and any(
        qualified_name.startswith(f"{project}{cs.SEPARATOR_DOT}")
        for project in projects
    )


def _log_embedding_progress(embedded_count: int, total: int) -> None:
    if (
        embedded_count > 0
        and embedded_count % settings.EMBEDDING_PROGRESS_INTERVAL == 0
    ):
        logger.debug(ls.EMBEDDING_PROGRESS, done=embedded_count, total=total)


class EmbeddingSync:
    """The embeddings pass of a sync, and the debt it leaves in the state
    directory when it could not finish.

    Each pass embeds every function and method the graph holds for the
    project, prunes the vectors of symbols it no longer holds, and checks the
    store kept what it was given. `nested_project_names` names the registered
    projects nested under this one, whose functions are theirs to embed.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        ast_cache: ASTCacheProtocol,
        repo_path: Path,
        project_name: str,
        skip_embeddings: bool,
        nested_project_names: Callable[[], list[str]],
    ) -> None:
        self._graph = graph
        self._ast_cache = ast_cache
        self._repo_path = repo_path
        self._project_name = project_name
        self._skip_embeddings = skip_embeddings
        self._nested_project_names = nested_project_names

    def _embedding_candidate(
        self, row: ResultRow
    ) -> tuple[int, EmbeddingSymbol, str] | None:
        # (node id, symbol, source) for one query row; None when the row does
        # not parse or its source cannot be read back from disk.
        parsed = self._parse_embedding_result(row)
        if parsed is None:
            return None
        qualified_name = parsed[cs.KEY_QUALIFIED_NAME]
        start_line = parsed.get(cs.KEY_START_LINE)
        end_line = parsed.get(cs.KEY_END_LINE)
        file_path = parsed.get(cs.KEY_PATH)
        source_code = (
            None
            if start_line is None or end_line is None or file_path is None
            else self._extract_source_code(
                qualified_name, file_path, start_line, end_line
            )
        )
        if not source_code:
            logger.debug(ls.NO_SOURCE_FOR, name=qualified_name)
            return None
        return parsed[cs.KEY_NODE_ID], _embedding_symbol(parsed), source_code

    def _embeddings_off_reason(self) -> str | None:
        """Why this run is set up to generate no embeddings, or None."""
        if self._skip_embeddings:
            return ls.EMBEDDINGS_SKIPPED
        if not has_semantic_dependencies():
            return ls.SEMANTIC_NOT_AVAILABLE
        return None

    def embeddings_owed(self, state_dir: Path) -> bool:
        """Whether an earlier pass left a debt that this run can pay."""
        if not self._owes_embeddings(state_dir):
            return False
        # Only a run that embeds can pay what an earlier one owes, so a run
        # without embeddings keeps the fast path and leaves the marker for
        # the next run that has them.
        return self._embeddings_off_reason() is None and self._graph is not None

    def _owes_embeddings(self, state_dir: Path) -> bool:
        if embeddings_marker(state_dir, self._project_name).exists():
            return True
        if not stamp_names_selected_store(
            embeddings_store_stamp(state_dir, self._project_name)
        ):
            return True
        shared = load_shared_embeddings_owed(state_dir / cs.EMBEDDINGS_PENDING_FILENAME)
        return shared is None or self._project_name in shared

    def _record_embeddings_pending(self, state_dir: Path, pending: bool) -> None:
        """Owe the embeddings pass to the next sync, or settle what was owed.

        The run publishes its hash cache whatever the pass did, so after a
        pass that left a symbol without its point, or a stale point behind,
        an unchanged checkout took the in-sync fast path, which never
        embeds: an edited function whose new point the store refused stayed
        unsearchable until another file changed (PR #2550). The
        marker makes that fast path refuse. Each pass covers every function
        of the project, so one that stores, verifies and prunes them all
        settles whatever an earlier run of it owed, and only that: one
        checkout can be indexed under several projects, each with a marker
        of its own (PR #3358).
        """
        if pending:
            logger.warning(ls.EMBEDDINGS_PENDING.format(project=self._project_name))
        else:
            save_embeddings_store(embeddings_store_stamp(state_dir, self._project_name))
        recorded = save_embeddings_owed(
            embeddings_marker(state_dir, self._project_name),
            self._project_name,
            pending,
        )
        self._split_shared_embeddings_marker(state_dir, recorded)

    def _split_shared_embeddings_marker(self, state_dir: Path, recorded: bool) -> None:
        """Hand each project a shared marker names a marker of its own.

        Only created, never removed, for the others: a project that syncs
        at the same time and settles its debt in between owes one more pass
        at worst. This run's own debt is what its pass decided, `recorded`
        whether its marker took that. The shared marker goes only once every
        debt it holds is in a file of its own: a write that failed (a full
        disk refuses a new file but not a removal) leaves it for the next
        sync to split again (PR #3358).
        """
        shared = state_dir / cs.EMBEDDINGS_PENDING_FILENAME
        owed = load_shared_embeddings_owed(shared)
        if owed == frozenset():
            return
        if owed is None:
            # Naming no one, it is every project's debt: each one indexed
            # from this tree still retries once.
            owed = frozenset(
                load_project_stamps(state_dir / cs.EXCLUSION_STATE_FILENAME)
            )
        failed = [
            project
            for project in sorted(owed - {self._project_name})
            if not save_embeddings_owed(
                embeddings_marker(state_dir, project), project, True
            )
        ]
        if failed or not recorded:
            return
        try:
            shared.unlink(missing_ok=True)
        except OSError:
            logger.debug(ls.EMBEDDINGS_PENDING_NOT_UPDATED)

    def generate_semantic_embeddings(self, run: EmbeddingRun, state_dir: Path) -> None:
        """Embed the project's functions and methods, pruning stale vectors,
        and owe the pass to the next sync in `state_dir` unless it settled."""
        if (off_reason := self._embeddings_off_reason()) is not None:
            logger.info(off_reason)
            return

        graph = self._graph
        if graph is None:
            logger.info(ls.INGESTOR_NO_QUERY)
            return

        try:
            from .embedder import embed_code_batch, get_embedding_cache
            from .store import (
                close_qdrant_client,
                delete_stale_embeddings,
                store_embedding_batch,
                verify_stored_ids,
            )

            logger.info(ls.PASS_5_EMBEDDINGS)

            # The query is prefix-scoped, and `svc.` also selects a nested
            # `svc.v2` project's functions, which that project embeds itself.
            nested = self._nested_project_names()
            results = [
                row
                for row in graph.fetch_all(
                    cs.CYPHER_QUERY_EMBEDDINGS, {"project_name": self._project_name}
                )
                if not _under_any_project(row.get(cs.KEY_QUALIFIED_NAME), nested)
            ]
            current = self._current_symbols(results)
            stored_ids: set[int] = set()

            if not results:
                logger.info(ls.NO_FUNCTIONS_FOR_EMBEDDING)
                # The project's last function may just have gone; its point
                # must go with it.
                pruned = self._delete_stale_embeddings(
                    run, current, stored_ids, delete_stale_embeddings
                )
                self._record_embeddings_pending(state_dir, not pruned)
                close_qdrant_client()
                return

            logger.info(ls.GENERATING_EMBEDDINGS, count=len(results))

            embedded_count = 0
            expected: dict[int, EmbeddingSymbol] = {}
            pending: list[tuple[int, EmbeddingSymbol, str]] = []
            flush_at = settings.QDRANT_BATCH_SIZE
            project_name = self._project_name

            def store(points: list[EmbeddingPoint]) -> int:
                return store_embedding_batch(project_name, points)

            def flush() -> int:
                return _flush_embedding_batch(
                    pending, expected, stored_ids, embed_code_batch, store
                )

            for row in results:
                candidate = self._embedding_candidate(row)
                if candidate is None:
                    continue
                pending.append(candidate)
                if len(pending) >= flush_at:
                    embedded_count += flush()
                    _log_embedding_progress(embedded_count, len(results))

            embedded_count += flush()

            logger.info(ls.EMBEDDINGS_COMPLETE, count=embedded_count)

            # Before the check, so it reads the store as the sync leaves it.
            # The prune still drops the point of a symbol whose replacement
            # was refused: search answers with the node a point names, and
            # this one names the node the re-parse deleted. The symbol is
            # owed to the next sync instead.
            pruned = self._delete_stale_embeddings(
                run, current, stored_ids, delete_stale_embeddings
            )
            verified = self._reconcile_embeddings(expected, verify_stored_ids)
            self._record_embeddings_pending(state_dir, not (pruned and verified))

            get_embedding_cache().save()
            close_qdrant_client()

        except KeyboardInterrupt:
            self._stop_interrupted_embeddings(run, state_dir)
        except Exception as e:  # noqa: BLE001 -- any vector backend's error; the pass is retried
            logger.warning(ls.EMBEDDING_GENERATION_FAILED, error=e)
            self._record_embeddings_pending(state_dir, True)

    def _current_symbols(self, rows: list[ResultRow]) -> dict[int, EmbeddingSymbol]:
        """Every function and method the graph holds for the project now,
        by node id, whether or not its source can be read to embed it."""
        current: dict[int, EmbeddingSymbol] = {}
        for row in rows:
            parsed = self._parse_embedding_result(row)
            if parsed is not None:
                current[parsed[cs.KEY_NODE_ID]] = _embedding_symbol(parsed)
        return current

    def _delete_stale_embeddings(
        self,
        run: EmbeddingRun,
        current: Mapping[int, EmbeddingSymbol],
        stored_ids: Collection[int],
        delete_fn: Callable[
            [str, Mapping[int, EmbeddingSymbol], Collection[int], Sequence[str]], int
        ],
    ) -> bool:
        """Drop the project's vectors of symbols `current` no longer holds,
        and say whether the delete ran.

        A re-parse re-creates a file's nodes, and a deleted function leaves
        nothing to overwrite its vector, so without this the store kept a
        vector per edit and every deleted function's (issue #2447). A vector
        keyed the old way goes only once `stored_ids` says its replacement is
        in, and a nested project's vectors are its own sync's business.
        With the registry unread a nested project's vectors pass as this
        one's, so nothing goes until a sync that reads it.
        """
        nested = self._nested_project_names()
        if run.registry_unread:
            logger.warning(
                ls.EMBEDDING_STALE_SKIPPED.format(project=self._project_name)
            )
            return False
        try:
            removed = delete_fn(self._project_name, current, stored_ids, nested)
        except Exception as e:  # noqa: BLE001 -- any vector backend's error; the pass is retried
            logger.warning(
                ls.EMBEDDING_STALE_FAILED.format(project=self._project_name, error=e)
            )
            return False
        if removed:
            logger.info(
                ls.EMBEDDING_STALE_REMOVED.format(
                    count=removed, project=self._project_name
                )
            )
        return True

    def _stop_interrupted_embeddings(self, run: EmbeddingRun, state_dir: Path) -> None:
        """Hold a Ctrl+C in the embeddings pass until `GraphUpdater.run` has
        committed.

        The graph writes are flushed before this pass, so letting the
        interrupt escape here would only skip `_commit_run_state`: with no
        hash cache the next sync re-parses the whole repository, and after
        `--clean` there is not even the old cache to fall back on. The run
        publishes its hash cache, so the pass is owed before that: otherwise
        an unchanged checkout took the in-sync fast path and the functions
        this pass had not reached stayed out of search (review of PR
        #3358). The next sync embeds every function again, and saving the
        embedding cache lets it reuse the vectors this pass already computed.
        """
        from .embedder import get_embedding_cache
        from .store import close_qdrant_client

        logger.warning(ls.EMBEDDINGS_INTERRUPTED)
        self._record_embeddings_pending(state_dir, True)
        get_embedding_cache().save()
        close_qdrant_client()
        run.embeddings_interrupted = True

    def _reconcile_embeddings(
        self,
        expected: Mapping[int, EmbeddingSymbol],
        verify_fn: Callable[[str, Mapping[int, EmbeddingSymbol]], set[int]],
    ) -> bool:
        """Check the store holds a point for each node in `expected`, and say
        whether it does; a check that failed proves nothing."""
        if not expected:
            return True
        try:
            stored_ids = verify_fn(self._project_name, expected)
        except Exception as e:  # noqa: BLE001 -- any vector backend's error; the pass is retried
            logger.warning(ls.EMBEDDING_RECONCILE_FAILED.format(error=e))
            return False
        missing = set(expected) - stored_ids
        if missing:
            sample = sorted(missing)[:10]
            logger.warning(
                ls.EMBEDDING_RECONCILE_MISSING.format(
                    missing=len(missing),
                    expected=len(expected),
                    sample_ids=sample,
                )
            )
            return False
        logger.info(ls.EMBEDDING_RECONCILE_OK.format(count=len(expected)))
        return True

    def _extract_source_code(
        self, qualified_name: str, file_path: str, start_line: int, end_line: int
    ) -> str | None:
        if not file_path or not start_line or not end_line:
            return None

        file_path_obj = self._repo_path / file_path

        ast_extractor = None
        if file_path_obj in self._ast_cache:
            root_node, language = self._ast_cache[file_path_obj]
            fqn_config = LANGUAGE_FQN_SPECS.get(language)

            if fqn_config:

                def ast_extractor_func(qname: str, path: Path) -> str | None:
                    return find_function_source_by_fqn(
                        root_node,
                        qname,
                        path,
                        self._repo_path,
                        self._project_name,
                        fqn_config,
                    )

                ast_extractor = ast_extractor_func

        return extract_source_with_fallback(
            file_path_obj, start_line, end_line, qualified_name, ast_extractor
        )

    def _parse_embedding_result(self, row: ResultRow) -> EmbeddingQueryResult | None:
        node_id = row.get(cs.KEY_NODE_ID)
        qualified_name = row.get(cs.KEY_QUALIFIED_NAME)
        # Part of the symbol's identity: a Function and a Method may share a
        # qualified name, so a row without its label cannot be keyed.
        label = _embedded_label(row.get(cs.KEY_LABEL))

        if (
            not isinstance(node_id, int)
            or not isinstance(qualified_name, str)
            or label is None
        ):
            return None

        start_line = row.get(cs.KEY_START_LINE)
        end_line = row.get(cs.KEY_END_LINE)
        file_path = row.get(cs.KEY_PATH)

        return EmbeddingQueryResult(
            node_id=node_id,
            qualified_name=qualified_name,
            label=label,
            start_line=start_line if isinstance(start_line, int) else None,
            end_line=end_line if isinstance(end_line, int) else None,
            path=file_path if isinstance(file_path, str) else None,
        )
