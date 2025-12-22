import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater

JS_AVAILABLE = importlib.util.find_spec("tree_sitter_javascript") is not None


@pytest.fixture
def temp_js_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "js_module_test"
    project_path.mkdir()
    return project_path


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestCommonJSDestructuringImports:
    def test_destructured_require_creates_import_relationship(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "utils.js").write_text(
            """
const { readFile, writeFile } = require('fs');

function processFile(path) {
    return readFile(path);
}
"""
        )

        run_updater(temp_js_project, mock_ingestor)

        rel_calls = mock_ingestor.ensure_relationship_batch.call_args_list
        imports_rels = [call for call in rel_calls if call.args[1] == "IMPORTS"]

        assert len(imports_rels) >= 1

    def test_aliased_destructured_require(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "aliased.js").write_text(
            """
const { readFile: rf, writeFile: wf } = require('fs');

function read(path) {
    return rf(path);
}
"""
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_names = [
            call[0][1].get("name", "") for call in calls if call[0][0] == "Function"
        ]

        assert "read" in function_names


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestCommonJSExports:
    def test_exports_dot_function_is_ingested(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "exports_dot.js").write_text(
            """
exports.myHelper = function() {
    return 'helper';
};

exports.anotherHelper = () => 'arrow helper';
"""
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "exports_dot" in call[0][1].get("qualified_name", "")
        ]

        assert any("myHelper" in qn for qn in function_qns)
        assert any("anotherHelper" in qn for qn in function_qns)

    def test_module_exports_dot_function_is_ingested(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "module_exports.js").write_text(
            """
module.exports.create = function(data) {
    return { ...data, id: Date.now() };
};

module.exports.update = (id, data) => ({ id, ...data });
"""
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "module_exports" in call[0][1].get("qualified_name", "")
        ]

        assert any("create" in qn for qn in function_qns)
        assert any("update" in qn for qn in function_qns)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestES6Exports:
    def test_export_const_arrow_function(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "es6_const.js").write_text(
            """
export const add = (a, b) => a + b;
export const subtract = (a, b) => a - b;
"""
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "es6_const" in call[0][1].get("qualified_name", "")
        ]

        assert any("add" in qn for qn in function_qns)
        assert any("subtract" in qn for qn in function_qns)

    def test_export_function_declaration(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "es6_func.js").write_text(
            """
export function multiply(a, b) {
    return a * b;
}

export function divide(a, b) {
    return a / b;
}
"""
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "es6_func" in call[0][1].get("qualified_name", "")
        ]

        assert any("multiply" in qn for qn in function_qns)
        assert any("divide" in qn for qn in function_qns)

    def test_export_generator_function(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "es6_generator.js").write_text(
            """
export function* range(start, end) {
    for (let i = start; i < end; i++) {
        yield i;
    }
}
"""
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "es6_generator" in call[0][1].get("qualified_name", "")
        ]

        assert any("range" in qn for qn in function_qns)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestMixedModuleSystems:
    def test_file_with_both_commonjs_and_es6_patterns(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "mixed.js").write_text(
            """
const { EventEmitter } = require('events');

export function createEmitter() {
    return new EventEmitter();
}

exports.legacyCreate = function() {
    return new EventEmitter();
};
"""
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "mixed" in call[0][1].get("qualified_name", "")
        ]

        assert any("createEmitter" in qn for qn in function_qns)
        assert any("legacyCreate" in qn for qn in function_qns)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestNestedRequires:
    def test_require_in_function_scope(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "nested_require.js").write_text(
            """
function loadModule(name) {
    const mod = require(name);
    return mod;
}

function process() {
    const { join } = require('path');
    return join('a', 'b');
}
"""
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_names = [
            call[0][1].get("name", "") for call in calls if call[0][0] == "Function"
        ]

        assert "loadModule" in function_names
        assert "process" in function_names
