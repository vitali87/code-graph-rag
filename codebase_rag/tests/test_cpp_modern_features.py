from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_modern_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with modern features."""
    project_path = temp_repo / "cpp_modern_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    (project_path / "src" / "main.cpp").write_text(
        encoding="utf-8", data="int main() { return 0; }"
    )

    return project_path


def test_auto_keyword_type_deduction(
    cpp_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test auto keyword and type deduction features."""
    test_file = cpp_modern_project / "auto_features.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <vector>
#include <map>
#include <string>
#include <memory>

// Auto with basic types
void testBasicAuto() {
    auto integer = 42;                    // int
    auto floating = 3.14;                 // double
    auto character = 'a';                 // char
    auto boolean = true;                  // bool
    auto text = "Hello World";            // const char*
    auto string_obj = std::string("C++"); // std::string

    std::cout << "Integer: " << integer << std::endl;
    std::cout << "Float: " << floating << std::endl;
}

// Auto with containers
void testContainerAuto() {
    auto numbers = std::vector<int>{1, 2, 3, 4, 5};
    auto scores = std::map<std::string, double>{
        {"Alice", 95.5},
        {"Bob", 87.2},
        {"Charlie", 92.0}
    };

    // Auto with iterators
    for (auto it = numbers.begin(); it != numbers.end(); ++it) {
        std::cout << *it << " ";
    }

    for (auto& pair : scores) {
        std::cout << pair.first << ": " << pair.second << std::endl;
    }
}

// Auto with function return types
auto calculateSum(int a, int b) -> int {
    return a + b;
}

auto createVector() -> std::vector<int> {
    return std::vector<int>{10, 20, 30};
}

// Template function with auto
template<typename T>
auto processValue(T value) -> decltype(value * 2) {
    return value * 2;
}

// Auto with complex expressions
void testComplexAuto() {
    auto vec = createVector();
    auto sum = calculateSum(10, 20);
    auto doubled = processValue(15);

    // Auto with smart pointers
    auto ptr = std::make_unique<int>(42);
    auto shared = std::make_shared<std::string>("Shared");

    std::cout << "Vector size: " << vec.size() << std::endl;
    std::cout << "Sum: " << sum << std::endl;
    std::cout << "Doubled: " << doubled << std::endl;
    std::cout << "Unique ptr value: " << *ptr << std::endl;
    std::cout << "Shared ptr value: " << *shared << std::endl;
}

// Auto with const and references
void testAutoQualifiers() {
    int value = 100;

    auto copy = value;           // int
    auto& reference = value;     // int&
    const auto const_copy = value; // const int
    const auto& const_ref = value; // const int&

    reference = 200;  // Modifies original value
    // const_copy = 300;  // Error: cannot modify const
    // const_ref = 400;   // Error: cannot modify const reference

    std::cout << "Original value: " << value << std::endl;
    std::cout << "Copy: " << copy << std::endl;
}

// Decltype examples
void testDecltype() {
    int x = 10;
    double y = 3.14;

    decltype(x) another_int = 20;      // int
    decltype(y) another_double = 2.71; // double
    decltype(x + y) result = x + y;    // double

    std::cout << "Another int: " << another_int << std::endl;
    std::cout << "Another double: " << another_double << std::endl;
    std::cout << "Result: " << result << std::endl;
}

class AutoFeatureDemo {
private:
    std::vector<int> data_;

public:
    AutoFeatureDemo() : data_{1, 2, 3, 4, 5} {}

    // Auto in member functions
    auto getData() const -> const std::vector<int>& {
        return data_;
    }

    auto findMaxElement() const -> int {
        auto max_it = std::max_element(data_.begin(), data_.end());
        return *max_it;
    }

    void processData() {
        // Range-based for with auto
        for (const auto& element : data_) {
            std::cout << element << " ";
        }
        std::cout << std::endl;

        // Auto with algorithms
        auto count = std::count_if(data_.begin(), data_.end(),
                                   [](const auto& val) { return val > 2; });
        std::cout << "Elements > 2: " << count << std::endl;
    }
};

void demonstrateAutoFeatures() {
    testBasicAuto();
    testContainerAuto();
    testComplexAuto();
    testAutoQualifiers();
    testDecltype();

    AutoFeatureDemo demo;
    auto data = demo.getData();
    auto max_val = demo.findMaxElement();
    demo.processData();

    std::cout << "Max value: " << max_val << std::endl;
}
""",
    )

    run_updater(cpp_modern_project, mock_ingestor)

    project_name = cpp_modern_project.name

    expected_functions = [
        f"{project_name}.auto_features.testBasicAuto",
        f"{project_name}.auto_features.testContainerAuto",
        f"{project_name}.auto_features.calculateSum",
        f"{project_name}.auto_features.createVector",
        f"{project_name}.auto_features.testComplexAuto",
        f"{project_name}.auto_features.demonstrateAutoFeatures",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_lambda_expressions(
    cpp_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test lambda expressions and closures."""
    test_file = cpp_modern_project / "lambda_features.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <vector>
#include <algorithm>
#include <functional>

