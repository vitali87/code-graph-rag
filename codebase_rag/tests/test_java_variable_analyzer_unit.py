import sys
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Language, Node, Parser

from codebase_rag import constants as cs
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.java.type_inference import JavaTypeInferenceEngine
from codebase_rag.tests.conftest import MockNode, create_mock_node


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
    # load() mirrors the real cache's reload-on-miss contract through the
    # per-test dunder mocks (closes over `cache`, so reassignment is seen).
    cache.load = lambda key: cache[key] if key in cache else None
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


def _parse_java_method(body: str) -> Node:
    tsjava = pytest.importorskip("tree_sitter_java")
    source = "class Example { void run() {" + body + "}}"
    tree = Parser(Language(tsjava.language())).parse(source.encode())
    assert not tree.root_node.has_error
    class_body = tree.root_node.named_children[0].child_by_field_name(cs.FIELD_BODY)
    assert class_body is not None
    return class_body.named_children[0]


@pytest.mark.parametrize("shape", ["nested_blocks", "string_concatenation"])
def test_build_variable_type_map_handles_deep_java_ast(
    engine: JavaTypeInferenceEngine, shape: str
) -> None:
    depth = sys.getrecursionlimit() + 50
    if shape == "nested_blocks":
        body = "{" * depth + 'String value = "text";' + "}" * depth
    else:
        body = "String value = " + '"text" + ' * depth + '"end";'
    method = _parse_java_method(body + 'String after = "done";')

    result = engine.build_variable_type_map(method, "com.example")

    assert result == {"value": "java.lang.String", "after": "java.lang.String"}


