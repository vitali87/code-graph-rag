from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tree_sitter import Language, Parser

from codebase_rag import constants as cs
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parsers.handlers.base import BaseLanguageHandler
from codebase_rag.parsers.handlers.cpp import CppHandler
from codebase_rag.parsers.handlers.java import JavaHandler
from codebase_rag.parsers.handlers.js_ts import JsTsHandler
from codebase_rag.parsers.handlers.lua import LuaHandler
from codebase_rag.parsers.handlers.python import PythonHandler
from codebase_rag.parsers.handlers.rust import RustHandler
from codebase_rag.tests.conftest import create_mock_node

if TYPE_CHECKING:
    from codebase_rag.types_defs import ASTNode

try:
    import tree_sitter_javascript as tsjs

    JS_AVAILABLE = True
except ImportError:
    JS_AVAILABLE = False

try:
    import tree_sitter_python as tspython

    PYTHON_AVAILABLE = True
except ImportError:
    PYTHON_AVAILABLE = False

try:
    import tree_sitter_cpp as tscpp

    CPP_AVAILABLE = True
except ImportError:
    CPP_AVAILABLE = False

try:
    import tree_sitter_rust as tsrust

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

try:
    import tree_sitter_java as tsjava

    JAVA_AVAILABLE = True
except ImportError:
    JAVA_AVAILABLE = False

try:
    import tree_sitter_lua as tslua

    LUA_AVAILABLE = True
except ImportError:
    LUA_AVAILABLE = False


@pytest.fixture
def js_parser() -> Parser | None:
    if not JS_AVAILABLE:
        return None
    language = Language(tsjs.language())
    return Parser(language)


@pytest.fixture
def python_parser() -> Parser | None:
    if not PYTHON_AVAILABLE:
        return None
    language = Language(tspython.language())
    return Parser(language)


@pytest.fixture
def cpp_parser() -> Parser | None:
    if not CPP_AVAILABLE:
        return None
    language = Language(tscpp.language())
    return Parser(language)


@pytest.fixture
def rust_parser() -> Parser | None:
    if not RUST_AVAILABLE:
        return None
    language = Language(tsrust.language())
    return Parser(language)


@pytest.fixture
def java_parser() -> Parser | None:
    if not JAVA_AVAILABLE:
        return None
    language = Language(tsjava.language())
    return Parser(language)


@pytest.fixture
def lua_parser() -> Parser | None:
    if not LUA_AVAILABLE:
        return None
    language = Language(tslua.language())
    return Parser(language)


