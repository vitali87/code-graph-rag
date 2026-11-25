"""Check every walk and every reading of the ignore files a test run makes
against the frame's.

Loaded as a plugin (`-p codebase_rag.tests.discovery_shadow`):

- every index compares the frame's file plan with Python's walk, through the
  check the `both` cells use (`engine_frame.CHECK`);
- every other call of `walk_eligible_files`, wherever it was imported, first
  walks the same repository once more with Python and with the frame, and
  compares the two plans, hashes and all;
- every `load_ignore_patterns` is compared with what the frame reads from
  the same ignore files (`engine_frame.frame_ignore_patterns`).

Python's answer is the one used, so the tests run as they always do; a
difference fails the test that made the call. `test_engine_discovery.py`
runs the existing walk and ignore tests under it
(docs/reports/PHASE2_FRAME.md, section 13, step 5).

Not compared: a walk while a test has patched the built-in names or endings
the frame holds copies of, and a reading while a test stands in for git
(`subprocess.run` patched), which the frame runs for real.
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
from collections.abc import Callable, Generator, Iterator
from pathlib import Path

import pytest

from codebase_rag import config, engine_frame
from codebase_rag.config import CgrignorePatterns
from codebase_rag.tests.engine_modes import (
    FramePlanCheck,
    frame_holds_python_constants,
)
from codebase_rag.utils import path_utils

SUMMARY = (
    "discovery shadow: {walks} walks and {readings} ignore readings compared, "
    "{differed} differed, {skipped} made with patched constants or git"
)
READING_DIFFERS = "load_ignore_patterns({repo}): Python {python}, the frame {frame}"
_SHOWN = 20

_differences: list[str] = []
_counts = {"walks": 0, "readings": 0, "skipped": 0}
_WALK = path_utils.walk_eligible_files
_LOAD = config.load_ignore_patterns
_RUN = subprocess.run


class _Check(FramePlanCheck):
    """`engine_frame.CHECK` for the session: every index compared, counted."""

    def compares(self) -> bool:
        if not super().compares():
            _counts["skipped"] += 1
            return False
        _counts["walks"] += 1
        return True


_CHECK = _Check(_differences.append)


def _compare_walk(
    repo_path: Path,
    exclude: frozenset[str] | None,
    unignore: frozenset[str] | None,
) -> None:
    if not _CHECK.compares():
        return
    try:
        python = engine_frame.python_file_plan(repo_path, exclude, unignore)
    except (ValueError, re.error):
        # The walk itself raises on this pattern, as the call below will.
        return
    try:
        frame = engine_frame.frame_file_plan(repo_path, exclude, unignore)
    except ValueError as error:
        _differences.append(f"{repo_path}: the frame raised {error}")
        return
    assert frame is not None, "the extension lacks the walk; rebuild the crate"
    if difference := engine_frame.plan_difference(engine_frame.hashed(python), frame):
        _CHECK.differs(repo_path, difference)


@functools.wraps(_WALK)
def _shadow_walk(
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
    on_dir: Callable[[str, str], None] | None = None,
    on_symlink: Callable[[str], None] | None = None,
    on_special: Callable[[str], None] | None = None,
    on_undecodable: Callable[[str], None] | None = None,
) -> Iterator[tuple[str, str, str]]:
    _compare_walk(repo_path, exclude_paths, unignore_paths)
    yield from _WALK(
        repo_path,
        exclude_paths,
        unignore_paths,
        on_dir=on_dir,
        on_symlink=on_symlink,
        on_special=on_special,
        on_undecodable=on_undecodable,
    )


@functools.wraps(_LOAD)
def _shadow_load(repo_path: Path) -> CgrignorePatterns:
    python = _LOAD(repo_path)
    if subprocess.run is not _RUN or not frame_holds_python_constants():
        _counts["skipped"] += 1
        return python
    _counts["readings"] += 1
    frame = engine_frame.frame_ignore_patterns(repo_path)
    assert frame is not None, "the extension lacks the reader; rebuild the crate"
    if (frame.exclude, frame.unignore) != (python.exclude, python.unignore):
        _differences.append(
            READING_DIFFERS.format(
                repo=repo_path,
                python=(sorted(python.exclude), sorted(python.unignore)),
                frame=(sorted(frame.exclude), sorted(frame.unignore)),
            )
        )
    return python


def _install() -> None:
    engine_frame.CHECK = _CHECK
    # Modules that imported a function by name before this plugin loaded
    # hold the original; the rest will import the shadow. The frame's own
    # module keeps Python's walk, which each walk is compared with, and this
    # one the originals the shadows call.
    kept = {engine_frame.__name__, __name__}
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict) or namespace.get("__name__") in kept:
            continue
        for name, value in list(namespace.items()):
            if value is _WALK:
                namespace[name] = _shadow_walk
            elif value is _LOAD:
                namespace[name] = _shadow_load


_install()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    before = len(_differences)
    result = yield
    if found := _differences[before:]:
        pytest.fail(
            "the frame's walk or ignore files differ from Python's:\n"
            + "\n".join(found[:_SHOWN]),
            pytrace=False,
        )
    return result


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    terminalreporter.write_line(
        SUMMARY.format(
            walks=_counts["walks"],
            readings=_counts["readings"],
            differed=len(_differences),
            skipped=_counts["skipped"],
        )
    )
    for difference in _differences[:_SHOWN]:
        terminalreporter.write_line(difference)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # A difference outside a test's call (in a fixture) fails the run too.
    if _differences and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
