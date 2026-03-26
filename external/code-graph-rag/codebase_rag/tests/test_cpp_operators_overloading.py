from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_operators_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with operator overloading."""
    project_path = temp_repo / "cpp_operators_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_arithmetic_operators(
    cpp_operators_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test arithmetic operator overloading."""
    test_file = cpp_operators_project / "arithmetic_operators.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>

// Complex number class with arithmetic operators
class Complex {
private:
    double real_;
    double imaginary_;

public:
    Complex(double real = 0.0, double imaginary = 0.0)
        : real_(real), imaginary_(imaginary) {}

    // Getters
    double real() const { return real_; }
    double imaginary() const { return imaginary_; }

    // Binary arithmetic operators (member functions)
    Complex operator+(const Complex& other) const {
        return Complex(real_ + other.real_, imaginary_ + other.imaginary_);
    }

    Complex operator-(const Complex& other) const {
        return Complex(real_ - other.real_, imaginary_ - other.imaginary_);
    }

    Complex operator*(const Complex& other) const {
        return Complex(
            real_ * other.real_ - imaginary_ * other.imaginary_,
            real_ * other.imaginary_ + imaginary_ * other.real_
        );
    }

    Complex operator/(const Complex& other) const {
        double denominator = other.real_ * other.real_ + other.imaginary_ * other.imaginary_;
        return Complex(
            (real_ * other.real_ + imaginary_ * other.imaginary_) / denominator,
            (imaginary_ * other.real_ - real_ * other.imaginary_) / denominator
        );
    }

    // Unary arithmetic operators
    Complex operator+() const {
        return *this;  // Unary plus
    }

    Complex operator-() const {
        return Complex(-real_, -imaginary_);  // Unary minus
    }

    // Compound assignment operators
    Complex& operator+=(const Complex& other) {
        real_ += other.real_;
        imaginary_ += other.imaginary_;
        return *this;
    }

    Complex& operator-=(const Complex& other) {
        real_ -= other.real_;
        imaginary_ -= other.imaginary_;
        return *this;
    }

    Complex& operator*=(const Complex& other) {
        double temp_real = real_ * other.real_ - imaginary_ * other.imaginary_;
        imaginary_ = real_ * other.imaginary_ + imaginary_ * other.real_;
        real_ = temp_real;
        return *this;
    }

    Complex& operator/=(const Complex& other) {
        *this = *this / other;
        return *this;
    }

    // Scalar operations
    Complex operator*(double scalar) const {
        return Complex(real_ * scalar, imaginary_ * scalar);
    }

    Complex operator/(double scalar) const {
        return Complex(real_ / scalar, imaginary_ / scalar);
    }

    Complex& operator*=(double scalar) {
        real_ *= scalar;
        imaginary_ *= scalar;
        return *this;
    }

    Complex& operator/=(double scalar) {
        real_ /= scalar;
        imaginary_ /= scalar;
        return *this;
    }

    void print() const {
        std::cout << real_ << " + " << imaginary_ << "i" << std::endl;
    }
};

// Non-member arithmetic operators for scalar operations
Complex operator*(double scalar, const Complex& c) {
    return c * scalar;
}

Complex operator+(double scalar, const Complex& c) {
    return Complex(scalar + c.real(), c.imaginary());
}

Complex operator-(double scalar, const Complex& c) {
    return Complex(scalar - c.real(), -c.imaginary());
}

// Vector class with arithmetic operations
class Vector3D {
private:
    double x_, y_, z_;

public:
    Vector3D(double x = 0.0, double y = 0.0, double z = 0.0)
        : x_(x), y_(y), z_(z) {}

    // Getters
    double x() const { return x_; }
    double y() const { return y_; }
    double z() const { return z_; }

    // Vector addition
    Vector3D operator+(const Vector3D& other) const {
        return Vector3D(x_ + other.x_, y_ + other.y_, z_ + other.z_);
    }

    // Vector subtraction
    Vector3D operator-(const Vector3D& other) const {
        return Vector3D(x_ - other.x_, y_ - other.y_, z_ - other.z_);
    }

    // Dot product (scalar result)
    double operator*(const Vector3D& other) const {
        return x_ * other.x_ + y_ * other.y_ + z_ * other.z_;
    }

    // Scalar multiplication
    Vector3D operator*(double scalar) const {
        return Vector3D(x_ * scalar, y_ * scalar, z_ * scalar);
    }

    // Scalar division
    Vector3D operator/(double scalar) const {
        return Vector3D(x_ / scalar, y_ / scalar, z_ / scalar);
    }

