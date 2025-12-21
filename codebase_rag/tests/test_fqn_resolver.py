from __future__ import annotations

from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Parser, Tree

from codebase_rag.constants import SupportedLanguage
from codebase_rag.language_spec import LANGUAGE_FQN_SPECS
from codebase_rag.utils.fqn_resolver import (
    extract_function_fqns,
    find_function_source_by_fqn,
    resolve_fqn_from_ast,
)


def get_python_parser() -> Parser:
    parser = Parser(Language(tspython.language()))
    return parser


def parse_python(code: str) -> Tree:
    parser = get_python_parser()
    return parser.parse(code.encode())


class TestResolveFqnFromAst:
    def test_simple_function(self) -> None:
        code = "def my_func(): pass"
        tree = parse_python(code)
        func_node = tree.root_node.children[0]
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = resolve_fqn_from_ast(
            func_node, file_path, repo_root, "project", config
        )

        assert result == "project.mymodule.my_func"

    def test_nested_in_class(self) -> None:
        code = """
class MyClass:
    def my_method(self):
        pass
"""
        tree = parse_python(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        assert class_body is not None
        method_node = class_body.children[0]
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = resolve_fqn_from_ast(
            method_node, file_path, repo_root, "project", config
        )

        assert result == "project.mymodule.MyClass.my_method"

    def test_deeply_nested(self) -> None:
        code = """
class Outer:
    class Inner:
        def method(self):
            pass
"""
        tree = parse_python(code)
        outer_class = tree.root_node.children[0]
        outer_body = outer_class.child_by_field_name("body")
        assert outer_body is not None
        inner_class = outer_body.children[0]
        inner_body = inner_class.child_by_field_name("body")
        assert inner_body is not None
        method_node = inner_body.children[0]
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "pkg" / "module.py"

        result = resolve_fqn_from_ast(
            method_node, file_path, repo_root, "project", config
        )

        assert result == "project.pkg.module.Outer.Inner.method"

    def test_init_file_excluded_from_path(self) -> None:
        code = "def func(): pass"
        tree = parse_python(code)
        func_node = tree.root_node.children[0]
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "pkg" / "__init__.py"

        result = resolve_fqn_from_ast(
            func_node, file_path, repo_root, "project", config
        )

        assert result == "project.pkg.func"

    def test_lambda_returns_none(self) -> None:
        code = "f = lambda x: x"
        tree = parse_python(code)
        expr_stmt = tree.root_node.children[0]
        assign = expr_stmt.children[0]
        lambda_node = assign.child_by_field_name("right")
        assert lambda_node is not None
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = resolve_fqn_from_ast(
            lambda_node, file_path, repo_root, "project", config
        )

        assert result is None


class TestFindFunctionSourceByFqn:
    def test_finds_matching_function(self) -> None:
        code = "def target_func(): pass"
        tree = parse_python(code)
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = find_function_source_by_fqn(
            tree.root_node,
            "project.mymodule.target_func",
            file_path,
            repo_root,
            "project",
            config,
        )

        assert result == "def target_func(): pass"

    def test_returns_none_when_not_found(self) -> None:
        code = "def other_func(): pass"
        tree = parse_python(code)
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = find_function_source_by_fqn(
            tree.root_node,
            "project.mymodule.target_func",
            file_path,
            repo_root,
            "project",
            config,
        )

        assert result is None

    def test_finds_nested_method(self) -> None:
        code = """
class MyClass:
    def my_method(self):
        pass
"""
        tree = parse_python(code)
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = find_function_source_by_fqn(
            tree.root_node,
            "project.mymodule.MyClass.my_method",
            file_path,
            repo_root,
            "project",
            config,
        )

        assert result is not None
        assert "def my_method(self):" in result

    def test_empty_tree_returns_none(self) -> None:
        code = ""
        tree = parse_python(code)
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = find_function_source_by_fqn(
            tree.root_node,
            "project.mymodule.func",
            file_path,
            repo_root,
            "project",
            config,
        )

        assert result is None


class TestExtractFunctionFqns:
    def test_extracts_single_function(self) -> None:
        code = "def my_func(): pass"
        tree = parse_python(code)
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = extract_function_fqns(
            tree.root_node, file_path, repo_root, "project", config
        )

        assert len(result) == 1
        assert result[0][0] == "project.mymodule.my_func"

    def test_extracts_multiple_functions(self) -> None:
        code = """
def func1(): pass
def func2(): pass
"""
        tree = parse_python(code)
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = extract_function_fqns(
            tree.root_node, file_path, repo_root, "project", config
        )

        assert len(result) == 2
        fqns = {fqn for fqn, _ in result}
        assert fqns == {"project.mymodule.func1", "project.mymodule.func2"}

    def test_extracts_nested_methods(self) -> None:
        code = """
class MyClass:
    def method1(self): pass
    def method2(self): pass
"""
        tree = parse_python(code)
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = extract_function_fqns(
            tree.root_node, file_path, repo_root, "project", config
        )

        assert len(result) == 2
        fqns = {fqn for fqn, _ in result}
        assert fqns == {
            "project.mymodule.MyClass.method1",
            "project.mymodule.MyClass.method2",
        }

    def test_empty_tree_returns_empty_list(self) -> None:
        code = ""
        tree = parse_python(code)
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = extract_function_fqns(
            tree.root_node, file_path, repo_root, "project", config
        )

        assert result == []

    def test_skips_lambdas(self) -> None:
        code = """
def named_func(): pass
f = lambda x: x
"""
        tree = parse_python(code)
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = extract_function_fqns(
            tree.root_node, file_path, repo_root, "project", config
        )

        assert len(result) == 1
        assert result[0][0] == "project.mymodule.named_func"

    def test_extracts_from_multiple_classes(self) -> None:
        code = """
class ClassA:
    def method_a(self): pass

class ClassB:
    def method_b(self): pass
"""
        tree = parse_python(code)
        config = LANGUAGE_FQN_SPECS[SupportedLanguage.PYTHON]
        repo_root = Path("/repo")
        file_path = repo_root / "mymodule.py"

        result = extract_function_fqns(
            tree.root_node, file_path, repo_root, "project", config
        )

        assert len(result) == 2
        fqns = {fqn for fqn, _ in result}
        assert fqns == {
            "project.mymodule.ClassA.method_a",
            "project.mymodule.ClassB.method_b",
        }