// Basic lambda expressions
void testBasicLambdas() {
    // Simple lambda
    auto simple_lambda = []() {
        std::cout << "Hello from lambda!" << std::endl;
    };
    simple_lambda();

    // Lambda with parameters
    auto add = [](int a, int b) {
        return a + b;
    };
    int result = add(5, 3);
    std::cout << "Add result: " << result << std::endl;

    // Lambda with return type specification
    auto divide = [](double a, double b) -> double {
        if (b != 0.0) {
            return a / b;
        }
        return 0.0;
    };
    double division = divide(10.0, 3.0);
    std::cout << "Division result: " << division << std::endl;
}

// Lambda with captures
void testLambdaCaptures() {
    int x = 10;
    int y = 20;
    std::string message = "Captured values: ";

    // Capture by value
    auto capture_by_value = [x, y]() {
        std::cout << "x = " << x << ", y = " << y << std::endl;
        // x = 15;  // Error: cannot modify captured value
    };
    capture_by_value();

    // Capture by reference
    auto capture_by_reference = [&x, &y]() {
        x = 30;
        y = 40;
        std::cout << "Modified x = " << x << ", y = " << y << std::endl;
    };
    capture_by_reference();
    std::cout << "After lambda: x = " << x << ", y = " << y << std::endl;

    // Capture all by value
    auto capture_all_value = [=]() {
        std::cout << message << x << ", " << y << std::endl;
        // Cannot modify any captured variables
    };
    capture_all_value();

    // Capture all by reference
    auto capture_all_reference = [&]() {
        message = "All captured by reference: ";
        x = 50;
        y = 60;
        std::cout << message << x << ", " << y << std::endl;
    };
    capture_all_reference();

    // Mixed capture
    auto mixed_capture = [=, &message](int z) mutable {
        x = 100;  // Can modify because of mutable
        message = "Mixed capture: ";  // Captured by reference
        std::cout << message << x << ", " << y << ", " << z << std::endl;
    };
    mixed_capture(70);
}

// Lambdas with STL algorithms
void testLambdasWithSTL() {
    std::vector<int> numbers = {5, 2, 8, 1, 9, 3, 7, 4, 6};

    // Sort with custom lambda comparator
    std::sort(numbers.begin(), numbers.end(), [](int a, int b) {
        return a > b;  // Descending order
    });

    std::cout << "Sorted (descending): ";
    for (const auto& num : numbers) {
        std::cout << num << " ";
    }
    std::cout << std::endl;

    // Find elements with lambda predicate
    auto it = std::find_if(numbers.begin(), numbers.end(), [](int n) {
        return n % 2 == 0;  // Find first even number
    });

    if (it != numbers.end()) {
        std::cout << "First even number: " << *it << std::endl;
    }

    // Count elements with lambda
    auto even_count = std::count_if(numbers.begin(), numbers.end(), [](int n) {
        return n % 2 == 0;
    });
    std::cout << "Even numbers count: " << even_count << std::endl;

    // Transform with lambda
    std::vector<int> squared(numbers.size());
    std::transform(numbers.begin(), numbers.end(), squared.begin(), [](int n) {
        return n * n;
    });

    std::cout << "Squared: ";
    for (const auto& num : squared) {
        std::cout << num << " ";
    }
    std::cout << std::endl;
}

// Generic lambdas (C++14)
void testGenericLambdas() {
    // Generic lambda with auto parameters
    auto generic_add = [](auto a, auto b) {
        return a + b;
    };

    auto int_result = generic_add(10, 20);           // int + int
    auto double_result = generic_add(3.14, 2.86);    // double + double
    auto string_result = generic_add(std::string("Hello"), std::string(" World"));

    std::cout << "Int result: " << int_result << std::endl;
    std::cout << "Double result: " << double_result << std::endl;
    std::cout << "String result: " << string_result << std::endl;

    // Generic lambda with type checking
    auto process = [](auto value) {
        using T = decltype(value);
        if constexpr (std::is_same_v<T, int>) {
            std::cout << "Processing integer: " << value << std::endl;
            return value * 2;
        } else if constexpr (std::is_same_v<T, std::string>) {
            std::cout << "Processing string: " << value << std::endl;
            return value + value;
        } else {
            std::cout << "Processing other type: " << value << std::endl;
            return value;
        }
    };

    auto processed_int = process(42);
    auto processed_string = process(std::string("test"));
    auto processed_double = process(3.14);
}

// Higher-order functions with lambdas
std::function<int(int)> createMultiplier(int factor) {
    return [factor](int value) {
        return value * factor;
    };
}

std::function<bool(int)> createPredicate(int threshold) {
    return [threshold](int value) {
        return value > threshold;
    };
}

void testHigherOrderFunctions() {
    auto multiply_by_3 = createMultiplier(3);
    auto multiply_by_5 = createMultiplier(5);

    std::cout << "10 * 3 = " << multiply_by_3(10) << std::endl;
    std::cout << "10 * 5 = " << multiply_by_5(10) << std::endl;

    auto greater_than_50 = createPredicate(50);
    auto greater_than_10 = createPredicate(10);

    std::vector<int> values = {5, 15, 25, 55, 65};

    auto count_gt_50 = std::count_if(values.begin(), values.end(), greater_than_50);
    auto count_gt_10 = std::count_if(values.begin(), values.end(), greater_than_10);

    std::cout << "Values > 50: " << count_gt_50 << std::endl;
    std::cout << "Values > 10: " << count_gt_10 << std::endl;
}

