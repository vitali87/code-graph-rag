"""The frame's change detection against Python's.

Phase 2's step 6 (docs/reports/PHASE2_FRAME.md, sections 6 and 13) moves
the file half of change detection to Rust: the state folder, the hash cache
and the folder times, the in-sync check, and each run's new, changed and
deleted files. The frame must read and write the same files byte for byte,
so an upgrade keeps users' state, and decide what Python's sync decides.
Each piece is held here to Python's own: the readers and writers on the
same files, the folder's name, the in-sync check's cases, the hash reuse,
the single-file run's exceptions, and the run's fallback to Python's answer
on a difference. The existing incremental-sync tests run once more with
every run's change detection and every reading and writing of the state
files compared (`change_shadow.py`).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import engine_frame
from codebase_rag.checkout_state import prepare_state_dir, state_dir, state_file
from codebase_rag.config import settings
from codebase_rag.graph_updater import (
    GraphUpdater,
    _load_dir_mtimes,
    _load_hash_cache,
    _publish_hash_cache,
    _save_dir_mtimes,
    _trustworthy_cache_mtime,
)
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import (
    _MockIngestor,
    create_and_run_updater,
    force_mtime_after_cache,
)
from codebase_rag.tests.engine_modes import CHANGE_SHADOW_DIR_ENV

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent
SHADOW_PLUGIN = "codebase_rag.tests.change_shadow"
# The tests that rerun under the shadow: every test file that syncs a
# repository more than once or reads its state files, found as the files
# naming the state files, the in-sync check or a re-hash after the cache.
SHADOW_MARKERS = re.compile(
    r"skipped_because_in_sync|HASH_CACHE_FILENAME|DIR_MTIMES_FILENAME"
    r"|_is_already_in_sync|force_mtime_after_cache"
)
needs_permissions = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="POSIX permissions, which root reads past",
)
needs_links = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")


def _engine() -> engine_frame.Engine:
    made = engine_frame.engine()
    # The checkout builds its own crate, so the state files are always there.
    assert made is not None and hasattr(made, "changes"), (
        "the extension lacks change detection; rebuild the crate"
    )
    return made


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=cs.ENCODING_UTF8)
    return path


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _write(repo, "a.py", "def a():\n    return 1\n")
    _write(repo, "pkg/b.py", "def b():\n    return 2\n")
    _write(repo, "pkg/sub/c.py", "C = 3\n")
    return repo


def _updater(repo: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(_MockIngestor(), repo, parsers, queries, skip_embeddings=True)


# --- the state folder ---------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "plain",
        "a.b c",
        "a..b  é c",
        "мой-repo",
        "__x--",
        "...",
        "é-é",
        "UPPER_case-1",
        "😀",
        "a\tb",
    ],
)
def test_the_state_folder_is_named_as_python_names_it(
    tmp_path: Path, name: str
) -> None:
    repo = tmp_path / name
    repo.mkdir()

    assert _engine().state_dir(settings.CGR_HOME, repo) == str(state_dir(repo))


def test_the_state_folder_of_a_link_a_relative_path_and_a_missing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    monkeypatch.chdir(tmp_path)
    paths = [
        Path("real"),
        tmp_path / "gone" / ".." / "real",
        tmp_path / "missing" / "x",
    ]
    if sys.platform != "win32":
        link.symlink_to(target)
        paths.append(link)

    for path in paths:
        assert _engine().state_dir(settings.CGR_HOME, path) == str(state_dir(path))


def _old_state(repo: Path) -> None:
    """The state files an older cgr left in the checkout, with set mtimes."""
    repo.mkdir(parents=True)
    for number, name in enumerate(
        [
            cs.HASH_CACHE_FILENAME,
            cs.DIR_MTIMES_FILENAME,
            cs.PARSER_FINGERPRINT_FILENAME,
            cs.SYNC_LOCK_FILENAME,
        ]
    ):
        path = repo / name
        path.write_text(f"{name}\n", encoding=cs.ENCODING_UTF8)
        os.utime(
            path, ns=(1_700_000_000_123_456_789 + number, 1_600_000_000_987_654_321)
        )


def _listing(directory: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        entry.name: (entry.read_bytes(), entry.stat().st_mtime_ns, entry.stat().st_mode)
        for entry in sorted(directory.iterdir())
    }


def test_a_first_use_moves_old_state_in_as_python_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python_home, frame_home = tmp_path / "python-home", tmp_path / "frame-home"
    python_repo, frame_repo = tmp_path / "a" / "repo", tmp_path / "b" / "repo"
    for home, repo in ((python_home, python_repo), (frame_home, frame_repo)):
        _old_state(repo)
        # One state file is already there, newer than the checkout's copy.
        monkeypatch.setattr(settings, "CGR_HOME", home)
        kept = state_dir(repo) / cs.DIR_MTIMES_FILENAME
        kept.parent.mkdir(parents=True)
        kept.write_text("{}", encoding=cs.ENCODING_UTF8)
        os.utime(kept, ns=(1, 1_800_000_000_000_000_000))
    monkeypatch.setattr(settings, "CGR_HOME", python_home)

    python_dir = prepare_state_dir(python_repo)
    prepared = _engine().prepare_state_dir(frame_home, frame_repo)

    assert prepared is not None
    frame_dir, adopted, failed, unavailable = prepared

    assert (failed, unavailable) == ([], None)
    assert adopted == [cs.HASH_CACHE_FILENAME, cs.PARSER_FINGERPRINT_FILENAME]
    assert _listing(Path(frame_dir)) == _listing(python_dir)
    # Moved, so gone from the checkout, but for the lock an older cgr takes.
    assert _listing(frame_repo) == _listing(python_repo)
    assert sorted(path.name for path in frame_repo.iterdir()) == [cs.SYNC_LOCK_FILENAME]


@needs_links
def test_a_link_under_a_state_file_s_name_is_neither_moved_nor_followed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("not cgr's", encoding=cs.ENCODING_UTF8)
    (repo / cs.HASH_CACHE_FILENAME).symlink_to(secret)

    prepared = _engine().prepare_state_dir(settings.CGR_HOME, repo)

    assert prepared is not None
    frame_dir, adopted, _failed, _unavailable = prepared

    assert adopted == []
    assert not (Path(frame_dir) / cs.HASH_CACHE_FILENAME).exists()
    assert (repo / cs.HASH_CACHE_FILENAME).is_symlink()


def test_the_frame_holds_python_s_state_names() -> None:
    engine_type = type(_engine())
    assert getattr(engine_type, "HASH_CACHE_FILENAME") == cs.HASH_CACHE_FILENAME
    assert getattr(engine_type, "DIR_MTIMES_FILENAME") == cs.DIR_MTIMES_FILENAME
    assert getattr(engine_type, "HASH_CACHE_UNREADABLE") == cs.HASH_CACHE_UNREADABLE
    assert getattr(engine_type, "STATE_DIRNAME") == cs.CGR_STATE_DIRNAME
    assert getattr(engine_type, "EDIT_HISTORY_FILENAME") == cs.EDIT_HISTORY_FILENAME
    assert frozenset(getattr(engine_type, "STATE_LOCK_FILENAMES")) == (
        cs.CGR_STATE_LOCK_FILENAMES
    )
    assert getattr(engine_type, "PROJECT_NAME_DIGEST_MARKER") == (
        cs.PROJECT_NAME_DIGEST_MARKER
    )
    assert getattr(engine_type, "PROJECT_NAME_DIGEST_LEN") == cs.PROJECT_NAME_DIGEST_LEN
    assert getattr(engine_type, "INT_MAX_STR_DIGITS") == sys.get_int_max_str_digits()


# --- the hash cache and the folder times -------------------------------------

ODD_NAMES = {
    "a.py": "0" * 32,
    "pkg/é 名前.py": "1" * 32,
    'q"uote\\back/slash.py': cs.HASH_CACHE_UNREADABLE,
    "tab\there\x7f\x01.py": "2" * 32,
    "😀/emoji.py": "3" * 32,
}
# Documents the readers must agree on, each as the bytes of the file.
CACHE_DOCUMENTS = {
    "empty object": b"{}",
    "empty file": b"",
    "not utf-8": b'\xff{"a.py": "x"}',
    "byte-order mark": b'\xef\xbb\xbf{"a.py": "x"}',
    "not an object": b'["a.py", "x"]',
    "a string": b'"a.py"',
    "trailing data": b'{"a.py": "x"} {}',
    "trailing comma": b'{"a.py": "x",}',
    "truncated": b'{"a.py": "x", "b.py": "y',
    "duplicates": b'{"a.py": 1, "b.py": "y", "a.py": "x", "b.py": null}',
    "non-string values": b'{"a.py": 1, "b.py": true, "c.py": null, "d.py": [], "e.py": "x"}',
    "nan and infinity": b'{"a.py": NaN, "b.py": -Infinity, "c.py": "x"}',
    "names from before 3722": b'{"a\\udcff.py": "x", "b.py": "\\udcfe", "c.py": "y"}',
    "surrogate pair": b'{"\\ud83d\\ude00.py": "x"}',
    "escapes": b'{"a\\n\\t\\"\\\\\\/\\b\\f\\r\\u00e9.py": "x"}',
    "bad escape": b'{"a\\x.py": "x"}',
    "control character": b'{"a\tb.py": "x"}',
    "long integer": b'{"a.py": "x", "n": ' + b"1" * 4300 + b"}",
    "too long an integer": b'{"a.py": "x", "n": ' + b"1" * 4301 + b"}",
    "huge float": b'{"a.py": "x", "n": 1' + b"0" * 400 + b".0}",
    "nested": b'{"a.py": {"b": [1, {"c": [[], {}]}]}, "b.py": "y"}',
    "deep": b'{"a.py": "x", "n": ' + b"[" * 300 + b"]" * 300 + b"}",
    "past python's recursion limit": (
        b'{"a.py": "x", "n": ' + b"[" * 20_000 + b"]" * 20_000 + b"}"
    ),
    "whitespace": b' \r\n\t{ "a.py" : "x" , "b.py":"y"}\n\r ',
    "numbers ending early": b'{"a.py": 1.}',
    "leading zero": b'{"a.py": 01}',
}
MTIME_DOCUMENTS = {
    "times": b'{".": 1795611622.6097696, "src": 1, "lib": -0, "x": 1e400}',
    "booleans and strings": b'{".": true, "a": false, "b": "1.5", "c": null}',
    "overflowing integer": b'{".": 1.5, "a": 1' + b"0" * 400 + b"}",
    "overflow under a dropped name": b'{".": 1.5, "a\\udcff": 1' + b"0" * 400 + b"}",
    "nan": b'{".": NaN, "a": -Infinity}',
    "duplicates": b'{".": 1, "a": 2, ".": 3}',
    "not an object": b"[1.5]",
    "not utf-8": b'{".": 1.5, "\xff": 2}',
}


@pytest.mark.parametrize(
    "document", CACHE_DOCUMENTS.values(), ids=CACHE_DOCUMENTS.keys()
)
def test_the_frame_reads_a_hash_cache_as_python_does(
    tmp_path: Path, document: bytes
) -> None:
    cache = tmp_path / cs.HASH_CACHE_FILENAME
    cache.write_bytes(document)

    entries, _error = _engine().read_hash_cache(cache)

    assert entries == list(_load_hash_cache(cache).items())


def _unusable_hashes(md5: str) -> dict[str, str | None]:
    """A cache naming `a.py` by `md5`, two well-named files whose hashes are
    unusable, and a name from before #3722."""
    return {"a.py": md5, "b.py": "bad\udcff", "c.py": None, "pkg/bad\udcff.py": md5}