    // Unary minus
    Vector3D operator-() const {
        return Vector3D(-x_, -y_, -z_);
    }

    // Compound assignment
    Vector3D& operator+=(const Vector3D& other) {
        x_ += other.x_;
        y_ += other.y_;
        z_ += other.z_;
        return *this;
    }

    Vector3D& operator-=(const Vector3D& other) {
        x_ -= other.x_;
        y_ -= other.y_;
        z_ -= other.z_;
        return *this;
    }

    Vector3D& operator*=(double scalar) {
        x_ *= scalar;
        y_ *= scalar;
        z_ *= scalar;
        return *this;
    }

    void print() const {
        std::cout << "(" << x_ << ", " << y_ << ", " << z_ << ")" << std::endl;
    }
};

// Non-member operator for scalar * vector
Vector3D operator*(double scalar, const Vector3D& v) {
    return v * scalar;
}

void testArithmeticOperators() {
    std::cout << "=== Testing Arithmetic Operators ===" << std::endl;

    // Complex number operations
    Complex c1(3.0, 4.0);
    Complex c2(1.0, 2.0);

    std::cout << "c1: "; c1.print();
    std::cout << "c2: "; c2.print();

    Complex sum = c1 + c2;
    Complex diff = c1 - c2;
    Complex product = c1 * c2;
    Complex quotient = c1 / c2;

    std::cout << "c1 + c2 = "; sum.print();
    std::cout << "c1 - c2 = "; diff.print();
    std::cout << "c1 * c2 = "; product.print();
    std::cout << "c1 / c2 = "; quotient.print();

    // Unary operators
    Complex positive = +c1;
    Complex negative = -c1;
    std::cout << "+c1 = "; positive.print();
    std::cout << "-c1 = "; negative.print();

    // Compound assignment
    Complex c3 = c1;
    c3 += c2;
    std::cout << "c1 += c2: "; c3.print();

    // Scalar operations
    Complex scaled = c1 * 2.0;
    Complex inverse_scaled = 3.0 * c1;
    std::cout << "c1 * 2.0 = "; scaled.print();
    std::cout << "3.0 * c1 = "; inverse_scaled.print();

    // Vector operations
    Vector3D v1(1.0, 2.0, 3.0);
    Vector3D v2(4.0, 5.0, 6.0);

    std::cout << "v1: "; v1.print();
    std::cout << "v2: "; v2.print();

    Vector3D v_sum = v1 + v2;
    Vector3D v_diff = v1 - v2;
    double dot_product = v1 * v2;

    std::cout << "v1 + v2 = "; v_sum.print();
    std::cout << "v1 - v2 = "; v_diff.print();
    std::cout << "v1 · v2 = " << dot_product << std::endl;

    Vector3D v_scaled = v1 * 2.0;
    Vector3D v_inverse_scaled = 0.5 * v1;
    std::cout << "v1 * 2.0 = "; v_scaled.print();
    std::cout << "0.5 * v1 = "; v_inverse_scaled.print();
}

void demonstrateArithmeticOperators() {
    testArithmeticOperators();
}
""",
    )

    run_updater(cpp_operators_project, mock_ingestor)

    project_name = cpp_operators_project.name

    expected_classes = [
        f"{project_name}.arithmetic_operators.Complex",
        f"{project_name}.arithmetic_operators.Vector3D",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 2, (
        f"Expected at least 2 arithmetic operator classes, found {len(found_classes)}: {found_classes}"
    )


def test_comparison_operators(
    cpp_operators_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test comparison operator overloading."""
    test_file = cpp_operators_project / "comparison_operators.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <vector>
#include <algorithm>

// Person class with comparison operators
class Person {
private:
    std::string name_;
    int age_;
    double height_; // in meters

public:
    Person(const std::string& name, int age, double height)
        : name_(name), age_(age), height_(height) {}

    // Getters
    const std::string& name() const { return name_; }
    int age() const { return age_; }
    double height() const { return height_; }

    // Equality operators
    bool operator==(const Person& other) const {
        return name_ == other.name_ && age_ == other.age_ && height_ == other.height_;
    }

    bool operator!=(const Person& other) const {
        return !(*this == other);
    }

    // Relational operators (comparison by age)
    bool operator<(const Person& other) const {
        return age_ < other.age_;
    }

    bool operator<=(const Person& other) const {
        return age_ <= other.age_;
    }

    bool operator>(const Person& other) const {
        return age_ > other.age_;
    }

    bool operator>=(const Person& other) const {
        return age_ >= other.age_;
    }

