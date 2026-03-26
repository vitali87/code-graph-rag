from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_constexpr_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with constexpr and compile-time programming patterns."""
    project_path = temp_repo / "cpp_constexpr_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_basic_constexpr(
    cpp_constexpr_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic constexpr functions and variables."""
    test_file = cpp_constexpr_project / "basic_constexpr.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <array>
#include <string_view>
#include <type_traits>

// Basic constexpr functions
constexpr int square(int x) {
    return x * x;
}

constexpr int factorial(int n) {
    return (n <= 1) ? 1 : n * factorial(n - 1);
}

constexpr int fibonacci(int n) {
    return (n <= 1) ? n : fibonacci(n - 1) + fibonacci(n - 2);
}

constexpr double power(double base, int exp) {
    return (exp == 0) ? 1.0 : base * power(base, exp - 1);
}

// Constexpr variables
constexpr int BUFFER_SIZE = 1024;
constexpr double PI = 3.14159265359;
constexpr int MAX_NODES = square(100);  // Computed at compile time
constexpr int FACTORIAL_10 = factorial(10);

// Compile-time string operations
constexpr size_t string_length(const char* str) {
    size_t len = 0;
    while (str[len] != '\\0') {
        ++len;
    }
    return len;
}

constexpr bool string_equal(const char* a, const char* b) {
    while (*a && *b && *a == *b) {
        ++a;
        ++b;
    }
    return *a == *b;
}

constexpr const char* find_char(const char* str, char c) {
    while (*str && *str != c) {
        ++str;
    }
    return (*str == c) ? str : nullptr;
}

// Constexpr class for compile-time computations
class MathUtilities {
public:
    static constexpr int gcd(int a, int b) {
        return (b == 0) ? a : gcd(b, a % b);
    }

    static constexpr int lcm(int a, int b) {
        return (a * b) / gcd(a, b);
    }

    static constexpr bool is_prime(int n) {
        if (n <= 1) return false;
        if (n <= 3) return true;
        if (n % 2 == 0 || n % 3 == 0) return false;

        for (int i = 5; i * i <= n; i += 6) {
            if (n % i == 0 || n % (i + 2) == 0) {
                return false;
            }
        }
        return true;
    }

    static constexpr int count_primes_up_to(int limit) {
        int count = 0;
        for (int i = 2; i <= limit; ++i) {
            if (is_prime(i)) {
                ++count;
            }
        }
        return count;
    }
};

// Constexpr array generation
template<size_t N>
constexpr std::array<int, N> generate_squares() {
    std::array<int, N> arr{};
    for (size_t i = 0; i < N; ++i) {
        arr[i] = static_cast<int>(i * i);
    }
    return arr;
}

template<size_t N>
constexpr std::array<int, N> generate_fibonacci_sequence() {
    std::array<int, N> arr{};
    if (N > 0) arr[0] = 0;
    if (N > 1) arr[1] = 1;

    for (size_t i = 2; i < N; ++i) {
        arr[i] = arr[i-1] + arr[i-2];
    }
    return arr;
}

// Constexpr data structures
template<typename T, size_t Capacity>
class ConstexprVector {
private:
    T data_[Capacity];
    size_t size_;

public:
    constexpr ConstexprVector() : data_{}, size_(0) {}

    constexpr void push_back(const T& value) {
        if (size_ < Capacity) {
            data_[size_++] = value;
        }
    }

    constexpr const T& operator[](size_t index) const {
        return data_[index];
    }

    constexpr T& operator[](size_t index) {
        return data_[index];
    }

    constexpr size_t size() const { return size_; }
    constexpr size_t capacity() const { return Capacity; }

    constexpr const T* begin() const { return data_; }
    constexpr const T* end() const { return data_ + size_; }

    constexpr T* begin() { return data_; }
    constexpr T* end() { return data_ + size_; }
};

template<size_t N>
constexpr ConstexprVector<int, N> create_prime_vector() {
    ConstexprVector<int, N> primes;
    for (int i = 2; primes.size() < N; ++i) {
        if (MathUtilities::is_prime(i)) {
            primes.push_back(i);
        }
    }
    return primes;
}

