from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.java.type_inference import JavaTypeInferenceEngine
from codebase_rag.tests.conftest import create_mock_node


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
    registry.items = MagicMock(return_value=[])
    return registry


@pytest.fixture
def mock_ast_cache() -> MagicMock:
    cache = MagicMock()
    cache.__contains__ = MagicMock(return_value=False)
    cache.__getitem__ = MagicMock(return_value=(None, None))
    return cache


@pytest.fixture
def engine(
    mock_import_processor: MagicMock,
    mock_function_registry: MagicMock,
    mock_ast_cache: MagicMock,
) -> JavaTypeInferenceEngine:
    return JavaTypeInferenceEngine(
        import_processor=mock_import_processor,
        function_registry=mock_function_registry,
        repo_path=Path("/test/repo"),
        project_name="test_project",
        ast_cache=mock_ast_cache,
        queries={},
        module_qn_to_file_path={},
        class_inheritance={},
        simple_name_lookup=defaultdict(set),
    )


class TestAnalyzeJavaParameters:
    def test_no_parameters_node(self, engine: JavaTypeInferenceEngine) -> None:
        scope_node = create_mock_node("method_declaration", fields={})
        local_var_types: dict[str, str] = {}

        engine._analyze_java_parameters(scope_node, local_var_types, "com.example")

        assert local_var_types == {}

    def test_formal_parameter_with_type(self, engine: JavaTypeInferenceEngine) -> None:
        name_node = create_mock_node(cs.TS_IDENTIFIER, "userId")
        type_node = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        param_node = create_mock_node(
            cs.TS_FORMAL_PARAMETER,
            fields={cs.FIELD_NAME: name_node, cs.FIELD_TYPE: type_node},
        )
        params_node = create_mock_node(
            cs.FIELD_PARAMETERS,
            children=[param_node],
        )
        scope_node = create_mock_node(
            "method_declaration",
            fields={cs.FIELD_PARAMETERS: params_node},
        )
        local_var_types: dict[str, str] = {}

        engine._analyze_java_parameters(scope_node, local_var_types, "com.example")

        assert "userId" in local_var_types
        assert local_var_types["userId"] == "java.lang.String"

    def test_multiple_formal_parameters(self, engine: JavaTypeInferenceEngine) -> None:
        name1 = create_mock_node(cs.TS_IDENTIFIER, "name")
        type1 = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        param1 = create_mock_node(
            cs.TS_FORMAL_PARAMETER,
            fields={cs.FIELD_NAME: name1, cs.FIELD_TYPE: type1},
        )

        name2 = create_mock_node(cs.TS_IDENTIFIER, "count")
        type2 = create_mock_node(cs.TS_TYPE_IDENTIFIER, "int")
        param2 = create_mock_node(
            cs.TS_FORMAL_PARAMETER,
            fields={cs.FIELD_NAME: name2, cs.FIELD_TYPE: type2},
        )

        params_node = create_mock_node(
            cs.FIELD_PARAMETERS,
            children=[param1, param2],
        )
        scope_node = create_mock_node(
            "method_declaration",
            fields={cs.FIELD_PARAMETERS: params_node},
        )
        local_var_types: dict[str, str] = {}

        engine._analyze_java_parameters(scope_node, local_var_types, "com.example")

        assert local_var_types["name"] == "java.lang.String"
        assert local_var_types["count"] == "int"

    def test_spread_parameter(self, engine: JavaTypeInferenceEngine) -> None:
        type_identifier = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        name_node = create_mock_node(cs.TS_IDENTIFIER, "args")
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: name_node},
        )
        spread_param = create_mock_node(
            cs.TS_SPREAD_PARAMETER,
            children=[type_identifier, declarator],
        )
        params_node = create_mock_node(
            cs.FIELD_PARAMETERS,
            children=[spread_param],
        )
        scope_node = create_mock_node(
            "method_declaration",
            fields={cs.FIELD_PARAMETERS: params_node},
        )
        local_var_types: dict[str, str] = {}

        engine._analyze_java_parameters(scope_node, local_var_types, "com.example")

        assert "args" in local_var_types
        assert local_var_types["args"] == "java.lang.String[]"

    def test_formal_parameter_missing_name(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        type_node = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        param_node = create_mock_node(
            cs.TS_FORMAL_PARAMETER,
            fields={cs.FIELD_TYPE: type_node},
        )
        params_node = create_mock_node(
            cs.FIELD_PARAMETERS,
            children=[param_node],
        )
        scope_node = create_mock_node(
            "method_declaration",
            fields={cs.FIELD_PARAMETERS: params_node},
        )
        local_var_types: dict[str, str] = {}

        engine._analyze_java_parameters(scope_node, local_var_types, "com.example")

        assert local_var_types == {}


class TestAnalyzeJavaLocalVariables:
    def test_local_variable_declaration(self, engine: JavaTypeInferenceEngine) -> None:
        name_node = create_mock_node(cs.TS_IDENTIFIER, "count")
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: name_node},
        )
        type_node = create_mock_node(cs.TS_TYPE_IDENTIFIER, "int")
        decl_node = create_mock_node(
            cs.TS_LOCAL_VARIABLE_DECLARATION,
            fields={cs.FIELD_TYPE: type_node, cs.FIELD_DECLARATOR: declarator},
        )
        scope_node = create_mock_node("block", children=[decl_node])
        local_var_types: dict[str, str] = {}

        engine._analyze_java_local_variables(scope_node, local_var_types, "com.example")

        assert local_var_types["count"] == "int"

    def test_local_variable_with_object_creation_value(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        name_node = create_mock_node(cs.TS_IDENTIFIER, "list")
        type_in_creation = create_mock_node(cs.TS_TYPE_IDENTIFIER, "ArrayList")
        value_node = create_mock_node(
            cs.TS_OBJECT_CREATION_EXPRESSION,
            fields={cs.FIELD_TYPE: type_in_creation},
        )
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: name_node, cs.FIELD_VALUE: value_node},
        )
        type_node = create_mock_node(cs.TS_TYPE_IDENTIFIER, "List")
        decl_node = create_mock_node(
            cs.TS_LOCAL_VARIABLE_DECLARATION,
            fields={cs.FIELD_TYPE: type_node, cs.FIELD_DECLARATOR: declarator},
        )
        scope_node = create_mock_node("block", children=[decl_node])
        local_var_types: dict[str, str] = {}

        engine._analyze_java_local_variables(scope_node, local_var_types, "com.example")

        assert local_var_types["list"] == "ArrayList"

    def test_nested_local_variables(self, engine: JavaTypeInferenceEngine) -> None:
        name1 = create_mock_node(cs.TS_IDENTIFIER, "outer")
        decl1 = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: name1},
        )
        type1 = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        var_decl1 = create_mock_node(
            cs.TS_LOCAL_VARIABLE_DECLARATION,
            fields={cs.FIELD_TYPE: type1, cs.FIELD_DECLARATOR: decl1},
        )

        name2 = create_mock_node(cs.TS_IDENTIFIER, "inner")
        decl2 = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: name2},
        )
        type2 = create_mock_node(cs.TS_TYPE_IDENTIFIER, "int")
        var_decl2 = create_mock_node(
            cs.TS_LOCAL_VARIABLE_DECLARATION,
            fields={cs.FIELD_TYPE: type2, cs.FIELD_DECLARATOR: decl2},
        )
        inner_block = create_mock_node("block", children=[var_decl2])

        scope_node = create_mock_node("block", children=[var_decl1, inner_block])
        local_var_types: dict[str, str] = {}

        engine._analyze_java_local_variables(scope_node, local_var_types, "com.example")

        assert local_var_types["outer"] == "java.lang.String"
        assert local_var_types["inner"] == "int"

    def test_local_variable_missing_type(self, engine: JavaTypeInferenceEngine) -> None:
        name_node = create_mock_node(cs.TS_IDENTIFIER, "count")
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: name_node},
        )
        decl_node = create_mock_node(
            cs.TS_LOCAL_VARIABLE_DECLARATION,
            fields={cs.FIELD_DECLARATOR: declarator},
        )
        scope_node = create_mock_node("block", children=[decl_node])
        local_var_types: dict[str, str] = {}

        engine._analyze_java_local_variables(scope_node, local_var_types, "com.example")

        assert local_var_types == {}