    void print() const {
        std::cout << name_ << " (age " << age_ << ", height " << height_ << "m)" << std::endl;
    }
};

// Version class with lexicographic comparison
class Version {
private:
    int major_, minor_, patch_;

public:
    Version(int major, int minor, int patch)
        : major_(major), minor_(minor), patch_(patch) {}

    // Getters
    int major() const { return major_; }
    int minor() const { return minor_; }
    int patch() const { return patch_; }

    // Equality operators
    bool operator==(const Version& other) const {
        return major_ == other.major_ && minor_ == other.minor_ && patch_ == other.patch_;
    }

    bool operator!=(const Version& other) const {
        return !(*this == other);
    }

    // Lexicographic comparison
    bool operator<(const Version& other) const {
        if (major_ != other.major_) return major_ < other.major_;
        if (minor_ != other.minor_) return minor_ < other.minor_;
        return patch_ < other.patch_;
    }

    bool operator<=(const Version& other) const {
        return *this < other || *this == other;
    }

    bool operator>(const Version& other) const {
        return !(*this <= other);
    }

    bool operator>=(const Version& other) const {
        return !(*this < other);
    }

    void print() const {
        std::cout << major_ << "." << minor_ << "." << patch_ << std::endl;
    }
};

// Money class with comparison operators
class Money {
private:
    int cents_;  // Store as cents to avoid floating point issues

public:
    Money(double dollars = 0.0) : cents_(static_cast<int>(dollars * 100 + 0.5)) {}

    double dollars() const { return cents_ / 100.0; }
    int cents() const { return cents_; }

    // Equality operators
    bool operator==(const Money& other) const {
        return cents_ == other.cents_;
    }

    bool operator!=(const Money& other) const {
        return cents_ != other.cents_;
    }

    // Relational operators
    bool operator<(const Money& other) const {
        return cents_ < other.cents_;
    }

    bool operator<=(const Money& other) const {
        return cents_ <= other.cents_;
    }

    bool operator>(const Money& other) const {
        return cents_ > other.cents_;
    }

    bool operator>=(const Money& other) const {
        return cents_ >= other.cents_;
    }

    void print() const {
        std::cout << "$" << dollars() << std::endl;
    }
};

// Custom comparator using function object
struct PersonAgeComparator {
    bool operator()(const Person& a, const Person& b) const {
        return a.age() < b.age();
    }
};

struct PersonNameComparator {
    bool operator()(const Person& a, const Person& b) const {
        return a.name() < b.name();
    }
};

void testComparisonOperators() {
    std::cout << "=== Testing Comparison Operators ===" << std::endl;

    // Person comparison
    Person alice("Alice", 25, 1.65);
    Person bob("Bob", 30, 1.75);
    Person charlie("Charlie", 25, 1.80);

    std::cout << "People:" << std::endl;
    alice.print();
    bob.print();
    charlie.print();

    // Equality tests
    std::cout << "alice == bob: " << std::boolalpha << (alice == bob) << std::endl;
    std::cout << "alice != bob: " << std::boolalpha << (alice != bob) << std::endl;
    std::cout << "alice == charlie: " << std::boolalpha << (alice == charlie) << std::endl;

    // Relational tests (by age)
    std::cout << "alice < bob: " << std::boolalpha << (alice < bob) << std::endl;
    std::cout << "alice <= charlie: " << std::boolalpha << (alice <= charlie) << std::endl;
    std::cout << "bob > alice: " << std::boolalpha << (bob > alice) << std::endl;
    std::cout << "bob >= charlie: " << std::boolalpha << (bob >= charlie) << std::endl;

    // Version comparison
    Version v1(1, 2, 3);
    Version v2(1, 2, 4);
    Version v3(1, 3, 0);
    Version v4(2, 0, 0);

    std::cout << "\nVersions:" << std::endl;
    std::cout << "v1: "; v1.print();
    std::cout << "v2: "; v2.print();
    std::cout << "v3: "; v3.print();
    std::cout << "v4: "; v4.print();

    std::cout << "v1 < v2: " << std::boolalpha << (v1 < v2) << std::endl;
    std::cout << "v2 < v3: " << std::boolalpha << (v2 < v3) << std::endl;
    std::cout << "v3 < v4: " << std::boolalpha << (v3 < v4) << std::endl;

    // Money comparison
    Money m1(10.50);
    Money m2(15.75);
    Money m3(10.50);

    std::cout << "\nMoney amounts:" << std::endl;
    std::cout << "m1: "; m1.print();
    std::cout << "m2: "; m2.print();
    std::cout << "m3: "; m3.print();

    std::cout << "m1 == m3: " << std::boolalpha << (m1 == m3) << std::endl;
    std::cout << "m1 < m2: " << std::boolalpha << (m1 < m2) << std::endl;
    std::cout << "m2 > m1: " << std::boolalpha << (m2 > m1) << std::endl;
}

