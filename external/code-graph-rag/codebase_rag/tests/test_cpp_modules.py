from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_modules_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with modules patterns."""
    project_path = temp_repo / "cpp_modules_test"
    project_path.mkdir()

    (project_path / "modules").mkdir()
    (project_path / "src").mkdir()
    (project_path / "interfaces").mkdir()

    return project_path


def test_basic_module_interface(
    cpp_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic module interface declarations and exports."""
    interface_file = cpp_modules_project / "interfaces" / "math_module.ixx"
    interface_file.write_text(
        encoding="utf-8",
        data="""
// math_module.ixx - Module interface file
export module math_operations;

import <iostream>;
import <vector>;
import <cmath>;

// Export namespace
export namespace math {
    // Export basic arithmetic functions
    export double add(double a, double b);
    export double subtract(double a, double b);
    export double multiply(double a, double b);
    export double divide(double a, double b);

    // Export advanced math functions
    export double power(double base, double exponent);
    export double square_root(double value);
    export double factorial(int n);

    // Export template functions
    export template<typename T>
    T maximum(const T& a, const T& b) {
        return (a > b) ? a : b;
    }

    export template<typename T>
    T minimum(const T& a, const T& b) {
        return (a < b) ? a : b;
    }

    // Export template class
    export template<typename T>
    class Calculator {
    private:
        T accumulator_;

    public:
        Calculator() : accumulator_(T{}) {}

        void reset() { accumulator_ = T{}; }
        T get_result() const { return accumulator_; }

        Calculator& add(const T& value) {
            accumulator_ += value;
            return *this;
        }

        Calculator& multiply(const T& value) {
            accumulator_ *= value;
            return *this;
        }

        Calculator& apply(std::function<T(T)> func) {
            accumulator_ = func(accumulator_);
            return *this;
        }
    };

    // Export constants
    export constexpr double PI = 3.14159265359;
    export constexpr double E = 2.71828182846;

    // Export enums
    export enum class Operation {
        ADD,
        SUBTRACT,
        MULTIPLY,
        DIVIDE,
        POWER
    };

    // Export structs
    export struct Point2D {
        double x, y;

        Point2D(double x = 0.0, double y = 0.0) : x(x), y(y) {}

        double distance_to(const Point2D& other) const {
            double dx = x - other.x;
            double dy = y - other.y;
            return std::sqrt(dx * dx + dy * dy);
        }

        Point2D operator+(const Point2D& other) const {
            return Point2D(x + other.x, y + other.y);
        }
    };

    // Export class with complex features
    export class MathProcessor {
    private:
        std::vector<double> history_;
        Operation last_operation_;

    public:
        MathProcessor() : last_operation_(Operation::ADD) {}

        double process(double a, double b, Operation op);
        void clear_history() { history_.clear(); }

        const std::vector<double>& get_history() const { return history_; }
        Operation get_last_operation() const { return last_operation_; }

        // Template member function
        template<typename Container>
        double sum_container(const Container& container) {
            double sum = 0.0;
            for (const auto& value : container) {
                sum += static_cast<double>(value);
            }
            history_.push_back(sum);
            return sum;
        }
    };

    // Export type aliases
    export using FloatCalculator = Calculator<float>;
    export using DoubleCalculator = Calculator<double>;
    export using IntCalculator = Calculator<int>;

    // Export function templates with concepts (if available)
    export template<typename T>
    requires std::is_arithmetic_v<T>
    T safe_divide(T numerator, T denominator) {
        if (denominator == T{}) {
            throw std::invalid_argument("Division by zero");
        }
        return numerator / denominator;
    }
}

// Non-exported internal functions (module implementation details)
namespace math::internal {
    double validate_input(double value) {
        return std::isfinite(value) ? value : 0.0;
    }

    template<typename T>
    T clamp(const T& value, const T& min_val, const T& max_val) {
        return std::max(min_val, std::min(value, max_val));
    }
}
""",
    )

    impl_file = cpp_modules_project / "src" / "math_module.cpp"
    impl_file.write_text(
        encoding="utf-8",
        data="""
// math_module.cpp - Module implementation file
module math_operations;

import <stdexcept>;
import <cmath>;

// Implement exported functions
namespace math {
    double add(double a, double b) {
        return internal::validate_input(a) + internal::validate_input(b);
    }

    double subtract(double a, double b) {
        return internal::validate_input(a) - internal::validate_input(b);
    }

    double multiply(double a, double b) {
        return internal::validate_input(a) * internal::validate_input(b);
    }

    double divide(double a, double b) {
        double validated_a = internal::validate_input(a);
        double validated_b = internal::validate_input(b);

        if (std::abs(validated_b) < 1e-10) {
            throw std::invalid_argument("Division by zero");
        }

        return validated_a / validated_b;
    }

    double power(double base, double exponent) {
        return std::pow(internal::validate_input(base),
                       internal::validate_input(exponent));
    }

    double square_root(double value) {
        double validated = internal::validate_input(value);
        if (validated < 0.0) {
            throw std::invalid_argument("Square root of negative number");
        }
        return std::sqrt(validated);
    }

    double factorial(int n) {
        if (n < 0) {
            throw std::invalid_argument("Factorial of negative number");
        }

        double result = 1.0;
        for (int i = 2; i <= n; ++i) {
            result *= i;
        }
        return result;
    }

    // Implement MathProcessor methods
    double MathProcessor::process(double a, double b, Operation op) {
        double result = 0.0;
        last_operation_ = op;

        switch (op) {
            case Operation::ADD:
                result = add(a, b);
                break;
            case Operation::SUBTRACT:
                result = subtract(a, b);
                break;
            case Operation::MULTIPLY:
                result = multiply(a, b);
                break;
            case Operation::DIVIDE:
                result = divide(a, b);
                break;
            case Operation::POWER:
                result = power(a, b);
                break;
        }

        history_.push_back(result);
        return result;
    }
}
""",
    )

    run_updater(cpp_modules_project, mock_ingestor)

    project_name = cpp_modules_project.name

    expected_classes = [
        f"{project_name}.math_module.Calculator",
        f"{project_name}.math_module.MathProcessor",
        f"{project_name}.math_module.Point2D",
    ]

    expected_functions = [
        f"{project_name}.math_module.add",
        f"{project_name}.math_module.subtract",
        f"{project_name}.math_module.multiply",
        f"{project_name}.math_module.divide",
        f"{project_name}.math_module.power",
        f"{project_name}.math_module.square_root",
        f"{project_name}.math_module.factorial",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 module class, found {len(found_classes)}: {found_classes}"
    )

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_module_partitions(
    cpp_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test module partitions and internal module structure."""
    primary_interface = cpp_modules_project / "interfaces" / "data_structures.ixx"
    primary_interface.write_text(
        encoding="utf-8",
        data="""
// data_structures.ixx - Primary module interface
export module data_structures;

// Export partitions
export import :containers;
export import :algorithms;
export import :iterators;

// Main module exports
export namespace ds {
    // Unified interface for all data structures
    template<typename T>
    concept DataStructure = requires(T t) {
        { t.size() } -> std::convertible_to<std::size_t>;
        { t.empty() } -> std::convertible_to<bool>;
        { t.clear() } -> std::same_as<void>;
    };

    // Factory functions
    export template<typename T>
    auto make_dynamic_array(std::initializer_list<T> init) {
        return containers::DynamicArray<T>(init);
    }

    export template<typename T>
    auto make_linked_list(std::initializer_list<T> init) {
        return containers::LinkedList<T>(init);
    }

    // Utility functions
    export template<DataStructure Container, typename Predicate>
    void process_if(Container& container, Predicate pred) {
        algorithms::for_each_if(container, pred);
    }
}
""",
    )

    container_partition = cpp_modules_project / "modules" / "containers.ixx"
    container_partition.write_text(
        encoding="utf-8",
        data="""
// containers.ixx - Container partition
export module data_structures:containers;

import <memory>;
import <initializer_list>;
import <stdexcept>;

export namespace ds::containers {
    // Dynamic array implementation
    template<typename T>
    class DynamicArray {
    private:
        std::unique_ptr<T[]> data_;
        std::size_t size_;
        std::size_t capacity_;

        void resize_if_needed() {
            if (size_ >= capacity_) {
                std::size_t new_capacity = capacity_ * 2;
                auto new_data = std::make_unique<T[]>(new_capacity);

                for (std::size_t i = 0; i < size_; ++i) {
                    new_data[i] = std::move(data_[i]);
                }

                data_ = std::move(new_data);
                capacity_ = new_capacity;
            }
        }

    public:
        DynamicArray() : size_(0), capacity_(4) {
            data_ = std::make_unique<T[]>(capacity_);
        }

        DynamicArray(std::initializer_list<T> init)
            : size_(init.size()), capacity_(std::max(std::size_t{4}, init.size())) {
            data_ = std::make_unique<T[]>(capacity_);
            std::size_t i = 0;
            for (const auto& item : init) {
                data_[i++] = item;
            }
        }

        void push_back(const T& value) {
            resize_if_needed();
            data_[size_++] = value;
        }

        void push_back(T&& value) {
            resize_if_needed();
            data_[size_++] = std::move(value);
        }

        T& operator[](std::size_t index) {
            if (index >= size_) throw std::out_of_range("Index out of range");
            return data_[index];
        }

        const T& operator[](std::size_t index) const {
            if (index >= size_) throw std::out_of_range("Index out of range");
            return data_[index];
        }

        std::size_t size() const { return size_; }
        bool empty() const { return size_ == 0; }
        void clear() { size_ = 0; }

        // Iterator support
        T* begin() { return data_.get(); }
        T* end() { return data_.get() + size_; }
        const T* begin() const { return data_.get(); }
        const T* end() const { return data_.get() + size_; }
    };

    // Linked list implementation
    template<typename T>
    class LinkedList {
    private:
        struct Node {
            T data;
            std::unique_ptr<Node> next;

            Node(const T& value) : data(value) {}
            Node(T&& value) : data(std::move(value)) {}
        };

        std::unique_ptr<Node> head_;
        std::size_t size_;

    public:
        LinkedList() : size_(0) {}

        LinkedList(std::initializer_list<T> init) : size_(0) {
            for (const auto& item : init) {
                push_back(item);
            }
        }

        void push_front(const T& value) {
            auto new_node = std::make_unique<Node>(value);
            new_node->next = std::move(head_);
            head_ = std::move(new_node);
            ++size_;
        }

        void push_back(const T& value) {
            auto new_node = std::make_unique<Node>(value);

            if (!head_) {
                head_ = std::move(new_node);
            } else {
                Node* current = head_.get();
                while (current->next) {
                    current = current->next.get();
                }
                current->next = std::move(new_node);
            }
            ++size_;
        }

        void pop_front() {
            if (head_) {
                head_ = std::move(head_->next);
                --size_;
            }
        }

        std::size_t size() const { return size_; }
        bool empty() const { return size_ == 0; }
        void clear() {
            head_.reset();
            size_ = 0;
        }

        // Simple iterator
        class iterator {
            Node* current_;
        public:
            iterator(Node* node) : current_(node) {}
            T& operator*() { return current_->data; }
            iterator& operator++() {
                if (current_) current_ = current_->next.get();
                return *this;
            }
            bool operator!=(const iterator& other) const {
                return current_ != other.current_;
            }
        };

        iterator begin() { return iterator(head_.get()); }
        iterator end() { return iterator(nullptr); }
    };
}
""",
    )

    algorithms_partition = cpp_modules_project / "modules" / "algorithms.ixx"
    algorithms_partition.write_text(
        encoding="utf-8",
        data="""
// algorithms.ixx - Algorithms partition
export module data_structures:algorithms;

import <functional>;
import <type_traits>;

export namespace ds::algorithms {
    // Generic algorithms for data structures
    template<typename Container, typename Function>
    void for_each(Container& container, Function func) {
        for (auto& item : container) {
            func(item);
        }
    }

    template<typename Container, typename Predicate>
    void for_each_if(Container& container, Predicate pred) {
        for (auto& item : container) {
            if (pred(item)) {
                // Process item that matches predicate
                std::cout << "Processing: " << item << std::endl;
            }
        }
    }

    template<typename Container, typename T>
    bool contains(const Container& container, const T& value) {
        for (const auto& item : container) {
            if (item == value) {
                return true;
            }
        }
        return false;
    }

    template<typename Container, typename Predicate>
    std::size_t count_if(const Container& container, Predicate pred) {
        std::size_t count = 0;
        for (const auto& item : container) {
            if (pred(item)) {
                ++count;
            }
        }
        return count;
    }

    template<typename Container, typename T>
    void fill(Container& container, const T& value) {
        for (auto& item : container) {
            item = value;
        }
    }

    template<typename Container, typename Transformer>
    void transform(Container& container, Transformer transformer) {
        for (auto& item : container) {
            item = transformer(item);
        }
    }

    // Specialized algorithms
    template<typename Container>
    requires std::is_arithmetic_v<typename Container::value_type>
    auto sum(const Container& container) {
        using T = typename Container::value_type;
        T result{};
        for (const auto& item : container) {
            result += item;
        }
        return result;
    }

    template<typename Container>
    requires std::is_arithmetic_v<typename Container::value_type>
    auto average(const Container& container) {
        if (container.empty()) return typename Container::value_type{};
        return sum(container) / static_cast<typename Container::value_type>(container.size());
    }
}
""",
    )

    run_updater(cpp_modules_project, mock_ingestor)

    project_name = cpp_modules_project.name

    expected_classes = [
        f"{project_name}.containers.DynamicArray",
        f"{project_name}.containers.LinkedList",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert found_classes, (
        f"Expected at least 1 partition class, found {len(found_classes)}: {found_classes}"
    )


def test_module_imports_usage(
    cpp_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test module imports and usage patterns."""
    usage_file = cpp_modules_project / "src" / "module_usage.cpp"
    usage_file.write_text(
        encoding="utf-8",
        data="""
// module_usage.cpp - Using imported modules
import math_operations;
import data_structures;
import <iostream>;
import <vector>;
import <string>;

// Using imported modules
void testMathOperations() {
    using namespace math;

    std::cout << "=== Testing Math Operations Module ===" << std::endl;

    // Test basic operations
    double result1 = add(10.5, 5.3);
    double result2 = multiply(result1, 2.0);
    double result3 = power(result2, 0.5);

    std::cout << "Calculations: " << result1 << ", " << result2 << ", " << result3 << std::endl;

    // Test template functions
    auto max_int = maximum(42, 38);
    auto min_double = minimum(3.14, 2.71);

    std::cout << "Max int: " << max_int << ", Min double: " << min_double << std::endl;

    // Test Calculator class
    DoubleCalculator calc;
    double calc_result = calc.add(10.0).multiply(2.0).add(5.0).get_result();
    std::cout << "Calculator result: " << calc_result << std::endl;

    // Test Point2D
    Point2D p1(0.0, 0.0);
    Point2D p2(3.0, 4.0);
    double distance = p1.distance_to(p2);
    Point2D p3 = p1 + p2;

    std::cout << "Distance: " << distance << ", Sum point: (" << p3.x << ", " << p3.y << ")" << std::endl;

    // Test MathProcessor
    MathProcessor processor;
    double proc_result1 = processor.process(10.0, 5.0, Operation::ADD);
    double proc_result2 = processor.process(proc_result1, 3.0, Operation::MULTIPLY);

    std::cout << "Processor results: " << proc_result1 << ", " << proc_result2 << std::endl;
    std::cout << "History size: " << processor.get_history().size() << std::endl;

    // Test concepts and safe operations
    try {
        double safe_result = safe_divide(10.0, 2.0);
        std::cout << "Safe division: " << safe_result << std::endl;

        // This should throw
        double unsafe_result = safe_divide(10.0, 0.0);
    } catch (const std::exception& e) {
        std::cout << "Caught expected exception: " << e.what() << std::endl;
    }
}

void testDataStructures() {
    using namespace ds;
    using namespace ds::containers;
    using namespace ds::algorithms;

    std::cout << "=== Testing Data Structures Module ===" << std::endl;

    // Test DynamicArray
    auto array = make_dynamic_array({1, 2, 3, 4, 5});
    array.push_back(6);
    array.push_back(7);

    std::cout << "Dynamic array size: " << array.size() << std::endl;
    std::cout << "Array contents: ";
    for (const auto& item : array) {
        std::cout << item << " ";
    }
    std::cout << std::endl;

    // Test LinkedList
    auto list = make_linked_list<std::string>({"hello", "world", "from", "modules"});
    list.push_front("say");

    std::cout << "Linked list size: " << list.size() << std::endl;
    std::cout << "List contents: ";
    for (const auto& item : list) {
        std::cout << item << " ";
    }
    std::cout << std::endl;

    // Test algorithms
    std::cout << "Contains 'world': " << contains(list, std::string("world")) << std::endl;

    auto count = count_if(array, [](int x) { return x % 2 == 0; });
    std::cout << "Even numbers in array: " << count << std::endl;

    auto array_sum = sum(array);
    auto array_avg = average(array);
    std::cout << "Array sum: " << array_sum << ", average: " << array_avg << std::endl;

    // Test with concept
    process_if(array, [](int x) { return x > 3; });
}

void testModuleInterop() {
    std::cout << "=== Testing Module Interoperability ===" << std::endl;

    // Use math module with data structures
    auto numbers = ds::make_dynamic_array<double>({1.0, 2.0, 3.0, 4.0, 5.0});

    // Apply math operations to container
    ds::algorithms::transform(numbers, [](double x) {
        return math::power(x, 2.0);  // Square each element
    });

    std::cout << "Squared numbers: ";
    for (const auto& num : numbers) {
        std::cout << num << " ";
    }
    std::cout << std::endl;

    // Use math calculator with container data
    math::DoubleCalculator calc;
    for (const auto& num : numbers) {
        calc.add(num);
    }

    std::cout << "Sum of squared numbers: " << calc.get_result() << std::endl;

    // Complex interop example
    auto points = ds::make_dynamic_array<math::Point2D>({
        {0.0, 0.0}, {1.0, 1.0}, {2.0, 2.0}, {3.0, 3.0}
    });

    math::Point2D origin(0.0, 0.0);
    std::cout << "Distances from origin: ";
    for (const auto& point : points) {
        double dist = origin.distance_to(point);
        std::cout << dist << " ";
    }
    std::cout << std::endl;
}

// Global module usage demonstration
void demonstrateModuleUsage() {
    testMathOperations();
    testDataStructures();
    testModuleInterop();

    std::cout << "=== Module Usage Demonstration Complete ===" << std::endl;
}

// Module feature showcase
void showcaseModuleFeatures() {
    std::cout << "=== Module Features Showcase ===" << std::endl;

    // Demonstrate clean interfaces
    std::cout << "1. Clean module interfaces with export/import" << std::endl;
    std::cout << "2. Module partitions for code organization" << std::endl;
    std::cout << "3. Template exports and instantiation" << std::endl;
    std::cout << "4. Concept integration with modules" << std::endl;
    std::cout << "5. Cross-module functionality" << std::endl;
    std::cout << "6. Encapsulation of implementation details" << std::endl;

    demonstrateModuleUsage();
}
""",
    )

    run_updater(cpp_modules_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    imports_relationships = get_relationships(mock_ingestor, "IMPORTS")

    [
        call
        for call in imports_relationships
        if "module_usage" in call.args[0][2]
        and any(
            module_name in str(call.args[2])
            for module_name in ["math_operations", "data_structures"]
        )
    ]

    module_function_calls = [
        call for call in call_relationships if "module_usage" in call.args[0][2]
    ]

    assert len(module_function_calls) >= 5, (
        f"Expected at least 5 module function calls, found {len(module_function_calls)}"
    )
