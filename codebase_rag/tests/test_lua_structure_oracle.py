# Covers the Lua structure oracle harness (evals/oracles/lua_oracle +
# evals/lua_l1.py): luaparse is authoritative ground truth, and cgr's Lua
# nodes are graded against it on (kind, file, start_line). Lua has no
# classes, so every function is a Function.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_lua_nodes
from evals.oracles import lua_oracle_available, run_lua_oracle
from evals.score import score_node_kinds
from evals.types_defs import GraphData

LUA_SRC = """\
local M = {}
function freeFn(a) return a + 1 end
local function localFn(b) return b end
function M.tableFn(c) return c end
function M:methodFn(d) return d end
local arrow = function(e) return e end
return M
"""


def _require_lua() -> None:
    if not lua_oracle_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.LUA not in load_parsers()[0]:
        pytest.skip("lua parser not available")


def test_cgr_matches_luaparse_oracle_on_lua_structure(tmp_path: Path) -> None:
    _require_lua()
    project = tmp_path / "lua_oracle_test"
    project.mkdir()
    (project / "m.lua").write_text(LUA_SRC, encoding="utf-8")

    cgr = GraphData(
        nodes=extract_cgr_lua_nodes(project, project.name),
        edges=set(),
        name_edges=set(),
    )
    oracle = run_lua_oracle(project)

    result = score_node_kinds(cgr, oracle, ec.LUA_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    row = by_label.get(cs.NodeLabel.FUNCTION.value)
    assert row is not None, by_label
    assert row["precision"] == 1.0 and row["recall"] == 1.0, row