def test_a_bad_name_is_dropped_and_a_bad_hash_read_as_unreadable_by_both(
    tmp_path: Path,
) -> None:
    # A well-named file whose hash is unusable keeps its name, marked as a
    # file the last run could not read: dropped, an edit to it went unseen
    # by the in-sync check and it was parsed as new, its old definitions
    # kept. A name that is not UTF-8 is dropped.
    md5 = "0123456789abcdef" * 2
    cache = tmp_path / cs.HASH_CACHE_FILENAME
    cache.write_text(json.dumps(_unusable_hashes(md5)), encoding=cs.ENCODING_UTF8)
    expected = {
        "a.py": md5,
        "b.py": cs.HASH_CACHE_UNREADABLE,
        "c.py": cs.HASH_CACHE_UNREADABLE,
    }

    assert _load_hash_cache(cache) == expected
    assert _engine().read_hash_cache(cache) == (list(expected.items()), None)


def test_a_missing_cache_reads_as_none_and_says_nothing(tmp_path: Path) -> None:
    assert _engine().read_hash_cache(tmp_path / "missing") == ([], None)
    assert _engine().read_dir_mtimes(tmp_path / "missing") == []
    assert _load_hash_cache(tmp_path / "missing") == {}


@pytest.mark.parametrize(
    "document", MTIME_DOCUMENTS.values(), ids=MTIME_DOCUMENTS.keys()
)
def test_the_frame_reads_folder_times_as_python_does(
    tmp_path: Path, document: bytes
) -> None:
    cache = tmp_path / cs.DIR_MTIMES_FILENAME
    cache.write_bytes(document)

    # By repr, so a NaN on both sides is the same.
    assert repr(_engine().read_dir_mtimes(cache)) == repr(
        list(_load_dir_mtimes(cache).items())
    )


