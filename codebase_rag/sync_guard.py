"""What a CLI writer of a project holds while it writes (issues #2411, #2219).

`cgr start` and `cgr watch` both write a checkout into a project of the
shared graph. Before the first write each claims the project name for its
repository, so a name another repository indexes is not silently replaced
(#2411), and each run of writes goes between the CLI sync's `:IncompleteRun`
marker going down and coming off, so one that stops part-way leaves a graph
that says it is partial (#2219). The CLI turns a refusal into its own message
and exit; the watcher runs these on its own connection for as long as it
watches (review of PR 3365). Neither imports the other's module, so these
live here.
"""

import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from . import constants as cs
from . import cypher_queries as cq
from . import logs as ls
from .services import QueryProtocol
from .types_defs import PropertyValue, ResultRow
from .utils.process_owner import (
    pid_has_exited,
    process_host,
    process_table_identified,
)


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
    the first one's graph (review of PR 2499).
    """
    if not repo.is_dir():
        return OwnerCheck(None)
    root = str(repo.resolve())
    try:
        rows = ingestor.fetch_all(
            cq.CYPHER_CLAIM_PROJECT_ROOT,
            {cs.KEY_PROJECT_NAME: project_name, cs.KEY_ROOT_PATH: root},
        )
    except Exception as exc:
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
    """Give back a claim whose writer failed before writing (review of PR 2499).

    The release query keeps it once that writer's marker or any code is
    down. Best effort: the writer's own failure is what is being reported.
    """
    params: dict[str, PropertyValue] = {
        cs.KEY_PROJECT_NAME: project_name,
        cs.KEY_ROOT_PATH: str(repo.resolve()),
    }
    try:
        ingestor.execute_write(cq.CYPHER_RELEASE_PROJECT_CLAIM, params)
    except Exception as exc:
        logger.warning(
            ls.PROJECT_CLAIM_RELEASE_FAILED.format(project_name=project_name, error=exc)
        )


@contextmanager
def project_claim(
    ingestor: QueryProtocol, project_name: str, repo: Path, *, assume_yes: bool
) -> Generator[list[str] | None]:
    """Claim the project on `ingestor` for as long as the writer runs (#2411).

    Raises `SyncRefusedError`, before anything is written, when the name is
    another repository's or its owner cannot be read, unless `assume_yes`.
    Yields the roots `mark_sync_incomplete` must still find on the Project,
    so every later write is refused once the claim is lost. A claim this
    writer created is given back when it fails, as `cgr start` gives back
    its own (review of PR 2499).
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
def sync_owner() -> Generator[dict[str, PropertyValue]]:
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
    check saw may have been released and taken since (review of PR 2499).
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
    except Exception as exc:
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
    except Exception as exc:
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
    warning is its own to lift (review of PR 3365).
    """
    params = sync_marker_params(project_name)
    params[cs.KEY_OWNER_TOKEN] = owner[cs.KEY_OWNER_TOKEN]
    try:
        ingestor.execute_write(cq.CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER, params)
    except Exception as exc:
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


def partial_graph_refusal(ingestor: QueryProtocol, project_name: str) -> str | None:
    """Why a CLI edit may not plan from the project's graph, or None if it may.

    Any run's marker counts, the MCP server's as well as a CLI writer's. One
    whose run reached its first write may sit over a graph that lost
    definitions and calls. One that stopped before writing (`writing=false`)
    left the graph as it was, so it does not refuse; the MCP server clears it.
    A store that cannot answer refuses, as the MCP server's
    `_persisted_incomplete` does: "cannot tell" is not "finished".
    """
    try:
        rows = ingestor.fetch_all(
            cq.CYPHER_PROJECT_IS_INCOMPLETE, {cs.KEY_PROJECT_NAME: project_name}
        )
    except Exception as exc:
        return cs.CLI_ERR_EDIT_MARKER_UNREADABLE.format(project=project_name, error=exc)
    if any(
        row.get(cs.KEY_RUN_INCOMPLETE) and row.get(cs.KEY_WRITING, True) for row in rows
    ):
        return cs.CLI_ERR_EDIT_GRAPH_PARTIAL.format(project=project_name)
    return None


class StrandedRun(NamedTuple):
    """A run whose marker a full rebuild retires, and the exited process it names."""

    run_id: str
    owner_host: str
    owner_pid: int


def run_owner() -> dict[str, PropertyValue]:
    """The process an MCP or `cgr check` run's marker names (review of PR #3496)."""
    return {cs.KEY_OWNER_HOST: process_host(), cs.KEY_OWNER_PID: os.getpid()}


def _stopped_run(row: ResultRow) -> StrandedRun | None:
    """The run a marker names if its process provably exited.

    Only a process in this process table can be checked, and only where the
    host's name tells this table from every other (`process_table_identified`):
    a hostname alone may be another machine's, whose pid is no process here.
    A marker from another host, from a live process (this one included), or
    from before markers named their process is kept: it may guard a graph its
    run is still writing, as `cli._stopped_sync_token` reasons.
    """
    run_id = row.get(cs.KEY_RUN_ID)
    pid = row.get(cs.KEY_OWNER_PID)
    here = process_host()
    if (
        not isinstance(run_id, str)
        or not process_table_identified()
        or row.get(cs.KEY_OWNER_HOST) != here
        or not isinstance(pid, int)
        or pid == os.getpid()
        or not pid_has_exited(pid)
    ):
        return None
    return StrandedRun(run_id, here, pid)


def stranded_runs(
    ingestor: QueryProtocol, project_name: str, own_run_id: str
) -> list[StrandedRun]:
    """The other runs whose markers a full rebuild of the project supersedes.

    Asked before the rebuild marks. A run counts only once its process has
    exited, whichever checkout it wrote from: the checkout's sync lock keeps
    out only the writers of this checkout under this CGR_HOME, and a run of
    the same project elsewhere may still be writing (review of PR #3496). The
    rebuild must then be forced: a stopped re-ingest may have deleted an
    unchanged dependent's definitions, which an incremental run skips by its
    saved hash, as `cgr watch`'s `_marked_run` says. A store that cannot
    answer yields none, so the rebuild runs as before and those markers stay
    for the next one.
    """
    try:
        rows = ingestor.fetch_all(
            cq.CYPHER_STRANDED_RUN_IDS,
            {cs.KEY_PROJECT_NAME: project_name, cs.KEY_RUN_ID: own_run_id},
        )
    except Exception as exc:
        logger.warning(
            ls.STRANDED_RUNS_UNREADABLE.format(project=project_name, error=exc)
        )
        return []
    return [run for row in rows if (run := _stopped_run(row)) is not None]


def retire_stranded_runs(
    ingestor: QueryProtocol, project_name: str, runs: list[StrandedRun]
) -> None:
    """Take off the markers of `runs` once a forced rebuild committed.

    Never fails the rebuild, which is complete by now: a marker that stays
    only refuses until the next full sync retires it.
    """
    if not runs:
        return
    try:
        for run in runs:
            ingestor.execute_write(
                cq.CYPHER_RETIRE_STRANDED_RUN,
                {
                    cs.KEY_PROJECT_NAME: project_name,
                    cs.KEY_RUN_ID: run.run_id,
                    cs.KEY_OWNER_HOST: run.owner_host,
                    cs.KEY_OWNER_PID: run.owner_pid,
                },
            )
    except Exception as exc:
        logger.warning(
            ls.STRANDED_RUNS_NOT_RETIRED.format(project=project_name, error=exc)
        )
        return
    logger.info(ls.STRANDED_RUNS_RETIRED.format(project=project_name, count=len(runs)))
