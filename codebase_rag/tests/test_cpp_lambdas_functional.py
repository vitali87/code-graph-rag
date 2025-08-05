"""
Comprehensive C++ lambda expressions and functional programming testing.
Tests lambda expressions, captures, std::function, functional algorithms,
and functional programming patterns for graph building applications.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def cpp_lambdas_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with lambda and functional programming patterns."""
    project_path = temp_repo / "cpp_lambdas_test"
    project_path.mkdir()

    # Create basic structure
    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_basic_lambdas(
    cpp_lambdas_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic lambda expressions with different capture modes."""
    test_file = cpp_lambdas_project / "basic_lambdas.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <vector>
#include <algorithm>
#include <functional>
#include <string>

class LambdaBasicsDemo {
private:
    std::vector<int> numbers_;
    std::string prefix_;
    int multiplier_;

public:
    LambdaBasicsDemo() : prefix_("Result"), multiplier_(2) {
        numbers_ = {5, 2, 8, 1, 9, 3, 7, 4, 6};
    }

    void demonstrateBasicLambdas() {
        std::cout << "=== Basic Lambda Expressions ===" << std::endl;

        // Simple lambda with no captures
        auto simple_lambda = []() {
            std::cout << "Simple lambda called" << std::endl;
        };
        simple_lambda();

        // Lambda with parameters
        auto add_lambda = [](int a, int b) {
            return a + b;
        };
        int sum = add_lambda(10, 20);
        std::cout << "Add lambda result: " << sum << std::endl;

        // Lambda with return type specification
        auto divide_lambda = [](double a, double b) -> double {
            return (b != 0.0) ? a / b : 0.0;
        };
        double quotient = divide_lambda(15.0, 3.0);
        std::cout << "Divide lambda result: " << quotient << std::endl;

        // Lambda with multiple statements
        auto complex_lambda = [](int n) {
            std::cout << "Processing number: " << n << std::endl;
            int result = n * n + 1;
            std::cout << "Calculated result: " << result << std::endl;
            return result;
        };
        int processed = complex_lambda(7);
        std::cout << "Complex lambda final result: " << processed << std::endl;
    }

    void demonstrateCaptureByValue() {
        std::cout << "=== Capture by Value ===" << std::endl;

        int local_value = 100;
        std::string local_string = "Local";

        // Capture specific variables by value
        auto capture_specific = [local_value, local_string](int input) {
            std::cout << "Captured value: " << local_value << ", string: " << local_string << std::endl;
            return input + local_value;
        };

        int result1 = capture_specific(50);
        std::cout << "Result with specific capture: " << result1 << std::endl;

        // Capture all by value
        auto capture_all_value = [=](int input) {
            std::cout << "All captured - prefix: " << prefix_ << ", multiplier: " << multiplier_ << std::endl;
            return input * multiplier_;
        };

        int result2 = capture_all_value(25);
        std::cout << "Result with capture all by value: " << result2 << std::endl;

        // Modify original values (won't affect captured values)
        local_value = 200;
        multiplier_ = 5;

        std::cout << "After modification:" << std::endl;
        int result3 = capture_specific(50);
        std::cout << "Specific capture result (unchanged): " << result3 << std::endl;

        int result4 = capture_all_value(25);
        std::cout << "Capture all result (unchanged): " << result4 << std::endl;
    }

    void demonstrateCaptureByReference() {
        std::cout << "=== Capture by Reference ===" << std::endl;

        int counter = 0;
        std::vector<int> results;

        // Capture specific variables by reference
        auto increment_counter = [&counter, &results](int value) {
            counter++;
            results.push_back(value * counter);
            std::cout << "Counter: " << counter << ", added: " << value * counter << std::endl;
        };

        // Use lambda multiple times
        increment_counter(10);
        increment_counter(20);
        increment_counter(30);

        std::cout << "Final counter: " << counter << std::endl;
        std::cout << "Results vector size: " << results.size() << std::endl;

        // Capture all by reference
        auto process_with_member_access = [&](int factor) {
            multiplier_ *= factor; // Modify member variable
            prefix_ += "_modified"; // Modify member variable

            std::cout << "Modified multiplier: " << multiplier_ << std::endl;
            std::cout << "Modified prefix: " << prefix_ << std::endl;
        };

        process_with_member_access(3);
        std::cout << "Class member multiplier after lambda: " << multiplier_ << std::endl;
        std::cout << "Class member prefix after lambda: " << prefix_ << std::endl;
    }

    void demonstrateMixedCaptures() {
        std::cout << "=== Mixed Captures ===" << std::endl;

        int by_value = 100;
        int by_reference = 200;
        std::string message = "Mixed";

        // Mixed capture: some by value, some by reference
        auto mixed_lambda = [by_value, &by_reference, message = std::move(message)](int input) mutable {
            by_reference += input; // Modifies original variable
            message += "_processed"; // Modifies captured copy (mutable)

            std::cout << "In lambda - by_value: " << by_value << std::endl;
            std::cout << "In lambda - by_reference: " << by_reference << std::endl;
            std::cout << "In lambda - message: " << message << std::endl;

            return by_value + by_reference;
        };

        int result = mixed_lambda(50);
        std::cout << "Mixed lambda result: " << result << std::endl;
        std::cout << "After lambda - by_reference: " << by_reference << std::endl;
        // Note: message was moved, so it's empty in the original scope
    }

    void demonstrateGenericLambdas() {
        std::cout << "=== Generic Lambdas (C++14) ===" << std::endl;

        // Generic lambda using auto parameters
        auto generic_printer = [](const auto& item) {
            std::cout << "Generic print: " << item << std::endl;
        };

        generic_printer(42);
        generic_printer(3.14);
        generic_printer(std::string("Hello"));

        // Generic lambda with multiple auto parameters
        auto generic_comparator = [](const auto& a, const auto& b) {
            return a < b;
        };

        std::cout << "42 < 100: " << std::boolalpha << generic_comparator(42, 100) << std::endl;
        std::cout << "3.14 < 2.71: " << std::boolalpha << generic_comparator(3.14, 2.71) << std::endl;

        // Generic lambda with template syntax (C++20)
        auto template_lambda = []<typename T>(const T& value) {
            std::cout << "Template lambda with type: " << typeid(T).name()
                      << ", value: " << value << std::endl;
            return value;
        };

        template_lambda(123);
        template_lambda(45.67);
    }

    void printNumbers() const {
        std::cout << "Numbers: ";
        for (int n : numbers_) {
            std::cout << n << " ";
        }
        std::cout << std::endl;
    }
};

void testBasicLambdaExpressions() {
    std::cout << "=== Testing Basic Lambda Expressions ===" << std::endl;

    LambdaBasicsDemo demo;
    demo.demonstrateBasicLambdas();
    demo.demonstrateCaptureByValue();
    demo.demonstrateCaptureByReference();
    demo.demonstrateMixedCaptures();
    demo.demonstrateGenericLambdas();
}

void testLambdasWithSTLAlgorithms() {
    std::cout << "=== Testing Lambdas with STL Algorithms ===" << std::endl;

    std::vector<int> numbers = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};

    std::cout << "Original numbers: ";
    for (int n : numbers) std::cout << n << " ";
    std::cout << std::endl;

    // std::for_each with lambda
    std::cout << "Using for_each to print squares: ";
    std::for_each(numbers.begin(), numbers.end(), [](int n) {
        std::cout << n * n << " ";
    });
    std::cout << std::endl;

    // std::transform with lambda
    std::vector<int> doubled(numbers.size());
    std::transform(numbers.begin(), numbers.end(), doubled.begin(), [](int n) {
        return n * 2;
    });

    std::cout << "Doubled numbers: ";
    for (int n : doubled) std::cout << n << " ";
    std::cout << std::endl;

    // std::find_if with lambda
    auto it = std::find_if(numbers.begin(), numbers.end(), [](int n) {
        return n > 5 && n % 2 == 0;
    });

    if (it != numbers.end()) {
        std::cout << "First even number > 5: " << *it << std::endl;
    }

    // std::count_if with lambda
    int even_count = std::count_if(numbers.begin(), numbers.end(), [](int n) {
        return n % 2 == 0;
    });
    std::cout << "Count of even numbers: " << even_count << std::endl;

    // std::sort with lambda comparator
    std::vector<std::string> words = {"apple", "banana", "cherry", "date", "elderberry"};

    std::cout << "Original words: ";
    for (const auto& word : words) std::cout << word << " ";
    std::cout << std::endl;

    // Sort by length
    std::sort(words.begin(), words.end(), [](const std::string& a, const std::string& b) {
        return a.length() < b.length();
    });

    std::cout << "Sorted by length: ";
    for (const auto& word : words) std::cout << word << " ";
    std::cout << std::endl;

    // std::remove_if with lambda
    auto original_numbers = numbers;
    auto new_end = std::remove_if(numbers.begin(), numbers.end(), [](int n) {
        return n % 3 == 0; // Remove multiples of 3
    });
    numbers.erase(new_end, numbers.end());

    std::cout << "After removing multiples of 3: ";
    for (int n : numbers) std::cout << n << " ";
    std::cout << std::endl;
}

// Function object demonstration
class MultiplierFunctor {
private:
    int factor_;

public:
    MultiplierFunctor(int factor) : factor_(factor) {}

    int operator()(int value) const {
        return value * factor_;
    }

    // Conversion to std::function
    operator std::function<int(int)>() const {
        return [factor = factor_](int value) { return value * factor; };
    }
};

void testFunctionObjects() {
    std::cout << "=== Testing Function Objects and std::function ===" << std::endl;

    std::vector<int> numbers = {1, 2, 3, 4, 5};

    // Traditional function object
    MultiplierFunctor multiply_by_3(3);

    std::vector<int> result1(numbers.size());
    std::transform(numbers.begin(), numbers.end(), result1.begin(), multiply_by_3);

    std::cout << "Using functor: ";
    for (int n : result1) std::cout << n << " ";
    std::cout << std::endl;

    // std::function with lambda
    std::function<int(int)> multiply_lambda = [](int n) { return n * 4; };

    std::vector<int> result2(numbers.size());
    std::transform(numbers.begin(), numbers.end(), result2.begin(), multiply_lambda);

    std::cout << "Using std::function with lambda: ";
    for (int n : result2) std::cout << n << " ";
    std::cout << std::endl;

    // std::function with functor
    std::function<int(int)> multiply_function = multiply_by_3;

    std::vector<int> result3(numbers.size());
    std::transform(numbers.begin(), numbers.end(), result3.begin(), multiply_function);

    std::cout << "Using std::function with functor: ";
    for (int n : result3) std::cout << n << " ";
    std::cout << std::endl;

    // Array of std::function
    std::vector<std::function<int(int)>> operations = {
        [](int n) { return n + 10; },
        [](int n) { return n * n; },
        [](int n) { return n / 2; },
        MultiplierFunctor(5)
    };

    int test_value = 6;
    std::cout << "Applying operations to " << test_value << ":" << std::endl;
    for (size_t i = 0; i < operations.size(); ++i) {
        int result = operations[i](test_value);
        std::cout << "  Operation " << i << ": " << result << std::endl;
    }
}

// Higher-order functions
template<typename Container, typename Predicate>
auto filter(const Container& container, Predicate pred) {
    Container result;
    std::copy_if(container.begin(), container.end(), std::back_inserter(result), pred);
    return result;
}

template<typename Container, typename Transform>
auto map(const Container& container, Transform transform) {
    using ValueType = decltype(transform(*container.begin()));
    std::vector<ValueType> result;
    result.reserve(container.size());
    std::transform(container.begin(), container.end(), std::back_inserter(result), transform);
    return result;
}

template<typename Container, typename BinaryOp>
auto fold(const Container& container, typename Container::value_type init, BinaryOp op) {
    return std::accumulate(container.begin(), container.end(), init, op);
}

void testHigherOrderFunctions() {
    std::cout << "=== Testing Higher-Order Functions ===" << std::endl;

    std::vector<int> numbers = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};

    // Filter even numbers
    auto evens = filter(numbers, [](int n) { return n % 2 == 0; });
    std::cout << "Even numbers: ";
    for (int n : evens) std::cout << n << " ";
    std::cout << std::endl;

    // Map to squares
    auto squares = map(numbers, [](int n) { return n * n; });
    std::cout << "Squares: ";
    for (int n : squares) std::cout << n << " ";
    std::cout << std::endl;

    // Map to strings
    auto strings = map(numbers, [](int n) { return "num_" + std::to_string(n); });
    std::cout << "Strings: ";
    for (const auto& s : strings) std::cout << s << " ";
    std::cout << std::endl;

    // Fold (reduce) to sum
    int sum = fold(numbers, 0, [](int acc, int n) { return acc + n; });
    std::cout << "Sum: " << sum << std::endl;

    // Fold to product
    int product = fold(numbers, 1, [](int acc, int n) { return acc * n; });
    std::cout << "Product: " << product << std::endl;

    // Combine operations
    auto even_squares_sum = fold(
        map(filter(numbers, [](int n) { return n % 2 == 0; }),
            [](int n) { return n * n; }),
        0,
        [](int acc, int n) { return acc + n; }
    );
    std::cout << "Sum of squares of even numbers: " << even_squares_sum << std::endl;
}

void testLambdaClosures() {
    std::cout << "=== Testing Lambda Closures ===" << std::endl;

    // Counter closure
    auto makeCounter = [](int start = 0) {
        return [count = start](int increment = 1) mutable {
            count += increment;
            return count;
        };
    };

    auto counter1 = makeCounter(10);
    auto counter2 = makeCounter(100);

    std::cout << "Counter1: " << counter1() << std::endl; // 11
    std::cout << "Counter1: " << counter1(5) << std::endl; // 16
    std::cout << "Counter2: " << counter2() << std::endl; // 101
    std::cout << "Counter1: " << counter1() << std::endl; // 17

    // Function factory
    auto makeMultiplier = [](int factor) {
        return [factor](int value) {
            return value * factor;
        };
    };

    auto double_it = makeMultiplier(2);
    auto triple_it = makeMultiplier(3);

    std::vector<int> test_values = {1, 2, 3, 4, 5};

    std::cout << "Doubling: ";
    for (int val : test_values) {
        std::cout << double_it(val) << " ";
    }
    std::cout << std::endl;

    std::cout << "Tripling: ";
    for (int val : test_values) {
        std::cout << triple_it(val) << " ";
    }
    std::cout << std::endl;

    // Predicate factory
    auto makeRangePredicate = [](int min, int max) {
        return [min, max](int value) {
            return value >= min && value <= max;
        };
    };

    auto in_range_5_15 = makeRangePredicate(5, 15);
    std::vector<int> range_test = {1, 7, 12, 18, 3, 9, 16, 11};

    auto in_range = filter(range_test, in_range_5_15);
    std::cout << "Values in range 5-15: ";
    for (int n : in_range) std::cout << n << " ";
    std::cout << std::endl;
}

void demonstrateLambdasAndFunctional() {
    testBasicLambdaExpressions();
    testLambdasWithSTLAlgorithms();
    testFunctionObjects();
    testHigherOrderFunctions();
    testLambdaClosures();
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_lambdas_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_lambdas_project.name

    # Expected classes and functions with lambda usage
    expected_classes = [
        f"{project_name}.basic_lambdas.LambdaBasicsDemo",
        f"{project_name}.basic_lambdas.MultiplierFunctor",
    ]

    expected_functions = [
        f"{project_name}.basic_lambdas.testBasicLambdaExpressions",
        f"{project_name}.basic_lambdas.testLambdasWithSTLAlgorithms",
        f"{project_name}.basic_lambdas.testFunctionObjects",
        f"{project_name}.basic_lambdas.testHigherOrderFunctions",
        f"{project_name}.basic_lambdas.testLambdaClosures",
        f"{project_name}.basic_lambdas.demonstrateLambdasAndFunctional",
    ]

    # Get all Class node creation calls
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Verify expected classes were created
    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 lambda class, found {len(found_classes)}: {found_classes}"
    )

    # Get all Function node creation calls
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Verify at least some expected functions were created
    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_async_functional_patterns(
    cpp_lambdas_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test async and concurrent functional patterns."""
    test_file = cpp_lambdas_project / "async_functional.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <future>
#include <thread>
#include <vector>
#include <functional>
#include <numeric>
#include <chrono>

class AsyncFunctionalDemo {
private:
    std::vector<int> data_;

public:
    AsyncFunctionalDemo() {
        // Initialize with some test data
        data_.resize(1000);
        std::iota(data_.begin(), data_.end(), 1);
    }

    void demonstrateAsyncLambdas() {
        std::cout << "=== Async Lambda Patterns ===" << std::endl;

        // Async computation with lambda
        auto async_sum = std::async(std::launch::async, [this]() {
            std::this_thread::sleep_for(std::chrono::milliseconds(100)); // Simulate work
            return std::accumulate(data_.begin(), data_.end(), 0);
        });

        auto async_product = std::async(std::launch::async, [this]() {
            std::this_thread::sleep_for(std::chrono::milliseconds(100)); // Simulate work
            return std::accumulate(data_.begin(), data_.begin() + 10, 1,
                                 [](int acc, int val) { return acc * val; });
        });

        // Do other work while async operations run
        std::cout << "Performing other work while async operations run..." << std::endl;
        int local_calc = data_.size() * 2;

        // Get results
        int sum = async_sum.get();
        int product = async_product.get();

        std::cout << "Async sum: " << sum << std::endl;
        std::cout << "Async product (first 10): " << product << std::endl;
        std::cout << "Local calculation: " << local_calc << std::endl;
    }

    void demonstrateParallelFunctional() {
        std::cout << "=== Parallel Functional Patterns ===" << std::endl;

        // Parallel map-reduce pattern
        const size_t num_threads = 4;
        const size_t chunk_size = data_.size() / num_threads;

        std::vector<std::future<int>> futures;

        // Launch parallel computations
        for (size_t i = 0; i < num_threads; ++i) {
            size_t start = i * chunk_size;
            size_t end = (i == num_threads - 1) ? data_.size() : start + chunk_size;

            auto future = std::async(std::launch::async,
                [this, start, end]() {
                    int chunk_sum = 0;
                    for (size_t j = start; j < end; ++j) {
                        chunk_sum += data_[j] * data_[j]; // Square each element
                    }
                    std::cout << "Thread processed chunk [" << start << ", " << end
                              << ") with sum: " << chunk_sum << std::endl;
                    return chunk_sum;
                });

            futures.push_back(std::move(future));
        }

        // Collect results
        int total_sum = 0;
        for (auto& future : futures) {
            total_sum += future.get();
        }

        std::cout << "Total sum of squares: " << total_sum << std::endl;
    }

    void demonstrateThreadLocalFunctional() {
        std::cout << "=== Thread-Local Functional Patterns ===" << std::endl;

        thread_local int thread_counter = 0;

        auto worker_lambda = [this](int thread_id, int work_amount) {
            // Each thread has its own counter
            for (int i = 0; i < work_amount; ++i) {
                thread_counter++;
            }

            // Process some data with thread-local state
            int local_sum = 0;
            for (int i = 0; i < work_amount && i < static_cast<int>(data_.size()); ++i) {
                local_sum += data_[i] + thread_counter;
            }

            std::cout << "Thread " << thread_id << " - counter: " << thread_counter
                      << ", local sum: " << local_sum << std::endl;

            return local_sum;
        };

        // Launch multiple threads
        std::vector<std::thread> threads;
        std::vector<int> results(3);

        for (int i = 0; i < 3; ++i) {
            threads.emplace_back([&worker_lambda, &results, i]() {
                results[i] = worker_lambda(i, (i + 1) * 10);
            });
        }

        // Wait for all threads
        for (auto& t : threads) {
            t.join();
        }

        std::cout << "Results: ";
        for (int result : results) {
            std::cout << result << " ";
        }
        std::cout << std::endl;
    }
};

void testAsyncFunctionalPatterns() {
    AsyncFunctionalDemo demo;
    demo.demonstrateAsyncLambdas();
    demo.demonstrateParallelFunctional();
    demo.demonstrateThreadLocalFunctional();
}

// Event system using functional patterns
class EventSystem {
private:
    std::vector<std::function<void(const std::string&)>> event_handlers_;

public:
    void subscribe(std::function<void(const std::string&)> handler) {
        event_handlers_.push_back(std::move(handler));
    }

    void emit(const std::string& event_data) {
        std::cout << "Emitting event: " << event_data << std::endl;
        for (const auto& handler : event_handlers_) {
            handler(event_data);
        }
    }

    template<typename Predicate>
    void subscribe_conditional(std::function<void(const std::string&)> handler, Predicate condition) {
        auto conditional_handler = [handler = std::move(handler), condition](const std::string& data) {
            if (condition(data)) {
                handler(data);
            }
        };
        event_handlers_.push_back(std::move(conditional_handler));
    }
};

void testEventSystemWithLambdas() {
    std::cout << "=== Event System with Lambdas ===" << std::endl;

    EventSystem event_system;

    // Subscribe with different lambda handlers
    event_system.subscribe([](const std::string& data) {
        std::cout << "Logger: " << data << std::endl;
    });

    event_system.subscribe([](const std::string& data) {
        if (data.find("error") != std::string::npos) {
            std::cout << "Error handler activated for: " << data << std::endl;
        }
    });

    // Conditional subscription
    event_system.subscribe_conditional(
        [](const std::string& data) {
            std::cout << "Important event handler: " << data << std::endl;
        },
        [](const std::string& data) {
            return data.find("important") != std::string::npos;
        }
    );

    // Emit various events
    event_system.emit("Normal operation");
    event_system.emit("An error occurred");
    event_system.emit("Important system update");
    event_system.emit("Another normal operation");
}

void demonstrateAsyncFunctionalPatterns() {
    testAsyncFunctionalPatterns();
    testEventSystemWithLambdas();
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_lambdas_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_lambdas_project.name

    # Expected classes with async functional patterns
    expected_classes = [
        f"{project_name}.async_functional.AsyncFunctionalDemo",
        f"{project_name}.async_functional.EventSystem",
    ]

    # Get all Class node creation calls
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Verify expected classes were created
    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 async functional class, found {len(found_classes)}: {found_classes}"
    )


def test_cpp_lambdas_comprehensive(
    cpp_lambdas_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all lambda and functional patterns create proper relationships."""
    test_file = cpp_lambdas_project / "comprehensive_lambdas.cpp"
    test_file.write_text(
        """
// Comprehensive lambda and functional programming demonstration
#include <iostream>
#include <vector>
#include <algorithm>
#include <functional>

class ComprehensiveLambdaDemo {
private:
    std::vector<int> data_;

public:
    ComprehensiveLambdaDemo() : data_{1, 2, 3, 4, 5} {}

    void demonstrateComprehensiveLambdas() {
        std::cout << "=== Comprehensive Lambda Demo ===" << std::endl;

        // Combine multiple lambda patterns
        auto processor = [this](auto transform, auto filter) {
            std::vector<int> result;

            std::for_each(data_.begin(), data_.end(), [&](int value) {
                int transformed = transform(value);
                if (filter(transformed)) {
                    result.push_back(transformed);
                }
            });

            return result;
        };

        // Use the processor with different lambdas
        auto doubled_evens = processor(
            [](int x) { return x * 2; },      // Transform: double
            [](int x) { return x % 2 == 0; }  // Filter: even numbers
        );

        std::cout << "Doubled evens: ";
        for (int val : doubled_evens) {
            std::cout << val << " ";
        }
        std::cout << std::endl;

        // Functional composition
        auto compose = [](auto f, auto g) {
            return [f, g](auto x) { return f(g(x)); };
        };

        auto square = [](int x) { return x * x; };
        auto add_one = [](int x) { return x + 1; };
        auto square_then_add_one = compose(add_one, square);

        std::cout << "Composed function (square then add 1) on 5: "
                  << square_then_add_one(5) << std::endl;
    }
};

void demonstrateComprehensiveLambdas() {
    ComprehensiveLambdaDemo demo;
    demo.demonstrateComprehensiveLambdas();
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_lambdas_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Verify all relationship types exist
    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    call_relationships = [c for c in all_relationships if c.args[1] == "CALLS"]
    defines_relationships = [c for c in all_relationships if c.args[1] == "DEFINES"]

    # Should have comprehensive lambda coverage
    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_lambdas" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 2, (
        f"Expected at least 2 comprehensive lambda calls, found {len(comprehensive_calls)}"
    )

    # Test that lambda parsing doesn't interfere with other relationships
    assert defines_relationships, "Should still have DEFINES relationships"