@pytest.mark.parametrize(
    "hashes",
    [{}, {"a.py": "0" * 32}, ODD_NAMES],
    ids=["empty", "one", "odd names"],
)
@pytest.mark.parametrize(
    "stamp",
    [1_795_611_622.6097696, 1_700_000_000.0, 0.0, 1_234_567_890.123_456_7],
)
def test_a_cache_written_by_either_is_the_other_s_byte_for_byte(
    tmp_path: Path, hashes: dict[str, str], stamp: float
) -> None:
    python_cache = tmp_path / "python" / cs.HASH_CACHE_FILENAME
    frame_cache = tmp_path / "frame" / cs.HASH_CACHE_FILENAME

    assert _publish_hash_cache(python_cache, hashes, stamp)
    _engine().publish_hash_cache(frame_cache, list(hashes.items()), stamp)
    # Before either is read, which moves its access time.
    python_stat, frame_stat = python_cache.stat(), frame_cache.stat()

    assert frame_cache.read_bytes() == python_cache.read_bytes()
    # The watermark stamped to the nanosecond, as `os.utime` stamps it.
    assert frame_stat.st_mtime_ns == python_stat.st_mtime_ns
    assert frame_stat.st_atime_ns == python_stat.st_atime_ns
    assert abs(frame_stat.st_mtime - stamp) < 1e-6
    # Each reads what the other wrote.
    assert _load_hash_cache(frame_cache) == hashes
    assert _engine().read_hash_cache(python_cache) == (list(hashes.items()), None)
    assert _engine().hash_cache_text(list(hashes.items())) == json.dumps(
        hashes, indent=2
    )
    # Nothing is left beside the cache.
    assert [path.name for path in frame_cache.parent.iterdir()] == [
        cs.HASH_CACHE_FILENAME
    ]


