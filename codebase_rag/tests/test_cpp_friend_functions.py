from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_friend_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with friend relationships."""
    project_path = temp_repo / "cpp_friend_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_friend_functions(
    cpp_friend_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test friend functions and their access privileges."""
    test_file = cpp_friend_project / "friend_functions.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <cmath>

// Forward declarations
class Complex;
class Vector3D;

// Friend function prototypes
double calculateDistance(const Vector3D& a, const Vector3D& b);
Complex conjugate(const Complex& c);

// Complex number class with friend functions
class Complex {
private:
    double real_;
    double imaginary_;

public:
    Complex(double real = 0.0, double imaginary = 0.0)
        : real_(real), imaginary_(imaginary) {}

    // Regular member functions
    double magnitude() const {
        return std::sqrt(real_ * real_ + imaginary_ * imaginary_);
    }

    void print() const {
        std::cout << real_ << " + " << imaginary_ << "i" << std::endl;
    }

    // Friend function declarations
    friend double getRealPart(const Complex& c);
    friend double getImaginaryPart(const Complex& c);
    friend Complex conjugate(const Complex& c);
    friend Complex add(const Complex& a, const Complex& b);
    friend Complex multiply(const Complex& a, const Complex& b);

    // Friend operator functions
    friend std::ostream& operator<<(std::ostream& os, const Complex& c);
    friend std::istream& operator>>(std::istream& is, Complex& c);
    friend Complex operator+(const Complex& a, const Complex& b);
    friend Complex operator*(const Complex& a, const Complex& b);
    friend bool operator==(const Complex& a, const Complex& b);

    // Friend class declaration
    friend class ComplexAnalyzer;
};

// Friend function implementations
double getRealPart(const Complex& c) {
    return c.real_;  // Can access private members
}

double getImaginaryPart(const Complex& c) {
    return c.imaginary_;  // Can access private members
}

Complex conjugate(const Complex& c) {
    return Complex(c.real_, -c.imaginary_);  // Access private members
}

Complex add(const Complex& a, const Complex& b) {
    return Complex(a.real_ + b.real_, a.imaginary_ + b.imaginary_);
}

Complex multiply(const Complex& a, const Complex& b) {
    double real_part = a.real_ * b.real_ - a.imaginary_ * b.imaginary_;
    double imag_part = a.real_ * b.imaginary_ + a.imaginary_ * b.real_;
    return Complex(real_part, imag_part);
}

// Friend operator implementations
std::ostream& operator<<(std::ostream& os, const Complex& c) {
    os << c.real_ << " + " << c.imaginary_ << "i";
    return os;
}

std::istream& operator>>(std::istream& is, Complex& c) {
    std::cout << "Enter real part: ";
    is >> c.real_;
    std::cout << "Enter imaginary part: ";
    is >> c.imaginary_;
    return is;
}

Complex operator+(const Complex& a, const Complex& b) {
    return add(a, b);  // Use friend function
}

Complex operator*(const Complex& a, const Complex& b) {
    return multiply(a, b);  // Use friend function
}

bool operator==(const Complex& a, const Complex& b) {
    return (a.real_ == b.real_) && (a.imaginary_ == b.imaginary_);
}

// Friend class example
class ComplexAnalyzer {
private:
    std::string analysis_name_;

public:
    ComplexAnalyzer(const std::string& name) : analysis_name_(name) {}

    void analyzeComplex(const Complex& c) {
        std::cout << "=== " << analysis_name_ << " Analysis ===" << std::endl;

        // Direct access to private members because of friend relationship
        std::cout << "Real part: " << c.real_ << std::endl;
        std::cout << "Imaginary part: " << c.imaginary_ << std::endl;
        std::cout << "Magnitude: " << c.magnitude() << std::endl;

        // Analyze quadrant
        if (c.real_ > 0 && c.imaginary_ > 0) {
            std::cout << "Quadrant: I" << std::endl;
        } else if (c.real_ < 0 && c.imaginary_ > 0) {
            std::cout << "Quadrant: II" << std::endl;
        } else if (c.real_ < 0 && c.imaginary_ < 0) {
            std::cout << "Quadrant: III" << std::endl;
        } else if (c.real_ > 0 && c.imaginary_ < 0) {
            std::cout << "Quadrant: IV" << std::endl;
        } else {
            std::cout << "On axis" << std::endl;
        }
    }

