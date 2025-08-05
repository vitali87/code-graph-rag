"""
Comprehensive C++ concurrency testing.
Tests threads, mutexes, atomics, condition variables, futures, async operations,
and various synchronization primitives.
"""

import os
import sys
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def cpp_concurrency_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with concurrency patterns."""
    project_path = temp_repo / "cpp_concurrency_test"
    project_path.mkdir()

    # Create basic structure
    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_thread_basics(
    cpp_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic thread creation and management."""
    test_file = cpp_concurrency_project / "thread_basics.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <thread>
#include <chrono>
#include <vector>
#include <functional>
#include <sstream>

// Simple thread function
void simpleThreadFunction(int id) {
    std::cout << "Thread " << id << " is running" << std::endl;
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    std::cout << "Thread " << id << " finished" << std::endl;
}

// Thread function with parameters
void threadWithParameters(int id, const std::string& message, double value) {
    std::stringstream ss;
    ss << "Thread " << id << ": " << message << " (value: " << value << ")" << std::endl;
    std::cout << ss.str();
}

// Class with thread member function
class ThreadedWorker {
private:
    int id_;
    bool running_;
    std::thread worker_thread_;

public:
    ThreadedWorker(int id) : id_(id), running_(false) {}

    ~ThreadedWorker() {
        stop();
    }

    void start() {
        running_ = true;
        worker_thread_ = std::thread(&ThreadedWorker::run, this);
    }

    void stop() {
        if (running_) {
            running_ = false;
            if (worker_thread_.joinable()) {
                worker_thread_.join();
            }
        }
    }

    bool isRunning() const { return running_; }

private:
    void run() {
        while (running_) {
            std::cout << "Worker " << id_ << " processing..." << std::endl;
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        }
        std::cout << "Worker " << id_ << " stopped" << std::endl;
    }
};

// Thread pool pattern
class SimpleThreadPool {
private:
    std::vector<std::thread> threads_;
    std::vector<std::function<void()>> tasks_;
    bool shutdown_ = false;

public:
    SimpleThreadPool(size_t num_threads) {
        for (size_t i = 0; i < num_threads; ++i) {
            threads_.emplace_back([this, i] {
                workerLoop(i);
            });
        }
    }

    ~SimpleThreadPool() {
        shutdown_ = true;
        for (auto& thread : threads_) {
            if (thread.joinable()) {
                thread.join();
            }
        }
    }