void testSortingWithComparators() {
    std::cout << "\n=== Testing Sorting with Comparators ===" << std::endl;

    std::vector<Person> people = {
        Person("David", 35, 1.80),
        Person("Alice", 25, 1.65),
        Person("Charlie", 30, 1.75),
        Person("Bob", 28, 1.70)
    };

    std::cout << "Original order:" << std::endl;
    for (const auto& person : people) {
        person.print();
    }

    // Sort by age using default comparison operator
    std::sort(people.begin(), people.end());
    std::cout << "\nSorted by age (using operator<):" << std::endl;
    for (const auto& person : people) {
        person.print();
    }

    // Sort by name using custom comparator
    std::sort(people.begin(), people.end(), PersonNameComparator{});
    std::cout << "\nSorted by name (using custom comparator):" << std::endl;
    for (const auto& person : people) {
        person.print();
    }

    // Sort by age again using function object
    std::sort(people.begin(), people.end(), PersonAgeComparator{});
    std::cout << "\nSorted by age (using function object):" << std::endl;
    for (const auto& person : people) {
        person.print();
    }

    // Sort using lambda
    std::sort(people.begin(), people.end(), [](const Person& a, const Person& b) {
        return a.height() < b.height();  // Sort by height
    });
    std::cout << "\nSorted by height (using lambda):" << std::endl;
    for (const auto& person : people) {
        person.print();
    }
}

void demonstrateComparisonOperators() {
    testComparisonOperators();
    testSortingWithComparators();
}
""",
    )

    run_updater(cpp_operators_project, mock_ingestor)

    project_name = cpp_operators_project.name

    expected_classes = [
        f"{project_name}.comparison_operators.Person",
        f"{project_name}.comparison_operators.Version",
        f"{project_name}.comparison_operators.Money",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 2, (
        f"Expected at least 2 comparison operator classes, found {len(found_classes)}: {found_classes}"
    )


def test_stream_function_call_operators(
    cpp_operators_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test stream operators and function call operators."""
    test_file = cpp_operators_project / "stream_function_operators.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <sstream>
#include <vector>
#include <string>

// Point class with stream operators
class Point {
private:
    double x_, y_;

public:
    Point(double x = 0.0, double y = 0.0) : x_(x), y_(y) {}

    double x() const { return x_; }
    double y() const { return y_; }
    void setX(double x) { x_ = x; }
    void setY(double y) { y_ = y; }

    // Friend functions for stream operators
    friend std::ostream& operator<<(std::ostream& os, const Point& p);
    friend std::istream& operator>>(std::istream& is, Point& p);
};

// Stream output operator
std::ostream& operator<<(std::ostream& os, const Point& p) {
    os << "(" << p.x_ << ", " << p.y_ << ")";
    return os;
}

// Stream input operator
std::istream& operator>>(std::istream& is, Point& p) {
    char lparen, comma, rparen;
    is >> lparen >> p.x_ >> comma >> p.y_ >> rparen;
    return is;
}

// Function object (functor) classes
class Adder {
private:
    int value_;

public:
    Adder(int value) : value_(value) {}

    // Function call operator
    int operator()(int x) const {
        return x + value_;
    }

    // Overloaded function call operator
    int operator()(int x, int y) const {
        return x + y + value_;
    }
};

class Multiplier {
private:
    double factor_;

public:
    Multiplier(double factor) : factor_(factor) {}

    double operator()(double x) const {
        return x * factor_;
    }

    template<typename T>
    T operator()(T x) const {
        return static_cast<T>(x * factor_);
    }
};

// Generic function object
template<typename T>
class Comparator {
private:
    T threshold_;

public:
    Comparator(T threshold) : threshold_(threshold) {}

    bool operator()(const T& value) const {
        return value > threshold_;
    }
};

// Matrix class with function call operator for element access
class Matrix {
private:
    std::vector<std::vector<double>> data_;
    size_t rows_, cols_;

public:
    Matrix(size_t rows, size_t cols) : rows_(rows), cols_(cols) {
        data_.resize(rows);
        for (auto& row : data_) {
            row.resize(cols, 0.0);
        }
    }

    // Function call operator for element access
    double& operator()(size_t row, size_t col) {
        return data_[row][col];
    }