class ConstexprDemo {
private:
    static constexpr int DEMO_SIZE = 10;
    static constexpr auto squares_ = generate_squares<DEMO_SIZE>();
    static constexpr auto fibonacci_ = generate_fibonacci_sequence<DEMO_SIZE>();
    static constexpr auto primes_ = create_prime_vector<DEMO_SIZE>();

public:
    void demonstrateBasicConstexpr() const {
        std::cout << "=== Basic Constexpr Demonstrations ===" << std::endl;

        // Compile-time constants
        std::cout << "Compile-time constants:" << std::endl;
        std::cout << "  BUFFER_SIZE: " << BUFFER_SIZE << std::endl;
        std::cout << "  PI: " << PI << std::endl;
        std::cout << "  MAX_NODES: " << MAX_NODES << std::endl;
        std::cout << "  FACTORIAL_10: " << FACTORIAL_10 << std::endl;

        // Constexpr function results
        std::cout << "\\nConstexpr function results:" << std::endl;
        constexpr int sq_15 = square(15);
        constexpr int fact_8 = factorial(8);
        constexpr int fib_10 = fibonacci(10);
        constexpr double pow_2_8 = power(2.0, 8);

        std::cout << "  square(15): " << sq_15 << std::endl;
        std::cout << "  factorial(8): " << fact_8 << std::endl;
        std::cout << "  fibonacci(10): " << fib_10 << std::endl;
        std::cout << "  power(2.0, 8): " << pow_2_8 << std::endl;

        // String operations
        std::cout << "\\nConstexpr string operations:" << std::endl;
        constexpr const char* test_str = "Hello, constexpr!";
        constexpr size_t str_len = string_length(test_str);
        constexpr bool strings_equal = string_equal("test", "test");
        constexpr const char* found_char = find_char(test_str, 'c');

        std::cout << "  String: " << test_str << std::endl;
        std::cout << "  Length: " << str_len << std::endl;
        std::cout << "  Strings equal: " << std::boolalpha << strings_equal << std::endl;
        std::cout << "  Found 'c' at: " << (found_char ? found_char : "not found") << std::endl;
    }

    void demonstrateMathUtilities() const {
        std::cout << "=== Math Utilities Demo ===" << std::endl;

        constexpr int gcd_result = MathUtilities::gcd(48, 18);
        constexpr int lcm_result = MathUtilities::lcm(12, 15);
        constexpr bool is_17_prime = MathUtilities::is_prime(17);
        constexpr bool is_15_prime = MathUtilities::is_prime(15);
        constexpr int prime_count = MathUtilities::count_primes_up_to(50);

        std::cout << "  GCD(48, 18): " << gcd_result << std::endl;
        std::cout << "  LCM(12, 15): " << lcm_result << std::endl;
        std::cout << "  Is 17 prime: " << std::boolalpha << is_17_prime << std::endl;
        std::cout << "  Is 15 prime: " << std::boolalpha << is_15_prime << std::endl;
        std::cout << "  Prime count up to 50: " << prime_count << std::endl;
    }

    void demonstrateConstexprArrays() const {
        std::cout << "=== Constexpr Arrays Demo ===" << std::endl;

        std::cout << "Squares: ";
        for (size_t i = 0; i < squares_.size(); ++i) {
            std::cout << squares_[i] << " ";
        }
        std::cout << std::endl;

        std::cout << "Fibonacci: ";
        for (size_t i = 0; i < fibonacci_.size(); ++i) {
            std::cout << fibonacci_[i] << " ";
        }
        std::cout << std::endl;

        std::cout << "Primes: ";
        for (size_t i = 0; i < primes_.size(); ++i) {
            std::cout << primes_[i] << " ";
        }
        std::cout << std::endl;
    }

