from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.definition_facts import DefinitionFacts
from codebase_rag.parsers.js_ts.module_system import JsTsModuleSystem
from codebase_rag.tests.conftest import create_mock_node


@pytest.fixture
def mock_ingestor() -> MagicMock:
    ingestor = MagicMock()
    ingestor.ensure_node_batch = MagicMock()
    ingestor.ensure_relationship_batch = MagicMock()
    return ingestor


@pytest.fixture
def mock_import_processor() -> MagicMock:
    processor = MagicMock()
    processor.resolve_js_module_path = MagicMock(return_value="resolved.module")
    return processor


@pytest.fixture
def mock_function_registry() -> MagicMock:
    registry = MagicMock()
    registry.__contains__ = MagicMock(return_value=False)
    registry.__setitem__ = MagicMock()
    return registry


@pytest.fixture
def module_system(
    mock_ingestor: MagicMock,
    mock_import_processor: MagicMock,
    mock_function_registry: MagicMock,
) -> JsTsModuleSystem:
    host = MagicMock()
    host._get_docstring.return_value = None
    function_ingest = MagicMock()
    function_ingest.span_claimed_for_qn.return_value = False
    return JsTsModuleSystem(
        host=host,
        function_ingest=function_ingest,
        ingestor=mock_ingestor,
        repo_path=Path("/test/repo"),
        function_registry=mock_function_registry,
        simple_name_lookup=defaultdict(set),
        module_qn_to_file_path={},
        import_processor=mock_import_processor,
        facts=DefinitionFacts(),
    )


@pytest.fixture
def mock_language_queries() -> dict[cs.SupportedLanguage, MagicMock]:
    mock_lang = MagicMock()
    return {
        cs.SupportedLanguage.JS: {cs.QUERY_LANGUAGE: mock_lang},
        cs.SupportedLanguage.TS: {cs.QUERY_LANGUAGE: mock_lang},
    }


