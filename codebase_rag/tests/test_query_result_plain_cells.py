"""Query result cells print what the graph holds, never markup (#2260).

Values reach the table from indexed code and from MCP `annotate` glosses;
Rich parsed them as markup, truncating `list[int]` to `list`, raising on
`[/]`, and turning `[link=...]` into a live terminal hyperlink.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from codebase_rag.tools.codebase_query import create_query_tool

pytestmark = [pytest.mark.anyio]

_LINK = "[link=https://evil.example/x]see docs[/link] [bold red]PASSED[/bold red]"


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


async def _render(rows: list[dict]) -> str:
    ingestor = MagicMock()
    ingestor.fetch_read_only.return_value = rows
    cypher_gen = MagicMock()
    cypher_gen.generate = AsyncMock(return_value="MATCH (n) RETURN n")
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=400)
    tool = create_query_tool(ingestor, cypher_gen, console=console)
    result = await tool.function(natural_language_query="q")
    assert result.results == rows
    return buffer.getvalue()


@pytest.mark.parametrize(
    "value", ["list[int]", "Dict[str, int]", "arr[i]", "[/]", "[bold]x[/bold]"]
)
async def test_bracketed_values_print_literally(value: str) -> None:
    out = await _render([{"annotation": value}])

    assert value in out


async def test_a_link_payload_is_shown_not_obeyed() -> None:
    out = await _render([{"docstring": _LINK}])

    assert _LINK in out
    assert "\x1b]8;" not in out
    assert "evil.example" in out


async def test_a_bracketed_column_name_prints_literally() -> None:
    out = await _render([{"[b]n[/b]": "x"}])

    assert "[b]n[/b]" in out


async def test_control_sequences_in_a_value_are_escaped() -> None:
    payload = "a\x1b]8;;https://evil.example/\x07b\x9bc"
    out = await _render([{"docstring": payload}])

    assert "\x1b]8;;https://evil" not in out
    assert "\x9b" not in out
    assert "a\\x1b]8;;https://evil.example/\\x07b\\x9bc" in out


async def test_scalar_and_empty_values_still_render() -> None:
    out = await _render([{"name": "f", "lines": 42, "ratio": 3.5, "doc": None}])

    assert "42" in out
    assert "3.5" in out
