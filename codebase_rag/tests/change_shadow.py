"""Check every run's change detection, and every reading and writing of the
sync's state files a test run makes, against the frame's.

Loaded as a plugin (`-p codebase_rag.tests.change_shadow`):

- every index compares the frame's file plan, in-sync check and change
  detection with Python's, through the check the `both` cells use
  (`engine_frame.CHECK`);
- every `_load_hash_cache` and `_load_dir_mtimes` is compared with what the
  frame reads from the same file;
- every `_publish_hash_cache` is compared with the frame's publishing of the
  same cache under the same stamp, in a scratch folder: the bytes written
  and the mtime and atime they carry, to the nanosecond; every
  `_save_dir_mtimes` with the frame's text for the same folder times.

Python's answer is the one used, so the tests run as they always do; a
difference fails the test that made the call, in its fixtures too, and the
run (`shadows.py`). `test_engine_change_detection.py`
runs the existing incremental-sync tests under it
(docs/reports/PHASE2_FRAME.md, section 13, step 6). Under xdist each worker
counts its own share, so with `CGR_CHANGE_SHADOW_DIR` set each process also
writes its counts and differences there, as `<worker>.json`, for the caller
to add up.

Not compared: a run, a reading or a writing while a test patches what
Python's change detection decides with or reads the files by, or the
functions this plugin wraps, which it then cannot reach.
"""

from __future__ import annotations

import functools
import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from codebase_rag import engine_frame
from codebase_rag import graph_updater as gu
from codebase_rag.graph_updater import DirMtimesCache, FileHashCache
from codebase_rag.tests.engine_modes import CHANGE_SHADOW_DIR_ENV, FramePlanCheck
from codebase_rag.tests.shadows import SHOWN, ShadowFailures

SUMMARY = (
    "change shadow: {changes} change detections, {readings} state readings and "
    "{writings} state writings compared, {differed} differed, {skipped} not "
    "compared"
)
READING_DIFFERS = "{reader}({path}): Python {python}, the frame {frame}"
WRITING_DIFFERS = "{writer}({path}): Python wrote {python}, the frame {frame}"
SCRATCH_PREFIX = "cgr-change-shadow-"
REPORT_FILE = "{worker}.json"
WORKER_ENV = "PYTEST_XDIST_WORKER"
CONTROLLER = "controller"
DIFFERENCES_KEY = "differences"
FAILED = "the frame's change detection or state files differ from Python's:\n"

_differences: list[str] = []
_FAILURES = ShadowFailures("change_shadow", _differences, FAILED)
_counts = {"changes": 0, "readings": 0, "writings": 0, "skipped": 0}
_LOAD_HASHES = gu._load_hash_cache
_LOAD_MTIMES = gu._load_dir_mtimes
_PUBLISH = gu._publish_hash_cache
_SAVE_MTIMES = gu._save_dir_mtimes


class _Check(FramePlanCheck):
    """`engine_frame.CHECK` for the session: every run compared, counted."""

    def compares_changes(self) -> bool:
        if not super().compares_changes():
            _counts["skipped"] += 1
            return False
        _counts["changes"] += 1
        return True


def _engine() -> engine_frame.Engine:
    made = engine_frame.engine()
    assert made is not None and hasattr(made, "read_hash_cache"), (
        "the extension lacks the state files; rebuild the crate"
    )
    return made


def _publish_calls() -> tuple[object, ...]:
    """What Python's publishing writes, stamps and renames by."""
    return (
        os.open,
        os.fstat,
        os.fsync,
        os.utime,
        os.replace,
        json.dump,
        gu._open_exclusive_temp,
        gu._fsync_no_follow,
        gu._stamp_by_path,
        gu._WINDOWS,
    )


_PUBLISH_CALLS = _publish_calls()


def _compared(original: Callable[..., object], name: str) -> bool:
    """Whether a call can be compared: its function unpatched, and the
    session's check able to compare a run now."""
    if getattr(gu, name) is not original or not FramePlanCheck.compares_changes(_CHECK):
        _counts["skipped"] += 1
        return False
    return True


@functools.wraps(_LOAD_HASHES)
def _shadow_load_hashes(cache_path: Path) -> FileHashCache:
    python = _LOAD_HASHES(cache_path)
    if _compared(_shadow_load_hashes, "_load_hash_cache"):
        _counts["readings"] += 1
        frame, _error = _engine().read_hash_cache(cache_path)
        if list(python.items()) != frame:
            _differences.append(
                READING_DIFFERS.format(
                    reader="_load_hash_cache",
                    path=cache_path,
                    python=list(python.items()),
                    frame=frame,
                )
            )
    return python


