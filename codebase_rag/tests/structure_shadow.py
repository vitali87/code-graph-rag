"""Check every structure pass and every File row a test run makes against
the frame's.

Loaded as a plugin (`-p codebase_rag.tests.structure_shadow`):

- every whole derivation of the structure (`identify_structure()` with no
  scope, a run's or a test's own) compares the frame's map and Package and
  Folder rows with Python's, and every run its File rows, through the
  check the `both` cells use (`engine_frame.CHECK`);
- every `process_generic_file` compares the File row the frame builds under
  Python's map at that moment with the one Python writes.

Python's answer is the one used, so the tests run as they always do; a
difference fails the test that made the call. `test_engine_structure.py`
runs the existing structure tests under it (docs/reports/PHASE2_FRAME.md,
section 13, step 7). Under xdist each worker counts its own share, so with
`CGR_STRUCTURE_SHADOW_DIR` set each process also writes its counts and
differences there, as `<worker>.json`, for the caller to add up.

Not compared: a derivation or a row while a test patches what the structure
pass decides with or resolves paths by, or the method this plugin wraps.
"""

from __future__ import annotations

import functools
import json
import os
from collections.abc import Generator
from pathlib import Path

import pytest

from codebase_rag import engine_frame
from codebase_rag.parsers.structure_processor import StructureProcessor
from codebase_rag.tests.engine_modes import STRUCTURE_SHADOW_DIR_ENV, FramePlanCheck

SUMMARY = (
    "structure shadow: {structures} structures and run File rows and {rows} "
    "File rows compared, {differed} differed, {skipped} not compared"
)
ROW_DIFFERS = "process_generic_file({path}): {difference}"
REPORT_FILE = "{worker}.json"
WORKER_ENV = "PYTEST_XDIST_WORKER"
CONTROLLER = "controller"
DIFFERENCES_KEY = "differences"
_SHOWN = 20

_differences: list[str] = []
_counts = {"structures": 0, "rows": 0, "skipped": 0}
_PROCESS = StructureProcessor.process_generic_file


class _Check(FramePlanCheck):
    """`engine_frame.CHECK` for the session: every structure compared,
    counted."""

    def compares_structure(self) -> bool:
        if not super().compares_structure():
            _counts["skipped"] += 1
            return False
        _counts["structures"] += 1
        return True


@functools.wraps(_PROCESS)
def _shadow_process(self: StructureProcessor, file_path: Path, file_name: str) -> None:
    if StructureProcessor.process_generic_file is not _shadow_process or not (
        FramePlanCheck.compares_structure(_CHECK)
    ):
        _counts["skipped"] += 1
    else:
        _compare_row(self, file_path, file_name)
    _PROCESS(self, file_path, file_name)


def _compare_row(
    processor: StructureProcessor, file_path: Path, file_name: str
) -> None:
    try:
        python = processor.file_row(file_path, file_name)
    except ValueError:
        # Not under the root: the call below raises, as it always has.
        return
    _counts["rows"] += 1
    elements = [
        (key.as_posix(), qn) for key, qn in processor.structural_elements.items()
    ]
    frame = engine_frame.frame_file_rows(
        processor.repo_path, processor.project_name, elements, [file_path]
    )
    assert frame is not None, "the extension lacks the File rows; rebuild the crate"
    if difference := engine_frame.file_rows_difference([file_path], [python], frame):
        _differences.append(
            ROW_DIFFERS.format(path=file_path, difference=difference[:_SHOWN])
        )


setattr(StructureProcessor, "process_generic_file", _shadow_process)
_CHECK = _Check(_differences.append)
engine_frame.CHECK = _CHECK


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    before = len(_differences)
    result = yield
    if found := _differences[before:]:
        pytest.fail(
            "the frame's structure or File rows differ from Python's:\n"
            + "\n".join(found[:_SHOWN]),
            pytrace=False,
        )
    return result


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    terminalreporter.write_line(
        SUMMARY.format(
            structures=_counts["structures"],
            rows=_counts["rows"],
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
    if report_dir := os.environ.get(STRUCTURE_SHADOW_DIR_ENV):
        worker = os.environ.get(WORKER_ENV, CONTROLLER)
        (Path(report_dir) / REPORT_FILE.format(worker=worker)).write_text(
            json.dumps({**_counts, DIFFERENCES_KEY: _differences}),
            encoding="utf-8",
        )
