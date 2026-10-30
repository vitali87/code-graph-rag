"""The generated sources a sync indexes beside the checked-in ones (issue
#1140): annotation-processor output under a build directory, and the delombok
overlay a Lombok-annotated file is parsed through."""

from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..parsers.factory import ProcessorFactory
from ..parsers.java.generated import (
    discover_generated_source_roots,
    generated_prefixes_for,
    unignore_patterns_for,
)
from ..parsers.java.lombok import (
    build_delombok_overlay,
    current_lombok_identity,
    overlay_identity,
)
from ..sync.state import empty_delombok_state, load_delombok_state, save_delombok_state


class GeneratedSources:
    """Recomputed per run, so a build or a Lombok jar that appears between
    watch runs is picked up, and committed only once the run succeeds.

    `overlay` maps a file key to the delomboked bytes its parse reads
    instead of the checked-in source; `stale_keys` names the files whose
    expansion changed since the last committed run, which the hash cache
    alone cannot see because their checked-in bytes did not.
    """

    def __init__(
        self,
        factory: ProcessorFactory,
        repo_path: Path,
        configured_unignore_paths: frozenset[str] | None,
    ) -> None:
        self._factory = factory
        self._repo_path = repo_path
        self._configured_unignore_paths = configured_unignore_paths
        self.overlay: dict[str, bytes] = {}
        self.state_changed = False
        self.stale_keys: set[str] = set()
        self._state_candidate: dict = empty_delombok_state()

    def refresh(self, state_dir: Path) -> frozenset[str] | None:
        """Rediscover this run's generated sources; the rescued paths they
        add to the configured ones, which the walk must now keep."""
        # Annotation-processor output next to a build file (issue #1140):
        # carve those exact subtrees out of the target/build prune, register
        # them as Java import-probe roots, and stamp their modules generated.
        # Recomputed per run so a build that appears between watch runs is
        # picked up; no roots leaves everything exactly as before.
        roots = discover_generated_source_roots(self._repo_path)
        dp = self._factory.definition_processor
        dp.generated_source_prefixes = generated_prefixes_for(roots)
        self._factory.import_processor.java.set_java_generated_roots(roots)
        # Rebuilt from the CONFIGURED base every run, never accumulated: a
        # root that vanished (target/ cleaned) must stop rescuing its subtree.
        patterns = unignore_patterns_for(roots)
        base = (
            frozenset(self._configured_unignore_paths)
            if self._configured_unignore_paths
            else frozenset()
        )
        combined = base | patterns
        resolved = combined if combined else None
        # The factory-owned processors hold their own copies for structure
        # traversal, import-time walks and the declaration tie-break; they must
        # prune identically or a sweep could read files the indexer skipped
        # (issue #1088 invariant). The factory derives that set from its own
        # slots rather than taking a list from here, because a list here failed
        # open when a third consumer was added (#1720).
        self._factory.propagate_unignore_paths(resolved)
        if roots:
            logger.info(ls.GENERATED_SOURCES_REGISTERED, count=len(roots))
        self._refresh_delombok_overlay(state_dir)
        return resolved

    def _refresh_delombok_overlay(self, state_dir: Path) -> None:
        # Delombok overlay (issue #1140 tier 1): rebuilt per run so a jar or
        # annotation appearing between watch runs is picked up; empty means
        # raw parsing everywhere, exactly as before. The persisted identity
        # detects overlay CHANGES (jar appears/vanishes, version bump): the
        # affected files' cached hashes still match the checked-in source,
        # so without forcing them through a reparse the graph would keep
        # stale (or never gain) generated members.
        self.overlay = build_delombok_overlay(self._repo_path)
        previous = load_delombok_state(state_dir / cs.DELOMBOK_STATE_FILENAME)
        current = {
            "identity": overlay_identity(self.overlay),
            "keys": sorted(self.overlay),
            # Two Lombok versions can expand identically today and diverge on
            # the next annotation edit; the version keeps the state honest.
            "lombok": current_lombok_identity(),
        }
        self.state_changed = previous != current
        self.stale_keys = (
            set(previous.get("keys", ())) | set(current["keys"])
            if self.state_changed
            else set()
        )
        # Held in memory until the run SUCCEEDS: persisting now would let a
        # crashed run convince its successor that the stale files were
        # already reprocessed. Single-file runs never commit it either.
        self._state_candidate = current

    def commit(self, state_dir: Path) -> None:
        """Record this run's overlay as the one the graph now reflects."""
        if self.state_changed:
            save_delombok_state(
                state_dir / cs.DELOMBOK_STATE_FILENAME, self._state_candidate
            )