def test_a_cache_the_frame_cannot_publish_leaves_the_last_one(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("", encoding=cs.ENCODING_UTF8)

    with pytest.raises(OSError):
        _engine().publish_hash_cache(
            blocked / cs.HASH_CACHE_FILENAME, [("a.py", "x")], 1.0
        )
    assert blocked.read_text(encoding=cs.ENCODING_UTF8) == ""


def _random_floats(count: int) -> Iterator[float]:
    rng = random.Random(3629)
    for _ in range(count):
        yield struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0]
        yield rng.uniform(1.5e9, 1.9e9)
        yield rng.randint(0, 2**40) + rng.randint(0, 10**9) * 1e-9
    # Exactly halfway between two shortest strings, which repr rounds to even.
    yield from (26_363_981_746_409.3125, 1_059_438_285_926_254.25, 0.5, 2.5)
    yield from (0.0, -0.0, 5e-324, 1e16, 1e15, 1e-4, 1e-5, 2.0**1023)
    yield from (math.nan, math.inf, -math.inf)


def test_folder_times_are_written_as_python_writes_them(tmp_path: Path) -> None:
    floats = list(_random_floats(20_000))
    mtimes = {f"d{index}": value for index, value in enumerate(floats)}
    python_file = tmp_path / "python" / cs.DIR_MTIMES_FILENAME
    frame_file = tmp_path / "frame" / cs.DIR_MTIMES_FILENAME

    _save_dir_mtimes(python_file, mtimes)
    _engine().save_dir_mtimes(frame_file, list(mtimes.items()))

    assert frame_file.read_bytes() == python_file.read_bytes()
    assert repr(_engine().read_dir_mtimes(python_file)) == repr(
        list(_load_dir_mtimes(frame_file).items())
    )
    assert _engine().dir_mtimes_text([]) == json.dumps({})


def test_folder_times_are_python_s_floats_to_the_nanosecond(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # Times whose nanoseconds a float cannot hold exactly.
    for number, folder in enumerate([repo, repo / "pkg", repo / "pkg" / "sub"]):
        os.utime(folder, ns=(1, 1_795_611_622_609_769_582 + 111_111_111 * number))
    changes = _frame_changes(_updater(repo))

    assert changes.mtimes == (
        (cs.ROOT_DIR_KEY, os.stat(repo).st_mtime),
        ("pkg", os.stat(repo / "pkg").st_mtime),
        ("pkg/sub", os.stat(repo / "pkg" / "sub").st_mtime),
    )


# --- runs ----------------------------------------------------------------------


def _frame_changes(updater: GraphUpdater) -> engine_frame.FileChanges:
    """The frame's change detection for a project run of `updater`'s
    repository from its current state, read as a run reads it."""
    inputs = updater._frame_state_inputs()
    made = _engine()
    baseline = made.change_baseline(
        inputs.cache_path, False, False, False, False, False, []
    )
    changes = engine_frame.frame_changes(baseline, inputs, None, frozenset())
    assert changes is not None
    return changes


class _Recorded:
    """A `PlanCheck` that keeps what it is told, and counts what it compares."""

    def __init__(self, compares: bool = True) -> None:
        self._compares = compares
        self.compared = 0
        self.plans: list[list[str]] = []
        self.changes: list[list[str]] = []
        self.structures: list[list[str]] = []

    def compares(self) -> bool:
        return self._compares

    def differs(self, repo_path: Path, difference: Sequence[str]) -> None:
        self.plans.append(list(difference))

    def compares_changes(self) -> bool:
        self.compared += self._compares
        return self._compares

    def changes_differ(self, repo_path: Path, difference: Sequence[str]) -> None:
        self.changes.append(list(difference))

    def compares_structure(self) -> bool:
        return self._compares

    def structure_differs(self, repo_path: Path, difference: Sequence[str]) -> None:
        self.structures.append(list(difference))


@pytest.fixture
def checked(monkeypatch: pytest.MonkeyPatch) -> _Recorded:
    """Every run compared, the frame off, any difference kept."""
    check = _Recorded()
    monkeypatch.setattr(engine_frame, "ENABLED", False)
    monkeypatch.setattr(engine_frame, "CHECK", check)
    return check


@pytest.fixture
def answers(
    checked: _Recorded, monkeypatch: pytest.MonkeyPatch
) -> list[engine_frame.FileChanges]:
    """Each run's answers in pairs, Python's then the frame's, every run
    compared."""
    found: list[engine_frame.FileChanges] = []
    original = engine_frame.changes_for_run

    def recording(
        baseline: engine_frame.ChangeBaseline | None,
        inputs: engine_frame.StateInputs,
        single: tuple[Path, str | None] | None,
        graph_paths: frozenset[str] | None,
        python: engine_frame.FileChanges,
    ) -> engine_frame.FileChanges:
        assert baseline is not None
        frame = engine_frame.frame_changes(baseline, inputs, single, graph_paths)
        assert frame is not None
        found.extend((python, frame))
        return original(baseline, inputs, single, graph_paths, python)

    monkeypatch.setattr(engine_frame, "changes_for_run", recording)
    return found


def _in_sync_both(updater: GraphUpdater) -> tuple[bool, bool | None]:
    """Python's in-sync verdict and the frame's, for the state on disk."""
    return updater._files_in_sync(), engine_frame.frame_in_sync(
        updater._frame_state_inputs(), discard_cache=False
    )


def _sync(repo: Path) -> GraphUpdater:
    return create_and_run_updater(repo, _MockIngestor())


def _cache(repo: Path) -> Path:
    return state_file(repo, cs.HASH_CACHE_FILENAME)


def _replace_cache(repo: Path, hashes: Mapping[str, str | None]) -> None:
    """Write `hashes` over the published cache, keeping its mtime."""
    cache = _cache(repo)
    stamp = cache.stat()
    cache.write_text(json.dumps(hashes, indent=2), encoding=cs.ENCODING_UTF8)
    os.utime(cache, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))


