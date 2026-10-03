# `cgr graph importers --through-reexports` / MCP `importers` with
# `through_reexports` (issue #2573). Libraries expose their internals through
# a facade -- `pkg/__init__.py`, Rust `lib.rs` with `pub use`, a TS `index.ts`
# barrel -- so the direct importers of an internal module are the facade and
# a handful of siblings, and every real consumer stays invisible. The fake
# graph below reproduces the IMPORTS edge shapes the indexer writes for each
# of those (alias / imported_name as dumped from a real index), and the walk
# must reach the consumers through them by NAME, never by mere transitive
# import.
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.graph_cli import cli as graph_cli
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.types_defs import PropertyDict, ResultRow

P = "proj"
# A registered project whose name extends this one: its rows start with
# `proj.` and pass every prefix filter, so only ownership can drop them.
EXTRA = "proj.extra"

# (importer qn, importer path, imported qn, line, col, end_line, end_col,
#  alias, imported_name)
Edge = tuple[str, str, str, int, int, int, int, str | None, str | None]

EDGES: list[Edge] = [
    # Python, httpx-shaped: the facade names Client and star-imports _models.
    (
        f"{P}.shop",
        "shop/__init__.py",
        f"{P}.shop._client",
        2,
        0,
        2,
        27,
        "Client",
        "Client",
    ),
    (f"{P}.shop", "shop/__init__.py", f"{P}.shop._models", 3, 0, 3, 23, None, "*"),
    # A sibling that imports Client for its own use: not a facade.
    (
        f"{P}.shop._api",
        "shop/_api.py",
        f"{P}.shop._client",
        1,
        0,
        1,
        27,
        "Client",
        "Client",
    ),
    (f"{P}.app", "app.py", f"{P}.shop", 1, 0, 1, 11, "shop", None),
    (f"{P}.cli", "cli.py", f"{P}.shop", 1, 0, 1, 23, "Client", "Client"),
    (f"{P}.report", "report.py", f"{P}.shop", 1, 0, 1, 25, "Response", "Response"),
    # Imports the internal module directly AND the facade.
    (f"{P}.direct", "direct.py", f"{P}.shop._client", 1, 0, 1, 31, "Client", "Client"),
    (f"{P}.direct", "direct.py", f"{P}.shop", 2, 0, 2, 11, "shop", None),
    (
        f"{P}.uses_api",
        "uses_api.py",
        f"{P}.shop._api",
        1,
        0,
        1,
        29,
        "request",
        "request",
    ),
    (f"{P}.whole_api", "whole_api.py", f"{P}.shop._api", 1, 0, 1, 25, "api", None),
    (
        f"{P}.tests.test_client",
        "tests/test_client.py",
        f"{P}.shop",
        1,
        0,
        1,
        11,
        "shop",
        None,
    ),
    # A sub-facade re-exporting the facade's name, and its consumer: 2 hops.
    (
        f"{P}.plugins",
        "plugins/__init__.py",
        f"{P}.shop",
        1,
        0,
        1,
        23,
        "Client",
        "Client",
    ),
    (
        f"{P}.plugin_user",
        "plugin_user.py",
        f"{P}.plugins",
        3,
        0,
        3,
        26,
        "Client",
        "Client",
    ),
    # The extending project: a consumer of the facade, and a facade of its
    # own whose consumer in THIS project must not be reached through it.
    (f"{EXTRA}.app", "app.py", f"{P}.shop", 1, 0, 1, 11, "shop", None),
    (
        f"{EXTRA}.pkg",
        "pkg/__init__.py",
        f"{P}.shop._client",
        1,
        0,
        1,
        27,
        "Client",
        "Client",
    ),
    (
        f"{P}.via_foreign",
        "via_foreign.py",
        f"{EXTRA}.pkg",
        1,
        0,
        1,
        30,
        "Client",
        "Client",
    ),
    # Rust, mini-redis-shaped: `pub use crate::frame::Frame;` in lib.rs.
    (f"{P}.src.lib", "src/lib.rs", f"{P}.src.frame", 4, 0, 4, 28, "Frame", "Frame"),
    (
        f"{P}.src.connection",
        "src/connection.rs",
        f"{P}.src.lib",
        1,
        0,
        1,
        17,
        "Frame",
        "Frame",
    ),
    (
        f"{P}.src.cmd",
        "src/cmd.rs",
        f"{P}.src.lib",
        1,
        0,
        1,
        22,
        "Connection",
        "Connection",
    ),
    (
        f"{P}.src.server",
        "src/server.rs",
        f"{P}.src.frame",
        1,
        0,
        1,
        24,
        "Frame",
        "Frame",
    ),
    # `pub use crate::frame::*;` in a mod.rs: a glob is written with the
    # module's own name and no alias.
    (f"{P}.src.net", "src/net/mod.rs", f"{P}.src.frame", 1, 0, 1, 24, None, "frame"),
    (f"{P}.src.modimp", "src/modimp.rs", f"{P}.src.net", 3, 0, 3, 22, "Frame", "Frame"),
    # `use crate::connection;`: the whole of a module that only USES Frame.
    (
        f"{P}.src.whole",
        "src/whole.rs",
        f"{P}.src.connection",
        1,
        0,
        1,
        22,
        "connection",
        "connection",
    ),
    # TS barrel: `export { Frame } from './frame'` in index.ts.
    (f"{P}.web.index", "web/index.ts", f"{P}.web.frame", 1, 0, 1, 32, "Frame", "Frame"),
    (f"{P}.web.conn", "web/conn.ts", f"{P}.web.index", 1, 0, 1, 32, "Frame", "Frame"),
    (f"{P}.web.wire", "web/wire.ts", f"{P}.web.index", 1, 0, 1, 33, "encode", "encode"),
    # `import * as w from './index'`: a namespace import of the barrel.
    (f"{P}.web.ns", "web/ns.ts", f"{P}.web.index", 1, 0, 1, 29, "w", "*"),
]


