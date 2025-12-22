from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.lua_type_inference import LuaTypeInferenceEngine
from codebase_rag.types_defs import NodeType


@pytest.fixture(scope="module")
def lua_parser():
    parsers, _ = load_parsers()
    if cs.SupportedLanguage.LUA not in parsers:
        pytest.skip("Lua parser not available")
    return parsers[cs.SupportedLanguage.LUA]


@pytest.fixture
def mock_import_processor() -> MagicMock:
    processor = MagicMock(spec=ImportProcessor)
    processor.import_mapping = {}
    return processor


@pytest.fixture
def mock_function_registry() -> MagicMock:
    registry = MagicMock()
    registry.__contains__ = MagicMock(return_value=False)
    registry.__getitem__ = MagicMock(return_value=None)
    registry.find_with_prefix = MagicMock(return_value=[])
    return registry


@pytest.fixture
def lua_type_engine(
    mock_import_processor: MagicMock,
    mock_function_registry: MagicMock,
) -> LuaTypeInferenceEngine:
    return LuaTypeInferenceEngine(
        import_processor=mock_import_processor,
        function_registry=mock_function_registry,
        project_name="test_project",
    )


class TestLuaTypeInferenceWithRealParsing:
    def test_simple_variable_declaration_with_method_call(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Person"
        )

        code = b"local person = Person:new()"
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "person" in result
        assert result["person"] == "myapp.main.Person"

    def test_multiple_variable_declarations(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x
            in {"myapp.main.Person", "myapp.main.Logger", "myapp.main.Config"}
        )

        code = b"""
local person = Person:new()
local logger = Logger:create()
local config = Config:load()
"""
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "person" in result
        assert result["person"] == "myapp.main.Person"
        assert "logger" in result
        assert result["logger"] == "myapp.main.Logger"
        assert "config" in result
        assert result["config"] == "myapp.main.Config"

    def test_nested_function_with_variable_declarations(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Database"
        )

        code = b"""
local function init()
    local db = Database:connect()
    return db
end
"""
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "db" in result
        assert result["db"] == "myapp.main.Database"

    def test_variable_with_imported_class(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {
            "myapp.main": {"HttpClient": "external.http"}
        }

        code = b"local client = HttpClient:new()"
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "client" in result
        assert result["client"] == "external.http.HttpClient"

    def test_variable_declaration_without_method_call(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        code = b"local x = 42"
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "x" not in result

    def test_variable_with_regular_function_call(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        code = b"local result = someFunction()"
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "result" not in result

    def test_variable_with_table_constructor(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        code = b"local tbl = {}"
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "tbl" not in result

    def test_variable_with_string_value(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        code = b'local name = "hello"'
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "name" not in result

    def test_class_resolved_via_method_prefix(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=False)
        mock_function_registry.find_with_prefix = MagicMock(
            return_value=[
                ("myapp.main.Widget:init", NodeType.METHOD),
                ("myapp.main.Widget:render", NodeType.METHOD),
            ]
        )

        code = b"local widget = Widget:new()"
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "widget" in result
        assert result["widget"] == "myapp.main.Widget"

    def test_unresolvable_class_skipped(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=False)
        mock_function_registry.find_with_prefix = MagicMock(return_value=[])

        code = b"local obj = UnknownClass:create()"
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "obj" not in result

    def test_mixed_resolvable_and_unresolvable(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Known"
        )

        code = b"""
local a = Known:new()
local b = Unknown:new()
local c = Known:create()
"""
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "a" in result
        assert result["a"] == "myapp.main.Known"
        assert "b" not in result
        assert "c" in result
        assert result["c"] == "myapp.main.Known"

    def test_variable_in_if_block(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Handler"
        )

        code = b"""
if condition then
    local handler = Handler:new()
end
"""
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "handler" in result
        assert result["handler"] == "myapp.main.Handler"

    def test_variable_in_for_loop(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Item"
        )

        code = b"""
for i = 1, 10 do
    local item = Item:create()
end
"""
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "item" in result
        assert result["item"] == "myapp.main.Item"

    def test_chained_method_call_only_first_part(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Builder"
        )

        code = b"local result = Builder:new():build()"
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert len(result) == 0 or "result" not in result


class TestLuaTypeInferenceComplexScenarios:
    def test_class_with_module_table_pattern(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.M.Class"
        )

        code = b"""
local M = {}
function M.Class:new()
    return setmetatable({}, self)
end
local obj = M.Class:new()
"""
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "M" not in result or result.get("M") != "myapp.main.M.Class"

    def test_empty_code(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        code = b""
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert result == {}

    def test_only_comments(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        code = b"-- This is a comment\n--[[ Multi-line comment ]]"
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert result == {}

    def test_global_variable_not_tracked(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Person"
        )

        code = b"globalPerson = Person:new()"
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "globalPerson" not in result

    def test_unicode_identifier(
        self,
        lua_parser,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Person"
        )

        code = "local персона = Person:new()".encode()
        tree = lua_parser.parse(code)

        result = lua_type_engine.build_lua_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert len(result) <= 1
