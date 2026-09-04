"""`cgr check`: the structural delta of the working tree against a git ref.

The graph is assumed to reflect `--base` (index at the base, then edit);
the files that differ between the base and the working tree are re-ingested
and the delta reported the same way the MCP write tools report it after
each write (issue #1525). Exit status 1 with `--fail-on-found` makes it a
CI or pre-commit gate.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from tree_sitter import Parser

from . import constants as cs
from .graph_updater import GraphUpdater
from .structural_delta import StructuralDelta, normalise_paths, observe
from .types_defs import LanguageQueries

_GIT_DELETED = "D"


_CGR_STATE_PREFIX = ".cgr-"


class CheckError(ValueError):
    """The working tree cannot be compared against the requested base."""


def changed_since(repo_root: Path, base: str) -> tuple[list[str], list[str]]:
    """Files that differ from `base`: (changed or added, deleted).

    Untracked files count as added; a rename shows as one deletion and one
    addition so the delta reports the old symbols as removed or renamed.
    """
    try:
        status = subprocess.run(
            # `--relative`: paths relative to `repo_root`, which may sit below
            # the git toplevel; `ls-files --others` is already cwd-relative.
            [
                cs.SHELL_CMD_GIT,
                "diff",
                "--name-status",
                "--no-renames",
                "--relative",
                base,
                "--",
            ],
            cwd=repo_root,
            capture_output=True,
            encoding=cs.ENCODING_UTF8,
            check=True,
        ).stdout
        untracked = subprocess.run(
            [cs.SHELL_CMD_GIT, "ls-files", "--others", "--exclude-standard"],
            cwd=repo_root,
            capture_output=True,
            encoding=cs.ENCODING_UTF8,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise CheckError(
            cs.CHECK_GIT_FAILED.format(base=base, error=detail.strip())
        ) from error
    changed: set[str] = set()
    deleted: set[str] = set()
    for line in status.splitlines():
        code, _sep, path = line.partition("\t")
        if not path:
            continue
        (deleted if code.startswith(_GIT_DELETED) else changed).add(path)
    # cgr's own untracked state files (hash cache, directory mtimes, ...)
    # are not source and must not be re-ingested or reported as reparsed.
    changed.update(
        line
        for line in untracked.splitlines()
        if line and not Path(line).name.startswith(_CGR_STATE_PREFIX)
    )
    return sorted(changed), sorted(deleted)


def run_check(
    repo_root: Path,
    base: str,
    project_name: str,
    ingestor: object,
    parsers: Mapping[cs.SupportedLanguage, Parser],
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
) -> StructuralDelta:
    """Re-ingest what changed since `base` and return the structural delta."""
    changed, deleted = changed_since(repo_root, base)
    changed = normalise_paths(changed, repo_root)
    deleted = normalise_paths(deleted, repo_root)
    updater = GraphUpdater(
        ingestor=ingestor,  # type: ignore[arg-type]
        repo_path=repo_root,
        parsers=parsers,
        queries=queries,
        project_name=project_name,
    )
    fetch_all = getattr(ingestor, "fetch_all")
    return observe(
        fetch_all,
        project_name,
        [*changed, *deleted],
        lambda: updater.reingest(changed, deleted=deleted),
        repo_root=repo_root,
    )