def _row(edge: Edge) -> ResultRow:
    src, path, _dst, line, col, end_line, end_col, alias, name = edge
    return {
        cs.KEY_QUALIFIED_NAME: src,
        cs.KEY_PATH: path,
        cs.KEY_LINE: line,
        cs.KEY_COL: col,
        cs.KEY_END_LINE: end_line,
        cs.KEY_END_COL: end_col,
        cs.KEY_ALIAS: alias,
        cs.KEY_IMPORTED_NAME: name,
    }


def _fetch_for(edges: list[Edge], budget: int = 100):
    """A fetch answering the importer reads from `edges`, refusing anything
    else and failing loudly past `budget` calls, so a walk that never
    terminates fails the test instead of hanging it."""
    calls = 0

    def fetch(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        nonlocal calls
        calls += 1
        assert calls <= budget, "the walk did not terminate"
        if query == cq.CYPHER_LIST_PROJECTS:
            return [{cs.KEY_NAME: name} for name in (P, EXTRA)]
        p = params or {}
        assert p.get(cs.KEY_PROJECT_PREFIX) == f"{P}.", "every read is scoped"
        if cs.KEY_QNS in p:
            wanted = set(p[cs.KEY_QNS])
            out = [
                {**_row(e), cs.KEY_TO_QN: e[2]}
                for e in edges
                if e[2] in wanted and e[0].startswith(f"{P}.")
            ]
        else:
            assert query == cq.CYPHER_GRAPH_IMPORTERS
            out = [
                _row(e)
                for e in edges
                if e[2] == p.get(cs.KEY_QN) and e[0].startswith(f"{P}.")
            ]
        # Deliberately unsorted: the walk must order its own output.
        return list(reversed(out))

    return fetch


def _hops(
    rows: list[graph_query.ReexportImporterRow],
) -> set[tuple[str, tuple[str, ...]]]:
    return {(r["module"], tuple(h["module"] for h in r["via"])) for r in rows}


def _through(
    target: str, edges: list[Edge] = EDGES
) -> list[graph_query.ReexportImporterRow]:
    return graph_query.importers_through_reexports(_fetch_for(edges), P, target)


# --- the consumers the issue says are invisible ---------------------------------


def test_a_consumer_of_the_package_facade_is_listed_with_its_via_hop() -> None:
    rows = _through(f"{P}.shop._client")
    app = next(r for r in rows if r["module"] == f"{P}.app")
    # The consumer's own statement, then the facade statement it came through.
    assert app == {
        "module": f"{P}.app",
        "path": "app.py",
        "line": 1,
        "col": 0,
        "end_line": 1,
        "end_col": 11,
        "alias": "shop",
        "imported_name": None,
        "via": [
            {
                "module": f"{P}.shop",
                "path": "shop/__init__.py",
                "line": 2,
                "col": 0,
                "end_line": 2,
                "end_col": 27,
                "alias": "Client",
                "imported_name": "Client",
            }
        ],
    }


def test_python_facade_consumers_by_name_and_whole_module_are_reached() -> None:
    hops = _hops(_through(f"{P}.shop._client"))
    assert (f"{P}.cli", (f"{P}.shop",)) in hops
    assert (f"{P}.tests.test_client", (f"{P}.shop",)) in hops


def test_rust_pub_use_in_lib_rs_is_followed() -> None:
    assert _hops(_through(f"{P}.src.frame")) == {
        (f"{P}.src.lib", ()),
        (f"{P}.src.server", ()),
        (f"{P}.src.net", ()),
        (f"{P}.src.connection", (f"{P}.src.lib",)),
        # Through the glob in net/mod.rs, which forwards every name.
        (f"{P}.src.modimp", (f"{P}.src.net",)),
    }


def test_a_ts_barrel_is_followed_by_name_and_by_namespace_import() -> None:
    assert _hops(_through(f"{P}.web.frame")) == {
        (f"{P}.web.index", ()),
        (f"{P}.web.conn", (f"{P}.web.index",)),
        (f"{P}.web.ns", (f"{P}.web.index",)),
    }


def test_a_chain_of_facades_lists_every_hop_consumer_side_first() -> None:
    rows = _through(f"{P}.shop._client")
    user = next(r for r in rows if r["module"] == f"{P}.plugin_user")
    assert [(h["path"], h["line"]) for h in user["via"]] == [
        ("plugins/__init__.py", 1),
        ("shop/__init__.py", 2),
    ]


def test_a_star_reexport_forwards_the_names_it_does_not_list() -> None:
    hops = _hops(_through(f"{P}.shop._models"))
    assert (f"{P}.report", (f"{P}.shop",)) in hops


def test_rows_are_ordered_direct_first_then_by_module() -> None:
    rows = _through(f"{P}.shop._client")
    keys = [(len(r["via"]), r["module"]) for r in rows]
    assert keys == sorted(keys)
    assert [r["module"] for r in rows if not r["via"]] == [
        f"{P}.direct",
        f"{P}.shop",
        f"{P}.shop._api",
    ]


def test_the_output_does_not_depend_on_the_fetch_order() -> None:
    forward = _through(f"{P}.shop._client")
    backward = _through(f"{P}.shop._client", list(reversed(EDGES)))
    assert json.dumps(forward) == json.dumps(backward)


# --- what must NOT be reached ------------------------------------------------------


def test_a_name_the_facade_does_not_take_from_the_target_is_not_followed() -> None:
    modules = {m for m, _via in _hops(_through(f"{P}.shop._client"))}
    # `from shop import Response`: the facade's Response is not _client's.
    assert f"{P}.report" not in modules
    assert f"{P}.src.cmd" not in {m for m, _ in _hops(_through(f"{P}.src.frame"))}
    assert f"{P}.web.wire" not in {m for m, _ in _hops(_through(f"{P}.web.frame"))}


def test_a_module_that_only_uses_the_target_is_not_treated_as_a_facade() -> None:
    modules = {m for m, _via in _hops(_through(f"{P}.shop._client"))}
    # `from shop._api import request` names nothing _api took from _client,
    # and `import shop._api as api` is the whole of a plain module.
    assert f"{P}.uses_api" not in modules
    assert f"{P}.whole_api" not in modules
    # `use crate::connection;`: the whole of a module that only uses Frame.
    assert f"{P}.src.whole" not in {m for m, _ in _hops(_through(f"{P}.src.frame"))}


def test_a_direct_importer_also_reaching_through_the_facade_is_listed_once() -> None:
    rows = [r for r in _through(f"{P}.shop._client") if r["module"] == f"{P}.direct"]
    assert len(rows) == 1
    # Its own direct statement, not the `import shop` one.
    assert (rows[0]["line"], rows[0]["via"]) == (1, [])


def test_the_other_projects_modules_are_neither_listed_nor_hops() -> None:
    modules = {m for m, _via in _hops(_through(f"{P}.shop._client"))}
    assert f"{EXTRA}.app" not in modules
    assert f"{EXTRA}.pkg" not in modules
    assert f"{P}.via_foreign" not in modules


def test_a_reexport_cycle_terminates_and_lists_each_module_once() -> None:
    cycle: list[Edge] = [
        (f"{P}.pkg", "pkg/__init__.py", f"{P}.pkg._core", 1, 0, 1, 25, "Core", "Core"),
        (
            f"{P}.pkg",
            "pkg/__init__.py",
            f"{P}.pkg._compat",
            2,
            0,
            2,
            27,
            "Core",
            "Core",
        ),
        # The compat shim takes Core back from the facade: pkg <-> _compat.
        (f"{P}.pkg._compat", "pkg/_compat.py", f"{P}.pkg", 1, 0, 1, 20, "Core", "Core"),
        # The target itself imports from its own facade.
        (f"{P}.pkg._core", "pkg/_core.py", f"{P}.pkg", 5, 0, 5, 22, "helper", "helper"),
        (f"{P}.user", "user.py", f"{P}.pkg", 1, 0, 1, 20, "Core", "Core"),
    ]
    rows = graph_query.importers_through_reexports(
        _fetch_for(cycle, budget=10), P, f"{P}.pkg._core"
    )
    assert _hops(rows) == {
        (f"{P}.pkg", ()),
        (f"{P}.pkg._compat", (f"{P}.pkg",)),
        (f"{P}.user", (f"{P}.pkg",)),
    }
    assert len(rows) == 3


def test_a_name_reaching_a_listed_module_later_still_reaches_its_consumers() -> None:
    # `mid` imports Short from the target, so it is listed as a direct
    # importer; Long reaches it one hop later, through the facade. A module
    # importing Long from `mid` reaches the target through `mid` and `pkg`.
    late: list[Edge] = [
        (f"{P}.mid", "mid.py", f"{P}.core", 1, 0, 1, 22, "Short", "Short"),
        (f"{P}.pkg", "pkg/__init__.py", f"{P}.core", 1, 0, 1, 21, "Long", "Long"),
        (f"{P}.mid", "mid.py", f"{P}.pkg", 2, 0, 2, 20, "Long", "Long"),
        (f"{P}.consumer", "consumer.py", f"{P}.mid", 1, 0, 1, 20, "Long", "Long"),
    ]
    rows = graph_query.importers_through_reexports(
        _fetch_for(late, budget=10), P, f"{P}.core"
    )
    assert _hops(rows) == {
        (f"{P}.mid", ()),
        (f"{P}.pkg", ()),
        (f"{P}.consumer", (f"{P}.mid", f"{P}.pkg")),
    }
    # `mid` is still listed once, at the depth it was first reached.
    assert len(rows) == 3


def test_a_ts_named_import_sharing_the_barrels_name_is_not_the_whole_barrel() -> None:
    # `import { index } from './index'` binds the barrel's own `index`
    # export: a JS/TS named import is never the whole module, whatever its
    # name (#2573 review).
    barrel: list[Edge] = [
        (
            f"{P}.web.index",
            "web/index.ts",
            f"{P}.web.frame",
            1,
            0,
            1,
            32,
            "Frame",
            "Frame",
        ),
        (
            f"{P}.web.conn",
            "web/conn.ts",
            f"{P}.web.index",
            1,
            0,
            1,
            32,
            "Frame",
            "Frame",
        ),
        (
            f"{P}.web.named",
            "web/named.ts",
            f"{P}.web.index",
            1,
            0,
            1,
            32,
            "index",
            "index",
        ),
    ]
    rows = graph_query.importers_through_reexports(
        _fetch_for(barrel, budget=10), P, f"{P}.web.frame"
    )
    assert _hops(rows) == {
        (f"{P}.web.index", ()),
        (f"{P}.web.conn", (f"{P}.web.index",)),
    }


def test_a_whole_module_import_under_its_own_name_still_reaches() -> None:
    # Negative: Rust `use crate::net;` and Python `from shop import plugins`
    # record a whole-module import under the module's own name, so a facade
    # imported that way is still walked through.
    own_name: list[Edge] = [
        (
            f"{P}.src.net",
            "src/net/mod.rs",
            f"{P}.src.frame",
            1,
            0,
            1,
            24,
            None,
            "frame",
        ),
        (f"{P}.src.user", "src/user.rs", f"{P}.src.net", 1, 0, 1, 13, "net", "net"),
        (
            f"{P}.shop.plugins",
            "shop/plugins/__init__.py",
            f"{P}.shop._client",
            1,
            0,
            1,
            27,
            "Client",
            "Client",
        ),
        (
            f"{P}.consumer",
            "consumer.py",
            f"{P}.shop.plugins",
            1,
            0,
            1,
            25,
            "plugins",
            "plugins",
        ),
    ]
    rust = graph_query.importers_through_reexports(
        _fetch_for(own_name, budget=10), P, f"{P}.src.frame"
    )
    python = graph_query.importers_through_reexports(
        _fetch_for(own_name, budget=10), P, f"{P}.shop._client"
    )
    assert (f"{P}.src.user", (f"{P}.src.net",)) in _hops(rust)
    assert (f"{P}.consumer", (f"{P}.shop.plugins",)) in _hops(python)


def test_an_unknown_target_answers_empty_as_the_direct_query_does() -> None:
    fetch = _fetch_for(EDGES)
    assert graph_query.importers(fetch, P, f"{P}.nope") == []
    assert graph_query.importers_through_reexports(fetch, P, f"{P}.nope") == []


def test_the_direct_query_is_unchanged() -> None:
    rows = graph_query.importers(_fetch_for(EDGES), P, f"{P}.shop._client")
    assert rows == [
        {
            "module": f"{P}.direct",
            "path": "direct.py",
            "line": 1,
            "col": 0,
            "end_line": 1,
            "end_col": 31,
            "alias": "Client",
            "imported_name": "Client",
        },
        {
            "module": f"{P}.shop",
            "path": "shop/__init__.py",
            "line": 2,
            "col": 0,
            "end_line": 2,
            "end_col": 27,
            "alias": "Client",
            "imported_name": "Client",
        },
        {
            "module": f"{P}.shop._api",
            "path": "shop/_api.py",
            "line": 1,
            "col": 0,
            "end_line": 1,
            "end_col": 27,
            "alias": "Client",
            "imported_name": "Client",
        },
    ]


# --- cgr graph importers / MCP importers ----------------------------------------


def _mock_connect() -> MagicMock:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=_fetch_for(EDGES))
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)
    return ingestor


