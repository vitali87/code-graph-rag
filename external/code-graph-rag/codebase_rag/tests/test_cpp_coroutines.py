from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_coroutines_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with coroutines patterns."""
    project_path = temp_repo / "cpp_coroutines_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_basic_generator_coroutines(
    cpp_coroutines_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic generator coroutines with co_yield."""
    test_file = cpp_coroutines_project / "basic_generators.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <coroutine>
#include <iostream>
#include <memory>
#include <exception>

// Basic generator coroutine
template<typename T>
struct Generator {
    struct promise_type;
    using handle_type = std::coroutine_handle<promise_type>;

    struct promise_type {
        T current_value;

        Generator get_return_object() {
            return Generator{handle_type::from_promise(*this)};
        }

        std::suspend_always initial_suspend() { return {}; }
        std::suspend_always final_suspend() noexcept { return {}; }

        std::suspend_always yield_value(const T& value) {
            current_value = value;
            return {};
        }

        void return_void() {}
        void unhandled_exception() { std::terminate(); }
    };

    handle_type coro;

    Generator(handle_type h) : coro(h) {}
    ~Generator() { if (coro) coro.destroy(); }

    // Move-only semantics
    Generator(const Generator&) = delete;
    Generator& operator=(const Generator&) = delete;
    Generator(Generator&& other) noexcept : coro(other.coro) {
        other.coro = {};
    }
    Generator& operator=(Generator&& other) noexcept {
        if (this != &other) {
            if (coro) coro.destroy();
            coro = other.coro;
            other.coro = {};
        }
        return *this;
    }

    // Iterator interface
    class iterator {
        handle_type coro_;
    public:
        iterator(handle_type coro) : coro_(coro) {}

        iterator& operator++() {
            coro_.resume();
            if (coro_.done()) coro_ = nullptr;
            return *this;
        }

        T operator*() const { return coro_.promise().current_value; }
        bool operator==(const iterator& other) const { return coro_ == other.coro_; }
        bool operator!=(const iterator& other) const { return !(*this == other); }
    };

    iterator begin() {
        if (coro) {
            coro.resume();
            if (coro.done()) return end();
        }
        return iterator{coro};
    }

    iterator end() { return iterator{nullptr}; }
};

// Simple number generator
Generator<int> generateNumbers(int start, int end) {
    for (int i = start; i <= end; ++i) {
        co_yield i;
    }
}

// Fibonacci generator
Generator<long long> fibonacciGenerator(int count) {
    long long a = 0, b = 1;

    for (int i = 0; i < count; ++i) {
        if (i == 0) {
            co_yield a;
        } else if (i == 1) {
            co_yield b;
        } else {
            long long next = a + b;
            co_yield next;
            a = b;
            b = next;
        }
    }
}

// String generator with transformations
Generator<std::string> transformedStrings(const std::vector<std::string>& strings) {
    for (const auto& str : strings) {
        // Transform to uppercase
        std::string upper = str;
        std::transform(upper.begin(), upper.end(), upper.begin(), ::toupper);
        co_yield upper;

        // Transform to lowercase
        std::string lower = str;
        std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
        co_yield lower;

        // Reverse string
        std::string reversed = str;
        std::reverse(reversed.begin(), reversed.end());
        co_yield reversed;
    }
}

// Generator with early termination
Generator<int> conditionalGenerator(int limit) {
    for (int i = 1; i <= 100; ++i) {
        if (i > limit) {
            co_return;  // Early termination
        }

        if (i % 2 == 0) {
            co_yield i;
        }
    }
}

void testBasicGenerators() {
    std::cout << "=== Testing Basic Generator Coroutines ===" << std::endl;

    // Test number generator
    std::cout << "Numbers 1-5: ";
    for (const auto& num : generateNumbers(1, 5)) {
        std::cout << num << " ";
    }
    std::cout << std::endl;

    // Test Fibonacci generator
    std::cout << "First 10 Fibonacci numbers: ";
    for (const auto& fib : fibonacciGenerator(10)) {
        std::cout << fib << " ";
    }
    std::cout << std::endl;

    // Test string transformations
    std::vector<std::string> words = {"Hello", "World"};
    std::cout << "Transformed strings:" << std::endl;
    for (const auto& transformed : transformedStrings(words)) {
        std::cout << "  " << transformed << std::endl;
    }

    // Test conditional generator
    std::cout << "Even numbers up to 10: ";
    for (const auto& num : conditionalGenerator(10)) {
        std::cout << num << " ";
    }
    std::cout << std::endl;
}

