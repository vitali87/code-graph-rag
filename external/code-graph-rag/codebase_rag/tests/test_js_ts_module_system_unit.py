from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.js_ts.module_system import JsTsModuleSystemMixin
from codebase_rag.tests.conftest import create_mock_node


class ConcreteModuleSystemMixin(JsTsModuleSystemMixin):
    def __init__(
        self,
        ingestor: MagicMock,
        import_processor: MagicMock,
        function_registry: MagicMock,
        simple_name_lookup: defaultdict[str, set[str]],
    ) -> None:
        super().__init__()
        self.ingestor = ingestor
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.repo_path = Path("/test/repo")
        self.project_name = "test_project"
        self._get_docstring = MagicMock(return_value=None)
        self._is_export_inside_function = MagicMock(return_value=False)


@pytest.fixture
def mock_ingestor() -> MagicMock:
    ingestor = MagicMock()
    ingestor.ensure_node_batch = MagicMock()
    ingestor.ensure_relationship_batch = MagicMock()
    return ingestor


@pytest.fixture
def mock_import_processor() -> MagicMock:
    processor = MagicMock()
    processor._resolve_js_module_path = MagicMock(return_value="resolved.module")
    return processor


@pytest.fixture
def mock_function_registry() -> MagicMock:
    registry = MagicMock()
    registry.__contains__ = MagicMock(return_value=False)
    registry.__setitem__ = MagicMock()
    return registry


@pytest.fixture
def mixin(
    mock_ingestor: MagicMock,
    mock_import_processor: MagicMock,
    mock_function_registry: MagicMock,
) -> ConcreteModuleSystemMixin:
    return ConcreteModuleSystemMixin(
        ingestor=mock_ingestor,
        import_processor=mock_import_processor,
        function_registry=mock_function_registry,
        simple_name_lookup=defaultdict(set),
    )


@pytest.fixture
def mock_language_queries() -> dict[cs.SupportedLanguage, MagicMock]:
    mock_lang = MagicMock()
    return {
        cs.SupportedLanguage.JS: {cs.QUERY_LANGUAGE: mock_lang},
        cs.SupportedLanguage.TS: {cs.QUERY_LANGUAGE: mock_lang},
    }