def _cli(tmp_path: Path, *extra: str) -> str:
    args = ["importers", f"{P}.shop._client", "--project", P]
    args += ["--repo-path", str(tmp_path), *extra]
    with patch(
        "codebase_rag.cli_runtime.connect_memgraph", return_value=_mock_connect()
    ):
        result = CliRunner().invoke(graph_cli, args)
    assert result.exit_code == 0, result.output
    return result.output


def _json(rows: object) -> str:
    return json.dumps(rows, indent=cs.MCP_JSON_INDENT, sort_keys=True) + "\n"


def test_cli_through_reexports_lists_the_facade_consumers(tmp_path: Path) -> None:
    rows = json.loads(_cli(tmp_path, "--through-reexports"))
    assert {(r["module"], len(r["via"])) for r in rows} >= {
        (f"{P}.app", 1),
        (f"{P}.cli", 1),
        (f"{P}.plugin_user", 2),
    }


def test_cli_without_the_flag_prints_exactly_the_direct_rows(tmp_path: Path) -> None:
    direct = graph_query.importers(_fetch_for(EDGES), P, f"{P}.shop._client")
    assert _cli(tmp_path) == _json(direct)


def test_cli_help_says_the_default_is_direct_importers_only() -> None:
    result = CliRunner().invoke(graph_cli, ["importers", "--help"])
    assert result.exit_code == 0
    # Click wraps the help to the terminal width.
    text = " ".join(result.output.split())
    assert "--through-reexports" in text
    assert "Direct importers only, unless --through-reexports." in text


