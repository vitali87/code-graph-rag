"""`LuaFunctionIngest` names a Lua function value that has no name of its
own: by the table field it is built into, else by the assignment that binds
it, else not at all."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.parsers.lua.function_ingest import LuaFunctionIngest, function_name
from codebase_rag.tests.collaborator_support import index, nodes_of, parse
from codebase_rag.tests.conftest import get_nodes
from evals.recorded_graph import RecordingIngestor

_SOURCE = (
    "local t = { set = function() end, function() end }\n"
    "local f = function() end\n"
    "M.g = function() end\n"
)


def _function_values() -> list:
    return nodes_of(parse(cs.SupportedLanguage.LUA, _SOURCE), "function_definition")


def test_a_field_value_is_named_by_its_key_under_its_table() -> None:
    field_value = _function_values()[0]
    assert function_name(field_value) == ("t.set", "set")
    assert LuaFunctionIngest().nameless_function_name(field_value) == ("t.set", "set")


def test_a_field_without_a_key_stays_anonymous() -> None:
    # Naming it after the enclosing `t` would make every such field `t`.
    assert function_name(_function_values()[1]) == (None, None)


def test_an_assigned_value_is_named_by_its_target() -> None:
    _set, _keyless, local_value, field_target = _function_values()
    assert function_name(local_value) == ("f", None)
    assert function_name(field_target) == ("M.g", None)


def test_a_declaration_is_not_a_nameless_value() -> None:
    root = parse(cs.SupportedLanguage.LUA, "function named() end\n")
    (declaration,) = root.children
    assert function_name(declaration) == (None, None)


def test_each_table_function_gets_its_own_node(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    index(
        temp_repo / "lp",
        mock_ingestor,
        {"s.lua": "local s = { set = function() end, get = function() end }\n"},
    )
    functions = {
        str(n.properties[cs.KEY_QUALIFIED_NAME]): n.properties[cs.KEY_NAME]
        for n in get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
    }
    assert functions == {"lp.s.s.set": "set", "lp.s.s.get": "get"}
