from __future__ import annotations

import json
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.main import main_single_query

_QUESTION = "What does the parser do?"
_ANSWER = "The parser builds a knowledge graph."


@pytest.fixture
def mock_agent_stack() -> Generator[MagicMock, None, None]:
    agent = MagicMock()
    agent.run = AsyncMock(return_value=MagicMock(output=_ANSWER))
    with (
        patch("codebase_rag.main._setup_common_initialization"),
        patch("codebase_rag.main.connect_memgraph") as mock_connect,
        patch(
            "codebase_rag.main._initialize_services_and_agent",
            return_value=(agent, [], ""),
        ),
    ):
        mock_connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield agent


def test_default_format_prints_plain_text(
    mock_agent_stack: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    main_single_query("/repo", 100, _QUESTION)

    out = capsys.readouterr().out.strip()
    assert out == _ANSWER


def test_json_format_wraps_query_and_response(
    mock_agent_stack: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    main_single_query("/repo", 100, _QUESTION, output_format=cs.QueryFormat.JSON)

    payload = json.loads(capsys.readouterr().out)
    assert payload == {cs.KEY_QUERY: _QUESTION, cs.KEY_RESPONSE: _ANSWER}