    void compareComplexNumbers(const Complex& a, const Complex& b) {
        std::cout << "=== Comparison Analysis ===" << std::endl;

        // Direct access to private members
        std::cout << "Complex A: " << a.real_ << " + " << a.imaginary_ << "i" << std::endl;
        std::cout << "Complex B: " << b.real_ << " + " << b.imaginary_ << "i" << std::endl;

        double mag_a = std::sqrt(a.real_ * a.real_ + a.imaginary_ * a.imaginary_);
        double mag_b = std::sqrt(b.real_ * b.real_ + b.imaginary_ * b.imaginary_);

        if (mag_a > mag_b) {
            std::cout << "Complex A has larger magnitude" << std::endl;
        } else if (mag_b > mag_a) {
            std::cout << "Complex B has larger magnitude" << std::endl;
        } else {
            std::cout << "Both complex numbers have equal magnitude" << std::endl;
        }
    }
};

// Vector3D class with friend functions
class Vector3D {
private:
    double x_, y_, z_;

public:
    Vector3D(double x = 0.0, double y = 0.0, double z = 0.0)
        : x_(x), y_(y), z_(z) {}

    void print() const {
        std::cout << "(" << x_ << ", " << y_ << ", " << z_ << ")" << std::endl;
    }

    double magnitude() const {
        return std::sqrt(x_ * x_ + y_ * y_ + z_ * z_);
    }

    // Friend function declarations
    friend double calculateDistance(const Vector3D& a, const Vector3D& b);
    friend double dotProduct(const Vector3D& a, const Vector3D& b);
    friend Vector3D crossProduct(const Vector3D& a, const Vector3D& b);
    friend Vector3D normalize(const Vector3D& v);

    // Friend operators
    friend std::ostream& operator<<(std::ostream& os, const Vector3D& v);
    friend Vector3D operator+(const Vector3D& a, const Vector3D& b);
    friend Vector3D operator-(const Vector3D& a, const Vector3D& b);
    friend Vector3D operator*(double scalar, const Vector3D& v);

    // Friend class
    friend class VectorOperations;
};

// Friend function implementations for Vector3D
double calculateDistance(const Vector3D& a, const Vector3D& b) {
    double dx = a.x_ - b.x_;
    double dy = a.y_ - b.y_;
    double dz = a.z_ - b.z_;
    return std::sqrt(dx * dx + dy * dy + dz * dz);
}

double dotProduct(const Vector3D& a, const Vector3D& b) {
    return a.x_ * b.x_ + a.y_ * b.y_ + a.z_ * b.z_;
}

Vector3D crossProduct(const Vector3D& a, const Vector3D& b) {
    return Vector3D(
        a.y_ * b.z_ - a.z_ * b.y_,
        a.z_ * b.x_ - a.x_ * b.z_,
        a.x_ * b.y_ - a.y_ * b.x_
    );
}

Vector3D normalize(const Vector3D& v) {
    double mag = std::sqrt(v.x_ * v.x_ + v.y_ * v.y_ + v.z_ * v.z_);
    if (mag > 0) {
        return Vector3D(v.x_ / mag, v.y_ / mag, v.z_ / mag);
    }
    return Vector3D(0, 0, 0);
}

// Friend operators for Vector3D
std::ostream& operator<<(std::ostream& os, const Vector3D& v) {
    os << "(" << v.x_ << ", " << v.y_ << ", " << v.z_ << ")";
    return os;
}

Vector3D operator+(const Vector3D& a, const Vector3D& b) {
    return Vector3D(a.x_ + b.x_, a.y_ + b.y_, a.z_ + b.z_);
}

Vector3D operator-(const Vector3D& a, const Vector3D& b) {
    return Vector3D(a.x_ - b.x_, a.y_ - b.y_, a.z_ - b.z_);
}

Vector3D operator*(double scalar, const Vector3D& v) {
    return Vector3D(scalar * v.x_, scalar * v.y_, scalar * v.z_);
}

