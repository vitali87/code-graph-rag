"""What a CLI writer of a project holds while it writes (issues #2411, #2219).

`cgr start` and `cgr watch` both write a checkout into a project of the
shared graph. Before the first write each claims the project name for its
repository, so a name another repository indexes is not silently replaced
(#2411), and each run of writes goes between the CLI sync's `:IncompleteRun`
marker going down and coming off, so one that stops part-way leaves a graph
that says it is partial (#2219). The CLI turns a refusal into its own message
and exit; the watcher runs these on its own connection for as long as it
watches (PR #3365). Neither imports the other's module, so these
live here.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger

from . import constants as cs
from . import cypher_queries as cq
from . import logs as ls
from .services import QueryProtocol
from .types_defs import PropertyValue
from .utils.process_owner import process_host

if TYPE_CHECKING:
    from .graph_updater import GraphUpdater
    from .types_defs import ReingestReport


class SyncRefusedError(RuntimeError):
    """A sync stopped before its scan wrote anything; the message names the fix.

    Raised when the project is another repository's, when its owner cannot
    be read, when the incomplete-run marker cannot go down, or, for `cgr
    watch`, when the graph's constraints cannot be ensured under it.
    """


class OwnerCheck(NamedTuple):
    refusal: str | None
    created: bool = False
    # The roots the sync's marker write must still find on the Project, this
    # repository's first; None when there is nothing to hold it to (no
    # recorded root, or --yes taking the project over).
    owned_roots: list[str] | None = None


def project_owner_refusal(
    ingestor: QueryProtocol, project_name: str, repo: Path, assume_yes: bool
) -> OwnerCheck:
    """Why a sync must not replace what another repository indexed (#2411),
    and what its claim holds.

    A derived name carries a hash of its path, so only a chosen one can
    collide; the sync would delete the other repository's modules as stale
    and leave that repository's hash cache describing code no longer there.
    Refused rather than asked: the chat's own sync runs behind a status bar.

    The check claims the name for this repository in the same statement
    that reads its owner. A separate read let two syncs of a name nobody
    held both see no owner and both go ahead, and the later one replaced
    the first one's graph (PR #2499).
    """
    if not repo.is_dir():
        return OwnerCheck(None)
    root = str(repo.resolve())
    try:
        rows = ingestor.fetch_all(
            cq.CYPHER_CLAIM_PROJECT_ROOT,
            {cs.KEY_PROJECT_NAME: project_name, cs.KEY_ROOT_PATH: root},
        )
    except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
        logger.warning(ls.MG_PROJECT_ROOT_READ_FAILED.format(error=exc))
        if assume_yes:
            return OwnerCheck(None)
        return OwnerCheck(
            cs.CLI_ERR_PROJECT_OWNER_UNREADABLE.format(
                project_name=project_name, error=exc
            )
        )
    owner = rows[0].get(cs.KEY_ROOT_PATH) if rows else None
    if not isinstance(owner, str) or not owner:
        return OwnerCheck(None)
    if Path(owner).resolve() == repo.resolve():
        # --yes would replace whoever holds it by then, so nothing to hold.
        held = None if assume_yes else list(dict.fromkeys((root, owner)))
        return OwnerCheck(None, rows[0].get(cs.KEY_CREATED) is True, held)
    if assume_yes:
        logger.warning(
            ls.PROJECT_OWNER_REPLACED.format(project_name=project_name, root=owner)
        )
        return OwnerCheck(None)
    return OwnerCheck(
        cs.CLI_ERR_PROJECT_OWNED_ELSEWHERE.format(project_name=project_name, root=owner)
    )


def release_project_claim(
    ingestor: QueryProtocol, project_name: str, repo: Path
) -> None:
    """Give back a claim whose writer failed before writing (PR #2499).

    The release query keeps it once that writer's marker or any code is
    down. Best effort: the writer's own failure is what is being reported.
    """
    params: dict[str, PropertyValue] = {
        cs.KEY_PROJECT_NAME: project_name,
        cs.KEY_ROOT_PATH: str(repo.resolve()),
    }
    try:
        ingestor.execute_write(cq.CYPHER_RELEASE_PROJECT_CLAIM, params)
    except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the write is retried later
        logger.warning(
            ls.PROJECT_CLAIM_RELEASE_FAILED.format(project_name=project_name, error=exc)
        )


@contextmanager
def project_claim(
    ingestor: QueryProtocol, project_name: str, repo: Path, *, assume_yes: bool
) -> Iterator[list[str] | None]:
    """Claim the project on `ingestor` for as long as the writer runs (#2411).

    Raises `SyncRefusedError`, before anything is written, when the name is
    another repository's or its owner cannot be read, unless `assume_yes`.
    Yields the roots `mark_sync_incomplete` must still find on the Project,
    so every later write is refused once the claim is lost. A claim this
    writer created is given back when it fails, as `cgr start` gives back
    its own (PR #2499).
    """
    check = project_owner_refusal(ingestor, project_name, repo, assume_yes)
    if check.refusal is not None:
        raise SyncRefusedError(check.refusal)
    try:
        yield check.owned_roots
    except BaseException:
        if check.created:
            release_project_claim(ingestor, project_name, repo)
        raise


def sync_marker_params(project_name: str) -> dict[str, PropertyValue]:
    return {cs.KEY_PROJECT_NAME: project_name, cs.KEY_RUN_ID: cs.CLI_SYNC_RUN_ID}


# The CLI syncs THIS process is running, by the owner token on their marker.
# A delete in the same process sees its own pid alive whether or not the sync
# that wrote a marker has returned, so the pid cannot answer for these.
RUNNING_SYNC_TOKENS: set[str] = set()


@contextmanager
def sync_owner() -> Iterator[dict[str, PropertyValue]]:
    """Who this sync is, for its marker; registered as running while held."""
    token = uuid.uuid4().hex
    RUNNING_SYNC_TOKENS.add(token)
    try:
        yield {
            cs.KEY_OWNER_HOST: process_host(),
            cs.KEY_OWNER_PID: os.getpid(),
            cs.KEY_OWNER_TOKEN: token,
        }
    finally:
        RUNNING_SYNC_TOKENS.discard(token)


def mark_sync_incomplete(
    ingestor: QueryProtocol,
    project_name: str,
    owner: dict[str, PropertyValue],
    owned_roots: list[str] | None = None,
) -> None:
    """Put the `:IncompleteRun` marker down before the sync writes (#2219).

    The same marker the MCP mutating paths write (#1679), so a later MCP
    process refuses to hydrate from a graph an interrupted CLI sync left
    partial. Raises `SyncRefusedError` when it cannot go down: nothing has
    changed yet, and proceeding would make exactly that partial graph look
    complete. It names its `owner`, so a delete can tell whether this sync
    still runs.

    With `owned_roots`, the marker goes down only while the Project still
    has one of them, checked in the same statement: the claim the ownership
    check saw may have been released and taken since (PR #2499).
    """
    params = sync_marker_params(project_name) | owner
    params[cs.KEY_WRITING] = True
    try:
        if owned_roots is None:
            ingestor.execute_write(cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE, params)
            return
        params[cs.KEY_ROOT_PATH] = owned_roots[0]
        params[cs.KEY_ROOT_PATHS] = owned_roots
        marked = ingestor.fetch_all(cq.CYPHER_MARK_CLAIMED_PROJECT_INCOMPLETE, params)
    except Exception as exc:
        raise SyncRefusedError(
            cs.CLI_ERR_SYNC_MARKER_FAILED.format(project=project_name)
        ) from exc
    if not marked:
        raise SyncRefusedError(claim_lost_refusal(ingestor, project_name))


def claim_lost_refusal(ingestor: QueryProtocol, project_name: str) -> str:
    try:
        rows = ingestor.fetch_all(
            cq.CYPHER_PROJECT_ROOT_PATH, {cs.KEY_PROJECT_NAME: project_name}
        )
    except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
        return cs.CLI_ERR_PROJECT_OWNER_UNREADABLE.format(
            project_name=project_name, error=exc
        )
    owner = rows[0].get(cs.KEY_ROOT_PATH) if rows else None
    return cs.CLI_ERR_PROJECT_OWNED_ELSEWHERE.format(
        project_name=project_name, root=owner
    )


def clear_sync_incomplete(ingestor: QueryProtocol, project_name: str) -> None:
    """Take the marker off once the sync is recorded; never fails the sync.

    The graph is whole by now, so a failed clear only leaves a stale marker
    for the next sync of the project to clear (see `CLI_SYNC_RUN_ID`).
    """
    try:
        ingestor.execute_write(
            cq.CYPHER_CLEAR_PROJECT_INCOMPLETE, sync_marker_params(project_name)
        )
    except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the write is retried later
        logger.warning(
            ls.CLI_SYNC_MARKER_NOT_CLEARED.format(project=project_name, error=exc)
        )


def clear_own_sync_incomplete(
    ingestor: QueryProtocol, project_name: str, owner: dict[str, PropertyValue]
) -> None:
    """Take off the marker `owner` put down, and no other; never fails the write.

    The CLI sync's marker is one node per project, re-marked by whichever
    writer marks next. A writer the checkout lock does not hold back (a sync
    of another checkout under the same name) may have marked since, and its
    warning is its own to lift (PR #3365).
    """
    params = sync_marker_params(project_name)
    params[cs.KEY_OWNER_TOKEN] = owner[cs.KEY_OWNER_TOKEN]
    try:
        ingestor.execute_write(cq.CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER, params)
    except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the write is retried later
        logger.warning(
            ls.CLI_SYNC_MARKER_NOT_CLEARED.format(project=project_name, error=exc)
        )


def sync_marker_down(ingestor: QueryProtocol, project_name: str) -> bool:
    """Whether a CLI sync's marker is down on the project: one did not finish."""
    return bool(
        ingestor.fetch_all(
            cq.CYPHER_CLI_SYNC_MARKER_OWNER, sync_marker_params(project_name)
        )
    )


class UnfinishedSyncError(RuntimeError):
    """A CLI sync of the project stopped part-way, so its graph may be partial."""


class WriteMarker:
    """`cgr start`'s `:IncompleteRun` marker around each CLI write (#2219).

    Down before the initial scan, a full re-index or a re-ingest's first
    write, and off once that write succeeded, so a writer stopped part-way
    leaves a graph `cgr status` and the MCP server know to be partial. The
    watcher, the chat's graph refresh, `cgr rename` and `cgr edit undo` all
    write through it. It is the CLI sync's own marker, so `cgr start` clears
    what these left; a writer takes off only a marker it put down itself, so
    one another writer put down since stays (PR #3365). With
    `owned_roots`, every mark holds to the claim `cgr watch` took on its
    project (#2411): once another repository has the name, the write is
    refused instead of replacing that repository's graph.
    """

    def __init__(
        self,
        ingestor: QueryProtocol,
        project_name: str,
        owner: dict[str, PropertyValue],
        owned_roots: list[str] | None,
    ) -> None:
        self._ingestor = ingestor
        self._project_name = project_name
        self._owner = owner
        self._owned_roots = owned_roots

    def mark(self) -> None:
        mark_sync_incomplete(
            self._ingestor, self._project_name, self._owner, self._owned_roots
        )

    def unfinished(self) -> bool:
        """Whether a CLI sync's marker is already down: one did not finish.

        Asked with the checkout lock held, so no writer of this checkout can
        still be running behind it.
        """
        return sync_marker_down(self._ingestor, self._project_name)

    def mark_scoped(self) -> None:
        """Mark before a re-ingest writes, unless a sync left the graph partial.

        A re-ingest resolves its files against the rest of the graph, and
        clearing the shared marker after it would call whole a graph an
        unfinished `cgr start` left partial. The MCP `reingest` refuses there
        for the same reason (#1705); the watcher re-indexes instead.
        """
        if self.unfinished():
            raise UnfinishedSyncError(self._project_name)
        self.mark()

    def clear(self) -> None:
        clear_own_sync_incomplete(self._ingestor, self._project_name, self._owner)


def marked_run(
    updater: GraphUpdater,
    marker: WriteMarker | None,
    force: bool = False,
    before_run: Callable[[], None] | None = None,
) -> None:
    """Run the updater over the whole checkout between `marker`'s mark and clear.

    The caller holds the checkout's sync lock, so the marker goes down after
    a sync that held it first has taken its own off: they share one. It
    comes off once the run committed, as `cgr start` takes its own off: a
    Ctrl+C landing after the commit found the graph whole. Any other failure
    leaves it down. `before_run` runs between the mark and the run, where
    `cgr start` ensures the graph's constraints.

    A marker already down is an earlier writer's that did not finish: a
    watcher stopped part-way through a re-ingest may have deleted an
    unchanged dependent's definitions, which an incremental run skips by
    their saved hash. So the run is forced, and only that rebuild takes the
    marker off (PR #3365).
    """
    if marker is None:
        if before_run is not None:
            before_run()
        updater.run(force=force)
        return
    if not force and marker.unfinished():
        logger.warning(
            ls.WATCHER_FULL_RUN_OVER_UNFINISHED_SYNC.format(
                project=updater.project_name
            )
        )
        force = True
    marker.mark()
    try:
        if before_run is not None:
            before_run()
        updater.run(force=force)
    except KeyboardInterrupt:
        if updater.committed:
            marker.clear()
        raise
    marker.clear()


def marked_reingest(
    updater: GraphUpdater, marker: WriteMarker
) -> Callable[[list[str]], ReingestReport]:
    """`updater.reingest` with `marker` down from its first write until it
    returns, for a CLI writer outside the watcher.

    `cgr rename`, `cgr edit undo` and the chat's graph refresh re-ingested
    with no marker, so a process that died part-way left a partial graph
    that `cgr status` and the MCP server took for whole. A re-ingest over a
    graph an unfinished sync left partial is refused before it writes, as
    the watcher's is: `ReingestAborted`, caused by `UnfinishedSyncError`.
    """

    def reingest(paths: list[str]) -> ReingestReport:
        report = updater.reingest(paths, before_write=marker.mark_scoped)
        marker.clear()
        return report

    return reingest