    void demonstrateConstexprVector() const {
        std::cout << "=== Constexpr Vector Demo ===" << std::endl;

        constexpr auto create_test_vector = []() {
            ConstexprVector<int, 5> vec;
            vec.push_back(10);
            vec.push_back(20);
            vec.push_back(30);
            vec.push_back(40);
            vec.push_back(50);
            return vec;
        };

        constexpr auto test_vec = create_test_vector();

        std::cout << "Constexpr vector contents: ";
        for (size_t i = 0; i < test_vec.size(); ++i) {
            std::cout << test_vec[i] << " ";
        }
        std::cout << std::endl;
        std::cout << "Vector size: " << test_vec.size() << std::endl;
        std::cout << "Vector capacity: " << test_vec.capacity() << std::endl;
    }
};

void testBasicConstexprFeatures() {
    ConstexprDemo demo;
    demo.demonstrateBasicConstexpr();
    demo.demonstrateMathUtilities();
    demo.demonstrateConstexprArrays();
    demo.demonstrateConstexprVector();
}

// Runtime vs compile-time demonstration
void demonstrateCompileTimeVsRuntime() {
    std::cout << "=== Compile-time vs Runtime Demo ===" << std::endl;

    // Compile-time computation
    constexpr int compile_time_result = factorial(10);
    std::cout << "Compile-time factorial(10): " << compile_time_result << std::endl;

    // Runtime computation
    int n = 10;  // Not constexpr, so factorial will be computed at runtime
    int runtime_result = factorial(n);
    std::cout << "Runtime factorial(10): " << runtime_result << std::endl;

    // Demonstrate with arrays
    constexpr auto compile_time_array = generate_squares<5>();
    std::cout << "Compile-time generated squares: ";
    for (const auto& val : compile_time_array) {
        std::cout << val << " ";
    }
    std::cout << std::endl;

    // Show that constexpr can be used in template parameters
    std::array<int, factorial(5)> sized_by_factorial;
    std::cout << "Array sized by factorial(5): " << sized_by_factorial.size() << " elements" << std::endl;
}

void demonstrateBasicConstexpr() {
    testBasicConstexprFeatures();
    demonstrateCompileTimeVsRuntime();
}
""",
    )

    run_updater(cpp_constexpr_project, mock_ingestor)

    project_name = cpp_constexpr_project.name

    expected_classes = [
        f"{project_name}.basic_constexpr.MathUtilities",
        f"{project_name}.basic_constexpr.ConstexprDemo",
    ]

    expected_functions = [
        f"{project_name}.basic_constexpr.square",
        f"{project_name}.basic_constexpr.factorial",
        f"{project_name}.basic_constexpr.fibonacci",
        f"{project_name}.basic_constexpr.testBasicConstexprFeatures",
        f"{project_name}.basic_constexpr.demonstrateBasicConstexpr",
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


def test_constexpr_if_and_templates(
    cpp_constexpr_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test constexpr if and template metaprogramming."""
    test_file = cpp_constexpr_project / "constexpr_if_templates.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <type_traits>
#include <string>
#include <vector>

// constexpr if demonstrations (C++17)
template<typename T>
constexpr auto process_value(T value) {
    if constexpr (std::is_integral_v<T>) {
        return value * 2;  // Double integers
    } else if constexpr (std::is_floating_point_v<T>) {
        return value * 3.14;  // Multiply floats by pi
    } else if constexpr (std::is_same_v<T, std::string>) {
        return value + "_processed";  // Append to strings
    } else {
        return value;  // Return as-is for other types
    }
}

template<typename Container>
constexpr size_t process_container(const Container& container) {
    if constexpr (std::is_same_v<Container, std::string>) {
        return container.length();
    } else if constexpr (requires { container.size(); }) {
        return container.size();
    } else {
        return 0;
    }
}

// Type trait utilities
template<typename T>
struct TypeInfo {
    static constexpr bool is_numeric = std::is_arithmetic_v<T>;
    static constexpr bool is_pointer = std::is_pointer_v<T>;
    static constexpr bool is_reference = std::is_reference_v<T>;
    static constexpr size_t type_size = sizeof(T);