// Friend class for Vector3D
class VectorOperations {
public:
    static void performAnalysis(const Vector3D& v1, const Vector3D& v2) {
        std::cout << "=== Vector Operations Analysis ===" << std::endl;

        // Direct access to private members
        std::cout << "Vector 1: (" << v1.x_ << ", " << v1.y_ << ", " << v1.z_ << ")" << std::endl;
        std::cout << "Vector 2: (" << v2.x_ << ", " << v2.y_ << ", " << v2.z_ << ")" << std::endl;

        // Calculate various operations
        double distance = calculateDistance(v1, v2);
        double dot = dotProduct(v1, v2);
        Vector3D cross = crossProduct(v1, v2);

        std::cout << "Distance: " << distance << std::endl;
        std::cout << "Dot product: " << dot << std::endl;
        std::cout << "Cross product: " << cross << std::endl;

        // Check if vectors are perpendicular
        if (std::abs(dot) < 1e-10) {
            std::cout << "Vectors are perpendicular" << std::endl;
        } else {
            double angle = std::acos(dot / (v1.magnitude() * v2.magnitude())) * 180.0 / M_PI;
            std::cout << "Angle between vectors: " << angle << " degrees" << std::endl;
        }
    }

    static Vector3D computeAverage(const Vector3D& v1, const Vector3D& v2) {
        // Direct access to private members
        return Vector3D(
            (v1.x_ + v2.x_) / 2.0,
            (v1.y_ + v2.y_) / 2.0,
            (v1.z_ + v2.z_) / 2.0
        );
    }
};

void testFriendFunctions() {
    std::cout << "=== Testing Friend Functions ===" << std::endl;

    // Test Complex friend functions
    Complex c1(3.0, 4.0);
    Complex c2(1.0, 2.0);

    std::cout << "Complex numbers:" << std::endl;
    std::cout << "c1: " << c1 << std::endl;
    std::cout << "c2: " << c2 << std::endl;

    // Use friend functions
    std::cout << "Real part of c1: " << getRealPart(c1) << std::endl;
    std::cout << "Imaginary part of c1: " << getImaginaryPart(c1) << std::endl;

    Complex conj = conjugate(c1);
    std::cout << "Conjugate of c1: " << conj << std::endl;

    Complex sum = c1 + c2;  // Uses friend operator+
    Complex product = c1 * c2;  // Uses friend operator*

    std::cout << "c1 + c2 = " << sum << std::endl;
    std::cout << "c1 * c2 = " << product << std::endl;
    std::cout << "c1 == c2: " << std::boolalpha << (c1 == c2) << std::endl;

    // Test Vector3D friend functions
    Vector3D v1(1.0, 2.0, 3.0);
    Vector3D v2(4.0, 5.0, 6.0);

    std::cout << "\nVector operations:" << std::endl;
    std::cout << "v1: " << v1 << std::endl;
    std::cout << "v2: " << v2 << std::endl;

    double distance = calculateDistance(v1, v2);
    double dot = dotProduct(v1, v2);
    Vector3D cross = crossProduct(v1, v2);
    Vector3D normalized = normalize(v1);

    std::cout << "Distance: " << distance << std::endl;
    std::cout << "Dot product: " << dot << std::endl;
    std::cout << "Cross product: " << cross << std::endl;
    std::cout << "Normalized v1: " << normalized << std::endl;

    Vector3D sum_v = v1 + v2;
    Vector3D diff_v = v1 - v2;
    Vector3D scaled_v = 2.5 * v1;

    std::cout << "v1 + v2 = " << sum_v << std::endl;
    std::cout << "v1 - v2 = " << diff_v << std::endl;
    std::cout << "2.5 * v1 = " << scaled_v << std::endl;
}

void testFriendClasses() {
    std::cout << "\n=== Testing Friend Classes ===" << std::endl;

    // Test ComplexAnalyzer (friend of Complex)
    Complex c1(3.0, 4.0);
    Complex c2(-2.0, 1.0);

    ComplexAnalyzer analyzer("Comprehensive");
    analyzer.analyzeComplex(c1);
    analyzer.compareComplexNumbers(c1, c2);

    // Test VectorOperations (friend of Vector3D)
    Vector3D v1(1.0, 0.0, 0.0);
    Vector3D v2(0.0, 1.0, 0.0);

    VectorOperations::performAnalysis(v1, v2);

    Vector3D average = VectorOperations::computeAverage(v1, v2);
    std::cout << "Average vector: " << average << std::endl;
}