def test_build_variable_type_map_preserves_declaration_order(
    engine: JavaTypeInferenceEngine,
) -> None:
    method = _parse_java_method('String value = "text"; var copy = value;')

    result = engine.build_variable_type_map(method, "com.example")

    assert result == {"value": "java.lang.String", "copy": "java.lang.String"}


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

    @pytest.mark.parametrize(
        ("signature", "expected"),
        [
            ("java.util.List<String>... xs", {"xs": "java.util.List<String>[]"}),
            ("String[]... ys", {"ys": "java.lang.String[][]"}),
            ("int... zs", {"zs": "int[]"}),
            ("final /* c */ String... vs", {"vs": "java.lang.String[]"}),
            # An ANNOTATION in the modifiers, which the `modifiers` skip must
            # cover as well as `final` and a comment (Copilot, #1974).
            ("final @Deprecated String... ws", {"ws": "java.lang.String[]"}),
        ],
    )
    def test_spread_parameter_of_every_shape_binds_a_local(
        self, engine: JavaTypeInferenceEngine, signature: str, expected: dict[str, str]
    ) -> None:
        """Parsed with the real grammar: a generic, array or primitive varargs
        bound no local before, since only `type_identifier` was read
        (issue #1974); a comment after `final` is not the type either."""
        tsjava = pytest.importorskip("tree_sitter_java")
        source = f"class Example {{ void run({signature}) {{}} }}"
        tree = Parser(Language(tsjava.language())).parse(source.encode())
        assert not tree.root_node.has_error
        class_body = tree.root_node.named_children[0].child_by_field_name(cs.FIELD_BODY)
        assert class_body is not None
        method = class_body.named_children[0]
        local_var_types: dict[str, str] = {}

        engine._analyze_java_parameters(method, local_var_types, "com.example")

        assert local_var_types == expected

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
    def test_class_field_extracted(self, engine: JavaTypeInferenceEngine) -> None:
        # Parsed with the real grammar: the class's fields are cached on the
        # class node (issue #2453), which a tree-sitter node can key and the
        # value-compared MockNode cannot.
        tsjava = pytest.importorskip("tree_sitter_java")
        source = "class MyClass { private String name; void run() {} }"
        tree = Parser(Language(tsjava.language())).parse(source.encode())
        assert not tree.root_node.has_error
        class_body = tree.root_node.named_children[0].child_by_field_name(cs.FIELD_BODY)
        assert class_body is not None
        method_body = class_body.named_children[1].child_by_field_name(cs.FIELD_BODY)
        assert method_body is not None

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
        right1 = create_mock_node(cs.TS_DECIMAL_INTEGER_LITERAL, "42")
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
        # tree-sitter-java names integer literals by base, so the node type is
        # `decimal_integer_literal`; the old `integer_literal` matched nothing.
        expr = create_mock_node(cs.TS_DECIMAL_INTEGER_LITERAL, "42")

        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result == "int"

    def test_long_suffixed_literal(self, engine: JavaTypeInferenceEngine) -> None:
        expr = create_mock_node(cs.TS_DECIMAL_INTEGER_LITERAL, "42L")

        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result == "long"

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

    @pytest.mark.parametrize(
        "expr",
        [
            create_mock_node(cs.TS_OBJECT_CREATION_EXPRESSION),
            create_mock_node(cs.TS_ARRAY_CREATION_EXPRESSION),
            create_mock_node(
                cs.TS_ARRAY_CREATION_EXPRESSION,
                fields={cs.FIELD_TYPE: create_mock_node(cs.TS_TYPE_IDENTIFIER)},
            ),
            create_mock_node(cs.TS_JAVA_CAST_EXPRESSION),
            create_mock_node(
                cs.TS_PARENTHESIZED_EXPRESSION,
                children=[create_mock_node("("), create_mock_node(")")],
            ),
        ],
        ids=[
            "object-creation-without-type",
            "array-creation-without-type",
            "array-creation-with-empty-type",
            "cast-without-type",
            "parentheses-around-nothing",
        ],
    )
    def test_expression_without_a_type_to_read_is_untyped(
        self, engine: JavaTypeInferenceEngine, expr: MockNode
    ) -> None:
        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result is None

    def test_identifier_without_text_is_never_looked_up(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        expr = create_mock_node(cs.TS_IDENTIFIER)

        result = engine._infer_java_type_from_expression(
            expr, "com.example", {"": "Ghost"}
        )

        assert result is None

    def test_cast_reads_its_target_type(self, engine: JavaTypeInferenceEngine) -> None:
        expr = create_mock_node(
            cs.TS_JAVA_CAST_EXPRESSION,
            fields={cs.FIELD_TYPE: create_mock_node(cs.TS_TYPE_IDENTIFIER, "Square")},
        )

        result = engine._infer_java_type_from_expression(expr, "com.example")

        assert result == "Square"

    def test_parentheses_type_their_inner_expression(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        inner = create_mock_node(cs.TS_IDENTIFIER, "shape")
        expr = create_mock_node(
            cs.TS_PARENTHESIZED_EXPRESSION,
            children=[create_mock_node("("), inner, create_mock_node(")")],
        )

        result = engine._infer_java_type_from_expression(
            expr, "com.example", {"shape": "Square"}
        )

        assert result == "Square"


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


class TestTryWithResourcesAndCatchParameters:
    # Issue #3341: try-with-resources variables and catch parameters never
    # entered the variable-type map, so calls through them got no CALLS edge.

    def test_explicit_resource(self, engine: JavaTypeInferenceEngine) -> None:
        method = _parse_java_method("try (Res r = new Res()) { int y = r.hashCode(); }")

        result = engine.build_variable_type_map(method, "com.example")

        assert result["r"] == "Res"

    def test_var_resource_inferred_from_initializer(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        method = _parse_java_method("try (var r = new Res()) { int y = r.hashCode(); }")

        result = engine.build_variable_type_map(method, "com.example")

        assert result["r"] == "Res"

    def test_final_resource(self, engine: JavaTypeInferenceEngine) -> None:
        method = _parse_java_method(
            "try (final Res r = new Res()) { int y = r.hashCode(); }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["r"] == "Res"

    def test_two_resources(self, engine: JavaTypeInferenceEngine) -> None:
        method = _parse_java_method(
            "try (Res a = new Res(); Res b = new Res()) { int y = 1; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["a"] == "Res"
        assert result["b"] == "Res"

    def test_catch_parameter(self, engine: JavaTypeInferenceEngine) -> None:
        method = _parse_java_method(
            "try { int x = 1; } catch (AppError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["e"] == "AppError"

    def test_catch_parameter_final_and_annotated(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        method = _parse_java_method(
            "try { int x = 1; } catch (final @Deprecated AppError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["e"] == "AppError"

    def test_catch_parameter_scoped_type(self, engine: JavaTypeInferenceEngine) -> None:
        method = _parse_java_method(
            "try { int x = 1; } catch (pkg.AppError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["e"] == "pkg.AppError"

    def _engine_with_inheritance(
        self,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
        mock_ast_cache: MagicMock,
        class_inheritance: dict[str, list[str]],
    ) -> JavaTypeInferenceEngine:
        return JavaTypeInferenceEngine(
            import_processor=mock_import_processor,
            function_registry=mock_function_registry,
            repo_path=Path("/test/repo"),
            project_name="test_project",
            ast_cache=mock_ast_cache,
            queries={},
            module_qn_to_file_path={},
            class_inheritance=class_inheritance,
            simple_name_lookup=defaultdict(set),
        )

    def test_multi_catch_common_first_party_supertype(
        self,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
        mock_ast_cache: MagicMock,
    ) -> None:
        engine = self._engine_with_inheritance(
            mock_import_processor,
            mock_function_registry,
            mock_ast_cache,
            {
                "AppError": ["BaseError"],
                "OtherError": ["BaseError"],
                "BaseError": ["RuntimeException"],
            },
        )
        method = _parse_java_method(
            "try { int x = 1; } catch (AppError | OtherError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["e"] == "BaseError"

    def test_multi_catch_unrelated_types_stay_untyped(
        self,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
        mock_ast_cache: MagicMock,
    ) -> None:
        engine = self._engine_with_inheritance(
            mock_import_processor,
            mock_function_registry,
            mock_ast_cache,
            {
                "AppError": ["RuntimeException"],
                "OtherError": ["RuntimeException"],
            },
        )
        method = _parse_java_method(
            "try { int x = 1; } catch (AppError | OtherError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert "e" not in result

    def test_multi_catch_jdk_types_stay_untyped(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        # IOException | SQLException share only JDK supertypes: never guess.
        method = _parse_java_method(
            "try { int x = 1; } catch (IOException | SQLException e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert "e" not in result

    def test_bare_resource_keeps_outer_type(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        # Java 9 `try (r)` declares nothing: it must not clobber the outer type.
        method = _parse_java_method("Other r = new Other(); try (r) { int y = 2; }")

        result = engine.build_variable_type_map(method, "com.example")

        assert result["r"] == "Other"

    def test_undeclared_bare_resource_binds_nothing(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        # Guard (passes with or without the fix): a bare `try (r)` with no
        # declaration of `r` anywhere produces no entry at all. It proves
        # the resource pass never invents a binding from a bare reference;
        # it is not a fail-before regression test.
        method = _parse_java_method("try (r) { int y = 2; }")

        result = engine.build_variable_type_map(method, "com.example")

        assert "r" not in result

    def test_qualified_multi_catch_binds_first_party_lca(
        self,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
        mock_ast_cache: MagicMock,
    ) -> None:
        # Qualified alternatives whose keys use the doubled file-module form
        # still resolve: identity comes from the package path, not the
        # simple name alone (case 5/9 of the #3341 case matrix).
        engine = self._engine_with_inheritance(
            mock_import_processor,
            mock_function_registry,
            mock_ast_cache,
            {
                "jX.com.example.AppError.AppError": [
                    "jX.com.example.BaseError.BaseError"
                ],
                "jX.com.example.OtherError.OtherError": [
                    "jX.com.example.BaseError.BaseError"
                ],
                "jX.com.example.BaseError.BaseError": ["java.lang.RuntimeException"],
            },
        )
        method = _parse_java_method(
            "try { int x = 1; } "
            "catch (com.example.AppError | com.example.OtherError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["e"] == "jX.com.example.BaseError.BaseError"

    def test_first_party_plus_jdk_alternative_stays_untyped(
        self,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
        mock_ast_cache: MagicMock,
    ) -> None:
        # The JDK alternative contributes no chain: a same-named first-party
        # IOException must not stand in for java.io.IOException (case 11).
        engine = self._engine_with_inheritance(
            mock_import_processor,
            mock_function_registry,
            mock_ast_cache,
            {
                "AppError": ["BaseError"],
                "BaseError": ["java.lang.RuntimeException"],
                "IOException": ["BaseError"],
            },
        )
        method = _parse_java_method(
            "try { int x = 1; } catch (AppError | java.io.IOException e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert "e" not in result

    def test_three_alternatives_bind_common_supertype(
        self,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
        mock_ast_cache: MagicMock,
    ) -> None:
        engine = self._engine_with_inheritance(
            mock_import_processor,
            mock_function_registry,
            mock_ast_cache,
            {
                "AppError": ["BaseError"],
                "OtherError": ["BaseError"],
                "ThirdError": ["BaseError"],
                "BaseError": ["java.lang.RuntimeException"],
            },
        )
        method = _parse_java_method(
            "try { int x = 1; } "
            "catch (AppError | OtherError | ThirdError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["e"] == "BaseError"

    def test_duplicate_alternatives_dedupe_to_one_type(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        method = _parse_java_method(
            "try { int x = 1; } catch (AppError | AppError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["e"] == "AppError"

    def test_error_alternative_binds_nothing(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        # Annotations on union alternatives do not parse with this grammar:
        # the catch_type carries an ERROR node, so nothing is bound.
        tsjava = pytest.importorskip("tree_sitter_java")
        source = (
            "class Example { void run() { "
            "try { int x = 1; } "
            "catch (@A AppError | @B OtherError e) { int y = 2; } } }"
        )
        tree = Parser(Language(tsjava.language())).parse(source.encode())
        assert tree.root_node.has_error
        class_body = tree.root_node.named_children[0].child_by_field_name(cs.FIELD_BODY)
        assert class_body is not None
        method = class_body.named_children[0]

        result = engine.build_variable_type_map(method, "com.example")

        assert "e" not in result

    def test_diamond_inheritance_binds_nearest_common_supertype(
        self,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
        mock_ast_cache: MagicMock,
    ) -> None:
        engine = self._engine_with_inheritance(
            mock_import_processor,
            mock_function_registry,
            mock_ast_cache,
            {
                "B": ["A"],
                "C": ["A"],
                "A": ["java.lang.Object"],
            },
        )
        method = _parse_java_method("try { int x = 1; } catch (B | C e) { int y = 2; }")

        result = engine.build_variable_type_map(method, "com.example")

        assert result["e"] == "A"

    def test_grandparent_supertype_binds(
        self,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
        mock_ast_cache: MagicMock,
    ) -> None:
        engine = self._engine_with_inheritance(
            mock_import_processor,
            mock_function_registry,
            mock_ast_cache,
            {
                "AppError": ["MidError"],
                "MidError": ["BaseError"],
                "BaseError": ["java.lang.RuntimeException"],
                "OtherError": ["BaseError"],
            },
        )
        method = _parse_java_method(
            "try { int x = 1; } catch (AppError | OtherError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["e"] == "BaseError"

    def test_chain_into_jdk_parent_stays_untyped(
        self,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
        mock_ast_cache: MagicMock,
    ) -> None:
        engine = self._engine_with_inheritance(
            mock_import_processor,
            mock_function_registry,
            mock_ast_cache,
            {
                "AppError": ["java.lang.RuntimeException"],
                "OtherError": ["BaseError"],
                "BaseError": ["java.lang.RuntimeException"],
            },
        )
        method = _parse_java_method(
            "try { int x = 1; } catch (AppError | OtherError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert "e" not in result

    def test_inheritance_cycle_terminates(
        self,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
        mock_ast_cache: MagicMock,
    ) -> None:
        engine = self._engine_with_inheritance(
            mock_import_processor,
            mock_function_registry,
            mock_ast_cache,
            {"A": ["B"], "B": ["A"]},
        )
        method = _parse_java_method("try { int x = 1; } catch (A | B e) { int y = 2; }")

        result = engine.build_variable_type_map(method, "com.example")

        # Both chains contain both classes; the tie breaks deterministically
        # to the first alternative's nearest candidate. The point is that
        # the walk terminates and binds one of them, never hangs.
        assert result["e"] == "A"

    def test_missing_parent_stays_untyped(
        self,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
        mock_ast_cache: MagicMock,
    ) -> None:
        engine = self._engine_with_inheritance(
            mock_import_processor,
            mock_function_registry,
            mock_ast_cache,
            {"AppError": ["Missing"], "OtherError": ["BaseError"]},
        )
        method = _parse_java_method(
            "try { int x = 1; } catch (AppError | OtherError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert "e" not in result

    def test_single_qualified_jdk_catch_keeps_qualified_type(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        # Case 20: the single-catch path resolves through
        # `_resolve_java_type_name`, which returns a dotted name verbatim, so
        # a qualified JDK type is never re-identified as a first-party class.
        method = _parse_java_method(
            "try { int x = 1; } catch (java.io.IOException e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["e"] == "java.io.IOException"

    def test_untyped_multi_catch_leaves_outer_binding(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        # Case 22: with no common supertype, the catch pass writes nothing,
        # so an outer variable of the same name keeps its type.
        method = _parse_java_method(
            "Other e = new Other(); "
            "try { int x = 1; } catch (AppError | OtherError e) { int y = 2; }"
        )

        result = engine.build_variable_type_map(method, "com.example")

        assert result["e"] == "Other"


class TestInheritanceKeyFor:
    # Direct contract tests for `_inheritance_key_for`, the lookup the
    # Greptile P1 / CodeRabbit finding is about. Keys use the doubled
    # file-module form the real registry produces.

    def _key_for(self, engine: JavaTypeInferenceEngine, resolved: str) -> str | None:
        return engine._inheritance_key_for(resolved)

    def test_simple_name_resolves_doubled_key(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        engine.class_inheritance = {
            "jX.com.example.AppError.AppError": ["jX.com.example.BaseError.BaseError"]
        }

        assert self._key_for(engine, "AppError") == "jX.com.example.AppError.AppError"

    def test_simple_name_without_class_returns_none(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        engine.class_inheritance = {
            "jX.com.example.AppError.AppError": ["jX.com.example.BaseError.BaseError"]
        }

        assert self._key_for(engine, "Nope") is None

    def test_simple_name_shared_by_two_classes_returns_none(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        engine.class_inheritance = {
            "jX.com.example.AppError.AppError": ["jX.com.example.BaseError.BaseError"],
            "jX.other.AppError.AppError": ["jX.other.BaseError.BaseError"],
        }

        assert self._key_for(engine, "AppError") is None

    def test_qualified_name_exact_key_resolves(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        engine.class_inheritance = {"com.example.AppError": ["com.example.BaseError"]}

        assert self._key_for(engine, "com.example.AppError") == "com.example.AppError"

    def test_qualified_name_doubled_key_resolves(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        engine.class_inheritance = {
            "jX.com.example.AppError.AppError": ["jX.com.example.BaseError.BaseError"]
        }

        assert (
            self._key_for(engine, "com.example.AppError")
            == "jX.com.example.AppError.AppError"
        )

    def test_qualified_name_plain_key_suffix_resolves(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        engine.class_inheritance = {
            "jX.com.example.AppError": ["jX.com.example.BaseError"]
        }

        assert (
            self._key_for(engine, "com.example.AppError") == "jX.com.example.AppError"
        )

    def test_qualified_jdk_name_never_resolves_to_first_party(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        engine.class_inheritance = {
            "jX.com.example.IOException.IOException": [
                "jX.com.example.BaseError.BaseError"
            ],
            "jX.com.example.SQLException.SQLException": [
                "jX.com.example.BaseError.BaseError"
            ],
            "jX.com.example.Exception.Exception": [
                "jX.com.example.BaseError.BaseError"
            ],
        }

        assert self._key_for(engine, "java.io.IOException") is None
        assert self._key_for(engine, "java.sql.SQLException") is None
        assert self._key_for(engine, "java.lang.Exception") is None

    def test_qualified_unknown_package_returns_none(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        engine.class_inheritance = {
            "jX.com.example.AppError.AppError": ["jX.com.example.BaseError.BaseError"]
        }

        assert self._key_for(engine, "foo.bar.Baz") is None

    def test_nested_name_resolves_on_package_identity(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        engine.class_inheritance = {"com.example.Outer.Inner": ["com.example.Base"]}

        assert self._key_for(engine, "Outer.Inner") == "com.example.Outer.Inner"

        engine.class_inheritance = {
            "jX.com.example.Outer.Inner.Inner": ["jX.com.example.Base.Base"]
        }

        assert (
            self._key_for(engine, "Outer.Inner") == "jX.com.example.Outer.Inner.Inner"
        )

    def test_qualified_name_with_two_suffix_matches_returns_none(
        self, engine: JavaTypeInferenceEngine
    ) -> None:
        # Two different packages both end in the written path: identity is
        # ambiguous, so nothing is claimed.
        engine.class_inheritance = {
            "jX.com.example.AppError.AppError": ["jX.com.example.BaseError.BaseError"],
            "jX.org.example.AppError.AppError": ["jX.org.example.BaseError.BaseError"],
        }

        assert self._key_for(engine, "example.AppError") is None
