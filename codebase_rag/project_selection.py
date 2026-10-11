"""Which indexed project a command reads (issue #2415).

Project names are `<dir>__<hash>`, so a user cannot guess one. Every command
that reads or deletes one project takes it the way `cgr graph` always has:
`--project`, or else the project `--repo-path` (the current directory when
not given) derives to. A name the graph does not hold is refused with its
close matches, and with the indexed projects only when nothing is close.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple, Protocol

from . import constants as cs
from . import graph_query
from .utils.path_utils import derive_project_name

ProjectRoots = dict[str, str | None]


class ProjectRootsReader(Protocol):
    def list_project_roots(self) -> ProjectRoots: ...


class ProjectChoice(NamedTuple):
    """The project to read, or (`name` None) why none could be chosen."""

    name: str | None
    refusal: str = ""


def suggestion_tail(
    names: Sequence[str], known: Sequence[str], rooted: Sequence[str] = ()
) -> str:
    """The end of a refusal for `names`: what they most likely meant.

    `rooted` are the projects indexed from the directory a derived name came
    from, under a name of their own; they lead. Every indexed project is
    listed only when nothing is close, since that list can run to dozens.
    """
    close = (
        match
        for name in names
        for match in graph_query.close_project_names(name, known)
    )
    suggestions = list(dict.fromkeys([*rooted, *close]))
    if suggestions:
        return graph_query.did_you_mean(suggestions)
    if known:
        return cs.CLI_ERR_GRAPH_INDEXED_PROJECTS.format(
            projects=cs.SEPARATOR_COMMA_SPACE.join(sorted(known))
        )
    return cs.CLI_ERR_GRAPH_NOTHING_INDEXED


def unknown_project(name: str, known: Sequence[str]) -> str:
    return cs.CLI_ERR_GRAPH_UNKNOWN_PROJECT.format(project=name) + suggestion_tail(
        [name], known
    )


def repo_not_indexed(
    repo_path: Path, name: str, known: Sequence[str], roots: ProjectRoots
) -> str:
    """The refusal for a project derived from `repo_path` that is not indexed.

    The directory is what was never indexed, so it is named rather than the
    hash; a project indexed from it under a name of its own is the likeliest
    thing meant.
    """
    message = cs.CLI_ERR_GRAPH_REPO_NOT_INDEXED.format(path=repo_path.resolve())
    if not known:
        # The message already says to index this directory.
        return message
    rooted = graph_query.projects_rooted_at(roots, repo_path)
    return message + suggestion_tail([name], known, rooted)


def choose_project(
    project: str | None,
    repo_path: Path | None,
    known: Sequence[str],
    graph: ProjectRootsReader,
) -> ProjectChoice:
    """The one project a command reads.

    `project` wins; else the project `repo_path` derives to, the current
    directory when it is None. A run that gives neither, in a directory that
    was never indexed, still reads the graph's only project, as `dead-code`
    and `duplicates` always did. `graph` is asked for the projects' roots only
    to explain a refusal.
    """
    if project:
        if project in known:
            return ProjectChoice(project)
        return ProjectChoice(None, unknown_project(project, known))
    here = (repo_path or Path.cwd()).resolve()
    derived = derive_project_name(here)
    if derived in known:
        return ProjectChoice(derived)
    if repo_path is None and len(known) == 1:
        return ProjectChoice(known[0])
    return ProjectChoice(
        None, repo_not_indexed(here, derived, known, graph.list_project_roots())
    )


def scope_refusal(
    missing: Sequence[str],
    known: Sequence[str],
    repo_path: Path | None,
    graph: ProjectRootsReader,
) -> str:
    """Why a `stats`/`export` scope naming `missing` cannot be read."""
    if repo_path is not None:
        derived = derive_project_name(repo_path)
        if derived in missing:
            return repo_not_indexed(
                repo_path, derived, known, graph.list_project_roots()
            )
    return cs.CLI_ERR_PROJECTS_NOT_INDEXED.format(
        missing=cs.SEPARATOR_COMMA_SPACE.join(missing)
    ) + suggestion_tail(missing, known)
