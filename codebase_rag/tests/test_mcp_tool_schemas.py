"""`tool_schemas`: the input schema each MCP tool's arguments are checked
against, pinned against the handler the registry pairs it with rather than
against the schema's own text. The server calls `handler(**arguments)`, so a
property the handler does not take fails every call that sends it, a
required property the handler defaults makes a valid call fail validation,
and a parameter the schema does not declare can never be sent."""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.mcp import constants as mcs
from codebase_rag.mcp import tool_schemas as mts
from codebase_rag.tests.conftest import bare_registry

_JSON_TYPES: dict[type, mcs.MCPSchemaType] = {
    str: mcs.MCPSchemaType.STRING,
    int: mcs.MCPSchemaType.INTEGER,
    float: mcs.MCPSchemaType.NUMBER,
    bool: mcs.MCPSchemaType.BOOLEAN,
    list: mcs.MCPSchemaType.ARRAY,
    dict: mcs.MCPSchemaType.OBJECT,
}


def _handlers() -> dict[str, Callable[..., object]]:
    # The registry's own pairing of tool and handler, with both optional
    # groups on so that every tool is offered.
    registry = bare_registry()
    registry._semantic_search_tool = MagicMock()
    registry._structural_available = True
    return {name: tool.handler for name, tool in registry._build_tools().items()}


HANDLERS = _handlers()


def _parameters(name: str) -> dict[str, inspect.Parameter]:
    return dict(inspect.signature(HANDLERS[name]).parameters)


def _parameter_types(name: str) -> dict[str, object]:
    # Only the parameters' annotations: a return annotation may name a type
    # imported for type checking alone, which cannot be evaluated here.
    function = getattr(HANDLERS[name], "__func__", HANDLERS[name])
    annotations = {
        key: value
        for key, value in inspect.get_annotations(function).items()
        if key != "return"
    }
    holder = types.SimpleNamespace(__annotations__=annotations)
    return typing.get_type_hints(holder, globalns=function.__globals__)


def _json_type(hint: object) -> mcs.MCPSchemaType:
    # `str | None` is the optional string; `list[str]` an array.
    present = [arg for arg in typing.get_args(hint) if arg is not type(None)]
    if isinstance(hint, types.UnionType) and len(present) == 1:
        hint = present[0]
    origin = typing.get_origin(hint) or hint
    assert isinstance(origin, type), hint
    return _JSON_TYPES[origin]


def test_every_tool_has_a_schema_and_a_handler() -> None:
    # The checks below run per tool; a tool missing from either side would
    # leave its pairing unchecked.
    assert set(HANDLERS) == set(cs.MCPToolName)
    for name in cs.MCPToolName:
        assert mts.input_schema(name)["type"] == mcs.MCPSchemaType.OBJECT


@pytest.mark.parametrize("name", list(cs.MCPToolName))
def test_the_schema_declares_exactly_the_handlers_parameters(
    name: cs.MCPToolName,
) -> None:
    schema = mts.input_schema(name)

    assert set(schema["properties"]) == set(_parameters(name))


@pytest.mark.parametrize("name", list(cs.MCPToolName))
def test_the_required_parameters_are_those_the_handler_does_not_default(
    name: cs.MCPToolName,
) -> None:
    required = mts.input_schema(name)["required"]
    undefaulted = [
        key
        for key, parameter in _parameters(name).items()
        if parameter.default is inspect.Parameter.empty
    ]

    assert len(required) == len(set(required))
    assert set(required) == set(undefaulted)


@pytest.mark.parametrize("name", list(cs.MCPToolName))
def test_a_declared_default_is_the_handlers_own(name: cs.MCPToolName) -> None:
    parameters = _parameters(name)

    for key, prop in mts.input_schema(name)["properties"].items():
        if "default" in prop:
            assert prop["default"] == parameters[key].default, key


@pytest.mark.parametrize("name", list(cs.MCPToolName))
def test_each_property_has_the_json_type_of_its_parameter(
    name: cs.MCPToolName,
) -> None:
    hints = _parameter_types(name)

    for key, prop in mts.input_schema(name)["properties"].items():
        assert prop["type"] == _json_type(hints[key]), key


@pytest.mark.parametrize("name", list(cs.MCPToolName))
def test_only_a_handler_that_answers_in_text_is_sent_as_text(
    name: cs.MCPToolName,
) -> None:
    # The server JSON-encodes a JSON tool's result: a text answer marked as
    # JSON would reach the client as one quoted string, and a structured
    # answer marked as text as its Python repr.
    function = getattr(HANDLERS[name], "__func__", HANDLERS[name])
    answers_text = inspect.get_annotations(function)["return"] in (str, "str")

    assert mts.returns_json(name) is not answers_text


def test_an_input_schema_is_a_fresh_copy_each_time() -> None:
    # Each server listing hands the schema to the client library, which may
    # adjust it; the table every later listing reads must stay as declared.
    first = mts.input_schema(cs.MCPToolName.READ_FILE)
    first["properties"].clear()
    first["required"].append(cs.MCPParamName.OFFSET)

    second = mts.input_schema(cs.MCPToolName.READ_FILE)

    assert cs.MCPParamName.FILE_PATH in second["properties"]
    assert second["required"] == [cs.MCPParamName.FILE_PATH]