def _rewrite_cache(repo: Path, change: Callable[[dict[str, str]], None]) -> None:
    """Change the published cache, keeping its mtime."""
    hashes = json.loads(_cache(repo).read_text(encoding=cs.ENCODING_UTF8))
    change(hashes)
    _replace_cache(repo, hashes)


def _flat_repo(tmp_path: Path) -> Path:
    """A repository of `a.py`, `b.py` and `c.py`, all in its root."""
    repo = tmp_path / "repo"
    for name in ("a", "b", "c"):
        _write(repo, f"{name}.py", f"def {name}():\n    return 1\n")
    return repo


def _unusable_cache_and_an_edit(repo: Path) -> Path:
    """The synced `repo`'s cache replaced by `_unusable_hashes` under its
    own mtime, then `b.py` edited in place with its folder's time held, so
    only `b.py`'s cache entry can tell the in-sync check of the edit."""
    md5 = json.loads(_cache(repo).read_text(encoding=cs.ENCODING_UTF8))["a.py"]
    _replace_cache(repo, _unusable_hashes(md5))
    folder = repo.stat()
    edited = _write(repo, "b.py", "def b():\n    return 2\n")
    os.utime(repo, ns=(folder.st_atime_ns, folder.st_mtime_ns))
    return edited


def _push_back(path: Path, before: Path) -> None:
    """Give `path` an mtime under `before`'s, as `cp -p` or `tar -x` do."""
    stamp = before.stat().st_mtime_ns - 10**9
    os.utime(path, ns=(stamp, stamp))