class LambdaDemo {
private:
    std::vector<int> data_;

public:
    LambdaDemo(std::vector<int> data) : data_(std::move(data)) {}

    void processWithLambda() {
        // Member function using lambda
        std::for_each(data_.begin(), data_.end(), [this](int& value) {
            value = this->transform(value);
        });

        // Lambda accessing member variables
        auto printer = [this]() {
            std::cout << "Data: ";
            for (const auto& val : this->data_) {
                std::cout << val << " ";
            }
            std::cout << std::endl;
        };
        printer();
    }

private:
    int transform(int value) {
        return value * value + 1;
    }
};

void demonstrateLambdaFeatures() {
    testBasicLambdas();
    testLambdaCaptures();
    testLambdasWithSTL();
    testGenericLambdas();
    testHigherOrderFunctions();

    LambdaDemo demo({1, 2, 3, 4, 5});
    demo.processWithLambda();
}
""",
    )

    run_updater(cpp_modern_project, mock_ingestor)

    project_name = cpp_modern_project.name

    expected_functions = [
        f"{project_name}.lambda_features.testBasicLambdas",
        f"{project_name}.lambda_features.testLambdaCaptures",
        f"{project_name}.lambda_features.testLambdasWithSTL",
        f"{project_name}.lambda_features.testGenericLambdas",
        f"{project_name}.lambda_features.demonstrateLambdaFeatures",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_smart_pointers_move_semantics(
    cpp_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test smart pointers and move semantics."""
    test_file = cpp_modern_project / "smart_pointers_move.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <memory>
#include <vector>
#include <string>
#include <utility>

// RAII class for demonstration
class Resource {
private:
    std::string name_;
    size_t size_;

public:
    Resource(const std::string& name, size_t size)
        : name_(name), size_(size) {
        std::cout << "Resource '" << name_ << "' created (" << size_ << " bytes)" << std::endl;
    }

    ~Resource() {
        std::cout << "Resource '" << name_ << "' destroyed" << std::endl;
    }

    // Copy constructor
    Resource(const Resource& other)
        : name_(other.name_ + "_copy"), size_(other.size_) {
        std::cout << "Resource '" << name_ << "' copied from '" << other.name_ << "'" << std::endl;
    }

    // Move constructor
    Resource(Resource&& other) noexcept
        : name_(std::move(other.name_)), size_(other.size_) {
        other.size_ = 0;
        std::cout << "Resource '" << name_ << "' moved" << std::endl;
    }

    // Copy assignment
    Resource& operator=(const Resource& other) {
        if (this != &other) {
            name_ = other.name_ + "_assigned";
            size_ = other.size_;
            std::cout << "Resource '" << name_ << "' copy assigned" << std::endl;
        }
        return *this;
    }

    // Move assignment
    Resource& operator=(Resource&& other) noexcept {
        if (this != &other) {
            name_ = std::move(other.name_);
            size_ = other.size_;
            other.size_ = 0;
            std::cout << "Resource '" << name_ << "' move assigned" << std::endl;
        }
        return *this;
    }

    void use() const {
        std::cout << "Using resource '" << name_ << "' (" << size_ << " bytes)" << std::endl;
    }

    const std::string& getName() const { return name_; }
    size_t getSize() const { return size_; }
};

// Test unique_ptr
void testUniquePtr() {
    std::cout << "=== Testing unique_ptr ===" << std::endl;

    // Create unique_ptr
    auto resource1 = std::make_unique<Resource>("unique1", 1024);
    resource1->use();

    // Transfer ownership
    auto resource2 = std::move(resource1);
    // resource1 is now nullptr
    if (!resource1) {
        std::cout << "resource1 is now null" << std::endl;
    }
    resource2->use();

    // Create and reset
    auto resource3 = std::make_unique<Resource>("unique3", 2048);
    resource3->use();
    resource3.reset();  // Explicitly destroy

    // Create array with unique_ptr
    auto array_ptr = std::make_unique<int[]>(10);
    for (int i = 0; i < 10; ++i) {
        array_ptr[i] = i * i;
    }

    std::cout << "Array values: ";
    for (int i = 0; i < 10; ++i) {
        std::cout << array_ptr[i] << " ";
    }
    std::cout << std::endl;
}

// Test shared_ptr
void testSharedPtr() {
    std::cout << "=== Testing shared_ptr ===" << std::endl;

    // Create shared_ptr
    auto shared1 = std::make_shared<Resource>("shared1", 4096);
    std::cout << "shared1 use count: " << shared1.use_count() << std::endl;

    {
        auto shared2 = shared1;  // Copy, increases reference count
        std::cout << "After copy, use count: " << shared1.use_count() << std::endl;

        auto shared3 = std::move(shared1);  // Move, doesn't increase count
        std::cout << "After move, shared3 use count: " << shared3.use_count() << std::endl;

        shared2->use();
        shared3->use();
    }  // shared2 and shared3 go out of scope, reference count decreases

    // shared1 is moved-from (null), but the resource might still exist if other shared_ptrs hold it
    std::cout << "End of testSharedPtr scope" << std::endl;
}

