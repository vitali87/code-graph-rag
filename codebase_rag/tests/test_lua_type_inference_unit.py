from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.lua_type_inference import LuaTypeInferenceEngine
from codebase_rag.tests.conftest import MockNode, create_mock_node
from codebase_rag.types_defs import NodeType


def create_lua_variable_declaration(
    var_name: str, class_name: str, method_name: str
) -> MockNode:
    identifier_class = create_mock_node(cs.TS_LUA_IDENTIFIER, class_name)
    identifier_method = create_mock_node(cs.TS_LUA_IDENTIFIER, method_name)

    method_index_expr = create_mock_node(
        cs.TS_LUA_METHOD_INDEX_EXPRESSION,
        children=[identifier_class, identifier_method],
    )

    function_call = create_mock_node(
        cs.TS_LUA_FUNCTION_CALL,
        children=[method_index_expr],
    )

    expression_list = create_mock_node(
        cs.TS_LUA_EXPRESSION_LIST,
        children=[function_call],
    )

    var_identifier = create_mock_node(cs.TS_LUA_IDENTIFIER, var_name)
    variable_list = create_mock_node(
        cs.TS_LUA_VARIABLE_LIST,
        children=[var_identifier],
    )

    assignment_stmt = create_mock_node(
        cs.TS_LUA_ASSIGNMENT_STATEMENT,
        children=[variable_list, expression_list],
    )

    return create_mock_node(
        cs.TS_LUA_VARIABLE_DECLARATION,
        children=[assignment_stmt],
    )


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


