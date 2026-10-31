"""Issue #2917: Ctrl+D at the `cgr start` prompt ends the session.

prompt_toolkit raises EOFError for Ctrl+D on an empty buffer, and when the
terminal's input closes. The chat loop left only on KeyboardInterrupt, so
EOFError reached its generic handler: "An unexpected error occurred:" with
an empty message and a full traceback, then the prompt again, for every
Ctrl+D (and forever once the input had closed).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag import main as m


def _run(tmp_path: Path, answers: list[object]) -> tuple[MagicMock, MagicMock, int]:
    def ask(_prompt: str) -> str:
        # Out of answers: leave, so a loop that never exits cannot hang the
        # test (it re-prompted forever after an EOFError).
        answer = answers.pop(0) if answers else KeyboardInterrupt()
        if isinstance(answer, BaseException):
            raise answer
        return str(answer)

    with (
        patch.object(m, "init_session_log"),
        patch.object(m, "get_multiline_input", side_effect=ask) as prompt,
        patch.object(m.logger, "exception") as logged,
        patch.object(m, "_run_agent_response_loop") as agent,
    ):
        asyncio.run(
            m._run_interactive_loop(
                MagicMock(), [], tmp_path, MagicMock(), "Ask", MagicMock()
            )
        )
    return logged, agent, prompt.call_count


def test_ctrl_d_on_an_empty_prompt_ends_the_session(tmp_path: Path) -> None:
    logged, agent, asked = _run(tmp_path, [EOFError(), "never asked"])

    assert asked == 1
    logged.assert_not_called()
    agent.assert_not_called()


# Negative: what must not change.


def test_exit_and_ctrl_c_still_end_the_session(tmp_path: Path) -> None:
    for answers in (["exit"], [KeyboardInterrupt()]):
        logged, _agent, asked = _run(tmp_path, [*answers, "never asked"])

        assert asked == 1
        logged.assert_not_called()


def test_an_unexpected_error_is_still_logged_and_the_prompt_returns(
    tmp_path: Path,
) -> None:
    logged, _agent, asked = _run(tmp_path, [RuntimeError("boom"), "exit"])

    assert asked == 2
    logged.assert_called_once()