class TestBaseLanguageHandler:
    def test_is_inside_method_with_object_literals_returns_false(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_FUNCTION_DECLARATION)
        assert handler.is_inside_method_with_object_literals(node) is False

    def test_is_class_method_returns_false(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_METHOD_DEFINITION)
        assert handler.is_class_method(node) is False

    def test_is_export_inside_function_returns_false(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_EXPORT_STATEMENT)
        assert handler.is_export_inside_function(node) is False

    def test_extract_function_name_with_name_field(self) -> None:
        handler = BaseLanguageHandler()
        name_node = create_mock_node(cs.TS_IDENTIFIER, text="myFunction")
        node = create_mock_node(
            cs.TS_FUNCTION_DECLARATION,
            fields={cs.TS_FIELD_NAME: name_node},
        )
        assert handler.extract_function_name(node) == "myFunction"

    def test_extract_function_name_without_name_returns_none(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_FUNCTION_DECLARATION)
        assert handler.extract_function_name(node) is None

    def test_build_function_qualified_name_simple(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_FUNCTION_DECLARATION)
        result = handler.build_function_qualified_name(
            node=node,
            module_qn="project.module",
            func_name="myFunc",
            lang_config=None,
            file_path=None,
            repo_path=Path("/repo"),
            project_name="project",
        )
        assert result == "project.module.myFunc"

    def test_is_function_exported_returns_false(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_FUNCTION_DECLARATION)
        assert handler.is_function_exported(node) is False

    def test_should_process_as_impl_block_returns_false(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_IMPL_ITEM)
        assert handler.should_process_as_impl_block(node) is False

    def test_extract_impl_target_returns_none(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_IMPL_ITEM)
        assert handler.extract_impl_target(node) is None

    def test_build_method_qualified_name_simple(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_METHOD_DEFINITION)
        result = handler.build_method_qualified_name(
            class_qn="project.module.MyClass",
            method_name="myMethod",
            method_node=node,
        )
        assert result == "project.module.MyClass.myMethod"

    def test_extract_base_class_name_with_text(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_IDENTIFIER, text="BaseClass")
        assert handler.extract_base_class_name(node) == "BaseClass"

    def test_extract_base_class_name_without_text_returns_none(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_IDENTIFIER, text="")
        assert handler.extract_base_class_name(node) is None

    def test_extract_decorators_returns_empty_list(self) -> None:
        handler = BaseLanguageHandler()
        node = create_mock_node(cs.TS_FUNCTION_DECLARATION)
        assert handler.extract_decorators(node) == []

    @pytest.mark.skipif(not PYTHON_AVAILABLE, reason="Python parser not available")
    def test_build_nested_function_qn_with_parent_functions(
        self, python_parser: Parser
    ) -> None:
        handler = BaseLanguageHandler()
        code = b"""
def outer():
    def inner():
        pass
"""
        tree = python_parser.parse(code)
        outer_func = tree.root_node.children[0]
        body = outer_func.child_by_field_name("body")
        inner_func = body.children[0]

        result = handler.build_nested_function_qn(
            func_node=inner_func,
            module_qn="project.module",
            func_name="inner",
            lang_config=LANGUAGE_SPECS[cs.SupportedLanguage.PYTHON],
        )
        assert result == "project.module.outer.inner"

    @pytest.mark.skipif(not PYTHON_AVAILABLE, reason="Python parser not available")
    def test_build_nested_function_qn_stops_at_class(
        self, python_parser: Parser
    ) -> None:
        handler = BaseLanguageHandler()
        code = b"""
class MyClass:
    def method(self):
        def nested():
            pass
"""
        tree = python_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        method = class_body.children[0]
        method_body = method.child_by_field_name("body")
        nested_func = method_body.children[0]

        result = handler.build_nested_function_qn(
            func_node=nested_func,
            module_qn="project.module",
            func_name="nested",
            lang_config=LANGUAGE_SPECS[cs.SupportedLanguage.PYTHON],
        )
        assert result is None


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestJsTsHandler:
    def test_is_inside_method_with_object_literals_nested_in_method(
        self, js_parser: Parser
    ) -> None:
        handler = JsTsHandler()
        code = b"""
class MyClass {
    myMethod() {
        return {
            nested() { return 'nested'; }
        };
    }
}
"""
        tree = js_parser.parse(code)
        class_body = tree.root_node.children[0].child_by_field_name("body")
        method_def = class_body.children[1]
        method_body = method_def.child_by_field_name("body")
        return_stmt = method_body.children[1]
        obj = return_stmt.children[1]
        pair = obj.children[1]
        nested_func = pair.children[0]

        assert handler.is_inside_method_with_object_literals(nested_func) is True

    def test_is_inside_method_with_object_literals_standalone_object(
        self, js_parser: Parser
    ) -> None:
        handler = JsTsHandler()
        code = b"""
const obj = {
    method() { return 'method'; }
};
"""
        tree = js_parser.parse(code)
        var_decl = tree.root_node.children[0]
        declarator = var_decl.children[1]
        obj = declarator.child_by_field_name("value")
        pair = obj.children[1]

        assert handler.is_inside_method_with_object_literals(pair) is False

    def test_is_inside_method_with_object_literals_stops_at_class_body(
        self, js_parser: Parser
    ) -> None:
        handler = JsTsHandler()
        code = b"""
class MyClass {
    myMethod() { return 'method'; }
}
"""
        tree = js_parser.parse(code)
        class_body = tree.root_node.children[0].child_by_field_name("body")
        method_def = class_body.children[1]

        assert handler.is_inside_method_with_object_literals(method_def) is False

    def test_is_class_method_in_class_body(self, js_parser: Parser) -> None:
        handler = JsTsHandler()
        code = b"""
class MyClass {
    myMethod() { return 'method'; }
}
"""
        tree = js_parser.parse(code)
        class_body = tree.root_node.children[0].child_by_field_name("body")
        method_node = class_body.children[1]

        assert handler.is_class_method(method_node) is True

    def test_is_class_method_at_module_level(self, js_parser: Parser) -> None:
        handler = JsTsHandler()
        code = b"function standalone() { return 'standalone'; }"
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]

        assert handler.is_class_method(func_node) is False

    def test_is_export_inside_function_at_module_level(self, js_parser: Parser) -> None:
        handler = JsTsHandler()
        code = b"export function myFunc() { return 'exported'; }"
        tree = js_parser.parse(code)
        export_node = tree.root_node.children[0]

        assert handler.is_export_inside_function(export_node) is False

    def test_is_export_inside_function_nested(self, js_parser: Parser) -> None:
        handler = JsTsHandler()
        code = b"""
function outer() {
    module.exports.inner = function() { return 'inner'; };
}
"""
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]
        body = func_node.child_by_field_name("body")
        expr_stmt = body.children[1]

        assert handler.is_export_inside_function(expr_stmt) is True

    def test_extract_function_name_arrow_in_variable_declarator(
        self, js_parser: Parser
    ) -> None:
        handler = JsTsHandler()
        code = b"const myArrow = (x) => x * 2;"
        tree = js_parser.parse(code)
        var_decl = tree.root_node.children[0]
        declarator = var_decl.children[1]
        arrow_node = declarator.child_by_field_name("value")

        result = handler.extract_function_name(arrow_node)
        assert result == "myArrow"

    def test_extract_function_name_arrow_in_callback(self, js_parser: Parser) -> None:
        handler = JsTsHandler()
        code = b"doSomething((x) => x * 2);"
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        call = expr_stmt.children[0]
        args = call.child_by_field_name("arguments")
        arrow_node = args.children[1]

        result = handler.extract_function_name(arrow_node)
        assert result is None

    def test_extract_function_name_regular_function(self, js_parser: Parser) -> None:
        handler = JsTsHandler()
        code = b"function myFunc() { return 42; }"
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]

        result = handler.extract_function_name(func_node)
        assert result == "myFunc"

    def test_build_nested_function_qn_with_class_and_object_literals(
        self, js_parser: Parser
    ) -> None:
        handler = JsTsHandler()
        code = b"""
class MyClass {
    myMethod() {
        return {
            nested() { return 'nested'; }
        };
    }
}
"""
        tree = js_parser.parse(code)
        class_body = tree.root_node.children[0].child_by_field_name("body")
        method_def = class_body.children[1]
        method_body = method_def.child_by_field_name("body")
        return_stmt = method_body.children[1]
        obj = return_stmt.children[1]
        pair = obj.children[1]
        nested_func = pair.children[0]

        result = handler.build_nested_function_qn(
            func_node=nested_func,
            module_qn="project.module",
            func_name="nested",
            lang_config=LANGUAGE_SPECS[cs.SupportedLanguage.JS],
        )
        assert result is not None
        assert "MyClass" in result
        assert "myMethod" in result
        assert "nested" in result

    def test_build_nested_function_qn_skips_class_without_object_literals(
        self, js_parser: Parser
    ) -> None:
        handler = JsTsHandler()
        code = b"""
class MyClass {
    myMethod() {
        function nested() { return 'nested'; }
    }
}
"""
        tree = js_parser.parse(code)
        class_body = tree.root_node.children[0].child_by_field_name("body")
        method_def = class_body.children[1]
        method_body = method_def.child_by_field_name("body")
        nested_func = method_body.children[1]

        result = handler.build_nested_function_qn(
            func_node=nested_func,
            module_qn="project.module",
            func_name="nested",
            lang_config=LANGUAGE_SPECS[cs.SupportedLanguage.JS],
        )
        assert result is None

    def test_extract_decorators_returns_empty_for_undecorated(
        self, js_parser: Parser
    ) -> None:
        handler = JsTsHandler()
        code = b"function foo() {}"
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]

        result = handler.extract_decorators(func_node)
        assert result == []


