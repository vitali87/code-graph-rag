from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_metaprogramming_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with template metaprogramming patterns."""
    project_path = temp_repo / "cpp_metaprogramming_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_basic_metaprogramming(
    cpp_metaprogramming_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic template metaprogramming techniques."""
    test_file = cpp_metaprogramming_project / "basic_metaprogramming.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <type_traits>
#include <string>
#include <vector>

// Compile-time factorial using template recursion
template<int N>
struct Factorial {
    static constexpr int value = N * Factorial<N - 1>::value;
};

template<>
struct Factorial<0> {
    static constexpr int value = 1;
};

// Compile-time Fibonacci using template recursion
template<int N>
struct Fibonacci {
    static constexpr int value = Fibonacci<N - 1>::value + Fibonacci<N - 2>::value;
};

template<>
struct Fibonacci<0> {
    static constexpr int value = 0;
};

template<>
struct Fibonacci<1> {
    static constexpr int value = 1;
};

// Type list metaprogramming
template<typename... Types>
struct TypeList {};

template<typename T, typename... Types>
struct TypeList<T, Types...> {
    using Head = T;
    using Tail = TypeList<Types...>;
    static constexpr size_t size = 1 + sizeof...(Types);
};

template<>
struct TypeList<> {
    static constexpr size_t size = 0;
};

// Get type at index
template<size_t Index, typename List>
struct TypeAt;

template<size_t Index, typename T, typename... Types>
struct TypeAt<Index, TypeList<T, Types...>> {
    using type = typename TypeAt<Index - 1, TypeList<Types...>>::type;
};

template<typename T, typename... Types>
struct TypeAt<0, TypeList<T, Types...>> {
    using type = T;
};

// Check if type is in list
template<typename T, typename List>
struct Contains;

template<typename T>
struct Contains<T, TypeList<>> {
    static constexpr bool value = false;
};

template<typename T, typename U, typename... Types>
struct Contains<T, TypeList<U, Types...>> {
    static constexpr bool value = std::is_same_v<T, U> || Contains<T, TypeList<Types...>>::value;
};

// SFINAE (Substitution Failure Is Not An Error) demonstrations
template<typename T>
class SFINAEDetector {
private:
    // Test for member function existence
    template<typename U>
    static auto test_size(int) -> decltype(std::declval<U>().size(), std::true_type{});
    template<typename>
    static std::false_type test_size(...);

    template<typename U>
    static auto test_push_back(int) -> decltype(std::declval<U>().push_back(std::declval<typename U::value_type>()), std::true_type{});
    template<typename>
    static std::false_type test_push_back(...);

    template<typename U>
    static auto test_iterator(int) -> decltype(std::declval<U>().begin(), std::declval<U>().end(), std::true_type{});
    template<typename>
    static std::false_type test_iterator(...);

public:
    static constexpr bool has_size = decltype(test_size<T>(0))::value;
    static constexpr bool has_push_back = decltype(test_push_back<T>(0))::value;
    static constexpr bool has_iterator = decltype(test_iterator<T>(0))::value;
};

// SFINAE-based function overloading
template<typename T>
std::enable_if_t<std::is_arithmetic_v<T>, void>
process_value(T value, const std::string& name) {
    std::cout << "Processing arithmetic " << name << ": " << value << " (doubled: " << value * 2 << ")" << std::endl;
}

template<typename T>
std::enable_if_t<std::is_same_v<T, std::string>, void>
process_value(const T& value, const std::string& name) {
    std::cout << "Processing string " << name << ": " << value << " (length: " << value.length() << ")" << std::endl;
}

template<typename T>
std::enable_if_t<SFINAEDetector<T>::has_size && SFINAEDetector<T>::has_iterator, void>
process_value(const T& container, const std::string& name) {
    std::cout << "Processing container " << name << ": size=" << container.size() << ", elements: ";
    for (const auto& item : container) {
        std::cout << item << " ";
    }
    std::cout << std::endl;
}

// Tag dispatch pattern
struct IteratorTag {};
struct ContainerTag {};
struct ArithmeticTag {};

template<typename T>
constexpr auto get_category() {
    if constexpr (std::is_arithmetic_v<T>) {
        return ArithmeticTag{};
    } else if constexpr (SFINAEDetector<T>::has_iterator) {
        return ContainerTag{};
    } else {
        return IteratorTag{};
    }
}

template<typename T>
void process_with_tag(const T& value, ArithmeticTag) {
    std::cout << "Tag dispatch - arithmetic: " << value << std::endl;
}