void demonstrateFriendFunctions() {
    testFriendFunctions();
    testFriendClasses();
}
""",
    )

    run_updater(cpp_friend_project, mock_ingestor)

    project_name = cpp_friend_project.name

    expected_classes = [
        f"{project_name}.friend_functions.Complex",
        f"{project_name}.friend_functions.ComplexAnalyzer",
        f"{project_name}.friend_functions.Vector3D",
        f"{project_name}.friend_functions.VectorOperations",
    ]

    expected_functions = [
        f"{project_name}.friend_functions.testFriendFunctions",
        f"{project_name}.friend_functions.testFriendClasses",
        f"{project_name}.friend_functions.demonstrateFriendFunctions",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_friend_templates(
    cpp_friend_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test friend templates and template specializations."""
    test_file = cpp_friend_project / "friend_templates.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <vector>
#include <string>
#include <type_traits>

// Forward declarations for template friends
template<typename T> class Container;
template<typename T> void printContainer(const Container<T>& container);
template<typename T> Container<T> mergeContainers(const Container<T>& a, const Container<T>& b);

// Container class with template friend functions
template<typename T>
class Container {
private:
    std::vector<T> data_;
    std::string name_;

public:
    Container(const std::string& name) : name_(name) {}

    void add(const T& item) {
        data_.push_back(item);
    }

    size_t size() const { return data_.size(); }
    bool empty() const { return data_.empty(); }

    // Template friend function
    template<typename U>
    friend void printContainer(const Container<U>& container);

    // Template friend function with same template parameter
    friend void printContainer<T>(const Container<T>& container);

    // Template friend function for merging
    friend Container<T> mergeContainers<T>(const Container<T>& a, const Container<T>& b);

    // Friend class template
    template<typename U>
    friend class ContainerAnalyzer;

    // Specific friend for Container<int>
    friend class SpecializedIntHandler;
};

// Template friend function implementations
template<typename T>
void printContainer(const Container<T>& container) {
    std::cout << "Container '" << container.name_ << "' contains " << container.data_.size() << " items:" << std::endl;
    for (size_t i = 0; i < container.data_.size(); ++i) {
        std::cout << "  [" << i << "] = " << container.data_[i] << std::endl;
    }
}

template<typename T>
Container<T> mergeContainers(const Container<T>& a, const Container<T>& b) {
    Container<T> result(a.name_ + "_merged_with_" + b.name_);

    // Access private members directly
    result.data_.reserve(a.data_.size() + b.data_.size());
    result.data_.insert(result.data_.end(), a.data_.begin(), a.data_.end());
    result.data_.insert(result.data_.end(), b.data_.begin(), b.data_.end());

    return result;
}

// Friend class template
template<typename T>
class ContainerAnalyzer {
private:
    std::string analyzer_name_;

public:
    ContainerAnalyzer(const std::string& name) : analyzer_name_(name) {}

    void analyzeContainer(const Container<T>& container) {
        std::cout << "=== " << analyzer_name_ << " Analysis ===" << std::endl;

        // Direct access to private members
        std::cout << "Container name: " << container.name_ << std::endl;
        std::cout << "Number of elements: " << container.data_.size() << std::endl;

        if (!container.data_.empty()) {
            std::cout << "First element: " << container.data_.front() << std::endl;
            std::cout << "Last element: " << container.data_.back() << std::endl;

            // Type-specific analysis
            if constexpr (std::is_arithmetic_v<T>) {
                T sum = T{};
                for (const T& item : container.data_) {
                    sum += item;
                }
                std::cout << "Sum of elements: " << sum << std::endl;
                std::cout << "Average: " << static_cast<double>(sum) / container.data_.size() << std::endl;
            }
        }
    }

    void compareContainers(const Container<T>& a, const Container<T>& b) {
        std::cout << "=== Container Comparison ===" << std::endl;

        std::cout << "Container A ('" << a.name_ << "'): " << a.data_.size() << " elements" << std::endl;
        std::cout << "Container B ('" << b.name_ << "'): " << b.data_.size() << " elements" << std::endl;

        if (a.data_.size() > b.data_.size()) {
            std::cout << "Container A is larger" << std::endl;
        } else if (b.data_.size() > a.data_.size()) {
            std::cout << "Container B is larger" << std::endl;
        } else {
            std::cout << "Containers have equal size" << std::endl;
        }
    }
};

