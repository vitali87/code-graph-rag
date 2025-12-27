from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.py.type_inference import PythonTypeInferenceEngine
from codebase_rag.tests.conftest import create_mock_node
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
    registry.get = MagicMock(return_value=None)
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
) -> PythonTypeInferenceEngine:
    return PythonTypeInferenceEngine(
        import_processor=mock_import_processor,
        function_registry=mock_function_registry,
        repo_path=Path("/test/repo"),
        project_name="test_project",
        ast_cache=mock_ast_cache,
        queries={},
        module_qn_to_file_path={},
        class_inheritance={},
        simple_name_lookup=defaultdict(set),
        js_type_inference_getter=lambda: MagicMock(),
    )


class TestCalculateMatchScore:
    def test_exact_match_returns_100(self, engine: PythonTypeInferenceEngine) -> None:
        score = engine._calculate_match_score("user", "user")
        assert score == cs.PY_SCORE_EXACT_MATCH

    def test_exact_match_case_insensitive(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        score = engine._calculate_match_score("user", "user")
        assert score == cs.PY_SCORE_EXACT_MATCH

    def test_suffix_match_class_ends_with_param(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        score = engine._calculate_match_score("user", "appuser")
        assert score == cs.PY_SCORE_SUFFIX_MATCH

    def test_suffix_match_param_ends_with_class(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        score = engine._calculate_match_score("userservice", "service")
        assert score == cs.PY_SCORE_SUFFIX_MATCH

    def test_contains_match_returns_scaled_score(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        score = engine._calculate_match_score("myuserhandler", "user")
        assert 0 < score < cs.PY_SCORE_SUFFIX_MATCH

    def test_no_match_returns_zero(self, engine: PythonTypeInferenceEngine) -> None:
        score = engine._calculate_match_score("foo", "bar")
        assert score == 0


class TestFindBestClassMatch:
    def test_finds_exact_match(self, engine: PythonTypeInferenceEngine) -> None:
        result = engine._find_best_class_match("User", ["User", "Account", "Service"])
        assert result == "User"

    def test_finds_suffix_match(self, engine: PythonTypeInferenceEngine) -> None:
        result = engine._find_best_class_match("user", ["AppUser", "Account"])
        assert result == "AppUser"

    def test_returns_none_for_no_match(self, engine: PythonTypeInferenceEngine) -> None:
        result = engine._find_best_class_match("xyz", ["Foo", "Bar"])
        assert result is None

    def test_returns_none_for_empty_list(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        result = engine._find_best_class_match("user", [])
        assert result is None

    def test_prefers_exact_over_suffix(self, engine: PythonTypeInferenceEngine) -> None:
        result = engine._find_best_class_match("user", ["AppUser", "User"])
        assert result == "User"


class TestExtractVariableName:
    def test_extracts_identifier(self, engine: PythonTypeInferenceEngine) -> None:
        node = create_mock_node(cs.TS_PY_IDENTIFIER, "my_var")
        result = engine._extract_variable_name(node)
        assert result == "my_var"

    def test_returns_none_for_non_identifier(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        node = create_mock_node("some_other_type", "text")
        result = engine._extract_variable_name(node)
        assert result is None

    def test_returns_none_for_empty_text(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        node = create_mock_node(cs.TS_PY_IDENTIFIER, "")
        result = engine._extract_variable_name(node)
        assert result is None


class TestProcessTypedParameter:
    def test_extracts_typed_parameter(self, engine: PythonTypeInferenceEngine) -> None:
        name_node = create_mock_node(cs.TS_PY_IDENTIFIER, "user_id")
        type_node = create_mock_node(cs.TS_PY_IDENTIFIER, "int")
        param = create_mock_node(
            cs.TS_PY_TYPED_PARAMETER,
            fields={cs.TS_FIELD_TYPE: type_node},
            children=[name_node, type_node],
        )
        local_var_types: dict[str, str] = {}

        engine._process_typed_parameter(param, local_var_types)

        assert local_var_types["user_id"] == "int"

    def test_skips_missing_name(self, engine: PythonTypeInferenceEngine) -> None:
        type_node = create_mock_node(cs.TS_PY_IDENTIFIER, "int")
        param = create_mock_node(
            cs.TS_PY_TYPED_PARAMETER,
            fields={cs.TS_FIELD_TYPE: type_node},
            children=[],
        )
        local_var_types: dict[str, str] = {}

        engine._process_typed_parameter(param, local_var_types)

        assert local_var_types == {}

    def test_skips_missing_type(self, engine: PythonTypeInferenceEngine) -> None:
        name_node = create_mock_node(cs.TS_PY_IDENTIFIER, "user_id")
        param = create_mock_node(
            cs.TS_PY_TYPED_PARAMETER,
            children=[name_node],
        )
        local_var_types: dict[str, str] = {}

        engine._process_typed_parameter(param, local_var_types)

        assert local_var_types == {}


class TestProcessParameter:
    def test_routes_identifier_to_untyped(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        param = create_mock_node(cs.TS_PY_IDENTIFIER, "user")
        local_var_types: dict[str, str] = {}

        engine._process_parameter(param, local_var_types, "test.module")

        assert local_var_types == {}

    def test_routes_typed_parameter(self, engine: PythonTypeInferenceEngine) -> None:
        name_node = create_mock_node(cs.TS_PY_IDENTIFIER, "count")
        type_node = create_mock_node(cs.TS_PY_IDENTIFIER, "int")
        param = create_mock_node(
            cs.TS_PY_TYPED_PARAMETER,
            fields={cs.TS_FIELD_TYPE: type_node},
            children=[name_node, type_node],
        )
        local_var_types: dict[str, str] = {}

        engine._process_parameter(param, local_var_types, "test.module")

        assert local_var_types["count"] == "int"

    def test_routes_typed_default_parameter(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        name_node = create_mock_node(cs.TS_PY_IDENTIFIER, "count")
        type_node = create_mock_node(cs.TS_PY_IDENTIFIER, "int")
        param = create_mock_node(
            cs.TS_PY_TYPED_DEFAULT_PARAMETER,
            fields={cs.TS_FIELD_NAME: name_node, cs.TS_FIELD_TYPE: type_node},
        )
        local_var_types: dict[str, str] = {}

        engine._process_parameter(param, local_var_types, "test.module")

        assert local_var_types["count"] == "int"


class TestInferListElementType:
    def test_extracts_class_from_call_in_list(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        func_node = create_mock_node(cs.TS_PY_IDENTIFIER, "User")
        call_node = create_mock_node(
            cs.TS_PY_CALL,
            fields={cs.TS_FIELD_FUNCTION: func_node},
        )
        list_node = create_mock_node(cs.TS_PY_LIST, children=[call_node])

        result = engine._infer_list_element_type(list_node)

        assert result == "User"

    def test_skips_lowercase_function(self, engine: PythonTypeInferenceEngine) -> None:
        func_node = create_mock_node(cs.TS_PY_IDENTIFIER, "create_user")
        call_node = create_mock_node(
            cs.TS_PY_CALL,
            fields={cs.TS_FIELD_FUNCTION: func_node},
        )
        list_node = create_mock_node(cs.TS_PY_LIST, children=[call_node])

        result = engine._infer_list_element_type(list_node)

        assert result is None

    def test_returns_none_for_empty_list(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        list_node = create_mock_node(cs.TS_PY_LIST, children=[])

        result = engine._infer_list_element_type(list_node)

        assert result is None


class TestAnalyzeForClause:
    def test_extracts_loop_variable_type(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        left_node = create_mock_node(cs.TS_PY_IDENTIFIER, "item")
        func_node = create_mock_node(cs.TS_PY_IDENTIFIER, "Product")
        call_node = create_mock_node(
            cs.TS_PY_CALL,
            fields={cs.TS_FIELD_FUNCTION: func_node},
        )
        right_node = create_mock_node(cs.TS_PY_LIST, children=[call_node])
        for_node = create_mock_node(
            cs.TS_PY_FOR_STATEMENT,
            fields={cs.TS_FIELD_LEFT: left_node, cs.TS_FIELD_RIGHT: right_node},
        )
        local_var_types: dict[str, str] = {}

        engine._analyze_for_clause(for_node, local_var_types, "test.module")

        assert local_var_types.get("item") == "Product"

    def test_skips_missing_left_node(self, engine: PythonTypeInferenceEngine) -> None:
        right_node = create_mock_node(cs.TS_PY_LIST, children=[])
        for_node = create_mock_node(
            cs.TS_PY_FOR_STATEMENT,
            fields={cs.TS_FIELD_RIGHT: right_node},
        )
        local_var_types: dict[str, str] = {}

        engine._analyze_for_clause(for_node, local_var_types, "test.module")

        assert local_var_types == {}


class TestCollectAvailableClasses:
    def test_collects_classes_from_registry(
        self, engine: PythonTypeInferenceEngine, mock_function_registry: MagicMock
    ) -> None:
        mock_function_registry.find_with_prefix.return_value = [
            ("test.module.User", NodeType.CLASS),
            ("test.module.Account", NodeType.CLASS),
            ("test.module.helper", NodeType.FUNCTION),
        ]

        result = engine._collect_available_classes("test.module")

        assert "User" in result
        assert "Account" in result
        assert "helper" not in result

    def test_collects_imported_classes(
        self,
        engine: PythonTypeInferenceEngine,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {
            "test.module": {"User": "other.module.User"}
        }
        mock_function_registry.get.return_value = NodeType.CLASS

        result = engine._collect_available_classes("test.module")

        assert "User" in result

    def test_returns_empty_for_no_classes(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        result = engine._collect_available_classes("empty.module")

        assert result == []


class TestInferMethodReturnElementType:
    def test_returns_none_for_non_collection_name(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        result = engine._infer_method_return_element_type("user", "test.module")
        assert result is None

    def test_matches_all_prefix(self, engine: PythonTypeInferenceEngine) -> None:
        result = engine._infer_method_return_element_type("all_users", "test.module")
        assert result is None

    def test_matches_plural_suffix(self, engine: PythonTypeInferenceEngine) -> None:
        result = engine._infer_method_return_element_type("users", "test.module")
        assert result is None


class TestInferVariableElementType:
    def test_returns_known_type(self, engine: PythonTypeInferenceEngine) -> None:
        local_var_types = {"items": "Product"}

        result = engine._infer_variable_element_type(
            "items", local_var_types, "test.module"
        )

        assert result == "Product"

    def test_skips_list_type(self, engine: PythonTypeInferenceEngine) -> None:
        local_var_types = {"items": cs.TYPE_INFERENCE_LIST}

        result = engine._infer_variable_element_type(
            "items", local_var_types, "test.module"
        )

        assert result is None

    def test_falls_back_to_method_return(
        self, engine: PythonTypeInferenceEngine
    ) -> None:
        result = engine._infer_variable_element_type("users", {}, "test.module")

        assert result is None
