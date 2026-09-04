"""`cgr check`: the structural delta of the working tree against a git ref.

The graph is assumed to reflect `--base` (index at the base, then edit);
the files that differ between the base and the working tree are re-ingested
and the delta reported the same way the MCP write tools report it after
each write (issue #1525). Exit status 1 with `--fail-on-found` makes it a
CI or pre-commit gate.

The check measures the graph against the working tree, and the re-ingest
brings the graph up to the tree, so a second run on an unchanged tree
reports nothing: the delta was already applied. Rebuild the graph at the
base (or index at the base before editing) to measure the same edit again.
The CLI holds no lock against a concurrently running MCP server; like every
other command that writes the graph, run it when no other writer is active.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from tree_sitter import Parser

from . import constants as cs
from .config import load_ignore_patterns
from .graph_updater import GraphUpdater, _load_exclusion_state
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
    if base.startswith("-"):
        # Placed before git's own `--`, a dash-prefixed value would be read
        # as a diff option (`--cached` compares the index) rather than a
        # revision, and the check would measure the wrong files.
        raise CheckError(cs.CHECK_BAD_BASE.format(base=base))
    try:
        status = subprocess.run(
            # `--relative`: paths relative to `repo_root`, which may sit below
            # the git toplevel; `ls-files --others` is already cwd-relative.
            # `-z`: NUL-delimited, unquoted paths, so a name holding a tab or
            # a newline is not C-quoted into something that does not exist.
            [
                cs.SHELL_CMD_GIT,
                "diff",
                "--name-status",
                "--no-renames",
                "--relative",
                "-z",
                base,
                "--",
            ],
            cwd=repo_root,
            capture_output=True,
            encoding=cs.ENCODING_UTF8,
            check=True,
        ).stdout
        untracked = subprocess.run(
            [cs.SHELL_CMD_GIT, "ls-files", "--others", "--exclude-standard", "-z"],
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
    # `-z` output alternates status and path fields, each NUL-terminated.
    fields = [f for f in status.split("\0") if f]
    for code, path in zip(fields[0::2], fields[1::2], strict=False):
        (deleted if code.startswith(_GIT_DELETED) else changed).add(path)
    # cgr's own untracked state files (hash cache, directory mtimes, ...)
    # are not source and must not be re-ingested or reported as reparsed.
    changed.update(
        entry
        for entry in untracked.split("\0")
        if entry and not Path(entry).name.startswith(_CGR_STATE_PREFIX)
    )
    return sorted(changed), sorted(deleted)


def indexed_scope(
    repo_root: Path, project_name: str
) -> tuple[frozenset[str] | None, frozenset[str] | None]:
    """The exclusion scope `project_name`'s graph was last indexed under.

    The last completed run stamps its effective `--exclude` and unignore
    sets (CLI flags included, not only `.cgrignore`) in the exclusion state
    file; the check must re-ingest under that same scope or a file the
    index deliberately left out would enter the graph. The stamp is per
    repository, so one written by ANOTHER project indexed from this tree
    refuses the check rather than lending that project's scope. Without a
    stamp the `.cgrignore` file is the only scope there is.
    """
    stored = _load_exclusion_state(repo_root / cs.EXCLUSION_STATE_FILENAME)
    if stored is not None:
        owner = stored.get("project")
        if isinstance(owner, str) and owner != project_name:
            raise CheckError(
                cs.CHECK_SCOPE_OF_OTHER_PROJECT.format(
                    project=project_name, other=owner
                )
            )
        exclude = stored.get("exclude") or []
        unignore = stored.get("unignore") or []
        return (
            frozenset(exclude) or None,  # type: ignore[arg-type]
            frozenset(unignore) or None,  # type: ignore[arg-type]
        )
    cgrignore = load_ignore_patterns(repo_root)
    return cgrignore.exclude or None, cgrignore.unignore or None


def run_check(
    repo_root: Path,
    base: str,
    project_name: str,
    ingestor: object,
    parsers: Mapping[cs.SupportedLanguage, Parser],
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> StructuralDelta:
    """Re-ingest what changed since `base` and return the structural delta.

    `exclude_paths` and `unignore_paths` are the project's indexing scope
    (the `.cgrignore` file plus any CLI excludes): a changed file outside
    that scope must not enter the graph through the check.
    """
    changed, deleted = changed_since(repo_root, base)
    changed = normalise_paths(changed, repo_root)
    deleted = normalise_paths(deleted, repo_root)
    updater = GraphUpdater(
        ingestor=ingestor,  # type: ignore[arg-type]
        repo_path=repo_root,
        parsers=parsers,
        queries=queries,
        project_name=project_name,
        exclude_paths=exclude_paths,
        unignore_paths=unignore_paths,
    )
    fetch_all = getattr(ingestor, "fetch_all")
    return observe(
        fetch_all,
        project_name,
        [*changed, *deleted],
        lambda: updater.reingest(changed, deleted=deleted),
        repo_root=repo_root,
    )
