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
out of the tree and migrated the same way. A lock is the exception
(`CGR_STATE_LOCK_FILENAMES`): it stays in the tree, where an older cgr
still running takes it, and an edit takes it as well as its own.

A move never replaces a file already in the directory. Another process
may publish the checkout's current state there at any moment, and the
tree's copy is older than anything it publishes, so the move copies the
file beside its destination and then links the copy into place, which
fails rather than replaces when the destination exists. The copy keeps
the mtime, and the destination never shares an inode with the tree's
copy, which state written in place would otherwise change too.
"""

from __future__ import annotations

import contextlib
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


def prepare_state_dir(repo_path: Path, *, adopt: bool = True) -> Path:
    """`state_dir(repo_path)`, created, with the state left in the tree moved in.

    Creating it is best effort. When CGR_HOME cannot be written the problem
    is logged and the directory returned anyway: reads find nothing and
    writes fail the way they failed on a read-only tree, so the sync runs
    without incremental state instead of stopping.

    `adopt=False` only creates it, for the edit lock: the history an older
    cgr may still be appending to is moved once that lock is held.
    """
    directory = state_dir(repo_path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        logger.warning(ls.CHECKOUT_STATE_DIR_UNAVAILABLE, path=directory, error=error)
        return directory
    if not adopt:
        return directory
    adopted = [
        name
        for name in sorted(cs.CGR_STATE_FILENAMES - cs.CGR_STATE_LOCK_FILENAMES)
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

    Returns whether it became `target`. A `target` that exists, or appears
    while the move runs, is kept and `legacy` dropped.
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
    try:
        placed = not target.exists() and _place(legacy, target)
    except FileExistsError:
        placed = False
    except OSError as error:
        # The tree's copy is all there is, so it stays.
        logger.warning(
            ls.CHECKOUT_STATE_ADOPT_FAILED, legacy=legacy, target=target, error=error
        )
        return False
    # Moved, or the directory holds the copy kept current since the upgrade
    # (or one a sync just published). A copy in the tree is older, for
    # instance restored by checking out a commit that still tracked it, and
    # taking it would roll the state back; either way it goes.
    _discard(legacy)
    return placed


def _place(legacy: Path, target: Path) -> bool:
    """Copy `legacy` to `target` unless `target` exists, keeping its mtime.

    Raises FileExistsError when `target` appeared meanwhile. Returns False
    when `legacy` is gone: another process moved it first.
    """
    handle, name = tempfile.mkstemp(
        dir=target.parent, prefix=f"{target.name}.", suffix=cs.TMP_EXTENSION
    )
    os.close(handle)
    temp = Path(name)
    try:
        # copy2 keeps the mtime, which the in-sync check compares every
        # cached file against.
        shutil.copy2(legacy, temp)
        _link_no_replace(temp, target)
    except FileNotFoundError:
        if legacy.exists():
            raise
        return False
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
    return True


def _link_no_replace(source: Path, target: Path) -> None:
    """Give `source` the name `target`, raising FileExistsError if it is taken.

    A hard link is that operation on POSIX and on NTFS alike, and atomic:
    the destination is never seen half written. `os.replace` replaces on
    every platform, and `os.rename` replaces on POSIX, so neither will do.
    """
    try:
        os.link(source, target)
    except (FileExistsError, FileNotFoundError):
        raise
    except OSError:
        # A filesystem without hard links (FAT, some network shares).
        _create_exclusive(source, target)


def _create_exclusive(source: Path, target: Path) -> None:
    """Write `source` to a new `target`, failing if one exists (O_EXCL).

    Never replaces, like the link, but is not atomic: a reader in the
    instant it takes sees a short file, read as no state. Only a CGR_HOME
    without hard links comes here.
    """
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(target, flags, cs.EDIT_TEMP_FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as out, source.open("rb") as data:
            shutil.copyfileobj(data, out)
        shutil.copystat(source, target)
    except OSError:
        # Half written, or without the mtime the in-sync check relies on: it
        # must not pass for the state, or the next run drops the tree's copy
        # as stale.
        target.unlink(missing_ok=True)
        raise


def _discard(legacy: Path) -> None:
    try:
        legacy.unlink(missing_ok=True)
    except OSError as error:
        logger.debug(ls.CHECKOUT_STATE_LEGACY_LEFT, legacy=legacy, error=error)
