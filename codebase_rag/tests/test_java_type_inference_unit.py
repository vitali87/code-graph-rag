from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.java_type_inference import JavaTypeInferenceEngine
from codebase_rag.types_defs import NodeType


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
def type_inference_engine(
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


class TestJavaTypeResolverMixin:
    def test_resolve_java_type_name_primitive_types(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        assert (
            type_inference_engine._resolve_java_type_name("int", "com.example") == "int"
        )
        assert (
            type_inference_engine._resolve_java_type_name("boolean", "com.example")
            == "boolean"
        )
        assert (
            type_inference_engine._resolve_java_type_name("double", "com.example")
            == "double"
        )
        assert (
            type_inference_engine._resolve_java_type_name("long", "com.example")
            == "long"
        )
        assert (
            type_inference_engine._resolve_java_type_name("float", "com.example")
            == "float"
        )
        assert (
            type_inference_engine._resolve_java_type_name("byte", "com.example")
            == "byte"
        )
        assert (
            type_inference_engine._resolve_java_type_name("short", "com.example")
            == "short"
        )
        assert (
            type_inference_engine._resolve_java_type_name("char", "com.example")
            == "char"
        )

    def test_resolve_java_type_name_wrapper_types(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        assert (
            type_inference_engine._resolve_java_type_name("Integer", "com.example")
            == "java.lang.Integer"
        )
        assert (
            type_inference_engine._resolve_java_type_name("Boolean", "com.example")
            == "java.lang.Boolean"
        )
        assert (
            type_inference_engine._resolve_java_type_name("String", "com.example")
            == "java.lang.String"
        )
        assert (
            type_inference_engine._resolve_java_type_name("Double", "com.example")
            == "java.lang.Double"
        )

    def test_resolve_java_type_name_fully_qualified(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        assert (
            type_inference_engine._resolve_java_type_name(
                "java.util.List", "com.example"
            )
            == "java.util.List"
        )
        assert (
            type_inference_engine._resolve_java_type_name(
                "com.example.MyClass", "other.pkg"
            )
            == "com.example.MyClass"
        )

    def test_resolve_java_type_name_array_types(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        assert (
            type_inference_engine._resolve_java_type_name("int[]", "com.example")
            == "int[]"
        )
        assert (
            type_inference_engine._resolve_java_type_name("String[]", "com.example")
            == "java.lang.String[]"
        )

    def test_resolve_java_type_name_generic_types(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        result = type_inference_engine._resolve_java_type_name(
            "List<String>", "com.example"
        )
        assert result == "List"

        result = type_inference_engine._resolve_java_type_name(
            "Map<String, Integer>", "com.example"
        )
        assert result == "Map"

    def test_resolve_java_type_name_from_import_mapping(
        self,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {
            "com.example": {"List": "java.util.List", "MyClass": "com.other.MyClass"}
        }
        assert (
            type_inference_engine._resolve_java_type_name("List", "com.example")
            == "java.util.List"
        )
        assert (
            type_inference_engine._resolve_java_type_name("MyClass", "com.example")
            == "com.other.MyClass"
        )

    def test_resolve_java_type_name_empty_returns_object(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        assert (
            type_inference_engine._resolve_java_type_name("", "com.example") == "Object"
        )

    def test_resolve_java_type_name_same_package_class(
        self,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "com.example.Helper"
        )
        mock_function_registry.__getitem__ = MagicMock(return_value=NodeType.CLASS)

        result = type_inference_engine._resolve_java_type_name("Helper", "com.example")
        assert result == "com.example.Helper"

    def test_module_qn_to_java_fqn(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        type_inference_engine.module_qn_to_file_path = {
            "project.src.main.java.com.example.Helper": Path("/test/Helper.java")
        }
        type_inference_engine._fqn_to_module_qn = (
            type_inference_engine._build_fqn_lookup_map()
        )

        result = type_inference_engine._module_qn_to_java_fqn(
            "project.src.main.java.com.example.Helper"
        )
        assert result == "com.example.Helper"

    def test_calculate_module_distance(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        distance = type_inference_engine._calculate_module_distance(
            "com.example.utils.Helper", "com.example.service.UserService"
        )
        assert distance >= 0

        same_distance = type_inference_engine._calculate_module_distance(
            "com.example.Helper", "com.example.Service"
        )
        assert same_distance < distance

    def test_rank_module_candidates_empty(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        result = type_inference_engine._rank_module_candidates(
            [], "Helper", "com.example"
        )
        assert result == []

    def test_rank_module_candidates_no_current_module(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        candidates = ["com.a.Helper", "com.b.Helper"]
        result = type_inference_engine._rank_module_candidates(
            candidates, "Helper", None
        )
        assert result == candidates

    def test_find_registry_entries_under_with_prefix(
        self,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.find_with_prefix = MagicMock(
            return_value=[
                ("com.example.Helper.method1", NodeType.METHOD),
                ("com.example.Helper.method2", NodeType.METHOD),
            ]
        )

        result = list(
            type_inference_engine._find_registry_entries_under("com.example.Helper")
        )
        assert len(result) == 2

    def test_find_registry_entries_under_fallback_to_items(
        self,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.find_with_prefix = MagicMock(return_value=[])
        mock_function_registry.items = MagicMock(
            return_value=[
                ("com.example.Helper.method1", NodeType.METHOD),
                ("com.example.Helper.method2", NodeType.METHOD),
                ("com.example.Other.method3", NodeType.METHOD),
            ]
        )

        result = list(
            type_inference_engine._find_registry_entries_under("com.example.Helper")
        )
        assert len(result) == 2


class TestJavaMethodResolverMixin:
    def test_resolve_java_object_type_from_local_vars(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        local_var_types = {"user": "com.example.User", "name": "java.lang.String"}

        result = type_inference_engine._resolve_java_object_type(
            "user", local_var_types, "com.example"
        )
        assert result == "com.example.User"

        result = type_inference_engine._resolve_java_object_type(
            "name", local_var_types, "com.example"
        )
        assert result == "java.lang.String"

    def test_resolve_java_object_type_this_reference(
        self,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.find_with_prefix = MagicMock(
            return_value=[("com.example.MyClass", NodeType.CLASS)]
        )

        result = type_inference_engine._resolve_java_object_type(
            "this", {}, "com.example"
        )
        assert result == "com.example.MyClass"

    def test_resolve_java_object_type_super_reference(
        self,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.find_with_prefix = MagicMock(
            return_value=[("com.example.ChildClass", NodeType.CLASS)]
        )
        type_inference_engine.class_inheritance = {
            "com.example.ChildClass": ["com.example.ParentClass"]
        }

        result = type_inference_engine._resolve_java_object_type(
            "super", {}, "com.example"
        )
        assert result == "com.example.ParentClass"

    def test_resolve_java_object_type_from_import(
        self,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {
            "com.example": {"Helper": "com.utils.Helper"}
        }

        result = type_inference_engine._resolve_java_object_type(
            "Helper", {}, "com.example"
        )
        assert result == "com.utils.Helper"

    def test_resolve_java_object_type_same_package_class(
        self,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "com.example.Utils"
        )
        mock_function_registry.__getitem__ = MagicMock(return_value=NodeType.CLASS)
        mock_function_registry.find_with_prefix = MagicMock(return_value=[])

        result = type_inference_engine._resolve_java_object_type(
            "Utils", {}, "com.example"
        )
        assert result == "com.example.Utils"

    def test_resolve_java_object_type_unknown(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        result = type_inference_engine._resolve_java_object_type(
            "unknownVar", {}, "com.example"
        )
        assert result is None

    def test_find_parent_class(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        type_inference_engine.class_inheritance = {
            "com.example.Child": ["com.example.Parent", "java.io.Serializable"]
        }

        result = type_inference_engine._find_parent_class("com.example.Child")
        assert result == "com.example.Parent"

    def test_find_parent_class_not_found(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        result = type_inference_engine._find_parent_class("com.example.NoParent")
        assert result is None

    def test_resolve_static_or_local_method(
        self,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.find_with_prefix = MagicMock(
            return_value=[
                ("com.example.MyClass.helperMethod(String)", NodeType.METHOD),
                ("com.example.MyClass.otherMethod()", NodeType.METHOD),
            ]
        )

        result = type_inference_engine._resolve_static_or_local_method(
            "helperMethod", "com.example"
        )
        assert result is not None
        assert result[0] == NodeType.METHOD

    def test_resolve_static_or_local_method_not_found(
        self,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.find_with_prefix = MagicMock(return_value=[])

        result = type_inference_engine._resolve_static_or_local_method(
            "nonExistent", "com.example"
        )
        assert result is None

    def test_is_matching_method(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        assert type_inference_engine._is_matching_method("process", "process") is True
        assert type_inference_engine._is_matching_method("process()", "process") is True
        assert (
            type_inference_engine._is_matching_method("process(String)", "process")
            is True
        )
        assert (
            type_inference_engine._is_matching_method("processData", "process") is False
        )
        assert type_inference_engine._is_matching_method("other", "process") is False

    def test_heuristic_method_return_type_getter(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        assert (
            type_inference_engine._heuristic_method_return_type("getName")
            == "java.lang.String"
        )
        assert (
            type_inference_engine._heuristic_method_return_type("getId")
            == "java.lang.Long"
        )
        assert type_inference_engine._heuristic_method_return_type("getSize") == "int"
        assert type_inference_engine._heuristic_method_return_type("getLength") == "int"

    def test_heuristic_method_return_type_boolean(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        assert (
            type_inference_engine._heuristic_method_return_type("isValid") == "boolean"
        )
        assert (
            type_inference_engine._heuristic_method_return_type("hasPermission")
            == "boolean"
        )
        assert (
            type_inference_engine._heuristic_method_return_type("isEmpty") == "boolean"
        )

    def test_heuristic_method_return_type_create_pattern(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        assert (
            type_inference_engine._heuristic_method_return_type("factory.createUser")
            == "User"
        )
        assert (
            type_inference_engine._heuristic_method_return_type("factory.createOrder")
            == "Order"
        )

    def test_heuristic_method_return_type_unknown(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        assert (
            type_inference_engine._heuristic_method_return_type("doSomething") is None
        )
        assert type_inference_engine._heuristic_method_return_type("process") is None

    def test_collect_candidate_modules(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        type_inference_engine._fqn_to_module_qn = {
            "Helper": ["com.example.Helper", "com.utils.Helper"],
            "com.example.Helper": ["com.example.Helper"],
        }

        result = type_inference_engine._collect_candidate_modules(
            ["Helper", "com.example.Helper"]
        )
        assert "com.example.Helper" in result
        assert "com.utils.Helper" in result


class TestJavaVariableAnalyzerMixin:
    def test_lookup_variable_type_caching(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        type_inference_engine._lookup_cache["com.example:testVar"] = "java.lang.String"

        result = type_inference_engine._lookup_variable_type("testVar", "com.example")
        assert result == "java.lang.String"

    def test_lookup_variable_type_cycle_detection(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        type_inference_engine._lookup_in_progress.add("com.example:cyclicVar")

        result = type_inference_engine._lookup_variable_type("cyclicVar", "com.example")
        assert result is None

    def test_lookup_variable_type_empty_var_name(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        result = type_inference_engine._lookup_variable_type("", "com.example")
        assert result is None

    def test_lookup_variable_type_empty_module(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        result = type_inference_engine._lookup_variable_type("testVar", "")
        assert result is None


class TestJavaTypeInferenceEngineIntegration:
    def test_build_fqn_lookup_map(
        self, type_inference_engine: JavaTypeInferenceEngine
    ) -> None:
        type_inference_engine.module_qn_to_file_path = {
            "project.src.main.java.com.example.Helper": Path("/test/Helper.java"),
            "project.src.main.java.com.example.utils.StringUtils": Path(
                "/test/StringUtils.java"
            ),
        }

        fqn_map = type_inference_engine._build_fqn_lookup_map()

        assert "Helper" in fqn_map or "com.example.Helper" in fqn_map