template<typename T>
void process_with_tag(const T& container, ContainerTag) {
    std::cout << "Tag dispatch - container with " << container.size() << " elements" << std::endl;
}

template<typename T>
void process_with_tag(const T& value, IteratorTag) {
    std::cout << "Tag dispatch - other type" << std::endl;
}

// Template specialization patterns
template<typename T>
class SpecializationDemo {
public:
    void process() {
        std::cout << "Generic template processing" << std::endl;
    }

    static constexpr const char* type_name() { return "Generic"; }
};

// Partial specialization for pointers
template<typename T>
class SpecializationDemo<T*> {
public:
    void process() {
        std::cout << "Pointer specialization processing" << std::endl;
    }

    static constexpr const char* type_name() { return "Pointer"; }
};

// Full specialization for specific types
template<>
class SpecializationDemo<int> {
public:
    void process() {
        std::cout << "Integer specialization processing" << std::endl;
    }

    static constexpr const char* type_name() { return "Integer"; }
};

template<>
class SpecializationDemo<std::string> {
public:
    void process() {
        std::cout << "String specialization processing" << std::endl;
    }

    static constexpr const char* type_name() { return "String"; }
};

class MetaprogrammingDemo {
public:
    void demonstrateBasicMetaprogramming() {
        std::cout << "=== Basic Template Metaprogramming ===" << std::endl;

        // Compile-time computations
        constexpr int fact5 = Factorial<5>::value;
        constexpr int fib10 = Fibonacci<10>::value;

        std::cout << "Factorial<5>: " << fact5 << std::endl;
        std::cout << "Fibonacci<10>: " << fib10 << std::endl;

        // Type list demonstrations
        using MyTypes = TypeList<int, double, std::string, char>;

        std::cout << "Type list size: " << MyTypes::size << std::endl;

        using FirstType = TypeAt<0, MyTypes>::type;
        using ThirdType = TypeAt<2, MyTypes>::type;

        std::cout << "First type is int: " << std::boolalpha << std::is_same_v<FirstType, int> << std::endl;
        std::cout << "Third type is string: " << std::boolalpha << std::is_same_v<ThirdType, std::string> << std::endl;

        constexpr bool contains_double = Contains<double, MyTypes>::value;
        constexpr bool contains_float = Contains<float, MyTypes>::value;

        std::cout << "Contains double: " << std::boolalpha << contains_double << std::endl;
        std::cout << "Contains float: " << std::boolalpha << contains_float << std::endl;
    }

    void demonstrateSFINAE() {
        std::cout << "=== SFINAE Demonstrations ===" << std::endl;

        // Test SFINAE detection
        std::cout << "std::vector<int> has size: " << std::boolalpha
                  << SFINAEDetector<std::vector<int>>::has_size << std::endl;
        std::cout << "std::vector<int> has push_back: " << std::boolalpha
                  << SFINAEDetector<std::vector<int>>::has_push_back << std::endl;
        std::cout << "int has size: " << std::boolalpha
                  << SFINAEDetector<int>::has_size << std::endl;

        // SFINAE-based function overloading
        process_value(42, "number");
        process_value(3.14, "pi");
        process_value(std::string("hello"), "greeting");

        std::vector<int> vec = {1, 2, 3, 4, 5};
        process_value(vec, "vector");
    }

    void demonstrateTagDispatch() {
        std::cout << "=== Tag Dispatch Demonstrations ===" << std::endl;

        int number = 42;
        std::vector<int> container = {1, 2, 3};
        std::string text = "hello";

        process_with_tag(number, get_category<int>());
        process_with_tag(container, get_category<std::vector<int>>());
        process_with_tag(text, get_category<std::string>());
    }

    void demonstrateSpecialization() {
        std::cout << "=== Template Specialization Demonstrations ===" << std::endl;

        SpecializationDemo<double> generic_demo;
        SpecializationDemo<int*> pointer_demo;
        SpecializationDemo<int> int_demo;
        SpecializationDemo<std::string> string_demo;

        std::cout << "Generic demo (" << SpecializationDemo<double>::type_name() << "): ";
        generic_demo.process();

        std::cout << "Pointer demo (" << SpecializationDemo<int*>::type_name() << "): ";
        pointer_demo.process();

        std::cout << "Integer demo (" << SpecializationDemo<int>::type_name() << "): ";
        int_demo.process();

        std::cout << "String demo (" << SpecializationDemo<std::string>::type_name() << "): ";
        string_demo.process();
    }
};