@pytest.fixture
def registry(tmp_path: Path) -> MCPToolsRegistry:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=_fetch_for(EDGES, budget=1000))
    ingestor.list_projects.return_value = [P, EXTRA]
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        return MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=ingestor, cypher_gen=MagicMock()
        )


def test_mcp_importers_declares_through_reexports_as_an_optional_boolean(
    registry: MCPToolsRegistry,
) -> None:
    schema = registry._tools[cs.MCPToolName.IMPORTERS].input_schema
    prop = schema["properties"][cs.MCPParamName.THROUGH_REEXPORTS]
    assert prop["type"] == cs.MCPSchemaType.BOOLEAN
    assert cs.MCPParamName.THROUGH_REEXPORTS not in schema["required"]


async def test_mcp_through_reexports_answers_what_the_cli_does(
    registry: MCPToolsRegistry, tmp_path: Path
) -> None:
    rows = await registry.importers(
        f"{P}.shop._client", through_reexports=True, project=P
    )
    assert _json(rows) == _cli(tmp_path, "--through-reexports")


async def test_mcp_importers_default_is_the_direct_rows(
    registry: MCPToolsRegistry, tmp_path: Path
) -> None:
    rows = await registry.importers(f"{P}.shop._client", project=P)
    assert _json(rows) == _cli(tmp_path)
    assert all("via" not in r for r in rows)
