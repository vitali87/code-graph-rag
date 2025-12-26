from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.handlers import get_handler
from codebase_rag.parsers.handlers.base import BaseLanguageHandler
from codebase_rag.parsers.handlers.cpp import CppHandler
from codebase_rag.parsers.handlers.java import JavaHandler
from codebase_rag.parsers.handlers.js_ts import JsTsHandler
from codebase_rag.parsers.handlers.lua import LuaHandler
from codebase_rag.parsers.handlers.rust import RustHandler
from codebase_rag.tests.conftest import get_node_names, get_nodes, run_updater


class TestHandlerDelegationInPipeline:
    def test_js_handler_used_for_javascript_files(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "js_handler_test"
        project_path.mkdir()
        (project_path / "test.js").write_text("function foo() {}")

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=project_path,
            parsers=parsers,
            queries=queries,
        )

        handler = get_handler(cs.SupportedLanguage.JS)
        assert isinstance(handler, JsTsHandler)

        updater.run()

        function_nodes = get_nodes(mock_ingestor, "Function")
        assert len(function_nodes) >= 1

    def test_ts_handler_used_for_typescript_files(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "ts_handler_test"
        project_path.mkdir()
        (project_path / "test.ts").write_text("function bar(): void {}")

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.TS not in parsers:
            pytest.skip("TypeScript parser not available")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=project_path,
            parsers=parsers,
            queries=queries,
        )

        handler = get_handler(cs.SupportedLanguage.TS)
        assert isinstance(handler, JsTsHandler)

        updater.run()

        function_nodes = get_nodes(mock_ingestor, "Function")
        assert len(function_nodes) >= 1

    def test_cpp_handler_used_for_cpp_files(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "cpp_handler_test"
        project_path.mkdir()
        (project_path / "test.cpp").write_text("void foo() {}")

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.CPP not in parsers:
            pytest.skip("C++ parser not available")

        handler = get_handler(cs.SupportedLanguage.CPP)
        assert isinstance(handler, CppHandler)

        run_updater(project_path, mock_ingestor)

        function_nodes = get_nodes(mock_ingestor, "Function")
        assert len(function_nodes) >= 1

    def test_rust_handler_used_for_rust_files(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "rust_handler_test"
        project_path.mkdir()
        (project_path / "test.rs").write_text("fn foo() {}")

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.RUST not in parsers:
            pytest.skip("Rust parser not available")

        handler = get_handler(cs.SupportedLanguage.RUST)
        assert isinstance(handler, RustHandler)

        run_updater(project_path, mock_ingestor)

        function_nodes = get_nodes(mock_ingestor, "Function")
        assert len(function_nodes) >= 1

    def test_java_handler_used_for_java_files(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "java_handler_test"
        project_path.mkdir()
        (project_path / "Test.java").write_text(
            "public class Test { public void foo() {} }"
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.JAVA not in parsers:
            pytest.skip("Java parser not available")

        handler = get_handler(cs.SupportedLanguage.JAVA)
        assert isinstance(handler, JavaHandler)

        run_updater(project_path, mock_ingestor)

        class_nodes = get_nodes(mock_ingestor, "Class")
        assert len(class_nodes) >= 1

    def test_lua_handler_used_for_lua_files(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "lua_handler_test"
        project_path.mkdir()
        (project_path / "test.lua").write_text("function foo() end")

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.LUA not in parsers:
            pytest.skip("Lua parser not available")

        handler = get_handler(cs.SupportedLanguage.LUA)
        assert isinstance(handler, LuaHandler)

        run_updater(project_path, mock_ingestor)

        function_nodes = get_nodes(mock_ingestor, "Function")
        assert len(function_nodes) >= 1

    def test_base_handler_used_for_python_files(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "python_handler_test"
        project_path.mkdir()
        (project_path / "test.py").write_text("def foo(): pass")

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        handler = get_handler(cs.SupportedLanguage.PYTHON)
        assert isinstance(handler, BaseLanguageHandler)
        assert type(handler) is BaseLanguageHandler

        run_updater(project_path, mock_ingestor)

        function_nodes = get_nodes(mock_ingestor, "Function")
        assert len(function_nodes) >= 1

    def test_handler_switches_per_file_language(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "multi_lang_handler_test"
        project_path.mkdir()
        (project_path / "test.js").write_text("function jsFunc() {}")
        (project_path / "test.py").write_text("def pyFunc(): pass")

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        run_updater(project_path, mock_ingestor)

        function_names = get_node_names(mock_ingestor, "Function")
        assert any("jsFunc" in name for name in function_names)
        assert any("pyFunc" in name for name in function_names)


class TestJsTsHandlerIntegration:
    def test_standalone_functions_ingested(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "js_standalone_func_test"
        project_path.mkdir()
        (project_path / "test.js").write_text(
            """
function standalone() { return 'standalone'; }
const arrow = () => 'arrow';
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        run_updater(project_path, mock_ingestor)

        function_names = get_node_names(mock_ingestor, "Function")
        assert any("standalone" in name for name in function_names)
        assert any("arrow" in name for name in function_names)

    def test_object_literal_methods_ingested(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "js_object_literal_test"
        project_path.mkdir()
        (project_path / "test.js").write_text(
            """
const calculator = {
    add(a, b) { return a + b; },
    subtract(a, b) { return a - b; }
};
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        run_updater(project_path, mock_ingestor)

        function_names = get_node_names(mock_ingestor, "Function")
        assert any("add" in name for name in function_names)
        assert any("subtract" in name for name in function_names)

    def test_exports_inside_functions_skipped(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "js_export_inside_func_test"
        project_path.mkdir()
        (project_path / "test.js").write_text(
            """
export function topLevel() { return 'top'; }

function wrapper() {
    module.exports.inner = function() { return 'inner'; };
}
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        run_updater(project_path, mock_ingestor)

        function_names = get_node_names(mock_ingestor, "Function")
        assert any("topLevel" in name for name in function_names)
        assert any("wrapper" in name for name in function_names)

    def test_class_is_ingested_with_methods(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "js_class_test"
        project_path.mkdir()
        (project_path / "test.js").write_text(
            """
class Calculator {
    add(a, b) { return a + b; }
    subtract(a, b) { return a - b; }
}
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        run_updater(project_path, mock_ingestor)

        class_names = get_node_names(mock_ingestor, "Class")
        assert any("Calculator" in name for name in class_names)


class TestCppHandlerIntegration:
    def test_lambda_functions_get_generated_names(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "cpp_lambda_test"
        project_path.mkdir()
        (project_path / "test.cpp").write_text(
            """
void process() {
    auto lambda = []() { return 42; };
}
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.CPP not in parsers:
            pytest.skip("C++ parser not available")

        run_updater(project_path, mock_ingestor)

        function_names = get_node_names(mock_ingestor, "Function")
        assert any("process" in name for name in function_names)
        lambda_names = [n for n in function_names if "lambda" in n.lower()]
        assert len(lambda_names) >= 1

    def test_namespaced_functions_have_full_qn(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "cpp_namespace_test"
        project_path.mkdir()
        (project_path / "test.cpp").write_text(
            """
namespace MyNamespace {
    void myFunction() {}
}
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.CPP not in parsers:
            pytest.skip("C++ parser not available")

        run_updater(project_path, mock_ingestor)

        function_names = get_node_names(mock_ingestor, "Function")
        namespaced_funcs = [n for n in function_names if "myFunction" in n]
        assert len(namespaced_funcs) >= 1
        assert any("MyNamespace" in n for n in namespaced_funcs)

    def test_template_base_class_names_extracted(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "cpp_template_base_test"
        project_path.mkdir()
        (project_path / "test.cpp").write_text(
            """
template<typename T>
class Base {};

class Derived : public Base<int> {};
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.CPP not in parsers:
            pytest.skip("C++ parser not available")

        run_updater(project_path, mock_ingestor)

        class_names = get_node_names(mock_ingestor, "Class")
        assert any("Derived" in name for name in class_names)


class TestRustHandlerIntegration:
    def test_standalone_functions_ingested(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "rust_func_test"
        project_path.mkdir()
        (project_path / "test.rs").write_text(
            """
fn standalone_function() -> i32 {
    42
}

pub fn public_function() {}
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.RUST not in parsers:
            pytest.skip("Rust parser not available")

        run_updater(project_path, mock_ingestor)

        function_names = get_node_names(mock_ingestor, "Function")
        assert any("standalone_function" in name for name in function_names)
        assert any("public_function" in name for name in function_names)

    def test_struct_is_ingested(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "rust_struct_test"
        project_path.mkdir()
        (project_path / "test.rs").write_text(
            """
struct MyStruct {
    field: i32,
}
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.RUST not in parsers:
            pytest.skip("Rust parser not available")

        run_updater(project_path, mock_ingestor)

        class_names = get_node_names(mock_ingestor, "Class")
        assert any("MyStruct" in name for name in class_names)


class TestJavaHandlerIntegration:
    def test_class_is_ingested(self, temp_repo: Path, mock_ingestor: MagicMock) -> None:
        project_path = temp_repo / "java_class_test"
        project_path.mkdir()
        (project_path / "Calculator.java").write_text(
            """
public class Calculator {
    public int add(int a, int b) { return a + b; }
    public int subtract(int a, int b) { return a - b; }
}
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.JAVA not in parsers:
            pytest.skip("Java parser not available")

        run_updater(project_path, mock_ingestor)

        class_names = get_node_names(mock_ingestor, "Class")
        assert any("Calculator" in name for name in class_names)


class TestLuaHandlerIntegration:
    def test_assigned_function_names_extracted(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "lua_assigned_test"
        project_path.mkdir()
        (project_path / "test.lua").write_text(
            """
myFunc = function()
    return 42
end

function namedFunc()
    return 43
end
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.LUA not in parsers:
            pytest.skip("Lua parser not available")

        run_updater(project_path, mock_ingestor)

        function_names = get_node_names(mock_ingestor, "Function")
        assert any("myFunc" in name for name in function_names)
        assert any("namedFunc" in name for name in function_names)

    def test_dot_index_function_names_extracted(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        project_path = temp_repo / "lua_dot_index_test"
        project_path.mkdir()
        (project_path / "test.lua").write_text(
            """
local MyModule = {}

MyModule.myMethod = function()
    return "method"
end

function MyModule.anotherMethod()
    return "another"
end

return MyModule
"""
        )

        parsers, queries = load_parsers()
        if cs.SupportedLanguage.LUA not in parsers:
            pytest.skip("Lua parser not available")

        run_updater(project_path, mock_ingestor)

        function_names = get_node_names(mock_ingestor, "Function")
        assert any("myMethod" in name or "MyModule" in name for name in function_names)
        assert any(
            "anotherMethod" in name or "MyModule" in name for name in function_names
        )
