from pathlib import Path

import pytest

from evals import constants as ec
from evals.lua_retrieval import (
    cgr_lua_call_edges,
    oracle_lua_call_edges,
    score_lua_retrieval,
)
from evals.oracles import lua_oracle_available

needs_node = pytest.mark.skipif(
    not lua_oracle_available(), reason="node toolchain not installed"
)


def _make_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "mod.lua").write_text(
        "local M = {}\n"
        "local function local_add(a, b) return a + b end\n"
        "function M.mul(a, b) return a * b end\n"
        "function M.use(x, y) return local_add(x, y) + M.mul(x, y) end\n"
        "function M.orphan() return 9 end\n"
        "return M\n",
        encoding="utf-8",
    )
    (root / "main.lua").write_text(
        "local mod = require('mod')\n"
        "local function compute(x, y) return mod.use(x, y) end\n"
        "local r = compute(4, 2)\n",
        encoding="utf-8",
    )


@needs_node
def test_oracle_captures_first_party_lua_calls(tmp_path: Path) -> None:
    _make_project(tmp_path)
    edges, declared = oracle_lua_call_edges(tmp_path)

    # (H) local_add(), M.mul() (in M.use), mod.use() (in compute), compute()
    # (H) (top level) are first-party calls reduced to their simple names.
    assert ("mod.lua", "local_add") in edges
    assert ("mod.lua", "mul") in edges
    assert ("main.lua", "use") in edges
    assert ("main.lua", "compute") in edges
    # (H) orphan is declared but never called -> never a call edge.
    assert ("mod.lua", "orphan") not in edges
    assert {"local_add", "mul", "use", "orphan", "compute"} <= declared


@needs_node
def test_cgr_matches_oracle_on_clean_lua_project(tmp_path: Path) -> None:
    _make_project(tmp_path)
    oracle, declared = oracle_lua_call_edges(tmp_path)
    cgr = cgr_lua_call_edges(tmp_path, tmp_path.name, declared)
    assert cgr == oracle


@needs_node
def test_cgr_resolves_function_expression_body_calls(tmp_path: Path) -> None:
    # (H) A function expression bound to a table field (`M.runner = function()...`)
    # (H) is named by cgr's definition pass (qn M.runner), so the calls in its body
    # (H) must attribute to that node. cgr previously skipped the whole body because
    # (H) the call pass could not derive the caller name from the nameless function
    # (H) expression (same family as the JS/TS arrow-caller gap).
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "fnexpr.lua").write_text(
        "local M = {}\n"
        "local function target() return 1 end\n"
        "M.runner = function() return target() end\n"
        "return M\n",
        encoding="utf-8",
    )
    oracle, declared = oracle_lua_call_edges(tmp_path)
    assert ("fnexpr.lua", "target") in oracle
    cgr = cgr_lua_call_edges(tmp_path, tmp_path.name, declared)
    assert ("fnexpr.lua", "target") in cgr


def test_score_lua_retrieval_prf() -> None:
    result = score_lua_retrieval(
        {("a.lua", "f"), ("a.lua", "g")}, {("a.lua", "f"), ("b.lua", "h")}
    )
    row = next(r for r in result.rows if r["label"] == ec.LUA_RETRIEVAL_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
