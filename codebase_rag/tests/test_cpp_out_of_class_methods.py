from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_nodes, run_updater


@pytest.fixture
def cpp_out_of_class_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "cpp_out_of_class_test"
    project_path.mkdir()
    (project_path / "src").mkdir()
    return project_path


def _get_method_names(mock_ingestor: MagicMock, class_name: str) -> set[str]:
    method_calls = get_nodes(mock_ingestor, "Method")
    method_names = set()
    for call in method_calls:
        qn = call[0][1].get("qualified_name", "")
        # (H) Use precise matching to avoid "Resource" matching "AnotherResource"
        parts = qn.split(".")
        if class_name in parts:
            name_part = parts[-1]
            method_names.add(name_part)
    return method_names


def test_simple_out_of_class_method_definitions(
    cpp_out_of_class_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    test_file = cpp_out_of_class_project / "calculator.cpp"
    test_file.write_text(
        """
class Calculator {
public:
    int add(int a, int b);
    int subtract(int a, int b);
    int multiply(int a, int b);
    double divide(int a, int b);
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

double Calculator::divide(int a, int b) {
    if (b == 0) return 0;
    return static_cast<double>(a) / b;
}

void useCalculator() {
    Calculator calc;
    int sum = calc.add(5, 3);
    int diff = calc.subtract(10, 4);
    int product = calc.multiply(6, 7);
    double quotient = calc.divide(15, 3);
}
"""
    )

    run_updater(cpp_out_of_class_project, mock_ingestor)

    method_names = _get_method_names(mock_ingestor, "Calculator")

    assert "add" in method_names, f"Expected 'add' in method names, got: {method_names}"
    assert "subtract" in method_names, "Expected 'subtract' in method names"
    assert "multiply" in method_names, "Expected 'multiply' in method names"
    assert "divide" in method_names, "Expected 'divide' in method names"


def test_nested_namespace_out_of_class_methods(
    cpp_out_of_class_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    test_file = cpp_out_of_class_project / "nested_namespace.cpp"
    test_file.write_text(
        """
namespace Outer {
namespace Inner {

class MyClass {
public:
    void method1();
    int method2(int x);
    static void staticMethod();
};

void MyClass::method1() {
    // Implementation
}

int MyClass::method2(int x) {
    return x * 2;
}

void MyClass::staticMethod() {
    // Static implementation
}

} // namespace Inner
} // namespace Outer

void useNestedClass() {
    Outer::Inner::MyClass obj;
    obj.method1();
    int result = obj.method2(42);
    Outer::Inner::MyClass::staticMethod();
}
"""
    )

    run_updater(cpp_out_of_class_project, mock_ingestor)

    method_names = _get_method_names(mock_ingestor, "MyClass")

    assert "method1" in method_names, (
        f"Expected 'method1' in method names, got: {method_names}"
    )
    assert "method2" in method_names, "Expected 'method2' in method names"
    assert "staticMethod" in method_names, "Expected 'staticMethod' in method names"


def test_out_of_class_operator_overloading(
    cpp_out_of_class_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    test_file = cpp_out_of_class_project / "operators.cpp"
    test_file.write_text(
        """
class Vector2D {
public:
    double x, y;

    Vector2D(double x = 0, double y = 0);
    Vector2D operator+(const Vector2D& other) const;
    Vector2D operator-(const Vector2D& other) const;
    Vector2D operator*(double scalar) const;
    bool operator==(const Vector2D& other) const;
    double& operator[](int index);
};

Vector2D::Vector2D(double x, double y) : x(x), y(y) {}

Vector2D Vector2D::operator+(const Vector2D& other) const {
    return Vector2D(x + other.x, y + other.y);
}

Vector2D Vector2D::operator-(const Vector2D& other) const {
    return Vector2D(x - other.x, y - other.y);
}

Vector2D Vector2D::operator*(double scalar) const {
    return Vector2D(x * scalar, y * scalar);
}

bool Vector2D::operator==(const Vector2D& other) const {
    return x == other.x && y == other.y;
}

double& Vector2D::operator[](int index) {
    return (index == 0) ? x : y;
}

void useVector() {
    Vector2D v1(1, 2);
    Vector2D v2(3, 4);
    Vector2D sum = v1 + v2;
    Vector2D diff = v1 - v2;
    Vector2D scaled = v1 * 2.0;
    bool equal = v1 == v2;
    double val = v1[0];
}
"""
    )

    run_updater(cpp_out_of_class_project, mock_ingestor)

    method_names = _get_method_names(mock_ingestor, "Vector2D")
    operator_methods = [m for m in method_names if "operator" in m]

    assert len(operator_methods) >= 4, (
        f"Expected at least 4 operator definitions, got {len(operator_methods)}: {operator_methods}"
    )


def test_out_of_class_constructor_destructor(
    cpp_out_of_class_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    test_file = cpp_out_of_class_project / "ctor_dtor.cpp"
    test_file.write_text(
        """
class Resource {
public:
    Resource();
    Resource(int size);
    Resource(const Resource& other);
    ~Resource();

    void allocate(int size);
    void deallocate();

private:
    int* data_;
    int size_;
};

Resource::Resource() : data_(nullptr), size_(0) {}

Resource::Resource(int size) : size_(size) {
    data_ = new int[size];
}

Resource::Resource(const Resource& other) : size_(other.size_) {
    data_ = new int[size_];
    for (int i = 0; i < size_; ++i) {
        data_[i] = other.data_[i];
    }
}

Resource::~Resource() {
    delete[] data_;
}

void Resource::allocate(int size) {
    delete[] data_;
    size_ = size;
    data_ = new int[size];
}

void Resource::deallocate() {
    delete[] data_;
    data_ = nullptr;
    size_ = 0;
}

void useResource() {
    Resource r1;
    Resource r2(100);
    Resource r3(r2);
    r1.allocate(50);
    r1.deallocate();
}
"""
    )

    run_updater(cpp_out_of_class_project, mock_ingestor)

    method_names = _get_method_names(mock_ingestor, "Resource")

    assert "Resource" in method_names, (
        f"Expected constructor 'Resource', got: {method_names}"
    )
    assert "~Resource" in method_names, (
        f"Expected destructor '~Resource', got: {method_names}"
    )
    assert "allocate" in method_names, "Expected 'allocate' in method names"
    assert "deallocate" in method_names, "Expected 'deallocate' in method names"


def test_deeply_nested_qualified_identifier(
    cpp_out_of_class_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    test_file = cpp_out_of_class_project / "deep_nested.cpp"
    test_file.write_text(
        """
namespace Level1 {
namespace Level2 {
namespace Level3 {

class DeepClass {
public:
    void deepMethod();
    int anotherDeepMethod(int x);
};

void DeepClass::deepMethod() {
    // Deep implementation
}

int DeepClass::anotherDeepMethod(int x) {
    return x * x;
}

} // namespace Level3
} // namespace Level2
} // namespace Level1

void useDeepClass() {
    Level1::Level2::Level3::DeepClass obj;
    obj.deepMethod();
    int result = obj.anotherDeepMethod(10);
}
"""
    )

    run_updater(cpp_out_of_class_project, mock_ingestor)

    method_names = _get_method_names(mock_ingestor, "DeepClass")

    assert "deepMethod" in method_names, f"Expected 'deepMethod', got: {method_names}"
    assert "anotherDeepMethod" in method_names, "Expected 'anotherDeepMethod'"


def test_template_out_of_class_methods(
    cpp_out_of_class_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    test_file = cpp_out_of_class_project / "template_methods.cpp"
    test_file.write_text(
        """
template<typename T>
class Container {
public:
    void add(const T& item);
    T get(int index) const;
    int size() const;

private:
    T* items_;
    int size_;
    int capacity_;
};

template<typename T>
void Container<T>::add(const T& item) {
    // Implementation
}

template<typename T>
T Container<T>::get(int index) const {
    return items_[index];
}

template<typename T>
int Container<T>::size() const {
    return size_;
}

void useContainer() {
    Container<int> intContainer;
    intContainer.add(42);
    int val = intContainer.get(0);
    int sz = intContainer.size();

    Container<double> doubleContainer;
    doubleContainer.add(3.14);
}
"""
    )

    run_updater(cpp_out_of_class_project, mock_ingestor)

    method_names = _get_method_names(mock_ingestor, "Container")

    assert len(method_names) >= 3, f"Expected at least 3 methods, got: {method_names}"


def test_mixed_inline_and_out_of_class_methods(
    cpp_out_of_class_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    test_file = cpp_out_of_class_project / "mixed_methods.cpp"
    test_file.write_text(
        """
class MixedClass {
public:
    // Inline method definitions
    int inlineMethod1() { return 42; }
    void inlineMethod2() { /* inline */ }

    // Out-of-class method declarations
    void outOfClassMethod1();
    int outOfClassMethod2(int x);
    double outOfClassMethod3(double a, double b);
};

// Out-of-class method definitions
void MixedClass::outOfClassMethod1() {
    // Implementation
}

int MixedClass::outOfClassMethod2(int x) {
    return x * 2;
}

double MixedClass::outOfClassMethod3(double a, double b) {
    return a + b;
}

void useMixedClass() {
    MixedClass obj;
    int r1 = obj.inlineMethod1();
    obj.inlineMethod2();
    obj.outOfClassMethod1();
    int r2 = obj.outOfClassMethod2(5);
    double r3 = obj.outOfClassMethod3(1.0, 2.0);
}
"""
    )

    run_updater(cpp_out_of_class_project, mock_ingestor)

    method_names = _get_method_names(mock_ingestor, "MixedClass")

    assert "inlineMethod1" in method_names, (
        f"Expected inline methods, got: {method_names}"
    )
    assert "inlineMethod2" in method_names
    assert "outOfClassMethod1" in method_names, "Expected out-of-class methods"
    assert "outOfClassMethod2" in method_names
    assert "outOfClassMethod3" in method_names
