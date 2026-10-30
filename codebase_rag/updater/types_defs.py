from pathlib import Path
from typing import Protocol


class WalkScope(Protocol):
    """The paths the updater's walk excludes and the ones it rescues.

    Read where they are needed rather than copied at construction: a run's
    generated-source discovery replaces the rescued set, and every reader
    must prune as the walk it runs beside does (issue #1088).
    """

    exclude_paths: frozenset[str] | None
    unignore_paths: frozenset[str] | None


class FileStateRemover(Protocol):
    """The updater's own removal of a file from its state, called through
    the updater so every route to a re-parse strips the old entry the same
    way (`GraphUpdater.remove_file_from_state`)."""

    def remove_file_from_state(
        self, file_path: Path, frontend_current: bool = False
    ) -> None: ...