try:
    import tree_sitter_typescript as tsts

    TS_AVAILABLE = True
except ImportError:
    TS_AVAILABLE = False


@pytest.fixture
def ts_parser() -> Parser | None:
    if not TS_AVAILABLE:
        return None
    language = Language(tsts.language_typescript())
    return Parser(language)


@pytest.mark.skipif(not TS_AVAILABLE, reason="tree-sitter-typescript not available")
class TestJsTsHandlerTypeScriptDecorators:
    def test_extract_decorators_single_decorator(self, ts_parser: Parser) -> None:
        handler = JsTsHandler()
        code = b"""
@Component
class MyClass {}
"""
        tree = ts_parser.parse(code)
        class_node = tree.root_node.children[0]

        result = handler.extract_decorators(class_node)
        assert result == ["@Component"]

    def test_extract_decorators_decorator_with_call(self, ts_parser: Parser) -> None:
        handler = JsTsHandler()
        code = b"""
@Injectable()
class MyService {}
"""
        tree = ts_parser.parse(code)
        class_node = tree.root_node.children[0]

        result = handler.extract_decorators(class_node)
        assert result == ["@Injectable()"]

    def test_extract_decorators_decorator_with_args(self, ts_parser: Parser) -> None:
        handler = JsTsHandler()
        code = b"""
@Component({selector: 'app-root', template: '<div></div>'})
class AppComponent {}
"""
        tree = ts_parser.parse(code)
        class_node = tree.root_node.children[0]

        result = handler.extract_decorators(class_node)
        assert len(result) == 1
        assert (
            "@Component({selector: 'app-root', template: '<div></div>'})" in result[0]
        )

    def test_extract_decorators_multiple_decorators(self, ts_parser: Parser) -> None:
        handler = JsTsHandler()
        code = b"""
@Component
@Injectable()
class MyClass {}
"""
        tree = ts_parser.parse(code)
        class_node = tree.root_node.children[0]

        result = handler.extract_decorators(class_node)
        assert len(result) == 2
        assert "@Component" in result
        assert "@Injectable()" in result

    def test_extract_decorators_member_expression(self, ts_parser: Parser) -> None:
        handler = JsTsHandler()
        code = b"""
@ng.Component
class MyClass {}
"""
        tree = ts_parser.parse(code)
        class_node = tree.root_node.children[0]

        result = handler.extract_decorators(class_node)
        assert result == ["@ng.Component"]


