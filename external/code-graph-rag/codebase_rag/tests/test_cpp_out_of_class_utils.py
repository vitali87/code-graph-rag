from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp import utils as cpp_utils


@pytest.fixture(scope="module")
def cpp_parser():
    parsers, _ = load_parsers()
    return parsers.get(cs.SupportedLanguage.CPP)


def parse_cpp(cpp_parser, code: str):
    return cpp_parser.parse(code.encode())


def find_nodes_by_type(node, target_type: str) -> list:
    results = []
    if node.type == target_type:
        results.append(node)
    for child in node.children:
        results.extend(find_nodes_by_type(child, target_type))
    return results


class TestIsOutOfClassMethodDefinition:
    def test_simple_out_of_class_method(self, cpp_parser) -> None:
        code = """\
class Foo {
    void bar();
};

void Foo::bar() {
    return;
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        assert cpp_utils.is_out_of_class_method_definition(func_defs[0]) is True

    def test_inline_method_not_out_of_class(self, cpp_parser) -> None:
        code = """\
class Foo {
    void bar() { return; }
};
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        assert cpp_utils.is_out_of_class_method_definition(func_defs[0]) is False

    def test_standalone_function_not_out_of_class(self, cpp_parser) -> None:
        code = """\
void standalone() {
    return;
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        assert cpp_utils.is_out_of_class_method_definition(func_defs[0]) is False

    def test_namespaced_function_not_out_of_class(self, cpp_parser) -> None:
        code = """\
namespace MyNs {
void func() {
    return;
}
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        assert cpp_utils.is_out_of_class_method_definition(func_defs[0]) is False

    def test_template_out_of_class_method(self, cpp_parser) -> None:
        code = """\
template<typename T>
class Container {
    void add(const T& item);
};

template<typename T>
void Container<T>::add(const T& item) {
    // impl
}
"""
        tree = parse_cpp(cpp_parser, code)
        template_decls = find_nodes_by_type(tree.root_node, "template_declaration")

        method_template = None
        for td in template_decls:
            func_defs = find_nodes_by_type(td, "function_definition")
            if func_defs:
                method_template = td
                break

        assert method_template is not None
        assert cpp_utils.is_out_of_class_method_definition(method_template) is True

