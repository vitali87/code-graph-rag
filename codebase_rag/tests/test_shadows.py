"""A shadow plugin's difference fails the test that made it and the run,
made in a fixture and under xdist too (`shadows.py`)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.shadows import child_env

REPO_ROOT = Path(__file__).resolve().parents[2]
PLANTED = "a difference made in a fixture"
# pytest's closing line: each outcome's count, then the time taken.
SUMMARY = re.compile(r"^=*\s*((?:\d+ \w+(?:, )?)+) in [\d.]+s\b", re.MULTILINE)
OUTCOME = re.compile(r"(\d+) (\w+)")
PLURALS = {"errors": "error", "warnings": "warning"}
# A fixture records a difference as a shadow does, before its test's call.
PLANTING = """
import importlib

import pytest


@pytest.fixture
def planted():
    importlib.import_module("{plugin}")._differences.append("{planted}")


def test_with_a_difference_made_in_its_fixture(planted):
    pass


def test_without_one():
    pass
"""


@pytest.mark.parametrize("workers", ["0", "2"])
@pytest.mark.parametrize(
    "plugin",
    [
        "codebase_rag.tests.ignore_rules_shadow",
        "codebase_rag.tests.discovery_shadow",
        "codebase_rag.tests.change_shadow",
    ],
)
def test_a_difference_made_in_a_fixture_fails_its_test_and_the_run(
    tmp_path: Path, plugin: str, workers: str
) -> None:
    tests = tmp_path / "test_planted.py"
    tests.write_text(
        PLANTING.format(plugin=plugin, planted=PLANTED), encoding=cs.ENCODING_UTF8
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-n",
            workers,
            "-p",
            plugin,
            # Rooted at its own folder, so collection starts there. Rooted at
            # the checkout (on another drive, on Windows CI) or at a folder
            # above both, pytest collects each folder from there down to the
            # file, the shared temporary folder among them, and another
            # process's folder can vanish between that listing and the stat
            # Windows makes of each one listed (`samefile_nofollow`).
            "--rootdir",
            str(tmp_path),
            # Its own temporary root: under the default, shared with this
            # run's workers, pytest's sweep of old folders can meet one
            # another process is removing, and adds a warning (#3773).
            "--basetemp",
            str(tmp_path / "basetemp"),
            str(tests),
        ],
        cwd=REPO_ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=600,
    )

    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, output[-4000:]
    assert _outcomes(output) == {"passed": 1, "error": 1}, output[-4000:]
    assert PLANTED in output, output[-4000:]


def _outcomes(output: str) -> dict[str, int]:
    """Each outcome pytest's closing line counts, warnings left out: a
    warning is no outcome, and one of the run's own can join the line."""
    lines = SUMMARY.findall(output)
    counts = {
        PLURALS.get(word, word): int(count)
        for count, word in OUTCOME.findall(lines[-1] if lines else "")
    }
    counts.pop("warning", None)
    return counts
