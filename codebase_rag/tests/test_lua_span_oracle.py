# Covers Lua node SPAN (end_line) validation: cgr's end_line for each Function
# is graded against the luaparse oracle (node.loc.end.line), joined on
# (kind, file, start). Exercises a global, a nested, and a multi-line
# anonymous function expression so spans are not single line.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_lua_graph
from evals.oracles import lua_oracle_available, run_lua_oracle
from evals.score import score_span

LUA_SRC = """\
function outer(a, b)
    local function inner(x)
        return x + 1
    end
    return inner(a) + b
end

local handler = function(v)
    return v * 2
end

return outer(handler(1), 2)
"""


def _require_lua() -> None:
    if not lua_oracle_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.LUA not in load_parsers()[0]:
        pytest.skip("lua parser not available")


def test_cgr_matches_luaparse_oracle_on_node_spans(tmp_path: Path) -> None:
    _require_lua()
    project = tmp_path / "lua_span_test"
    project.mkdir()
    (project / "lib.lua").write_text(LUA_SRC, encoding="utf-8")

    cgr = extract_cgr_lua_graph(project, project.name)
    oracle = run_lua_oracle(project)

    result = score_span(cgr, oracle, ec.LUA_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    aggregate = by_label.get(ec.AGGREGATE_LABEL)
    assert aggregate is not None, (by_label, result.diff)
    assert aggregate["precision"] == 1.0 and aggregate["recall"] == 1.0, (
        aggregate,
        result.diff,
    )
    assert aggregate["tp"] >= 3, aggregate