    def test_nested_class_out_of_class_method(self, cpp_parser) -> None:
        code = """\
class Outer {
    class Inner {
        void method();
    };
};

void Outer::Inner::method() {
    return;
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        outer_func_defs = [f for f in func_defs if f.parent.type == "translation_unit"]
        assert len(outer_func_defs) == 1
        assert cpp_utils.is_out_of_class_method_definition(outer_func_defs[0]) is True

    def test_lambda_not_out_of_class(self, cpp_parser) -> None:
        code = """\
auto fn = []() { return 42; };
"""
        tree = parse_cpp(cpp_parser, code)
        lambdas = find_nodes_by_type(tree.root_node, "lambda_expression")

        assert len(lambdas) == 1
        assert cpp_utils.is_out_of_class_method_definition(lambdas[0]) is False


class TestExtractClassNameFromOutOfClassMethod:
    def test_simple_class_name(self, cpp_parser) -> None:
        code = """\
class Calculator {
    int add(int a, int b);
};

int Calculator::add(int a, int b) {
    return a + b;
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        class_name = cpp_utils.extract_class_name_from_out_of_class_method(func_defs[0])
        assert class_name == "Calculator"

    def test_nested_class_name(self, cpp_parser) -> None:
        code = """\
class Outer {
    class Inner {
        void method();
    };
};

void Outer::Inner::method() {
    return;
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        outer_func_defs = [f for f in func_defs if f.parent.type == "translation_unit"]
        assert len(outer_func_defs) == 1
        class_name = cpp_utils.extract_class_name_from_out_of_class_method(
            outer_func_defs[0]
        )
        assert class_name == "Outer::Inner"

    def test_template_class_name(self, cpp_parser) -> None:
        code = """\
template<typename T>
class Container {
    void add(const T& item);
};

template<typename T>
void Container<T>::add(const T& item) {
    // impl
}
"""
        tree = parse_cpp(cpp_parser, code)
        template_decls = find_nodes_by_type(tree.root_node, "template_declaration")

        method_template = None
        for td in template_decls:
            func_defs = find_nodes_by_type(td, "function_definition")
            if func_defs:
                method_template = td
                break

        assert method_template is not None
        class_name = cpp_utils.extract_class_name_from_out_of_class_method(
            method_template
        )
        assert class_name == "Container"

    def test_struct_class_name(self, cpp_parser) -> None:
        code = """\
struct Point {
    double distance(const Point& other) const;
};

double Point::distance(const Point& other) const {
    return 0.0;
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        class_name = cpp_utils.extract_class_name_from_out_of_class_method(func_defs[0])
        assert class_name == "Point"

    def test_returns_none_for_standalone_function(self, cpp_parser) -> None:
        code = """\
void standalone() {
    return;
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        class_name = cpp_utils.extract_class_name_from_out_of_class_method(func_defs[0])
        assert class_name is None

    def test_returns_none_for_inline_method(self, cpp_parser) -> None:
        code = """\
class Foo {
    void bar() { return; }
};
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        class_name = cpp_utils.extract_class_name_from_out_of_class_method(func_defs[0])
        assert class_name is None


class TestExtractFunctionNameForOutOfClass:
    def test_simple_method_name(self, cpp_parser) -> None:
        code = """\
class Foo {
    void bar();
};

void Foo::bar() {
    return;
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        method_name = cpp_utils.extract_function_name(func_defs[0])
        assert method_name == "bar"

    def test_constructor_name(self, cpp_parser) -> None:
        code = """\
class MyClass {
    MyClass();
};

MyClass::MyClass() {
    // impl
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        method_name = cpp_utils.extract_function_name(func_defs[0])
        assert method_name == "MyClass"

    def test_destructor_name(self, cpp_parser) -> None:
        code = """\
class MyClass {
    ~MyClass();
};

MyClass::~MyClass() {
    // impl
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        method_name = cpp_utils.extract_function_name(func_defs[0])
        assert method_name == "~MyClass"

    def test_operator_plus_name(self, cpp_parser) -> None:
        code = """\
class Vector {
    Vector operator+(const Vector& other) const;
};

Vector Vector::operator+(const Vector& other) const {
    return Vector();
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        method_name = cpp_utils.extract_function_name(func_defs[0])
        assert "operator" in method_name

    def test_template_method_name(self, cpp_parser) -> None:
        code = """\
template<typename T>
class Container {
    void push(const T& item);
};

template<typename T>
void Container<T>::push(const T& item) {
    // impl
}
"""
        tree = parse_cpp(cpp_parser, code)
        template_decls = find_nodes_by_type(tree.root_node, "template_declaration")

        method_template = None
        for td in template_decls:
            func_defs = find_nodes_by_type(td, "function_definition")
            if func_defs:
                method_template = td
                break

        assert method_template is not None
        method_name = cpp_utils.extract_function_name(method_template)
        assert method_name == "push"


class TestGetInnerFunctionNode:
    def test_returns_same_node_for_function_definition(self, cpp_parser) -> None:
        code = """\
void Foo::bar() {
    return;
}
"""
        tree = parse_cpp(cpp_parser, code)
        func_defs = find_nodes_by_type(tree.root_node, "function_definition")

        assert len(func_defs) == 1
        inner = cpp_utils._get_inner_function_node(func_defs[0])
        assert inner == func_defs[0]

    def test_returns_inner_function_for_template(self, cpp_parser) -> None:
        code = """\
template<typename T>
void Container<T>::add(const T& item) {
    // impl
}
"""
        tree = parse_cpp(cpp_parser, code)
        template_decls = find_nodes_by_type(tree.root_node, "template_declaration")

        assert len(template_decls) == 1
        inner = cpp_utils._get_inner_function_node(template_decls[0])
        assert inner.type == "function_definition"
        assert inner != template_decls[0]