class TestAnalyzeJavaClassFields:
    def test_class_field_extracted(
        self, engine: JavaTypeInferenceEngine, mock_ast_cache: MagicMock
    ) -> None:
        class_name_node = create_mock_node(cs.TS_TYPE_IDENTIFIER, "MyClass")
        field_name_node = create_mock_node(cs.TS_IDENTIFIER, "name")
        field_declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: field_name_node},
        )
        field_type_node = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        field_decl = create_mock_node(
            cs.TS_FIELD_DECLARATION,
            fields={
                cs.FIELD_TYPE: field_type_node,
                cs.FIELD_DECLARATOR: field_declarator,
            },
        )
        class_body = create_mock_node("class_body", children=[field_decl])
        class_node = create_mock_node(
            cs.TS_CLASS_DECLARATION,
            fields={cs.FIELD_NAME: class_name_node, cs.FIELD_BODY: class_body},
            children=[class_body],
        )
        method_body = create_mock_node("block")
        method_node = create_mock_node(
            "method_declaration",
            children=[method_body],
        )
        method_node.node_parent = class_node
        method_body.node_parent = method_node

        local_var_types: dict[str, str] = {}

        engine._analyze_java_class_fields(method_body, local_var_types, "com.example")

        assert "name" in local_var_types
        assert "this.name" in local_var_types
        assert local_var_types["name"] == "java.lang.String"

    def test_no_containing_class(self, engine: JavaTypeInferenceEngine) -> None:
        scope_node = create_mock_node("block")
        local_var_types: dict[str, str] = {}

        engine._analyze_java_class_fields(scope_node, local_var_types, "com.example")

        assert local_var_types == {}