// Lazy evaluation generator
template<typename T>
Generator<T> lazyFilter(Generator<T> source, std::function<bool(const T&)> predicate) {
    for (const auto& item : source) {
        if (predicate(item)) {
            co_yield item;
        }
    }
}

template<typename T, typename F>
auto lazyMap(Generator<T> source, F transform) -> Generator<decltype(transform(std::declval<T>()))> {
    for (const auto& item : source) {
        co_yield transform(item);
    }
}

void testLazyGenerators() {
    std::cout << "=== Testing Lazy Generator Pipelines ===" << std::endl;

    // Create a pipeline: numbers -> filter evens -> square -> print
    auto numbers = generateNumbers(1, 20);
    auto evenNumbers = lazyFilter(std::move(numbers), [](int n) { return n % 2 == 0; });
    auto squaredNumbers = lazyMap(std::move(evenNumbers), [](int n) { return n * n; });

    std::cout << "Squared even numbers (1-20): ";
    for (const auto& squared : squaredNumbers) {
        std::cout << squared << " ";
    }
    std::cout << std::endl;
}

void demonstrateBasicGenerators() {
    testBasicGenerators();
    testLazyGenerators();
}
""",
    )

    run_updater(cpp_coroutines_project, mock_ingestor)

    project_name = cpp_coroutines_project.name

    expected_classes = [
        f"{project_name}.basic_generators.Generator",
    ]

    expected_functions = [
        f"{project_name}.basic_generators.generateNumbers",
        f"{project_name}.basic_generators.fibonacciGenerator",
        f"{project_name}.basic_generators.transformedStrings",
        f"{project_name}.basic_generators.testBasicGenerators",
        f"{project_name}.basic_generators.demonstrateBasicGenerators",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 coroutine class, found {len(found_classes)}: {found_classes}"
    )

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_async_await_coroutines(
    cpp_coroutines_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test async/await style coroutines with co_await."""
    test_file = cpp_coroutines_project / "async_await.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <coroutine>
#include <iostream>
#include <future>
#include <thread>
#include <chrono>
#include <string>
#include <vector>

// Task coroutine for async operations
template<typename T>
struct Task {
    struct promise_type;
    using handle_type = std::coroutine_handle<promise_type>;

    struct promise_type {
        T result_;
        std::exception_ptr exception_;

        Task get_return_object() {
            return Task{handle_type::from_promise(*this)};
        }

        std::suspend_never initial_suspend() { return {}; }
        std::suspend_never final_suspend() noexcept { return {}; }

        void return_value(const T& value) {
            result_ = value;
        }

        void unhandled_exception() {
            exception_ = std::current_exception();
        }
    };

    handle_type coro;

    Task(handle_type h) : coro(h) {}
    ~Task() {
        if (coro) coro.destroy();
    }

    // Move-only semantics
    Task(const Task&) = delete;
    Task& operator=(const Task&) = delete;
    Task(Task&& other) noexcept : coro(other.coro) {
        other.coro = {};
    }
    Task& operator=(Task&& other) noexcept {
        if (this != &other) {
            if (coro) coro.destroy();
            coro = other.coro;
            other.coro = {};
        }
        return *this;
    }

    T get() {
        if (!coro.done()) {
            coro.resume();
        }

        if (coro.promise().exception_) {
            std::rethrow_exception(coro.promise().exception_);
        }

        return coro.promise().result_;
    }

    bool is_ready() const {
        return coro.done();
    }
};

// Awaitable wrapper for std::future
template<typename T>
struct FutureAwaiter {
    std::future<T> future_;

    FutureAwaiter(std::future<T>&& future) : future_(std::move(future)) {}

    bool await_ready() const {
        return future_.wait_for(std::chrono::seconds(0)) == std::future_status::ready;
    }

    void await_suspend(std::coroutine_handle<> handle) {
        std::thread([this, handle]() {
            future_.wait();
            handle.resume();
        }).detach();
    }

    T await_resume() {
        return future_.get();
    }
};

// Helper function to make futures awaitable
template<typename T>
FutureAwaiter<T> await_future(std::future<T>&& future) {
    return FutureAwaiter<T>(std::move(future));
}

// Async computation functions
std::future<int> calculateAsync(int value) {
    return std::async(std::launch::async, [value]() {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        return value * value;
    });
}

std::future<std::string> fetchDataAsync(const std::string& key) {
    return std::async(std::launch::async, [key]() {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        return "Data for " + key;
    });
}

