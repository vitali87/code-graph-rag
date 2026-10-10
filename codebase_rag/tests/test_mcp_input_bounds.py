"""MCP tools answer out-of-range input and unknown symbols in their own words.

`read_file` passed a negative `offset` to `islice()` and reported its
message, returned "Lines 1-0" for a negative `limit` and "Lines 1001-1000
of 7" past the end; `resolve` sent a 20-digit line to Bolt and reported
"int too big to convert"; an empty `structural_search` pattern returned
ast-grep's Rust cause chain and backtrace; and `definition` and
`get_code_snippet` answered an unknown name as a success while `callers`
refused it (issue #3244).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_query
from codebase_rag import tool_errors as te
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.server import _reports_failure
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, ResultRow
from codebase_rag.utils.path_utils import derive_project_name
from evals.cgr_graph import _StatefulIngestor

pytestmark = [pytest.mark.anyio]

A_TS = (
    "export function f(x: number): number {\n  return x + 1;\n}\n\n"
    "export function g(): number {\n  return f(1);\n}\n"
)


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _registry(root: Path) -> MCPToolsRegistry:
    (root / "web").mkdir(parents=True, exist_ok=True)
    (root / "web/a.ts").write_text(A_TS, encoding="utf-8")
    project = derive_project_name(root)
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
    ).run(force=True)
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [project]
    return MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )


def _schema(registry: MCPToolsRegistry, tool: str) -> dict[str, object]:
    (schema,) = [s for s in registry.get_tool_schemas() if s.name == tool]
    return {**schema.inputSchema}


# --- read_file ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param({"offset": -1}, "offset -1 is negative", id="negative-offset"),
        pytest.param({"limit": -1}, "limit -1 is not positive", id="negative-limit"),
        pytest.param({"limit": 0}, "limit 0 is not positive", id="zero-limit"),
        pytest.param(
            {"offset": 1000},
            "offset 1000 is past the end of web/a.ts (7 lines)",
            id="past-the-end",
        ),
        pytest.param(
            {"offset": 7, "limit": 2},
            "offset 7 is past the end of web/a.ts (7 lines)",
            id="just-past-the-end",
        ),
    ],
)
async def test_read_file_refuses_an_out_of_range_window(
    temp_repo: Path, arguments: dict[str, int], message: str
) -> None:
    registry = _registry(temp_repo)

    result = await registry.read_file("web/a.ts", **arguments)

    assert isinstance(result, te.ToolFailure), result
    assert str(result) == f"Error: {message}"


async def test_read_file_schema_bounds_offset_and_limit(temp_repo: Path) -> None:
    # The MCP layer validates arguments against this schema before the
    # handler runs, so a bad value is a schema error at the boundary.
    schema = _schema(_registry(temp_repo), cs.MCPToolName.READ_FILE)

    for bad in ({"offset": -1}, {"limit": 0}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"file_path": "web/a.ts", **bad}, schema)
    jsonschema.validate({"file_path": "web/a.ts", "offset": 0, "limit": 1}, schema)


@pytest.mark.parametrize(
    ("arguments", "header"),
    [
        pytest.param({"offset": 2, "limit": 3}, "# Lines 3-5 of 7\n", id="window"),
        pytest.param({"offset": 6}, "# Lines 7-7 of 7\n", id="last-line"),
        pytest.param({"offset": 5, "limit": 100}, "# Lines 6-7 of 7\n", id="clipped"),
    ],
)
async def test_read_file_answers_a_window_inside_the_file(
    temp_repo: Path, arguments: dict[str, int], header: str
) -> None:
    # Negative: every window that overlaps the file reads as before.
    result = await _registry(temp_repo).read_file("web/a.ts", **arguments)

    assert isinstance(result, str)
    assert not isinstance(result, te.ToolFailure)
    assert result.startswith(header), result


async def test_an_empty_file_reads_from_its_start(temp_repo: Path) -> None:
    # Negative: offset 0 of an empty file is its (empty) start, not past it.
    registry = _registry(temp_repo)
    (temp_repo / "empty.ts").write_text("", encoding="utf-8")

    result = await registry.read_file("empty.ts", offset=0, limit=5)

    assert not isinstance(result, te.ToolFailure), result


# --- resolve ------------------------------------------------------------------


async def test_resolve_refuses_a_line_no_file_can_have(temp_repo: Path) -> None:
    result = await _registry(temp_repo).resolve("web/a.ts:99999999999999999999")

    assert isinstance(result, dict), result
    assert _reports_failure(result), result
    assert result[cs.DICT_KEY_ERROR] == (
        "Line 99999999999999999999 of web/a.ts is out of range."
    )


def test_a_huge_line_never_reaches_the_graph() -> None:
    # Bolt integers are 64-bit: the line would raise in the driver.
    def fetch_all(_query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        for value in (params or {}).values():
            if isinstance(value, int) and value > 2**63 - 1:
                raise OverflowError("int too big to convert")
        return []

    assert graph_query.resolve(fetch_all, "p", "web/a.ts:99999999999999999999") == []


async def test_resolve_still_answers_a_line_in_the_file(temp_repo: Path) -> None:
    # Negative.
    result = await _registry(temp_repo).resolve("web/a.ts:2")

    assert isinstance(result, list)
    assert [row["qualified_name"].rsplit(".", 1)[-1] for row in result] == ["f"]


# --- structural_search ----------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "cause"),
    [
        pytest.param("", "No AST root is detected", id="empty"),
        pytest.param("   ", "No AST root is detected", id="blank"),
        pytest.param("$$$", "Standalone multi meta variable", id="bare-multi-metavar"),
    ],
)
async def test_a_matcherless_pattern_is_one_line(
    temp_repo: Path, pattern: str, cause: str
) -> None:
    pytest.importorskip("ast_grep_py")
    result = await _registry(temp_repo).structural_search(
        pattern=pattern, language="typescript"
    )

    text = str(result)
    assert cause in text, text
    assert "Stack backtrace" not in text, text
    assert "\n" not in text.strip(), text


async def test_a_valid_pattern_still_matches(temp_repo: Path) -> None:
    # Negative.
    pytest.importorskip("ast_grep_py")
    result = await _registry(temp_repo).structural_search(
        pattern="f($X)", language="typescript"
    )

    assert "f(1)" in str(result)


# --- not in the graph -----------------------------------------------------------


async def test_definition_of_an_unknown_name_is_refused_like_callers(
    temp_repo: Path,
) -> None:
    registry = _registry(temp_repo)

    definition = await registry.definition("no.such.symbol")
    callers = await registry.callers("no.such.symbol")

    assert isinstance(definition, dict)
    assert isinstance(callers, dict)
    assert _reports_failure(definition)
    assert _reports_failure(callers)
    assert definition[cs.DICT_KEY_ERROR] == callers[cs.DICT_KEY_ERROR]
    # The row stays for a client that reads `found`.
    assert definition["found"] is False


async def test_a_known_definition_is_answered(temp_repo: Path) -> None:
    # Negative.
    registry = _registry(temp_repo)
    project = derive_project_name(temp_repo)

    result = await registry.definition(f"{project}.web.a.f")

    assert isinstance(result, dict), result
    assert not _reports_failure(result), result
    assert result["found"] is True


async def test_get_code_snippet_of_an_unknown_name_is_a_refusal(
    temp_repo: Path,
) -> None:
    registry = _registry(temp_repo)
    registry._code_tool = MagicMock()

    async def not_found(qualified_name: str) -> MagicMock:
        return MagicMock(
            model_dump=lambda: {
                "qualified_name": qualified_name,
                "source_code": "",
                "file_path": "",
                "line_start": 0,
                "line_end": 0,
                "found": False,
                "error_message": te.CODE_ENTITY_NOT_FOUND,
            }
        )

    registry._code_tool.function = not_found

    result = await registry.get_code_snippet("no.such.symbol")
    callers = await registry.callers("no.such.symbol")

    assert _reports_failure(result), result
    assert isinstance(callers, dict)
    assert result.get(cs.DICT_KEY_ERROR) == callers[cs.DICT_KEY_ERROR]
    assert result["found"] is False