// Test weak_ptr
void testWeakPtr() {
    std::cout << "=== Testing weak_ptr ===" << std::endl;

    auto shared = std::make_shared<Resource>("weak_test", 8192);
    std::weak_ptr<Resource> weak = shared;

    std::cout << "Weak ptr expired: " << weak.expired() << std::endl;
    std::cout << "Weak ptr use count: " << weak.use_count() << std::endl;

    // Convert weak_ptr to shared_ptr
    if (auto locked = weak.lock()) {
        locked->use();
        std::cout << "Successfully locked weak_ptr" << std::endl;
    }

    shared.reset();  // Destroy the shared_ptr
    std::cout << "After shared.reset(), weak ptr expired: " << weak.expired() << std::endl;

    // Try to lock again
    if (auto locked = weak.lock()) {
        locked->use();
    } else {
        std::cout << "Failed to lock weak_ptr - resource is destroyed" << std::endl;
    }
}

// Test move semantics
void testMoveSemantics() {
    std::cout << "=== Testing Move Semantics ===" << std::endl;

    // Move constructor
    Resource original("original", 16384);
    Resource moved = std::move(original);  // Move constructor
    moved.use();

    // Move assignment
    Resource another("another", 32768);
    Resource target("target", 1024);
    target = std::move(another);  // Move assignment
    target.use();

    // Perfect forwarding example
    auto createResource = [](auto&& name, auto&& size) {
        return std::make_unique<Resource>(std::forward<decltype(name)>(name),
                                          std::forward<decltype(size)>(size));
    };

    std::string resource_name = "forwarded";
    auto forwarded = createResource(std::move(resource_name), 65536);
    forwarded->use();

    // Vector with move semantics
    std::vector<Resource> resources;
    resources.reserve(3);  // Avoid reallocations

    resources.emplace_back("vector1", 1000);  // Construct in place
    resources.emplace_back("vector2", 2000);
    resources.emplace_back("vector3", 3000);

    std::cout << "Vector contents:" << std::endl;
    for (const auto& res : resources) {
        res.use();
    }
}

// Custom deleter example
void testCustomDeleter() {
    std::cout << "=== Testing Custom Deleter ===" << std::endl;

    // Custom deleter for unique_ptr
    auto custom_deleter = [](Resource* ptr) {
        std::cout << "Custom deleter called for: " << ptr->getName() << std::endl;
        delete ptr;
    };

    std::unique_ptr<Resource, decltype(custom_deleter)> custom_unique(
        new Resource("custom_unique", 4096), custom_deleter);
    custom_unique->use();

    // Custom deleter for shared_ptr
    auto shared_with_deleter = std::shared_ptr<Resource>(
        new Resource("custom_shared", 8192),
        [](Resource* ptr) {
            std::cout << "Shared custom deleter called for: " << ptr->getName() << std::endl;
            delete ptr;
        }
    );
    shared_with_deleter->use();
}

// Factory function using smart pointers
std::unique_ptr<Resource> createResource(const std::string& name, size_t size) {
    return std::make_unique<Resource>(name, size);
}

std::shared_ptr<Resource> createSharedResource(const std::string& name, size_t size) {
    return std::make_shared<Resource>(name, size);
}

class SmartPtrManager {
private:
    std::vector<std::unique_ptr<Resource>> unique_resources_;
    std::vector<std::shared_ptr<Resource>> shared_resources_;

public:
    void addUniqueResource(std::unique_ptr<Resource> resource) {
        unique_resources_.push_back(std::move(resource));
    }

    void addSharedResource(std::shared_ptr<Resource> resource) {
        shared_resources_.push_back(std::move(resource));
    }

    void useAllResources() const {
        std::cout << "Using unique resources:" << std::endl;
        for (const auto& resource : unique_resources_) {
            resource->use();
        }

        std::cout << "Using shared resources:" << std::endl;
        for (const auto& resource : shared_resources_) {
            resource->use();
            std::cout << "  Reference count: " << resource.use_count() << std::endl;
        }
    }

    size_t getUniqueCount() const { return unique_resources_.size(); }
    size_t getSharedCount() const { return shared_resources_.size(); }
};

