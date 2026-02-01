import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater

JS_AVAILABLE = importlib.util.find_spec("tree_sitter_javascript") is not None
TS_AVAILABLE = importlib.util.find_spec("tree_sitter_typescript") is not None


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
            encoding="utf-8",
            data="""
const { readFile, writeFile } = require('fs');

function processFile(path) {
    return readFile(path);
}
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        rel_calls = mock_ingestor.ensure_relationship_batch.call_args_list
        imports_rels = [call for call in rel_calls if call.args[1] == "IMPORTS"]

        assert len(imports_rels) >= 1

    def test_aliased_destructured_require(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "aliased.js").write_text(
            encoding="utf-8",
            data="""
const { readFile: rf, writeFile: wf } = require('fs');

function read(path) {
    return rf(path);
}
""",
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
            encoding="utf-8",
            data="""
exports.myHelper = function() {
    return 'helper';
};

exports.anotherHelper = () => 'arrow helper';
""",
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
            encoding="utf-8",
            data="""
module.exports.create = function(data) {
    return { ...data, id: Date.now() };
};

module.exports.update = (id, data) => ({ id, ...data });
""",
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
            encoding="utf-8",
            data="""
export const add = (a, b) => a + b;
export const subtract = (a, b) => a - b;
""",
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
            encoding="utf-8",
            data="""
export function multiply(a, b) {
    return a * b;
}

export function divide(a, b) {
    return a / b;
}
""",
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
            encoding="utf-8",
            data="""
export function* range(start, end) {
    for (let i = start; i < end; i++) {
        yield i;
    }
}
""",
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
            encoding="utf-8",
            data="""
const { EventEmitter } = require('events');

export function createEmitter() {
    return new EventEmitter();
}

exports.legacyCreate = function() {
    return new EventEmitter();
};
""",
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
            encoding="utf-8",
            data="""
function loadModule(name) {
    const mod = require(name);
    return mod;
}

function process() {
    const { join } = require('path');
    return join('a', 'b');
}
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_names = [
            call[0][1].get("name", "") for call in calls if call[0][0] == "Function"
        ]

        assert "loadModule" in function_names
        assert "process" in function_names


@pytest.fixture
def temp_ts_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "ts_module_test"
    project_path.mkdir()
    return project_path