void testBasicMetaprogramming() {
    MetaprogrammingDemo demo;
    demo.demonstrateBasicMetaprogramming();
    demo.demonstrateSFINAE();
    demo.demonstrateTagDispatch();
    demo.demonstrateSpecialization();
}

void demonstrateBasicMetaprogramming() {
    testBasicMetaprogramming();
}
""",
    )

    run_updater(cpp_metaprogramming_project, mock_ingestor)

    project_name = cpp_metaprogramming_project.name

    expected_classes = [
        f"{project_name}.basic_metaprogramming.SFINAEDetector",
        f"{project_name}.basic_metaprogramming.MetaprogrammingDemo",
    ]

    expected_functions = [
        f"{project_name}.basic_metaprogramming.testBasicMetaprogramming",
        f"{project_name}.basic_metaprogramming.demonstrateBasicMetaprogramming",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 metaprogramming class, found {len(found_classes)}: {found_classes}"
    )

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_advanced_metaprogramming(
    cpp_metaprogramming_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test advanced template metaprogramming patterns."""
    test_file = cpp_metaprogramming_project / "advanced_metaprogramming.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <type_traits>
#include <tuple>
#include <functional>

// Expression templates for compile-time computation
template<typename T>
struct Value {
    T value;
    constexpr Value(T v) : value(v) {}
    constexpr T eval() const { return value; }
};

template<typename L, typename R, typename Op>
struct BinaryExpression {
    L left;
    R right;
    Op op;

    constexpr BinaryExpression(L l, R r, Op o) : left(l), right(r), op(o) {}
    constexpr auto eval() const { return op(left.eval(), right.eval()); }
};

template<typename L, typename R>
constexpr auto operator+(L left, R right) {
    return BinaryExpression(left, right, [](auto a, auto b) { return a + b; });
}

template<typename L, typename R>
constexpr auto operator*(L left, R right) {
    return BinaryExpression(left, right, [](auto a, auto b) { return a * b; });
}

// Variadic template utilities
template<typename... Args>
struct ArgumentPack {
    static constexpr size_t size = sizeof...(Args);

    template<size_t I>
    using type_at = std::tuple_element_t<I, std::tuple<Args...>>;
};

// Fold expressions (C++17)
template<typename... Args>
constexpr auto sum_all(Args... args) {
    return (args + ...);  // Unary right fold
}

template<typename... Args>
constexpr auto multiply_all(Args... args) {
    return (args * ...);  // Unary right fold
}

template<typename... Args>
constexpr bool all_positive(Args... args) {
    return ((args > 0) && ...);  // Fold with &&
}

template<typename... Args>
void print_all(Args... args) {
    ((std::cout << args << " "), ...);  // Fold with comma operator
    std::cout << std::endl;
}

// Template template parameters
template<template<typename> class Container, typename T>
class ContainerWrapper {
private:
    Container<T> container_;

public:
    using value_type = T;
    using container_type = Container<T>;

    void add(const T& value) {
        if constexpr (requires { container_.push_back(value); }) {
            container_.push_back(value);
        } else if constexpr (requires { container_.insert(value); }) {
            container_.insert(value);
        }
    }

    auto size() const { return container_.size(); }
    auto begin() const { return container_.begin(); }
    auto end() const { return container_.end(); }

    const Container<T>& get_container() const { return container_; }
};

// CRTP (Curiously Recurring Template Pattern)
template<typename Derived>
class Printable {
public:
    void print() const {
        static_cast<const Derived*>(this)->print_impl();
    }

    void print_info() const {
        std::cout << "Printing " << static_cast<const Derived*>(this)->get_name() << ": ";
        print();
    }
};

class Point : public Printable<Point> {
private:
    double x_, y_;

public:
    Point(double x, double y) : x_(x), y_(y) {}

    void print_impl() const {
        std::cout << "Point(" << x_ << ", " << y_ << ")";
    }

    const char* get_name() const { return "Point"; }
};

class Circle : public Printable<Circle> {
private:
    double radius_;

public:
    Circle(double radius) : radius_(radius) {}

    void print_impl() const {
        std::cout << "Circle(r=" << radius_ << ")";
    }

    const char* get_name() const { return "Circle"; }
};

// Policy-based design
template<typename SortPolicy, typename PrintPolicy>
class DataProcessor {
private:
    std::vector<int> data_;

public:
    template<typename... Args>
    DataProcessor(Args... args) : data_{args...} {}

