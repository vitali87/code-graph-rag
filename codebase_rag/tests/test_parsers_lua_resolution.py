"""`LuaResolver` and its helpers: a table member reached under either of the
two spellings a Lua call and its definition may use, in the caller's own
module or in the table a `require` binds."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.call_site import CallSite
from codebase_rag.parsers.lua.resolution import (
    LuaResolver,
    lua_member_spellings,
    lua_package_methods,
)
from codebase_rag.tests.collaborator_support import edges, index
from evals.recorded_graph import RecordingIngestor

_LUA = cs.SupportedLanguage.LUA
_FILES = {
    "util/strings.lua": (
        "local M = {}\nfunction M.add(a, b) return a + b end\n"
        "function M:twice() return 2 end\nreturn M\n"
    ),
    "main.lua": (
        'local strings = require("util.strings")\n'
        "local Account = {}\n"
        "function Account:deposit(v) return v end\n"
        "function Account.audit() return Account.deposit(Account, 1) end\n"
        "local function run() strings.add(1, 2); strings:twice() end\n"
    ),
}


@pytest.fixture
def resolver(temp_repo: Path, mock_ingestor: RecordingIngestor) -> LuaResolver:
    updater = index(temp_repo / "lp", mock_ingestor, _FILES)
    lua = updater.factory.call_processor._resolver.lua
    assert isinstance(lua, LuaResolver)
    return lua


def test_a_member_is_spelled_both_ways_only_for_lua() -> None:
    assert lua_member_spellings(_LUA, "m.Account", "deposit", ":") == (
        "m.Account:deposit",
        "m.Account.deposit",
    )
    assert lua_member_spellings(cs.SupportedLanguage.PYTHON, "m.A", "f", ".") == ()


def test_package_methods_are_colon_members_one_segment_below_the_file() -> None:
    members = [
        ("lp.util.strings.M:twice", cs.NodeLabel.FUNCTION),
        ("lp.util.strings.M.add", cs.NodeLabel.FUNCTION),
        ("lp.util.strings.deep.M:twice", cs.NodeLabel.FUNCTION),
    ]
    depth = "lp.util.strings.M".count(".") + 1
    assert lua_package_methods(_LUA, members, depth, "twice") == [
        "lp.util.strings.M:twice"
    ]
    assert lua_package_methods(cs.SupportedLanguage.GO, members, depth, "twice") == []


def test_a_dot_call_reaches_a_colon_member_of_the_same_module(
    resolver: LuaResolver, mock_ingestor: RecordingIngestor
) -> None:
    assert resolver.lua_same_module_member("Account.deposit", "lp.main", _LUA) == (
        cs.NodeLabel.FUNCTION,
        "lp.main.Account:deposit",
    )
    assert resolver.lua_same_module_member("run", "lp.main", _LUA) is None
    assert ("lp.main.Account.audit", "lp.main.Account:deposit") in edges(
        mock_ingestor, cs.RelationshipType.CALLS
    )


def test_the_same_module_stage_decides_and_caches_a_hit(
    resolver: LuaResolver,
) -> None:
    site = CallSite("Account:deposit", "lp.main", None, None, None, _LUA, None, False)
    assert resolver.resolve_lua_same_module_member(site, None) == (
        True,
        (cs.NodeLabel.FUNCTION, "lp.main.Account:deposit"),
    )
    miss = site._replace(call_name="Account:withdraw")
    assert resolver.resolve_lua_same_module_member(miss, None) == (False, None)


def test_a_require_alias_reaches_the_table_its_module_returns(
    resolver: LuaResolver, mock_ingestor: RecordingIngestor
) -> None:
    assert resolver.resolve_lua_module_member(
        "lp.util.strings", "strings", "add", ".", "strings.add", _LUA
    ) == (True, (cs.NodeLabel.FUNCTION, "lp.util.strings.M.add"))
    # Decided for Lua even when nothing answers; not decided for another one.
    assert resolver.resolve_lua_module_member(
        "lp.util.strings", "strings", "nope", ".", "strings.nope", _LUA
    ) == (True, None)
    assert resolver.resolve_lua_module_member(
        "lp.util.strings", "strings", "add", ".", "strings.add", cs.SupportedLanguage.JS
    ) == (False, None)
    calls = edges(mock_ingestor, cs.RelationshipType.CALLS)
    assert ("lp.main.run", "lp.util.strings.M:twice") in calls
