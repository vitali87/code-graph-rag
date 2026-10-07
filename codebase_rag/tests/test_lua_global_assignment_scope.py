"""Issue #2889: Lua `global_assignment` flags a leaked global, not a local's update.

The rule matched every bare-identifier assignment outside a `local`
statement, so `local count = 0; count = count + 1` read as a leaked global:
reassigning a local, one of the commonest things Lua code does, fired every
time. Whether the name is a local is a question of lexical scope, which an
ast-grep pattern cannot answer, so the rule now names a scope filter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_ast_grep_analyzer import _fire

pytest.importorskip("ast_grep_py")

GLOBAL_ASSIGNMENT = "global_assignment"


def _flagged(tmp_path: Path, src: str) -> list[int]:
    return sorted(
        int(p[cs.KEY_START_LINE])
        for p in _fire(tmp_path, "m.lua", src)
        if p[cs.KEY_NAME] == GLOBAL_ASSIGNMENT
    )


def test_the_issue_flags_only_the_real_global(tmp_path: Path) -> None:
    src = (
        "local count = 0\n"
        "count = count + 1\n"  # 2: a local
        "realGlobal = 5\n"  # 3: the leak
        "local function worker()\n"
        "    local total = 0\n"
        "    total = total + 10\n"  # 6: a local in a function
        "    return total\n"
        "end\n"
        "local t = {}\n"
        "t.field = 99\n"
    )
    assert _flagged(tmp_path, src) == [3]


def test_every_way_lua_binds_a_local_is_seen(tmp_path: Path) -> None:
    src = (
        "local a, b\n"
        "a, b = 1, 2\n"  # multi-name local
        "local x <const> = 1\n"
        "x = 2\n"  # attributed local
        "local function f(p, ...)\n"
        "    p = 1\n"  # parameter
        "    f = nil\n"  # a local function's own name
        "    for i = 1, 3 do i = i + 1 end\n"  # numeric for variable
        "    for k, v in pairs({}) do v = k end\n"  # generic for variables
        "    if p then\n"
        "        a = 3\n"  # an outer local, two blocks up
        "        local g = function(q) q = 4; b = 5 end\n"  # closure
        "    end\n"
        "end\n"
        "local M = {}\n"
        "function M:method(s) s = 1; self = nil end\n"  # method's implicit self
        "function M.fn(r) r = 2 end\n"
    )
    assert _flagged(tmp_path, src) == []


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        # Assigned before the `local` that would bind it.
        ("early = 1\nlocal early = 2\n", [1]),
        # The block that declared it has ended.
        ("do local z = 1 end\nz = 2\n", [2]),
        # Declared in a sibling function, not an enclosing one.
        (
            "local function a() local y = 1 end\nlocal function b() y = 2 end\n",
            [2],
        ),
        # A local initialiser cannot see the local it initialises.
        ("local f = function() f = 1 end\n", [1]),
        # One bare target of a mixed assignment is still global.
        ("local known\nknown, leaked = 1, 2\n", [2]),
        # A global function's name is a global.
        ("function g() end\ng = nil\n", [2]),
        # A loop variable is local to its loop only.
        ("for i = 1, 2 do end\ni = 3\n", [2]),
    ],
)
def test_a_global_is_still_flagged(
    tmp_path: Path, src: str, expected: list[int]
) -> None:
    assert _flagged(tmp_path, src) == expected


def test_an_unknown_filter_is_a_malformed_rule_file(tmp_path: Path) -> None:
    from codebase_rag.analyzers.ast_grep_analyzer import _parse_rule_file

    rules = tmp_path / "x.yaml"
    rules.write_text(
        "ast_grep_id: lua\nextensions: [.lua]\nrules:\n"
        "  - id: r\n    filter: no_such_filter\n    rule: { kind: chunk }\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no_such_filter"):
        _parse_rule_file(rules, cs.NodeLabel.CODE_SMELL, cs.RelationshipType.HAS_SMELL)
