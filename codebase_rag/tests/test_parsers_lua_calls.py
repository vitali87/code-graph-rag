"""`LuaCallResolution` names a Lua caller as the definition pass names it, so
the calls in a function value's body are attributed to the node registered
for it: a table field's key path, an assignment target, or the generated
name of a function the module returns or files under a bracket key."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.parsers.lua.calls import LuaCallResolution, caller_func_name
from codebase_rag.tests.collaborator_support import edges, index, nodes_of, parse
from evals.recorded_graph import RecordingIngestor

_MAIN = (
    "local function run() return 1 end\n"
    "local t = { set = function() run() end, function() run() end }\n"
    "local f = function() run() end\n"
    'x["a.b"] = function() run() end\n'
    "return function() run() end\n"
)


def _values() -> list:
    return nodes_of(parse(cs.SupportedLanguage.LUA, _MAIN), "function_definition")


def test_each_function_values_calls_belong_to_its_registered_node(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "lp", mock_ingestor, {"main.lua": _MAIN})
    strategy = updater.factory.call_processor._calls_for(cs.SupportedLanguage.LUA)
    assert isinstance(strategy, LuaCallResolution)
    callers = {
        source
        for source, target in edges(mock_ingestor, cs.RelationshipType.CALLS)
        if target == "lp.main.run"
    }
    assert callers == {
        "lp.main.t.set",
        "lp.main.f",
        "lp.main.anonymous_3_11",
        "lp.main.anonymous_4_7",
    }


def test_caller_names_follow_the_definition_pass() -> None:
    field, keyless, local_value, bracket, returned = _values()
    assert caller_func_name(field) == "t.set"
    assert caller_func_name(local_value) == "f"
    assert caller_func_name(bracket) == "anonymous_3_11"
    assert caller_func_name(returned) == "anonymous_4_7"
    # A keyless field is anonymous in the definition pass, so no caller.
    assert caller_func_name(keyless) is None


def test_a_declared_function_keeps_its_own_name() -> None:
    root = parse(cs.SupportedLanguage.LUA, "local function run() end\n")
    (declaration,) = root.children
    assert LuaCallResolution().caller_name(declaration, "lp.main") == "run"
    # Only a function value is named by its surroundings.
    assert LuaCallResolution().caller_name(root, "lp.main") is None