@pytest.mark.skipif(not CPP_AVAILABLE, reason="tree-sitter-cpp not available")
class TestCppHandler:
    def test_extract_function_name_regular_function(self, cpp_parser: Parser) -> None:
        handler = CppHandler()
        code = b"void myFunction() {}"
        tree = cpp_parser.parse(code)
        func_node = tree.root_node.children[0]

        result = handler.extract_function_name(func_node)
        assert result == "myFunction"

    def test_extract_function_name_lambda_expression(self, cpp_parser: Parser) -> None:
        handler = CppHandler()
        code = b"auto lambda = []() { return 42; };"
        tree = cpp_parser.parse(code)
        decl = tree.root_node.children[0]

        def find_lambda(node: ASTNode) -> ASTNode | None:
            if node.type == cs.TS_CPP_LAMBDA_EXPRESSION:
                return node
            for child in node.children:
                if result := find_lambda(child):
                    return result
            return None

        lambda_node = find_lambda(decl)
        assert lambda_node is not None

        result = handler.extract_function_name(lambda_node)
        assert result is not None
        assert result.startswith("lambda_")

    def test_build_function_qualified_name_simple(self, cpp_parser: Parser) -> None:
        handler = CppHandler()
        code = b"void myFunction() {}"
        tree = cpp_parser.parse(code)
        func_node = tree.root_node.children[0]

        result = handler.build_function_qualified_name(
            node=func_node,
            module_qn="project.source.main",
            func_name="myFunction",
            lang_config=None,
            file_path=None,
            repo_path=Path("/repo"),
            project_name="project",
        )
        assert "myFunction" in result

    def test_is_function_exported_without_export(self, cpp_parser: Parser) -> None:
        handler = CppHandler()
        code = b"void myFunction() {}"
        tree = cpp_parser.parse(code)
        func_node = tree.root_node.children[0]

        result = handler.is_function_exported(func_node)
        assert result is False

    def test_extract_base_class_name_simple_identifier(
        self, cpp_parser: Parser
    ) -> None:
        handler = CppHandler()
        code = b"class Derived : public Base {};"
        tree = cpp_parser.parse(code)
        class_node = tree.root_node.children[0]

        def find_base_clause(node: ASTNode) -> ASTNode | None:
            if node.type == "base_class_clause":
                return node
            for child in node.children:
                if result := find_base_clause(child):
                    return result
            return None

        base_clause = find_base_clause(class_node)
        assert base_clause is not None

        def find_type_identifier(node: ASTNode) -> ASTNode | None:
            if node.type == "type_identifier":
                return node
            for child in node.children:
                if result := find_type_identifier(child):
                    return result
            return None

        base_node = find_type_identifier(base_clause)
        assert base_node is not None

        result = handler.extract_base_class_name(base_node)
        assert result == "Base"

    def test_extract_base_class_name_template_type(self, cpp_parser: Parser) -> None:
        handler = CppHandler()
        code = b"class Derived : public Base<int> {};"
        tree = cpp_parser.parse(code)
        class_node = tree.root_node.children[0]

        def find_template_type(node: ASTNode) -> ASTNode | None:
            if node.type == "template_type":
                return node
            for child in node.children:
                if result := find_template_type(child):
                    return result
            return None

        template_node = find_template_type(class_node)
        assert template_node is not None

        result = handler.extract_base_class_name(template_node)
        assert result == "Base"