void demonstrateSmartPointersAndMove() {
    testUniquePtr();
    testSharedPtr();
    testWeakPtr();
    testMoveSemantics();
    testCustomDeleter();

    // Test manager class
    SmartPtrManager manager;

    manager.addUniqueResource(createResource("managed_unique1", 1024));
    manager.addUniqueResource(createResource("managed_unique2", 2048));

    auto shared_res = createSharedResource("managed_shared", 4096);
    manager.addSharedResource(shared_res);
    manager.addSharedResource(shared_res);  // Same resource, reference count will be > 1

    manager.useAllResources();

    std::cout << "Manager has " << manager.getUniqueCount()
              << " unique and " << manager.getSharedCount() << " shared resources" << std::endl;
}
""",
    )

    run_updater(cpp_modern_project, mock_ingestor)

    project_name = cpp_modern_project.name

    expected_classes = [
        f"{project_name}.smart_pointers_move.Resource",
        f"{project_name}.smart_pointers_move.SmartPtrManager",
    ]

    expected_functions = [
        f"{project_name}.smart_pointers_move.testUniquePtr",
        f"{project_name}.smart_pointers_move.testSharedPtr",
        f"{project_name}.smart_pointers_move.testMoveSemantics",
        f"{project_name}.smart_pointers_move.demonstrateSmartPointersAndMove",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 smart pointer class, found {len(found_classes)}: {found_classes}"
    )

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_variadic_templates_constexpr(
    cpp_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test variadic templates and constexpr functions."""
    test_file = cpp_modern_project / "variadic_constexpr.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <tuple>
#include <type_traits>

// Constexpr functions
constexpr int factorial(int n) {
    return (n <= 1) ? 1 : n * factorial(n - 1);
}