class TestProcessCommonjsImport:
    def test_creates_module_node_and_relationship(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_ingestor: MagicMock,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor._resolve_js_module_path.return_value = "fs"

        mixin._process_commonjs_import("readFile", "fs", "my_module")

        mock_ingestor.ensure_node_batch.assert_called_once()
        call_args = mock_ingestor.ensure_node_batch.call_args
        assert call_args[0][0] == cs.NodeLabel.MODULE
        assert call_args[0][1][cs.KEY_QUALIFIED_NAME] == "fs"

        mock_ingestor.ensure_relationship_batch.assert_called_once()
        rel_args = mock_ingestor.ensure_relationship_batch.call_args
        assert rel_args[0][1] == cs.RelationshipType.IMPORTS

    def test_skips_duplicate_imports(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_ingestor: MagicMock,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor._resolve_js_module_path.return_value = "fs"

        mixin._process_commonjs_import("readFile", "fs", "my_module")
        mixin._process_commonjs_import("writeFile", "fs", "my_module")

        assert mock_ingestor.ensure_node_batch.call_count == 1

    def test_handles_resolution_error_gracefully(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_ingestor: MagicMock,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor._resolve_js_module_path.side_effect = Exception(
            "Resolution failed"
        )

        mixin._process_commonjs_import("readFile", "fs", "my_module")

        mock_ingestor.ensure_node_batch.assert_not_called()


class TestProcessVariableDeclaratorForCommonjs:
    def test_processes_simple_destructuring(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor._resolve_js_module_path.return_value = "fs"

        shorthand_id = create_mock_node(
            cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN, "readFile"
        )
        object_pattern = create_mock_node(cs.TS_OBJECT_PATTERN, children=[shorthand_id])

        module_string = create_mock_node(cs.TS_STRING, "'fs'")
        arguments = create_mock_node(cs.TS_ARGUMENTS, children=[module_string])
        require_id = create_mock_node(cs.TS_IDENTIFIER, cs.JS_REQUIRE_KEYWORD)
        call_expr = create_mock_node(
            cs.TS_CALL_EXPRESSION,
            fields={
                cs.FIELD_FUNCTION: require_id,
                cs.TS_FIELD_ARGUMENTS: arguments,
            },
        )

        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={
                cs.FIELD_NAME: object_pattern,
                cs.FIELD_VALUE: call_expr,
            },
        )

        mixin._process_variable_declarator_for_commonjs(declarator, "test_module")

        mock_import_processor._resolve_js_module_path.assert_called_once()

    def test_processes_aliased_destructuring(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor._resolve_js_module_path.return_value = "fs"

        key_node = create_mock_node(cs.TS_PROPERTY_IDENTIFIER, "readFile")
        value_node = create_mock_node(cs.TS_IDENTIFIER, "rf")
        pair_pattern = create_mock_node(
            cs.TS_PAIR_PATTERN,
            fields={
                cs.FIELD_KEY: key_node,
                cs.FIELD_VALUE: value_node,
            },
        )
        object_pattern = create_mock_node(cs.TS_OBJECT_PATTERN, children=[pair_pattern])

        module_string = create_mock_node(cs.TS_STRING, "'fs'")
        arguments = create_mock_node(cs.TS_ARGUMENTS, children=[module_string])
        require_id = create_mock_node(cs.TS_IDENTIFIER, cs.JS_REQUIRE_KEYWORD)
        call_expr = create_mock_node(
            cs.TS_CALL_EXPRESSION,
            fields={
                cs.FIELD_FUNCTION: require_id,
                cs.TS_FIELD_ARGUMENTS: arguments,
            },
        )

        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={
                cs.FIELD_NAME: object_pattern,
                cs.FIELD_VALUE: call_expr,
            },
        )

        mixin._process_variable_declarator_for_commonjs(declarator, "test_module")

        mock_import_processor._resolve_js_module_path.assert_called_once()

    def test_skips_non_object_pattern_name(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_import_processor: MagicMock,
    ) -> None:
        name_node = create_mock_node(cs.TS_IDENTIFIER, "fs")
        call_expr = create_mock_node(cs.TS_CALL_EXPRESSION)
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={
                cs.FIELD_NAME: name_node,
                cs.FIELD_VALUE: call_expr,
            },
        )

        mixin._process_variable_declarator_for_commonjs(declarator, "test_module")

        mock_import_processor._resolve_js_module_path.assert_not_called()

    def test_skips_non_require_call(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_import_processor: MagicMock,
    ) -> None:
        object_pattern = create_mock_node(cs.TS_OBJECT_PATTERN, children=[])
        import_id = create_mock_node(cs.TS_IDENTIFIER, "import")
        call_expr = create_mock_node(
            cs.TS_CALL_EXPRESSION,
            fields={cs.FIELD_FUNCTION: import_id},
        )
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={
                cs.FIELD_NAME: object_pattern,
                cs.FIELD_VALUE: call_expr,
            },
        )

        mixin._process_variable_declarator_for_commonjs(declarator, "test_module")

        mock_import_processor._resolve_js_module_path.assert_not_called()

    def test_skips_empty_object_pattern(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_import_processor: MagicMock,
    ) -> None:
        object_pattern = create_mock_node(cs.TS_OBJECT_PATTERN, children=[])

        module_string = create_mock_node(cs.TS_STRING, "'fs'")
        arguments = create_mock_node(cs.TS_ARGUMENTS, children=[module_string])
        require_id = create_mock_node(cs.TS_IDENTIFIER, cs.JS_REQUIRE_KEYWORD)
        call_expr = create_mock_node(
            cs.TS_CALL_EXPRESSION,
            fields={
                cs.FIELD_FUNCTION: require_id,
                cs.TS_FIELD_ARGUMENTS: arguments,
            },
        )

        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={
                cs.FIELD_NAME: object_pattern,
                cs.FIELD_VALUE: call_expr,
            },
        )

        mixin._process_variable_declarator_for_commonjs(declarator, "test_module")

        mock_import_processor._resolve_js_module_path.assert_not_called()


class TestIngestMissingImportPatterns:
    def test_skips_non_js_ts_languages(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_language_queries: dict[cs.SupportedLanguage, MagicMock],
    ) -> None:
        mixin._ingest_missing_import_patterns(
            MagicMock(),
            "test_module",
            cs.SupportedLanguage.PYTHON,
            mock_language_queries,
        )

    def test_skips_when_no_language_obj(
        self,
        mixin: ConcreteModuleSystemMixin,
    ) -> None:
        queries: dict[cs.SupportedLanguage, dict[str, MagicMock | None]] = {
            cs.SupportedLanguage.JS: {cs.QUERY_LANGUAGE: None}
        }
        mixin._ingest_missing_import_patterns(
            MagicMock(),
            "test_module",
            cs.SupportedLanguage.JS,
            queries,
        )


class TestIngestCommonjsExports:
    def test_skips_non_js_ts_languages(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_language_queries: dict[cs.SupportedLanguage, MagicMock],
    ) -> None:
        mixin._ingest_commonjs_exports(
            MagicMock(),
            "test_module",
            cs.SupportedLanguage.PYTHON,
            mock_language_queries,
        )

    def test_skips_when_no_language_obj(
        self,
        mixin: ConcreteModuleSystemMixin,
    ) -> None:
        queries: dict[cs.SupportedLanguage, dict[str, MagicMock | None]] = {
            cs.SupportedLanguage.JS: {cs.QUERY_LANGUAGE: None}
        }
        mixin._ingest_commonjs_exports(
            MagicMock(),
            "test_module",
            cs.SupportedLanguage.JS,
            queries,
        )