    const double& operator()(size_t row, size_t col) const {
        return data_[row][col];
    }

    size_t rows() const { return rows_; }
    size_t cols() const { return cols_; }

    // Stream output operator
    friend std::ostream& operator<<(std::ostream& os, const Matrix& m);
};

std::ostream& operator<<(std::ostream& os, const Matrix& m) {
    for (size_t i = 0; i < m.rows_; ++i) {
        for (size_t j = 0; j < m.cols_; ++j) {
            os << m(i, j) << " ";
        }
        os << std::endl;
    }
    return os;
}

// Calculator class with multiple function call operators
class Calculator {
public:
    // Addition
    double operator()(double a, double b) const {
        return a + b;
    }

    // Subtraction
    double operator()(double a, double b, char op) const {
        switch (op) {
            case '+': return a + b;
            case '-': return a - b;
            case '*': return a * b;
            case '/': return (b != 0) ? a / b : 0;
            default: return 0;
        }
    }

    // Vector sum
    double operator()(const std::vector<double>& values) const {
        double sum = 0.0;
        for (double value : values) {
            sum += value;
        }
        return sum;
    }
};

void testStreamOperators() {
    std::cout << "=== Testing Stream Operators ===" << std::endl;

    Point p1(3.14, 2.71);
    Point p2(-1.5, 4.2);

    // Output stream operator
    std::cout << "Point 1: " << p1 << std::endl;
    std::cout << "Point 2: " << p2 << std::endl;

    // Input stream operator
    std::cout << "Enter a point in format (x, y): ";
    Point input_point;
    // For testing, we'll simulate input
    std::stringstream ss("(5.0, 7.5)");
    ss >> input_point;
    std::cout << "Input point: " << input_point << std::endl;

    // Matrix output
    Matrix m(3, 3);
    m(0, 0) = 1.0; m(0, 1) = 2.0; m(0, 2) = 3.0;
    m(1, 0) = 4.0; m(1, 1) = 5.0; m(1, 2) = 6.0;
    m(2, 0) = 7.0; m(2, 1) = 8.0; m(2, 2) = 9.0;

    std::cout << "Matrix:" << std::endl << m;
}

void testFunctionCallOperators() {
    std::cout << "=== Testing Function Call Operators ===" << std::endl;

    // Function objects (functors)
    Adder add5(5);
    Multiplier times2(2.0);

    std::cout << "add5(10) = " << add5(10) << std::endl;
    std::cout << "add5(3, 7) = " << add5(3, 7) << std::endl;
    std::cout << "times2(6.5) = " << times2(6.5) << std::endl;
    std::cout << "times2<int>(7) = " << times2<int>(7) << std::endl;

    // Using function objects with STL algorithms
    std::vector<int> numbers = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};

    // Transform using function object
    std::vector<int> added(numbers.size());
    std::transform(numbers.begin(), numbers.end(), added.begin(), add5);

    std::cout << "Original: ";
    for (int n : numbers) std::cout << n << " ";
    std::cout << std::endl;

    std::cout << "After add5: ";
    for (int n : added) std::cout << n << " ";
    std::cout << std::endl;

    // Filter using predicate function object
    Comparator<int> greater_than_5(5);
    int count = std::count_if(numbers.begin(), numbers.end(), greater_than_5);
    std::cout << "Numbers > 5: " << count << std::endl;

    // Matrix function call operator
    Matrix matrix(2, 3);
    matrix(0, 0) = 1.1; matrix(0, 1) = 1.2; matrix(0, 2) = 1.3;
    matrix(1, 0) = 2.1; matrix(1, 1) = 2.2; matrix(1, 2) = 2.3;

    std::cout << "Matrix element (1, 2): " << matrix(1, 2) << std::endl;
    std::cout << "Full matrix:" << std::endl << matrix;

    // Calculator with multiple signatures
    Calculator calc;
    std::cout << "calc(10, 5) = " << calc(10, 5) << std::endl;
    std::cout << "calc(10, 5, '-') = " << calc(10, 5, '-') << std::endl;
    std::cout << "calc(10, 5, '*') = " << calc(10, 5, '*') << std::endl;

    std::vector<double> values = {1.1, 2.2, 3.3, 4.4, 5.5};
    std::cout << "calc(vector) = " << calc(values) << std::endl;
}

