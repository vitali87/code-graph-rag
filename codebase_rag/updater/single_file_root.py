"""Find the project root a single-file target keys against: the root of the
directory run that indexed it (issue #1775)."""

from pathlib import Path

from .. import constants as cs
from ..sync.checkout_state import state_dir
from ..utils.json_io import load_json


def project_root_for_single_file(target: Path) -> Path:
    """The project root owning `target`, or its parent when none is found.

    A single-file run must key the file as the directory run that indexed it
    did, or delete-before-reingest misses the node and writes a duplicate
    under the wrong key (#1775). The nearest ancestor whose hash cache lists
    the target wins, since that cache is evidence of the root that indexed
    it; only without one does the nearest `.git` apply, which is a guess. A
    cache outranks a nearer `.git` only when it lists a file inside that
    nested checkout: the cache lives under CGR_HOME and can outlive the tree
    it described (PR #2557). A target under neither marker keys against its
    parent, as before.
    """
    git_root: Path | None = None
    for ancestor in target.parents:
        cache = _hash_cache_marking(ancestor)
        if cache is not None and (
            git_root is None or _cache_lists_under(cache, ancestor, git_root)
        ):
            return ancestor
        if git_root is None and (ancestor / cs.GIT_DIR_NAME).exists():
            git_root = ancestor
    return git_root if git_root is not None else target.parent


def _hash_cache_marking(ancestor: Path) -> Path | None:
    """The hash cache a directory run at `ancestor` wrote, if there is one.

    In its state directory, or in the tree itself where an older cgr wrote
    it. `state_dir`, not `prepare_state_dir`: a lookup must not create a
    state directory for every ancestor it asks about (issue #2427).
    """
    for cache in (
        state_dir(ancestor) / cs.HASH_CACHE_FILENAME,
        ancestor / cs.HASH_CACHE_FILENAME,
    ):
        if cache.is_file():
            return cache
    return None


def _cache_lists_under(cache: Path, root: Path, checkout: Path) -> bool:
    """Whether the run at `root` that wrote `cache` indexed a file in
    `checkout`. Keys are POSIX paths relative to `root`; one that cannot be
    read lists nothing."""
    prefix = f"{checkout.relative_to(root).as_posix()}{cs.SEPARATOR_SLASH}"
    try:
        with cache.open(encoding=cs.ENCODING_UTF8) as f:
            entries = load_json(f)
    except (OSError, ValueError):
        return False
    return isinstance(entries, dict) and any(key.startswith(prefix) for key in entries)
