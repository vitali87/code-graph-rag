from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.js_ts.type_inference import JsTypeInferenceEngine
from codebase_rag.tests.conftest import MockNode, create_mock_node
from codebase_rag.types_defs import NodeType


def create_js_variable_declarator(
    var_name: str,
    value_node: MockNode,
) -> MockNode:
    name_node = create_mock_node(cs.TS_IDENTIFIER, var_name)
    return create_mock_node(
        cs.TS_VARIABLE_DECLARATOR,
        fields={"name": name_node, "value": value_node},
        children=[name_node, value_node],
    )


def create_new_expression(class_name: str) -> MockNode:
    identifier = create_mock_node(cs.TS_IDENTIFIER, class_name)
    return create_mock_node(
        cs.TS_NEW_EXPRESSION,
        fields={"constructor": identifier},
        children=[identifier],
    )


def create_call_expression_with_identifier(func_name: str) -> MockNode:
    func_node = create_mock_node(cs.TS_IDENTIFIER, func_name)
    return create_mock_node(
        cs.TS_CALL_EXPRESSION,
        fields={"function": func_node},
        children=[func_node],
    )


def create_member_expression(object_name: str, property_name: str) -> MockNode:
    obj_node = create_mock_node(cs.TS_IDENTIFIER, object_name)
    prop_node = create_mock_node(cs.TS_IDENTIFIER, property_name)
    return create_mock_node(
        cs.TS_MEMBER_EXPRESSION,
        fields={"object": obj_node, "property": prop_node},
        children=[obj_node, prop_node],
    )