    static constexpr const char* type_name() {
        if constexpr (std::is_same_v<T, int>) {
            return "int";
        } else if constexpr (std::is_same_v<T, double>) {
            return "double";
        } else if constexpr (std::is_same_v<T, std::string>) {
            return "string";
        } else if constexpr (std::is_pointer_v<T>) {
            return "pointer";
        } else {
            return "unknown";
        }
    }
};

// Compile-time algorithm selection
template<typename Iterator>
constexpr void sort_range(Iterator first, Iterator last) {
    using ValueType = typename std::iterator_traits<Iterator>::value_type;

    if constexpr (std::is_arithmetic_v<ValueType> &&
                  std::is_same_v<typename std::iterator_traits<Iterator>::iterator_category,
                                std::random_access_iterator_tag>) {
        // Use optimized sort for arithmetic types with random access
        std::cout << "Using optimized numeric sort" << std::endl;
        std::sort(first, last);
    } else {
        // Use generic sort
        std::cout << "Using generic sort" << std::endl;
        std::sort(first, last);
    }
}

// Constexpr factory pattern
template<typename T>
class ConstexprFactory {
public:
    template<typename... Args>
    static constexpr T create(Args&&... args) {
        if constexpr (std::is_default_constructible_v<T> && sizeof...(args) == 0) {
            return T{};
        } else if constexpr (std::is_constructible_v<T, Args...>) {
            return T{std::forward<Args>(args)...};
        } else {
            static_assert(std::is_constructible_v<T, Args...>, "Cannot construct type with given arguments");
        }
    }

    static constexpr bool can_create_default() {
        return std::is_default_constructible_v<T>;
    }

    template<typename... Args>
    static constexpr bool can_create_with_args() {
        return std::is_constructible_v<T, Args...>;
    }
};

// Compile-time string processing
template<size_t N>
class ConstexprString {
private:
    char data_[N + 1];
    size_t length_;

public:
    constexpr ConstexprString() : data_{}, length_(0) {}

    constexpr ConstexprString(const char (&str)[N + 1]) : length_(N) {
        for (size_t i = 0; i <= N; ++i) {
            data_[i] = str[i];
        }
    }

    constexpr char operator[](size_t index) const {
        return data_[index];
    }

    constexpr size_t size() const { return length_; }

    constexpr bool contains(char c) const {
        for (size_t i = 0; i < length_; ++i) {
            if (data_[i] == c) return true;
        }
        return false;
    }

    constexpr size_t count(char c) const {
        size_t count = 0;
        for (size_t i = 0; i < length_; ++i) {
            if (data_[i] == c) ++count;
        }
        return count;
    }

    template<size_t M>
    constexpr auto concatenate(const ConstexprString<M>& other) const {
        ConstexprString<N + M> result;

        // Copy this string
        for (size_t i = 0; i < length_; ++i) {
            result.data_[i] = data_[i];
        }

        // Copy other string
        for (size_t i = 0; i < other.length_; ++i) {
            result.data_[length_ + i] = other.data_[i];
        }

        result.data_[N + M] = '\\0';
        result.length_ = N + M;
        return result;
    }

    constexpr const char* c_str() const { return data_; }
};

// Deduction guide for ConstexprString
template<size_t N>
ConstexprString(const char (&)[N]) -> ConstexprString<N - 1>;

class ConstexprIfDemo {
public:
    void demonstrateConstexprIf() const {
        std::cout << "=== Constexpr If Demonstrations ===" << std::endl;

        // Type-based processing
        auto int_result = process_value(42);
        auto double_result = process_value(3.14);
        auto string_result = process_value(std::string("hello"));

        std::cout << "Integer result: " << int_result << std::endl;
        std::cout << "Double result: " << double_result << std::endl;
        std::cout << "String result: " << string_result << std::endl;

        // Container processing
        std::string str = "constexpr";
        std::vector<int> vec = {1, 2, 3, 4, 5};

        std::cout << "String size: " << process_container(str) << std::endl;
        std::cout << "Vector size: " << process_container(vec) << std::endl;
    }