// Specialized friend class for Container<int>
class SpecializedIntHandler {
public:
    static void processIntContainer(Container<int>& container) {
        std::cout << "=== Specialized Int Container Processing ===" << std::endl;

        // Direct access to private members
        std::cout << "Processing container: " << container.name_ << std::endl;

        // Perform int-specific operations
        if (!container.data_.empty()) {
            // Sort the data
            std::sort(container.data_.begin(), container.data_.end());

            // Remove duplicates
            auto last = std::unique(container.data_.begin(), container.data_.end());
            container.data_.erase(last, container.data_.end());

            // Calculate statistics
            int sum = 0;
            int min_val = container.data_.front();
            int max_val = container.data_.back();

            for (int val : container.data_) {
                sum += val;
            }

            std::cout << "Processed " << container.data_.size() << " unique elements" << std::endl;
            std::cout << "Sum: " << sum << ", Min: " << min_val << ", Max: " << max_val << std::endl;
            std::cout << "Average: " << static_cast<double>(sum) / container.data_.size() << std::endl;
        }
    }

    static void addPerfectSquares(Container<int>& container, int count) {
        std::cout << "Adding " << count << " perfect squares to " << container.name_ << std::endl;

        for (int i = 1; i <= count; ++i) {
            container.data_.push_back(i * i);
        }
    }
};

// Matrix class with friend template functions
template<typename T>
class Matrix {
private:
    std::vector<std::vector<T>> data_;
    size_t rows_, cols_;

public:
    Matrix(size_t rows, size_t cols) : rows_(rows), cols_(cols) {
        data_.resize(rows, std::vector<T>(cols, T{}));
    }

    T& operator()(size_t row, size_t col) {
        return data_[row][col];
    }

    const T& operator()(size_t row, size_t col) const {
        return data_[row][col];
    }

    size_t rows() const { return rows_; }
    size_t cols() const { return cols_; }

    // Template friend functions
    template<typename U>
    friend void printMatrix(const Matrix<U>& matrix);

    template<typename U>
    friend Matrix<U> transpose(const Matrix<U>& matrix);

    template<typename U>
    friend Matrix<U> multiply(const Matrix<U>& a, const Matrix<U>& b);
};

// Template friend function implementations for Matrix
template<typename T>
void printMatrix(const Matrix<T>& matrix) {
    std::cout << "Matrix " << matrix.rows_ << "x" << matrix.cols_ << ":" << std::endl;
    for (size_t i = 0; i < matrix.rows_; ++i) {
        for (size_t j = 0; j < matrix.cols_; ++j) {
            std::cout << matrix.data_[i][j] << " ";
        }
        std::cout << std::endl;
    }
}

template<typename T>
Matrix<T> transpose(const Matrix<T>& matrix) {
    Matrix<T> result(matrix.cols_, matrix.rows_);

    for (size_t i = 0; i < matrix.rows_; ++i) {
        for (size_t j = 0; j < matrix.cols_; ++j) {
            result.data_[j][i] = matrix.data_[i][j];
        }
    }

    return result;
}

template<typename T>
Matrix<T> multiply(const Matrix<T>& a, const Matrix<T>& b) {
    if (a.cols_ != b.rows_) {
        throw std::invalid_argument("Matrix dimensions incompatible for multiplication");
    }

    Matrix<T> result(a.rows_, b.cols_);

    for (size_t i = 0; i < a.rows_; ++i) {
        for (size_t j = 0; j < b.cols_; ++j) {
            for (size_t k = 0; k < a.cols_; ++k) {
                result.data_[i][j] += a.data_[i][k] * b.data_[k][j];
            }
        }
    }

    return result;
}