    void process() {
        SortPolicy::sort(data_);
        PrintPolicy::print(data_);
    }

    void add_data(int value) { data_.push_back(value); }
};

struct AscendingSort {
    static void sort(std::vector<int>& data) {
        std::cout << "Sorting in ascending order" << std::endl;
        std::sort(data.begin(), data.end());
    }
};

struct DescendingSort {
    static void sort(std::vector<int>& data) {
        std::cout << "Sorting in descending order" << std::endl;
        std::sort(data.begin(), data.end(), std::greater<int>());
    }
};

struct SimplePrint {
    static void print(const std::vector<int>& data) {
        std::cout << "Data: ";
        for (int val : data) {
            std::cout << val << " ";
        }
        std::cout << std::endl;
    }
};

struct DetailedPrint {
    static void print(const std::vector<int>& data) {
        std::cout << "Detailed data (" << data.size() << " elements): [";
        for (size_t i = 0; i < data.size(); ++i) {
            std::cout << data[i];
            if (i < data.size() - 1) std::cout << ", ";
        }
        std::cout << "]" << std::endl;
    }
};

// Metafunction composition
template<template<typename> class F, template<typename> class G>
struct Compose {
    template<typename T>
    using type = typename F<typename G<T>::type>::type;
};

template<typename T>
struct AddPointer {
    using type = T*;
};

template<typename T>
struct AddConst {
    using type = const T;
};

template<typename T>
struct RemoveReference {
    using type = std::remove_reference_t<T>;
};

class AdvancedMetaprogrammingDemo {
public:
    void demonstrateExpressionTemplates() {
        std::cout << "=== Expression Templates ===" << std::endl;

        constexpr auto expr = Value(10) + Value(20) * Value(3);
        constexpr auto result = expr.eval();

        std::cout << "Expression result: " << result << std::endl;

        constexpr auto complex_expr = Value(2) * (Value(3) + Value(4)) + Value(1);
        constexpr auto complex_result = complex_expr.eval();

        std::cout << "Complex expression result: " << complex_result << std::endl;
    }

    void demonstrateVariadicTemplates() {
        std::cout << "=== Variadic Templates ===" << std::endl;

        constexpr auto sum = sum_all(1, 2, 3, 4, 5);
        constexpr auto product = multiply_all(2, 3, 4);
        constexpr bool all_pos = all_positive(1, 2, 3, 4, 5);
        constexpr bool not_all_pos = all_positive(1, -2, 3, 4, 5);

        std::cout << "Sum: " << sum << std::endl;
        std::cout << "Product: " << product << std::endl;
        std::cout << "All positive: " << std::boolalpha << all_pos << std::endl;
        std::cout << "Not all positive: " << std::boolalpha << not_all_pos << std::endl;

        std::cout << "Print all: ";
        print_all(1, 2.5, "hello", 'c');

        std::cout << "Argument pack size: " << ArgumentPack<int, double, char>::size << std::endl;
    }

    void demonstrateTemplateTemplateParameters() {
        std::cout << "=== Template Template Parameters ===" << std::endl;

        ContainerWrapper<std::vector, int> vec_wrapper;
        vec_wrapper.add(1);
        vec_wrapper.add(2);
        vec_wrapper.add(3);

        std::cout << "Vector wrapper size: " << vec_wrapper.size() << std::endl;
        std::cout << "Vector wrapper contents: ";
        for (const auto& val : vec_wrapper) {
            std::cout << val << " ";
        }
        std::cout << std::endl;

        ContainerWrapper<std::set, int> set_wrapper;
        set_wrapper.add(3);
        set_wrapper.add(1);
        set_wrapper.add(2);
        set_wrapper.add(2); // Duplicate, won't be added to set

        std::cout << "Set wrapper size: " << set_wrapper.size() << std::endl;
        std::cout << "Set wrapper contents: ";
        for (const auto& val : set_wrapper) {
            std::cout << val << " ";
        }
        std::cout << std::endl;
    }

    void demonstrateCRTP() {
        std::cout << "=== CRTP (Curiously Recurring Template Pattern) ===" << std::endl;

        Point point(3.5, 4.2);
        Circle circle(2.5);

        point.print_info();
        std::cout << std::endl;

        circle.print_info();
        std::cout << std::endl;
    }