void demonstrateStreamAndFunctionOperators() {
    testStreamOperators();
    testFunctionCallOperators();
}
""",
    )

    run_updater(cpp_operators_project, mock_ingestor)

    project_name = cpp_operators_project.name

    expected_classes = [
        f"{project_name}.stream_function_operators.Point",
        f"{project_name}.stream_function_operators.Adder",
        f"{project_name}.stream_function_operators.Matrix",
        f"{project_name}.stream_function_operators.Calculator",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 3, (
        f"Expected at least 3 stream/function operator classes, found {len(found_classes)}: {found_classes}"
    )


def test_subscript_increment_operators(
    cpp_operators_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test subscript and increment/decrement operators."""
    test_file = cpp_operators_project / "subscript_increment_operators.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <vector>
#include <stdexcept>

// Dynamic array class with subscript operator
class DynamicArray {
private:
    double* data_;
    size_t size_;
    size_t capacity_;

public:
    DynamicArray(size_t initial_capacity = 10)
        : size_(0), capacity_(initial_capacity) {
        data_ = new double[capacity_];
    }

    ~DynamicArray() {
        delete[] data_;
    }

    // Copy constructor
    DynamicArray(const DynamicArray& other)
        : size_(other.size_), capacity_(other.capacity_) {
        data_ = new double[capacity_];
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = other.data_[i];
        }
    }

    // Copy assignment operator
    DynamicArray& operator=(const DynamicArray& other) {
        if (this != &other) {
            delete[] data_;
            size_ = other.size_;
            capacity_ = other.capacity_;
            data_ = new double[capacity_];
            for (size_t i = 0; i < size_; ++i) {
                data_[i] = other.data_[i];
            }
        }
        return *this;
    }

    // Move constructor
    DynamicArray(DynamicArray&& other) noexcept
        : data_(other.data_), size_(other.size_), capacity_(other.capacity_) {
        other.data_ = nullptr;
        other.size_ = 0;
        other.capacity_ = 0;
    }

    // Move assignment operator
    DynamicArray& operator=(DynamicArray&& other) noexcept {
        if (this != &other) {
            delete[] data_;
            data_ = other.data_;
            size_ = other.size_;
            capacity_ = other.capacity_;
            other.data_ = nullptr;
            other.size_ = 0;
            other.capacity_ = 0;
        }
        return *this;
    }

    // Subscript operators (const and non-const versions)
    double& operator[](size_t index) {
        if (index >= size_) {
            throw std::out_of_range("Index out of range");
        }
        return data_[index];
    }

    const double& operator[](size_t index) const {
        if (index >= size_) {
            throw std::out_of_range("Index out of range");
        }
        return data_[index];
    }

    void push_back(double value) {
        if (size_ >= capacity_) {
            resize();
        }
        data_[size_++] = value;
    }

    size_t size() const { return size_; }
    size_t capacity() const { return capacity_; }

private:
    void resize() {
        capacity_ *= 2;
        double* new_data = new double[capacity_];
        for (size_t i = 0; i < size_; ++i) {
            new_data[i] = data_[i];
        }
        delete[] data_;
        data_ = new_data;
    }
};

// Iterator class with increment/decrement operators
class Counter {
private:
    int value_;

public:
    Counter(int initial_value = 0) : value_(initial_value) {}

    int value() const { return value_; }

    // Prefix increment
    Counter& operator++() {
        ++value_;
        return *this;
    }

    // Postfix increment
    Counter operator++(int) {
        Counter temp = *this;
        ++value_;
        return temp;
    }

    // Prefix decrement
    Counter& operator--() {
        --value_;
        return *this;
    }

    // Postfix decrement
    Counter operator--(int) {
        Counter temp = *this;
        --value_;
        return temp;
    }

    // Compound assignment for larger increments
    Counter& operator+=(int increment) {
        value_ += increment;
        return *this;
    }

    Counter& operator-=(int decrement) {
        value_ -= decrement;
        return *this;
    }
};

// String-like class with subscript operator
class SimpleString {
private:
    char* data_;
    size_t length_;

public:
    SimpleString(const char* str = "") {
        length_ = strlen(str);
        data_ = new char[length_ + 1];
        strcpy(data_, str);
    }

    ~SimpleString() {
        delete[] data_;
    }

    // Copy constructor
    SimpleString(const SimpleString& other) : length_(other.length_) {
        data_ = new char[length_ + 1];
        strcpy(data_, other.data_);
    }

    // Copy assignment operator
    SimpleString& operator=(const SimpleString& other) {
        if (this != &other) {
            delete[] data_;
            length_ = other.length_;
            data_ = new char[length_ + 1];
            strcpy(data_, other.data_);
        }
        return *this;
    }

