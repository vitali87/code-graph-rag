from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.constants import SEPARATOR_DOT
from codebase_rag.tests.conftest import get_nodes, run_updater

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from codebase_rag.types_defs import PropertyDict


def _get_line_span(props: PropertyDict) -> int:
    start = props.get("start_line", 0)
    end = props.get("end_line", 0)
    if not isinstance(start, int) or not isinstance(end, int):
        return 0
    return end - start


def _get_method_name(props: PropertyDict) -> str:
    name = props.get("name", "")
    return name if isinstance(name, str) else ""


def _get_start_line(props: PropertyDict) -> int:
    start = props.get("start_line", 0)
    return start if isinstance(start, int) else 0


def _select_definition_props(candidates: list[PropertyDict]) -> PropertyDict | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=_get_line_span)


def _get_method_props(
    mock_ingestor: MagicMock, class_name: str, method_name: str
) -> PropertyDict | None:
    method_calls = get_nodes(mock_ingestor, "Method")
    candidates: list[PropertyDict] = []
    for call in method_calls:
        props = call[0][1]
        qn = props.get("qualified_name", "")
        if not isinstance(qn, str):
            continue
        parts = qn.split(SEPARATOR_DOT)
        if class_name in parts and parts[-1] == method_name:
            candidates.append(props)
    return _select_definition_props(candidates)


def _get_all_methods(mock_ingestor: MagicMock) -> list[PropertyDict]:
    method_calls = get_nodes(mock_ingestor, "Method")
    methods_by_qn: dict[str, list[PropertyDict]] = {}
    for call in method_calls:
        props = call[0][1]
        qn = props.get("qualified_name", "")
        if not isinstance(qn, str):
            continue
        if qn not in methods_by_qn:
            methods_by_qn[qn] = []
        methods_by_qn[qn].append(props)
    return [
        selected
        for candidates in methods_by_qn.values()
        if (selected := _select_definition_props(candidates))
    ]


@pytest.fixture
def cpp_line_numbers_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "cpp_line_numbers_test"
    project_path.mkdir()
    return project_path