void testTemplateFriends() {
    std::cout << "=== Testing Template Friends ===" << std::endl;

    // Test Container with different types
    Container<int> intContainer("IntNumbers");
    intContainer.add(10);
    intContainer.add(20);
    intContainer.add(30);
    intContainer.add(20);  // Duplicate for specialized processing

    Container<std::string> stringContainer("Words");
    stringContainer.add("hello");
    stringContainer.add("world");
    stringContainer.add("cpp");

    Container<double> doubleContainer("Doubles");
    doubleContainer.add(3.14);
    doubleContainer.add(2.71);
    doubleContainer.add(1.41);

    // Use template friend functions
    printContainer(intContainer);
    printContainer(stringContainer);
    printContainer(doubleContainer);

    // Test container merging
    Container<int> anotherIntContainer("MoreInts");
    anotherIntContainer.add(40);
    anotherIntContainer.add(50);

    Container<int> merged = mergeContainers(intContainer, anotherIntContainer);
    printContainer(merged);

    // Test template friend classes
    ContainerAnalyzer<int> intAnalyzer("Integer Analyzer");
    intAnalyzer.analyzeContainer(intContainer);
    intAnalyzer.compareContainers(intContainer, anotherIntContainer);

    ContainerAnalyzer<std::string> stringAnalyzer("String Analyzer");
    stringAnalyzer.analyzeContainer(stringContainer);

    // Test specialized friend class
    SpecializedIntHandler::processIntContainer(intContainer);
    printContainer(intContainer);  // Show processed result

    SpecializedIntHandler::addPerfectSquares(intContainer, 5);
    printContainer(intContainer);

    // Test Matrix friend templates
    Matrix<int> matrix1(2, 3);
    matrix1(0, 0) = 1; matrix1(0, 1) = 2; matrix1(0, 2) = 3;
    matrix1(1, 0) = 4; matrix1(1, 1) = 5; matrix1(1, 2) = 6;

    Matrix<int> matrix2(3, 2);
    matrix2(0, 0) = 7; matrix2(0, 1) = 8;
    matrix2(1, 0) = 9; matrix2(1, 1) = 10;
    matrix2(2, 0) = 11; matrix2(2, 1) = 12;

    std::cout << "\nMatrix operations:" << std::endl;
    printMatrix(matrix1);
    printMatrix(matrix2);

    Matrix<int> transposed = transpose(matrix1);
    std::cout << "Transposed matrix1:" << std::endl;
    printMatrix(transposed);

    Matrix<int> product = multiply(matrix1, matrix2);
    std::cout << "Matrix multiplication result:" << std::endl;
    printMatrix(product);
}

void demonstrateTemplateFriends() {
    testTemplateFriends();
}
""",
    )

    run_updater(cpp_friend_project, mock_ingestor)

    project_name = cpp_friend_project.name

    expected_functions = [
        f"{project_name}.friend_templates.testTemplateFriends",
        f"{project_name}.friend_templates.demonstrateTemplateFriends",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_functions = [func for func in expected_functions if func in created_functions]
    assert len(found_functions) >= 1, (
        f"Expected at least 1 friend template function, found {len(found_functions)}: {found_functions}"
    )


def test_cpp_friend_comprehensive(
    cpp_friend_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all friend relationship features create proper relationships."""
    test_file = cpp_friend_project / "comprehensive_friends.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Comprehensive friend relationships example
#include <iostream>
#include <string>
#include <vector>

// Forward declarations
class NodeGraph;
class EdgeProcessor;
template<typename T> class GraphAnalyzer;

class GraphNode {
private:
    int id_;
    std::string label_;
    std::vector<int> connections_;

public:
    GraphNode(int id, const std::string& label) : id_(id), label_(label) {}

    void addConnection(int target_id) {
        connections_.push_back(target_id);
    }

    // Friend function for graph building
    friend void connectNodes(GraphNode& from, GraphNode& to);

    // Friend class for graph operations
    friend class NodeGraph;
    friend class EdgeProcessor;

    // Friend template class
    template<typename T>
    friend class GraphAnalyzer;

    // Friend operators
    friend std::ostream& operator<<(std::ostream& os, const GraphNode& node);
    friend bool operator==(const GraphNode& a, const GraphNode& b);
};

// Friend function implementation
void connectNodes(GraphNode& from, GraphNode& to) {
    from.connections_.push_back(to.id_);  // Access private member
    std::cout << "Connected " << from.label_ << " to " << to.label_ << std::endl;
}

// Friend operators
std::ostream& operator<<(std::ostream& os, const GraphNode& node) {
    os << "Node(" << node.id_ << ", \"" << node.label_ << "\", " << node.connections_.size() << " connections)";
    return os;
}

bool operator==(const GraphNode& a, const GraphNode& b) {
    return a.id_ == b.id_;
}

// Friend class implementations
class NodeGraph {
private:
    std::vector<GraphNode> nodes_;

public:
    void addNode(const GraphNode& node) {
        nodes_.push_back(node);
    }