constexpr bool isPrime(int n) {
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

constexpr double power(double base, int exp) {
    return (exp == 0) ? 1.0 : base * power(base, exp - 1);
}

// Variadic template function - base case
template<typename T>
void print(const T& value) {
    std::cout << value << std::endl;
}

// Variadic template function - recursive case
template<typename T, typename... Args>
void print(const T& first, const Args&... args) {
    std::cout << first << " ";
    print(args...);  // Recursive call with remaining arguments
}

// Variadic template for sum calculation
template<typename T>
constexpr T sum(T value) {
    return value;
}

template<typename T, typename... Args>
constexpr T sum(T first, Args... args) {
    return first + sum(args...);
}

// Variadic template class
template<typename... Types>
class VariadicClass {
private:
    std::tuple<Types...> data_;

public:
    VariadicClass(Types... args) : data_(args...) {}

    template<size_t Index>
    auto get() const -> decltype(std::get<Index>(data_)) {
        return std::get<Index>(data_);
    }

    constexpr size_t size() const {
        return sizeof...(Types);
    }

    void printAll() const {
        printTuple(data_, std::index_sequence_for<Types...>{});
    }

private:
    template<typename Tuple, size_t... Indices>
    void printTuple(const Tuple& t, std::index_sequence<Indices...>) const {
        ((std::cout << std::get<Indices>(t) << " "), ...);
        std::cout << std::endl;
    }
};

// Perfect forwarding with variadic templates
template<typename T, typename... Args>
std::unique_ptr<T> make_unique_variadic(Args&&... args) {
    return std::make_unique<T>(std::forward<Args>(args)...);
}

// Fold expressions (C++17)
template<typename... Args>
constexpr auto fold_sum(Args... args) {
    return (args + ...);
}

template<typename... Args>
constexpr auto fold_product(Args... args) {
    return (args * ...);
}

template<typename... Args>
constexpr bool all_true(Args... args) {
    return (args && ...);
}

template<typename... Args>
constexpr bool any_true(Args... args) {
    return (args || ...);
}

// SFINAE with variadic templates
template<typename T, typename = void>
struct has_size : std::false_type {};

template<typename T>
struct has_size<T, std::void_t<decltype(std::declval<T>().size())>> : std::true_type {};

template<typename T>
constexpr bool has_size_v = has_size<T>::value;

// Type checking with variadic templates
template<typename T, typename... Args>
constexpr bool are_same_v = (std::is_same_v<T, Args> && ...);

template<typename T, typename... Args>
constexpr bool contains_type_v = (std::is_same_v<T, Args> || ...);

// Constexpr class
class ConstexprMath {
public:
    static constexpr double PI = 3.14159265359;
    static constexpr double E = 2.71828182846;

    static constexpr double circleArea(double radius) {
        return PI * radius * radius;
    }

    static constexpr double sphereVolume(double radius) {
        return (4.0 / 3.0) * PI * radius * radius * radius;
    }

    static constexpr int fibonacci(int n) {
        return (n <= 1) ? n : fibonacci(n - 1) + fibonacci(n - 2);
    }
};

void testConstexprFeatures() {
    std::cout << "=== Testing constexpr ===" << std::endl;

    // Compile-time constants
    constexpr int fact5 = factorial(5);
    constexpr bool is17Prime = isPrime(17);
    constexpr double pow23 = power(2.0, 3);

    std::cout << "5! = " << fact5 << std::endl;
    std::cout << "17 is prime: " << std::boolalpha << is17Prime << std::endl;
    std::cout << "2^3 = " << pow23 << std::endl;

    // Constexpr class methods
    constexpr double area = ConstexprMath::circleArea(5.0);
    constexpr int fib10 = ConstexprMath::fibonacci(10);

    std::cout << "Circle area (r=5): " << area << std::endl;
    std::cout << "Fibonacci(10): " << fib10 << std::endl;
}

void testVariadicTemplates() {
    std::cout << "=== Testing Variadic Templates ===" << std::endl;

    // Variadic function calls
    print("Hello", "World", 42, 3.14, true);

    // Variadic sum
    constexpr auto sum1 = sum(1, 2, 3, 4, 5);
    constexpr auto sum2 = sum(1.1, 2.2, 3.3);

    std::cout << "Sum of integers: " << sum1 << std::endl;
    std::cout << "Sum of doubles: " << sum2 << std::endl;

    // Variadic class
    VariadicClass<int, std::string, double> multi(42, "Hello", 3.14);
    std::cout << "Variadic class size: " << multi.size() << std::endl;
    std::cout << "Element 0: " << multi.get<0>() << std::endl;
    std::cout << "Element 1: " << multi.get<1>() << std::endl;
    std::cout << "Element 2: " << multi.get<2>() << std::endl;
    multi.printAll();

    // Fold expressions
    constexpr auto folded_sum = fold_sum(1, 2, 3, 4, 5);
    constexpr auto folded_product = fold_product(2, 3, 4);
    constexpr bool all_vals = all_true(true, true, true);
    constexpr bool any_vals = any_true(false, true, false);

    std::cout << "Folded sum: " << folded_sum << std::endl;
    std::cout << "Folded product: " << folded_product << std::endl;
    std::cout << "All true: " << std::boolalpha << all_vals << std::endl;
    std::cout << "Any true: " << std::boolalpha << any_vals << std::endl;
}

void testTypeTraits() {
    std::cout << "=== Testing Type Traits ===" << std::endl;

    // Type checking
    constexpr bool same_ints = are_same_v<int, int, int>;
    constexpr bool mixed_types = are_same_v<int, double, int>;

    std::cout << "All ints are same: " << std::boolalpha << same_ints << std::endl;
    std::cout << "Mixed types are same: " << std::boolalpha << mixed_types << std::endl;

    // Contains type check
    constexpr bool contains_int = contains_type_v<int, double, int, std::string>;
    constexpr bool contains_char = contains_type_v<char, double, int, std::string>;

    std::cout << "Contains int: " << std::boolalpha << contains_int << std::endl;
    std::cout << "Contains char: " << std::boolalpha << contains_char << std::endl;

    // SFINAE check
    std::cout << "std::vector has size(): " << std::boolalpha << has_size_v<std::vector<int>> << std::endl;
    std::cout << "int has size(): " << std::boolalpha << has_size_v<int> << std::endl;
}

void demonstrateVariadicConstexpr() {
    testConstexprFeatures();
    testVariadicTemplates();
    testTypeTraits();
}
""",
    )

    run_updater(cpp_modern_project, mock_ingestor)

    project_name = cpp_modern_project.name

    expected_functions = [
        f"{project_name}.variadic_constexpr.factorial",
        f"{project_name}.variadic_constexpr.isPrime",
        f"{project_name}.variadic_constexpr.testConstexprFeatures",
        f"{project_name}.variadic_constexpr.testVariadicTemplates",
        f"{project_name}.variadic_constexpr.demonstrateVariadicConstexpr",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_structured_bindings_ranges(
    cpp_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test structured bindings (C++17) and range-based for loops."""
    test_file = cpp_modern_project / "structured_bindings_ranges.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <vector>
#include <map>
#include <tuple>
#include <array>
#include <string>
#include <algorithm>

// Structured bindings with std::tuple
std::tuple<int, std::string, double> createPerson() {
    return std::make_tuple(25, "Alice", 5.6);
}

// Structured bindings with std::pair
std::pair<std::string, int> getNameAge() {
    return {"Bob", 30};
}

// Custom class for structured bindings
struct Point3D {
    double x, y, z;

    Point3D(double x, double y, double z) : x(x), y(y), z(z) {}
};

// Enable structured bindings for Point3D
namespace std {
    template<>
    struct tuple_size<Point3D> : std::integral_constant<size_t, 3> {};

    template<size_t I>
    struct tuple_element<I, Point3D> {
        using type = double;
    };
}

template<size_t I>
double get(const Point3D& p) {
    if constexpr (I == 0) return p.x;
    else if constexpr (I == 1) return p.y;
    else if constexpr (I == 2) return p.z;
}

void testStructuredBindings() {
    std::cout << "=== Testing Structured Bindings ===" << std::endl;

    // Structured bindings with tuple
    auto [age, name, height] = createPerson();
    std::cout << "Person: " << name << ", age " << age << ", height " << height << "m" << std::endl;

    // Structured bindings with pair
    auto [person_name, person_age] = getNameAge();
    std::cout << "Person: " << person_name << ", age " << person_age << std::endl;

    // Structured bindings with arrays
    int numbers[3] = {10, 20, 30};
    auto [first, second, third] = numbers;
    std::cout << "Array elements: " << first << ", " << second << ", " << third << std::endl;

    // Structured bindings with std::array
    std::array<std::string, 3> colors = {"red", "green", "blue"};
    auto [color1, color2, color3] = colors;
    std::cout << "Colors: " << color1 << ", " << color2 << ", " << color3 << std::endl;

    // Structured bindings with custom class
    Point3D point(1.0, 2.0, 3.0);
    auto [x, y, z] = point;
    std::cout << "Point coordinates: (" << x << ", " << y << ", " << z << ")" << std::endl;

    // Structured bindings with map iteration
    std::map<std::string, int> scores = {
        {"Alice", 95},
        {"Bob", 87},
        {"Charlie", 92}
    };

    std::cout << "Scores:" << std::endl;
    for (const auto& [student, score] : scores) {
        std::cout << "  " << student << ": " << score << std::endl;
    }
}

void testRangeBasedFor() {
    std::cout << "=== Testing Range-based For Loops ===" << std::endl;

    // Basic range-based for
    std::vector<int> numbers = {1, 2, 3, 4, 5};

    std::cout << "Numbers: ";
    for (const auto& num : numbers) {
        std::cout << num << " ";
    }
    std::cout << std::endl;

    // Modifying elements
    std::cout << "Doubling numbers: ";
    for (auto& num : numbers) {
        num *= 2;
        std::cout << num << " ";
    }
    std::cout << std::endl;

    // Range-based for with index (C++20-style simulation)
    std::cout << "Numbers with index:" << std::endl;
    size_t index = 0;
    for (const auto& num : numbers) {
        std::cout << "  [" << index++ << "] = " << num << std::endl;
    }

    // Range-based for with strings
    std::string text = "Hello";
    std::cout << "Characters in '" << text << "': ";
    for (const char& c : text) {
        std::cout << c << " ";
    }
    std::cout << std::endl;

    // Range-based for with maps
    std::map<std::string, double> prices = {
        {"apple", 1.20},
        {"banana", 0.80},
        {"orange", 1.50}
    };

    std::cout << "Prices:" << std::endl;
    for (const auto& item : prices) {
        std::cout << "  " << item.first << ": $" << item.second << std::endl;
    }

    // Range-based for with structured bindings
    std::cout << "Prices (with structured bindings):" << std::endl;
    for (const auto& [product, price] : prices) {
        std::cout << "  " << product << ": $" << price << std::endl;
    }

    // Range-based for with custom range
    auto createRange = [](int start, int end) {
        std::vector<int> range;
        for (int i = start; i <= end; ++i) {
            range.push_back(i);
        }
        return range;
    };

    std::cout << "Custom range (5 to 10): ";
    for (const auto& num : createRange(5, 10)) {
        std::cout << num << " ";
    }
    std::cout << std::endl;
}

// Custom iterator for demonstration
class NumberRange {
private:
    int start_, end_;

public:
    NumberRange(int start, int end) : start_(start), end_(end) {}

    class Iterator {
    private:
        int current_;

    public:
        Iterator(int value) : current_(value) {}

        int operator*() const { return current_; }
        Iterator& operator++() { ++current_; return *this; }
        bool operator!=(const Iterator& other) const { return current_ != other.current_; }
    };

    Iterator begin() const { return Iterator(start_); }
    Iterator end() const { return Iterator(end_ + 1); }
};

void testCustomRange() {
    std::cout << "=== Testing Custom Range ===" << std::endl;

    std::cout << "Custom range (1 to 7): ";
    for (const auto& num : NumberRange(1, 7)) {
        std::cout << num << " ";
    }
    std::cout << std::endl;
}

// Advanced range operations
void testAdvancedRanges() {
    std::cout << "=== Testing Advanced Range Operations ===" << std::endl;

    std::vector<std::pair<std::string, int>> students = {
        {"Alice", 95},
        {"Bob", 87},
        {"Charlie", 92},
        {"Diana", 98},
        {"Eve", 84}
    };

    // Filter and transform using range-based for
    std::cout << "High-scoring students (>90):" << std::endl;
    for (const auto& [name, score] : students) {
        if (score > 90) {
            std::cout << "  " << name << ": " << score << "%" << std::endl;
        }
    }

    // Calculate statistics
    double total_score = 0.0;
    int max_score = 0;
    std::string top_student;

    for (const auto& [name, score] : students) {
        total_score += score;
        if (score > max_score) {
            max_score = score;
            top_student = name;
        }
    }

    double average = total_score / students.size();
    std::cout << "Average score: " << average << "%" << std::endl;
    std::cout << "Top student: " << top_student << " with " << max_score << "%" << std::endl;

    // Nested containers
    std::vector<std::vector<int>> matrix = {
        {1, 2, 3},
        {4, 5, 6},
        {7, 8, 9}
    };

    std::cout << "Matrix:" << std::endl;
    for (const auto& row : matrix) {
        for (const auto& element : row) {
            std::cout << element << " ";
        }
        std::cout << std::endl;
    }
}

void demonstrateStructuredBindingsAndRanges() {
    testStructuredBindings();
    testRangeBasedFor();
    testCustomRange();
    testAdvancedRanges();
}
""",
    )

    run_updater(cpp_modern_project, mock_ingestor)

    project_name = cpp_modern_project.name

    expected_functions = [
        f"{project_name}.structured_bindings_ranges.testStructuredBindings",
        f"{project_name}.structured_bindings_ranges.testRangeBasedFor",
        f"{project_name}.structured_bindings_ranges.testCustomRange",
        f"{project_name}.structured_bindings_ranges.demonstrateStructuredBindingsAndRanges",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_cpp_modern_comprehensive(
    cpp_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all modern C++ features create proper relationships."""
    test_file = cpp_modern_project / "comprehensive_modern.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every modern C++ feature in one file
#include <iostream>
#include <memory>
#include <vector>
#include <map>
#include <algorithm>
#include <functional>
#include <tuple>

// Modern C++ comprehensive example
template<typename... Args>
class ModernContainer {
private:
    std::tuple<Args...> data_;

public:
    ModernContainer(Args&&... args) : data_(std::forward<Args>(args)...) {}

    template<size_t Index>
    constexpr auto get() const -> decltype(std::get<Index>(data_)) {
        return std::get<Index>(data_);
    }

    template<typename F>
    void apply(F&& func) {
        applyImpl(std::forward<F>(func), std::index_sequence_for<Args...>{});
    }

private:
    template<typename F, size_t... Indices>
    void applyImpl(F&& func, std::index_sequence<Indices...>) {
        (func(std::get<Indices>(data_)), ...);
    }
};

// Modern resource management with RAII
class ModernResource {
private:
    std::unique_ptr<int[]> data_;
    size_t size_;

public:
    ModernResource(size_t size) : size_(size), data_(std::make_unique<int[]>(size)) {
        // Initialize with lambda
        auto init = [this](size_t index) { data_[index] = static_cast<int>(index * index); };
        for (size_t i = 0; i < size_; ++i) {
            init(i);
        }
    }

    // Move-only semantics
    ModernResource(const ModernResource&) = delete;
    ModernResource& operator=(const ModernResource&) = delete;

    ModernResource(ModernResource&&) = default;
    ModernResource& operator=(ModernResource&&) = default;

    // Range-based for support
    int* begin() { return data_.get(); }
    int* end() { return data_.get() + size_; }
    const int* begin() const { return data_.get(); }
    const int* end() const { return data_.get() + size_; }

    size_t size() const noexcept { return size_; }

    // Modern member function with auto return type
    auto process() const -> std::vector<int> {
        std::vector<int> result;
        result.reserve(size_);

        // Range-based for with structured binding simulation
        for (const auto& value : *this) {
            result.push_back(value * 2);
        }

        return result;
    }
};

// Factory function with perfect forwarding
template<typename T, typename... Args>
auto make_modern(Args&&... args) -> std::unique_ptr<T> {
    return std::make_unique<T>(std::forward<Args>(args)...);
}

// Generic lambda with constexpr if
constexpr auto process_value = [](auto value) {
    using T = std::decay_t<decltype(value)>;

    if constexpr (std::is_integral_v<T>) {
        return value * 2;
    } else if constexpr (std::is_floating_point_v<T>) {
        return value * 1.5;
    } else {
        return value;
    }
};

void demonstrateModernFeatures() {
    // Auto type deduction
    auto numbers = std::vector<int>{1, 2, 3, 4, 5};
    auto squared = [](int x) { return x * x; };

    // Range-based for with auto
    std::cout << "Original: ";
    for (const auto& num : numbers) {
        std::cout << num << " ";
    }
    std::cout << std::endl;

    // STL algorithms with lambdas
    std::transform(numbers.begin(), numbers.end(), numbers.begin(), squared);

    std::cout << "Squared: ";
    for (const auto& num : numbers) {
        std::cout << num << " ";
    }
    std::cout << std::endl;

    // Smart pointers and move semantics
    auto resource = make_modern<ModernResource>(10);
    auto processed = resource->process();

    std::cout << "Processed resource: ";
    for (const auto& value : processed) {
        std::cout << value << " ";
    }
    std::cout << std::endl;

    // Variadic templates
    ModernContainer<int, std::string, double> container(42, std::string("modern"), 3.14);
    std::cout << "Container contents: ";
    container.apply([](const auto& value) {
        std::cout << value << " ";
    });
    std::cout << std::endl;

    // Structured bindings with map
    std::map<std::string, int> scores = {{"Alice", 95}, {"Bob", 87}};
    for (const auto& [name, score] : scores) {
        std::cout << name << ": " << score << std::endl;
    }

    // Generic lambda with constexpr if
    auto int_result = process_value(10);
    auto double_result = process_value(3.14);
    auto string_result = process_value(std::string("test"));

    std::cout << "Processed values: " << int_result << ", "
              << double_result << ", " << string_result << std::endl;

    // Perfect forwarding and move semantics
    std::vector<ModernResource> resources;
    resources.push_back(ModernResource(5));  // Move constructor
    resources.emplace_back(8);               // Construct in place

    std::cout << "Resources created: " << resources.size() << std::endl;
    for (const auto& res : resources) {
        std::cout << "  Size: " << res.size() << std::endl;
    }
}
""",
    )

    run_updater(cpp_modern_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call for call in call_relationships if "comprehensive_modern" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 5, (
        f"Expected at least 5 comprehensive modern calls, found {len(comprehensive_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
