"""Read and write the sync state cgr keeps between runs of one checkout.

`sync.checkout_state` decides WHERE that state lives; this module owns WHAT is in
each file and how it is read back: the file hash cache and its atomic,
no-follow publish, the directory mtimes, the exclusion stamp, the parser
fingerprint, the delombok overlay state and the embeddings markers.

Every reader degrades to "unknown" on a missing or corrupt file rather than
raising, so a damaged state directory costs a rescan, never a run.
"""

import hashlib
import json
import os
import stat
import time
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..config import settings
from ..types_defs import JsonValue
from ..utils.atomic_write import open_temp_sibling, write_text_atomic
from ..utils.json_io import load_json, loads_json

type FileHashCache = dict[str, str]
type DirMtimesCache = dict[str, float]


def content_digest(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def hash_file(filepath: Path) -> str:
    return content_digest(filepath.read_bytes())


def hash_file_with_bytes(filepath: Path) -> tuple[str, bytes] | None:
    try:
        with open(filepath, "rb") as f:
            data = f.read()
    except OSError as e:
        logger.warning(ls.FILE_UNREADABLE, path=filepath, error=e)
        return None
    return content_digest(data), data


def load_hash_cache(cache_path: Path) -> FileHashCache:
    if not cache_path.is_file():
        return {}
    try:
        with cache_path.open(encoding="utf-8") as f:
            data = load_json(f)
        if isinstance(data, dict):
            hashes = {k: v for k, v in data.items() if isinstance(v, str)}
            logger.info(ls.HASH_CACHE_LOADED, count=len(hashes), path=cache_path)
            return hashes
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(ls.HASH_CACHE_LOAD_FAILED, path=cache_path, error=e)
    return {}


_EMPTY_DELOMBOK_STATE: dict = {"identity": "", "keys": [], "lombok": ""}


def empty_delombok_state() -> dict:
    """A fresh delombok state for a checkout that has none yet."""
    return _EMPTY_DELOMBOK_STATE.copy()


def load_delombok_state(state_path: Path) -> dict:
    try:
        loaded = loads_json(state_path.read_text(encoding=cs.ENCODING_UTF8))
    except (OSError, json.JSONDecodeError):
        return _EMPTY_DELOMBOK_STATE.copy()
    if not isinstance(loaded, dict):
        return _EMPTY_DELOMBOK_STATE.copy()
    identity = loaded.get("identity")
    keys = loaded.get("keys")
    lombok = loaded.get("lombok")
    return {
        "identity": identity if isinstance(identity, str) else "",
        "keys": (
            sorted(key for key in keys if isinstance(key, str))
            if isinstance(keys, list)
            else []
        ),
        "lombok": lombok if isinstance(lombok, str) else "",
    }


def save_delombok_state(state_path: Path, state: dict) -> None:
    try:
        write_text_atomic(state_path, json.dumps(state, sort_keys=True))
    except OSError:
        return None


# Keys of the exclusion stamp that describe WHOSE scope it holds, for
# readers; the sync comparison is over the scope alone (issue #1981).
_EXCLUSION_READER_KEYS = frozenset({"project", "named"})


def exclusion_scope(
    stamp: dict[str, list[str] | str],
) -> dict[str, list[str] | str]:
    """The part of an exclusion stamp a sync compares: the scope, without
    the keys that only tell a reader whose scope it is."""
    return {k: v for k, v in stamp.items() if k not in _EXCLUSION_READER_KEYS}


def exclusion_state(
    exclude_paths: frozenset[str] | None,
    unignore_paths: frozenset[str] | None,
    project_name: str | None = None,
    named: bool = False,
) -> dict[str, list[str] | str]:
    state: dict[str, list[str] | str] = {
        "exclude": sorted(exclude_paths or ()),
        "unignore": sorted(unignore_paths or ()),
    }
    if project_name is not None:
        # The stamp is per repository; two projects indexed from the same
        # tree overwrite it, so a reader must know whose scope it holds.
        state["project"] = project_name
        # `named` records whether --project was GIVEN rather than defaulted
        # to the directory name. Without it the two are the same string and
        # a reader cannot tell a project deliberately called `myrepo` from
        # the tree's own default, so one lends the other its scope
        # (issue #1525). Absent on stamps written before this field: those
        # read as unnamed, which is what they were in the common case.
        state["named"] = "1" if named else ""
    return state


def load_exclusion_state(state_path: Path) -> dict[str, list[str] | str] | None:
    """The exclusion set the last completed run indexed under, or None.

    None means "cannot be established" and must be read as changed by the
    caller: an index written before this stamp existed carries an unknown
    exclusion set, exactly like a missing parser fingerprint.
    """
    try:
        loaded = loads_json(state_path.read_text(encoding=cs.ENCODING_UTF8))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return parse_exclusion_stamp(loaded)


def parse_exclusion_stamp(
    loaded: dict[str, JsonValue],
) -> dict[str, list[str] | str] | None:
    result: dict[str, list[str] | str] = {}
    for key in ("exclude", "unignore"):
        values = loaded.get(key)
        if not isinstance(values, list):
            return None
        result[key] = sorted(v for v in values if isinstance(v, str))
    project = loaded.get("project")
    if isinstance(project, str):
        result["project"] = project
    # Whether that project was named with --project rather than defaulted to
    # the directory name. Absent on stamps written before the field existed,
    # which read as unnamed -- what they were, unless someone named a project
    # after its own directory.
    named = loaded.get("named")
    if isinstance(named, str) and named:
        result["named"] = named
    return result


def load_project_stamps(state_path: Path) -> dict[str, dict[str, list[str] | str]]:
    """Every project's own stamp in the exclusion state file (issue #1987).

    The top-level stamp is overwritten by whichever project indexed the tree
    last; these entries are not, so a project indexed earlier can still read
    the scope its graph was built under. Each entry carries its `project`
    key, exactly as the top-level stamp does. A file written before the map
    existed has none, which reads as empty.
    """
    try:
        loaded = loads_json(state_path.read_text(encoding=cs.ENCODING_UTF8))
    except (OSError, json.JSONDecodeError):
        return {}
    projects = (
        loaded.get(cs.EXCLUSION_STATE_PROJECTS_KEY)
        if isinstance(loaded, dict)
        else None
    )
    if not isinstance(projects, dict):
        return {}
    stamps: dict[str, dict[str, list[str] | str]] = {}
    for name, entry in projects.items():
        if not isinstance(entry, dict):
            continue
        parsed = parse_exclusion_stamp(entry)
        if parsed is not None:
            parsed["project"] = name
            stamps[name] = parsed
    return stamps


def save_exclusion_state(state_path: Path, state: dict[str, list[str] | str]) -> None:
    """Write `state` as the last run's stamp and as its project's own entry."""
    projects: dict[str, dict[str, list[str] | str]] = {
        name: {k: v for k, v in stamp.items() if k != "project"}
        for name, stamp in load_project_stamps(state_path).items()
    }
    project = state.get("project")
    if isinstance(project, str):
        projects[project] = {k: v for k, v in state.items() if k != "project"}
    try:
        write_text_atomic(
            state_path,
            json.dumps(
                {**state, cs.EXCLUSION_STATE_PROJECTS_KEY: projects}, sort_keys=True
            ),
        )
    except OSError:
        return None


# Read once at import rather than `os.name` at each use: `pathlib.Path()`
# itself consults `os.name` at call time, so a test that forced `os.name` to
# simulate Windows made every `Path()` inside the publish raise
# NotImplementedError on this host. A module flag can be patched in isolation.
_WINDOWS = os.name == "nt"
# The hash cache is readable by its owner only.
_HASH_CACHE_MODE = 0o600


def _open_exclusive_temp(cache_path: Path) -> tuple[int, str]:
    """Create a fresh temporary file beside `cache_path`, never following a link.

    `O_EXCL` fails rather than reusing an existing path and `O_NOFOLLOW`
    fails rather than opening a symlink's target, so neither a guessed name
    nor a planted link can redirect the write. The name is random, which
    means an attacker cannot plant the link in the first place; the flags
    are what make that a guarantee rather than a probability.

    `O_NOFOLLOW` is POSIX; on platforms lacking it the flag is 0 and the
    random name plus `O_EXCL` still prevent reuse of a planted path.
    """
    descriptor, candidate = open_temp_sibling(cache_path, _HASH_CACHE_MODE)
    return descriptor, str(candidate)


def _fsync_no_follow(path: Path) -> None:
    """Sync `path` to disk through a fresh no-follow descriptor.

    O_RDWR, not O_RDONLY: on Windows `os.fsync` is `_commit()`, which needs
    a handle opened for WRITING and reports a read-only one as EBADF (#1701
    review, measured in CI). No O_TRUNC and no write follows, so the
    contents are untouched. O_NOFOLLOW so a link swapped in at the path
    cannot redirect the sync.
    """
    sync_fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(sync_fd)
    finally:
        os.close(sync_fd)


def _stamp_by_path(tmp_path: Path, observed_at: float) -> None:
    """Set the temporary's mtime through its PATH, where a descriptor cannot.

    Windows is the platform this exists for, and its `os.utime` supports
    NEITHER a descriptor NOR `follow_symlinks`: `Lib/os.py` adds `utime` to
    `supports_follow_symlinks` only for `HAVE_LUTIMES`/`HAVE_UTIMENSAT`,
    which `PC/pyconfig.h` does not define, and passing the keyword there
    raises NotImplementedError (#1701 local review; an earlier revision
    refused here and would have declined every Windows publish). So on
    Windows the stamp goes through the path, guarded by the caller's
    `S_ISLNK` refusal: a link swapped in before that lstat is refused, and
    one swapped in between the lstat and this call needs the same directory
    write access that already lets an attacker rewrite the cache outright,
    plus the symlink privilege Windows withholds by default.

    Any OTHER platform with neither capability has no safe stamp at all, and
    stamping through the path there reopens the exact window this closes: a
    link swapped in after the check gets its TARGET timestamped (#1701
    review, measured by forcing the branch). Declining the publish is the
    safe error: the previous cache stays and the next run re-hashes, where a
    mis-stamped publish loses edits.
    """
    if _WINDOWS:
        os.utime(tmp_path, (observed_at, observed_at))
    elif os.utime in os.supports_follow_symlinks:
        os.utime(tmp_path, (observed_at, observed_at), follow_symlinks=False)
    else:
        raise OSError(
            "cannot stamp the hash cache without following symlinks "
            f"on this platform; not publishing {tmp_path}"
        )


def publish_hash_cache(
    cache_path: Path, hashes: FileHashCache, observed_at: float | None
) -> bool:
    """Write the hash cache and its timestamp as one visible step.

    Returns True when the cache is published, False when it is not. The
    caller needs that answer: the directory-mtime map must not advance past
    a hash cache that failed to publish, or the pair on disk becomes the
    fresh-map/stale-cache combination that hides an addition entirely.

    `_is_already_in_sync` skips any file whose mtime is at or below this
    file's, so a cache carrying its own write time hides every edit made
    while the run was hashing. Writing then stamping leaves that exact state
    on disk between the two calls, and a run that stops there hands it to its
    successor. Building the whole thing on a temporary path and renaming it
    into place closes that window: `os.replace` is atomic within a
    filesystem, so a successor sees either the previous cache or a fully
    stamped new one, never an unstamped new one.

    On any failure the temporary file is removed and the PREVIOUS cache is
    left untouched, which is the safe direction: a cache that is one run out
    of date costs a rescan, while a mis-stamped one loses an edit.
    """
    # The temporary name is unpredictable and the file is created with
    # O_EXCL|O_NOFOLLOW, so a local process that can write to the repository
    # directory cannot pre-place a symlink here and have this truncate the
    # link's target with cache JSON. A PID-derived name opened with
    # `Path.open("w")` did exactly that: the name was guessable and the open
    # followed the link (issue #1700). O_EXCL also means an existing path is
    # a publish FAILURE rather than something to unlink and retry -- removing
    # it would be the same race one step later.
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = _open_exclusive_temp(cache_path)
        tmp_path = Path(tmp_name)
    except OSError as e:
        _warn_publish_failed(cache_path, e)
        return False
    try:
        _stage_hash_cache(fd, tmp_path, hashes, observed_at)
        os.replace(tmp_path, cache_path)
        logger.info(ls.HASH_CACHE_SAVED, count=len(hashes), path=cache_path)
        return True
    except OSError as e:
        _warn_publish_failed(cache_path, e)
        _discard_temp(tmp_path)
        return False


def _stage_hash_cache(
    fd: int, tmp_path: Path, hashes: FileHashCache, observed_at: float | None
) -> None:
    """Fill the temporary through `fd`, stamped with `observed_at` and synced,
    refusing it once its name no longer leads to the file written."""
    # Everything below acts on the DESCRIPTOR, never the pathname. The
    # exclusive no-follow open protects only creation: a local writer can
    # unlink the temporary between then and here and drop a symlink in its
    # place, after which a path-based `os.utime` rewrites the link
    # target's timestamp and `os.replace` publishes the link itself as the
    # cache -- with this function still reporting success. Verified
    # exploitable before this change (#1701).
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(hashes, f, indent=2)
        f.flush()
        # Stamping through the DESCRIPTOR where the platform allows it,
        # because a path-based utime can follow a symlink swapped in after
        # creation. Windows does not: `os.utime` is absent from
        # `os.supports_fd` there, so passing a descriptor raises
        # "TypeError: utime: path should be string, bytes or os.PathLike,
        # not int" and every hash-cache publish fails with it. This host is
        # POSIX, so the fd branch is the only one exercised locally and the
        # break was invisible until CI's Windows job ran (#1701).
        stamp_by_fd = os.utime in os.supports_fd
        if observed_at is not None and stamp_by_fd:
            os.utime(f.fileno(), (observed_at, observed_at))
        # `flush` only pushes the bytes to the OS. Without the fsync a
        # power loss after `os.replace` can expose a cache file whose
        # contents were never written -- and a truncated or empty cache is
        # worse than none, because `_is_already_in_sync` trusts whatever
        # is there. Sync after the utime so the timestamp is durable too,
        # then take the inode: write, sync, then publish (#1701).
        # `fstat` BEFORE `fsync`, not after. On Windows the fsync path
        # goes through `_commit()`, which can leave the descriptor
        # unusable, and the following `fstat` then raised
        # "[Errno 9] Bad file descriptor" -- every hash-cache publish
        # failed with it and 39 tests went red on that platform while
        # ubuntu and macOS stayed green (#1701).
        #
        # Order is safe either way: the inode identifies the same file
        # before and after the sync, and nothing between them can change
        # which file this descriptor refers to.
        created = os.fstat(f.fileno())
    # `fsync` AFTER the writer is closed, not inside it. On Windows the
    # sync goes through `_commit()` on a descriptor owned by the buffered
    # text wrapper, and it raised "[Errno 9] Bad file descriptor" there --
    # 39 tests red on Windows while ubuntu and macOS stayed green, twice
    # in a row, because reordering fstat and fsync inside the block was
    # not enough (#1701). The reopen
    # is no-follow, so a link swapped in between the close and here
    # cannot redirect the sync; the inode check below still refuses a
    # swapped path before anything is published.
    _fsync_no_follow(tmp_path)
    _refuse_swapped_temp(tmp_path, created)
    if observed_at is not None and not stamp_by_fd:
        # The path branch, for platforms whose `os.utime` takes no
        # descriptor (Windows). It has to run AFTER the inode check: a
        # stamp before it would follow a symlink swapped in after
        # creation and rewrite the link target's timestamp, which is
        # measured and was the whole point of the descriptor-based stamp.
        # `_stamp_by_path` carries the per-platform argument.
        #
        # Durability is then restored by re-opening and syncing, rather
        # than by moving the stamp earlier: a crash between the utime and
        # the replace would otherwise publish a cache carrying its write
        # time instead of `observed_at`, and `_is_already_in_sync` trusts
        # that mtime, so it would skip edits made during the run (#1701
        # review).
        _stamp_by_path(tmp_path, observed_at)
        _fsync_no_follow(tmp_path)


def _refuse_swapped_temp(tmp_path: Path, created: os.stat_result) -> None:
    """OSError when the temporary's name now leads to a link, or to another
    file than the one `created` describes."""
    # A cheap refusal, not the security boundary. The boundary is the
    # threat model, and it is worth stating because "there is still a
    # window between this check and the rename" is true and does not
    # matter (#1701):
    #
    # The temporary is always a SIBLING of the cache (`with_name`), and
    # `rename(2)` moves directory ENTRIES without following symlinks on
    # either name -- measured: renaming a symlink over a file leaves the
    # link's target untouched. So once `O_EXCL|O_NOFOLLOW` closes the
    # open-time follow, which was the only path that could write OUTSIDE
    # this directory, a post-creation swap can at worst leave the cache
    # entry pointing at a link the attacker chose.
    #
    # Doing that needs WRITE on the directory, and anyone with directory
    # write can unlink the cache and replace it outright without racing
    # anything. The window grants no capability its own precondition does
    # not already grant. The one directory shape writable by others is
    # world-writable-plus-sticky, and the sticky bit forbids renaming or
    # unlinking another user's entries, so the swap fails there too.
    #
    # The check stays because it costs one lstat and turns a swap into a
    # refusal rather than a silent publish.
    current = os.lstat(tmp_path)
    # A link at the temporary's name is a swap, full stop, on every
    # platform -- including the one below where the inode comparison
    # cannot be made. `lstat` never follows, so this reads the entry
    # itself (#1701).
    if stat.S_ISLNK(current.st_mode):
        raise OSError(f"temporary cache path {tmp_path} was replaced by a symlink")
    # Skipped where the identity comparison is not meaningful. On Windows
    # `st_ino` is a file INDEX rather than an inode: it can be 0 on some
    # filesystems, and `fstat` and `lstat` are not guaranteed to agree, so
    # comparing them there would refuse every publish rather than detect a
    # swap (#1701 review, decision (a)).
    #
    # The premise that makes this acceptable on that platform is the one
    # already argued for the fallback stamp: creating a symlink on Windows
    # requires privilege in the default configuration, and an attacker who
    # can write to the cache directory can rewrite the cache outright
    # without any race. The O_EXCL creation and the fsync stay on every
    # platform; only this comparison is conditional.
    identity_is_meaningful = (
        not _WINDOWS and created.st_ino != 0 and current.st_ino != 0
    )
    if identity_is_meaningful and (
        (current.st_ino, current.st_dev) != (created.st_ino, created.st_dev)
    ):
        raise OSError(f"temporary cache path {tmp_path} was replaced after creation")


def _warn_publish_failed(cache_path: Path, error: OSError) -> None:
    logger.warning(
        ls.HASH_CACHE_SAVE_FAILED,
        path=cache_path,
        error=error,
        errno=getattr(error, "errno", None),
        strerror=getattr(error, "strerror", None),
        failing_path=getattr(error, "filename", None),
    )


def _discard_temp(tmp_path: Path) -> None:
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError as cleanup_error:
        # Bound separately: `e` is why the PUBLISH failed, and reporting
        # that here would name a reason unrelated to the removal.
        # A filesystem too read-only to write the cache is too read-only
        # to have created the temporary, so there is normally nothing to
        # remove; if there is and it cannot go, the stale temporary is
        # inert (no reader looks for `.tmp`) and must not end the run.
        logger.warning(
            ls.CACHE_STAMP_CLEANUP_FAILED, path=tmp_path, error=cleanup_error
        )


def trustworthy_cache_mtime(cache_path: Path) -> float:
    """The cache mtime to compare files against, or 0.0 if it cannot be trusted.

    Both readers use this instant as a watermark: "every file at or below it is
    already accounted for". A stamp AHEAD of the wall clock makes that true of
    EVERY file on disk however recently edited, so nothing is re-hashed, the
    edit never reaches the graph, and no later run corrects it because the
    stamp stays ahead (issue #1665). The causes are ordinary -- an NTP step
    backwards, VM suspend and resume, `cp -p` or `rsync -t` from a host whose
    clock ran ahead, a restored backup or a copied container image.

    Returning 0.0 rather than clamping to `time.time()`: clamping is not
    enough, because "now" is never older than an edit made now, so a file
    written in the same instant still satisfies `mtime <= now` and is skipped
    for the wrong reason. 0.0 disables the shortcut entirely and defers to the
    HASH comparison, which is the authority the watermark is only an
    optimisation over. The cost is one re-hash pass on a tree whose cache is
    misstamped; the alternative is losing edits silently and permanently.

    Shared by both readers on purpose. They answer the same question, and a
    guard applied to one of two predicates is the shape that let the indexer
    and the watcher disagree in issue #1636.
    """
    try:
        stamped = cache_path.stat().st_mtime
    except OSError:
        return 0.0
    return 0.0 if stamped > time.time() else stamped


def load_parser_fingerprint(stamp_path: Path) -> str | None:
    try:
        return stamp_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_parser_fingerprint(stamp_path: Path, fingerprint: str) -> None:
    try:
        write_text_atomic(stamp_path, fingerprint)
    except OSError as e:
        logger.warning(ls.PARSER_FINGERPRINT_SAVE_FAILED, path=stamp_path, error=e)


def embeddings_marker(state_dir: Path, project_name: str) -> Path:
    """The file whose presence says `project_name` owes an embeddings pass.

    One per project, named by a digest because a project name may hold any
    character: each sync creates and removes only its own, so two projects
    syncing one checkout at once cannot erase each other's debt, as they
    could while one file listed them all (PR #3358).
    """
    digest = hashlib.sha256(project_name.encode(cs.ENCODING_UTF8)).hexdigest()
    return state_dir / cs.EMBEDDINGS_PENDING_PROJECT_TEMPLATE.format(digest=digest)


def embeddings_store_stamp(state_dir: Path, project_name: str) -> Path:
    """The file naming the vector store backend `project_name`'s points are in.

    A pass that settles the project writes the selected backend there. Once
    the setting changes, as when `sqlite` replaced Qdrant as the default, the
    points stay in a store nothing searches, and an unchanged checkout takes
    the in-sync fast path, which never embeds: until the stamp names the
    selected backend the project owes a pass. A checkout synced before the
    stamp existed has none; its points went to the default then, Qdrant, so
    only an install that now selects another backend owes that pass.
    """
    digest = hashlib.sha256(project_name.encode(cs.ENCODING_UTF8)).hexdigest()
    return state_dir / cs.EMBEDDINGS_STORE_PROJECT_TEMPLATE.format(digest=digest)


def stamp_names_selected_store(stamp: Path) -> bool:
    try:
        stamped = stamp.read_text(encoding=cs.ENCODING_UTF8).strip()
    except FileNotFoundError:
        stamped = str(cs.EMBEDDINGS_STORE_UNSTAMPED)
    except OSError:
        return False
    return stamped == str(settings.VECTOR_STORE_BACKEND)


def save_embeddings_store(stamp: Path) -> None:
    try:
        write_text_atomic(stamp, str(settings.VECTOR_STORE_BACKEND))
    except OSError:
        # Best effort, as for the pending marker: without the stamp the next
        # sync runs the pass again, which is slower but loses nothing.
        logger.debug(ls.EMBEDDINGS_PENDING_NOT_UPDATED)


def save_embeddings_owed(marker: Path, project_name: str, owed: bool) -> bool:
    """Create or remove `project_name`'s marker; say whether that went through."""
    try:
        if owed:
            # The name is for whoever lists the state directory; the file's
            # presence is what the sync reads, so a write cut short still
            # records the debt and needs no temporary sibling.
            marker.write_text(project_name, encoding=cs.ENCODING_UTF8)
        else:
            marker.unlink(missing_ok=True)
    except OSError:
        # Best effort, like the other markers: a read-only state directory
        # must not lose the run to a marker it could not write.
        logger.debug(ls.EMBEDDINGS_PENDING_NOT_UPDATED)
        return False
    return True


def load_shared_embeddings_owed(marker: Path) -> frozenset[str] | None:
    """The projects a marker shared by every project says owe a pass.

    An earlier build kept one marker for all projects: a JSON list of their
    names, and before that an empty file. Empty without one. None for one
    that names no project, the empty file included, since which project
    failed cannot be told then and every project owes.
    """
    try:
        loaded = loads_json(marker.read_text(encoding=cs.ENCODING_UTF8))
    except FileNotFoundError:
        return frozenset()
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, list):
        return None
    names = [name for name in loaded if isinstance(name, str)]
    return frozenset(names) if len(names) == len(loaded) else None