@functools.wraps(_LOAD_MTIMES)
def _shadow_load_mtimes(cache_path: Path) -> DirMtimesCache:
    python = _LOAD_MTIMES(cache_path)
    if _compared(_shadow_load_mtimes, "_load_dir_mtimes"):
        _counts["readings"] += 1
        frame = _engine().read_dir_mtimes(cache_path)
        # By repr, so a NaN read on both sides is the same.
        if repr(list(python.items())) != repr(frame):
            _differences.append(
                READING_DIFFERS.format(
                    reader="_load_dir_mtimes",
                    path=cache_path,
                    python=list(python.items()),
                    frame=frame,
                )
            )
    return python


@functools.wraps(_PUBLISH)
def _shadow_publish(
    cache_path: Path, hashes: FileHashCache, observed_at: float | None
) -> bool:
    published = _PUBLISH(cache_path, hashes, observed_at)
    if (
        published
        and observed_at is not None
        and _compared(_shadow_publish, "_publish_hash_cache")
        and all(
            now is found or now == found
            for now, found in zip(_publish_calls(), _PUBLISH_CALLS, strict=True)
        )
    ):
        _counts["writings"] += 1
        python = os.stat(cache_path)
        python_bytes = cache_path.read_bytes()
        with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as scratch:
            mirror = Path(scratch) / cache_path.name
            _engine().publish_hash_cache(mirror, list(hashes.items()), observed_at)
            frame = os.stat(mirror)
            frame_bytes = mirror.read_bytes()
        if (python_bytes, python.st_mtime_ns, python.st_atime_ns) != (
            frame_bytes,
            frame.st_mtime_ns,
            frame.st_atime_ns,
        ):
            _differences.append(
                WRITING_DIFFERS.format(
                    writer="_publish_hash_cache",
                    path=cache_path,
                    python=(python_bytes[:200], python.st_mtime_ns),
                    frame=(frame_bytes[:200], frame.st_mtime_ns),
                )
            )
    return published


@functools.wraps(_SAVE_MTIMES)
def _shadow_save_mtimes(cache_path: Path, mtimes: DirMtimesCache) -> None:
    _SAVE_MTIMES(cache_path, mtimes)
    if _compared(_shadow_save_mtimes, "_save_dir_mtimes"):
        _counts["writings"] += 1
        python = json.dumps(mtimes)
        frame = _engine().dir_mtimes_text(list(mtimes.items()))
        if python != frame:
            _differences.append(
                WRITING_DIFFERS.format(
                    writer="_save_dir_mtimes",
                    path=cache_path,
                    python=python,
                    frame=frame,
                )
            )


# Installed before the check is made, so the check takes the wrappers for
# the module's own functions, and a test that patches one is not compared.
for _name, _shadow in (
    ("_load_hash_cache", _shadow_load_hashes),
    ("_load_dir_mtimes", _shadow_load_mtimes),
    ("_publish_hash_cache", _shadow_publish),
    ("_save_dir_mtimes", _shadow_save_mtimes),
):
    setattr(gu, _name, _shadow)
_CHECK = _Check(_differences.append)
engine_frame.CHECK = _CHECK
# Modules that imported a function by name before this plugin loaded hold
# the original, as tests do; the run itself looks them up in its module.
for _module in list(sys.modules.values()):
    _namespace = getattr(_module, "__dict__", None)
    if not isinstance(_namespace, dict) or _namespace.get("__name__") in {
        gu.__name__,
        __name__,
    }:
        continue
    for _name, _value in list(_namespace.items()):
        for _original, _shadow in (
            (_LOAD_HASHES, _shadow_load_hashes),
            (_LOAD_MTIMES, _shadow_load_mtimes),
        ):
            if _value is _original:
                _namespace[_name] = _shadow


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(_FAILURES, _FAILURES.name)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    terminalreporter.write_line(
        SUMMARY.format(
            changes=_counts["changes"],
            readings=_counts["readings"],
            writings=_counts["writings"],
            differed=len(_differences),
            skipped=_counts["skipped"],
        )
    )
    for difference in _differences[:SHOWN]:
        terminalreporter.write_line(difference)


def pytest_sessionfinish(session: pytest.Session) -> None:
    if report_dir := os.environ.get(CHANGE_SHADOW_DIR_ENV):
        worker = os.environ.get(WORKER_ENV, CONTROLLER)
        (Path(report_dir) / REPORT_FILE.format(worker=worker)).write_text(
            json.dumps({**_counts, DIFFERENCES_KEY: _differences}),
            encoding="utf-8",
        )
