"""Issue #2880: `build_binary.py` logs the binary it built, not `{lang} bindings`.

It reused the parser loader's grammar-bindings messages. The success one was
logged with no arguments, so loguru printed "Successfully built {lang}
bindings" with the braces as they are, and the failure one called the
PyInstaller binary "bindings".
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from unittest.mock import patch

import pytest
from loguru import logger

from build_binary import build_binary
from codebase_rag import logs

BINARY = "code-graph-rag-linux-amd64"
CLEAN = ["PYZ-00.pyz", "libpython3.12.so.1.0"]

Records = list[tuple[str, str]]


@pytest.fixture
def records() -> Iterator[Records]:
    captured: Records = []
    sink = logger.add(
        lambda message: captured.append(
            (message.record["level"].name, message.record["message"])
        ),
        level="DEBUG",
    )
    try:
        yield captured
    finally:
        logger.remove(sink)


@pytest.fixture(autouse=True)
def linux_amd64() -> Iterator[None]:
    with (
        patch("build_binary.platform.system", return_value="Linux"),
        patch("build_binary.platform.machine", return_value="x86_64"),
    ):
        yield


def _succeed() -> bool:
    with (
        patch("build_binary.subprocess.run"),
        patch("build_binary._archive_entries", return_value=CLEAN),
        patch("build_binary.Path.exists", return_value=True),
        patch("build_binary.Path.stat") as stat,
        patch("build_binary.os.chmod"),
    ):
        stat.return_value.st_size = 1
        return build_binary()


def _fail() -> bool:
    error = subprocess.CalledProcessError(1, ["pyinstaller"], "out", "err")
    with patch("build_binary.subprocess.run", side_effect=error):
        return build_binary()


def _messages(records: Records, level: str) -> list[str]:
    return [message for name, message in records if name == level]


def test_a_successful_build_names_the_binary(records: Records) -> None:
    assert _succeed() is True

    assert f"Successfully built binary {BINARY}" in _messages(records, "SUCCESS")


def test_a_failed_build_names_the_binary(records: Records) -> None:
    assert _fail() is False

    assert _messages(records, "ERROR")[0] == (
        f"Failed to build binary {BINARY}: stdout=out, stderr=err"
    )


@pytest.mark.parametrize("build", [_succeed, _fail], ids=["success", "failure"])
def test_no_build_message_leaves_a_placeholder_or_says_bindings(
    records: Records, build: Callable[[], bool]
) -> None:
    build()

    assert not [m for _level, m in records if "{" in m or "bindings" in m]


# Negative: what must not change.


def test_the_grammar_bindings_messages_are_unchanged() -> None:
    assert logs.BUILD_SUCCESS.format(lang="python") == (
        "Successfully built python bindings"
    )
    assert logs.BUILD_FAILED.format(lang="python", stdout="o", stderr="e") == (
        "Failed to build python bindings: stdout=o, stderr=e"
    )


def test_a_successful_build_still_says_it_is_ready(records: Records) -> None:
    _succeed()

    assert _messages(records, "INFO")[0] == f"Building binary: {BINARY}"
    assert _messages(records, "SUCCESS")[-1] == logs.BUILD_READY


def test_a_failed_build_still_logs_its_output(records: Records) -> None:
    _fail()

    assert _messages(records, "ERROR")[1:] == ["STDOUT: out", "STDERR: err"]