class TestResolveLuaClassName:
    def test_resolve_via_import_mapping(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {
            "myapp.main": {"Person": "myapp.models"}
        }

        result = lua_type_engine._resolve_lua_class_name("Person", "myapp.main")

        assert result == "myapp.models.Person"

    def test_resolve_via_function_registry_direct(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Helper"
        )

        result = lua_type_engine._resolve_lua_class_name("Helper", "myapp.main")

        assert result == "myapp.main.Helper"

    def test_resolve_via_method_prefix_matching(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=False)
        mock_function_registry.find_with_prefix = MagicMock(
            return_value=[
                ("myapp.main.Application:new", NodeType.METHOD),
                ("myapp.main.Application:run", NodeType.METHOD),
            ]
        )

        result = lua_type_engine._resolve_lua_class_name("Application", "myapp.main")

        assert result == "myapp.main.Application"

    def test_resolve_returns_none_when_not_found(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=False)
        mock_function_registry.find_with_prefix = MagicMock(return_value=[])

        result = lua_type_engine._resolve_lua_class_name("Unknown", "myapp.main")

        assert result is None

    def test_resolve_import_takes_precedence(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {
            "myapp.main": {"Person": "external.models"}
        }
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Person"
        )

        result = lua_type_engine._resolve_lua_class_name("Person", "myapp.main")

        assert result == "external.models.Person"

    def test_resolve_with_no_import_mapping_for_module(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {}
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.utils.Logger"
        )

        result = lua_type_engine._resolve_lua_class_name("Logger", "myapp.utils")

        assert result == "myapp.utils.Logger"


class TestInferLuaVariableTypeFromValue:
    def test_infer_from_method_call(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Person"
        )

        identifier_class = create_mock_node(cs.TS_LUA_IDENTIFIER, "Person")
        identifier_method = create_mock_node(cs.TS_LUA_IDENTIFIER, "new")

        method_index_expr = create_mock_node(
            cs.TS_LUA_METHOD_INDEX_EXPRESSION,
            children=[identifier_class, identifier_method],
        )

        function_call = create_mock_node(
            cs.TS_LUA_FUNCTION_CALL,
            children=[method_index_expr],
        )

        result = lua_type_engine._infer_lua_variable_type_from_value(
            function_call, "myapp.main"
        )

        assert result == "myapp.main.Person"

    def test_infer_returns_none_for_non_function_call(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        string_node = create_mock_node("string", "hello")

        result = lua_type_engine._infer_lua_variable_type_from_value(
            string_node, "myapp.main"
        )

        assert result is None

    def test_infer_returns_none_for_function_call_without_method_index(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        identifier = create_mock_node(cs.TS_LUA_IDENTIFIER, "someFunc")
        function_call = create_mock_node(
            cs.TS_LUA_FUNCTION_CALL,
            children=[identifier],
        )

        result = lua_type_engine._infer_lua_variable_type_from_value(
            function_call, "myapp.main"
        )

        assert result is None

    def test_infer_returns_none_when_class_not_resolved(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=False)
        mock_function_registry.find_with_prefix = MagicMock(return_value=[])

        identifier_class = create_mock_node(cs.TS_LUA_IDENTIFIER, "UnknownClass")
        identifier_method = create_mock_node(cs.TS_LUA_IDENTIFIER, "new")

        method_index_expr = create_mock_node(
            cs.TS_LUA_METHOD_INDEX_EXPRESSION,
            children=[identifier_class, identifier_method],
        )

        function_call = create_mock_node(
            cs.TS_LUA_FUNCTION_CALL,
            children=[method_index_expr],
        )

        result = lua_type_engine._infer_lua_variable_type_from_value(
            function_call, "myapp.main"
        )

        assert result is None


class TestBuildLuaLocalVariableTypeMap:
    def test_build_map_with_single_variable(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Person"
        )

        var_decl = create_lua_variable_declaration("alice", "Person", "new")

        result = lua_type_engine.build_lua_local_variable_type_map(
            var_decl, "myapp.main"
        )

        assert "alice" in result
        assert result["alice"] == "myapp.main.Person"

    def test_build_map_with_nested_declarations(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x
            in {
                "myapp.main.Person",
                "myapp.main.Logger",
            }
        )

        var_decl1 = create_lua_variable_declaration("person", "Person", "new")
        var_decl2 = create_lua_variable_declaration("logger", "Logger", "create")

        root = create_mock_node("chunk", children=[var_decl1, var_decl2])

        result = lua_type_engine.build_lua_local_variable_type_map(root, "myapp.main")

        assert "person" in result
        assert result["person"] == "myapp.main.Person"
        assert "logger" in result
        assert result["logger"] == "myapp.main.Logger"

    def test_build_map_empty_for_non_matching_nodes(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        other_node = create_mock_node("function_declaration")

        result = lua_type_engine.build_lua_local_variable_type_map(
            other_node, "myapp.main"
        )

        assert result == {}

    def test_build_map_skips_unresolvable_types(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=False)
        mock_function_registry.find_with_prefix = MagicMock(return_value=[])

        var_decl = create_lua_variable_declaration("unknown", "UnknownClass", "new")

        result = lua_type_engine.build_lua_local_variable_type_map(
            var_decl, "myapp.main"
        )

        assert "unknown" not in result

    def test_build_map_with_imported_class(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {"myapp.main": {"Database": "myapp.db"}}

        var_decl = create_lua_variable_declaration("db", "Database", "connect")

        result = lua_type_engine.build_lua_local_variable_type_map(
            var_decl, "myapp.main"
        )

        assert "db" in result
        assert result["db"] == "myapp.db.Database"


class TestLuaTypeInferenceEdgeCases:
    def test_empty_module_qn(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=False)
        mock_function_registry.find_with_prefix = MagicMock(return_value=[])

        result = lua_type_engine._resolve_lua_class_name("Person", "")

        assert result is None

    def test_class_name_with_special_characters(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main._PrivateClass"
        )

        result = lua_type_engine._resolve_lua_class_name("_PrivateClass", "myapp.main")

        assert result == "myapp.main._PrivateClass"

    def test_method_prefix_with_dot_method(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=False)
        mock_function_registry.find_with_prefix = MagicMock(
            return_value=[
                ("myapp.main.Utils.staticMethod", NodeType.FUNCTION),
            ]
        )

        result = lua_type_engine._resolve_lua_class_name("Utils", "myapp.main")

        assert result is None

    def test_method_prefix_matching_colon_separator(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=False)
        mock_function_registry.find_with_prefix = MagicMock(
            return_value=[
                ("myapp.main.Widget:init", NodeType.METHOD),
            ]
        )

        result = lua_type_engine._resolve_lua_class_name("Widget", "myapp.main")

        assert result == "myapp.main.Widget"


class TestBuildLuaLocalVariableTypeMapEdgeCases:
    def test_variable_declaration_without_assignment(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        var_decl = create_mock_node(
            cs.TS_LUA_VARIABLE_DECLARATION,
            children=[],
        )

        result = lua_type_engine.build_lua_local_variable_type_map(
            var_decl, "myapp.main"
        )

        assert result == {}

    def test_assignment_with_empty_variable_list(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        expression_list = create_mock_node(
            cs.TS_LUA_EXPRESSION_LIST,
            children=[
                create_mock_node(cs.TS_LUA_FUNCTION_CALL, children=[]),
            ],
        )
        variable_list = create_mock_node(cs.TS_LUA_VARIABLE_LIST, children=[])
        assignment_stmt = create_mock_node(
            cs.TS_LUA_ASSIGNMENT_STATEMENT,
            children=[variable_list, expression_list],
        )
        var_decl = create_mock_node(
            cs.TS_LUA_VARIABLE_DECLARATION,
            children=[assignment_stmt],
        )

        result = lua_type_engine.build_lua_local_variable_type_map(
            var_decl, "myapp.main"
        )

        assert result == {}

    def test_assignment_with_empty_expression_list(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        var_identifier = create_mock_node(cs.TS_LUA_IDENTIFIER, "myVar")
        variable_list = create_mock_node(
            cs.TS_LUA_VARIABLE_LIST,
            children=[var_identifier],
        )
        expression_list = create_mock_node(cs.TS_LUA_EXPRESSION_LIST, children=[])
        assignment_stmt = create_mock_node(
            cs.TS_LUA_ASSIGNMENT_STATEMENT,
            children=[variable_list, expression_list],
        )
        var_decl = create_mock_node(
            cs.TS_LUA_VARIABLE_DECLARATION,
            children=[assignment_stmt],
        )

        result = lua_type_engine.build_lua_local_variable_type_map(
            var_decl, "myapp.main"
        )

        assert result == {}

    def test_more_variables_than_values(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Person"
        )

        var_id1 = create_mock_node(cs.TS_LUA_IDENTIFIER, "a")
        var_id2 = create_mock_node(cs.TS_LUA_IDENTIFIER, "b")
        var_id3 = create_mock_node(cs.TS_LUA_IDENTIFIER, "c")
        variable_list = create_mock_node(
            cs.TS_LUA_VARIABLE_LIST,
            children=[var_id1, var_id2, var_id3],
        )

        identifier_class = create_mock_node(cs.TS_LUA_IDENTIFIER, "Person")
        identifier_method = create_mock_node(cs.TS_LUA_IDENTIFIER, "new")
        method_index_expr = create_mock_node(
            cs.TS_LUA_METHOD_INDEX_EXPRESSION,
            children=[identifier_class, identifier_method],
        )
        function_call = create_mock_node(
            cs.TS_LUA_FUNCTION_CALL,
            children=[method_index_expr],
        )
        expression_list = create_mock_node(
            cs.TS_LUA_EXPRESSION_LIST,
            children=[function_call],
        )

        assignment_stmt = create_mock_node(
            cs.TS_LUA_ASSIGNMENT_STATEMENT,
            children=[variable_list, expression_list],
        )
        var_decl = create_mock_node(
            cs.TS_LUA_VARIABLE_DECLARATION,
            children=[assignment_stmt],
        )

        result = lua_type_engine.build_lua_local_variable_type_map(
            var_decl, "myapp.main"
        )

        assert "a" in result
        assert result["a"] == "myapp.main.Person"
        assert "b" not in result
        assert "c" not in result

    def test_non_function_call_in_expression_list(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        var_identifier = create_mock_node(cs.TS_LUA_IDENTIFIER, "myVar")
        variable_list = create_mock_node(
            cs.TS_LUA_VARIABLE_LIST,
            children=[var_identifier],
        )
        string_expr = create_mock_node("string", "hello")
        expression_list = create_mock_node(
            cs.TS_LUA_EXPRESSION_LIST,
            children=[string_expr],
        )
        assignment_stmt = create_mock_node(
            cs.TS_LUA_ASSIGNMENT_STATEMENT,
            children=[variable_list, expression_list],
        )
        var_decl = create_mock_node(
            cs.TS_LUA_VARIABLE_DECLARATION,
            children=[assignment_stmt],
        )

        result = lua_type_engine.build_lua_local_variable_type_map(
            var_decl, "myapp.main"
        )

        assert result == {}

    def test_function_call_without_method_index_expression(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        var_identifier = create_mock_node(cs.TS_LUA_IDENTIFIER, "result")
        variable_list = create_mock_node(
            cs.TS_LUA_VARIABLE_LIST,
            children=[var_identifier],
        )
        func_name = create_mock_node(cs.TS_LUA_IDENTIFIER, "someFunc")
        function_call = create_mock_node(
            cs.TS_LUA_FUNCTION_CALL,
            children=[func_name],
        )
        expression_list = create_mock_node(
            cs.TS_LUA_EXPRESSION_LIST,
            children=[function_call],
        )
        assignment_stmt = create_mock_node(
            cs.TS_LUA_ASSIGNMENT_STATEMENT,
            children=[variable_list, expression_list],
        )
        var_decl = create_mock_node(
            cs.TS_LUA_VARIABLE_DECLARATION,
            children=[assignment_stmt],
        )

        result = lua_type_engine.build_lua_local_variable_type_map(
            var_decl, "myapp.main"
        )

        assert result == {}

    def test_method_index_with_only_class_identifier(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=True)

        var_identifier = create_mock_node(cs.TS_LUA_IDENTIFIER, "obj")
        variable_list = create_mock_node(
            cs.TS_LUA_VARIABLE_LIST,
            children=[var_identifier],
        )
        identifier_class = create_mock_node(cs.TS_LUA_IDENTIFIER, "Person")
        method_index_expr = create_mock_node(
            cs.TS_LUA_METHOD_INDEX_EXPRESSION,
            children=[identifier_class],
        )
        function_call = create_mock_node(
            cs.TS_LUA_FUNCTION_CALL,
            children=[method_index_expr],
        )
        expression_list = create_mock_node(
            cs.TS_LUA_EXPRESSION_LIST,
            children=[function_call],
        )
        assignment_stmt = create_mock_node(
            cs.TS_LUA_ASSIGNMENT_STATEMENT,
            children=[variable_list, expression_list],
        )
        var_decl = create_mock_node(
            cs.TS_LUA_VARIABLE_DECLARATION,
            children=[assignment_stmt],
        )

        result = lua_type_engine.build_lua_local_variable_type_map(
            var_decl, "myapp.main"
        )

        assert result == {}

    def test_deeply_nested_variable_declaration(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Deep"
        )

        var_decl = create_lua_variable_declaration("deep", "Deep", "create")
        inner_block = create_mock_node("block", children=[var_decl])
        outer_block = create_mock_node("block", children=[inner_block])
        root = create_mock_node("chunk", children=[outer_block])

        result = lua_type_engine.build_lua_local_variable_type_map(root, "myapp.main")

        assert "deep" in result
        assert result["deep"] == "myapp.main.Deep"

    def test_variable_with_empty_text(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=True)

        var_identifier = create_mock_node(cs.TS_LUA_IDENTIFIER, "")
        variable_list = create_mock_node(
            cs.TS_LUA_VARIABLE_LIST,
            children=[var_identifier],
        )
        identifier_class = create_mock_node(cs.TS_LUA_IDENTIFIER, "Person")
        identifier_method = create_mock_node(cs.TS_LUA_IDENTIFIER, "new")
        method_index_expr = create_mock_node(
            cs.TS_LUA_METHOD_INDEX_EXPRESSION,
            children=[identifier_class, identifier_method],
        )
        function_call = create_mock_node(
            cs.TS_LUA_FUNCTION_CALL,
            children=[method_index_expr],
        )
        expression_list = create_mock_node(
            cs.TS_LUA_EXPRESSION_LIST,
            children=[function_call],
        )
        assignment_stmt = create_mock_node(
            cs.TS_LUA_ASSIGNMENT_STATEMENT,
            children=[variable_list, expression_list],
        )
        var_decl = create_mock_node(
            cs.TS_LUA_VARIABLE_DECLARATION,
            children=[assignment_stmt],
        )

        result = lua_type_engine.build_lua_local_variable_type_map(
            var_decl, "myapp.main"
        )

        assert result == {}


class TestInferLuaVariableTypeFromValueEdgeCases:
    def test_method_index_with_non_identifier_children(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        non_id_node = create_mock_node("expression", "something")
        method_index_expr = create_mock_node(
            cs.TS_LUA_METHOD_INDEX_EXPRESSION,
            children=[non_id_node],
        )
        function_call = create_mock_node(
            cs.TS_LUA_FUNCTION_CALL,
            children=[method_index_expr],
        )

        result = lua_type_engine._infer_lua_variable_type_from_value(
            function_call, "myapp.main"
        )

        assert result is None

    def test_method_index_with_empty_class_name(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
    ) -> None:
        identifier_class = create_mock_node(cs.TS_LUA_IDENTIFIER, "")
        identifier_method = create_mock_node(cs.TS_LUA_IDENTIFIER, "new")
        method_index_expr = create_mock_node(
            cs.TS_LUA_METHOD_INDEX_EXPRESSION,
            children=[identifier_class, identifier_method],
        )
        function_call = create_mock_node(
            cs.TS_LUA_FUNCTION_CALL,
            children=[method_index_expr],
        )

        result = lua_type_engine._infer_lua_variable_type_from_value(
            function_call, "myapp.main"
        )

        assert result is None

    def test_method_index_with_empty_method_name(
        self,
        lua_type_engine: LuaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=True)

        identifier_class = create_mock_node(cs.TS_LUA_IDENTIFIER, "Person")
        identifier_method = create_mock_node(cs.TS_LUA_IDENTIFIER, "")
        method_index_expr = create_mock_node(
            cs.TS_LUA_METHOD_INDEX_EXPRESSION,
            children=[identifier_class, identifier_method],
        )
        function_call = create_mock_node(
            cs.TS_LUA_FUNCTION_CALL,
            children=[method_index_expr],
        )

        result = lua_type_engine._infer_lua_variable_type_from_value(
            function_call, "myapp.main"
        )

        assert result is None