def create_call_expression_with_member(object_name: str, method_name: str) -> MockNode:
    member_expr = create_member_expression(object_name, method_name)
    return create_mock_node(
        cs.TS_CALL_EXPRESSION,
        fields={"function": member_expr},
        children=[member_expr],
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
    return registry


@pytest.fixture
def mock_find_method_ast_node() -> MagicMock:
    return MagicMock(return_value=None)


@pytest.fixture
def js_type_engine(
    mock_import_processor: MagicMock,
    mock_function_registry: MagicMock,
    mock_find_method_ast_node: MagicMock,
) -> JsTypeInferenceEngine:
    return JsTypeInferenceEngine(
        import_processor=mock_import_processor,
        function_registry=mock_function_registry,
        project_name="test_project",
        find_method_ast_node_func=mock_find_method_ast_node,
    )


class TestResolveJsClassName:
    def test_resolve_via_import_mapping_returns_imported_qn(
        self,
        js_type_engine: JsTypeInferenceEngine,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {
            "myapp.main": {"Person": "myapp.models"}
        }

        result = js_type_engine._resolve_js_class_name("Person", "myapp.main")

        assert result == "myapp.models"

    def test_resolve_via_import_mapping_checks_full_class_qn(
        self,
        js_type_engine: JsTypeInferenceEngine,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {
            "myapp.main": {"Person": "myapp.models"}
        }
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.models.Person"
        )
        mock_function_registry.__getitem__ = MagicMock(return_value=NodeType.CLASS)

        result = js_type_engine._resolve_js_class_name("Person", "myapp.main")

        assert result == "myapp.models.Person"

    def test_resolve_local_class_in_registry(
        self,
        js_type_engine: JsTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Helper"
        )
        mock_function_registry.__getitem__ = MagicMock(return_value=NodeType.CLASS)

        result = js_type_engine._resolve_js_class_name("Helper", "myapp.main")

        assert result == "myapp.main.Helper"

    def test_resolve_returns_none_when_not_found(
        self,
        js_type_engine: JsTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=False)

        result = js_type_engine._resolve_js_class_name("Unknown", "myapp.main")

        assert result is None

    def test_import_takes_precedence_over_local(
        self,
        js_type_engine: JsTypeInferenceEngine,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {
            "myapp.main": {"Person": "external.models"}
        }
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.Person"
        )
        mock_function_registry.__getitem__ = MagicMock(return_value=NodeType.CLASS)

        result = js_type_engine._resolve_js_class_name("Person", "myapp.main")

        assert result == "external.models"


class TestInferJsVariableTypeFromValue:
    def test_new_expression_returns_class_name(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        value_node = create_new_expression("MyClass")

        result = js_type_engine._infer_js_variable_type_from_value(
            value_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main",
        )

        assert result == "MyClass"

    def test_new_expression_resolves_class_qn(
        self,
        js_type_engine: JsTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.MyClass"
        )
        mock_function_registry.__getitem__ = MagicMock(return_value=NodeType.CLASS)
        value_node = create_new_expression("MyClass")

        result = js_type_engine._infer_js_variable_type_from_value(
            value_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main",
        )

        assert result == "myapp.main.MyClass"

    def test_call_expression_with_identifier_returns_func_name(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        value_node = create_call_expression_with_identifier("createInstance")

        result = js_type_engine._infer_js_variable_type_from_value(
            value_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main",
        )

        assert result == "createInstance"

    def test_unrecognized_node_type_returns_none(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        value_node = create_mock_node("string_literal", "hello")

        result = js_type_engine._infer_js_variable_type_from_value(
            value_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main",
        )

        assert result is None


class TestInferJsMethodReturnType:
    def test_invalid_method_call_format_returns_none(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        result = js_type_engine._infer_js_method_return_type(
            "invalidMethodCall", "myapp.main"
        )

        assert result is None

    def test_too_many_parts_returns_none(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        result = js_type_engine._infer_js_method_return_type("a.b.c", "myapp.main")

        assert result is None

    def test_unresolved_class_returns_none(
        self,
        js_type_engine: JsTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(return_value=False)

        result = js_type_engine._infer_js_method_return_type(
            "UnknownClass.method", "myapp.main"
        )

        assert result is None

    def test_method_ast_not_found_returns_none(
        self,
        js_type_engine: JsTypeInferenceEngine,
        mock_function_registry: MagicMock,
        mock_find_method_ast_node: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.MyClass"
        )
        mock_function_registry.__getitem__ = MagicMock(return_value=NodeType.CLASS)
        mock_find_method_ast_node.return_value = None

        result = js_type_engine._infer_js_method_return_type(
            "MyClass.getItem", "myapp.main"
        )

        assert result is None
        mock_find_method_ast_node.assert_called_once_with("myapp.main.MyClass.getItem")


class TestAnalyzeReturnStatements:
    def test_empty_method_returns_none(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        method_node = create_mock_node("method_definition", children=[])

        result = js_type_engine._analyze_return_statements(
            method_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main.MyClass.method",
        )

        assert result is None

    def test_return_with_no_expression_returns_none(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        return_keyword = create_mock_node(cs.TS_RETURN)
        return_stmt = create_mock_node(
            cs.TS_RETURN_STATEMENT,
            children=[return_keyword],
        )
        method_node = create_mock_node("method_definition", children=[return_stmt])

        result = js_type_engine._analyze_return_statements(
            method_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main.MyClass.method",
        )

        assert result is None


class TestBuildLocalVariableTypeMap:
    def test_empty_node_returns_empty_dict(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        root_node = create_mock_node("program", children=[])

        result = js_type_engine.build_local_variable_type_map(
            root_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main",
        )

        assert result == {}

    def test_variable_declarator_with_new_expression(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        new_expr = create_new_expression("MyClass")
        var_decl = create_js_variable_declarator("instance", new_expr)
        root_node = create_mock_node("program", children=[var_decl])

        result = js_type_engine.build_local_variable_type_map(
            root_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main",
        )

        assert "instance" in result
        assert result["instance"] == "MyClass"

    def test_variable_declarator_with_function_call(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        call_expr = create_call_expression_with_identifier("createUser")
        var_decl = create_js_variable_declarator("user", call_expr)
        root_node = create_mock_node("program", children=[var_decl])

        result = js_type_engine.build_local_variable_type_map(
            root_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main",
        )

        assert "user" in result
        assert result["user"] == "createUser"

    def test_multiple_variable_declarators(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        new_expr1 = create_new_expression("Person")
        var_decl1 = create_js_variable_declarator("person", new_expr1)
        new_expr2 = create_new_expression("Logger")
        var_decl2 = create_js_variable_declarator("logger", new_expr2)
        root_node = create_mock_node("program", children=[var_decl1, var_decl2])

        result = js_type_engine.build_local_variable_type_map(
            root_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main",
        )

        assert len(result) == 2
        assert result["person"] == "Person"
        assert result["logger"] == "Logger"

    def test_nested_variable_declarator(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        new_expr = create_new_expression("InnerClass")
        var_decl = create_js_variable_declarator("inner", new_expr)
        block = create_mock_node("block", children=[var_decl])
        function_body = create_mock_node("function_body", children=[block])
        root_node = create_mock_node("program", children=[function_body])

        result = js_type_engine.build_local_variable_type_map(
            root_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main",
        )

        assert "inner" in result
        assert result["inner"] == "InnerClass"

    def test_variable_without_value_is_skipped(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        name_node = create_mock_node(cs.TS_IDENTIFIER, "uninitialized")
        var_decl = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={"name": name_node, "value": None},
            children=[name_node],
        )
        root_node = create_mock_node("program", children=[var_decl])

        result = js_type_engine.build_local_variable_type_map(
            root_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main",
        )

        assert result == {}

    def test_variable_with_uninferrable_value_is_skipped(
        self,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        string_literal = create_mock_node("string_literal", "hello")
        var_decl = create_js_variable_declarator("greeting", string_literal)
        root_node = create_mock_node("program", children=[var_decl])

        result = js_type_engine.build_local_variable_type_map(
            root_node,  # ty: ignore[invalid-argument-type]  # (H) MockNode not Node
            "myapp.main",
        )

        assert result == {}