    // Move constructor
    SimpleString(SimpleString&& other) noexcept
        : data_(other.data_), length_(other.length_) {
        other.data_ = nullptr;
        other.length_ = 0;
    }

    // Move assignment operator
    SimpleString& operator=(SimpleString&& other) noexcept {
        if (this != &other) {
            delete[] data_;
            data_ = other.data_;
            length_ = other.length_;
            other.data_ = nullptr;
            other.length_ = 0;
        }
        return *this;
    }

    // Subscript operators
    char& operator[](size_t index) {
        if (index >= length_) {
            throw std::out_of_range("Index out of range");
        }
        return data_[index];
    }

    const char& operator[](size_t index) const {
        if (index >= length_) {
            throw std::out_of_range("Index out of range");
        }
        return data_[index];
    }

    size_t length() const { return length_; }
    const char* c_str() const { return data_; }
};

// Map-like class with subscript operator
template<typename Key, typename Value>
class SimpleMap {
private:
    struct Pair {
        Key key;
        Value value;
        bool used;

        Pair() : used(false) {}
        Pair(const Key& k, const Value& v) : key(k), value(v), used(true) {}
    };

    static const size_t CAPACITY = 100;
    Pair data_[CAPACITY];

    size_t hash(const Key& key) const {
        // Simple hash function (not production quality)
        return std::hash<Key>{}(key) % CAPACITY;
    }

public:
    // Subscript operator for insertion/access
    Value& operator[](const Key& key) {
        size_t index = hash(key);

        // Linear probing for collision resolution
        while (data_[index].used && data_[index].key != key) {
            index = (index + 1) % CAPACITY;
        }

        if (!data_[index].used) {
            data_[index] = Pair(key, Value{});
        }

        return data_[index].value;
    }

    bool contains(const Key& key) const {
        size_t index = hash(key);

        while (data_[index].used) {
            if (data_[index].key == key) {
                return true;
            }
            index = (index + 1) % CAPACITY;
        }

        return false;
    }
};

void testSubscriptOperators() {
    std::cout << "=== Testing Subscript Operators ===" << std::endl;

    // Dynamic array
    DynamicArray arr;
    arr.push_back(1.1);
    arr.push_back(2.2);
    arr.push_back(3.3);
    arr.push_back(4.4);

    std::cout << "Array elements: ";
    for (size_t i = 0; i < arr.size(); ++i) {
        std::cout << arr[i] << " ";
    }
    std::cout << std::endl;

    // Modify elements using subscript
    arr[1] = 99.9;
    std::cout << "After modification: ";
    for (size_t i = 0; i < arr.size(); ++i) {
        std::cout << arr[i] << " ";
    }
    std::cout << std::endl;

    // SimpleString
    SimpleString str("Hello");
    std::cout << "String: " << str.c_str() << std::endl;
    std::cout << "Characters: ";
    for (size_t i = 0; i < str.length(); ++i) {
        std::cout << str[i] << " ";
    }
    std::cout << std::endl;

    // Modify string
    str[0] = 'h';  // Change 'H' to 'h'
    std::cout << "Modified string: " << str.c_str() << std::endl;

    // SimpleMap
    SimpleMap<std::string, int> map;
    map["apple"] = 5;
    map["banana"] = 3;
    map["orange"] = 8;

    std::cout << "Map contents:" << std::endl;
    std::cout << "apple: " << map["apple"] << std::endl;
    std::cout << "banana: " << map["banana"] << std::endl;
    std::cout << "orange: " << map["orange"] << std::endl;

    // Access non-existent key (creates with default value)
    std::cout << "grape: " << map["grape"] << std::endl;
}

void testIncrementDecrementOperators() {
    std::cout << "=== Testing Increment/Decrement Operators ===" << std::endl;

    Counter counter(10);
    std::cout << "Initial value: " << counter.value() << std::endl;

    // Prefix increment
    ++counter;
    std::cout << "After ++counter: " << counter.value() << std::endl;

    // Postfix increment
    Counter old_value = counter++;
    std::cout << "After counter++: counter = " << counter.value()
              << ", old value = " << old_value.value() << std::endl;

    // Prefix decrement
    --counter;
    std::cout << "After --counter: " << counter.value() << std::endl;

    // Postfix decrement
    old_value = counter--;
    std::cout << "After counter--: counter = " << counter.value()
              << ", old value = " << old_value.value() << std::endl;

    // Compound assignment
    counter += 5;
    std::cout << "After counter += 5: " << counter.value() << std::endl;

    counter -= 3;
    std::cout << "After counter -= 3: " << counter.value() << std::endl;

    // Loop using increment operator
    std::cout << "Counting from 0 to 5: ";
    Counter loop_counter(0);
    while (loop_counter.value() <= 5) {
        std::cout << loop_counter.value() << " ";
        ++loop_counter;
    }
    std::cout << std::endl;
}