    void demonstrateTypeInfo() const {
        std::cout << "=== Type Info Demonstrations ===" << std::endl;

        std::cout << "int info:" << std::endl;
        std::cout << "  is_numeric: " << std::boolalpha << TypeInfo<int>::is_numeric << std::endl;
        std::cout << "  type_size: " << TypeInfo<int>::type_size << std::endl;
        std::cout << "  type_name: " << TypeInfo<int>::type_name() << std::endl;

        std::cout << "double* info:" << std::endl;
        std::cout << "  is_pointer: " << std::boolalpha << TypeInfo<double*>::is_pointer << std::endl;
        std::cout << "  type_size: " << TypeInfo<double*>::type_size << std::endl;
        std::cout << "  type_name: " << TypeInfo<double*>::type_name() << std::endl;

        std::cout << "string info:" << std::endl;
        std::cout << "  is_numeric: " << std::boolalpha << TypeInfo<std::string>::is_numeric << std::endl;
        std::cout << "  type_size: " << TypeInfo<std::string>::type_size << std::endl;
        std::cout << "  type_name: " << TypeInfo<std::string>::type_name() << std::endl;
    }

    void demonstrateConstexprFactory() const {
        std::cout << "=== Constexpr Factory Demonstrations ===" << std::endl;

        // Factory capabilities at compile time
        constexpr bool can_create_int_default = ConstexprFactory<int>::can_create_default();
        constexpr bool can_create_int_with_value = ConstexprFactory<int>::can_create_with_args<int>();

        std::cout << "Can create int with default constructor: " << std::boolalpha << can_create_int_default << std::endl;
        std::cout << "Can create int with value: " << std::boolalpha << can_create_int_with_value << std::endl;

        // Create objects
        constexpr auto default_int = ConstexprFactory<int>::create();
        constexpr auto value_int = ConstexprFactory<int>::create(42);

        std::cout << "Default int: " << default_int << std::endl;
        std::cout << "Value int: " << value_int << std::endl;
    }

    void demonstrateConstexprString() const {
        std::cout << "=== Constexpr String Demonstrations ===" << std::endl;

        constexpr ConstexprString str1("Hello");
        constexpr ConstexprString str2("World");
        constexpr auto concatenated = str1.concatenate(str2);

        std::cout << "String 1: " << str1.c_str() << " (size: " << str1.size() << ")" << std::endl;
        std::cout << "String 2: " << str2.c_str() << " (size: " << str2.size() << ")" << std::endl;
        std::cout << "Concatenated: " << concatenated.c_str() << " (size: " << concatenated.size() << ")" << std::endl;

        constexpr bool contains_l = str1.contains('l');
        constexpr size_t count_l = str1.count('l');

        std::cout << "String 1 contains 'l': " << std::boolalpha << contains_l << std::endl;
        std::cout << "Count of 'l' in string 1: " << count_l << std::endl;
    }

    void demonstrateAlgorithmSelection() const {
        std::cout << "=== Algorithm Selection Demonstrations ===" << std::endl;

        std::vector<int> int_vec = {5, 2, 8, 1, 9};
        std::vector<std::string> string_vec = {"zebra", "apple", "banana"};

        std::cout << "Sorting integer vector:" << std::endl;
        sort_range(int_vec.begin(), int_vec.end());
        for (const auto& val : int_vec) {
            std::cout << val << " ";
        }
        std::cout << std::endl;

        std::cout << "Sorting string vector:" << std::endl;
        sort_range(string_vec.begin(), string_vec.end());
        for (const auto& val : string_vec) {
            std::cout << val << " ";
        }
        std::cout << std::endl;
    }
};

void testConstexprIfAndTemplates() {
    ConstexprIfDemo demo;
    demo.demonstrateConstexprIf();
    demo.demonstrateTypeInfo();
    demo.demonstrateConstexprFactory();
    demo.demonstrateConstexprString();
    demo.demonstrateAlgorithmSelection();
}