class TestInSync:
    """Each case, the frame's verdict next to Python's, and the expected one."""

    def _case(self, repo: Path, expected: bool) -> None:
        updater = _updater(repo)
        assert _in_sync_both(updater) == (expected, expected)

    def test_an_unchanged_repository_is_in_sync(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _sync(repo)

        self._case(repo, True)

    def test_a_file_edited_with_its_mtime_pushed_back_is_hashed(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        edited = _write(repo, "pkg/b.py", "def b():\n    return 20\n")
        _push_back(edited, _cache(repo))

        # Its inode change time is later than the cache, so the md5 decides.
        self._case(repo, False)

    def test_a_file_rewritten_unchanged_with_its_mtime_pushed_back_is_in_sync(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        same = _write(repo, "pkg/b.py", "def b():\n    return 2\n")
        _push_back(same, _cache(repo))

        self._case(repo, True)

    def test_a_folder_with_a_file_added_or_removed_is_not(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        added = _write(repo, "pkg/sub/d.py", "D = 4\n")
        self._case(repo, False)
        added.unlink()
        (repo / "pkg" / "sub" / "c.py").unlink()

        self._case(repo, False)

    def test_a_folder_whose_mtime_moved_with_no_indexable_change_is(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        _write(repo, "pkg/scratch.pyc", "")
        (repo / "pkg" / "node_modules").mkdir()
        _write(repo, "pkg/sub/tmp.py", "").unlink()
        time.sleep(0.01)
        os.utime(repo / "pkg")

        self._case(repo, True)

    def test_an_edit_in_place_to_a_file_whose_cached_hash_is_unusable_is_seen(
        self, tmp_path: Path
    ) -> None:
        repo = _flat_repo(tmp_path)
        _sync(repo)
        _unusable_cache_and_an_edit(repo)

        # Dropped, the entry left only `a.py`, which is unchanged.
        self._case(repo, False)

    def test_an_unreadable_entry_is_not(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        _rewrite_cache(
            repo, lambda hashes: hashes.update({"a.py": cs.HASH_CACHE_UNREADABLE})
        )

        self._case(repo, False)

    @needs_links
    def test_a_link_where_a_cached_file_was_is_not(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        recorded = os.stat(repo).st_mtime_ns
        target = repo / "a.py"
        moved = tmp_path / "a.py"
        shutil.move(target, moved)
        target.symlink_to(moved)
        # The folder's time as recorded, so the file's own check decides.
        os.utime(repo, ns=(recorded, recorded))

        self._case(repo, False)

    def test_a_cache_mtime_in_the_future_counts_as_zero(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        future = time.time() + 3600
        os.utime(_cache(repo), (future, future))
        # Every file is hashed, and every hash still matches.
        self._case(repo, True)
        baseline = _engine().change_baseline(
            _cache(repo), False, False, False, False, False, []
        )

        assert _trustworthy_cache_mtime(_cache(repo)) == 0.0
        assert baseline.cache_mtime == 0.0

    def test_a_discarded_cache_or_missing_folder_times_are_not(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        updater = _updater(repo)
        assert (
            engine_frame.frame_in_sync(
                updater._frame_state_inputs(), discard_cache=True
            )
            is False
        )
        state_file(repo, cs.DIR_MTIMES_FILENAME).unlink()

        self._case(repo, False)


class TestRuns:
    def test_each_run_is_compared_and_agrees(
        self,
        tmp_path: Path,
        checked: _Recorded,
        answers: list[engine_frame.FileChanges],
    ) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        _sync(repo)
        _write(repo, "pkg/b.py", "def b():\n    return 20\n")
        force_mtime_after_cache(repo, repo / "pkg" / "b.py")
        _write(repo, "new.py", "N = 1\n")
        (repo / "pkg" / "sub" / "c.py").unlink()
        _sync(repo)

        assert (checked.plans, checked.changes) == ([], [])
        # Two runs' change detection, and one in-sync check per later run.
        assert checked.compared == 4
        python, frame = answers[-2:]
        assert engine_frame.changes_difference(python, frame) == []
        assert frame.changed == (("new.py", True), ("pkg/b.py", False))
        assert frame.deleted == ("pkg/sub/c.py",)
        assert frame.deleted_first == ("pkg/sub/c.py",)
        assert frame.cache_text == json.dumps(dict(python.hashes), indent=2)
        assert frame.mtimes is not None
        assert [key for key, _mtime in frame.mtimes] == [".", "pkg", "pkg/sub"]

    def test_a_hash_at_or_under_the_cache_mtime_is_kept_unread(
        self, tmp_path: Path, answers: list[engine_frame.FileChanges]
    ) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        # The cache vouches for a hash no read of the file gives: kept, it
        # proves the file was not read.
        _rewrite_cache(repo, lambda hashes: hashes.update({"a.py": "f" * 32}))
        rehashed = _write(repo, "pkg/b.py", "def b():\n    return 2\n")
        force_mtime_after_cache(repo, rehashed)
        _write(repo, "touch.py", "")

        _sync(repo)

        python, frame = answers[-2:]
        assert dict(frame.hashes)["a.py"] == "f" * 32
        assert dict(python.hashes)["a.py"] == "f" * 32
        assert dict(frame.hashes)["pkg/b.py"] == dict(python.hashes)["pkg/b.py"]
        assert engine_frame.changes_difference(python, frame) == []

    def test_files_whose_cached_hashes_are_unusable_are_re_parsed_as_known(
        self,
        tmp_path: Path,
        checked: _Recorded,
        answers: list[engine_frame.FileChanges],
    ) -> None:
        repo = _flat_repo(tmp_path)
        _sync(repo)
        edited = _unusable_cache_and_an_edit(repo)

        _sync(repo)

        assert checked.changes == []
        python, frame = answers[-2:]
        assert engine_frame.changes_difference(python, frame) == []
        # Hashed whatever their times, and known, so their old definitions
        # go first; nothing is deleted.
        assert frame.changed == (("b.py", False), ("c.py", False))
        rehashed = hashlib.md5(edited.read_bytes()).hexdigest()
        assert dict(frame.hashes)["b.py"] == dict(python.hashes)["b.py"] == rehashed
        assert frame.deleted == ()

    @needs_permissions
    def test_an_unreadable_file_at_or_under_the_cache_mtime_keeps_its_hash(
        self, tmp_path: Path, answers: list[engine_frame.FileChanges]
    ) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        recorded = json.loads(_cache(repo).read_text(encoding=cs.ENCODING_UTF8))
        locked = repo / "a.py"
        above = repo / "pkg" / "b.py"
        locked.chmod(0)
        force_mtime_after_cache(repo, above)
        above.chmod(0)
        _write(repo, "touch.py", "")
        try:
            _sync(repo)
        finally:
            locked.chmod(0o644)
            above.chmod(0o644)

        python, frame = answers[-2:]
        assert engine_frame.changes_difference(python, frame) == []
        assert dict(frame.hashes)["a.py"] == recorded["a.py"]
        assert dict(frame.hashes)["pkg/b.py"] == cs.HASH_CACHE_UNREADABLE
        assert frame.unreadable == ("pkg/b.py",)

    def test_corrupt_empty_and_missing_caches_are_full_builds_on_both(
        self, tmp_path: Path, answers: list[engine_frame.FileChanges]
    ) -> None:
        repo = _repo(tmp_path)
        for state in (b"{not json", b"", b"\xff", None):
            _sync(repo)
            cache = _cache(repo)
            if state is None:
                cache.unlink()
            else:
                cache.write_bytes(state)
            _sync(repo)
            python, frame = answers[-2:]
            assert engine_frame.changes_difference(python, frame) == []
            assert frame.full_build is True

    def test_the_watermark_stamps_the_cache_a_project_run_publishes(
        self, tmp_path: Path, checked: _Recorded, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(engine_frame, "ENABLED", True)
        repo = _repo(tmp_path)
        before = time.time()
        updater = _sync(repo)

        stamp = _cache(repo).stat().st_mtime
        # The earlier of the two watermarks, each taken before its hashing.
        assert before <= stamp <= time.time()
        assert updater.committed
        assert checked.changes == []


def _single(target: Path) -> GraphUpdater:
    updater = _updater(target)
    updater.run()
    return updater


class TestSingleFile:
    def test_it_merges_into_a_usable_cache_keeping_its_mtime(
        self,
        tmp_path: Path,
        checked: _Recorded,
        answers: list[engine_frame.FileChanges],
    ) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        mtimes = state_file(repo, cs.DIR_MTIMES_FILENAME).read_bytes()
        before = _cache(repo).stat()
        target = _write(repo, "pkg/b.py", "def b():\n    return 21\n")

        _single(target)

        python, frame = answers[-2:]
        assert engine_frame.changes_difference(python, frame) == []
        assert checked.changes == []
        assert frame.merge_base is not None and dict(frame.merge_base).keys() == {
            "a.py",
            "pkg/b.py",
            "pkg/sub/c.py",
        }
        assert frame.stamp == before.st_mtime
        assert (frame.mtimes, frame.mtimes_text, frame.deleted) == (None, None, ())
        assert frame.changed == (("pkg/b.py", False),)
        # Published as Python publishes it: its previous mtime kept, the
        # folder times untouched.
        assert _cache(repo).stat().st_mtime == before.st_mtime
        assert state_file(repo, cs.DIR_MTIMES_FILENAME).read_bytes() == mtimes

    @pytest.mark.parametrize("state", [None, b"", b"{oops", b"{}"])
    def test_it_publishes_nothing_without_a_usable_cache(
        self,
        tmp_path: Path,
        answers: list[engine_frame.FileChanges],
        state: bytes | None,
    ) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        if state is None:
            _cache(repo).unlink()
        else:
            _cache(repo).write_bytes(state)

        _single(repo / "pkg" / "b.py")

        python, frame = answers[-2:]
        assert engine_frame.changes_difference(python, frame) == []
        assert frame.cache_text is None

    def test_a_target_gone_is_its_only_deletion(
        self,
        tmp_path: Path,
        answers: list[engine_frame.FileChanges],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        target = repo / "pkg" / "b.py"
        updater = _updater(target)
        original = GraphUpdater._prepare_flux_stems

        def then_delete(
            self: GraphUpdater,
            eligible_files: list[tuple[Path, str]],
            old_hashes: dict[str, str],
            is_full_build: bool,
        ) -> set[str]:
            # Once the run has its file, before it hashes it; the harness's
            # grammar probe collects the file too, and must still find it.
            target.unlink()
            return original(self, eligible_files, old_hashes, is_full_build)

        monkeypatch.setattr(GraphUpdater, "_prepare_flux_stems", then_delete)
        updater.run()

        python, frame = answers[-2:]
        assert engine_frame.changes_difference(python, frame) == []
        assert (frame.gone, frame.deleted, frame.deleted_first) == (
            "pkg/b.py",
            ("pkg/b.py",),
            (),
        )
        assert "pkg/b.py" not in json.loads(frame.cache_text or "{}")


# --- choosing the answer --------------------------------------------------------


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frame on, as it will be in production, with no check in place."""
    monkeypatch.setattr(engine_frame, "ENABLED", True)
    monkeypatch.setattr(engine_frame, "CHECK", None)


def _planted(monkeypatch: pytest.MonkeyPatch, rel_path: str) -> None:
    """The frame's change detection, planted with a difference: `rel_path`
    not among the files to re-parse, and absent from its cache."""
    original = engine_frame.frame_changes

    def planted(
        baseline: engine_frame.ChangeBaseline,
        inputs: engine_frame.StateInputs,
        single: tuple[Path, str | None] | None,
        graph_paths: frozenset[str] | None,
    ) -> engine_frame.FileChanges | None:
        changes = original(baseline, inputs, single, graph_paths)
        assert changes is not None
        hashes = tuple(pair for pair in changes.hashes if pair[0] != rel_path)
        return changes._replace(
            hashes=hashes,
            changed=tuple(pair for pair in changes.changed if pair[0] != rel_path),
            cache_text=json.dumps(dict(hashes), indent=2),
        )

    monkeypatch.setattr(engine_frame, "frame_changes", planted)


def _warnings(run: Callable[[], object]) -> list[str]:
    warned: list[str] = []
    sink = logger.add(lambda message: warned.append(str(message)), level="WARNING")
    try:
        run()
    finally:
        logger.remove(sink)
    return warned


class TestChoosing:
    def test_off_and_unchecked_the_frame_is_never_asked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(engine_frame, "ENABLED", False)
        monkeypatch.setattr(engine_frame, "CHECK", None)

        def refusing(*_args: object) -> None:
            raise AssertionError("the frame was asked")

        monkeypatch.setattr(engine_frame, "frame_changes", refusing)
        monkeypatch.setattr(engine_frame, "frame_in_sync", refusing)
        repo = _repo(tmp_path)
        _sync(repo)

        assert _sync(repo).skipped_because_in_sync is True

    def test_on_and_equal_the_run_goes_on_with_the_frame_s_answer(
        self, tmp_path: Path, production: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        used: list[tuple[engine_frame.FileChanges, engine_frame.FileChanges]] = []
        original = engine_frame.changes_for_run

        def recording(
            baseline: engine_frame.ChangeBaseline | None,
            inputs: engine_frame.StateInputs,
            single: tuple[Path, str | None] | None,
            graph_paths: frozenset[str] | None,
            python: engine_frame.FileChanges,
        ) -> engine_frame.FileChanges:
            chosen = original(baseline, inputs, single, graph_paths, python)
            used.append((python, chosen))
            return chosen

        monkeypatch.setattr(engine_frame, "changes_for_run", recording)
        repo = _repo(tmp_path)

        warned = _warnings(lambda: _sync(repo))

        assert [line for line in warned if "differs from Python's" in line] == []
        ((python, chosen),) = used
        assert chosen is not python
        assert engine_frame.changes_difference(python, chosen) == []

    def test_on_a_difference_one_warning_names_it_and_python_s_is_used(
        self, tmp_path: Path, production: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path)
        _planted(monkeypatch, "pkg/b.py")

        warned = _warnings(lambda: _sync(repo))

        differs = [line for line in warned if "change detection" in line]
        assert len(differs) == 1
        assert str(repo) in differs[0]
        assert "pkg/b.py" in differs[0]
        recorded = json.loads(_cache(repo).read_text(encoding=cs.ENCODING_UTF8))
        assert sorted(recorded) == ["a.py", "pkg/b.py", "pkg/sub/c.py"]

    def test_an_in_sync_verdict_that_differs_falls_back_too(
        self, tmp_path: Path, production: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path)
        _sync(repo)
        monkeypatch.setattr(engine_frame, "frame_in_sync", lambda *_args: False)

        warned: list[GraphUpdater] = []
        messages = _warnings(lambda: warned.append(_sync(repo)))

        assert warned[0].skipped_because_in_sync is True
        assert sum("change detection" in line for line in messages) == 1

    def test_a_frame_that_raises_falls_back(
        self, tmp_path: Path, production: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raising(*_args: object) -> None:
            raise ValueError("refused")

        monkeypatch.setattr(engine_frame, "frame_changes", raising)
        repo = _repo(tmp_path)

        warned = _warnings(lambda: _sync(repo))

        assert sum("raised\trefused" in line for line in warned) == 1
        assert len(json.loads(_cache(repo).read_text(encoding=cs.ENCODING_UTF8))) == 3

    @pytest.mark.parametrize("enabled", [False, True])
    def test_a_check_is_told_of_the_difference_instead(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        checked: _Recorded,
        enabled: bool,
    ) -> None:
        monkeypatch.setattr(engine_frame, "ENABLED", enabled)
        _planted(monkeypatch, "pkg/b.py")
        repo = _repo(tmp_path)

        _sync(repo)

        (difference,) = checked.changes
        assert any(line.startswith("- changed\tpkg/b.py\t") for line in difference)
        # Strict: with the frame on, the run goes on with the frame's answer.
        recorded = json.loads(_cache(repo).read_text(encoding=cs.ENCODING_UTF8))
        assert ("pkg/b.py" in recorded) is not enabled

    def test_a_check_that_cannot_compare_leaves_the_frame_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        check = _Recorded(compares=False)
        monkeypatch.setattr(engine_frame, "ENABLED", True)
        monkeypatch.setattr(engine_frame, "CHECK", check)
        _planted(monkeypatch, "pkg/b.py")
        repo = _repo(tmp_path)

        _sync(repo)

        assert check.changes == []
        assert len(json.loads(_cache(repo).read_text(encoding=cs.ENCODING_UTF8))) == 3


def _shadowed_test_files() -> list[str]:
    return sorted(
        path.name
        for path in TESTS.glob("test_*.py")
        if path.name != Path(__file__).name
        and SHADOW_MARKERS.search(path.read_text(encoding=cs.ENCODING_UTF8))
    )


def test_every_existing_incremental_sync_test_agrees_with_the_frame(
    tmp_path: Path,
) -> None:
    # The existing tests that sync a repository more than once or read its
    # state files, with every run's change detection and every reading and
    # writing of the state files compared with the frame's; any difference
    # fails the test that made it. On two workers of their own: each writes
    # what it compared, added up here.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_XDIST", "PYTEST_CURRENT_TEST"))
    }
    env[CHANGE_SHADOW_DIR_ENV] = str(tmp_path)
    files = _shadowed_test_files()
    assert len(files) > 50, files
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-n",
            "2",
            "-p",
            SHADOW_PLUGIN,
            *(str(TESTS / name) for name in files),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=3600,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-8000:]
    reports = [
        json.loads(path.read_text(encoding=cs.ENCODING_UTF8))
        for path in tmp_path.glob("*.json")
    ]
    assert len(reports) == 3, output[-4000:]
    differences = [line for report in reports for line in report["differences"]]
    assert differences == [], differences[:20]
    total = {
        key: sum(report[key] for report in reports)
        for key in ("changes", "readings", "writings")
    }
    assert total["changes"] > 500, total
    assert total["readings"] > 500, total
    assert total["writings"] > 200, total