@pytest.mark.skipif(not RUST_AVAILABLE, reason="tree-sitter-rust not available")
class TestRustHandler:
    def test_should_process_as_impl_block_with_impl_item(
        self, rust_parser: Parser
    ) -> None:
        handler = RustHandler()
        code = b"""
impl MyStruct {
    fn method(&self) {}
}
"""
        tree = rust_parser.parse(code)
        impl_node = tree.root_node.children[0]

        assert handler.should_process_as_impl_block(impl_node) is True

    def test_should_process_as_impl_block_with_other_node(
        self, rust_parser: Parser
    ) -> None:
        handler = RustHandler()
        code = b"fn my_function() {}"
        tree = rust_parser.parse(code)
        func_node = tree.root_node.children[0]

        assert handler.should_process_as_impl_block(func_node) is False

    def test_extract_impl_target_struct(self, rust_parser: Parser) -> None:
        handler = RustHandler()
        code = b"""
impl MyStruct {
    fn method(&self) {}
}
"""
        tree = rust_parser.parse(code)
        impl_node = tree.root_node.children[0]

        result = handler.extract_impl_target(impl_node)
        assert result == "MyStruct"

    def test_extract_impl_target_trait_for_struct(self, rust_parser: Parser) -> None:
        handler = RustHandler()
        code = b"""
impl MyTrait for MyStruct {
    fn method(&self) {}
}
"""
        tree = rust_parser.parse(code)
        impl_node = tree.root_node.children[0]

        result = handler.extract_impl_target(impl_node)
        assert result == "MyStruct"

    def test_build_function_qualified_name_simple(self, rust_parser: Parser) -> None:
        handler = RustHandler()
        code = b"fn my_function() {}"
        tree = rust_parser.parse(code)
        func_node = tree.root_node.children[0]

        result = handler.build_function_qualified_name(
            node=func_node,
            module_qn="project.src.lib",
            func_name="my_function",
            lang_config=None,
            file_path=None,
            repo_path=Path("/repo"),
            project_name="project",
        )
        assert "my_function" in result

    def test_extract_decorators_single_attribute(self, rust_parser: Parser) -> None:
        handler = RustHandler()
        code = b"#[derive(Debug)]\nstruct MyStruct {}"
        tree = rust_parser.parse(code)
        struct_node = next(
            c for c in tree.root_node.children if c.type == cs.TS_RS_STRUCT_ITEM
        )

        result = handler.extract_decorators(struct_node)
        assert any("derive" in attr for attr in result)

    def test_extract_decorators_multiple_attributes(self, rust_parser: Parser) -> None:
        handler = RustHandler()
        code = b"#[derive(Debug, Clone)]\n#[allow(dead_code)]\nstruct MyStruct {}"
        tree = rust_parser.parse(code)
        struct_node = next(
            c for c in tree.root_node.children if c.type == cs.TS_RS_STRUCT_ITEM
        )

        result = handler.extract_decorators(struct_node)
        assert len(result) == 2
        assert any("derive" in attr for attr in result)
        assert any("allow" in attr for attr in result)

    def test_extract_decorators_function_attribute(self, rust_parser: Parser) -> None:
        handler = RustHandler()
        code = b"#[test]\nfn my_test() {}"
        tree = rust_parser.parse(code)
        func_node = next(
            c for c in tree.root_node.children if c.type == cs.TS_RS_FUNCTION_ITEM
        )

        result = handler.extract_decorators(func_node)
        assert any("test" in attr for attr in result)

    def test_extract_decorators_no_attributes(self, rust_parser: Parser) -> None:
        handler = RustHandler()
        code = b"fn my_function() {}"
        tree = rust_parser.parse(code)
        func_node = tree.root_node.children[0]

        result = handler.extract_decorators(func_node)
        assert result == []

    def test_extract_decorators_inner_attribute(self, rust_parser: Parser) -> None:
        handler = RustHandler()
        code = b"mod my_module {\n    #![allow(dead_code)]\n}"
        tree = rust_parser.parse(code)
        mod_node = tree.root_node.children[0]

        result = handler.extract_decorators(mod_node)
        assert any("allow" in attr for attr in result)