void demonstrateConstexprIfAndTemplates() {
    testConstexprIfAndTemplates();
}
""",
    )

    run_updater(cpp_constexpr_project, mock_ingestor)

    project_name = cpp_constexpr_project.name

    expected_classes = [
        f"{project_name}.constexpr_if_templates.ConstexprIfDemo",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 constexpr if class, found {len(found_classes)}: {found_classes}"
    )


def test_cpp_constexpr_comprehensive(
    cpp_constexpr_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all constexpr and compile-time patterns create proper relationships."""
    test_file = cpp_constexpr_project / "comprehensive_constexpr.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Comprehensive constexpr and compile-time programming demonstration
#include <iostream>
#include <array>
#include <type_traits>

// Compile-time graph algorithms
template<size_t NumNodes>
class ConstexprGraph {
private:
    bool adjacency_matrix_[NumNodes][NumNodes];
    size_t num_edges_;

public:
    constexpr ConstexprGraph() : adjacency_matrix_{}, num_edges_(0) {}

    constexpr void add_edge(size_t from, size_t to) {
        if (from < NumNodes && to < NumNodes && !adjacency_matrix_[from][to]) {
            adjacency_matrix_[from][to] = true;
            ++num_edges_;
        }
    }

    constexpr bool has_edge(size_t from, size_t to) const {
        return (from < NumNodes && to < NumNodes) ? adjacency_matrix_[from][to] : false;
    }

    constexpr size_t count_edges() const { return num_edges_; }

    constexpr size_t out_degree(size_t node) const {
        if (node >= NumNodes) return 0;

        size_t degree = 0;
        for (size_t i = 0; i < NumNodes; ++i) {
            if (adjacency_matrix_[node][i]) {
                ++degree;
            }
        }
        return degree;
    }

    constexpr bool is_connected_to_all(size_t node) const {
        if (node >= NumNodes) return false;

        for (size_t i = 0; i < NumNodes; ++i) {
            if (i != node && !adjacency_matrix_[node][i]) {
                return false;
            }
        }
        return true;
    }
};

template<size_t N>
constexpr auto create_complete_graph() {
    ConstexprGraph<N> graph;
    for (size_t i = 0; i < N; ++i) {
        for (size_t j = 0; j < N; ++j) {
            if (i != j) {
                graph.add_edge(i, j);
            }
        }
    }
    return graph;
}

class ComprehensiveConstexprDemo {
public:
    void demonstrateComprehensiveConstexpr() {
        std::cout << "=== Comprehensive Constexpr Demo ===" << std::endl;

        // Compile-time graph creation
        constexpr auto complete_graph = create_complete_graph<4>();
        constexpr size_t total_edges = complete_graph.count_edges();
        constexpr size_t node_0_degree = complete_graph.out_degree(0);
        constexpr bool node_0_connected_to_all = complete_graph.is_connected_to_all(0);

        std::cout << "Complete graph with 4 nodes:" << std::endl;
        std::cout << "  Total edges: " << total_edges << std::endl;
        std::cout << "  Node 0 out-degree: " << node_0_degree << std::endl;
        std::cout << "  Node 0 connected to all: " << std::boolalpha << node_0_connected_to_all << std::endl;

        // Compile-time computation pipeline
        constexpr auto pipeline_result = []() {
            constexpr int base = 5;
            constexpr int squared = base * base;
            constexpr int cubed = squared * base;
            constexpr bool is_odd = (cubed % 2) == 1;
            return std::make_pair(cubed, is_odd);
        }();

        std::cout << "\\nPipeline computation:" << std::endl;
        std::cout << "  Result: " << pipeline_result.first << std::endl;
        std::cout << "  Is odd: " << std::boolalpha << pipeline_result.second << std::endl;
    }
};

void demonstrateComprehensiveConstexpr() {
    ComprehensiveConstexprDemo demo;
    demo.demonstrateComprehensiveConstexpr();
}
""",
    )

    run_updater(cpp_constexpr_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_constexpr" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 2, (
        f"Expected at least 2 comprehensive constexpr calls, found {len(comprehensive_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"

    print(
        "✅ C++ constexpr and compile-time programming relationship validation passed:"
    )
