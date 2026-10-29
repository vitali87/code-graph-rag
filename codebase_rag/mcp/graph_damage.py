"""What an MCP server knows, in process, about damage to the graph it serves.

A run that dies part way leaves the graph partial. Its durable record is the
`:IncompleteRun` marker (`sync_guard`), one per project; this is the
in-process one, which also covers what no marker can describe: a wipe that
spans every project, and a failure whose own marker could not be written.
Reads refuse while it is up, and a scoped reingest will not hydrate from a
graph it marks.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class GraphDamage:
    # True from the moment an index or update starts mutating the graph until
    # it completes: a run that failed part way leaves a graph a scoped
    # reingest must not hydrate from, because it would treat the missing and
    # stale definitions as authoritative.
    incomplete: bool = False
    # The project the damage is ABOUT, or None when it is not attributable to
    # one (a wipe spans every project, and a failure before the name is known
    # could have touched anything). A read refuses only for the named project,
    # or for everything when it is None: one project's failed recovery must
    # not make every other project unreadable, and a healthy project's success
    # must not clear the warning the damaged one earned (issue #1774;
    # Greptile, PR #1547). None is therefore the BROADEST state: evidence
    # about one project cannot retire it.
    project: str | None = None
    # The projects still outstanding when the damage spans more than one: two
    # named projects were damaged and the pair cannot fit in `project`. A set
    # rather than a boolean, so repairs can retire it: the flag settles when
    # the last outstanding project is repaired, where a boolean would latch
    # every project's reads for the life of the process (greptile-local,
    # PR #1547).
    projects: set[str] = field(default_factory=set)
    # A wipe damages every project at once, including ones this process has
    # never named, so `projects` cannot enumerate it. Only a completed wipe
    # retires this.
    unbounded: bool = False
    # The project whose FAILED MARKER CLEAR raised `incomplete`, or None when
    # the flag came from somewhere else (a failed wipe, delete or update,
    # which wrote no marker for this project). Only a flag with this
    # provenance may be healed by a marker recovery (#1705).
    from_failed_clear: str | None = None

    def note_outstanding(self, *project_names: str | None) -> None:
        """Record projects whose damage is still outstanding.

        Additive: an earlier name must survive, because the flag settles only
        when the LAST outstanding project is repaired.
        """
        self.projects = {*self.projects, *(name for name in project_names if name)}

    def settle(self, project_name: str) -> None:
        """A successful marker clear for `project_name`: evidence about that
        project only.

        It retires the flag when the flag is this project's, or when it was
        never attributed and nothing else is outstanding (the run that earned
        it is the run clearing it). A flag about another project, or spanning
        several, stays: it is the sole record that they are partial.
        """
        self.projects = {name for name in self.projects if name != project_name}
        if not self.unbounded and (
            self.project == project_name or (self.project is None and not self.projects)
        ):
            self.incomplete = False
            self.project = None
            self.from_failed_clear = None
        elif self.from_failed_clear == project_name:
            # This project's stranded marker is gone, so the reason recorded
            # for the flag no longer holds; the flag itself is another's.
            self.from_failed_clear = None

    def blocks(self, project_name: str) -> bool:
        """Whether this process's record refuses reads of `project_name`."""
        return self.incomplete and self.project in (None, project_name)