    void addTask(std::function<void()> task) {
        tasks_.push_back(std::move(task));
    }

private:
    void workerLoop(size_t worker_id) {
        while (!shutdown_) {
            if (!tasks_.empty()) {
                auto task = tasks_.back();
                tasks_.pop_back();
                task();
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
    }
};

// Thread local storage
thread_local int tls_counter = 0;

void incrementThreadLocal() {
    tls_counter++;
    std::cout << "Thread " << std::this_thread::get_id()
              << " counter: " << tls_counter << std::endl;
}

// Function demonstrating thread creation patterns
void demonstrateThreadBasics() {
    std::cout << "=== Basic Thread Creation ===" << std::endl;

    // Create thread with function
    std::thread t1(simpleThreadFunction, 1);
    t1.join();

    // Create thread with lambda
    std::thread t2([]() {
        std::cout << "Lambda thread running" << std::endl;
    });
    t2.join();

    // Create thread with parameters
    std::thread t3(threadWithParameters, 3, "Hello from thread", 3.14);
    t3.join();

    // Multiple threads
    std::vector<std::thread> threads;
    for (int i = 0; i < 5; ++i) {
        threads.emplace_back(simpleThreadFunction, i);
    }

    // Join all threads
    for (auto& t : threads) {
        t.join();
    }

    // Detached threads
    std::thread detached([]() {
        std::cout << "Detached thread running" << std::endl;
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    });
    detached.detach();

    // Thread with class member function
    ThreadedWorker worker(1);
    worker.start();
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    worker.stop();

    // Thread pool usage
    SimpleThreadPool pool(4);
    for (int i = 0; i < 10; ++i) {
        pool.addTask([i]() {
            std::cout << "Task " << i << " executed" << std::endl;
        });
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(200));

    // Thread local storage
    std::vector<std::thread> tls_threads;
    for (int i = 0; i < 3; ++i) {
        tls_threads.emplace_back([]() {
            for (int j = 0; j < 3; ++j) {
                incrementThreadLocal();
            }
        });
    }

    for (auto& t : tls_threads) {
        t.join();
    }
}

// Thread utilities
void demonstrateThreadUtilities() {
    std::cout << "=== Thread Utilities ===" << std::endl;

    // Get thread ID
    std::thread::id main_thread_id = std::this_thread::get_id();
    std::cout << "Main thread ID: " << main_thread_id << std::endl;

    // Hardware concurrency
    unsigned int num_cores = std::thread::hardware_concurrency();
    std::cout << "Hardware concurrency: " << num_cores << " cores" << std::endl;

    // Thread sleep
    auto start = std::chrono::steady_clock::now();
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    auto end = std::chrono::steady_clock::now();
    auto duration = std::chrono::duration_cast<std::chrono::milliseconds>(end - start);
    std::cout << "Slept for " << duration.count() << " milliseconds" << std::endl;

    // Thread yield
    std::thread yielder([]() {
        for (int i = 0; i < 1000; ++i) {
            if (i % 100 == 0) {
                std::this_thread::yield();  // Give other threads a chance
            }
        }
    });
    yielder.join();
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_concurrency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_concurrency_project.name

    # Expected thread-related classes and functions
    expected_entities = [
        f"{project_name}.thread_basics.simpleThreadFunction",
        f"{project_name}.thread_basics.threadWithParameters",
        f"{project_name}.thread_basics.ThreadedWorker",
        f"{project_name}.thread_basics.SimpleThreadPool",
        f"{project_name}.thread_basics.demonstrateThreadBasics",
    ]

    # Get all node creation calls
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    function_calls = [call for call in all_calls if call[0][0] == "Function"]
    method_calls = [call for call in all_calls if call[0][0] == "Method"]

    created_entities = {
        call[0][1]["qualified_name"]
        for call in class_calls + function_calls + method_calls
    }

    # Verify expected entities were created
    found_entities = [
        entity for entity in expected_entities if entity in created_entities
    ]
    assert len(found_entities) >= 4, (
        f"Expected at least 4 thread entities, found {len(found_entities)}: {found_entities}"
    )


def test_mutex_and_locks(
    cpp_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test mutex and lock mechanisms."""
    test_file = cpp_concurrency_project / "mutex_locks.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <thread>
#include <mutex>
#include <shared_mutex>
#include <vector>
#include <chrono>

// Global mutex for demonstration
std::mutex global_mutex;
std::recursive_mutex recursive_mutex;
std::timed_mutex timed_mutex;
std::shared_mutex shared_mutex;

// Shared resource protected by mutex
class Counter {
private:
    mutable std::mutex mutex_;
    int value_;

public:
    Counter() : value_(0) {}

    void increment() {
        std::lock_guard<std::mutex> lock(mutex_);
        value_++;
    }

    void decrement() {
        std::lock_guard<std::mutex> lock(mutex_);
        value_--;
    }

    int getValue() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return value_;
    }

    // Try to increment with timeout
    bool tryIncrement(int timeout_ms) {
        std::unique_lock<std::mutex> lock(mutex_, std::defer_lock);
        if (lock.try_lock_for(std::chrono::milliseconds(timeout_ms))) {
            value_++;
            return true;
        }
        return false;
    }
};

// Bank account with multiple mutexes
class BankAccount {
private:
    mutable std::mutex balance_mutex_;
    mutable std::mutex history_mutex_;
    double balance_;
    std::vector<std::string> history_;

public:
    BankAccount(double initial_balance) : balance_(initial_balance) {}

    void deposit(double amount) {
        std::lock_guard<std::mutex> lock(balance_mutex_);
        balance_ += amount;

        std::lock_guard<std::mutex> history_lock(history_mutex_);
        history_.push_back("Deposit: " + std::to_string(amount));
    }

    bool withdraw(double amount) {
        std::lock_guard<std::mutex> lock(balance_mutex_);
        if (balance_ >= amount) {
            balance_ -= amount;

            std::lock_guard<std::mutex> history_lock(history_mutex_);
            history_.push_back("Withdrawal: " + std::to_string(amount));
            return true;
        }
        return false;
    }

    double getBalance() const {
        std::lock_guard<std::mutex> lock(balance_mutex_);
        return balance_;
    }

    // Transfer with deadlock avoidance
    static void transfer(BankAccount& from, BankAccount& to, double amount) {
        // Use std::lock to avoid deadlock
        std::unique_lock<std::mutex> lock1(from.balance_mutex_, std::defer_lock);
        std::unique_lock<std::mutex> lock2(to.balance_mutex_, std::defer_lock);
        std::lock(lock1, lock2);

        if (from.balance_ >= amount) {
            from.balance_ -= amount;
            to.balance_ += amount;
        }
    }
};

// Read-write lock pattern
class SharedData {
private:
    mutable std::shared_mutex mutex_;
    std::vector<int> data_;

public:
    void write(int value) {
        std::unique_lock<std::shared_mutex> lock(mutex_);
        data_.push_back(value);
    }

    void modify(size_t index, int value) {
        std::unique_lock<std::shared_mutex> lock(mutex_);
        if (index < data_.size()) {
            data_[index] = value;
        }
    }

    int read(size_t index) const {
        std::shared_lock<std::shared_mutex> lock(mutex_);
        if (index < data_.size()) {
            return data_[index];
        }
        return -1;
    }

    size_t size() const {
        std::shared_lock<std::shared_mutex> lock(mutex_);
        return data_.size();
    }

    std::vector<int> getAll() const {
        std::shared_lock<std::shared_mutex> lock(mutex_);
        return data_;  // Return copy
    }
};

// Recursive mutex example
class RecursiveResource {
private:
    mutable std::recursive_mutex mutex_;
    int value_;

public:
    RecursiveResource() : value_(0) {}

    void setValue(int v) {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        value_ = v;
        logValue();  // Recursive call
    }

    void logValue() {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        std::cout << "Value is: " << value_ << std::endl;
    }

    int getValue() const {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        return value_;
    }
};

// Scoped lock (C++17) for multiple mutexes
void demonstrateScopedLock() {
    std::mutex mutex1, mutex2, mutex3;

    {
        std::scoped_lock lock(mutex1, mutex2, mutex3);
        // All mutexes locked atomically
        std::cout << "All mutexes locked with scoped_lock" << std::endl;
    }  // All mutexes unlocked
}

// Once flag pattern
std::once_flag init_flag;
void initializeOnce() {
    std::call_once(init_flag, []() {
        std::cout << "Initialization performed only once" << std::endl;
    });
}

void demonstrateMutexPatterns() {
    std::cout << "=== Mutex and Lock Patterns ===" << std::endl;

    // Basic mutex with lock_guard
    {
        std::lock_guard<std::mutex> lock(global_mutex);
        std::cout << "Protected section with lock_guard" << std::endl;
    }

    // Unique lock with deferred locking
    {
        std::unique_lock<std::mutex> lock(global_mutex, std::defer_lock);
        // Do some work
        lock.lock();
        std::cout << "Protected section with unique_lock" << std::endl;
        lock.unlock();
        // Do more work
    }

    // Try lock
    {
        std::unique_lock<std::mutex> lock(global_mutex, std::try_to_lock);
        if (lock.owns_lock()) {
            std::cout << "Successfully acquired lock with try_lock" << std::endl;
        }
    }

    // Timed mutex
    {
        if (timed_mutex.try_lock_for(std::chrono::milliseconds(100))) {
            std::cout << "Acquired timed mutex" << std::endl;
            timed_mutex.unlock();
        }
    }

    // Counter with multiple threads
    Counter counter;
    std::vector<std::thread> threads;

    for (int i = 0; i < 10; ++i) {
        threads.emplace_back([&counter]() {
            for (int j = 0; j < 1000; ++j) {
                counter.increment();
            }
        });
    }

    for (auto& t : threads) {
        t.join();
    }

    std::cout << "Counter value: " << counter.getValue() << std::endl;

    // Bank account transfers
    BankAccount account1(1000.0);
    BankAccount account2(500.0);

    std::thread t1([&account1, &account2]() {
        for (int i = 0; i < 10; ++i) {
            BankAccount::transfer(account1, account2, 50.0);
        }
    });

    std::thread t2([&account1, &account2]() {
        for (int i = 0; i < 10; ++i) {
            BankAccount::transfer(account2, account1, 30.0);
        }
    });

    t1.join();
    t2.join();

    std::cout << "Account 1 balance: " << account1.getBalance() << std::endl;
    std::cout << "Account 2 balance: " << account2.getBalance() << std::endl;

    // Shared data with read-write lock
    SharedData shared_data;

    // Writers
    std::vector<std::thread> writers;
    for (int i = 0; i < 3; ++i) {
        writers.emplace_back([&shared_data, i]() {
            for (int j = 0; j < 5; ++j) {
                shared_data.write(i * 10 + j);
            }
        });
    }

    // Readers
    std::vector<std::thread> readers;
    for (int i = 0; i < 5; ++i) {
        readers.emplace_back([&shared_data]() {
            for (int j = 0; j < 10; ++j) {
                size_t size = shared_data.size();
                if (size > 0) {
                    shared_data.read(j % size);
                }
            }
        });
    }

    for (auto& t : writers) t.join();
    for (auto& t : readers) t.join();

    // Recursive mutex
    RecursiveResource recursive_resource;
    recursive_resource.setValue(42);

    // Once flag
    std::vector<std::thread> init_threads;
    for (int i = 0; i < 5; ++i) {
        init_threads.emplace_back(initializeOnce);
    }
    for (auto& t : init_threads) t.join();

    // Scoped lock demo
    demonstrateScopedLock();
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_concurrency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_concurrency_project.name

    # Expected mutex-related classes
    expected_classes = [
        f"{project_name}.mutex_locks.Counter",
        f"{project_name}.mutex_locks.BankAccount",
        f"{project_name}.mutex_locks.SharedData",
        f"{project_name}.mutex_locks.RecursiveResource",
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
    assert len(found_classes) >= 3, (
        f"Expected at least 3 mutex-related classes, found {len(found_classes)}: {found_classes}"
    )


def test_atomics_and_memory_ordering(
    cpp_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test atomic operations and memory ordering."""
    test_file = cpp_concurrency_project / "atomics.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <atomic>
#include <thread>
#include <vector>
#include <chrono>

// Basic atomic types
std::atomic<int> atomic_counter{0};
std::atomic<bool> atomic_flag{false};
std::atomic<double> atomic_double{0.0};

// Custom atomic structure
struct AtomicData {
    int x, y;
};
std::atomic<AtomicData> atomic_struct{{0, 0}};

// Lock-free counter
class LockFreeCounter {
private:
    std::atomic<int> count_;

public:
    LockFreeCounter() : count_(0) {}

    void increment() {
        count_.fetch_add(1, std::memory_order_relaxed);
    }

    void decrement() {
        count_.fetch_sub(1, std::memory_order_relaxed);
    }

    int get() const {
        return count_.load(std::memory_order_relaxed);
    }

    bool compareAndSwap(int expected, int desired) {
        return count_.compare_exchange_strong(expected, desired);
    }
};

// Spinlock using atomic flag
class SpinLock {
private:
    std::atomic_flag lock_ = ATOMIC_FLAG_INIT;

public:
    void lock() {
        while (lock_.test_and_set(std::memory_order_acquire)) {
            // Spin
        }
    }

    void unlock() {
        lock_.clear(std::memory_order_release);
    }
};

// Lock-free stack
template<typename T>
class LockFreeStack {
private:
    struct Node {
        T data;
        Node* next;
        Node(T value) : data(std::move(value)), next(nullptr) {}
    };

    std::atomic<Node*> head_;

public:
    LockFreeStack() : head_(nullptr) {}

    ~LockFreeStack() {
        while (Node* old_head = head_.load()) {
            head_.store(old_head->next);
            delete old_head;
        }
    }

    void push(T value) {
        Node* new_node = new Node(std::move(value));
        new_node->next = head_.load();
        while (!head_.compare_exchange_weak(new_node->next, new_node)) {
            // Retry
        }
    }

    bool pop(T& result) {
        Node* old_head = head_.load();
        while (old_head && !head_.compare_exchange_weak(old_head, old_head->next)) {
            // Retry
        }
        if (old_head) {
            result = old_head->data;
            delete old_head;
            return true;
        }
        return false;
    }
};

// Memory ordering examples
class MemoryOrderingDemo {
private:
    std::atomic<bool> flag_{false};
    std::atomic<int> data_{0};

public:
    // Producer with release semantics
    void producer() {
        data_.store(42, std::memory_order_relaxed);
        flag_.store(true, std::memory_order_release);
    }

    // Consumer with acquire semantics
    int consumer() {
        while (!flag_.load(std::memory_order_acquire)) {
            // Wait
        }
        return data_.load(std::memory_order_relaxed);
    }

    // Sequential consistency example
    void sequentialConsistency() {
        data_.store(100);  // Default is memory_order_seq_cst
        flag_.store(true);
    }
};

// Atomic shared pointer
template<typename T>
class AtomicSharedPtr {
private:
    std::atomic<std::shared_ptr<T>> ptr_;

public:
    AtomicSharedPtr() = default;

    void store(std::shared_ptr<T> new_ptr) {
        std::atomic_store(&ptr_, new_ptr);
    }

    std::shared_ptr<T> load() const {
        return std::atomic_load(&ptr_);
    }

    void exchange(std::shared_ptr<T> new_ptr) {
        std::atomic_exchange(&ptr_, new_ptr);
    }
};

void demonstrateAtomicOperations() {
    std::cout << "=== Atomic Operations ===" << std::endl;

    // Basic atomic operations
    atomic_counter.store(10);
    int old_value = atomic_counter.exchange(20);
    std::cout << "Exchanged " << old_value << " with 20" << std::endl;

    // Fetch and add
    int result = atomic_counter.fetch_add(5);
    std::cout << "Fetch and add: old=" << result << ", new=" << atomic_counter.load() << std::endl;

    // Compare and exchange
    int expected = 25;
    int desired = 30;
    if (atomic_counter.compare_exchange_strong(expected, desired)) {
        std::cout << "CAS succeeded" << std::endl;
    }

    // Lock-free counter with multiple threads
    LockFreeCounter counter;
    std::vector<std::thread> threads;

    for (int i = 0; i < 10; ++i) {
        threads.emplace_back([&counter]() {
            for (int j = 0; j < 10000; ++j) {
                counter.increment();
            }
        });
    }

    for (auto& t : threads) {
        t.join();
    }

    std::cout << "Lock-free counter: " << counter.get() << std::endl;

    // Spinlock test
    SpinLock spinlock;
    int shared_value = 0;
    threads.clear();

    for (int i = 0; i < 5; ++i) {
        threads.emplace_back([&spinlock, &shared_value]() {
            for (int j = 0; j < 1000; ++j) {
                spinlock.lock();
                shared_value++;
                spinlock.unlock();
            }
        });
    }

    for (auto& t : threads) {
        t.join();
    }

    std::cout << "Spinlock protected value: " << shared_value << std::endl;

    // Lock-free stack
    LockFreeStack<int> stack;

    // Producer threads
    threads.clear();
    for (int i = 0; i < 3; ++i) {
        threads.emplace_back([&stack, i]() {
            for (int j = 0; j < 5; ++j) {
                stack.push(i * 10 + j);
            }
        });
    }

    // Consumer thread
    threads.emplace_back([&stack]() {
        int value;
        int count = 0;
        while (count < 15) {
            if (stack.pop(value)) {
                std::cout << "Popped: " << value << std::endl;
                count++;
            }
        }
    });

    for (auto& t : threads) {
        t.join();
    }

    // Memory ordering
    MemoryOrderingDemo ordering_demo;

    std::thread producer([&ordering_demo]() {
        ordering_demo.producer();
    });

    std::thread consumer([&ordering_demo]() {
        int value = ordering_demo.consumer();
        std::cout << "Consumer got: " << value << std::endl;
    });

    producer.join();
    consumer.join();

    // Atomic operations on custom types
    AtomicData data{10, 20};
    atomic_struct.store(data);
    AtomicData loaded = atomic_struct.load();
    std::cout << "Atomic struct: x=" << loaded.x << ", y=" << loaded.y << std::endl;

    // Check if types are lock-free
    std::cout << "atomic<int> is lock-free: " << atomic_counter.is_lock_free() << std::endl;
    std::cout << "atomic<AtomicData> is lock-free: " << atomic_struct.is_lock_free() << std::endl;
}

// Performance comparison
void compareAtomicVsMutex() {
    const int iterations = 1000000;

    // Atomic counter
    std::atomic<int> atomic_cnt{0};
    auto start = std::chrono::high_resolution_clock::now();

    std::vector<std::thread> threads;
    for (int i = 0; i < 4; ++i) {
        threads.emplace_back([&atomic_cnt, iterations]() {
            for (int j = 0; j < iterations; ++j) {
                atomic_cnt.fetch_add(1);
            }
        });
    }

    for (auto& t : threads) {
        t.join();
    }

    auto atomic_time = std::chrono::high_resolution_clock::now() - start;

    // Mutex counter
    int mutex_cnt = 0;
    std::mutex mutex;
    start = std::chrono::high_resolution_clock::now();

    threads.clear();
    for (int i = 0; i < 4; ++i) {
        threads.emplace_back([&mutex_cnt, &mutex, iterations]() {
            for (int j = 0; j < iterations; ++j) {
                std::lock_guard<std::mutex> lock(mutex);
                mutex_cnt++;
            }
        });
    }

    for (auto& t : threads) {
        t.join();
    }

    auto mutex_time = std::chrono::high_resolution_clock::now() - start;

    std::cout << "Atomic time: " << std::chrono::duration_cast<std::chrono::milliseconds>(atomic_time).count() << "ms" << std::endl;
    std::cout << "Mutex time: " << std::chrono::duration_cast<std::chrono::milliseconds>(mutex_time).count() << "ms" << std::endl;
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_concurrency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_concurrency_project.name

    # Expected atomic-related classes
    expected_classes = [
        f"{project_name}.atomics.LockFreeCounter",
        f"{project_name}.atomics.SpinLock",
        f"{project_name}.atomics.LockFreeStack",
        f"{project_name}.atomics.MemoryOrderingDemo",
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
    assert len(found_classes) >= 3, (
        f"Expected at least 3 atomic-related classes, found {len(found_classes)}: {found_classes}"
    )


def test_condition_variables_and_futures(
    cpp_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test condition variables, futures, and promises."""
    test_file = cpp_concurrency_project / "condition_futures.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <future>
#include <queue>
#include <chrono>
#include <functional>

// Producer-Consumer with condition variable
template<typename T>
class ProducerConsumerQueue {
private:
    std::queue<T> queue_;
    mutable std::mutex mutex_;
    std::condition_variable cond_var_;
    bool done_ = false;

public:
    void produce(T item) {
        {
            std::unique_lock<std::mutex> lock(mutex_);
            queue_.push(std::move(item));
        }
        cond_var_.notify_one();
    }

    bool consume(T& item) {
        std::unique_lock<std::mutex> lock(mutex_);
        cond_var_.wait(lock, [this] { return !queue_.empty() || done_; });

        if (queue_.empty()) {
            return false;
        }

        item = std::move(queue_.front());
        queue_.pop();
        return true;
    }

    void done() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            done_ = true;
        }
        cond_var_.notify_all();
    }
};

// Event signaling with condition variable
class Event {
private:
    std::mutex mutex_;
    std::condition_variable cv_;
    bool signaled_ = false;

public:
    void signal() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            signaled_ = true;
        }
        cv_.notify_all();
    }

    void wait() {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [this] { return signaled_; });
    }

    bool wait_for(std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mutex_);
        return cv_.wait_for(lock, timeout, [this] { return signaled_; });
    }

    void reset() {
        std::lock_guard<std::mutex> lock(mutex_);
        signaled_ = false;
    }
};

// Barrier implementation
class Barrier {
private:
    std::mutex mutex_;
    std::condition_variable cv_;
    size_t count_;
    size_t waiting_ = 0;
    size_t generation_ = 0;

public:
    explicit Barrier(size_t count) : count_(count) {}

    void wait() {
        std::unique_lock<std::mutex> lock(mutex_);
        size_t gen = generation_;

        if (++waiting_ == count_) {
            generation_++;
            waiting_ = 0;
            cv_.notify_all();
        } else {
            cv_.wait(lock, [this, gen] { return gen != generation_; });
        }
    }
};

// Task with future
template<typename T>
class AsyncTask {
private:
    std::future<T> future_;

public:
    template<typename Func, typename... Args>
    void launch(Func&& func, Args&&... args) {
        future_ = std::async(std::launch::async,
                           std::forward<Func>(func),
                           std::forward<Args>(args)...);
    }

    T get() {
        return future_.get();
    }

    bool isReady() const {
        return future_.wait_for(std::chrono::seconds(0)) == std::future_status::ready;
    }

    void wait() {
        future_.wait();
    }
};

// Promise-based communication
class PromiseChannel {
private:
    std::promise<int> promise_;
    std::future<int> future_;

public:
    PromiseChannel() : future_(promise_.get_future()) {}

    void send(int value) {
        promise_.set_value(value);
    }

    int receive() {
        return future_.get();
    }

    bool hasValue() const {
        return future_.wait_for(std::chrono::seconds(0)) == std::future_status::ready;
    }
};

// Packaged task example
class TaskExecutor {
private:
    std::vector<std::packaged_task<int()>> tasks_;
    std::mutex mutex_;

public:
    std::future<int> addTask(std::function<int()> func) {
        std::lock_guard<std::mutex> lock(mutex_);
        tasks_.emplace_back(std::move(func));
        return tasks_.back().get_future();
    }

    void executeTasks() {
        std::lock_guard<std::mutex> lock(mutex_);
        for (auto& task : tasks_) {
            task();
        }
        tasks_.clear();
    }
};

// Shared future for multiple waiters
class BroadcastResult {
private:
    std::shared_future<int> shared_future_;

public:
    void setFuture(std::future<int>&& future) {
        shared_future_ = future.share();
    }

    int waitForResult() {
        return shared_future_.get();
    }
};

void demonstrateConditionVariables() {
    std::cout << "=== Condition Variables ===" << std::endl;

    // Producer-Consumer pattern
    ProducerConsumerQueue<int> queue;

    std::thread producer([&queue]() {
        for (int i = 1; i <= 5; ++i) {
            queue.produce(i);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        queue.done();
    });

    std::thread consumer([&queue]() {
        int item;
        while (queue.consume(item)) {
            std::cout << "Consumed: " << item << std::endl;
        }
    });

    producer.join();
    consumer.join();

    // Event signaling
    Event event;

    std::thread waiter([&event]() {
        std::cout << "Waiting for event..." << std::endl;
        event.wait();
        std::cout << "Event received!" << std::endl;
    });

    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    event.signal();
    waiter.join();

    // Timed wait
    Event timed_event;
    bool timeout = !timed_event.wait_for(std::chrono::milliseconds(100));
    std::cout << "Timed wait " << (timeout ? "timed out" : "succeeded") << std::endl;

    // Barrier synchronization
    Barrier barrier(3);
    std::vector<std::thread> threads;

    for (int i = 0; i < 3; ++i) {
        threads.emplace_back([&barrier, i]() {
            std::cout << "Thread " << i << " reached barrier" << std::endl;
            barrier.wait();
            std::cout << "Thread " << i << " passed barrier" << std::endl;
        });
    }

    for (auto& t : threads) {
        t.join();
    }
}

void demonstrateFutures() {
    std::cout << "=== Futures and Promises ===" << std::endl;

    // Async task
    auto future = std::async(std::launch::async, []() {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        return 42;
    });

    std::cout << "Doing other work..." << std::endl;
    int result = future.get();
    std::cout << "Async result: " << result << std::endl;

    // Promise and future
    std::promise<std::string> promise;
    std::future<std::string> string_future = promise.get_future();

    std::thread promise_thread([&promise]() {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        promise.set_value("Hello from promise!");
    });

    std::string message = string_future.get();
    std::cout << "Promise message: " << message << std::endl;
    promise_thread.join();

    // AsyncTask wrapper
    AsyncTask<int> task;
    task.launch([]() {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        return 123;
    });

    while (!task.isReady()) {
        std::cout << "Task not ready yet..." << std::endl;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    std::cout << "Task result: " << task.get() << std::endl;

    // Promise channel
    PromiseChannel channel;

    std::thread sender([&channel]() {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        channel.send(999);
    });

    int received = channel.receive();
    std::cout << "Received via channel: " << received << std::endl;
    sender.join();

    // Packaged task
    TaskExecutor executor;

    auto future1 = executor.addTask([]() { return 10; });
    auto future2 = executor.addTask([]() { return 20; });
    auto future3 = executor.addTask([]() { return 30; });

    executor.executeTasks();

    std::cout << "Task results: " << future1.get() << ", "
              << future2.get() << ", " << future3.get() << std::endl;

    // Shared future
    BroadcastResult broadcast;

    auto shared_promise = std::promise<int>();
    broadcast.setFuture(shared_promise.get_future());

    std::vector<std::thread> waiters;
    for (int i = 0; i < 3; ++i) {
        waiters.emplace_back([&broadcast, i]() {
            int result = broadcast.waitForResult();
            std::cout << "Waiter " << i << " got result: " << result << std::endl;
        });
    }

    shared_promise.set_value(777);

    for (auto& t : waiters) {
        t.join();
    }

    // Exception handling with futures
    auto exception_future = std::async(std::launch::async, []() {
        throw std::runtime_error("Async error");
        return 0;
    });

    try {
        exception_future.get();
    } catch (const std::exception& e) {
        std::cout << "Caught async exception: " << e.what() << std::endl;
    }
}

// Async pipeline pattern
void demonstrateAsyncPipeline() {
    std::cout << "=== Async Pipeline ===" << std::endl;

    auto stage1 = std::async(std::launch::async, []() {
        std::cout << "Stage 1: Processing..." << std::endl;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        return 10;
    });

    auto stage2 = std::async(std::launch::async, [&stage1]() {
        int input = stage1.get();
        std::cout << "Stage 2: Processing input " << input << std::endl;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        return input * 2;
    });

    auto stage3 = std::async(std::launch::async, [&stage2]() {
        int input = stage2.get();
        std::cout << "Stage 3: Processing input " << input << std::endl;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        return input + 5;
    });

    int final_result = stage3.get();
    std::cout << "Pipeline result: " << final_result << std::endl;
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_concurrency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_concurrency_project.name

    # Expected condition variable and future classes
    expected_classes = [
        f"{project_name}.condition_futures.ProducerConsumerQueue",
        f"{project_name}.condition_futures.Event",
        f"{project_name}.condition_futures.Barrier",
        f"{project_name}.condition_futures.AsyncTask",
        f"{project_name}.condition_futures.PromiseChannel",
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
    assert len(found_classes) >= 4, (
        f"Expected at least 4 condition/future classes, found {len(found_classes)}: {found_classes}"
    )


def test_cpp_concurrency_comprehensive(
    cpp_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all concurrency patterns create proper relationships."""
    test_file = cpp_concurrency_project / "comprehensive_concurrency.cpp"
    test_file.write_text(
        """
// Every C++ concurrency pattern in one file
#include <iostream>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <atomic>
#include <future>
#include <memory>
#include <vector>
#include <chrono>

// Thread pool with work stealing
class WorkStealingThreadPool {
private:
    std::vector<std::thread> threads_;
    std::atomic<bool> shutdown_{false};
    std::mutex queue_mutex_;
    std::condition_variable cv_;
    std::queue<std::function<void()>> tasks_;

public:
    WorkStealingThreadPool(size_t num_threads) {
        for (size_t i = 0; i < num_threads; ++i) {
            threads_.emplace_back([this] { workerLoop(); });
        }
    }

    ~WorkStealingThreadPool() {
        {
            std::lock_guard<std::mutex> lock(queue_mutex_);
            shutdown_ = true;
        }
        cv_.notify_all();
        for (auto& t : threads_) {
            t.join();
        }
    }

    template<typename F>
    void submit(F&& f) {
        {
            std::lock_guard<std::mutex> lock(queue_mutex_);
            tasks_.push(std::forward<F>(f));
        }
        cv_.notify_one();
    }

private:
    void workerLoop() {
        while (!shutdown_) {
            std::function<void()> task;
            {
                std::unique_lock<std::mutex> lock(queue_mutex_);
                cv_.wait(lock, [this] { return shutdown_ || !tasks_.empty(); });

                if (shutdown_ && tasks_.empty()) {
                    return;
                }

                if (!tasks_.empty()) {
                    task = std::move(tasks_.front());
                    tasks_.pop();
                }
            }

            if (task) {
                task();
            }
        }
    }
};

// Concurrent data structure with fine-grained locking
template<typename K, typename V>
class ConcurrentHashMap {
private:
    static constexpr size_t num_buckets = 16;

    struct Bucket {
        mutable std::shared_mutex mutex;
        std::unordered_map<K, V> map;
    };

    std::array<Bucket, num_buckets> buckets_;

    size_t getBucketIndex(const K& key) const {
        return std::hash<K>{}(key) % num_buckets;
    }

public:
    void insert(const K& key, const V& value) {
        size_t index = getBucketIndex(key);
        std::unique_lock<std::shared_mutex> lock(buckets_[index].mutex);
        buckets_[index].map[key] = value;
    }

    std::optional<V> get(const K& key) const {
        size_t index = getBucketIndex(key);
        std::shared_lock<std::shared_mutex> lock(buckets_[index].mutex);

        auto it = buckets_[index].map.find(key);
        if (it != buckets_[index].map.end()) {
            return it->second;
        }
        return std::nullopt;
    }

    bool remove(const K& key) {
        size_t index = getBucketIndex(key);
        std::unique_lock<std::shared_mutex> lock(buckets_[index].mutex);
        return buckets_[index].map.erase(key) > 0;
    }
};

// Active object pattern
class ActiveObject {
private:
    std::thread worker_;
    std::queue<std::function<void()>> tasks_;
    std::mutex mutex_;
    std::condition_variable cv_;
    std::atomic<bool> stop_{false};

public:
    ActiveObject() : worker_([this] { run(); }) {}

    ~ActiveObject() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stop_ = true;
        }
        cv_.notify_all();
        worker_.join();
    }

    template<typename F>
    auto enqueue(F&& f) -> std::future<decltype(f())> {
        auto task = std::make_shared<std::packaged_task<decltype(f())()>>(
            std::forward<F>(f)
        );
        auto result = task->get_future();

        {
            std::lock_guard<std::mutex> lock(mutex_);
            tasks_.push([task] { (*task)(); });
        }
        cv_.notify_one();

        return result;
    }

private:
    void run() {
        while (!stop_) {
            std::function<void()> task;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                cv_.wait(lock, [this] { return stop_ || !tasks_.empty(); });

                if (stop_ && tasks_.empty()) {
                    return;
                }

                if (!tasks_.empty()) {
                    task = std::move(tasks_.front());
                    tasks_.pop();
                }
            }

            if (task) {
                task();
            }
        }
    }
};

// Read-Write lock pattern with upgradeable locks
class UpgradeableRWLock {
private:
    mutable std::shared_mutex shared_mutex_;
    mutable std::mutex upgrade_mutex_;

public:
    class ReadLock {
        const UpgradeableRWLock* parent_;
        std::shared_lock<std::shared_mutex> lock_;
    public:
        explicit ReadLock(const UpgradeableRWLock* parent)
            : parent_(parent), lock_(parent->shared_mutex_) {}
    };

    class WriteLock {
        UpgradeableRWLock* parent_;
        std::unique_lock<std::shared_mutex> lock_;
    public:
        explicit WriteLock(UpgradeableRWLock* parent)
            : parent_(parent), lock_(parent->shared_mutex_) {}
    };

    class UpgradeableLock {
        UpgradeableRWLock* parent_;
        std::unique_lock<std::mutex> upgrade_lock_;
        std::shared_lock<std::shared_mutex> read_lock_;
    public:
        explicit UpgradeableLock(UpgradeableRWLock* parent)
            : parent_(parent),
              upgrade_lock_(parent->upgrade_mutex_),
              read_lock_(parent->shared_mutex_) {}

        WriteLock upgrade() {
            read_lock_.unlock();
            return WriteLock(parent_);
        }
    };

    ReadLock read() const { return ReadLock(this); }
    WriteLock write() { return WriteLock(this); }
    UpgradeableLock upgradeable() { return UpgradeableLock(this); }
};

// Concurrent pipeline with stages
template<typename T>
class ConcurrentPipeline {
private:
    struct Stage {
        std::function<T(T)> transform;
        std::queue<T> input_queue;
        std::mutex mutex;
        std::condition_variable cv;
        std::thread worker;
        std::atomic<bool> done{false};
    };

    std::vector<std::unique_ptr<Stage>> stages_;

public:
    void addStage(std::function<T(T)> transform) {
        auto stage = std::make_unique<Stage>();
        stage->transform = std::move(transform);

        size_t stage_index = stages_.size();
        stage->worker = std::thread([this, stage_index] {
            processStage(stage_index);
        });

        stages_.push_back(std::move(stage));
    }

    void process(T input) {
        if (!stages_.empty()) {
            std::lock_guard<std::mutex> lock(stages_[0]->mutex);
            stages_[0]->input_queue.push(std::move(input));
            stages_[0]->cv.notify_one();
        }
    }

    void shutdown() {
        for (auto& stage : stages_) {
            stage->done = true;
            stage->cv.notify_all();
        }

        for (auto& stage : stages_) {
            if (stage->worker.joinable()) {
                stage->worker.join();
            }
        }
    }

private:
    void processStage(size_t index) {
        auto& stage = *stages_[index];

        while (!stage.done) {
            T item;
            {
                std::unique_lock<std::mutex> lock(stage.mutex);
                stage.cv.wait(lock, [&stage] {
                    return stage.done || !stage.input_queue.empty();
                });

                if (stage.done && stage.input_queue.empty()) {
                    break;
                }

                if (!stage.input_queue.empty()) {
                    item = std::move(stage.input_queue.front());
                    stage.input_queue.pop();
                }
            }

            T result = stage.transform(item);

            if (index + 1 < stages_.size()) {
                std::lock_guard<std::mutex> lock(stages_[index + 1]->mutex);
                stages_[index + 1]->input_queue.push(std::move(result));
                stages_[index + 1]->cv.notify_one();
            }
        }
    }
};

void demonstrateComprehensiveConcurrency() {
    std::cout << "=== Comprehensive Concurrency Demo ===" << std::endl;

    // Work stealing thread pool
    WorkStealingThreadPool pool(4);

    std::atomic<int> counter{0};
    for (int i = 0; i < 10; ++i) {
        pool.submit([&counter, i] {
            std::cout << "Task " << i << " executed by thread "
                     << std::this_thread::get_id() << std::endl;
            counter.fetch_add(1);
        });
    }

    // Concurrent hash map
    ConcurrentHashMap<std::string, int> map;

    std::vector<std::thread> map_threads;
    for (int i = 0; i < 5; ++i) {
        map_threads.emplace_back([&map, i] {
            for (int j = 0; j < 10; ++j) {
                map.insert("key" + std::to_string(i * 10 + j), i * 10 + j);
            }
        });
    }

    for (auto& t : map_threads) {
        t.join();
    }

    // Active object
    ActiveObject active;

    auto future1 = active.enqueue([] { return 42; });
    auto future2 = active.enqueue([] { return std::string("Hello"); });

    std::cout << "Active object result 1: " << future1.get() << std::endl;
    std::cout << "Active object result 2: " << future2.get() << std::endl;

    // Upgradeable RW lock
    UpgradeableRWLock rw_lock;
    int shared_data = 0;

    std::thread reader([&rw_lock, &shared_data] {
        auto lock = rw_lock.read();
        std::cout << "Read value: " << shared_data << std::endl;
    });

    std::thread upgrader([&rw_lock, &shared_data] {
        auto upgradeable = rw_lock.upgradeable();
        // Read phase
        std::cout << "Upgradeable read: " << shared_data << std::endl;
        // Upgrade to write
        auto write_lock = upgradeable.upgrade();
        shared_data = 100;
        std::cout << "Upgraded and wrote: " << shared_data << std::endl;
    });

    reader.join();
    upgrader.join();

    // Concurrent pipeline
    ConcurrentPipeline<int> pipeline;

    pipeline.addStage([](int x) { return x * 2; });
    pipeline.addStage([](int x) { return x + 10; });
    pipeline.addStage([](int x) {
        std::cout << "Pipeline output: " << x << std::endl;
        return x;
    });

    for (int i = 1; i <= 5; ++i) {
        pipeline.process(i);
    }

    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    pipeline.shutdown();

    std::cout << "Total tasks executed: " << counter.load() << std::endl;
}

// Performance monitoring
class ConcurrencyBenchmark {
public:
    static void benchmark() {
        const int operations = 100000;

        // Benchmark different synchronization methods
        auto mutex_time = benchmarkMutex(operations);
        auto atomic_time = benchmarkAtomic(operations);
        auto lockfree_time = benchmarkLockFree(operations);

        std::cout << "Mutex time: " << mutex_time.count() << "ms" << std::endl;
        std::cout << "Atomic time: " << atomic_time.count() << "ms" << std::endl;
        std::cout << "Lock-free time: " << lockfree_time.count() << "ms" << std::endl;
    }

private:
    static std::chrono::milliseconds benchmarkMutex(int ops) {
        std::mutex mutex;
        int counter = 0;

        auto start = std::chrono::steady_clock::now();

        std::vector<std::thread> threads;
        for (int i = 0; i < 4; ++i) {
            threads.emplace_back([&mutex, &counter, ops] {
                for (int j = 0; j < ops / 4; ++j) {
                    std::lock_guard<std::mutex> lock(mutex);
                    counter++;
                }
            });
        }

        for (auto& t : threads) {
            t.join();
        }

        auto end = std::chrono::steady_clock::now();
        return std::chrono::duration_cast<std::chrono::milliseconds>(end - start);
    }

    static std::chrono::milliseconds benchmarkAtomic(int ops) {
        std::atomic<int> counter{0};

        auto start = std::chrono::steady_clock::now();

        std::vector<std::thread> threads;
        for (int i = 0; i < 4; ++i) {
            threads.emplace_back([&counter, ops] {
                for (int j = 0; j < ops / 4; ++j) {
                    counter.fetch_add(1);
                }
            });
        }

        for (auto& t : threads) {
            t.join();
        }

        auto end = std::chrono::steady_clock::now();
        return std::chrono::duration_cast<std::chrono::milliseconds>(end - start);
    }

    static std::chrono::milliseconds benchmarkLockFree(int ops) {
        // Simple lock-free counter using CAS
        std::atomic<int> counter{0};

        auto start = std::chrono::steady_clock::now();

        std::vector<std::thread> threads;
        for (int i = 0; i < 4; ++i) {
            threads.emplace_back([&counter, ops] {
                for (int j = 0; j < ops / 4; ++j) {
                    int expected = counter.load();
                    while (!counter.compare_exchange_weak(expected, expected + 1)) {
                        // Retry
                    }
                }
            });
        }

        for (auto& t : threads) {
            t.join();
        }

        auto end = std::chrono::steady_clock::now();
        return std::chrono::duration_cast<std::chrono::milliseconds>(end - start);
    }
};

void runComprehensiveBenchmark() {
    ConcurrencyBenchmark::benchmark();
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_concurrency_project,
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

    # Should have comprehensive concurrency coverage
    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_concurrency" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 10, (
        f"Expected at least 10 comprehensive concurrency calls, found {len(comprehensive_calls)}"
    )

    # Test that concurrency parsing doesn't interfere with other relationships
    assert defines_relationships, "Should still have DEFINES relationships"
    print("✅ C++ concurrency relationship validation passed:")
    print(f"   - CALLS relationships: {len(call_relationships)}")
    print(f"   - DEFINES relationships: {len(defines_relationships)}")
    print(f"   - Comprehensive concurrency calls: {len(comprehensive_calls)}")