std::future<double> computeAverageAsync(const std::vector<int>& values) {
    return std::async(std::launch::async, [values]() {
        std::this_thread::sleep_for(std::chrono::milliseconds(75));
        double sum = 0.0;
        for (int val : values) {
            sum += val;
        }
        return values.empty() ? 0.0 : sum / values.size();
    });
}

// Coroutine that uses co_await with multiple async operations
Task<std::string> processDataAsync(const std::string& input) {
    std::cout << "Starting async processing for: " << input << std::endl;

    // First async operation
    auto square_future = calculateAsync(42);
    int squared = co_await await_future(std::move(square_future));

    std::cout << "Squared result: " << squared << std::endl;

    // Second async operation
    auto data_future = fetchDataAsync(input + "_key");
    std::string data = co_await await_future(std::move(data_future));

    std::cout << "Fetched data: " << data << std::endl;

    // Third async operation
    std::vector<int> numbers = {1, 2, 3, 4, 5};
    auto avg_future = computeAverageAsync(numbers);
    double average = co_await await_future(std::move(avg_future));

    std::cout << "Computed average: " << average << std::endl;

    // Combine results
    std::string result = "Processed: " + input +
                        ", squared=" + std::to_string(squared) +
                        ", data=" + data +
                        ", avg=" + std::to_string(average);

    co_return result;
}

// Coroutine that chains multiple async operations
Task<int> chainedOperationsAsync() {
    std::cout << "Starting chained async operations" << std::endl;

    // Chain of dependent async operations
    auto future1 = calculateAsync(5);
    int result1 = co_await await_future(std::move(future1));

    auto future2 = calculateAsync(result1);
    int result2 = co_await await_future(std::move(future2));

    auto future3 = calculateAsync(result2);
    int result3 = co_await await_future(std::move(future3));

    std::cout << "Chained results: " << result1 << " -> " << result2 << " -> " << result3 << std::endl;

    co_return result3;
}

// Coroutine with error handling
Task<std::string> errorHandlingAsync(bool should_fail) {
    try {
        std::cout << "Error handling test, should_fail=" << should_fail << std::endl;

        if (should_fail) {
            throw std::runtime_error("Simulated async error");
        }

        auto future = fetchDataAsync("success");
        std::string data = co_await await_future(std::move(future));

        co_return "Success: " + data;
    } catch (const std::exception& e) {
        co_return "Error caught: " + std::string(e.what());
    }
}

// Parallel async operations
Task<std::vector<int>> parallelOperationsAsync() {
    std::cout << "Starting parallel async operations" << std::endl;

    // Start multiple operations in parallel
    auto future1 = calculateAsync(10);
    auto future2 = calculateAsync(20);
    auto future3 = calculateAsync(30);

    // Await all results
    int result1 = co_await await_future(std::move(future1));
    int result2 = co_await await_future(std::move(future2));
    int result3 = co_await await_future(std::move(future3));

    std::vector<int> results = {result1, result2, result3};
    std::cout << "Parallel results: ";
    for (int result : results) {
        std::cout << result << " ";
    }
    std::cout << std::endl;

    co_return results;
}

void testAsyncAwait() {
    std::cout << "=== Testing Async/Await Coroutines ===" << std::endl;

    // Test basic async processing
    auto task1 = processDataAsync("test_input");
    std::string result1 = task1.get();
    std::cout << "Final result: " << result1 << std::endl;

    // Test chained operations
    auto task2 = chainedOperationsAsync();
    int result2 = task2.get();
    std::cout << "Chained final result: " << result2 << std::endl;

    // Test error handling - success case
    auto task3 = errorHandlingAsync(false);
    std::string result3 = task3.get();
    std::cout << "Error handling (success): " << result3 << std::endl;

    // Test error handling - failure case
    auto task4 = errorHandlingAsync(true);
    std::string result4 = task4.get();
    std::cout << "Error handling (failure): " << result4 << std::endl;

    // Test parallel operations
    auto task5 = parallelOperationsAsync();
    auto results = task5.get();
    std::cout << "Parallel operations completed with " << results.size() << " results" << std::endl;
}

