"""Keep the chat session's graph in step with what its agent writes (issue #2916).

The MCP edit tools and the watcher re-ingest every file they change; the
`cgr start` agent's `replace_code` and `create_file` did not, so after its
first approved write the rest of the session queried, and planned against,
code that no longer existed.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import cypher_queries as cq
from .. import logs as ls
from ..graph_updater import GraphUpdater, ReingestAborted
from ..parser_loader import load_parsers
from ..services import IngestorProtocol, QueryProtocol


class GraphRefresher:
    """The session's scoped re-ingest of each file its agent writes.

    One updater for the session, as the watcher holds one, built on the first
    write; its first re-ingest hydrates the definitions from the graph. A
    failure never fails the write: it is reported in the tool's result, and a
    re-ingest that may have left the graph partial is followed by a full sync
    before the next one, as the watcher recovers (issue #1681).
    """

    __slots__ = (
        "_ingestor",
        "_repo_path",
        "_project_name",
        "_project_named",
        "_updater",
        "_needs_full_sync",
        "_lock",
    )

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        project_named: bool = False,
    ) -> None:
        self._ingestor = ingestor
        self._repo_path = repo_path
        self._project_name = project_name
        self._project_named = project_named
        self._updater: GraphUpdater | None = None
        self._needs_full_sync = False
        self._lock = threading.Lock()

    async def after_write(self, paths: list[str]) -> str:
        return await asyncio.to_thread(self.refresh, paths)

    def refresh(self, paths: list[str]) -> str:
        with self._lock:
            try:
                if not self._indexed():
                    # Nothing to keep in step: the first sync indexes it.
                    return ""
                updater = self._session_updater()
                if self._needs_full_sync:
                    # force: the files a failed re-ingest left without their
                    # nodes are unchanged on disk, so an incremental run
                    # would skip exactly them (as `_rebuild_after_failure`).
                    updater.run(force=True)
                    self._needs_full_sync = False
                report = updater.reingest(paths)
            except (ValueError, ReingestAborted) as exc:
                # Refused, or aborted while still reading: nothing written.
                return cs.MSG_CHAT_GRAPH_NOT_UPDATED.format(error=exc)
            except Exception as exc:  # noqa: BLE001 - the write itself succeeded
                logger.error(ls.CHAT_REINGEST_FAILED.format(error=exc))
                self._needs_full_sync = True
                return cs.MSG_CHAT_GRAPH_NOT_UPDATED.format(error=exc)
        return cs.MSG_CHAT_GRAPH_UPDATED.format(
            files=len(report.reparsed) + len(report.removed),
            dependents=len(report.affected),
        )

    def _indexed(self) -> bool:
        # A sink that cannot be read (an export target) cannot say, so the
        # re-ingest decides, as the updater does for itself.
        if not isinstance(self._ingestor, QueryProtocol):
            return True
        rows = self._ingestor.fetch_all(cq.CYPHER_LIST_PROJECTS)
        return any(row.get(cs.KEY_NAME) == self._project_name for row in rows)

    def _session_updater(self) -> GraphUpdater:
        if self._updater is None:
            parsers, queries = load_parsers()
            self._updater = GraphUpdater(
                ingestor=self._ingestor,
                repo_path=self._repo_path,
                parsers=parsers,
                queries=queries,
                project_name=self._project_name,
                project_named=self._project_named,
            )
        return self._updater