class TestProcessCommonjsImport:
    # The CommonJS fallback no longer emits IMPORTS edges directly; every
    # edge is deferred to the import processor so internal targets verify
    # against the real module qns before emission (issue #652).

    def test_defers_external_import_edge(
        self,
        module_system: JsTsModuleSystem,
        mock_ingestor: MagicMock,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.resolve_js_module_path.return_value = "fs"

        module_system._process_commonjs_import(
            "readFile", "fs", "my_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.defer_import_edge.assert_called_once_with(
            "my_module", "fs", cs.SupportedLanguage.JS
        )
        mock_ingestor.ensure_node_batch.assert_not_called()
        mock_ingestor.ensure_relationship_batch.assert_not_called()

    def test_defers_local_import_edge(
        self,
        module_system: JsTsModuleSystem,
        mock_ingestor: MagicMock,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.resolve_js_module_path.return_value = (
            "test_project.src.utils"
        )

        module_system._process_commonjs_import(
            "helper", "./src/utils", "my_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.defer_import_edge.assert_called_once_with(
            "my_module", "test_project.src.utils", cs.SupportedLanguage.JS
        )
        mock_ingestor.ensure_node_batch.assert_not_called()
        mock_ingestor.ensure_relationship_batch.assert_not_called()

    def test_skips_duplicate_imports(
        self,
        module_system: JsTsModuleSystem,
        mock_ingestor: MagicMock,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.resolve_js_module_path.return_value = "fs"

        module_system._process_commonjs_import(
            "readFile", "fs", "my_module", cs.SupportedLanguage.JS
        )
        module_system._process_commonjs_import(
            "writeFile", "fs", "my_module", cs.SupportedLanguage.JS
        )

        assert mock_import_processor.defer_import_edge.call_count == 1

    def test_a_resolution_error_reaches_the_pass_boundary(
        self,
        module_system: JsTsModuleSystem,
        mock_ingestor: MagicMock,
        mock_import_processor: MagicMock,
    ) -> None:
        # Caught once, in `ingest_missing_import_patterns`, and reported
        # there; this helper used to swallow it at debug level as well.
        mock_import_processor.resolve_js_module_path.side_effect = Exception(
            "Resolution failed"
        )

        with pytest.raises(Exception, match="Resolution failed"):
            module_system._process_commonjs_import(
                "readFile", "fs", "my_module", cs.SupportedLanguage.JS
            )

        mock_ingestor.ensure_node_batch.assert_not_called()
        mock_import_processor.defer_import_edge.assert_not_called()


class TestProcessVariableDeclaratorForCommonjs:
    def test_processes_simple_destructuring(
        self,
        module_system: JsTsModuleSystem,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.resolve_js_module_path.return_value = "fs"

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

        module_system._process_variable_declarator_for_commonjs(
            declarator, "test_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.resolve_js_module_path.assert_called_once()

    def test_processes_aliased_destructuring(
        self,
        module_system: JsTsModuleSystem,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.resolve_js_module_path.return_value = "fs"

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

        module_system._process_variable_declarator_for_commonjs(
            declarator, "test_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.resolve_js_module_path.assert_called_once()

    def test_skips_non_object_pattern_name(
        self,
        module_system: JsTsModuleSystem,
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

        module_system._process_variable_declarator_for_commonjs(
            declarator, "test_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.resolve_js_module_path.assert_not_called()

    def test_skips_non_require_call(
        self,
        module_system: JsTsModuleSystem,
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

        module_system._process_variable_declarator_for_commonjs(
            declarator, "test_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.resolve_js_module_path.assert_not_called()

    def test_skips_empty_object_pattern(
        self,
        module_system: JsTsModuleSystem,
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

        module_system._process_variable_declarator_for_commonjs(
            declarator, "test_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.resolve_js_module_path.assert_not_called()


class TestIngestMissingImportPatterns:
    def test_skips_non_js_ts_languages(
        self,
        module_system: JsTsModuleSystem,
        mock_language_queries: dict[cs.SupportedLanguage, MagicMock],
    ) -> None:
        module_system.ingest_missing_import_patterns(
            MagicMock(),
            "test_module",
            cs.SupportedLanguage.PYTHON,
            mock_language_queries,
        )

    def test_skips_when_no_language_obj(
        self,
        module_system: JsTsModuleSystem,
    ) -> None:
        queries: dict[cs.SupportedLanguage, dict[str, MagicMock | None]] = {
            cs.SupportedLanguage.JS: {cs.QUERY_LANGUAGE: None}
        }
        module_system.ingest_missing_import_patterns(
            MagicMock(),
            "test_module",
            cs.SupportedLanguage.JS,
            queries,
        )


class TestIngestCommonjsExports:
    def test_skips_non_js_ts_languages(
        self,
        module_system: JsTsModuleSystem,
        mock_language_queries: dict[cs.SupportedLanguage, MagicMock],
    ) -> None:
        module_system.ingest_commonjs_exports(
            MagicMock(),
            "test_module",
            cs.SupportedLanguage.PYTHON,
            mock_language_queries,
        )

    def test_skips_when_no_language_obj(
        self,
        module_system: JsTsModuleSystem,
    ) -> None:
        queries: dict[cs.SupportedLanguage, dict[str, MagicMock | None]] = {
            cs.SupportedLanguage.JS: {cs.QUERY_LANGUAGE: None}
        }
        module_system.ingest_commonjs_exports(
            MagicMock(),
            "test_module",
            cs.SupportedLanguage.JS,
            queries,
        )


class TestIngestEs6Exports:
    def test_handles_query_errors_gracefully(
        self,
        module_system: JsTsModuleSystem,
    ) -> None:
        mock_lang = MagicMock()
        queries: dict[cs.SupportedLanguage, dict[str, MagicMock]] = {
            cs.SupportedLanguage.JS: {cs.QUERY_LANGUAGE: mock_lang}
        }

        module_system.ingest_es6_exports(
            MagicMock(),
            "test_module",
            cs.SupportedLanguage.JS,
            queries,
        )


class TestEdgeCases:
    def test_missing_name_field_in_declarator(
        self,
        module_system: JsTsModuleSystem,
        mock_import_processor: MagicMock,
    ) -> None:
        call_expr = create_mock_node(cs.TS_CALL_EXPRESSION)
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_VALUE: call_expr},
        )

        module_system._process_variable_declarator_for_commonjs(
            declarator, "test_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.resolve_js_module_path.assert_not_called()

    def test_missing_value_field_in_declarator(
        self,
        module_system: JsTsModuleSystem,
        mock_import_processor: MagicMock,
    ) -> None:
        object_pattern = create_mock_node(cs.TS_OBJECT_PATTERN)
        declarator = create_mock_node(
            cs.TS_VARIABLE_DECLARATOR,
            fields={cs.FIELD_NAME: object_pattern},
        )

        module_system._process_variable_declarator_for_commonjs(
            declarator, "test_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.resolve_js_module_path.assert_not_called()

    def test_missing_function_field_in_call_expression(
        self,
        module_system: JsTsModuleSystem,
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

        module_system._process_variable_declarator_for_commonjs(
            declarator, "test_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.resolve_js_module_path.assert_not_called()

    def test_missing_arguments_in_require_call(
        self,
        module_system: JsTsModuleSystem,
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

        module_system._process_variable_declarator_for_commonjs(
            declarator, "test_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.resolve_js_module_path.assert_not_called()

    def test_non_string_module_argument(
        self,
        module_system: JsTsModuleSystem,
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

        module_system._process_variable_declarator_for_commonjs(
            declarator, "test_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.resolve_js_module_path.assert_not_called()

    def test_pair_pattern_with_wrong_key_type(
        self,
        module_system: JsTsModuleSystem,
        mock_import_processor: MagicMock,
    ) -> None:
        mock_import_processor.resolve_js_module_path.return_value = "fs"

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

        module_system._process_variable_declarator_for_commonjs(
            declarator, "test_module", cs.SupportedLanguage.JS
        )

        mock_import_processor.resolve_js_module_path.assert_not_called()
