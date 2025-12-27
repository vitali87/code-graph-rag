from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from tree_sitter import Parser

    from codebase_rag.types_defs import LanguageQueries


@pytest.fixture
def parsers_and_queries() -> tuple[
    dict[cs.SupportedLanguage, Parser], dict[cs.SupportedLanguage, LanguageQueries]
]:
    parsers, queries = load_parsers()
    return parsers, queries


class TestProcessCallsInFilePython:
    def test_processes_function_calls_in_file(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        test_file = temp_repo / "test_module.py"
        test_file.write_text("""
def helper():
    pass

def main():
    helper()
    print("hello")
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]
        assert len(calls) > 0

        call_targets = [c.args[2][2] for c in calls]
        helper_calls = [t for t in call_targets if "helper" in t]
        assert len(helper_calls) >= 1

    def test_processes_method_calls_in_class(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        test_file = temp_repo / "test_module.py"
        test_file.write_text("""
class MyClass:
    def helper(self):
        pass

    def main(self):
        self.helper()
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]

        caller_qns = [c.args[0][2] for c in calls]
        main_caller = [qn for qn in caller_qns if "main" in qn]
        assert len(main_caller) >= 1

    def test_processes_imported_function_calls(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        utils_dir = temp_repo / "utils"
        utils_dir.mkdir()
        (utils_dir / "__init__.py").write_text("")
        (utils_dir / "helpers.py").write_text("""
def format_string(s):
    return s.upper()
""")

        main_file = temp_repo / "main.py"
        main_file.write_text("""
from utils.helpers import format_string

def process():
    result = format_string("hello")
    return result
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]

        call_targets = [c.args[2][2] for c in calls]
        format_calls = [t for t in call_targets if "format_string" in t]
        assert len(format_calls) >= 1

    def test_processes_module_level_calls(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        test_file = temp_repo / "test_module.py"
        test_file.write_text("""
def setup():
    pass

setup()
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]

        caller_types = [c.args[0][0] for c in calls]
        module_callers = [t for t in caller_types if t == cs.NodeLabel.MODULE]
        assert len(module_callers) >= 1


class TestProcessCallsInFileJavaScript:
    def test_processes_function_calls_js(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        test_file = temp_repo / "test.js"
        test_file.write_text("""
function helper() {
    return 42;
}

function main() {
    const result = helper();
    console.log(result);
}
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]
        assert len(calls) > 0

    def test_processes_method_calls_js(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        test_file = temp_repo / "test.js"
        test_file.write_text("""
class MyClass {
    helper() {
        return 42;
    }

    main() {
        return this.helper();
    }
}
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]
        assert len(calls) >= 1

    def test_processes_builtin_calls_js(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        test_file = temp_repo / "test.js"
        test_file.write_text("""
function process(data) {
    const keys = Object.keys(data);
    const parsed = JSON.parse('{}');
    return keys;
}
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]

        call_targets = [c.args[2][2] for c in calls]
        builtin_calls = [t for t in call_targets if cs.BUILTIN_PREFIX in t]
        assert len(builtin_calls) >= 1


class TestProcessCallsInFileJava:
    def test_processes_method_invocation_java(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.JAVA not in parsers:
            pytest.skip("Java parser not available")

        test_file = temp_repo / "Test.java"
        test_file.write_text("""
public class Test {
    private void helper() {
    }

    public void main() {
        helper();
        System.out.println("hello");
    }
}
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]
        assert len(calls) >= 1

    def test_processes_same_class_method_calls_java(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.JAVA not in parsers:
            pytest.skip("Java parser not available")

        test_file = temp_repo / "Calculator.java"
        test_file.write_text("""
public class Calculator {
    public int add(int a, int b) {
        return a + b;
    }

    public int calculate() {
        return add(1, 2);
    }
}
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]

        call_targets = [c.args[2][2] for c in calls]
        add_calls = [t for t in call_targets if "add" in t]
        assert len(add_calls) >= 1


class TestProcessCallsInFileCpp:
    def test_processes_function_calls_cpp(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.CPP not in parsers:
            pytest.skip("C++ parser not available")

        test_file = temp_repo / "test.cpp"
        test_file.write_text("""
void helper() {
}

void main_func() {
    helper();
}
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]

        call_targets = [c.args[2][2] for c in calls]
        helper_calls = [t for t in call_targets if "helper" in t]
        assert len(helper_calls) >= 1

    def test_processes_method_calls_cpp(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.CPP not in parsers:
            pytest.skip("C++ parser not available")

        test_file = temp_repo / "test.cpp"
        test_file.write_text("""
class MyClass {
public:
    void helper() {
    }

    void main_method() {
        helper();
    }
};
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]
        assert len(calls) >= 1


class TestProcessCallsInFileRust:
    def test_processes_function_calls_rust(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.RUST not in parsers:
            pytest.skip("Rust parser not available")

        test_file = temp_repo / "lib.rs"
        test_file.write_text("""
fn helper() -> i32 {
    42
}

fn main_func() {
    let result = helper();
}
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]

        call_targets = [c.args[2][2] for c in calls]
        helper_calls = [t for t in call_targets if "helper" in t]
        assert len(helper_calls) >= 1

    def test_processes_impl_method_calls_rust(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.RUST not in parsers:
            pytest.skip("Rust parser not available")

        test_file = temp_repo / "lib.rs"
        test_file.write_text("""
struct MyStruct;

impl MyStruct {
    fn helper(&self) -> i32 {
        42
    }

    fn main_method(&self) {
        self.helper();
    }
}
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]
        assert len(calls) >= 1


class TestProcessCallsInFileTypeScript:
    def test_processes_function_calls_ts(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.TS not in parsers:
            pytest.skip("TypeScript parser not available")

        test_file = temp_repo / "test.ts"
        test_file.write_text("""
function helper(): number {
    return 42;
}

function main(): void {
    const result = helper();
    console.log(result);
}
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]
        assert len(calls) >= 1

    def test_processes_class_method_calls_ts(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.TS not in parsers:
            pytest.skip("TypeScript parser not available")

        test_file = temp_repo / "test.ts"
        test_file.write_text("""
class MyClass {
    private helper(): number {
        return 42;
    }

    public main(): number {
        return this.helper();
    }
}
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]
        assert len(calls) >= 1


class TestProcessCallsEdgeCases:
    def test_handles_empty_file(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        test_file = temp_repo / "empty.py"
        test_file.write_text("")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]
        assert len(calls) == 0

    def test_handles_file_with_only_imports(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        test_file = temp_repo / "imports_only.py"
        test_file.write_text("""
import os
import sys
from pathlib import Path
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

    def test_handles_nested_function_calls(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        test_file = temp_repo / "nested.py"
        test_file.write_text("""
def a():
    return 1

def b(x):
    return x + 1

def c(x):
    return x * 2

def main():
    result = c(b(a()))
    return result
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]

        call_targets = [c.args[2][2] for c in calls]
        all_funcs_called = all(
            any(f in t for t in call_targets) for f in ["a", "b", "c"]
        )
        assert all_funcs_called

    def test_handles_chained_method_calls(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        test_file = temp_repo / "chained.py"
        test_file.write_text("""
class Builder:
    def with_name(self, name):
        return self

    def with_value(self, value):
        return self

    def build(self):
        return {}

def main():
    result = Builder().with_name("test").with_value(42).build()
    return result
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]
        assert len(calls) >= 1

    def test_handles_init_py_module_qn(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        pkg_dir = temp_repo / "mypackage"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("""
def package_func():
    pass

package_func()
""")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        ]

        caller_qns = [c.args[0][2] for c in calls]
        package_callers = [qn for qn in caller_qns if "mypackage" in qn]
        assert len(package_callers) >= 1