class TestIngestEs6Exports:
    def test_handles_query_errors_gracefully(
        self,
        mixin: ConcreteModuleSystemMixin,
    ) -> None:
        mock_lang = MagicMock()
        queries: dict[cs.SupportedLanguage, dict[str, MagicMock]] = {
            cs.SupportedLanguage.JS: {cs.QUERY_LANGUAGE: mock_lang}
        }

        mixin._ingest_es6_exports(
            MagicMock(),
            "test_module",
            cs.SupportedLanguage.JS,
            queries,
        )


class TestEdgeCases:
    def test_missing_name_field_in_declarator(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_import_processor: MagicMock,
    ) -> None:
        call_expr = create_mock_node(cs.TS_CALL_EXPRESSION)
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_VALUE: call_expr},
        )

        mixin._process_variable_declarator_for_commonjs(declarator, "test_module")

        mock_import_processor._resolve_js_module_path.assert_not_called()

    def test_missing_value_field_in_declarator(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_import_processor: MagicMock,
    ) -> None:
        object_pattern = create_mock_node(cs.TS_OBJECT_PATTERN)
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: object_pattern},
        )

        mixin._process_variable_declarator_for_commonjs(declarator, "test_module")

        mock_import_processor._resolve_js_module_path.assert_not_called()

    def test_missing_function_field_in_call_expression(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_import_processor: MagicMock,
    ) -> None:
        object_pattern = create_mock_node(cs.TS_OBJECT_PATTERN)
        call_expr = create_mock_node(cs.TS_CALL_EXPRESSION, fields={})
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={
                cs.FIELD_NAME: object_pattern,
                cs.FIELD_VALUE: call_expr,
            },
        )

        mixin._process_variable_declarator_for_commonjs(declarator, "test_module")

        mock_import_processor._resolve_js_module_path.assert_not_called()

    def test_missing_arguments_in_require_call(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_import_processor: MagicMock,
    ) -> None:
        object_pattern = create_mock_node(cs.TS_OBJECT_PATTERN, children=[])
        require_id = create_mock_node(cs.TS_IDENTIFIER, cs.JS_REQUIRE_KEYWORD)
        call_expr = create_mock_node(
            cs.TS_CALL_EXPRESSION,
            fields={cs.FIELD_FUNCTION: require_id},
        )
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={
                cs.FIELD_NAME: object_pattern,
                cs.FIELD_VALUE: call_expr,
            },
        )

        mixin._process_variable_declarator_for_commonjs(declarator, "test_module")

        mock_import_processor._resolve_js_module_path.assert_not_called()

    def test_non_string_module_argument(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_import_processor: MagicMock,
    ) -> None:
        shorthand_id = create_mock_node(
            cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN, "readFile"
        )
        object_pattern = create_mock_node(cs.TS_OBJECT_PATTERN, children=[shorthand_id])

        identifier_arg = create_mock_node(cs.TS_IDENTIFIER, "moduleName")
        arguments = create_mock_node(cs.TS_ARGUMENTS, children=[identifier_arg])
        require_id = create_mock_node(cs.TS_IDENTIFIER, cs.JS_REQUIRE_KEYWORD)
        call_expr = create_mock_node(
            cs.TS_CALL_EXPRESSION,
            fields={
                cs.FIELD_FUNCTION: require_id,
                cs.TS_FIELD_ARGUMENTS: arguments,
            },
        )

        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={
                cs.FIELD_NAME: object_pattern,
                cs.FIELD_VALUE: call_expr,
            },
        )

        mixin._process_variable_declarator_for_commonjs(declarator, "test_module")

        mock_import_processor._resolve_js_module_path.assert_not_called()

    def test_pair_pattern_with_wrong_key_type(
        self,
        mixin: ConcreteModuleSystemMixin,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor._resolve_js_module_path.return_value = "fs"

        key_node = create_mock_node(cs.TS_STRING, "'readFile'")
        value_node = create_mock_node(cs.TS_IDENTIFIER, "rf")
        pair_pattern = create_mock_node(
            cs.TS_PAIR_PATTERN,
            fields={
                cs.FIELD_KEY: key_node,
                cs.FIELD_VALUE: value_node,
            },
        )
        object_pattern = create_mock_node(cs.TS_OBJECT_PATTERN, children=[pair_pattern])

        module_string = create_mock_node(cs.TS_STRING, "'fs'")
        arguments = create_mock_node(cs.TS_ARGUMENTS, children=[module_string])
        require_id = create_mock_node(cs.TS_IDENTIFIER, cs.JS_REQUIRE_KEYWORD)
        call_expr = create_mock_node(
            cs.TS_CALL_EXPRESSION,
            fields={
                cs.FIELD_FUNCTION: require_id,
                cs.TS_FIELD_ARGUMENTS: arguments,
            },
        )

        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={
                cs.FIELD_NAME: object_pattern,
                cs.FIELD_VALUE: call_expr,
            },
        )

        mixin._process_variable_declarator_for_commonjs(declarator, "test_module")

        mock_import_processor._resolve_js_module_path.assert_not_called()