void demonstrateSubscriptAndIncrementOperators() {
    testSubscriptOperators();
    testIncrementDecrementOperators();
}
""",
    )

    run_updater(cpp_operators_project, mock_ingestor)

    project_name = cpp_operators_project.name

    expected_classes = [
        f"{project_name}.subscript_increment_operators.DynamicArray",
        f"{project_name}.subscript_increment_operators.Counter",
        f"{project_name}.subscript_increment_operators.SimpleString",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 2, (
        f"Expected at least 2 subscript/increment operator classes, found {len(found_classes)}: {found_classes}"
    )


def test_cpp_operators_comprehensive(
    cpp_operators_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all operator overloading patterns create proper relationships."""
    test_file = cpp_operators_project / "comprehensive_operators.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every operator overloading pattern in one file
#include <iostream>
#include <vector>
#include <string>

class ComprehensiveOperators {
private:
    int value_;
    std::string name_;

public:
    ComprehensiveOperators(int value = 0, const std::string& name = "")
        : value_(value), name_(name) {}

    // Arithmetic operators
    ComprehensiveOperators operator+(const ComprehensiveOperators& other) const {
        return ComprehensiveOperators(value_ + other.value_, name_ + "_plus_" + other.name_);
    }

    ComprehensiveOperators operator-(const ComprehensiveOperators& other) const {
        return ComprehensiveOperators(value_ - other.value_, name_ + "_minus_" + other.name_);
    }

    // Comparison operators
    bool operator==(const ComprehensiveOperators& other) const {
        return value_ == other.value_;
    }

    bool operator<(const ComprehensiveOperators& other) const {
        return value_ < other.value_;
    }

    // Assignment operators
    ComprehensiveOperators& operator=(const ComprehensiveOperators& other) {
        if (this != &other) {
            value_ = other.value_;
            name_ = other.name_;
        }
        return *this;
    }

    ComprehensiveOperators& operator+=(const ComprehensiveOperators& other) {
        value_ += other.value_;
        name_ += "_added_" + other.name_;
        return *this;
    }

    // Increment/decrement operators
    ComprehensiveOperators& operator++() {  // Prefix
        ++value_;
        name_ += "_inc";
        return *this;
    }

    ComprehensiveOperators operator++(int) {  // Postfix
        ComprehensiveOperators temp = *this;
        ++value_;
        name_ += "_post";
        return temp;
    }

    // Subscript operator
    int& operator[](size_t index) {
        return value_;  // Simplified for demo
    }

    // Function call operator
    int operator()(int multiplier) const {
        return value_ * multiplier;
    }

    // Stream operators (as friends)
    friend std::ostream& operator<<(std::ostream& os, const ComprehensiveOperators& obj);
    friend std::istream& operator>>(std::istream& is, ComprehensiveOperators& obj);

    // Getters
    int value() const { return value_; }
    const std::string& name() const { return name_; }
};

// Stream operators implementation
std::ostream& operator<<(std::ostream& os, const ComprehensiveOperators& obj) {
    os << obj.name_ << "(" << obj.value_ << ")";
    return os;
}

std::istream& operator>>(std::istream& is, ComprehensiveOperators& obj) {
    is >> obj.value_;
    return is;
}

void demonstrateAllOperators() {
    // Test all operator categories
    ComprehensiveOperators obj1(10, "first");
    ComprehensiveOperators obj2(20, "second");

    // Arithmetic
    auto sum = obj1 + obj2;
    auto diff = obj1 - obj2;

    // Comparison
    bool equal = (obj1 == obj2);
    bool less = (obj1 < obj2);

    // Assignment
    ComprehensiveOperators obj3;
    obj3 = obj1;
    obj3 += obj2;

    // Increment
    ++obj1;
    obj2++;

    // Subscript
    obj1[0] = 100;

    // Function call
    int result = obj1(5);

    // Stream output
    std::cout << "Objects: " << obj1 << ", " << obj2 << ", " << sum << std::endl;
    std::cout << "Comparison: equal=" << equal << ", less=" << less << std::endl;
    std::cout << "Function call result: " << result << std::endl;
}
""",
    )

    run_updater(cpp_operators_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_operators" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 5, (
        f"Expected at least 5 comprehensive operator calls, found {len(comprehensive_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