@pytest.mark.skipif(not JAVA_AVAILABLE, reason="tree-sitter-java not available")
class TestJavaHandler:
    def test_build_method_qualified_name_with_params(self, java_parser: Parser) -> None:
        handler = JavaHandler()
        code = b"""
class MyClass {
    void myMethod(int a, String b) {}
}
"""
        tree = java_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        method_node = class_body.children[1]

        result = handler.build_method_qualified_name(
            class_qn="project.module.MyClass",
            method_name="myMethod",
            method_node=method_node,
        )
        assert "myMethod" in result
        assert "int" in result
        assert "String" in result

    def test_build_method_qualified_name_without_params(
        self, java_parser: Parser
    ) -> None:
        handler = JavaHandler()
        code = b"""
class MyClass {
    void myMethod() {}
}
"""
        tree = java_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        method_node = class_body.children[1]

        result = handler.build_method_qualified_name(
            class_qn="project.module.MyClass",
            method_name="myMethod",
            method_node=method_node,
        )
        assert result == "project.module.MyClass.myMethod"

    def test_build_method_qualified_name_overloaded_methods(
        self, java_parser: Parser
    ) -> None:
        handler = JavaHandler()
        code = b"""
class MyClass {
    void process(int x) {}
    void process(String s) {}
}
"""
        tree = java_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        method1 = class_body.children[1]
        method2 = class_body.children[2]

        result1 = handler.build_method_qualified_name(
            class_qn="project.module.MyClass",
            method_name="process",
            method_node=method1,
        )
        result2 = handler.build_method_qualified_name(
            class_qn="project.module.MyClass",
            method_name="process",
            method_node=method2,
        )
        assert result1 != result2
        assert "int" in result1
        assert "String" in result2

    def test_extract_decorators_single_annotation(self, java_parser: Parser) -> None:
        handler = JavaHandler()
        code = b"""
class MyClass {
    @Override
    void myMethod() {}
}
"""
        tree = java_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        method_node = class_body.children[1]

        result = handler.extract_decorators(method_node)
        assert "@Override" in result

    def test_extract_decorators_multiple_annotations(self, java_parser: Parser) -> None:
        handler = JavaHandler()
        code = b"""
class MyClass {
    @Override
    @Deprecated
    void myMethod() {}
}
"""
        tree = java_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        method_node = class_body.children[1]

        result = handler.extract_decorators(method_node)
        assert len(result) == 2
        assert "@Override" in result
        assert "@Deprecated" in result

    def test_extract_decorators_parameterized_annotation(
        self, java_parser: Parser
    ) -> None:
        handler = JavaHandler()
        code = b"""
class MyClass {
    @SuppressWarnings("unchecked")
    void myMethod() {}
}
"""
        tree = java_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        method_node = class_body.children[1]

        result = handler.extract_decorators(method_node)
        assert any("SuppressWarnings" in ann for ann in result)

    def test_extract_decorators_class_annotation(self, java_parser: Parser) -> None:
        handler = JavaHandler()
        code = b"""
@Deprecated
public class MyClass {}
"""
        tree = java_parser.parse(code)
        class_node = tree.root_node.children[0]

        result = handler.extract_decorators(class_node)
        assert "@Deprecated" in result

    def test_extract_decorators_no_annotations(self, java_parser: Parser) -> None:
        handler = JavaHandler()
        code = b"""
class MyClass {
    void myMethod() {}
}
"""
        tree = java_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        method_node = class_body.children[1]

        result = handler.extract_decorators(method_node)
        assert result == []


