"""`path:line` targets name a file however a caller spells it (issue #2611).

`cgr graph resolve`, and the MCP `resolve`, `annotate` and `glosses` tools,
matched a location only when its path was byte-for-byte the repo-relative
string stored on the node. `./x`, an absolute path inside the repository, the
`x:line:col` form compilers print and `x\\y` all answered `[]` with exit 0,
the same answer as "no definition spans that line". The fake graph below
holds three projects: `proj`, `proj.extra` (whose name extends it, issue
#1982) and `other` (the same layout, so the same relative path exists in
both); it raises on any query it does not model.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import gloss, graph_query
from codebase_rag.graph_cli import cli as graph_cli
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tests import test_gloss_tools as gt
from codebase_rag.types_defs import PropertyDict, ResultRow

P = "proj"
EXTRA = "proj.extra"
OTHER = "other"
SESSIONS = "src/pkg/sessions.py"
SESSION = f"{P}.src.pkg.sessions.Session"
INIT = f"{P}.src.pkg.sessions.Session.__init__"
# A POSIX file name may hold a colon; `src/odd:2:3` then reads either as
# line 2, column 3 of `src/odd` or as line 3 of `src/odd:2`.
ODD = "src/odd:2"
RENDER = f"{P}.src.odd.render"
# The file `src/odd` itself, added by the tests that hold both readings.
ODD_BASE = "src/odd"


def _node(
    label: str, qn: str, path: str, start: int | None = None, end: int | None = None
) -> ResultRow:
    return {
        cs.KEY_LABEL: label,
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_NAME: qn.rsplit(".", 1)[-1],
        cs.KEY_PATH: path,
        cs.KEY_START_LINE: start,
        cs.KEY_END_LINE: end,
    }


# Module nodes carry no span in the real graph, so a line answers with the
# definitions around it only.
NODES: list[ResultRow] = [
    _node("Module", f"{P}.src.pkg.sessions", SESSIONS),
    _node("Class", SESSION, SESSIONS, 10, 50),
    _node("Method", INIT, SESSIONS, 12, 20),
    _node("Module", f"{P}.src.odd", ODD),
    _node("Function", RENDER, ODD, 1, 5),
    # A document is a Module too (the document tier), with no definitions.
    _node("Module", f"{P}.README", "README.md"),
    _node("Module", f"{EXTRA}.src.only_extra", "src/only_extra.py"),
    _node("Function", f"{EXTRA}.src.only_extra.f", "src/only_extra.py", 1, 5),
    _node("Module", f"{OTHER}.src.pkg.sessions", SESSIONS),
    _node("Function", f"{OTHER}.src.pkg.sessions.request", SESSIONS, 5, 30),
    _node("Module", f"{OTHER}.src.pkg.adapters", "src/pkg/adapters.py"),
    _node("Function", f"{OTHER}.src.pkg.adapters.send", "src/pkg/adapters.py", 1, 9),
]


class Graph:
    """The fixture graph; `roots` is where each project was indexed from.

    A file is held by a project when the project has a Module at its path:
    File and Folder nodes are keyed by absolute path, so two projects
    indexed from one root share them and cannot say whose a file is.
    """

    def __init__(
        self, root: str = "/srv/proj", extra: list[ResultRow] | None = None
    ) -> None:
        self.roots = {P: root, EXTRA: "/srv/extra", OTHER: "/srv/other"}
        self.nodes = [*NODES, *(extra or [])]

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        p = params or {}
        if query == cq.CYPHER_PROJECT_IS_INCOMPLETE:
            return []
        if query == cq.CYPHER_LIST_PROJECTS:
            return [{cs.KEY_NAME: name} for name in (P, EXTRA, OTHER)]
        if query == cq.CYPHER_PROJECT_ROOT_PATH:
            return [{cs.KEY_ROOT_PATH: self.roots[str(p[cs.KEY_PROJECT_NAME])]}]
        prefix = str(p[cs.KEY_PROJECT_PREFIX])
        scoped = [
            n for n in self.nodes if str(n[cs.KEY_QUALIFIED_NAME]).startswith(prefix)
        ]
        if query == cq.CYPHER_GRAPH_RESOLVE_NAME:
            return [
                n
                for n in scoped
                if n[cs.KEY_QUALIFIED_NAME] == p[cs.KEY_QN]
                or str(n[cs.KEY_QUALIFIED_NAME]).endswith(str(p[cs.KEY_SUFFIX]))
                or n[cs.KEY_NAME] == p[cs.KEY_NAME]
            ]
        if query == cq.CYPHER_GRAPH_RESOLVE_LOCATION:
            line = p[cs.KEY_LINE]
            assert isinstance(line, int)
            return [
                n
                for n in scoped
                if n[cs.KEY_PATH] == p[cs.KEY_PATH]
                and isinstance(start := n[cs.KEY_START_LINE], int)
                and isinstance(end := n[cs.KEY_END_LINE], int)
                and start <= line <= end
            ]
        if query == cq.CYPHER_GRAPH_LOCATION_FILES:
            wanted = p[cs.CYPHER_PARAM_PATHS]
            assert isinstance(wanted, list)
            return [
                {
                    cs.KEY_PATH: n[cs.KEY_PATH],
                    cs.KEY_QUALIFIED_NAME: n[cs.KEY_QUALIFIED_NAME],
                }
                for n in scoped
                if n[cs.KEY_LABEL] == "Module" and n[cs.KEY_PATH] in wanted
            ]
        raise AssertionError(f"unexpected query: {query[:60]}")


def _qns(rows: Any) -> list[str]:
    assert isinstance(rows, list), rows
    return [row["qualified_name"] for row in rows]


INNERMOST = [INIT, SESSION]


def _mock_connect(graph: Graph) -> MagicMock:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=graph.fetch_all)
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)
    return ingestor


def _cli_resolve(graph: Graph, target: str, repo: Path, project: str = P) -> Any:
    with patch(
        "codebase_rag.cli_runtime.connect_memgraph", return_value=_mock_connect(graph)
    ):
        return CliRunner().invoke(
            graph_cli,
            ["resolve", target, "--project", project, "--repo-path", str(repo)],
        )


def _registry(graph: Graph, root: Path) -> MCPToolsRegistry:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=graph.fetch_all)
    ingestor.list_projects.return_value = [P, EXTRA, OTHER]
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        return MCPToolsRegistry(
            project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
        )


# --- the path spellings the issue lists ------------------------------------------


def test_the_stored_repo_relative_path_still_resolves() -> None:
    assert _qns(graph_query.resolve(Graph().fetch_all, P, f"{SESSIONS}:15")) == (
        INNERMOST
    )


@pytest.mark.parametrize(
    "target",
    [
        f"./{SESSIONS}:15",
        "src/./pkg//sessions.py:15",
        "/srv/proj/src/pkg/sessions.py:15",
        f"{SESSIONS}:15:9",
        "src\\pkg\\sessions.py:15",
        ".\\src\\pkg\\sessions.py:15:1",
        "/srv/proj/src/pkg/sessions.py:15:9",
    ],
)
def test_every_spelling_of_the_file_resolves_like_the_stored_path(
    target: str,
) -> None:
    assert _qns(graph_query.resolve(Graph().fetch_all, P, target)) == INNERMOST


def test_an_absolute_windows_path_under_a_windows_root_resolves() -> None:
    graph = Graph(root="C:\\work\\proj")
    for target in (
        "C:\\work\\proj\\src\\pkg\\sessions.py:15",
        "C:\\work\\proj\\src\\pkg\\sessions.py:15:9",
        "C:/work/proj/src/pkg/sessions.py:15",
        # VS Code reports the drive letter in lower case.
        "c:\\work\\proj\\src\\pkg\\sessions.py:15",
    ):
        assert _qns(graph_query.resolve(graph.fetch_all, P, target)) == INNERMOST


def test_an_absolute_path_through_a_symlinked_root_resolves(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "src" / "pkg").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    graph = Graph(root=str(real.resolve()))
    target = f"{link}/{SESSIONS}:15"
    assert _qns(graph_query.resolve(graph.fetch_all, P, target)) == INNERMOST


def test_parse_location_drops_a_trailing_column() -> None:
    assert graph_query.parse_location(f"{SESSIONS}:500:9") == (SESSIONS, 500)
    assert graph_query.parse_location(f"{SESSIONS}:500") == (SESSIONS, 500)


# --- cgr graph resolve --------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    [
        f"./{SESSIONS}:15",
        f"{SESSIONS}:15:9",
        "src\\pkg\\sessions.py:15",
        "{repo}/src/pkg/sessions.py:15",
    ],
)
def test_cli_resolve_accepts_every_spelling_of_the_path(
    tmp_path: Path, spelling: str
) -> None:
    graph = Graph(root=str(tmp_path))
    result = _cli_resolve(graph, spelling.format(repo=tmp_path), tmp_path)
    assert result.exit_code == 0, result.output
    assert _qns(json.loads(result.stdout)) == INNERMOST


def test_cli_resolve_refuses_a_file_the_project_does_not_hold(tmp_path: Path) -> None:
    result = _cli_resolve(Graph(), "src/pkg/nope.py:15", tmp_path)
    assert result.exit_code != 0, result.output
    assert result.exit_code == cs.GRAPH_EXIT_UNKNOWN_TARGET
    assert result.stdout == ""
    assert "src/pkg/nope.py" in result.stderr
    assert repr(P) in result.stderr


# --- MCP resolve / glosses / annotate -------------------------------------------------


async def test_mcp_resolve_accepts_an_absolute_path_under_the_server_root(
    tmp_path: Path,
) -> None:
    registry = _registry(Graph(root=str(tmp_path)), tmp_path)
    rows = await registry.resolve(f"{tmp_path}/{SESSIONS}:15", project=P)
    assert _qns(rows) == INNERMOST
    rows = await registry.resolve(f"./{SESSIONS}:15:9", project=P)
    assert _qns(rows) == INNERMOST


async def test_mcp_resolve_refuses_a_file_the_project_does_not_hold(
    tmp_path: Path,
) -> None:
    registry = _registry(Graph(), tmp_path)
    result = await registry.resolve("/somewhere/else/sessions.py:15", project=P)
    assert isinstance(result, dict), result
    assert "/somewhere/else/sessions.py" in result[cs.DICT_KEY_ERROR]
    assert repr(P) in result[cs.DICT_KEY_ERROR]


class _GlossGraph(gt.FakeGraph):
    """The gloss fixture graph, also answering which files it holds."""

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        # The parent raises on a query it does not model; only the file
        # lookup is added here.
        try:
            return super().fetch_all(query, params)
        except AssertionError:
            if query != cq.CYPHER_GRAPH_LOCATION_FILES:
                raise
        wanted = (params or {})[cs.CYPHER_PARAM_PATHS]
        assert isinstance(wanted, list)
        return [
            {
                cs.KEY_PATH: n[cs.KEY_PATH],
                cs.KEY_QUALIFIED_NAME: n[cs.KEY_QUALIFIED_NAME],
            }
            for n in self.nodes.values()
            if n[cs.KEY_LABEL] == "Module" and n[cs.KEY_PATH] in wanted
        ]


def _gloss_registry(graph: gt.FakeGraph, root: Path) -> MCPToolsRegistry:
    registry = gt._registry(graph, root)
    registry.ingestor.list_projects.return_value = [gt.P]
    return registry


async def test_mcp_annotate_and_glosses_accept_an_absolute_path_with_a_column(
    tmp_path: Path,
) -> None:
    # Line 12 of app.py is inside Store.get (11-13), the innermost definition.
    graph = _GlossGraph(root=str(tmp_path))
    registry = _gloss_registry(graph, tmp_path)
    target = f"{tmp_path}/app.py:12:5"
    row = await registry.annotate(
        target, gt.BODY, cs.GlossKind.SAFETY_PRECONDITION.value, project=gt.P
    )
    assert isinstance(row, dict)
    assert row.get("target_qn") == gt.STORE_GET, row
    notes = await registry.glosses(".\\app.py:12", project=gt.P)
    assert isinstance(notes, dict)
    assert notes.get("target", {}).get("qualified_name") == gt.STORE_GET, notes
    assert [n["qualified_name"] for n in notes["annotating"]] == [row["qualified_name"]]


async def test_mcp_annotate_refuses_a_file_the_project_does_not_hold(
    tmp_path: Path,
) -> None:
    graph = _GlossGraph()
    registry = _gloss_registry(graph, tmp_path)
    result = await registry.annotate(
        "lib/nope.py:12", gt.BODY, cs.GlossKind.SAFETY_PRECONDITION.value, project=gt.P
    )
    assert isinstance(result, dict)
    assert "lib/nope.py" in result[cs.DICT_KEY_ERROR]
    assert graph.writes == [], "a refused subject writes nothing"


# --- negative: what must not change ---------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [SESSION, "Session", "sessions.Session", "Session.__init__", "__init__"],
)
def test_name_targets_are_not_read_as_locations(target: str) -> None:
    assert graph_query.parse_location(target) is None
    rows = graph_query.resolve(Graph().fetch_all, P, target)
    assert rows, target
    assert all(r["qualified_name"].startswith(f"{P}.") for r in rows)


@pytest.mark.parametrize("target", ["ns::func", "std::vector<int>:x", "a.b:c"])
def test_colon_names_are_not_locations(target: str) -> None:
    assert graph_query.parse_location(target) is None


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("C:\\work\\x.py:7", ("C:\\work\\x.py", 7)),
        ("C:\\work\\x.py:7:3", ("C:\\work\\x.py", 7)),
        ("dir:v2/x.py:10", ("dir:v2/x.py", 10)),
        ("weird:12.py:3", ("weird:12.py", 3)),
    ],
)
def test_windows_drives_and_colons_in_paths_are_not_misparsed(
    target: str, expected: tuple[str, int]
) -> None:
    assert graph_query.parse_location(target) == expected


def test_a_file_name_ending_in_colon_digits_still_resolves() -> None:
    # `src/odd:2:3` is first read as line 2 of `src/odd`; that file does not
    # exist, and line 3 of `src/odd:2` does.
    assert _qns(graph_query.resolve(Graph().fetch_all, P, f"{ODD}:3")) == [RENDER]
    assert _qns(graph_query.resolve(Graph().fetch_all, P, f"./{ODD}:3")) == [RENDER]


def test_a_line_no_definition_spans_in_a_held_file_is_still_empty(
    tmp_path: Path,
) -> None:
    graph = Graph()
    for target in (f"{SESSIONS}:999", f"./{SESSIONS}:3", "README.md:1"):
        assert graph_query.resolve(graph.fetch_all, P, target) == []
        assert graph_query.resolve_or_refuse(graph.fetch_all, P, target) == []
        result = _cli_resolve(graph, target, tmp_path)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == []


async def test_mcp_a_line_no_definition_spans_is_still_empty(tmp_path: Path) -> None:
    registry = _registry(Graph(), tmp_path)
    assert await registry.resolve(f"{SESSIONS}:999", project=P) == []
    assert await registry.resolve("README.md:1", project=P) == []


def test_an_unknown_name_is_still_an_empty_answer(tmp_path: Path) -> None:
    # Unknown NAMES are issue #2461's; only a location's file is checked here.
    assert graph_query.resolve_or_refuse(Graph().fetch_all, P, "nothing") == []
    result = _cli_resolve(Graph(), "nothing", tmp_path)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


def test_another_projects_file_is_not_matched(tmp_path: Path) -> None:
    graph = Graph()
    # The same relative path exists in `other`: only proj's rows come back.
    assert _qns(graph_query.resolve(graph.fetch_all, P, f"{SESSIONS}:15")) == (
        INNERMOST
    )
    # A file only `other` or `proj.extra` holds is not proj's file.
    for target in ("src/pkg/adapters.py:3", "src/only_extra.py:3"):
        assert graph_query.resolve(graph.fetch_all, P, target) == []
        refused = graph_query.resolve_or_refuse(graph.fetch_all, P, target)
        assert isinstance(refused, dict), target
        assert target.split(":")[0] in refused[cs.DICT_KEY_ERROR]
    # An absolute path under OTHER's root is not made relative to it when
    # the project asked is proj.
    target = f"/srv/other/{SESSIONS}:15"
    assert graph_query.resolve(graph.fetch_all, P, target) == []
    assert isinstance(graph_query.resolve_or_refuse(graph.fetch_all, P, target), dict)
    # ... and the other project still answers its own file.
    assert _qns(graph_query.resolve(graph.fetch_all, OTHER, target)) == [
        f"{OTHER}.src.pkg.sessions.request"
    ]


def test_an_absolute_path_outside_every_root_is_refused_not_guessed() -> None:
    target = f"/elsewhere/checkout/{SESSIONS}:15"
    graph = Graph()
    assert graph_query.resolve(graph.fetch_all, P, target) == []
    refused = graph_query.resolve_or_refuse(graph.fetch_all, P, target)
    assert isinstance(refused, dict)
    assert f"/elsewhere/checkout/{SESSIONS}" in refused[cs.DICT_KEY_ERROR]


def test_gloss_resolve_one_names_the_unknown_file() -> None:
    graph = Graph()
    refused = gloss.resolve_one(graph.fetch_all, P, "src/pkg/nope.py:4:2")
    assert isinstance(refused, dict)
    assert "src/pkg/nope.py" in refused[cs.DICT_KEY_ERROR]
    # A held file with no definition at the line keeps the not-found refusal.
    missing = gloss.resolve_one(graph.fetch_all, P, f"{SESSIONS}:999")
    assert isinstance(missing, dict)
    assert missing[cs.DICT_KEY_ERROR] == cs.MCP_GLOSS_TARGET_NOT_FOUND.format(
        target=f"{SESSIONS}:999", project=P
    )


# --- a checkout that is not the project's root (bot review, P1) -----------------


def test_cli_an_unrelated_repo_path_does_not_lend_its_files(tmp_path: Path) -> None:
    # proj was indexed from /srv/proj; --repo-path is some other checkout
    # that happens to have the same relative file.
    result = _cli_resolve(Graph(), f"{tmp_path}/{SESSIONS}:15", tmp_path)
    assert result.exit_code == cs.GRAPH_EXIT_UNKNOWN_TARGET, result.output
    assert result.stdout == ""
    assert f"{tmp_path.as_posix()}/{SESSIONS}" in result.stderr


async def test_mcp_an_unrelated_server_root_does_not_lend_its_files(
    tmp_path: Path,
) -> None:
    registry = _registry(Graph(), tmp_path)
    result = await registry.resolve(f"{tmp_path}/{SESSIONS}:15", project=P)
    assert isinstance(result, dict), result
    assert f"{tmp_path.as_posix()}/{SESSIONS}" in result[cs.DICT_KEY_ERROR]


async def test_mcp_annotate_and_glosses_ignore_an_unrelated_server_root(
    tmp_path: Path,
) -> None:
    # The gloss project was indexed from /elsewhere/foreign-checkout, not
    # from this server's root, which holds its own app.py.
    graph = _GlossGraph()
    registry = _gloss_registry(graph, tmp_path)
    target = f"{tmp_path}/app.py:12"
    written = await registry.annotate(
        target, gt.BODY, cs.GlossKind.SAFETY_PRECONDITION.value, project=gt.P
    )
    assert isinstance(written, dict)
    assert f"{tmp_path.as_posix()}/app.py" in written.get(cs.DICT_KEY_ERROR, ""), (
        written
    )
    assert graph.writes == [], "nothing is written on another checkout's file"
    assert graph.glosses == {}
    notes = await registry.glosses(target, project=gt.P)
    assert isinstance(notes, dict)
    assert "target" not in notes, notes
    assert f"{tmp_path.as_posix()}/app.py" in notes[cs.DICT_KEY_ERROR]


def test_the_projects_own_root_through_a_symlink_still_resolves(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    result = _cli_resolve(Graph(root=str(real)), f"{link}/{SESSIONS}:15", link)
    assert result.exit_code == 0, result.output
    assert _qns(json.loads(result.stdout)) == INNERMOST


# --- `x:a:b` when both `x` and `x:a` are held (bot review, P2) -------------------


def _both_odd_files(base_definitions: bool) -> Graph:
    extra = [_node("Module", f"{P}.src.odd_base", ODD_BASE)]
    if base_definitions:
        extra.append(_node("Function", f"{P}.src.odd_base.top", ODD_BASE, 1, 4))
    return Graph(extra=extra)


@pytest.mark.parametrize("base_definitions", [False, True])
def test_a_column_reading_never_switches_to_another_held_file(
    tmp_path: Path, base_definitions: bool
) -> None:
    # `src/odd:2:3` is line 2 (column 3) of `src/odd` or line 3 of
    # `src/odd:2`; with both files held, either answer may name the wrong
    # file, so neither is given.
    graph = _both_odd_files(base_definitions)
    target = f"{ODD}:3"
    assert graph_query.resolve(graph.fetch_all, P, target) == []
    refused = graph_query.resolve_or_refuse(graph.fetch_all, P, target)
    assert isinstance(refused, dict), refused
    assert repr(ODD_BASE) in refused[cs.DICT_KEY_ERROR]
    assert repr(ODD) in refused[cs.DICT_KEY_ERROR]
    result = _cli_resolve(graph, target, tmp_path)
    assert result.exit_code == cs.GRAPH_EXIT_UNKNOWN_TARGET, result.output
    assert result.stdout == ""
    one = gloss.resolve_one(graph.fetch_all, P, target)
    assert isinstance(one, dict), one
    assert cs.DICT_KEY_ERROR in one, one


def test_the_ambiguous_files_can_each_still_be_named() -> None:
    graph = _both_odd_files(base_definitions=True)
    # An explicit column picks `src/odd:2` line 3 ...
    assert _qns(graph_query.resolve(graph.fetch_all, P, f"{ODD}:3:1")) == [RENDER]
    # ... and no column picks `src/odd` line 2.
    assert _qns(graph_query.resolve(graph.fetch_all, P, f"{ODD_BASE}:2")) == [
        f"{P}.src.odd_base.top"
    ]


def test_a_column_on_the_only_held_reading_still_resolves() -> None:
    # Only `src/pkg/sessions.py` is held, not `src/pkg/sessions.py:15`.
    assert _qns(graph_query.resolve(Graph().fetch_all, P, f"{SESSIONS}:15:9")) == (
        INNERMOST
    )


# --- held files are the project's own Modules (bot review, P2) -------------------


def test_a_file_without_a_module_of_this_project_is_refused() -> None:
    # `src/pkg/adapters.py` has a Module only in `other`; a shared Folder
    # in the real graph would reach its File node from proj's Project too.
    refused = graph_query.resolve_or_refuse(
        Graph().fetch_all, P, "src/pkg/adapters.py:3"
    )
    assert isinstance(refused, dict)
    assert "src/pkg/adapters.py" in refused[cs.DICT_KEY_ERROR]


def test_the_held_file_query_reads_only_the_projects_modules() -> None:
    # File and Folder nodes are shared by absolute path between projects
    # indexed from one root, so the lookup must not walk containment.
    query = cq.CYPHER_GRAPH_LOCATION_FILES
    assert cs.RelationshipType.CONTAINS_FOLDER.value not in query
    assert f":{cs.NodeLabel.FILE.value}" not in query
    assert "$project_prefix" in query


def test_the_target_descriptions_name_the_accepted_path_forms() -> None:
    from codebase_rag.tools import tool_descriptions as td

    for text in (td.MCP_PARAM_TARGET, td.MCP_TOOLS[cs.MCPToolName.RESOLVE]):
        assert "absolute" in text
        assert ":col" in text


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__]))