    void analyzeNode(const GraphNode& node) {
        // Direct access to private members due to friend relationship
        std::cout << "Analyzing node " << node.id_ << " ('" << node.label_ << "')" << std::endl;
        std::cout << "  Connections: " << node.connections_.size() << std::endl;

        for (int connection_id : node.connections_) {
            std::cout << "    -> " << connection_id << std::endl;
        }
    }

    void printAllNodes() {
        std::cout << "Graph contains " << nodes_.size() << " nodes:" << std::endl;
        for (const auto& node : nodes_) {
            analyzeNode(node);
        }
    }
};

class EdgeProcessor {
public:
    static int countTotalEdges(const std::vector<GraphNode>& nodes) {
        int total = 0;
        for (const auto& node : nodes) {
            // Direct access to private connections_
            total += node.connections_.size();
        }
        return total;
    }

    static void validateConnections(const GraphNode& node, const std::vector<GraphNode>& all_nodes) {
        std::cout << "Validating connections for " << node.label_ << ":" << std::endl;

        for (int connection_id : node.connections_) {
            bool found = false;
            for (const auto& other_node : all_nodes) {
                if (other_node.id_ == connection_id) {
                    found = true;
                    std::cout << "  Valid connection to " << other_node.label_ << std::endl;
                    break;
                }
            }
            if (!found) {
                std::cout << "  Invalid connection to ID " << connection_id << std::endl;
            }
        }
    }
};

// Friend template class
template<typename T>
class GraphAnalyzer {
private:
    T analysis_data_;

public:
    GraphAnalyzer(const T& data) : analysis_data_(data) {}

    void performAnalysis(const GraphNode& node) {
        std::cout << "=== Graph Analysis (Type: " << typeid(T).name() << ") ===" << std::endl;

        // Direct access to private members
        std::cout << "Node ID: " << node.id_ << std::endl;
        std::cout << "Node Label: " << node.label_ << std::endl;
        std::cout << "Degree (connections): " << node.connections_.size() << std::endl;

        // Use analysis data
        std::cout << "Analysis data: " << analysis_data_ << std::endl;

        // Determine node type based on connections
        if (node.connections_.empty()) {
            std::cout << "Node type: Isolated" << std::endl;
        } else if (node.connections_.size() == 1) {
            std::cout << "Node type: Leaf" << std::endl;
        } else {
            std::cout << "Node type: Hub" << std::endl;
        }
    }
};

void demonstrateComprehensiveFriends() {
    std::cout << "=== Comprehensive Friend Relationships Demo ===" << std::endl;

    // Create nodes
    GraphNode node1(1, "Function_A");
    GraphNode node2(2, "Function_B");
    GraphNode node3(3, "Class_X");
    GraphNode node4(4, "Class_Y");

    // Use friend function to connect nodes
    connectNodes(node1, node2);
    connectNodes(node1, node3);
    connectNodes(node2, node4);
    connectNodes(node3, node4);

    // Use friend operators
    std::cout << "Node details:" << std::endl;
    std::cout << "  " << node1 << std::endl;
    std::cout << "  " << node2 << std::endl;
    std::cout << "  " << node3 << std::endl;
    std::cout << "  " << node4 << std::endl;

    std::cout << "node1 == node2: " << std::boolalpha << (node1 == node2) << std::endl;

    // Use friend class NodeGraph
    NodeGraph graph;
    graph.addNode(node1);
    graph.addNode(node2);
    graph.addNode(node3);
    graph.addNode(node4);

    graph.printAllNodes();

    // Use friend class EdgeProcessor
    std::vector<GraphNode> all_nodes = {node1, node2, node3, node4};
    int total_edges = EdgeProcessor::countTotalEdges(all_nodes);
    std::cout << "Total edges in graph: " << total_edges << std::endl;

    EdgeProcessor::validateConnections(node1, all_nodes);

    // Use friend template class
    GraphAnalyzer<std::string> string_analyzer("String-based analysis");
    string_analyzer.performAnalysis(node1);

    GraphAnalyzer<int> int_analyzer(42);
    int_analyzer.performAnalysis(node3);

    GraphAnalyzer<double> double_analyzer(3.14159);
    double_analyzer.performAnalysis(node4);
}
""",
    )

    run_updater(cpp_friend_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_friends" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 3, (
        f"Expected at least 3 comprehensive friend calls, found {len(comprehensive_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