    void demonstratePolicyBasedDesign() {
        std::cout << "=== Policy-Based Design ===" << std::endl;

        DataProcessor<AscendingSort, SimplePrint> asc_simple(5, 2, 8, 1, 9);
        DataProcessor<DescendingSort, DetailedPrint> desc_detailed(3, 7, 1, 4, 6);

        std::cout << "Ascending sort with simple print:" << std::endl;
        asc_simple.process();

        std::cout << "Descending sort with detailed print:" << std::endl;
        desc_detailed.process();
    }

    void demonstrateMetafunctionComposition() {
        std::cout << "=== Metafunction Composition ===" << std::endl;

        using IntType = int;
        using ConstIntPtr = Compose<AddPointer, AddConst>::type<IntType>;
        using IntRefRemoved = RemoveReference<int&>::type;

        std::cout << "const int* type created: " << std::boolalpha
                  << std::is_same_v<ConstIntPtr, const int*> << std::endl;
        std::cout << "Reference removed: " << std::boolalpha
                  << std::is_same_v<IntRefRemoved, int> << std::endl;
    }
};

void testAdvancedMetaprogramming() {
    AdvancedMetaprogrammingDemo demo;
    demo.demonstrateExpressionTemplates();
    demo.demonstrateVariadicTemplates();
    demo.demonstrateTemplateTemplateParameters();
    demo.demonstrateCRTP();
    demo.demonstratePolicyBasedDesign();
    demo.demonstrateMetafunctionComposition();
}

void demonstrateAdvancedMetaprogramming() {
    testAdvancedMetaprogramming();
}
""",
    )

    run_updater(cpp_metaprogramming_project, mock_ingestor)

    project_name = cpp_metaprogramming_project.name

    expected_classes = [
        f"{project_name}.advanced_metaprogramming.Point",
        f"{project_name}.advanced_metaprogramming.Circle",
        f"{project_name}.advanced_metaprogramming.AdvancedMetaprogrammingDemo",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_cpp_metaprogramming_comprehensive(
    cpp_metaprogramming_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all metaprogramming patterns create proper relationships."""
    test_file = cpp_metaprogramming_project / "comprehensive_metaprogramming.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Comprehensive template metaprogramming demonstration
#include <iostream>
#include <type_traits>

// Compile-time graph representation using templates
template<size_t NumNodes>
struct GraphMetadata {
    static constexpr size_t node_count = NumNodes;
    static constexpr size_t max_edges = NumNodes * (NumNodes - 1);

    template<size_t From, size_t To>
    static constexpr bool valid_edge() {
        return From < NumNodes && To < NumNodes && From != To;
    }

    template<size_t Degree>
    static constexpr bool valid_degree() {
        return Degree < NumNodes;
    }
};

// Type-level computation for graph algorithms
template<typename GraphType>
class ComprehensiveMetaprogrammingDemo {
public:
    void demonstrateComprehensiveMetaprogramming() {
        std::cout << "=== Comprehensive Metaprogramming Demo ===" << std::endl;

        // Compile-time graph metadata
        constexpr size_t nodes = GraphType::node_count;
        constexpr size_t max_edges = GraphType::max_edges;
        constexpr bool valid_edge_0_1 = GraphType::template valid_edge<0, 1>();
        constexpr bool invalid_edge_0_0 = GraphType::template valid_edge<0, 0>();

        std::cout << "Graph nodes: " << nodes << std::endl;
        std::cout << "Max edges: " << max_edges << std::endl;
        std::cout << "Edge (0,1) valid: " << std::boolalpha << valid_edge_0_1 << std::endl;
        std::cout << "Edge (0,0) valid: " << std::boolalpha << invalid_edge_0_0 << std::endl;

        // Template metaprogramming pipeline
        using TestGraph = GraphMetadata<5>;
        process_graph_metadata<TestGraph>();
    }

private:
    template<typename Graph>
    void process_graph_metadata() {
        std::cout << "Processing graph with " << Graph::node_count << " nodes" << std::endl;

        if constexpr (Graph::node_count > 10) {
            std::cout << "Large graph detected" << std::endl;
        } else if constexpr (Graph::node_count > 5) {
            std::cout << "Medium graph detected" << std::endl;
        } else {
            std::cout << "Small graph detected" << std::endl;
        }
    }
};

void demonstrateComprehensiveMetaprogramming() {
    using SmallGraph = GraphMetadata<4>;
    ComprehensiveMetaprogrammingDemo<SmallGraph> demo;
    demo.demonstrateComprehensiveMetaprogramming();
}
""",
    )

    run_updater(cpp_metaprogramming_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_metaprogramming" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 2, (
        f"Expected at least 2 comprehensive metaprogramming calls, found {len(comprehensive_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