@pytest.mark.skipif(not LUA_AVAILABLE, reason="tree-sitter-lua not available")
class TestLuaHandler:
    def test_extract_function_name_with_name_field(self, lua_parser: Parser) -> None:
        handler = LuaHandler()
        code = b"function myFunc() end"
        tree = lua_parser.parse(code)
        func_node = tree.root_node.children[0]

        result = handler.extract_function_name(func_node)
        assert result == "myFunc"

    def test_extract_function_name_assigned_to_identifier(
        self, lua_parser: Parser
    ) -> None:
        handler = LuaHandler()
        code = b"myFunc = function() end"
        tree = lua_parser.parse(code)
        assignment = tree.root_node.children[0]

        def find_function_definition(node: ASTNode) -> ASTNode | None:
            if node.type == cs.TS_LUA_FUNCTION_DEFINITION:
                return node
            for child in node.children:
                if result := find_function_definition(child):
                    return result
            return None

        func_node = find_function_definition(assignment)
        assert func_node is not None

        result = handler.extract_function_name(func_node)
        assert result == "myFunc"

    def test_extract_function_name_assigned_to_dot_index(
        self, lua_parser: Parser
    ) -> None:
        handler = LuaHandler()
        code = b"MyModule.myFunc = function() end"
        tree = lua_parser.parse(code)
        assignment = tree.root_node.children[0]

        def find_function_definition(node: ASTNode) -> ASTNode | None:
            if node.type == cs.TS_LUA_FUNCTION_DEFINITION:
                return node
            for child in node.children:
                if result := find_function_definition(child):
                    return result
            return None

        func_node = find_function_definition(assignment)
        assert func_node is not None

        result = handler.extract_function_name(func_node)
        assert result is not None
        assert "myFunc" in result

    def test_extract_function_name_anonymous_returns_none(
        self, lua_parser: Parser
    ) -> None:
        handler = LuaHandler()
        code = b"doSomething(function() end)"
        tree = lua_parser.parse(code)

        def find_function_definition(node: ASTNode) -> ASTNode | None:
            if node.type == cs.TS_LUA_FUNCTION_DEFINITION:
                return node
            for child in node.children:
                if result := find_function_definition(child):
                    return result
            return None

        func_node = find_function_definition(tree.root_node)
        assert func_node is not None

        result = handler.extract_function_name(func_node)
        assert result is None


