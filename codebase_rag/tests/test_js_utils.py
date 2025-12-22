import pytest
from tree_sitter import Language, Parser

from codebase_rag.parsers.js_utils import (
    analyze_js_return_expression,
    extract_js_constructor_name,
    extract_js_method_call,
    find_js_method_in_ast,
    find_js_method_in_class_body,
    find_js_return_statements,
)

try:
    import tree_sitter_javascript as tsjs

    JS_AVAILABLE = True
except ImportError:
    JS_AVAILABLE = False


@pytest.fixture
def js_parser() -> Parser | None:
    if not JS_AVAILABLE:
        return None
    language = Language(tsjs.language())
    return Parser(language)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestExtractJsMethodCall:
    def test_simple_method_call(self, js_parser: Parser) -> None:
        code = b"Storage.getInstance();"
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        call_expr = expr_stmt.children[0]
        member_expr = call_expr.child_by_field_name("function")
        assert member_expr is not None

        result = extract_js_method_call(member_expr)
        assert result == "Storage.getInstance"

    def test_chained_method_call(self, js_parser: Parser) -> None:
        code = b"obj.method1().method2();"
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        call_expr = expr_stmt.children[0]
        member_expr = call_expr.child_by_field_name("function")
        assert member_expr is not None

        result = extract_js_method_call(member_expr)
        assert result is not None
        assert "method2" in result

    def test_property_access_without_call(self, js_parser: Parser) -> None:
        code = b"obj.property;"
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        member_expr = expr_stmt.children[0]

        result = extract_js_method_call(member_expr)
        assert result == "obj.property"

    def test_nested_object_access(self, js_parser: Parser) -> None:
        code = b"a.b.c;"
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        member_expr = expr_stmt.children[0]

        result = extract_js_method_call(member_expr)
        assert result is not None

    def test_non_member_expression_returns_none(self, js_parser: Parser) -> None:
        code = b"simpleFunction();"
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        call_expr = expr_stmt.children[0]

        result = extract_js_method_call(call_expr)
        assert result is None


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestFindJsMethodInClassBody:
    def test_finds_existing_method(self, js_parser: Parser) -> None:
        code = b"""
class MyClass {
    myMethod() {
        return 'hello';
    }
}
"""
        tree = js_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        assert class_body is not None

        result = find_js_method_in_class_body(class_body, "myMethod")
        assert result is not None
        assert result.type == "method_definition"

    def test_finds_constructor(self, js_parser: Parser) -> None:
        code = b"""
class MyClass {
    constructor() {
        this.value = 0;
    }
}
"""
        tree = js_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        assert class_body is not None

        result = find_js_method_in_class_body(class_body, "constructor")
        assert result is not None

    def test_finds_static_method(self, js_parser: Parser) -> None:
        code = b"""
class MyClass {
    static getInstance() {
        return new MyClass();
    }
}
"""
        tree = js_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        assert class_body is not None

        result = find_js_method_in_class_body(class_body, "getInstance")
        assert result is not None

    def test_returns_none_for_nonexistent_method(self, js_parser: Parser) -> None:
        code = b"""
class MyClass {
    existingMethod() {}
}
"""
        tree = js_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        assert class_body is not None

        result = find_js_method_in_class_body(class_body, "nonExistentMethod")
        assert result is None

    def test_multiple_methods_finds_correct_one(self, js_parser: Parser) -> None:
        code = b"""
class MyClass {
    methodA() { return 'A'; }
    methodB() { return 'B'; }
    methodC() { return 'C'; }
}
"""
        tree = js_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        assert class_body is not None

        result = find_js_method_in_class_body(class_body, "methodB")
        assert result is not None


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestFindJsMethodInAst:
    def test_finds_method_in_class(self, js_parser: Parser) -> None:
        code = b"""
class Storage {
    getInstance() {
        return this;
    }
}
"""
        tree = js_parser.parse(code)

        result = find_js_method_in_ast(tree.root_node, "Storage", "getInstance")
        assert result is not None
        assert result.type == "method_definition"

    def test_finds_method_in_nested_structure(self, js_parser: Parser) -> None:
        code = b"""
function outer() {
    class Inner {
        innerMethod() {
            return 'inner';
        }
    }
}
"""
        tree = js_parser.parse(code)

        result = find_js_method_in_ast(tree.root_node, "Inner", "innerMethod")
        assert result is not None

    def test_returns_none_for_nonexistent_class(self, js_parser: Parser) -> None:
        code = b"""
class ExistingClass {
    method() {}
}
"""
        tree = js_parser.parse(code)

        result = find_js_method_in_ast(tree.root_node, "NonExistent", "method")
        assert result is None

    def test_returns_none_for_nonexistent_method(self, js_parser: Parser) -> None:
        code = b"""
class MyClass {
    existingMethod() {}
}
"""
        tree = js_parser.parse(code)

        result = find_js_method_in_ast(tree.root_node, "MyClass", "nonExistent")
        assert result is None

    def test_multiple_classes_finds_correct_one(self, js_parser: Parser) -> None:
        code = b"""
class ClassA {
    methodA() { return 'A'; }
}
class ClassB {
    methodB() { return 'B'; }
}
"""
        tree = js_parser.parse(code)

        result = find_js_method_in_ast(tree.root_node, "ClassB", "methodB")
        assert result is not None


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestFindJsReturnStatements:
    def test_finds_single_return(self, js_parser: Parser) -> None:
        code = b"""
function myFunc() {
    return 42;
}
"""
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]

        return_nodes: list = []
        find_js_return_statements(func_node, return_nodes)
        assert len(return_nodes) == 1
        assert return_nodes[0].type == "return_statement"

    def test_finds_multiple_returns(self, js_parser: Parser) -> None:
        code = b"""
function myFunc(x) {
    if (x > 0) {
        return 'positive';
    } else if (x < 0) {
        return 'negative';
    }
    return 'zero';
}
"""
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]

        return_nodes: list = []
        find_js_return_statements(func_node, return_nodes)
        assert len(return_nodes) == 3

    def test_finds_nested_returns(self, js_parser: Parser) -> None:
        code = b"""
function outer() {
    function inner() {
        return 'inner';
    }
    return inner();
}
"""
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]

        return_nodes: list = []
        find_js_return_statements(func_node, return_nodes)
        assert len(return_nodes) == 2

    def test_no_returns_empty_list(self, js_parser: Parser) -> None:
        code = b"""
function noReturn() {
    console.log('no return');
}
"""
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]

        return_nodes: list = []
        find_js_return_statements(func_node, return_nodes)
        assert len(return_nodes) == 0

    def test_empty_return(self, js_parser: Parser) -> None:
        code = b"""
function earlyExit() {
    if (true) return;
    console.log('unreachable');
}
"""
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]

        return_nodes: list = []
        find_js_return_statements(func_node, return_nodes)
        assert len(return_nodes) == 1


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestExtractJsConstructorName:
    def test_simple_new_expression(self, js_parser: Parser) -> None:
        code = b"new Storage();"
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        new_expr = expr_stmt.children[0]

        result = extract_js_constructor_name(new_expr)
        assert result == "Storage"

    def test_new_with_arguments(self, js_parser: Parser) -> None:
        code = b"new Person('John', 30);"
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        new_expr = expr_stmt.children[0]

        result = extract_js_constructor_name(new_expr)
        assert result == "Person"

    def test_new_date(self, js_parser: Parser) -> None:
        code = b"new Date();"
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        new_expr = expr_stmt.children[0]

        result = extract_js_constructor_name(new_expr)
        assert result == "Date"

    def test_non_new_expression_returns_none(self, js_parser: Parser) -> None:
        code = b"regularFunction();"
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        call_expr = expr_stmt.children[0]

        result = extract_js_constructor_name(call_expr)
        assert result is None

    def test_new_with_member_expression(self, js_parser: Parser) -> None:
        code = b"new module.ClassName();"
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        new_expr = expr_stmt.children[0]

        result = extract_js_constructor_name(new_expr)
        assert result is None


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestAnalyzeJsReturnExpression:
    def test_return_new_expression(self, js_parser: Parser) -> None:
        code = b"return new Storage();"
        tree = js_parser.parse(code)
        return_stmt = tree.root_node.children[0]
        expr_node = return_stmt.children[1]

        result = analyze_js_return_expression(expr_node, "project.Storage.getInstance")
        assert result == "project.Storage"

    def test_return_this(self, js_parser: Parser) -> None:
        code = b"return this;"
        tree = js_parser.parse(code)
        return_stmt = tree.root_node.children[0]
        expr_node = return_stmt.children[1]

        result = analyze_js_return_expression(expr_node, "project.MyClass.chainMethod")
        assert result == "project.MyClass"

    def test_return_this_property(self, js_parser: Parser) -> None:
        code = b"return this.instance;"
        tree = js_parser.parse(code)
        return_stmt = tree.root_node.children[0]
        expr_node = return_stmt.children[1]

        result = analyze_js_return_expression(
            expr_node, "project.Singleton.getInstance"
        )
        assert result == "project.Singleton"

    def test_return_class_property(self, js_parser: Parser) -> None:
        code = b"return Storage.instance;"
        tree = js_parser.parse(code)
        return_stmt = tree.root_node.children[0]
        expr_node = return_stmt.children[1]

        result = analyze_js_return_expression(expr_node, "project.Storage.getInstance")
        assert result == "project.Storage"

    def test_return_unrelated_expression(self, js_parser: Parser) -> None:
        code = b"return someVariable;"
        tree = js_parser.parse(code)
        return_stmt = tree.root_node.children[0]
        expr_node = return_stmt.children[1]

        result = analyze_js_return_expression(expr_node, "project.MyClass.method")
        assert result is None

    def test_return_literal_returns_none(self, js_parser: Parser) -> None:
        code = b"return 42;"
        tree = js_parser.parse(code)
        return_stmt = tree.root_node.children[0]
        expr_node = return_stmt.children[1]

        result = analyze_js_return_expression(expr_node, "project.MyClass.method")
        assert result is None

    def test_short_qualified_name(self, js_parser: Parser) -> None:
        code = b"return new Storage();"
        tree = js_parser.parse(code)
        return_stmt = tree.root_node.children[0]
        expr_node = return_stmt.children[1]

        result = analyze_js_return_expression(expr_node, "getInstance")
        assert result == "Storage"

    def test_return_member_with_different_class_name(self, js_parser: Parser) -> None:
        code = b"return OtherClass.instance;"
        tree = js_parser.parse(code)
        return_stmt = tree.root_node.children[0]
        expr_node = return_stmt.children[1]

        result = analyze_js_return_expression(expr_node, "project.Storage.getInstance")
        assert result is None
