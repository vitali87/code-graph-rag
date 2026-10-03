"""Issue #2461: `cgr graph` and its MCP tools refuse a project or a name the
graph does not hold, instead of answering as if nothing matched.

A typo in the hashed project name, a qualified name that does not exist and a
directory that was never indexed all printed `[]` with exit 0: the answer a
real "nothing calls it" gives, which an agent or a CI script acts on (a
function nobody calls is safe to delete; a change no test reaches needs no
test run). MCP named an unknown project but returned it as a successful
result listing every indexed project, and answered an unknown name with `[]`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from mcp import types
from typer.testing import CliRunner as TyperRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.cli import app
from codebase_rag.graph_cli import cli as graph_cli
from codebase_rag.mcp import server as mcp_server
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.types_defs import MCPHandlerType, PropertyParams, ResultRow
from codebase_rag.utils.path_utils import derive_project_name

P = "gq__e7a07807"
# Another checkout of a directory named `gq`: same `<dir>` part, other hash.
TWIN = "gq__0badc0de"
UNRELATED = "zeta__00000000"
ROOTS: dict[str, str | None] = {
    P: "/repos/gq",
    TWIN: "/elsewhere/gq",
    UNRELATED: "/repos/zeta",
}

SHAPES = f"{P}.app.shapes"
TOTAL = f"{SHAPES}.total"
AVERAGE = f"{SHAPES}.average"
UNUSED = f"{SHAPES}.unused"
SHAPE = f"{SHAPES}.Shape"
AREA = f"{SHAPE}.area"
TEST_TOTAL = f"{P}.tests.test_shapes.test_total"
REPORT_TOTAL = f"{P}.app.report.total"
EXTERNAL = "os"
FOREIGN = f"{TWIN}.lib.helper"

NODES: dict[str, str] = {
    f"{P}.app": "Module",
    SHAPES: "Module",
    TOTAL: "Function",
    AVERAGE: "Function",
    UNUSED: "Function",
    SHAPE: "Class",
    AREA: "Method",
    f"{P}.app.report": "Module",
    # A second `total` elsewhere in the project: the suggestion for a name
    # whose module part is wrong.
    REPORT_TOTAL: "Function",
    f"{P}.tests.test_shapes": "Module",
    TEST_TOTAL: "Function",
    EXTERNAL: "ExternalModule",
    FOREIGN: "Function",
}
CALLS: list[tuple[str, str]] = [(AVERAGE, TOTAL), (TEST_TOTAL, TOTAL)]
_DEFINITION_LABELS = {label.value for label in cs.DEFINITION_NODE_LABELS}


def _path(qn: str) -> str:
    return "tests/test_shapes.py" if ".tests." in qn else "app/shapes.py"


def _row(qn: str) -> ResultRow:
    return {
        cs.KEY_LABEL: NODES[qn],
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_NAME: qn.rsplit(".", 1)[-1],
        cs.KEY_PATH: _path(qn),
        cs.KEY_START_LINE: 1,
        cs.KEY_END_LINE: 2,
        cs.KEY_DECORATORS: [],
        cs.KEY_DOCSTRING: None,
    }


class FakeGraph:
    """The fixed queries the graph tools issue, answered from NODES/CALLS."""

    def __init__(self, roots: dict[str, str | None] | None = None) -> None:
        self.roots = dict(ROOTS if roots is None else roots)
        self.queries: list[str] = []

    def __call__(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        self.queries.append(query)
        p = params or {}
        prefix = str(p.get(cs.KEY_PROJECT_PREFIX, ""))
        qn = str(p.get(cs.KEY_QN, ""))
        if query == cq.CYPHER_LIST_PROJECTS:
            return [
                {cs.KEY_NAME: name, cs.KEY_ROOT_PATH: root}
                for name, root in sorted(self.roots.items())
            ]
        if query == cq.CYPHER_PROJECT_IS_INCOMPLETE:
            return []
        if query == cq.CYPHER_PROJECT_ROOT_PATH:
            root = self.roots.get(str(p.get(cs.KEY_PROJECT_NAME, "")))
            return [{cs.KEY_ROOT_PATH: root}] if root else []
        if query == cq.CYPHER_GRAPH_NODE_EXISTS:
            return [{cs.KEY_QUALIFIED_NAME: qn}] if qn in NODES else []
        if query == cq.CYPHER_GRAPH_DEFINITIONS_UNDER:
            return [
                {cs.KEY_QUALIFIED_NAME: q}
                for q, label in NODES.items()
                if q.startswith(prefix) and label in _DEFINITION_LABELS
            ]
        if query == cq.CYPHER_GRAPH_DEFINITION:
            return [
                _row(q)
                for q, label in NODES.items()
                if q == qn and q.startswith(prefix) and label in _DEFINITION_LABELS
            ]
        if query == cq.CYPHER_GRAPH_RESOLVE_NAME:
            return [
                _row(q)
                for q, label in NODES.items()
                if q.startswith(prefix)
                and label in _DEFINITION_LABELS
                and (
                    q == qn
                    or q.endswith(str(p[cs.KEY_SUFFIX]))
                    or q.rsplit(".", 1)[-1] == p[cs.KEY_NAME]
                )
            ]
        if query in (cq.CYPHER_GRAPH_CALLERS, cq.CYPHER_GRAPH_CALLEES):
            callers = query == cq.CYPHER_GRAPH_CALLERS
            out: list[ResultRow] = []
            for src, dst in CALLS:
                if (dst if callers else src) != qn:
                    continue
                other = src if callers else dst
                if other.startswith(prefix):
                    out.append(
                        {
                            **_row(other),
                            cs.KEY_LINE: 2,
                            cs.KEY_COL: 4,
                            cs.KEY_ARG_COUNT: 1,
                            cs.KEY_KWARG_NAMES: [],
                        }
                    )
            return out
        if query in (
            cq.CYPHER_GRAPH_IMPLEMENTORS,
            cq.CYPHER_GRAPH_OVERRIDES,
            cq.CYPHER_GRAPH_IMPORTERS,
        ):
            return []
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return [
                {
                    **_row(q),
                    cs.KEY_IS_EXPORTED: False,
                    cs.KEY_OVERRIDES_EXTERNAL: False,
                    cs.KEY_RUST_CFG_TEST_MODS: [],
                    cs.KEY_RUST_UNGATED_MODS: [],
                }
                for q in NODES
                if q.startswith(prefix)
            ]
        if query == cq.CYPHER_DEAD_CODE_RELS:
            return [
                {
                    cs.KEY_FROM_LABEL: NODES[src],
                    cs.KEY_FROM_QN: src,
                    cs.KEY_REL_TYPE: cs.RelationshipType.CALLS.value,
                    cs.KEY_TO_LABEL: NODES[dst],
                    cs.KEY_TO_QN: dst,
                }
                for src, dst in CALLS
            ]
        raise AssertionError(f"unexpected query: {query[:60]}")


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def connected(graph: FakeGraph) -> Iterator[FakeGraph]:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=graph)
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)
    with patch("codebase_rag.cli_runtime.connect_memgraph", return_value=ingestor):
        yield graph


def _graph(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(graph_cli, list(args))
    return result.exit_code, result.stdout, result.stderr


TARGET_COMMANDS = [
    "callers",
    "callees",
    "implementors",
    "overrides",
    "importers",
    "tests-reaching",
]


# --- cgr graph: an unknown project --------------------------------------------


class TestUnknownProject:
    def test_a_typo_in_the_hash_is_refused_with_the_close_match(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        code, out, err = _graph(
            "callers", TOTAL, "--project", "gq__e7a07870", "--repo-path", str(tmp_path)
        )

        assert code == cs.GRAPH_EXIT_UNKNOWN_PROJECT
        assert out == ""
        assert "'gq__e7a07870' is not indexed" in err
        assert f"Did you mean: {P}" in err
        # A close match is named instead of every indexed project.
        assert UNRELATED not in err

    def test_no_query_runs_against_an_unknown_project(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        _graph("callers", TOTAL, "--project", "nope", "--repo-path", str(tmp_path))

        assert connected.queries == [cq.CYPHER_LIST_PROJECTS]

    def test_a_bare_directory_name_suggests_every_checkout_of_it(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        # `<dir>__<hash>` is not guessable; `gq` alone should still lead there.
        code, _, err = _graph(
            "resolve", "total", "--project", "gq", "--repo-path", str(tmp_path)
        )

        assert code == cs.GRAPH_EXIT_UNKNOWN_PROJECT
        assert P in err
        assert TWIN in err
        assert UNRELATED not in err

    def test_without_a_close_match_the_indexed_projects_are_listed(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        code, _, err = _graph(
            "resolve", "x", "--project", "unrelated_name", "--repo-path", str(tmp_path)
        )

        assert code == cs.GRAPH_EXIT_UNKNOWN_PROJECT
        assert "Did you mean" not in err
        assert all(name in err for name in ROOTS)

    @pytest.mark.parametrize("command", ["resolve", "definition", *TARGET_COMMANDS])
    def test_every_subcommand_refuses_it(
        self, connected: FakeGraph, tmp_path: Path, command: str
    ) -> None:
        code, out, _ = _graph(
            command, TOTAL, "--project", "gq__e7a07870", "--repo-path", str(tmp_path)
        )

        assert code == cs.GRAPH_EXIT_UNKNOWN_PROJECT
        assert out == ""


class TestNeverIndexedDirectory:
    def test_the_derived_project_is_refused_naming_the_directory(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        code, out, err = _graph("resolve", "f", "--repo-path", str(tmp_path))

        assert code == cs.GRAPH_EXIT_UNKNOWN_PROJECT
        assert out == ""
        assert f"No project is indexed for {tmp_path.resolve()}" in err
        assert "cgr start --update-graph" in err

    def test_a_repository_indexed_under_its_own_name_is_suggested(
        self, graph: FakeGraph, connected: FakeGraph, tmp_path: Path
    ) -> None:
        # `cgr start --project-name custom` from this directory: the derived
        # `<dir>__<hash>` is not in the graph, the repository still is.
        graph.roots["custom"] = str(tmp_path)

        code, _, err = _graph("resolve", "f", "--repo-path", str(tmp_path))

        assert code == cs.GRAPH_EXIT_UNKNOWN_PROJECT
        assert "Did you mean: custom" in err

    def test_an_indexed_directory_still_answers(
        self, graph: FakeGraph, connected: FakeGraph, tmp_path: Path
    ) -> None:
        # Negative: the derived name is the one the graph holds.
        graph.roots[derive_project_name(tmp_path)] = str(tmp_path)

        code, out, err = _graph("resolve", "f", "--repo-path", str(tmp_path))

        assert code == 0, err
        assert json.loads(out) == []


# --- cgr graph: an unknown qualified name -----------------------------------------


class TestUnknownTarget:
    def test_a_typo_in_the_name_is_refused_with_the_close_match(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        code, out, err = _graph(
            "callers", f"{SHAPES}.tota", "--project", P, "--repo-path", str(tmp_path)
        )

        assert code == cs.GRAPH_EXIT_UNKNOWN_TARGET
        assert out == ""
        assert f"'{SHAPES}.tota' is not in the graph" in err
        assert f"Did you mean: {TOTAL}" in err

    def test_a_name_in_the_wrong_module_suggests_the_same_name_elsewhere(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        code, _, err = _graph(
            "tests-reaching",
            f"{P}.app.total",
            "--project",
            P,
            "--repo-path",
            str(tmp_path),
        )

        assert code == cs.GRAPH_EXIT_UNKNOWN_TARGET
        assert TOTAL in err
        assert REPORT_TOTAL in err

    def test_with_nothing_close_it_points_at_resolve(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        code, _, err = _graph(
            "callers", f"{P}.zzz.qqq", "--project", P, "--repo-path", str(tmp_path)
        )

        assert code == cs.GRAPH_EXIT_UNKNOWN_TARGET
        assert "Did you mean" not in err
        assert "cgr graph resolve" in err

    @pytest.mark.parametrize("command", TARGET_COMMANDS)
    def test_every_target_command_refuses_it(
        self, connected: FakeGraph, tmp_path: Path, command: str
    ) -> None:
        code, out, _ = _graph(
            command, f"{SHAPES}.tota", "--project", P, "--repo-path", str(tmp_path)
        )

        assert code == cs.GRAPH_EXIT_UNKNOWN_TARGET
        assert out == ""


class TestWhatStillAnswers:
    """Negative: an empty answer about a name the graph holds is an answer."""

    def test_the_real_query_returns_its_rows(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        code, out, err = _graph(
            "callers", TOTAL, "--project", P, "--repo-path", str(tmp_path)
        )

        assert code == 0, err
        assert [r["qualified_name"] for r in json.loads(out)] == [
            AVERAGE,
            TEST_TOTAL,
        ]

    @pytest.mark.parametrize("command", TARGET_COMMANDS)
    def test_a_name_with_nothing_to_report_is_an_empty_list(
        self, connected: FakeGraph, tmp_path: Path, command: str
    ) -> None:
        code, out, err = _graph(
            command, UNUSED, "--project", P, "--repo-path", str(tmp_path)
        )

        assert code == 0, err
        assert json.loads(out) == []
        assert err == ""

    @pytest.mark.parametrize("target", [EXTERNAL, FOREIGN])
    def test_a_node_outside_the_project_still_exists(
        self, connected: FakeGraph, tmp_path: Path, target: str
    ) -> None:
        # Importers of `os` (an ExternalModule), callers of another project's
        # function: neither is under this project's prefix, both are real.
        code, out, err = _graph(
            "importers", target, "--project", P, "--repo-path", str(tmp_path)
        )

        assert code == 0, err
        assert json.loads(out) == []

    def test_resolve_with_no_match_is_still_an_empty_list(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        # `resolve` searches; finding nothing is its answer, not a mistake.
        code, out, err = _graph(
            "resolve", "no_such_name", "--project", P, "--repo-path", str(tmp_path)
        )

        assert code == 0, err
        assert json.loads(out) == []

    def test_definition_keeps_its_found_false_envelope(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        code, out, err = _graph(
            "definition", f"{SHAPES}.tota", "--project", P, "--repo-path", str(tmp_path)
        )

        assert code == 0, err
        assert json.loads(out)["found"] is False

    def test_the_existence_check_runs_only_for_an_empty_answer(
        self, connected: FakeGraph, tmp_path: Path
    ) -> None:
        _graph("callers", TOTAL, "--project", P, "--repo-path", str(tmp_path))

        assert cq.CYPHER_GRAPH_NODE_EXISTS not in connected.queries


def test_through_cgr_the_exit_statuses_reach_the_shell(
    connected: FakeGraph, tmp_path: Path
) -> None:
    # `graph` runs as a delegated click group inside typer; its status must
    # survive that boundary rather than end as 0.
    runner = TyperRunner()
    project = runner.invoke(
        app,
        ["graph", "callers", TOTAL, "--project", "x", "--repo-path", str(tmp_path)],
    )
    target = runner.invoke(
        app,
        ["graph", "callers", f"{TOTAL}x", "--project", P, "--repo-path", str(tmp_path)],
    )

    assert project.exit_code == cs.GRAPH_EXIT_UNKNOWN_PROJECT, project.output
    assert target.exit_code == cs.GRAPH_EXIT_UNKNOWN_TARGET, target.output


def test_the_refusal_is_printed_outside_the_graph_connection(
    graph: FakeGraph, tmp_path: Path
) -> None:
    # Raising the exit inside `with ingestor:` would log it as a failed write
    # with a traceback on every refusal.
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=graph)
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)
    with patch("codebase_rag.cli_runtime.connect_memgraph", return_value=ingestor):
        _graph("callers", TOTAL, "--project", "x", "--repo-path", str(tmp_path))

    ingestor.__exit__.assert_called_once_with(None, None, None)


# --- suggestions -----------------------------------------------------------------


class TestCloseProjectNames:
    def test_a_hash_typo_matches(self) -> None:
        assert graph_query.close_project_names("gq__e7a07870", ROOTS) == [P, TWIN]

    def test_a_bare_directory_name_matches_its_checkouts(self) -> None:
        assert set(graph_query.close_project_names("gq", ROOTS)) == {P, TWIN}

    def test_an_unrelated_name_matches_nothing(self) -> None:
        assert graph_query.close_project_names("unrelated_name", ROOTS) == []

    def test_the_list_is_bounded(self) -> None:
        many = [f"gq__{i:08x}" for i in range(50)]
        assert (
            len(graph_query.close_project_names("gq__00000000", many))
            == cs.GRAPH_SUGGESTION_LIMIT
        )


def test_similar_targets_are_the_projects_own(graph: FakeGraph) -> None:
    assert graph_query.similar_targets(graph, P, f"{SHAPES}.averag") == [AVERAGE]
    # Negative: TWIN holds `lib.helper`, P does not; a suggestion is a name
    # this project's queries can answer.
    assert graph_query.similar_targets(graph, P, f"{P}.lib.helper") == []


# --- MCP -------------------------------------------------------------------------


@pytest.fixture
def registry(graph: FakeGraph, tmp_path: Path) -> MCPToolsRegistry:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=graph)
    ingestor.list_projects.side_effect = lambda: sorted(graph.roots)
    ingestor.list_project_roots.side_effect = lambda: dict(graph.roots)
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        return MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=ingestor, cypher_gen=MagicMock()
        )


class TestMcp:
    @pytest.mark.parametrize("tool", ["callers", "tests_reaching"])
    async def test_an_unknown_name_is_an_error_with_the_close_match(
        self, registry: MCPToolsRegistry, tool: str
    ) -> None:
        handler: MCPHandlerType = getattr(registry, tool)
        result = await handler(f"{SHAPES}.tota", project=P)

        assert isinstance(result, dict)
        assert set(result) == {cs.DICT_KEY_ERROR}
        assert f"'{SHAPES}.tota' is not in the graph" in result[cs.DICT_KEY_ERROR]
        assert TOTAL in result[cs.DICT_KEY_ERROR]

    async def test_an_unknown_project_names_the_close_match_only(
        self, registry: MCPToolsRegistry
    ) -> None:
        result = await registry.resolve("total", project="gq__e7a07870")

        assert isinstance(result, dict)
        assert f"Did you mean: {P}" in result[cs.DICT_KEY_ERROR]
        assert UNRELATED not in result[cs.DICT_KEY_ERROR]

    async def test_an_unindexed_server_root_is_refused(
        self, registry: MCPToolsRegistry, tmp_path: Path
    ) -> None:
        result = await registry.callers(TOTAL)

        assert isinstance(result, dict)
        assert str(tmp_path.resolve()) in result[cs.DICT_KEY_ERROR]
        assert "index_repository" in result[cs.DICT_KEY_ERROR]

    async def test_a_name_with_no_callers_is_still_an_empty_list(
        self, registry: MCPToolsRegistry
    ) -> None:
        # Negative.
        assert await registry.callers(UNUSED, project=P) == []
        assert await registry.tests_reaching(UNUSED, project=P) == []

    async def test_an_indexed_server_root_still_answers(
        self, graph: FakeGraph, registry: MCPToolsRegistry, tmp_path: Path
    ) -> None:
        # Negative: the derived project is indexed, so the bare call runs.
        graph.roots[derive_project_name(tmp_path)] = str(tmp_path)

        assert await registry.resolve("total") == []

    async def test_without_a_close_match_every_project_is_listed(
        self, registry: MCPToolsRegistry
    ) -> None:
        # Negative: with nothing close, the list is the only lead left.
        result = await registry.resolve("total", project="unrelated_name")

        assert isinstance(result, dict)
        assert all(name in result[cs.DICT_KEY_ERROR] for name in ROOTS)


def _server_with(
    handler: MCPHandlerType | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str], Awaitable[types.CallToolResult]]:
    monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(tmp_path))
    monkeypatch.delenv(cs.MCPEnvVar.MCP_WORKSPACE, raising=False)
    tools = MagicMock()
    tools.get_tool_schemas.return_value = []
    tools.get_tool_handler.return_value = None if handler is None else (handler, True)
    with (
        patch.object(mcp_server, "setup_logging"),
        patch.object(mcp_server, "MemgraphIngestor"),
        patch.object(mcp_server, "LazyCypherGenerator"),
        patch.object(mcp_server, "create_mcp_tools_registry", return_value=tools),
        patch.object(
            type(mcp_server.settings), "active_orchestrator_config", MagicMock()
        ),
        patch.object(type(mcp_server.settings), "active_cypher_config", MagicMock()),
    ):
        server, _ = mcp_server.create_server()
    request_handler = server.request_handlers[types.CallToolRequest]

    async def call(name: str) -> types.CallToolResult:
        response = await request_handler(
            types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(name=name, arguments={}),
            )
        )
        assert isinstance(response.root, types.CallToolResult)
        return response.root

    return call


def _text(result: types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


class TestMcpIsError:
    async def test_a_refusal_is_an_error_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def refuse() -> dict[str, str]:
            return {cs.DICT_KEY_ERROR: "Unknown project 'gq__e7a07870'."}

        result = await _server_with(refuse, tmp_path, monkeypatch)("resolve")

        assert result.isError is True
        assert json.loads(_text(result)) == {
            cs.DICT_KEY_ERROR: "Unknown project 'gq__e7a07870'."
        }

    async def test_a_failing_tool_is_an_error_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom() -> list[str]:
            raise RuntimeError("down")

        result = await _server_with(boom, tmp_path, monkeypatch)("callers")

        assert result.isError is True
        assert "down" in _text(result)

    async def test_an_unknown_tool_is_an_error_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await _server_with(None, tmp_path, monkeypatch)("no_such_tool")

        assert result.isError is True

    async def test_an_empty_answer_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Negative: "exists, nothing calls it" stays a successful [].
        async def empty() -> list[str]:
            return []

        result = await _server_with(empty, tmp_path, monkeypatch)("callers")

        assert result.isError is False
        assert json.loads(_text(result)) == []

    async def test_a_payload_that_carries_an_error_field_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Negative: an applied rename reports a marker it could not clear
        # beside its result; the work happened, so the call succeeded.
        async def applied() -> dict[str, str | bool]:
            return {"applied": True, cs.DICT_KEY_ERROR: "marker stuck"}

        result = await _server_with(applied, tmp_path, monkeypatch)("rename")

        assert result.isError is False