def load_dir_mtimes(cache_path: Path) -> DirMtimesCache:
    if not cache_path.is_file():
        return {}
    try:
        with cache_path.open(encoding="utf-8") as f:
            data = load_json(f)
        if isinstance(data, dict):
            return {k: float(v) for k, v in data.items() if isinstance(v, int | float)}
    except (OSError, ValueError) as exc:
        # A cache: without it the next run rescans every directory.
        logger.debug(ls.STATE_CACHE_UNREADABLE, path=cache_path, error=exc)
    return {}


def save_dir_mtimes(cache_path: Path, mtimes: DirMtimesCache) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(cache_path, json.dumps(mtimes))
    except OSError as exc:
        logger.debug(ls.STATE_CACHE_UNWRITABLE, path=cache_path, error=exc)


def touch_empty_json(cache_path: Path) -> None:
    if cache_path.exists():
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as f:
            f.write(cs.JSON_EMPTY_OBJECT)
    except OSError as exc:
        logger.debug(ls.STATE_CACHE_UNWRITABLE, path=cache_path, error=exc)


def is_placeholder_hash_cache(cache_path: Path) -> bool:
    # Read quietly rather than through `load_hash_cache`, which logs every
    # load: this only classifies the file. One that cannot be read or decoded
    # is not the placeholder `touch_empty_json` wrote.
    try:
        return loads_json(cache_path.read_text(encoding=cs.ENCODING_UTF8)) == {}
    except (OSError, ValueError):
        return False
