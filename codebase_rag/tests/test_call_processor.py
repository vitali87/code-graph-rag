from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import NodeType

if TYPE_CHECKING:
    from tree_sitter import Node, Parser

    from codebase_rag.parsers.call_processor import CallProcessor
    from codebase_rag.types_defs import LanguageQueries


@pytest.fixture
def parsers_and_queries() -> tuple[
    dict[cs.SupportedLanguage, Parser], dict[cs.SupportedLanguage, LanguageQueries]
]:
    parsers, queries = load_parsers()
    return parsers, queries


@pytest.fixture
def call_processor(temp_repo: Path, mock_ingestor: MagicMock) -> CallProcessor:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )
    return updater.factory.call_processor


def parse_code(
    code: str,
    language: cs.SupportedLanguage,
    parsers: dict[cs.SupportedLanguage, Parser],
) -> Node:
    parser = parsers[language]
    tree = parser.parse(code.encode(cs.ENCODING_UTF8))
    return tree.root_node


def find_first_node_of_type(root: Node, node_type: str) -> Node | None:
    if root.type == node_type:
        return root
    for child in root.children:
        if result := find_first_node_of_type(child, node_type):
            return result
    return None


class TestGetCallTargetName:
    def test_identifier_call(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "foo()"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        call_node = find_first_node_of_type(root, "call")
        assert call_node is not None

        result = call_processor._get_call_target_name(call_node)
        assert result == "foo"

    def test_attribute_call(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "obj.method()"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        call_node = find_first_node_of_type(root, "call")
        assert call_node is not None

        result = call_processor._get_call_target_name(call_node)
        assert result == "obj.method"

    def test_chained_attribute_call(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "a.b.c()"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        call_node = find_first_node_of_type(root, "call")
        assert call_node is not None

        result = call_processor._get_call_target_name(call_node)
        assert result == "a.b.c"

    def test_member_expression_js(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        code = "obj.method();"
        root = parse_code(code, cs.SupportedLanguage.JS, parsers)
        call_node = find_first_node_of_type(root, "call_expression")
        assert call_node is not None

        result = call_processor._get_call_target_name(call_node)
        assert result == "obj.method"

    def test_no_function_child_returns_none(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "x = 1"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)

        result = call_processor._get_call_target_name(root)
        assert result is None


class TestGetIifeTargetName:
    def test_function_expression_iife(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        code = "(function() { return 1; })();"
        root = parse_code(code, cs.SupportedLanguage.JS, parsers)
        paren_expr = find_first_node_of_type(root, "parenthesized_expression")
        assert paren_expr is not None

        result = call_processor._get_iife_target_name(paren_expr)
        assert result is not None
        assert result.startswith(cs.IIFE_FUNC_PREFIX)

    def test_arrow_function_iife(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        code = "(() => { return 1; })();"
        root = parse_code(code, cs.SupportedLanguage.JS, parsers)
        paren_expr = find_first_node_of_type(root, "parenthesized_expression")
        assert paren_expr is not None

        result = call_processor._get_iife_target_name(paren_expr)
        assert result is not None
        assert result.startswith(cs.IIFE_ARROW_PREFIX)

    def test_non_iife_returns_none(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        code = "(x + y);"
        root = parse_code(code, cs.SupportedLanguage.JS, parsers)
        paren_expr = find_first_node_of_type(root, "parenthesized_expression")
        assert paren_expr is not None

        result = call_processor._get_iife_target_name(paren_expr)
        assert result is None


class TestResolveBuiltinCall:
    def test_js_builtin_pattern_object_keys(
        self, call_processor: CallProcessor
    ) -> None:
        result = call_processor._resolve_builtin_call("Object.keys")
        assert result is not None
        assert result[0] == cs.NodeLabel.FUNCTION
        assert result[1] == f"{cs.BUILTIN_PREFIX}.Object.keys"

    def test_js_builtin_pattern_json_parse(self, call_processor: CallProcessor) -> None:
        result = call_processor._resolve_builtin_call("JSON.parse")
        assert result is not None
        assert result[0] == cs.NodeLabel.FUNCTION
        assert result[1] == f"{cs.BUILTIN_PREFIX}.JSON.parse"

    def test_bind_method(self, call_processor: CallProcessor) -> None:
        result = call_processor._resolve_builtin_call("someFunc.bind")
        assert result is not None
        assert result[0] == cs.NodeLabel.FUNCTION
        assert result[1] == f"{cs.BUILTIN_PREFIX}.Function.prototype.bind"

    def test_call_method(self, call_processor: CallProcessor) -> None:
        result = call_processor._resolve_builtin_call("someFunc.call")
        assert result is not None
        assert result[0] == cs.NodeLabel.FUNCTION
        assert result[1] == f"{cs.BUILTIN_PREFIX}.Function.prototype.call"

    def test_apply_method(self, call_processor: CallProcessor) -> None:
        result = call_processor._resolve_builtin_call("someFunc.apply")
        assert result is not None
        assert result[0] == cs.NodeLabel.FUNCTION
        assert result[1] == f"{cs.BUILTIN_PREFIX}.Function.prototype.apply"

    def test_prototype_call(self, call_processor: CallProcessor) -> None:
        # (H) .call suffix is matched first, returns Function.prototype.call
        result = call_processor._resolve_builtin_call("Array.prototype.slice.call")
        assert result is not None
        assert result[0] == cs.NodeLabel.FUNCTION
        assert result[1] == f"{cs.BUILTIN_PREFIX}.Function.prototype.call"

    def test_prototype_apply(self, call_processor: CallProcessor) -> None:
        result = call_processor._resolve_builtin_call("String.prototype.split.apply")
        assert result is not None
        assert result[0] == cs.NodeLabel.FUNCTION
        assert result[1] == f"{cs.BUILTIN_PREFIX}.Function.prototype.apply"

    def test_non_builtin_returns_none(self, call_processor: CallProcessor) -> None:
        result = call_processor._resolve_builtin_call("myCustomFunction")
        assert result is None

    def test_unknown_method_returns_none(self, call_processor: CallProcessor) -> None:
        result = call_processor._resolve_builtin_call("obj.unknownMethod")
        assert result is None


class TestResolveSuperCall:
    @pytest.fixture
    def processor_with_inheritance(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> CallProcessor:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        processor = updater.factory.call_processor

        processor.class_inheritance["proj.module.ChildClass"] = [
            "proj.module.ParentClass"
        ]
        processor.class_inheritance["proj.module.ParentClass"] = [
            "proj.module.GrandparentClass"
        ]

        processor.function_registry["proj.module.ParentClass.constructor"] = (
            NodeType.METHOD
        )
        processor.function_registry["proj.module.ParentClass.someMethod"] = (
            NodeType.METHOD
        )
        processor.function_registry["proj.module.GrandparentClass.inheritedMethod"] = (
            NodeType.METHOD
        )

        return processor

    def test_super_calls_constructor(
        self, processor_with_inheritance: CallProcessor
    ) -> None:
        result = processor_with_inheritance._resolve_super_call(
            cs.KEYWORD_SUPER, class_context="proj.module.ChildClass"
        )
        assert result is not None
        assert result[1] == "proj.module.ParentClass.constructor"

    def test_super_dot_method(self, processor_with_inheritance: CallProcessor) -> None:
        result = processor_with_inheritance._resolve_super_call(
            f"{cs.KEYWORD_SUPER}.someMethod", class_context="proj.module.ChildClass"
        )
        assert result is not None
        assert result[1] == "proj.module.ParentClass.someMethod"

    def test_super_inherited_from_grandparent(
        self, processor_with_inheritance: CallProcessor
    ) -> None:
        result = processor_with_inheritance._resolve_super_call(
            f"{cs.KEYWORD_SUPER}.inheritedMethod",
            class_context="proj.module.ChildClass",
        )
        assert result is not None
        assert result[1] == "proj.module.GrandparentClass.inheritedMethod"

    def test_super_no_class_context_returns_none(
        self, processor_with_inheritance: CallProcessor
    ) -> None:
        result = processor_with_inheritance._resolve_super_call(
            cs.KEYWORD_SUPER, class_context=None
        )
        assert result is None

    def test_super_unknown_class_returns_none(
        self, processor_with_inheritance: CallProcessor
    ) -> None:
        result = processor_with_inheritance._resolve_super_call(
            cs.KEYWORD_SUPER, class_context="proj.module.UnknownClass"
        )
        assert result is None

    def test_super_method_not_found_returns_none(
        self, processor_with_inheritance: CallProcessor
    ) -> None:
        result = processor_with_inheritance._resolve_super_call(
            f"{cs.KEYWORD_SUPER}.nonExistentMethod",
            class_context="proj.module.ChildClass",
        )
        assert result is None


class TestResolveCppOperatorCall:
    def test_builtin_operator_plus(self, call_processor: CallProcessor) -> None:
        result = call_processor._resolve_cpp_operator_call(
            "operator_plus", "proj.module"
        )
        assert result is not None
        assert result[0] == cs.NodeLabel.FUNCTION
        assert result[1] == cs.CPP_OPERATORS["operator_plus"]

    def test_builtin_operator_equal(self, call_processor: CallProcessor) -> None:
        result = call_processor._resolve_cpp_operator_call(
            "operator_equal", "proj.module"
        )
        assert result is not None
        assert result[0] == cs.NodeLabel.FUNCTION
        assert result[1] == cs.CPP_OPERATORS["operator_equal"]

    def test_non_operator_returns_none(self, call_processor: CallProcessor) -> None:
        result = call_processor._resolve_cpp_operator_call(
            "someFunction", "proj.module"
        )
        assert result is None

    def test_custom_operator_from_registry(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        processor = updater.factory.call_processor

        processor.function_registry["proj.module.MyClass.operator_custom"] = (
            NodeType.METHOD
        )

        result = processor._resolve_cpp_operator_call("operator_custom", "proj.module")
        assert result is not None
        assert result[1] == "proj.module.MyClass.operator_custom"


class TestResolveInheritedMethod:
    @pytest.fixture
    def processor_with_inheritance(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> CallProcessor:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        processor = updater.factory.call_processor

        processor.class_inheritance["proj.Child"] = ["proj.Parent"]
        processor.class_inheritance["proj.Parent"] = ["proj.Grandparent"]
        processor.class_inheritance["proj.Grandparent"] = []

        processor.function_registry["proj.Parent.parentMethod"] = NodeType.METHOD
        processor.function_registry["proj.Grandparent.grandparentMethod"] = (
            NodeType.METHOD
        )

        return processor

    def test_finds_method_in_parent(
        self, processor_with_inheritance: CallProcessor
    ) -> None:
        result = processor_with_inheritance._resolve_inherited_method(
            "proj.Child", "parentMethod"
        )
        assert result is not None
        assert result[1] == "proj.Parent.parentMethod"

    def test_finds_method_in_grandparent(
        self, processor_with_inheritance: CallProcessor
    ) -> None:
        result = processor_with_inheritance._resolve_inherited_method(
            "proj.Child", "grandparentMethod"
        )
        assert result is not None
        assert result[1] == "proj.Grandparent.grandparentMethod"

    def test_method_not_found_returns_none(
        self, processor_with_inheritance: CallProcessor
    ) -> None:
        result = processor_with_inheritance._resolve_inherited_method(
            "proj.Child", "nonExistent"
        )
        assert result is None

    def test_unknown_class_returns_none(
        self, processor_with_inheritance: CallProcessor
    ) -> None:
        result = processor_with_inheritance._resolve_inherited_method(
            "proj.Unknown", "someMethod"
        )
        assert result is None


class TestIsMethodChain:
    def test_simple_method_not_chain(self, call_processor: CallProcessor) -> None:
        assert call_processor._is_method_chain("obj.method") is False

    def test_method_with_parens_is_chain(self, call_processor: CallProcessor) -> None:
        assert call_processor._is_method_chain("obj.method().other") is True

    def test_chained_calls_is_chain(self, call_processor: CallProcessor) -> None:
        assert call_processor._is_method_chain("a.b().c().d") is True

    def test_no_dots_not_chain(self, call_processor: CallProcessor) -> None:
        assert call_processor._is_method_chain("method()") is False

    def test_empty_string_not_chain(self, call_processor: CallProcessor) -> None:
        assert call_processor._is_method_chain("") is False


class TestCalculateImportDistance:
    def test_same_module_distance_zero(self, call_processor: CallProcessor) -> None:
        distance = call_processor._calculate_import_distance(
            "proj.pkg.mod.func", "proj.pkg.mod"
        )
        assert distance == 0

    def test_sibling_module_distance_one(self, call_processor: CallProcessor) -> None:
        distance = call_processor._calculate_import_distance(
            "proj.pkg.other.func", "proj.pkg.mod"
        )
        assert distance == 1

    def test_distant_module_higher_distance(
        self, call_processor: CallProcessor
    ) -> None:
        distance = call_processor._calculate_import_distance(
            "other.pkg.mod.func", "proj.pkg.mod"
        )
        assert distance > 2

    def test_common_prefix_reduces_distance(
        self, call_processor: CallProcessor
    ) -> None:
        close_distance = call_processor._calculate_import_distance(
            "proj.pkg.other.func", "proj.pkg.mod"
        )
        far_distance = call_processor._calculate_import_distance(
            "other.pkg.other.func", "proj.pkg.mod"
        )
        assert close_distance < far_distance


class TestGetNodeName:
    def test_gets_name_from_function_def(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def my_function(): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        result = call_processor._get_node_name(func_node)
        assert result == "my_function"

    def test_gets_name_from_class_def(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "class MyClass: pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        class_node = find_first_node_of_type(root, "class_definition")
        assert class_node is not None

        result = call_processor._get_node_name(class_node)
        assert result == "MyClass"

    def test_returns_none_for_no_name(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "x = 1"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        expr_node = find_first_node_of_type(root, "expression_statement")
        assert expr_node is not None

        result = call_processor._get_node_name(expr_node)
        assert result is None

    def test_gets_field_by_custom_field_name(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def my_func(arg1): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        result = call_processor._get_node_name(func_node, "name")
        assert result == "my_func"


class TestIsMethod:
    def test_function_in_class_is_method(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
class MyClass:
    def my_method(self):
        pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = call_processor._is_method(func_node, lang_config)
        assert result is True

    def test_top_level_function_is_not_method(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def my_function(): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = call_processor._is_method(func_node, lang_config)
        assert result is False

    def test_nested_function_is_not_method(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
def outer():
    def inner():
        pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)

        def find_inner_function(node: Node) -> Node | None:
            if node.type == "function_definition":
                name_node = node.child_by_field_name("name")
                if name_node and name_node.text == b"inner":
                    return node
            for child in node.children:
                if result := find_inner_function(child):
                    return result
            return None

        inner_func = find_inner_function(root)
        assert inner_func is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = call_processor._is_method(inner_func, lang_config)
        assert result is False


class TestBuildNestedQualifiedName:
    def test_top_level_function(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def my_func(): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = call_processor._build_nested_qualified_name(
            func_node, "proj.module", "my_func", lang_config
        )
        assert result == "proj.module.my_func"

    def test_nested_function(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
def outer():
    def inner():
        pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)

        def find_inner_function(node: Node) -> Node | None:
            if node.type == "function_definition":
                name_node = node.child_by_field_name("name")
                if name_node and name_node.text == b"inner":
                    return node
            for child in node.children:
                if result := find_inner_function(child):
                    return result
            return None

        inner_func = find_inner_function(root)
        assert inner_func is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = call_processor._build_nested_qualified_name(
            inner_func, "proj.module", "inner", lang_config
        )
        assert result == "proj.module.outer.inner"

    def test_method_returns_none(
        self,
        call_processor: CallProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
class MyClass:
    def my_method(self):
        pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = call_processor._build_nested_qualified_name(
            func_node, "proj.module", "my_method", lang_config
        )
        assert result is None


class TestResolveFunctionCall:
    @pytest.fixture
    def processor_with_registry(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> CallProcessor:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        processor = updater.factory.call_processor

        processor.function_registry["proj.module.local_func"] = NodeType.FUNCTION
        processor.function_registry["proj.module.MyClass.method"] = NodeType.METHOD
        processor.function_registry["proj.other.other_func"] = NodeType.FUNCTION
        processor.function_registry["proj.utils.helper"] = NodeType.FUNCTION

        processor.import_processor.import_mapping["proj.module"] = {
            "other_func": "proj.other.other_func",
            "helper": "proj.utils.helper",
            "MyClass": "proj.module.MyClass",
        }

        return processor

    def test_resolves_same_module_function(
        self, processor_with_registry: CallProcessor
    ) -> None:
        result = processor_with_registry._resolve_function_call(
            "local_func", "proj.module"
        )
        assert result is not None
        assert result[1] == "proj.module.local_func"

    def test_resolves_imported_function(
        self, processor_with_registry: CallProcessor
    ) -> None:
        result = processor_with_registry._resolve_function_call(
            "other_func", "proj.module"
        )
        assert result is not None
        assert result[1] == "proj.other.other_func"

    def test_resolves_method_on_imported_class(
        self, processor_with_registry: CallProcessor
    ) -> None:
        result = processor_with_registry._resolve_function_call(
            "MyClass.method", "proj.module"
        )
        assert result is not None
        assert result[1] == "proj.module.MyClass.method"

    def test_returns_none_for_unknown_function(
        self, processor_with_registry: CallProcessor
    ) -> None:
        result = processor_with_registry._resolve_function_call(
            "unknown_func", "proj.module"
        )
        assert result is None

    def test_resolves_local_variable_method_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        processor = updater.factory.call_processor

        processor.function_registry["proj.models.User.save"] = NodeType.METHOD
        processor.import_processor.import_mapping["proj.service"] = {
            "User": "proj.models.User"
        }

        local_var_types = {"user": "User"}

        result = processor._resolve_function_call(
            "user.save", "proj.service", local_var_types
        )
        assert result is not None
        assert result[1] == "proj.models.User.save"

    def test_iife_function_resolution(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        processor = updater.factory.call_processor

        iife_qn = "proj.module.__iife_func_1_5"
        processor.function_registry[iife_qn] = NodeType.FUNCTION

        result = processor._resolve_function_call("__iife_func_1_5", "proj.module")
        assert result is not None
        assert result[1] == iife_qn


class TestResolveChainedCall:
    @pytest.fixture
    def processor_with_types(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> CallProcessor:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        processor = updater.factory.call_processor

        processor.function_registry["proj.models.QuerySet.filter"] = NodeType.METHOD
        processor.function_registry["proj.models.QuerySet.all"] = NodeType.METHOD
        processor.function_registry["proj.models.User.objects"] = NodeType.METHOD

        processor.import_processor.import_mapping["proj.views"] = {
            "User": "proj.models.User",
            "QuerySet": "proj.models.QuerySet",
        }

        return processor

    def test_returns_none_for_unresolvable_chain(
        self, processor_with_types: CallProcessor
    ) -> None:
        result = processor_with_types._resolve_chained_call(
            "unknown().method", "proj.views"
        )
        assert result is None

    def test_returns_none_for_non_chained_expression(
        self, processor_with_types: CallProcessor
    ) -> None:
        result = processor_with_types._resolve_chained_call("simple_call", "proj.views")
        assert result is None

    def test_returns_none_for_chain_without_type_info(
        self, processor_with_types: CallProcessor
    ) -> None:
        result = processor_with_types._resolve_chained_call(
            "unknown_object.method_one().method_two", "proj.views"
        )
        assert result is None


class TestTryResolveMethod:
    @pytest.fixture
    def processor_with_methods(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> CallProcessor:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        processor = updater.factory.call_processor

        processor.function_registry["proj.models.User.save"] = NodeType.METHOD
        processor.function_registry["proj.models.BaseModel.validate"] = NodeType.METHOD

        processor.class_inheritance["proj.models.User"] = ["proj.models.BaseModel"]

        return processor

    def test_resolves_direct_method(
        self, processor_with_methods: CallProcessor
    ) -> None:
        result = processor_with_methods._try_resolve_method("proj.models.User", "save")
        assert result is not None
        assert result[1] == "proj.models.User.save"

    def test_resolves_inherited_method(
        self, processor_with_methods: CallProcessor
    ) -> None:
        result = processor_with_methods._try_resolve_method(
            "proj.models.User", "validate"
        )
        assert result is not None
        assert result[1] == "proj.models.BaseModel.validate"

    def test_returns_none_for_unknown_method(
        self, processor_with_methods: CallProcessor
    ) -> None:
        result = processor_with_methods._try_resolve_method(
            "proj.models.User", "unknown_method"
        )
        assert result is None


class TestResolveClassQnFromType:
    @pytest.fixture
    def processor_with_imports(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> CallProcessor:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        processor = updater.factory.call_processor

        processor.function_registry["proj.models.User"] = NodeType.CLASS
        processor.import_processor.import_mapping["proj.service"] = {
            "User": "proj.models.User"
        }

        return processor

    def test_resolves_from_import_map(
        self, processor_with_imports: CallProcessor
    ) -> None:
        import_map = {"User": "proj.models.User"}
        result = processor_with_imports._resolve_class_qn_from_type(
            "User", import_map, "proj.service"
        )
        assert result == "proj.models.User"

    def test_returns_dotted_type_as_is(
        self, processor_with_imports: CallProcessor
    ) -> None:
        import_map: dict[str, str] = {}
        result = processor_with_imports._resolve_class_qn_from_type(
            "proj.models.User", import_map, "proj.service"
        )
        assert result == "proj.models.User"

    def test_fallback_to_local_resolution(
        self, processor_with_imports: CallProcessor
    ) -> None:
        import_map: dict[str, str] = {}
        result = processor_with_imports._resolve_class_qn_from_type(
            "UnknownClass", import_map, "proj.service"
        )
        assert result == ""
