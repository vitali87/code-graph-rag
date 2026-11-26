"""Check every module map a test run fills against the frame's module-name
plan.

Loaded as a plugin (`-p codebase_rag.tests.module_shadow`): every index
whose module map starts empty compares the frame's plan for the files it
parsed with the map Python's definition processor filled, as the `module`
lines of `index.txt`, through the check the `both` cells use
(`engine_frame.CHECK`), with its file plan, change detection and structure.

Python's map is the one used, so the tests run as they always do; a
difference fails the test that made the run. `test_engine_merge.py` runs
the existing hand-off and module-naming tests under it
(docs/reports/PHASE2_FRAME.md, section 13, step 8). Under xdist each worker
counts its own share, so with `CGR_MODULE_SHADOW_DIR` set each process also
writes its counts and differences there, as `<worker>.json`, for the caller
to add up.

Not compared: a run whose map holds names no file of it claimed (one that
reloaded the graph's modules, or a reused updater's), one a file failed
in, and one made while a test patches what Python names modules by or
parses a file into the map through.
"""

from __future__ import annotations

import functools
import json
import os
from collections.abc import Generator, Mapping, Sequence
from pathlib import Path

import pytest

from codebase_rag import engine_frame
from codebase_rag.tests.engine_modes import MODULE_SHADOW_DIR_ENV, FramePlanCheck

SUMMARY = (
    "module shadow: {runs} module maps and {modules} modules compared, "
    "{differed} differed, {skipped} not compared"
)
REPORT_FILE = "{worker}.json"
WORKER_ENV = "PYTEST_XDIST_WORKER"
CONTROLLER = "controller"
DIFFERENCES_KEY = "differences"
_SHOWN = 20

_differences: list[str] = []
_counts = {"runs": 0, "modules": 0, "skipped": 0}
_MODULES_FOR_RUN = engine_frame.modules_for_run


class _Check(FramePlanCheck):
    """`engine_frame.CHECK` for the session: every module map compared,
    counted."""

    def compares_modules(self) -> bool:
        if not super().compares_modules():
            _counts["skipped"] += 1
            return False
        _counts["runs"] += 1
        return True


@functools.wraps(_MODULES_FOR_RUN)
def _counted(
    repo_path: Path,
    project_name: str,
    claims: Sequence[engine_frame.ModuleClaim],
    exclude: frozenset[str] | None,
    unignore: frozenset[str] | None,
    python: Mapping[str, Path],
) -> None:
    _counts["modules"] += len(python)
    _MODULES_FOR_RUN(repo_path, project_name, claims, exclude, unignore, python)


engine_frame.modules_for_run = _counted
_CHECK = _Check(_differences.append)
engine_frame.CHECK = _CHECK


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    before = len(_differences)
    result = yield
    if found := _differences[before:]:
        pytest.fail(
            "the frame's module names, or another of its answers, differ from "
            "Python's:\n" + "\n".join(found[:_SHOWN]),
            pytrace=False,
        )
    return result


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    terminalreporter.write_line(
        SUMMARY.format(
            runs=_counts["runs"],
            modules=_counts["modules"],
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
    if report_dir := os.environ.get(MODULE_SHADOW_DIR_ENV):
        worker = os.environ.get(WORKER_ENV, CONTROLLER)
        (Path(report_dir) / REPORT_FILE.format(worker=worker)).write_text(
            json.dumps({**_counts, DIFFERENCES_KEY: _differences}),
            encoding="utf-8",
        )
