"""The CLI logs at INFO unless the user asks otherwise (issue #2353).

loguru's own stderr handler logs at DEBUG, which made `cgr start
--update-graph` print every Cypher query: 438k of ~480k lines on this
repository. The CLI now swaps that one handler for an INFO one, leaves any
sink someone else added alone, and defers to LOGURU_LEVEL when it is set.
"""

import os
import re
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import logs
from codebase_rag import main as cgr_main

_TIMEOUT_SECONDS = 120

_PROBE = (
    "from loguru import logger\n"
    "from codebase_rag import cli\n"
    "{setup}"
    "cli._default_log_level_to_info()\n"
    "logger.debug('probe-debug-line')\n"
    "logger.info('probe-info-line')\n"
)


def _stderr_of(setup: str = "", loguru_level: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "LOGURU_LEVEL"}
    if loguru_level is not None:
        env["LOGURU_LEVEL"] = loguru_level
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(setup=setup)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_TIMEOUT_SECONDS,
        check=True,
        env=env,
    )
    return result.stderr


def test_the_cli_hides_debug_lines_by_default() -> None:
    stderr = _stderr_of()
    assert "probe-info-line" in stderr
    assert "probe-debug-line" not in stderr


def test_loguru_level_from_the_user_wins() -> None:
    stderr = _stderr_of(loguru_level="DEBUG")
    assert "probe-debug-line" in stderr
    assert "probe-info-line" in stderr


def test_a_sink_someone_else_installed_is_left_alone() -> None:
    setup = (
        "import sys\n"
        "logger.remove()\n"
        "logger.add(lambda m: sys.stderr.write('own:' + m.record['message'] + '\\n'))\n"
    )
    stderr = _stderr_of(setup=setup)
    assert "own:probe-debug-line" in stderr
    assert "own:probe-info-line" in stderr


@pytest.fixture
def _chat_log(monkeypatch: pytest.MonkeyPatch) -> Generator[list[str], None, None]:
    lines: list[str] = []
    monkeypatch.setattr(cgr_main, "_rich_log_sink", lambda m: lines.append(str(m)))
    session = cgr_main.app_context.session
    monkeypatch.setattr(session, "target_repo", session.target_repo)
    yield lines
    logger.remove()


def test_the_chat_sink_hides_debug_lines_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _chat_log: list[str]
) -> None:
    monkeypatch.delenv("LOGURU_LEVEL", raising=False)
    cgr_main._setup_common_initialization(str(tmp_path))
    logger.debug("chat-debug-line")
    logger.info("chat-info-line")
    assert any("chat-info-line" in line for line in _chat_log)
    assert not any("chat-debug-line" in line for line in _chat_log)


def test_the_chat_sink_follows_loguru_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _chat_log: list[str]
) -> None:
    monkeypatch.setenv("LOGURU_LEVEL", "DEBUG")
    cgr_main._setup_common_initialization(str(tmp_path))
    logger.debug("chat-debug-line")
    assert any("chat-debug-line" in line for line in _chat_log)


def test_every_indexing_pass_has_its_own_number() -> None:
    numbers = [
        match.group(1)
        for value in vars(logs).values()
        if isinstance(value, str)
        for match in [re.search(r"--- Pass (\d+):", value)]
        if match
    ]
    assert len(numbers) >= 5
    assert len(numbers) == len(set(numbers)), sorted(numbers)