void demonstrateAsyncAwait() {
    testAsyncAwait();
}
""",
    )

    run_updater(cpp_coroutines_project, mock_ingestor)

    project_name = cpp_coroutines_project.name

    expected_functions = [
        f"{project_name}.async_await.calculateAsync",
        f"{project_name}.async_await.fetchDataAsync",
        f"{project_name}.async_await.processDataAsync",
        f"{project_name}.async_await.chainedOperationsAsync",
        f"{project_name}.async_await.testAsyncAwait",
        f"{project_name}.async_await.demonstrateAsyncAwait",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_custom_coroutine_types(
    cpp_coroutines_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test custom coroutine types and advanced coroutine patterns."""
    test_file = cpp_coroutines_project / "custom_coroutines.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <coroutine>
#include <iostream>
#include <memory>
#include <queue>
#include <functional>

// Event-driven coroutine for simulation
struct EventCoroutine {
    struct promise_type;
    using handle_type = std::coroutine_handle<promise_type>;

    struct promise_type {
        std::queue<std::string> events_;
        std::string current_event_;

        EventCoroutine get_return_object() {
            return EventCoroutine{handle_type::from_promise(*this)};
        }

        std::suspend_always initial_suspend() { return {}; }
        std::suspend_always final_suspend() noexcept { return {}; }

        struct EventAwaiter {
            std::string expected_event_;
            promise_type* promise_;

            EventAwaiter(const std::string& event, promise_type* p)
                : expected_event_(event), promise_(p) {}

            bool await_ready() {
                return !promise_->events_.empty() &&
                       promise_->events_.front() == expected_event_;
            }

            void await_suspend(handle_type handle) {
                // In a real implementation, this would register for the event
            }

            std::string await_resume() {
                if (!promise_->events_.empty()) {
                    std::string event = promise_->events_.front();
                    promise_->events_.pop();
                    return event;
                }
                return "";
            }
        };

        EventAwaiter await_transform(const std::string& event) {
            return EventAwaiter{event, this};
        }

        void return_void() {}
        void unhandled_exception() { std::terminate(); }

        void post_event(const std::string& event) {
            events_.push(event);
        }
    };

    handle_type coro;

    EventCoroutine(handle_type h) : coro(h) {}
    ~EventCoroutine() { if (coro) coro.destroy(); }

    // Move-only semantics
    EventCoroutine(const EventCoroutine&) = delete;
    EventCoroutine& operator=(const EventCoroutine&) = delete;
    EventCoroutine(EventCoroutine&& other) noexcept : coro(other.coro) {
        other.coro = {};
    }
    EventCoroutine& operator=(EventCoroutine&& other) noexcept {
        if (this != &other) {
            if (coro) coro.destroy();
            coro = other.coro;
            other.coro = {};
        }
        return *this;
    }

    void post_event(const std::string& event) {
        if (coro) {
            coro.promise().post_event(event);
            if (coro.done()) return;
            coro.resume();
        }
    }

    bool is_done() const {
        return !coro || coro.done();
    }

    void start() {
        if (coro && !coro.done()) {
            coro.resume();
        }
    }
};

// State machine coroutine
struct StateMachine {
    struct promise_type;
    using handle_type = std::coroutine_handle<promise_type>;

    enum class State {
        INIT,
        PROCESSING,
        WAITING,
        COMPLETE,
        ERROR
    };

    struct promise_type {
        State current_state_ = State::INIT;
        std::string state_data_;

        StateMachine get_return_object() {
            return StateMachine{handle_type::from_promise(*this)};
        }

        std::suspend_always initial_suspend() { return {}; }
        std::suspend_always final_suspend() noexcept { return {}; }

        struct StateTransition {
            State target_state_;
            std::string data_;
            promise_type* promise_;

            StateTransition(State state, const std::string& data, promise_type* p)
                : target_state_(state), data_(data), promise_(p) {}

            bool await_ready() { return false; }
            void await_suspend(handle_type) {}

            State await_resume() {
                promise_->current_state_ = target_state_;
                promise_->state_data_ = data_;
                std::cout << "State transition to: " << static_cast<int>(target_state_)
                         << " with data: " << data_ << std::endl;
                return target_state_;
            }
        };

        StateTransition await_transform(State state) {
            return StateTransition{state, "", this};
        }

        template<typename T>
        StateTransition await_transform(std::pair<State, T> state_data) {
            return StateTransition{state_data.first, std::to_string(state_data.second), this};
        }

        void return_void() {}
        void unhandled_exception() {
            current_state_ = State::ERROR;
        }

        State get_state() const { return current_state_; }
        const std::string& get_data() const { return state_data_; }
    };

    handle_type coro;

    StateMachine(handle_type h) : coro(h) {}
    ~StateMachine() { if (coro) coro.destroy(); }