@pytest.mark.skipif(not TS_AVAILABLE, reason="tree-sitter-typescript not available")
class TestTypeScriptModules:
    def test_typescript_export_function(
        self, temp_ts_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_ts_project / "utils.ts").write_text(
            encoding="utf-8",
            data="""
export function greet(name: string): string {
    return `Hello, ${name}!`;
}

export const calculate = (a: number, b: number): number => a + b;
""",
        )

        run_updater(temp_ts_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "utils" in call[0][1].get("qualified_name", "")
        ]

        assert any("greet" in qn for qn in function_qns)
        assert any("calculate" in qn for qn in function_qns)

    def test_typescript_async_export_function(
        self, temp_ts_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_ts_project / "async_utils.ts").write_text(
            encoding="utf-8",
            data="""
export async function fetchData(url: string): Promise<string> {
    return fetch(url).then(r => r.text());
}

export const fetchJson = async <T>(url: string): Promise<T> => {
    const response = await fetch(url);
    return response.json();
};
""",
        )

        run_updater(temp_ts_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "async_utils" in call[0][1].get("qualified_name", "")
        ]

        assert any("fetchData" in qn for qn in function_qns)
        assert any("fetchJson" in qn for qn in function_qns)

    def test_typescript_class_with_methods(
        self, temp_ts_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_ts_project / "service.ts").write_text(
            encoding="utf-8",
            data="""
export class UserService {
    constructor(private db: Database) {}

    async getUser(id: string): Promise<User> {
        return this.db.find(id);
    }

    createUser(data: UserData): User {
        return this.db.create(data);
    }
}
""",
        )

        run_updater(temp_ts_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        class_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Class"
            and "service" in call[0][1].get("qualified_name", "")
        ]

        assert any("UserService" in qn for qn in class_qns)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestMultipleDestructuredImports:
    def test_multiple_destructured_from_same_module(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "multi_import.js").write_text(
            encoding="utf-8",
            data="""
const { readFile, writeFile, appendFile } = require('fs');
const { join, resolve, dirname } = require('path');

function processFiles(files) {
    return files.map(f => join(dirname(f), 'processed'));
}
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        rel_calls = mock_ingestor.ensure_relationship_batch.call_args_list
        imports_rels = [call for call in rel_calls if call.args[1] == "IMPORTS"]

        assert len(imports_rels) >= 2

    def test_mixed_destructured_and_default_require(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "mixed_import.js").write_text(
            encoding="utf-8",
            data="""
const fs = require('fs');
const { join } = require('path');
const { EventEmitter } = require('events');

function readAndJoin(file, dir) {
    return join(dir, fs.readFileSync(file, 'utf8'));
}
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_names = [
            call[0][1].get("name", "") for call in calls if call[0][0] == "Function"
        ]

        assert "readAndJoin" in function_names


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestExportConstFunctionExpression:
    def test_export_const_function_expression(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "func_expr.js").write_text(
            encoding="utf-8",
            data="""
export const handler = function(event) {
    return { statusCode: 200, body: event };
};

export const asyncHandler = async function(event) {
    await Promise.resolve();
    return event;
};
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "func_expr" in call[0][1].get("qualified_name", "")
        ]

        assert any("handler" in qn for qn in function_qns)
        assert any("asyncHandler" in qn for qn in function_qns)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestAsyncExports:
    def test_async_function_export(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "async_exports.js").write_text(
            encoding="utf-8",
            data="""
export async function fetchUsers() {
    return await fetch('/users').then(r => r.json());
}

exports.legacyFetch = async function() {
    return await fetch('/legacy').then(r => r.json());
};

module.exports.anotherFetch = async (url) => {
    return await fetch(url).then(r => r.json());
};
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "async_exports" in call[0][1].get("qualified_name", "")
        ]

        assert any("fetchUsers" in qn for qn in function_qns)
        assert any("legacyFetch" in qn for qn in function_qns)
        assert any("anotherFetch" in qn for qn in function_qns)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestLargeFileWithManyExports:
    def test_file_with_many_exports(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        exports_code = "\n".join(
            [f"export const func{i} = (x) => x + {i};" for i in range(20)]
        )
        (temp_js_project / "many_exports.js").write_text(
            encoding="utf-8", data=exports_code
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "many_exports" in call[0][1].get("qualified_name", "")
        ]

        assert len(function_qns) >= 10


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestCommonJSObjectExports:
    def test_module_exports_object_with_methods(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "object_exports.js").write_text(
            encoding="utf-8",
            data="""
module.exports = {
    create: function(data) {
        return { id: 1, ...data };
    },
    update: (id, data) => ({ id, ...data }),
    delete: function(id) {
        return { deleted: id };
    }
};
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        module_calls = [call for call in calls if call[0][0] == "Module"]

        assert len(module_calls) >= 1


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestDeepNestedModulePaths:
    def test_deeply_nested_require_paths(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        nested_dir = temp_js_project / "src" / "utils" / "helpers"
        nested_dir.mkdir(parents=True)

        (nested_dir / "string.js").write_text(
            encoding="utf-8",
            data="""
const { join } = require('path');
const { readFileSync } = require('fs');

exports.readAndJoin = function(file, dir) {
    return join(dir, readFileSync(file, 'utf8'));
};
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"] for call in calls if call[0][0] == "Function"
        ]

        assert any("readAndJoin" in qn for qn in function_qns)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestSpecialCharactersInExports:
    def test_exports_with_special_names(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "special_names.js").write_text(
            encoding="utf-8",
            data="""
exports.$helper = function() {
    return 'dollar helper';
};

exports._privateHelper = () => 'private helper';

exports.helper123 = function() {
    return 'numbered helper';
};
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "special_names" in call[0][1].get("qualified_name", "")
        ]

        assert any("$helper" in qn for qn in function_qns)
        assert any("_privateHelper" in qn for qn in function_qns)
        assert any("helper123" in qn for qn in function_qns)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestEmptyAndMinimalFiles:
    def test_empty_file(self, temp_js_project: Path, mock_ingestor: MagicMock) -> None:
        (temp_js_project / "empty.js").write_text(encoding="utf-8", data="")

        run_updater(temp_js_project, mock_ingestor)

    def test_file_with_only_comments(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "comments_only.js").write_text(
            encoding="utf-8",
            data="""
// This is a comment
/* This is a block comment */
/**
 * This is a JSDoc comment
 */
""",
        )

        run_updater(temp_js_project, mock_ingestor)

    def test_file_with_only_imports(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "imports_only.js").write_text(
            encoding="utf-8",
            data="""
const fs = require('fs');
const path = require('path');
const { EventEmitter } = require('events');
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        rel_calls = mock_ingestor.ensure_relationship_batch.call_args_list
        imports_rels = [call for call in rel_calls if call.args[1] == "IMPORTS"]

        assert len(imports_rels) >= 1


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestIIFEPatterns:
    def test_iife_with_exports(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "iife.js").write_text(
            encoding="utf-8",
            data="""
(function() {
    exports.init = function() {
        return 'initialized';
    };
})();

(() => {
    exports.setup = () => 'setup complete';
})();
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "iife" in call[0][1].get("qualified_name", "")
        ]

        assert any("init" in qn for qn in function_qns)
        assert any("setup" in qn for qn in function_qns)


@pytest.mark.skipif(not TS_AVAILABLE, reason="tree-sitter-typescript not available")
class TestTypeScriptInterfaces:
    def test_typescript_with_interfaces_and_types(
        self, temp_ts_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_ts_project / "types.ts").write_text(
            encoding="utf-8",
            data="""
export interface User {
    id: string;
    name: string;
}

export type UserCreator = (data: Partial<User>) => User;

export function createUser(data: Partial<User>): User {
    return { id: '1', name: 'default', ...data };
}
""",
        )

        run_updater(temp_ts_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "types" in call[0][1].get("qualified_name", "")
        ]

        assert any("createUser" in qn for qn in function_qns)


@pytest.mark.skipif(not TS_AVAILABLE, reason="tree-sitter-typescript not available")
class TestTypeScriptDecorators:
    def test_typescript_class_with_decorators(
        self, temp_ts_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_ts_project / "decorated.ts").write_text(
            encoding="utf-8",
            data="""
function Injectable() {
    return function(target: any) {};
}

@Injectable()
export class ApiService {
    getData(): Promise<string> {
        return Promise.resolve('data');
    }
}
""",
        )

        run_updater(temp_ts_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        class_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Class"
            and "decorated" in call[0][1].get("qualified_name", "")
        ]

        assert any("ApiService" in qn for qn in class_qns)