@pytest.mark.skipif(not PYTHON_AVAILABLE, reason="tree-sitter-python not available")
class TestPythonHandler:
    def test_extract_decorators_simple_identifier(self, python_parser: Parser) -> None:
        handler = PythonHandler()
        code = b"@my_decorator\ndef func(): pass"
        tree = python_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        func_node = next(
            c for c in decorated_def.children if c.type == cs.TS_PY_FUNCTION_DEFINITION
        )

        result = handler.extract_decorators(func_node)
        assert result == ["@my_decorator"]

    def test_extract_decorators_call_decorator(self, python_parser: Parser) -> None:
        handler = PythonHandler()
        code = b"@decorator_factory(arg1, arg2)\ndef func(): pass"
        tree = python_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        func_node = next(
            c for c in decorated_def.children if c.type == cs.TS_PY_FUNCTION_DEFINITION
        )

        result = handler.extract_decorators(func_node)
        assert result == ["@decorator_factory(arg1, arg2)"]

    def test_extract_decorators_dotted_decorator(self, python_parser: Parser) -> None:
        handler = PythonHandler()
        code = b"@module.submodule.decorator\ndef func(): pass"
        tree = python_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        func_node = next(
            c for c in decorated_def.children if c.type == cs.TS_PY_FUNCTION_DEFINITION
        )

        result = handler.extract_decorators(func_node)
        assert result == ["@module.submodule.decorator"]

    def test_extract_decorators_multiple(self, python_parser: Parser) -> None:
        handler = PythonHandler()
        code = b"@first\n@second\n@third\ndef func(): pass"
        tree = python_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        func_node = next(
            c for c in decorated_def.children if c.type == cs.TS_PY_FUNCTION_DEFINITION
        )

        result = handler.extract_decorators(func_node)
        assert len(result) == 3
        assert "@first" in result
        assert "@second" in result
        assert "@third" in result

    def test_extract_decorators_no_decorators(self, python_parser: Parser) -> None:
        handler = PythonHandler()
        code = b"def func(): pass"
        tree = python_parser.parse(code)
        func_node = tree.root_node.children[0]

        result = handler.extract_decorators(func_node)
        assert result == []

    def test_extract_decorators_class_decorator(self, python_parser: Parser) -> None:
        handler = PythonHandler()
        code = b"@dataclass\nclass MyClass: pass"
        tree = python_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        class_node = next(
            c for c in decorated_def.children if c.type == cs.TS_PY_CLASS_DEFINITION
        )

        result = handler.extract_decorators(class_node)
        assert result == ["@dataclass"]

    def test_extract_decorators_with_args(self, python_parser: Parser) -> None:
        handler = PythonHandler()
        code = (
            b"@app.route('/api/users', methods=['GET', 'POST'])\ndef get_users(): pass"
        )
        tree = python_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        func_node = next(
            c for c in decorated_def.children if c.type == cs.TS_PY_FUNCTION_DEFINITION
        )

        result = handler.extract_decorators(func_node)
        assert len(result) == 1
        assert "@app.route('/api/users', methods=['GET', 'POST'])" in result[0]

    def test_extract_decorators_dataclass_with_options(
        self, python_parser: Parser
    ) -> None:
        handler = PythonHandler()
        code = b"@dataclass(frozen=True, slots=True)\nclass Config: pass"
        tree = python_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        class_node = next(
            c for c in decorated_def.children if c.type == cs.TS_PY_CLASS_DEFINITION
        )

        result = handler.extract_decorators(class_node)
        assert result == ["@dataclass(frozen=True, slots=True)"]