class TestIssue194OutOfClassLineNumbers:
    def test_simple_out_of_class_method_has_definition_line_numbers(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "simple.cpp"
        test_file.write_text(
            """\
class QCMakeCacheView {
public:
    bool setSearchFilter(QString const& s);
};

bool QCMakeCacheView::setSearchFilter(QString const& s)
{
    return true;
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        props = _get_method_props(mock_ingestor, "QCMakeCacheView", "setSearchFilter")

        assert props is not None, "Method should exist"
        assert props["start_line"] == 6, (
            f"Expected start_line=6 (definition), got {props['start_line']}"
        )
        assert props["end_line"] == 9, (
            f"Expected end_line=9 (definition), got {props['end_line']}"
        )

    def test_multiple_out_of_class_methods_have_correct_lines(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "multiple.cpp"
        test_file.write_text(
            """\
class Calculator {
public:
    int add(int a, int b);
    int subtract(int a, int b);
    int multiply(int a, int b);
};

int Calculator::add(int a, int b) {
    return a + b;
}

int Calculator::subtract(int a, int b) {
    return a - b;
}

int Calculator::multiply(int a, int b) {
    return a * b;
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        add_props = _get_method_props(mock_ingestor, "Calculator", "add")
        subtract_props = _get_method_props(mock_ingestor, "Calculator", "subtract")
        multiply_props = _get_method_props(mock_ingestor, "Calculator", "multiply")

        assert add_props is not None
        assert add_props["start_line"] == 8
        assert add_props["end_line"] == 10

        assert subtract_props is not None
        assert subtract_props["start_line"] == 12
        assert subtract_props["end_line"] == 14

        assert multiply_props is not None
        assert multiply_props["start_line"] == 16
        assert multiply_props["end_line"] == 18

    def test_constructor_out_of_class_has_definition_line_numbers(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "constructor.cpp"
        test_file.write_text(
            """\
class MyClass {
public:
    MyClass(int value);
private:
    int value_;
};

MyClass::MyClass(int value) : value_(value) {
    // constructor body
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        props = _get_method_props(mock_ingestor, "MyClass", "MyClass")

        assert props is not None, "Constructor should exist"
        assert props["start_line"] == 8, (
            f"Expected start_line=8 (definition), got {props['start_line']}"
        )
        assert props["end_line"] == 10, (
            f"Expected end_line=10 (definition), got {props['end_line']}"
        )

    def test_destructor_out_of_class_has_definition_line_numbers(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "destructor.cpp"
        test_file.write_text(
            """\
class Resource {
public:
    ~Resource();
private:
    int* data_;
};

Resource::~Resource() {
    delete data_;
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        props = _get_method_props(mock_ingestor, "Resource", "~Resource")

        assert props is not None, "Destructor should exist"
        assert props["start_line"] == 8, (
            f"Expected start_line=8 (definition), got {props['start_line']}"
        )
        assert props["end_line"] == 10, (
            f"Expected end_line=10 (definition), got {props['end_line']}"
        )


class TestTemplateOutOfClassLineNumbers:
    def test_template_method_has_definition_line_numbers(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "template.cpp"
        test_file.write_text(
            """\
template<typename T>
class Container {
public:
    void add(const T& item);
    T get(int index) const;
};

template<typename T>
void Container<T>::add(const T& item) {
    // add implementation
}

template<typename T>
T Container<T>::get(int index) const {
    return T();
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        add_props = _get_method_props(mock_ingestor, "Container", "add")
        get_props = _get_method_props(mock_ingestor, "Container", "get")

        assert add_props is not None, "add method should exist"
        assert add_props["start_line"] == 8, (
            f"Expected start_line=8, got {add_props['start_line']}"
        )
        assert add_props["end_line"] == 11, (
            f"Expected end_line=11, got {add_props['end_line']}"
        )

        assert get_props is not None, "get method should exist"
        assert get_props["start_line"] == 13, (
            f"Expected start_line=13, got {get_props['start_line']}"
        )
        assert get_props["end_line"] == 16, (
            f"Expected end_line=16, got {get_props['end_line']}"
        )


class TestInlineMethodLineNumbers:
    def test_inline_method_has_correct_line_numbers(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "inline.cpp"
        test_file.write_text(
            """\
class Simple {
public:
    int getValue() const { return value_; }
    void setValue(int v) { value_ = v; }
private:
    int value_;
};
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        get_props = _get_method_props(mock_ingestor, "Simple", "getValue")
        set_props = _get_method_props(mock_ingestor, "Simple", "setValue")

        assert get_props is not None
        assert get_props["start_line"] == 3
        assert get_props["end_line"] == 3

        assert set_props is not None
        assert set_props["start_line"] == 4
        assert set_props["end_line"] == 4

    def test_multiline_inline_method_has_correct_lines(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "multiline_inline.cpp"
        test_file.write_text(
            """\
class Complex {
public:
    int compute(int x) {
        int result = x * 2;
        result += 10;
        return result;
    }
};
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        props = _get_method_props(mock_ingestor, "Complex", "compute")

        assert props is not None
        assert props["start_line"] == 3
        assert props["end_line"] == 7


class TestMixedInlineAndOutOfClassLineNumbers:
    def test_mixed_methods_have_correct_line_numbers(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "mixed.cpp"
        test_file.write_text(
            """\
class MixedClass {
public:
    int inlineMethod() { return 42; }
    void outOfClassMethod();
    double anotherInline() const { return 3.14; }
};

void MixedClass::outOfClassMethod() {
    // implementation
    return;
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        inline1_props = _get_method_props(mock_ingestor, "MixedClass", "inlineMethod")
        out_of_class_props = _get_method_props(
            mock_ingestor, "MixedClass", "outOfClassMethod"
        )
        inline2_props = _get_method_props(mock_ingestor, "MixedClass", "anotherInline")

        assert inline1_props is not None
        assert inline1_props["start_line"] == 3
        assert inline1_props["end_line"] == 3

        assert out_of_class_props is not None
        assert out_of_class_props["start_line"] == 8
        assert out_of_class_props["end_line"] == 11

        assert inline2_props is not None
        assert inline2_props["start_line"] == 5
        assert inline2_props["end_line"] == 5


class TestNestedClassOutOfClassLineNumbers:
    def test_nested_class_method_has_definition_line_numbers(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "nested.cpp"
        test_file.write_text(
            """\
class Outer {
public:
    class Inner {
    public:
        void innerMethod();
    };
    void outerMethod();
};

void Outer::outerMethod() {
    // outer implementation
}

void Outer::Inner::innerMethod() {
    // inner implementation
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        outer_props = _get_method_props(mock_ingestor, "Outer", "outerMethod")
        inner_props = _get_method_props(mock_ingestor, "Inner", "innerMethod")

        assert outer_props is not None
        assert outer_props["start_line"] == 10
        assert outer_props["end_line"] == 12

        assert inner_props is not None
        assert inner_props["start_line"] == 14
        assert inner_props["end_line"] == 16


class TestNamespacedClassOutOfClassLineNumbers:
    def test_namespaced_class_method_has_definition_line_numbers(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "namespaced.cpp"
        test_file.write_text(
            """\
namespace MyNamespace {

class MyClass {
public:
    void method();
};

void MyClass::method() {
    // implementation
}

}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        props = _get_method_props(mock_ingestor, "MyClass", "method")

        assert props is not None
        assert props["start_line"] == 8
        assert props["end_line"] == 10

    def test_deeply_nested_namespace_has_correct_lines(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "deep_namespace.cpp"
        test_file.write_text(
            """\
namespace Level1 {
namespace Level2 {
namespace Level3 {

class DeepClass {
public:
    void deepMethod();
};

void DeepClass::deepMethod() {
    // deep implementation
}

}
}
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        props = _get_method_props(mock_ingestor, "DeepClass", "deepMethod")

        assert props is not None
        assert props["start_line"] == 10
        assert props["end_line"] == 12


class TestOperatorOverloadingLineNumbers:
    def test_operator_methods_have_definition_line_numbers(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "operators.cpp"
        test_file.write_text(
            """\
class Vector {
public:
    Vector operator+(const Vector& other) const;
    Vector operator-(const Vector& other) const;
    bool operator==(const Vector& other) const;
};

Vector Vector::operator+(const Vector& other) const {
    return Vector();
}

Vector Vector::operator-(const Vector& other) const {
    return Vector();
}

bool Vector::operator==(const Vector& other) const {
    return true;
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        methods = _get_all_methods(mock_ingestor)
        operator_methods = [
            m for m in methods if "operator" in _get_method_name(m).lower()
        ]

        assert len(operator_methods) >= 3, (
            f"Expected at least 3 operator methods, got {len(operator_methods)}"
        )

        for method in operator_methods:
            start_line = _get_start_line(method)
            assert start_line >= 8, (
                f"Operator method should have definition line >= 8, got {start_line}"
            )


class TestDeclarationOnlyMethods:
    def test_declaration_only_methods_have_declaration_line_numbers(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "declaration_only.cpp"
        test_file.write_text(
            """\
class HeaderOnlyClass {
public:
    void method1();
    int method2(int x);
    void method3(const std::string& s);
};
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        method1_props = _get_method_props(mock_ingestor, "HeaderOnlyClass", "method1")
        method2_props = _get_method_props(mock_ingestor, "HeaderOnlyClass", "method2")
        method3_props = _get_method_props(mock_ingestor, "HeaderOnlyClass", "method3")

        assert method1_props is not None
        assert method1_props["start_line"] == 3
        assert method1_props["end_line"] == 3

        assert method2_props is not None
        assert method2_props["start_line"] == 4
        assert method2_props["end_line"] == 4

        assert method3_props is not None
        assert method3_props["start_line"] == 5
        assert method3_props["end_line"] == 5


class TestStructMethodLineNumbers:
    def test_struct_out_of_class_method_has_definition_lines(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "struct_methods.cpp"
        test_file.write_text(
            """\
struct Point {
    double x, y;
    double distance(const Point& other) const;
    Point operator+(const Point& other) const;
};

double Point::distance(const Point& other) const {
    double dx = x - other.x;
    double dy = y - other.y;
    return dx * dx + dy * dy;
}

Point Point::operator+(const Point& other) const {
    return Point{x + other.x, y + other.y};
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        distance_props = _get_method_props(mock_ingestor, "Point", "distance")

        assert distance_props is not None
        assert distance_props["start_line"] == 7
        assert distance_props["end_line"] == 11


class TestConstAndStaticMethodLineNumbers:
    def test_const_method_has_correct_lines(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "const_method.cpp"
        test_file.write_text(
            """\
class ConstExample {
public:
    int getValue() const;
    void setValue(int v);
private:
    int value_;
};

int ConstExample::getValue() const {
    return value_;
}

void ConstExample::setValue(int v) {
    value_ = v;
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        get_props = _get_method_props(mock_ingestor, "ConstExample", "getValue")
        set_props = _get_method_props(mock_ingestor, "ConstExample", "setValue")

        assert get_props is not None
        assert get_props["start_line"] == 9
        assert get_props["end_line"] == 11

        assert set_props is not None
        assert set_props["start_line"] == 13
        assert set_props["end_line"] == 15

    def test_static_method_has_correct_lines(
        self,
        cpp_line_numbers_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        test_file = cpp_line_numbers_project / "static_method.cpp"
        test_file.write_text(
            """\
class StaticExample {
public:
    static int getCount();
    static void resetCount();
private:
    static int count_;
};

int StaticExample::getCount() {
    return count_;
}

void StaticExample::resetCount() {
    count_ = 0;
}
"""
        )

        run_updater(cpp_line_numbers_project, mock_ingestor)

        get_props = _get_method_props(mock_ingestor, "StaticExample", "getCount")
        reset_props = _get_method_props(mock_ingestor, "StaticExample", "resetCount")

        assert get_props is not None
        assert get_props["start_line"] == 9
        assert get_props["end_line"] == 11

        assert reset_props is not None
        assert reset_props["start_line"] == 13
        assert reset_props["end_line"] == 15
