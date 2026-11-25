"""A shadow plugin's difference fails the test that made it and the run,
made in a fixture and under xdist too (`shadows.py`)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.shadows import child_env

REPO_ROOT = Path(__file__).resolve().parents[2]
PLANTED = "a difference made in a fixture"
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
    assert "1 passed, 1 error" in output, output[-4000:]
    assert PLANTED in output, output[-4000:]
