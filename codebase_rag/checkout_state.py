"""Where cgr keeps what it knows about one checkout (issue #2427).

Every sync wrote its incremental state (hash cache, directory mtimes,
exclusion stamp, parser fingerprint, pending markers), and every edit its
history and lock, into the root of the indexed checkout. A clean `git status`
became a list of untracked `.cgr-*` files, a routine `git add -A` committed
machine-local state (mtimes, a project name hashed from the clone's absolute
path), a committed state file then showed as modified after every sync and in
`cgr check`, and a read-only checkout could not be synced incrementally.

The state lives in one directory per checkout instead,
`CGR_HOME/state/<name>__<hash>`, named the way `derive_project_name` names
the checkout's default project. It is keyed by the checkout rather than by
the project because the state belongs to the tree: a checkout indexed under
several `--project-name`s shares one hash cache, and the exclusion stamp
keeps each project's entry inside that one file.

Nothing here is specific to one state file. Every name in
`CGR_STATE_FILENAMES` that an older cgr left in the checkout root is moved
into the directory the first time the checkout's state is reached, so an
upgrade keeps its hash cache (and the mtime the in-sync check compares
files against) and its edit history, and a name added to the set is kept
out of the tree and migrated the same way.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

from loguru import logger

from . import constants as cs
from . import logs as ls
from .config import settings
from .utils.path_utils import derive_project_name


def state_dir(repo_path: Path) -> Path:
    """The directory holding the state of the checkout at `repo_path`.

    Nothing is created or moved, so a lookup (has cgr synced this
    ancestor?) leaves no directory behind.
    """
    return (
        settings.CGR_HOME.expanduser()
        / cs.CGR_STATE_DIRNAME
        / derive_project_name(repo_path)
    )


def prepare_state_dir(repo_path: Path) -> Path:
    """`state_dir(repo_path)`, created, with the state left in the tree moved in.

    Creating it is best effort. When CGR_HOME cannot be written the problem
    is logged and the directory returned anyway: reads find nothing and
    writes fail the way they failed on a read-only tree, so the sync runs
    without incremental state instead of stopping.
    """
    directory = state_dir(repo_path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        logger.warning(ls.CHECKOUT_STATE_DIR_UNAVAILABLE, path=directory, error=error)
        return directory
    adopted = [
        name
        for name in sorted(cs.CGR_STATE_FILENAMES)
        if _adopt(repo_path / name, directory / name)
    ]
    if adopted:
        logger.info(
            ls.CHECKOUT_STATE_ADOPTED,
            repo=repo_path,
            path=directory,
            names=", ".join(adopted),
        )
    return directory


def state_file(repo_path: Path, name: str) -> Path:
    """Where the checkout's state file `name` is read and written."""
    return prepare_state_dir(repo_path) / name


def _adopt(legacy: Path, target: Path) -> bool:
    """Move `legacy`, left in the tree by an older cgr, to `target`.

    Returns whether it became `target`.
    """
    try:
        found = legacy.lstat()
    except OSError:
        return False
    # Only a regular file is cgr's own. A link under a state file's name,
    # say in a cloned repository, would point cgr's later writes wherever
    # it leads, so it is neither followed nor moved.
    if not stat.S_ISREG(found.st_mode):
        return False
    if target.exists():
        # The directory's copy is the one kept current since the upgrade.
        # A copy in the tree is older, for instance restored by checking out
        # a commit that still tracked it, and taking it would roll the
        # state back.
        _discard(legacy)
        return False
    try:
        # A rename keeps the mtime, which the in-sync check compares every
        # cached file against.
        os.replace(legacy, target)
        return True
    except FileNotFoundError:
        # Another process moved it first.
        return False
    except OSError:
        pass
    # Another filesystem, or a tree cgr may read but not change.
    if not _copy(legacy, target):
        return False
    _discard(legacy)
    return True


def _copy(legacy: Path, target: Path) -> bool:
    temp: Path | None = None
    try:
        handle, name = tempfile.mkstemp(
            dir=target.parent, prefix=f"{target.name}.", suffix=cs.TMP_EXTENSION
        )
        os.close(handle)
        temp = Path(name)
        # copy2 keeps the mtime, as the rename does.
        shutil.copy2(legacy, temp)
        os.replace(temp, target)
    except OSError as error:
        if temp is not None:
            temp.unlink(missing_ok=True)
        logger.warning(
            ls.CHECKOUT_STATE_ADOPT_FAILED, legacy=legacy, target=target, error=error
        )
        return False
    return True


def _discard(legacy: Path) -> None:
    try:
        legacy.unlink(missing_ok=True)
    except OSError as error:
        logger.debug(ls.CHECKOUT_STATE_LEGACY_LEFT, legacy=legacy, error=error)
