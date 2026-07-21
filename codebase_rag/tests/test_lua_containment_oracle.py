# Covers Lua containment-edge validation. Lua has no classes/methods, so the
# only containment edge is DEFINES: the module DEFINES top-level functions,
# and a function DEFINES those nested in its body. Graded against the
# luaparse oracle, joined on (kind, file, line).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_lua_graph
from evals.oracles import lua_oracle_available, run_lua_oracle
from evals.score import score_edge_types

LUA_SRC = """\
local function freeFn(a)
    return a + 1
end

function globalFn()
    local function nested()
        return 1
    end
    return nested
end

local cb = function(x) return x end
"""


def _require_lua() -> None:
    if not lua_oracle_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.LUA not in load_parsers()[0]:
        pytest.skip("lua parser not available")


def test_cgr_matches_luaparse_oracle_on_containment_edges(tmp_path: Path) -> None:
    _require_lua()
    project = tmp_path / "lua_edge"
    project.mkdir()
    (project / "lib.lua").write_text(LUA_SRC, encoding="utf-8")

    cgr = extract_cgr_lua_graph(project, project.name)
    oracle = run_lua_oracle(project)

    result = score_edge_types(cgr, oracle, ec.SCORED_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    # Lua only has DEFINES (no methods, so no DEFINES_METHOD row at all).
    row = by_label.get(cs.RelationshipType.DEFINES.value)
    assert row is not None, (by_label, result.diff)
    assert row["precision"] == 1.0 and row["recall"] == 1.0, (row, result.diff)
    assert cs.RelationshipType.DEFINES_METHOD.value not in by_label, by_label