    // Move-only semantics
    StateMachine(const StateMachine&) = delete;
    StateMachine& operator=(const StateMachine&) = delete;
    StateMachine(StateMachine&& other) noexcept : coro(other.coro) {
        other.coro = {};
    }
    StateMachine& operator=(StateMachine&& other) noexcept {
        if (this != &other) {
            if (coro) coro.destroy();
            coro = other.coro;
            other.coro = {};
        }
        return *this;
    }

    void step() {
        if (coro && !coro.done()) {
            coro.resume();
        }
    }

    State get_current_state() const {
        return coro ? coro.promise().get_state() : State::ERROR;
    }

    std::string get_state_data() const {
        return coro ? coro.promise().get_data() : "";
    }

    bool is_done() const {
        return !coro || coro.done();
    }
};

// Event-driven simulation
EventCoroutine simulateEventSystem() {
    std::cout << "Event system simulation started" << std::endl;

    // Wait for initialization event
    std::string init_event = co_await std::string("INIT");
    std::cout << "Received event: " << init_event << std::endl;

    // Wait for data ready event
    std::string data_event = co_await std::string("DATA_READY");
    std::cout << "Received event: " << data_event << std::endl;

    // Wait for processing complete event
    std::string complete_event = co_await std::string("PROCESSING_COMPLETE");
    std::cout << "Received event: " << complete_event << std::endl;

    std::cout << "Event simulation completed" << std::endl;
}

// State machine example
StateMachine dataProcessingStateMachine() {
    using State = StateMachine::State;

    std::cout << "Data processing state machine started" << std::endl;

    // Initialize
    co_await State::INIT;

    // Start processing
    co_await std::make_pair(State::PROCESSING, 0);

    // Simulate processing steps
    for (int i = 1; i <= 5; ++i) {
        co_await std::make_pair(State::PROCESSING, i * 20);  // Progress percentage

        if (i == 3) {
            // Simulate waiting state
            co_await std::make_pair(State::WAITING, 1000);
        }
    }

    // Complete processing
    co_await std::make_pair(State::COMPLETE, 100);

    std::cout << "State machine processing completed" << std::endl;
}

// Recursive coroutine generator
template<typename T>
Generator<T> recursiveTraversal(const std::vector<std::vector<T>>& matrix) {
    std::function<Generator<T>(int, int)> traverse = [&](int row, int col) -> Generator<T> {
        if (row >= matrix.size() || col >= matrix[row].size()) {
            co_return;
        }

        // Yield current element
        co_yield matrix[row][col];

        // Recursively traverse right and down
        if (col + 1 < matrix[row].size()) {
            auto right_gen = traverse(row, col + 1);
            for (const auto& value : right_gen) {
                co_yield value;
            }
        }

        if (row + 1 < matrix.size()) {
            auto down_gen = traverse(row + 1, col);
            for (const auto& value : down_gen) {
                co_yield value;
            }
        }
    };

    auto gen = traverse(0, 0);
    for (const auto& value : gen) {
        co_yield value;
    }
}

void testCustomCoroutines() {
    std::cout << "=== Testing Custom Coroutine Types ===" << std::endl;

    // Test event-driven coroutine
    auto event_coro = simulateEventSystem();
    event_coro.start();

    event_coro.post_event("INIT");
    event_coro.post_event("DATA_READY");
    event_coro.post_event("PROCESSING_COMPLETE");

    // Test state machine coroutine
    auto state_machine = dataProcessingStateMachine();

    while (!state_machine.is_done()) {
        std::cout << "Current state: " << static_cast<int>(state_machine.get_current_state())
                  << ", data: " << state_machine.get_state_data() << std::endl;
        state_machine.step();
    }

    // Test recursive traversal
    std::vector<std::vector<int>> matrix = {
        {1, 2, 3},
        {4, 5, 6},
        {7, 8, 9}
    };

    std::cout << "Matrix traversal: ";
    for (const auto& value : recursiveTraversal(matrix)) {
        std::cout << value << " ";
    }
    std::cout << std::endl;
}

void demonstrateCustomCoroutines() {
    testCustomCoroutines();
}
""",
    )

    run_updater(cpp_coroutines_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    custom_coroutine_calls = [
        call for call in call_relationships if "custom_coroutines" in call.args[0][2]
    ]

    assert len(custom_coroutine_calls) >= 3, (
        f"Expected at least 3 custom coroutine calls, found {len(custom_coroutine_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