class TestAnalyzeJavaConstructorAssignments:
    def test_assignment_expression_inferred(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        left_node = create_mock_node(cs.TS_IDENTIFIER, "userId")
        right_node = create_mock_node(cs.TS_STRING_LITERAL, '"test"')
        assignment = create_mock_node(
            cs.TS_ASSIGNMENT_EXPRESSION,
            fields={cs.FIELD_LEFT: left_node, cs.FIELD_RIGHT: right_node},
            children=[left_node, right_node],
        )
        scope_node = create_mock_node("block", children=[assignment])
        local_var_types: dict[str, str] = {}

        engine._analyze_java_constructor_assignments(
            scope_node, local_var_types, "com.example"
        )

        assert local_var_types["userId"] == "java.lang.String"

    def test_assignment_with_field_access(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        object_node = create_mock_node(cs.TS_IDENTIFIER, "this")
        field_node = create_mock_node(cs.TS_IDENTIFIER, "name")
        left_node = create_mock_node(
            cs.TS_FIELD_ACCESS,
            fields={cs.FIELD_OBJECT: object_node, cs.FIELD_FIELD: field_node},
        )
        right_node = create_mock_node(cs.TS_STRING_LITERAL, '"value"')
        assignment = create_mock_node(
            cs.TS_ASSIGNMENT_EXPRESSION,
            fields={cs.FIELD_LEFT: left_node, cs.FIELD_RIGHT: right_node},
            children=[left_node, right_node],
        )
        scope_node = create_mock_node("block", children=[assignment])
        local_var_types: dict[str, str] = {}

        engine._analyze_java_constructor_assignments(
            scope_node, local_var_types, "com.example"
        )

        assert local_var_types["this.name"] == "java.lang.String"

    def test_nested_assignments(self, engine: JavaTypeInferenceEngine) -> None:
        left1 = create_mock_node(cs.TS_IDENTIFIER, "count")
        right1 = create_mock_node(cs.TS_INTEGER_LITERAL, "42")
        assign1 = create_mock_node(
            cs.TS_ASSIGNMENT_EXPRESSION,
            fields={cs.FIELD_LEFT: left1, cs.FIELD_RIGHT: right1},
            children=[left1, right1],
        )

        left2 = create_mock_node(cs.TS_IDENTIFIER, "value")
        right2 = create_mock_node(cs.TS_DECIMAL_FLOATING_POINT_LITERAL, "3.14")
        assign2 = create_mock_node(
            cs.TS_ASSIGNMENT_EXPRESSION,
            fields={cs.FIELD_LEFT: left2, cs.FIELD_RIGHT: right2},
            children=[left2, right2],
        )
        nested_block = create_mock_node("block", children=[assign2])

        scope_node = create_mock_node("block", children=[assign1, nested_block])
        local_var_types: dict[str, str] = {}

        engine._analyze_java_constructor_assignments(
            scope_node, local_var_types, "com.example"
        )

        assert local_var_types["count"] == "int"
        assert local_var_types["value"] == "double"


class TestExtractJavaVariableReference:
    def test_identifier(self, engine: JavaTypeInferenceEngine) -> None:
        node = create_mock_node(cs.TS_IDENTIFIER, "myVar")

        result = engine._extract_java_variable_reference(node)

        assert result == "myVar"

    def test_field_access(self, engine: JavaTypeInferenceEngine) -> None:
        object_node = create_mock_node(cs.TS_IDENTIFIER, "this")
        field_node = create_mock_node(cs.TS_IDENTIFIER, "name")
        node = create_mock_node(
            cs.TS_FIELD_ACCESS,
            fields={cs.FIELD_OBJECT: object_node, cs.FIELD_FIELD: field_node},
        )

        result = engine._extract_java_variable_reference(node)

        assert result == "this.name"

    def test_unknown_node_type(self, engine: JavaTypeInferenceEngine) -> None:
        node = create_mock_node("unknown_type", "something")

        result = engine._extract_java_variable_reference(node)

        assert result is None

    def test_field_access_missing_parts(self, engine: JavaTypeInferenceEngine) -> None:
        object_node = create_mock_node(cs.TS_IDENTIFIER, "obj")
        node = create_mock_node(
            cs.TS_FIELD_ACCESS,
            fields={cs.FIELD_OBJECT: object_node},
        )

        result = engine._extract_java_variable_reference(node)

        assert result is None


class TestAnalyzeJavaEnhancedForLoops:
    def test_enhanced_for_with_type_and_name(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        type_node = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        name_node = create_mock_node(cs.TS_IDENTIFIER, "item")
        for_node = create_mock_node(
            cs.TS_ENHANCED_FOR_STATEMENT,
            fields={cs.FIELD_TYPE: type_node, cs.FIELD_NAME: name_node},
        )
        scope_node = create_mock_node("block", children=[for_node])
        local_var_types: dict[str, str] = {}

        engine._analyze_java_enhanced_for_loops(
            scope_node, local_var_types, "com.example"
        )

        assert local_var_types["item"] == "java.lang.String"

    def test_enhanced_for_with_child_variable_declarator(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        type_identifier = create_mock_node(cs.TS_TYPE_IDENTIFIER, "Integer")
        name_node = create_mock_node(cs.TS_IDENTIFIER, "num")
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: name_node},
        )
        for_node = create_mock_node(
            cs.TS_ENHANCED_FOR_STATEMENT,
            children=[type_identifier, declarator],
        )
        declarator.node_parent = for_node

        scope_node = create_mock_node("block", children=[for_node])
        local_var_types: dict[str, str] = {}

        engine._analyze_java_enhanced_for_loops(
            scope_node, local_var_types, "com.example"
        )

        assert local_var_types["num"] == "java.lang.Integer"

    def test_nested_enhanced_for_loops(self, engine: JavaTypeInferenceEngine) -> None:
        type1 = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        name1 = create_mock_node(cs.TS_IDENTIFIER, "outer")
        for1 = create_mock_node(
            cs.TS_ENHANCED_FOR_STATEMENT,
            fields={cs.FIELD_TYPE: type1, cs.FIELD_NAME: name1},
        )

        type2 = create_mock_node(cs.TS_TYPE_IDENTIFIER, "Integer")
        name2 = create_mock_node(cs.TS_IDENTIFIER, "inner")
        for2 = create_mock_node(
            cs.TS_ENHANCED_FOR_STATEMENT,
            fields={cs.FIELD_TYPE: type2, cs.FIELD_NAME: name2},
        )

        inner_block = create_mock_node("block", children=[for2])
        for1.node_children.append(inner_block)
        inner_block.node_parent = for1

        scope_node = create_mock_node("block", children=[for1])
        local_var_types: dict[str, str] = {}

        engine._analyze_java_enhanced_for_loops(
            scope_node, local_var_types, "com.example"
        )

        assert local_var_types["outer"] == "java.lang.String"
        assert local_var_types["inner"] == "java.lang.Integer"


class TestInferJavaTypeFromExpression:
    def test_object_creation_expression(self, engine: JavaTypeInferenceEngine) -> None:
        type_node = create_mock_node(cs.TS_TYPE_IDENTIFIER, "ArrayList")
        expr = create_mock_node(
            cs.TS_OBJECT_CREATION_EXPRESSION,
            fields={cs.FIELD_TYPE: type_node},
        )

        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result == "ArrayList"

    def test_string_literal(self, engine: JavaTypeInferenceEngine) -> None:
        expr = create_mock_node(cs.TS_STRING_LITERAL, '"hello"')

        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result == "String"

    def test_integer_literal(self, engine: JavaTypeInferenceEngine) -> None:
        expr = create_mock_node(cs.TS_INTEGER_LITERAL, "42")

        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result == "int"

    def test_decimal_floating_point_literal(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        expr = create_mock_node(cs.TS_DECIMAL_FLOATING_POINT_LITERAL, "3.14")

        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result == "double"

    def test_true_literal(self, engine: JavaTypeInferenceEngine) -> None:
        expr = create_mock_node(cs.TS_TRUE, "true")

        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result == "boolean"

    def test_false_literal(self, engine: JavaTypeInferenceEngine) -> None:
        expr = create_mock_node(cs.TS_FALSE, "false")

        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result == "boolean"

    def test_array_creation_expression(self, engine: JavaTypeInferenceEngine) -> None:
        type_node = create_mock_node(cs.TS_TYPE_IDENTIFIER, "int")
        expr = create_mock_node(
            cs.TS_ARRAY_CREATION_EXPRESSION,
            fields={cs.FIELD_TYPE: type_node},
        )

        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result == "int[]"

    def test_unknown_expression_type(self, engine: JavaTypeInferenceEngine) -> None:
        expr = create_mock_node("unknown_expression", "something")

        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result is None


class TestCollectAllVariableTypes:
    def test_collects_from_all_sources(self, engine: JavaTypeInferenceEngine) -> None:
        param_name = create_mock_node(cs.TS_IDENTIFIER, "param1")
        param_type = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        param = create_mock_node(
            cs.TS_FORMAL_PARAMETER,
            fields={cs.FIELD_NAME: param_name, cs.FIELD_TYPE: param_type},
        )
        params_node = create_mock_node(cs.FIELD_PARAMETERS, children=[param])

        local_name = create_mock_node(cs.TS_IDENTIFIER, "local1")
        local_decl = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: local_name},
        )
        local_type = create_mock_node(cs.TS_TYPE_IDENTIFIER, "int")
        local_var = create_mock_node(
            cs.TS_LOCAL_VARIABLE_DECLARATION,
            fields={cs.FIELD_TYPE: local_type, cs.FIELD_DECLARATOR: local_decl},
        )

        left = create_mock_node(cs.TS_IDENTIFIER, "assigned1")
        right = create_mock_node(cs.TS_STRING_LITERAL, '"value"')
        assignment = create_mock_node(
            cs.TS_ASSIGNMENT_EXPRESSION,
            fields={cs.FIELD_LEFT: left, cs.FIELD_RIGHT: right},
            children=[left, right],
        )

        for_type = create_mock_node(cs.TS_TYPE_IDENTIFIER, "Double")
        for_name = create_mock_node(cs.TS_IDENTIFIER, "loopVar")
        for_stmt = create_mock_node(
            cs.TS_ENHANCED_FOR_STATEMENT,
            fields={cs.FIELD_TYPE: for_type, cs.FIELD_NAME: for_name},
        )

        body = create_mock_node("block", children=[local_var, assignment, for_stmt])
        scope_node = create_mock_node(
            "method_declaration",
            fields={cs.FIELD_PARAMETERS: params_node},
            children=[params_node, body],
        )

        local_var_types: dict[str, str] = {}
        engine._collect_all_variable_types(scope_node, local_var_types, "com.example")

        assert "param1" in local_var_types
        assert "local1" in local_var_types
        assert "assigned1" in local_var_types
        assert "loopVar" in local_var_types


class TestBuildVariableTypeMap:
    def test_builds_map_successfully(self, engine: JavaTypeInferenceEngine) -> None:
        name_node = create_mock_node(cs.TS_IDENTIFIER, "myVar")
        type_node = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: name_node},
        )
        decl = create_mock_node(
            cs.TS_LOCAL_VARIABLE_DECLARATION,
            fields={cs.FIELD_TYPE: type_node, cs.FIELD_DECLARATOR: declarator},
        )
        scope_node = create_mock_node("method_declaration", children=[decl])

        result = engine.build_variable_type_map(scope_node, "com.example")

        assert "myVar" in result
        assert result["myVar"] == "java.lang.String"

    def test_returns_empty_on_no_variables(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        scope_node = create_mock_node("method_declaration", children=[])

        result = engine.build_variable_type_map(scope_node, "com.example")

        assert result == {}


class TestLookupJavaFieldType:
    def test_returns_none_for_empty_class_type(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        result = engine._lookup_java_field_type("", "field", "com.example")

        assert result is None

    def test_returns_none_for_empty_field_name(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        result = engine._lookup_java_field_type("MyClass", "", "com.example")

        assert result is None

    def test_returns_none_when_file_not_in_cache(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        engine.module_qn_to_file_path = {}

        result = engine._lookup_java_field_type("MyClass", "field", "com.example")

        assert result is None


class TestFindFieldTypeInClass:
    def test_finds_field_in_class(self, engine: JavaTypeInferenceEngine) -> None:
        class_name = create_mock_node(cs.TS_TYPE_IDENTIFIER, "MyClass")
        field_name = create_mock_node(cs.TS_IDENTIFIER, "myField")
        field_declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: field_name},
        )
        field_type = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        field_decl = create_mock_node(
            cs.TS_FIELD_DECLARATION,
            fields={
                cs.FIELD_TYPE: field_type,
                cs.FIELD_DECLARATOR: field_declarator,
            },
        )
        class_body = create_mock_node("class_body", children=[field_decl])
        class_node = create_mock_node(
            cs.TS_CLASS_DECLARATION,
            fields={cs.FIELD_NAME: class_name, cs.FIELD_BODY: class_body},
        )
        root_node = create_mock_node("program", children=[class_node])

        result = engine._find_field_type_in_class(
            root_node, "MyClass", "myField", "com.example"
        )

        assert result == "java.lang.String"

    def test_returns_none_when_class_not_found(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        class_name = create_mock_node(cs.TS_TYPE_IDENTIFIER, "OtherClass")
        class_node = create_mock_node(
            cs.TS_CLASS_DECLARATION,
            fields={cs.FIELD_NAME: class_name},
        )
        root_node = create_mock_node("program", children=[class_node])

        result = engine._find_field_type_in_class(
            root_node, "MyClass", "myField", "com.example"
        )

        assert result is None

    def test_returns_none_when_field_not_found(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        class_name = create_mock_node(cs.TS_TYPE_IDENTIFIER, "MyClass")
        other_field_name = create_mock_node(cs.TS_IDENTIFIER, "otherField")
        field_declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: other_field_name},
        )
        field_type = create_mock_node(cs.TS_TYPE_IDENTIFIER, "String")
        field_decl = create_mock_node(
            cs.TS_FIELD_DECLARATION,
            fields={
                cs.FIELD_TYPE: field_type,
                cs.FIELD_DECLARATOR: field_declarator,
            },
        )
        class_body = create_mock_node("class_body", children=[field_decl])
        class_node = create_mock_node(
            cs.TS_CLASS_DECLARATION,
            fields={cs.FIELD_NAME: class_name, cs.FIELD_BODY: class_body},
        )
        root_node = create_mock_node("program", children=[class_node])

        result = engine._find_field_type_in_class(
            root_node, "MyClass", "myField", "com.example"
        )

        assert result is None


class TestDoVariableTypeLookup:
    def test_returns_none_for_short_module_qn(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        result = engine._do_variable_type_lookup("varName", "single")

        assert result is None

    def test_returns_none_when_module_not_in_path_map(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        engine.module_qn_to_file_path = {}

        result = engine._do_variable_type_lookup("varName", "com.example")

        assert result is None

    def test_returns_none_when_file_not_in_ast_cache(
        self, engine: JavaTypeInferenceEngine, mock_ast_cache: MagicMock
    ) -> None:
        file_path = Path("/test/Example.java")
        engine.module_qn_to_file_path = {"com.example": file_path}
        mock_ast_cache.__contains__ = MagicMock(return_value=False)

        result = engine._do_variable_type_lookup("varName", "com.example")

        assert result is None
