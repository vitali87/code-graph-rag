"""
Comprehensive C++ move semantics testing.
Tests rvalue references, move constructors, move assignments, perfect forwarding,
std::move, std::forward, and various move optimization patterns.
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
def cpp_move_semantics_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with move semantics patterns."""
    project_path = temp_repo / "cpp_move_semantics_test"
    project_path.mkdir()

    # Create basic structure
    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_basic_move_semantics(
    cpp_move_semantics_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic move constructors and move assignment operators."""
    test_file = cpp_move_semantics_project / "basic_move_semantics.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <string>
#include <vector>
#include <memory>
#include <utility>

// Class demonstrating move semantics
class MoveableResource {
private:
    std::string* data_;
    size_t size_;

public:
    // Constructor
    MoveableResource(const std::string& data) : size_(data.length()) {
        data_ = new std::string(data);
        std::cout << "Constructor: " << *data_ << std::endl;
    }

    // Copy constructor
    MoveableResource(const MoveableResource& other) : size_(other.size_) {
        data_ = new std::string(*other.data_);
        std::cout << "Copy constructor: " << *data_ << std::endl;
    }

    // Copy assignment operator
    MoveableResource& operator=(const MoveableResource& other) {
        if (this != &other) {
            delete data_;
            size_ = other.size_;
            data_ = new std::string(*other.data_);
            std::cout << "Copy assignment: " << *data_ << std::endl;
        }
        return *this;
    }

    // Move constructor
    MoveableResource(MoveableResource&& other) noexcept : data_(other.data_), size_(other.size_) {
        other.data_ = nullptr;
        other.size_ = 0;
        std::cout << "Move constructor: " << (data_ ? *data_ : "null") << std::endl;
    }

    // Move assignment operator
    MoveableResource& operator=(MoveableResource&& other) noexcept {
        if (this != &other) {
            delete data_;
            data_ = other.data_;
            size_ = other.size_;
            other.data_ = nullptr;
            other.size_ = 0;
            std::cout << "Move assignment: " << (data_ ? *data_ : "null") << std::endl;
        }
        return *this;
    }

    // Destructor
    ~MoveableResource() {
        if (data_) {
            std::cout << "Destructor: " << *data_ << std::endl;
            delete data_;
        } else {
            std::cout << "Destructor: moved-from object" << std::endl;
        }
    }

    // Utility methods
    const std::string& getData() const {
        return data_ ? *data_ : *new std::string("moved-from");
    }

    size_t getSize() const { return size_; }

    bool isEmpty() const { return data_ == nullptr; }
};

// Factory function returning by value (enables RVO/move)
MoveableResource createResource(const std::string& data) {
    return MoveableResource(data);
}

// Function taking rvalue reference
void processRValue(MoveableResource&& resource) {
    std::cout << "Processing rvalue: " << resource.getData() << std::endl;
}

// Function taking lvalue reference
void processLValue(const MoveableResource& resource) {
    std::cout << "Processing lvalue: " << resource.getData() << std::endl;
}

// Overloaded function for both lvalue and rvalue
void processResource(const MoveableResource& resource) {
    std::cout << "Processing lvalue ref: " << resource.getData() << std::endl;
}

void processResource(MoveableResource&& resource) {
    std::cout << "Processing rvalue ref: " << resource.getData() << std::endl;
}

void demonstrateBasicMoveSemantics() {
    std::cout << "=== Basic Move Semantics ===" << std::endl;

    // Constructor
    MoveableResource resource1("Original Data");

    // Copy constructor
    MoveableResource resource2 = resource1;

    // Move constructor using std::move
    MoveableResource resource3 = std::move(resource1);
    std::cout << "After move, resource1 is " << (resource1.isEmpty() ? "empty" : "not empty") << std::endl;

    // Copy assignment
    MoveableResource resource4("Assignment Target");
    resource4 = resource2;

    // Move assignment
    MoveableResource resource5("Move Assignment Target");
    resource5 = std::move(resource2);
    std::cout << "After move assignment, resource2 is " << (resource2.isEmpty() ? "empty" : "not empty") << std::endl;

    // RVO/NRVO - Return Value Optimization
    auto rvo_resource = createResource("RVO Resource");

    // Move from temporary
    MoveableResource temp_resource = createResource("Temporary");

    // Function overload resolution
    MoveableResource permanent("Permanent");
    processResource(permanent);  // Calls lvalue version
    processResource(std::move(permanent));  // Calls rvalue version
    processResource(MoveableResource("Temporary"));  // Calls rvalue version
}

// String class with move semantics
class MyString {
private:
    char* data_;
    size_t length_;
    size_t capacity_;

public:
    // Default constructor
    MyString() : data_(nullptr), length_(0), capacity_(0) {
        std::cout << "MyString default constructor" << std::endl;
    }

    // Constructor from C-string
    MyString(const char* str) {
        length_ = strlen(str);
        capacity_ = length_ + 1;
        data_ = new char[capacity_];
        strcpy(data_, str);
        std::cout << "MyString constructor: " << data_ << std::endl;
    }

    // Copy constructor
    MyString(const MyString& other) : length_(other.length_), capacity_(other.capacity_) {
        if (other.data_) {
            data_ = new char[capacity_];
            strcpy(data_, other.data_);
            std::cout << "MyString copy constructor: " << data_ << std::endl;
        } else {
            data_ = nullptr;
        }
    }

    // Move constructor
    MyString(MyString&& other) noexcept
        : data_(other.data_), length_(other.length_), capacity_(other.capacity_) {
        other.data_ = nullptr;
        other.length_ = 0;
        other.capacity_ = 0;
        std::cout << "MyString move constructor: " << (data_ ? data_ : "null") << std::endl;
    }

    // Copy assignment
    MyString& operator=(const MyString& other) {
        if (this != &other) {
            delete[] data_;
            length_ = other.length_;
            capacity_ = other.capacity_;
            if (other.data_) {
                data_ = new char[capacity_];
                strcpy(data_, other.data_);
                std::cout << "MyString copy assignment: " << data_ << std::endl;
            } else {
                data_ = nullptr;
            }
        }
        return *this;
    }

    // Move assignment
    MyString& operator=(MyString&& other) noexcept {
        if (this != &other) {
            delete[] data_;
            data_ = other.data_;
            length_ = other.length_;
            capacity_ = other.capacity_;
            other.data_ = nullptr;
            other.length_ = 0;
            other.capacity_ = 0;
            std::cout << "MyString move assignment: " << (data_ ? data_ : "null") << std::endl;
        }
        return *this;
    }

    // Destructor
    ~MyString() {
        if (data_) {
            std::cout << "MyString destructor: " << data_ << std::endl;
            delete[] data_;
        } else {
            std::cout << "MyString destructor: moved-from object" << std::endl;
        }
    }

    // Utility methods
    const char* c_str() const { return data_ ? data_ : ""; }
    size_t length() const { return length_; }
    bool empty() const { return data_ == nullptr || length_ == 0; }

    // Concatenation with move semantics
    MyString operator+(const MyString& other) const {
        size_t new_length = length_ + other.length_;
        char* new_data = new char[new_length + 1];

        if (data_) strcpy(new_data, data_);
        else new_data[0] = '\\0';

        if (other.data_) strcat(new_data, other.data_);

        MyString result;
        result.data_ = new_data;
        result.length_ = new_length;
        result.capacity_ = new_length + 1;

        return result;  // Move on return
    }

    MyString& operator+=(const MyString& other) {
        if (other.length_ > 0) {
            size_t new_length = length_ + other.length_;
            if (new_length >= capacity_) {
                capacity_ = new_length * 2;
                char* new_data = new char[capacity_];
                if (data_) {
                    strcpy(new_data, data_);
                    delete[] data_;
                } else {
                    new_data[0] = '\\0';
                }
                data_ = new_data;
            }
            strcat(data_, other.data_);
            length_ = new_length;
        }
        return *this;
    }
};

void demonstrateStringMoveSemantics() {
    std::cout << "=== String Move Semantics ===" << std::endl;

    MyString str1("Hello");
    MyString str2(" World");

    // Copy construction
    MyString str3 = str1;

    // Move construction
    MyString str4 = std::move(str2);
    std::cout << "str2 after move: '" << str2.c_str() << "'" << std::endl;

    // String concatenation
    MyString result = str1 + str4;  // Creates temporary, then moves
    std::cout << "Concatenation result: " << result.c_str() << std::endl;

    // Move assignment
    MyString final_str("Initial");
    final_str = std::move(result);
    std::cout << "Final string: " << final_str.c_str() << std::endl;
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_move_semantics_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_move_semantics_project.name

    # Expected move semantics classes and functions
    expected_entities = [
        f"{project_name}.basic_move_semantics.MoveableResource",
        f"{project_name}.basic_move_semantics.MyString",
        f"{project_name}.basic_move_semantics.createResource",
        f"{project_name}.basic_move_semantics.processRValue",
        f"{project_name}.basic_move_semantics.demonstrateBasicMoveSemantics",
    ]

    # Get all node creation calls
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    function_calls = [call for call in all_calls if call[0][0] == "Function"]

    created_entities = {
        call[0][1]["qualified_name"] for call in class_calls + function_calls
    }

    # Verify expected entities were created
    found_entities = [
        entity for entity in expected_entities if entity in created_entities
    ]
    assert len(found_entities) >= 4, (
        f"Expected at least 4 move semantics entities, found {len(found_entities)}: {found_entities}"
    )


def test_perfect_forwarding(
    cpp_move_semantics_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test perfect forwarding and universal references."""
    test_file = cpp_move_semantics_project / "perfect_forwarding.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <string>
#include <vector>
#include <utility>
#include <type_traits>

// Simple class for forwarding demonstrations
class ForwardingTarget {
private:
    std::string name_;

public:
    // Constructor from string
    ForwardingTarget(const std::string& name) : name_(name) {
        std::cout << "ForwardingTarget copy constructor: " << name_ << std::endl;
    }

    // Constructor from rvalue string
    ForwardingTarget(std::string&& name) : name_(std::move(name)) {
        std::cout << "ForwardingTarget move constructor: " << name_ << std::endl;
    }

    // Copy constructor
    ForwardingTarget(const ForwardingTarget& other) : name_(other.name_) {
        std::cout << "ForwardingTarget copy from other: " << name_ << std::endl;
    }

    // Move constructor
    ForwardingTarget(ForwardingTarget&& other) noexcept : name_(std::move(other.name_)) {
        std::cout << "ForwardingTarget move from other: " << name_ << std::endl;
    }

    const std::string& getName() const { return name_; }
};

// Perfect forwarding factory
template<typename T, typename... Args>
std::unique_ptr<T> make_unique_perfect(Args&&... args) {
    std::cout << "Perfect forwarding factory called" << std::endl;
    return std::make_unique<T>(std::forward<Args>(args)...);
}

// Wrapper with perfect forwarding
template<typename T>
class Wrapper {
private:
    T wrapped_;

public:
    // Perfect forwarding constructor
    template<typename U>
    Wrapper(U&& value) : wrapped_(std::forward<U>(value)) {
        std::cout << "Wrapper perfect forwarding constructor" << std::endl;
    }

    // Perfect forwarding assignment
    template<typename U>
    Wrapper& operator=(U&& value) {
        wrapped_ = std::forward<U>(value);
        std::cout << "Wrapper perfect forwarding assignment" << std::endl;
        return *this;
    }

    const T& get() const { return wrapped_; }
    T& get() { return wrapped_; }
};

// Function with universal reference
template<typename T>
void processUniversal(T&& param) {
    std::cout << "Universal reference function called" << std::endl;
    std::cout << "Type: " << typeid(T).name() << std::endl;

    if constexpr (std::is_lvalue_reference_v<T>) {
        std::cout << "Received lvalue reference" << std::endl;
    } else {
        std::cout << "Received rvalue reference" << std::endl;
    }
}

// Forwarding function
template<typename T>
void forwardingFunction(T&& param) {
    std::cout << "Forwarding function - before forward" << std::endl;
    processUniversal(std::forward<T>(param));
    std::cout << "Forwarding function - after forward" << std::endl;
}

// Variadic template with perfect forwarding
template<typename Func, typename... Args>
auto invoke_perfect(Func&& func, Args&&... args)
    -> decltype(std::forward<Func>(func)(std::forward<Args>(args)...)) {
    std::cout << "Perfect forwarding invoke with " << sizeof...(args) << " arguments" << std::endl;
    return std::forward<Func>(func)(std::forward<Args>(args)...);
}

// Function object for testing
struct MultiplyBy {
    int factor_;

    MultiplyBy(int factor) : factor_(factor) {}

    int operator()(int value) const {
        std::cout << "MultiplyBy called: " << value << " * " << factor_ << std::endl;
        return value * factor_;
    }
};

void demonstratePerfectForwarding() {
    std::cout << "=== Perfect Forwarding ===" << std::endl;

    // Perfect forwarding factory
    std::string lvalue_str = "LValue String";
    auto target1 = make_unique_perfect<ForwardingTarget>(lvalue_str);  // Copy
    auto target2 = make_unique_perfect<ForwardingTarget>(std::string("RValue String"));  // Move

    // Wrapper with perfect forwarding
    std::string wrapped_str = "Wrapped";
    Wrapper<std::string> wrapper1(wrapped_str);  // Copy
    Wrapper<std::string> wrapper2(std::string("Move Wrapped"));  // Move

    // Universal reference function
    std::string test_str = "Test";
    processUniversal(test_str);  // Lvalue reference
    processUniversal(std::string("Temporary"));  // Rvalue reference
    processUniversal(std::move(test_str));  // Rvalue reference

    // Forwarding chain
    std::string forward_str = "Forward Test";
    forwardingFunction(forward_str);  // Lvalue
    forwardingFunction(std::string("Forward Temp"));  // Rvalue

    // Perfect forwarding with function objects
    MultiplyBy multiply_by_3(3);

    int result1 = invoke_perfect(multiply_by_3, 5);  // Lvalue function object
    int result2 = invoke_perfect(MultiplyBy(2), 7);  // Rvalue function object

    std::cout << "Results: " << result1 << ", " << result2 << std::endl;

    // Lambda with perfect forwarding
    auto perfect_lambda = [](auto&& func, auto&&... args) {
        std::cout << "Lambda perfect forwarding" << std::endl;
        return std::forward<decltype(func)>(func)(std::forward<decltype(args)>(args)...);
    };

    int lambda_result = perfect_lambda(MultiplyBy(4), 3);
    std::cout << "Lambda result: " << lambda_result << std::endl;
}

// SFINAE with perfect forwarding
template<typename T>
class SFINAEWrapper {
private:
    T value_;

public:
    // Enable if T is constructible from Args
    template<typename... Args,
             typename = std::enable_if_t<std::is_constructible_v<T, Args...>>>
    SFINAEWrapper(Args&&... args) : value_(std::forward<Args>(args)...) {
        std::cout << "SFINAE wrapper constructed" << std::endl;
    }

    // Perfect forwarding assignment with SFINAE
    template<typename U,
             typename = std::enable_if_t<std::is_assignable_v<T&, U>>>
    SFINAEWrapper& operator=(U&& other) {
        value_ = std::forward<U>(other);
        std::cout << "SFINAE assignment" << std::endl;
        return *this;
    }

    const T& get() const { return value_; }
};

// Conditional perfect forwarding
template<typename T>
void conditionalForward(T&& param) {
    if constexpr (std::is_same_v<std::decay_t<T>, std::string>) {
        std::cout << "String-specific forwarding" << std::endl;
        ForwardingTarget target(std::forward<T>(param));
    } else {
        std::cout << "Generic forwarding for type: " << typeid(T).name() << std::endl;
    }
}

void demonstrateAdvancedForwarding() {
    std::cout << "=== Advanced Perfect Forwarding ===" << std::endl;

    // SFINAE wrapper
    SFINAEWrapper<std::string> sfinae_wrapper("SFINAE Test");
    std::string assign_str = "Assignment";
    sfinae_wrapper = assign_str;
    sfinae_wrapper = std::string("Move Assignment");

    // Conditional forwarding
    std::string cond_str = "Conditional";
    conditionalForward(cond_str);
    conditionalForward(std::string("Conditional Move"));
    conditionalForward(42);
    conditionalForward(3.14);
}

// Reference collapsing demonstration
template<typename T>
void demonstrateReferenceCollapsing(T&& param) {
    std::cout << "=== Reference Collapsing ===" << std::endl;

    // T&& can be either lvalue reference or rvalue reference
    using param_type = decltype(param);

    std::cout << "Parameter type info:" << std::endl;
    std::cout << "  Is lvalue reference: " << std::is_lvalue_reference_v<param_type> << std::endl;
    std::cout << "  Is rvalue reference: " << std::is_rvalue_reference_v<param_type> << std::endl;
    std::cout << "  Type name: " << typeid(T).name() << std::endl;

    // Forward appropriately
    ForwardingTarget target(std::forward<T>(param));
}

void testReferenceCollapsing() {
    std::string lvalue = "LValue";
    std::string& lvalue_ref = lvalue;

    // T deduced as std::string&, T&& becomes std::string& (reference collapsing)
    demonstrateReferenceCollapsing(lvalue);
    demonstrateReferenceCollapsing(lvalue_ref);

    // T deduced as std::string, T&& becomes std::string&&
    demonstrateReferenceCollapsing(std::string("RValue"));
    demonstrateReferenceCollapsing(std::move(lvalue));
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_move_semantics_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_move_semantics_project.name

    # Expected perfect forwarding classes and functions
    expected_classes = [
        f"{project_name}.perfect_forwarding.ForwardingTarget",
        f"{project_name}.perfect_forwarding.Wrapper",
        f"{project_name}.perfect_forwarding.MultiplyBy",
        f"{project_name}.perfect_forwarding.SFINAEWrapper",
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
        f"Expected at least 3 perfect forwarding classes, found {len(found_classes)}: {found_classes}"
    )


def test_move_optimization_patterns(
    cpp_move_semantics_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test various move optimization patterns and techniques."""
    test_file = cpp_move_semantics_project / "move_optimization.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <string>
#include <vector>
#include <memory>
#include <utility>
#include <algorithm>

// Container with move optimizations
template<typename T>
class OptimizedVector {
private:
    T* data_;
    size_t size_;
    size_t capacity_;

    void reallocate(size_t new_capacity) {
        T* new_data = static_cast<T*>(operator new(new_capacity * sizeof(T)));

        if constexpr (std::is_nothrow_move_constructible_v<T>) {
            // Use move construction if it's noexcept
            for (size_t i = 0; i < size_; ++i) {
                new (new_data + i) T(std::move(data_[i]));
                data_[i].~T();
            }
        } else {
            // Fall back to copy construction
            for (size_t i = 0; i < size_; ++i) {
                new (new_data + i) T(data_[i]);
                data_[i].~T();
            }
        }

        operator delete(data_);
        data_ = new_data;
        capacity_ = new_capacity;
    }

public:
    OptimizedVector() : data_(nullptr), size_(0), capacity_(0) {}

    ~OptimizedVector() {
        for (size_t i = 0; i < size_; ++i) {
            data_[i].~T();
        }
        operator delete(data_);
    }

    // Move constructor
    OptimizedVector(OptimizedVector&& other) noexcept
        : data_(other.data_), size_(other.size_), capacity_(other.capacity_) {
        other.data_ = nullptr;
        other.size_ = 0;
        other.capacity_ = 0;
    }

    // Move assignment
    OptimizedVector& operator=(OptimizedVector&& other) noexcept {
        if (this != &other) {
            // Destroy current elements
            for (size_t i = 0; i < size_; ++i) {
                data_[i].~T();
            }
            operator delete(data_);

            // Move from other
            data_ = other.data_;
            size_ = other.size_;
            capacity_ = other.capacity_;

            other.data_ = nullptr;
            other.size_ = 0;
            other.capacity_ = 0;
        }
        return *this;
    }

    // Copy operations (rule of 5)
    OptimizedVector(const OptimizedVector& other) : size_(other.size_), capacity_(other.capacity_) {
        data_ = static_cast<T*>(operator new(capacity_ * sizeof(T)));
        for (size_t i = 0; i < size_; ++i) {
            new (data_ + i) T(other.data_[i]);
        }
    }

    OptimizedVector& operator=(const OptimizedVector& other) {
        if (this != &other) {
            OptimizedVector temp(other);
            *this = std::move(temp);  // Use move assignment
        }
        return *this;
    }

    // Emplace back with perfect forwarding
    template<typename... Args>
    void emplace_back(Args&&... args) {
        if (size_ >= capacity_) {
            reallocate(capacity_ == 0 ? 1 : capacity_ * 2);
        }
        new (data_ + size_) T(std::forward<Args>(args)...);
        ++size_;
    }

    // Push back with move semantics
    void push_back(const T& value) {
        emplace_back(value);
    }

    void push_back(T&& value) {
        emplace_back(std::move(value));
    }

    // Access
    T& operator[](size_t index) { return data_[index]; }
    const T& operator[](size_t index) const { return data_[index]; }

    size_t size() const { return size_; }
    size_t capacity() const { return capacity_; }

    // Iterator support for range-based for
    T* begin() { return data_; }
    T* end() { return data_ + size_; }
    const T* begin() const { return data_; }
    const T* end() const { return data_ + size_; }
};

// Move-only type
class MoveOnlyResource {
private:
    std::unique_ptr<int[]> data_;
    size_t size_;

public:
    explicit MoveOnlyResource(size_t size) : size_(size) {
        data_ = std::make_unique<int[]>(size);
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = static_cast<int>(i);
        }
        std::cout << "MoveOnlyResource created with size " << size_ << std::endl;
    }

    // Move constructor
    MoveOnlyResource(MoveOnlyResource&& other) noexcept
        : data_(std::move(other.data_)), size_(other.size_) {
        other.size_ = 0;
        std::cout << "MoveOnlyResource moved (size " << size_ << ")" << std::endl;
    }

    // Move assignment
    MoveOnlyResource& operator=(MoveOnlyResource&& other) noexcept {
        if (this != &other) {
            data_ = std::move(other.data_);
            size_ = other.size_;
            other.size_ = 0;
            std::cout << "MoveOnlyResource move assigned (size " << size_ << ")" << std::endl;
        }
        return *this;
    }

    // Delete copy operations
    MoveOnlyResource(const MoveOnlyResource&) = delete;
    MoveOnlyResource& operator=(const MoveOnlyResource&) = delete;

    size_t size() const { return size_; }

    int* getData() const { return data_.get(); }

    void process() const {
        if (data_) {
            std::cout << "Processing " << size_ << " elements" << std::endl;
        }
    }
};

// Factory functions with move optimizations
MoveOnlyResource createMoveOnlyResource(size_t size) {
    return MoveOnlyResource(size);  // RVO/NRVO
}

OptimizedVector<std::string> createStringVector() {
    OptimizedVector<std::string> vec;
    vec.emplace_back("First");
    vec.emplace_back("Second");
    vec.push_back(std::string("Third"));
    return vec;  // RVO
}

// Sink function taking by value for optimal performance
void processMoveOnly(MoveOnlyResource resource) {
    std::cout << "Processing move-only resource in sink function" << std::endl;
    resource.process();
}

// Function returning multiple values using structured bindings and move
std::pair<MoveOnlyResource, OptimizedVector<std::string>>
createMultipleResources() {
    auto resource = createMoveOnlyResource(10);
    auto vector = createStringVector();
    return std::make_pair(std::move(resource), std::move(vector));
}

void demonstrateMoveOptimizations() {
    std::cout << "=== Move Optimizations ===" << std::endl;

    // Move-only resource
    auto resource1 = createMoveOnlyResource(5);
    auto resource2 = std::move(resource1);

    // Sink function
    processMoveOnly(createMoveOnlyResource(3));
    processMoveOnly(std::move(resource2));

    // Container optimizations
    OptimizedVector<std::string> vec;

    std::string str1 = "Copy this";
    vec.push_back(str1);  // Copy
    vec.push_back(std::string("Move this"));  // Move
    vec.emplace_back("Construct in place");  // Direct construction

    std::cout << "Vector contents:" << std::endl;
    for (const auto& item : vec) {
        std::cout << "  " << item << std::endl;
    }

    // Move container
    auto vec2 = std::move(vec);
    std::cout << "After move - vec size: " << vec.size() << ", vec2 size: " << vec2.size() << std::endl;

    // Multiple return values
    auto [resource, string_vec] = createMultipleResources();
    resource.process();

    std::cout << "Returned vector size: " << string_vec.size() << std::endl;
}

// Conditional move based on type traits
template<typename T>
class ConditionalMover {
private:
    T value_;

public:
    template<typename U>
    ConditionalMover(U&& value) : value_(std::forward<U>(value)) {}

    // Move if possible, copy otherwise
    T extract() {
        if constexpr (std::is_move_constructible_v<T>) {
            return std::move(value_);
        } else {
            return value_;  // Copy
        }
    }

    // Force move even if potentially throwing
    T extract_force_move() {
        return std::move(value_);
    }

    // Safe move - only if noexcept
    T extract_safe() {
        if constexpr (std::is_nothrow_move_constructible_v<T>) {
            return std::move(value_);
        } else {
            return value_;
        }
    }
};

// Class with conditional noexcept
class ConditionalNoexcept {
private:
    std::string data_;

public:
    ConditionalNoexcept(std::string data) : data_(std::move(data)) {}

    // Copy constructor
    ConditionalNoexcept(const ConditionalNoexcept& other) : data_(other.data_) {}

    // Move constructor - noexcept depends on std::string's move constructor
    ConditionalNoexcept(ConditionalNoexcept&& other)
        noexcept(std::is_nothrow_move_constructible_v<std::string>)
        : data_(std::move(other.data_)) {}

    // Assignment with strong exception safety
    ConditionalNoexcept& operator=(ConditionalNoexcept other) {
        // Pass by value, then move
        swap(*this, other);
        return *this;
    }

    friend void swap(ConditionalNoexcept& first, ConditionalNoexcept& second) noexcept {
        using std::swap;
        swap(first.data_, second.data_);
    }

    const std::string& getData() const { return data_; }
};

void demonstrateConditionalMove() {
    std::cout << "=== Conditional Move Semantics ===" << std::endl;

    // Test with string (move constructible)
    ConditionalMover<std::string> string_mover("Test String");
    auto extracted_string = string_mover.extract();
    std::cout << "Extracted string: " << extracted_string << std::endl;

    // Test with move-only type
    ConditionalMover<MoveOnlyResource> resource_mover(createMoveOnlyResource(7));
    auto extracted_resource = resource_mover.extract();
    extracted_resource.process();

    // Conditional noexcept class
    ConditionalNoexcept obj1("First");
    ConditionalNoexcept obj2("Second");

    std::cout << "Before swap: obj1=" << obj1.getData() << ", obj2=" << obj2.getData() << std::endl;
    swap(obj1, obj2);
    std::cout << "After swap: obj1=" << obj1.getData() << ", obj2=" << obj2.getData() << std::endl;
}

// Algorithm with move optimization
template<typename Iterator>
void move_elements(Iterator first, Iterator last, Iterator dest) {
    while (first != last) {
        *dest = std::move(*first);
        ++first;
        ++dest;
    }
}

// Custom swap with move semantics
template<typename T>
void optimized_swap(T& a, T& b) noexcept(std::is_nothrow_move_constructible_v<T> &&
                                       std::is_nothrow_move_assignable_v<T>) {
    T temp = std::move(a);
    a = std::move(b);
    b = std::move(temp);
}

void demonstrateAlgorithmOptimizations() {
    std::cout << "=== Algorithm Move Optimizations ===" << std::endl;

    // Vector of move-only resources
    std::vector<MoveOnlyResource> resources;
    resources.push_back(createMoveOnlyResource(1));
    resources.push_back(createMoveOnlyResource(2));
    resources.push_back(createMoveOnlyResource(3));

    // Move elements to another vector
    std::vector<MoveOnlyResource> moved_resources;
    moved_resources.reserve(resources.size());

    for (auto& resource : resources) {
        moved_resources.push_back(std::move(resource));
    }

    std::cout << "Moved " << moved_resources.size() << " resources" << std::endl;
    for (const auto& resource : moved_resources) {
        resource.process();
    }

    // Custom swap test
    std::string str_a = "String A";
    std::string str_b = "String B";

    std::cout << "Before swap: a=" << str_a << ", b=" << str_b << std::endl;
    optimized_swap(str_a, str_b);
    std::cout << "After swap: a=" << str_a << ", b=" << str_b << std::endl;
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_move_semantics_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_move_semantics_project.name

    # Expected move optimization classes
    expected_classes = [
        f"{project_name}.move_optimization.OptimizedVector",
        f"{project_name}.move_optimization.MoveOnlyResource",
        f"{project_name}.move_optimization.ConditionalMover",
        f"{project_name}.move_optimization.ConditionalNoexcept",
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
        f"Expected at least 3 move optimization classes, found {len(found_classes)}: {found_classes}"
    )


def test_cpp_move_semantics_comprehensive(
    cpp_move_semantics_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all move semantics patterns create proper relationships."""
    test_file = cpp_move_semantics_project / "comprehensive_move_semantics.cpp"
    test_file.write_text(
        """
// Every C++ move semantics pattern in one file
#include <iostream>
#include <string>
#include <vector>
#include <memory>
#include <utility>
#include <type_traits>

// Comprehensive class with all move patterns
class ComprehensiveMoveClass {
private:
    std::unique_ptr<int[]> data_;
    size_t size_;
    std::string name_;

public:
    // Constructor
    ComprehensiveMoveClass(size_t size, std::string name)
        : size_(size), name_(std::move(name)) {
        data_ = std::make_unique<int[]>(size_);
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = static_cast<int>(i);
        }
        std::cout << "Constructed: " << name_ << std::endl;
    }

    // Copy constructor
    ComprehensiveMoveClass(const ComprehensiveMoveClass& other)
        : size_(other.size_), name_(other.name_) {
        data_ = std::make_unique<int[]>(size_);
        std::copy(other.data_.get(), other.data_.get() + size_, data_.get());
        std::cout << "Copy constructed: " << name_ << std::endl;
    }

    // Move constructor
    ComprehensiveMoveClass(ComprehensiveMoveClass&& other) noexcept
        : data_(std::move(other.data_)), size_(other.size_), name_(std::move(other.name_)) {
        other.size_ = 0;
        std::cout << "Move constructed: " << name_ << std::endl;
    }

    // Copy assignment (copy-and-swap idiom)
    ComprehensiveMoveClass& operator=(const ComprehensiveMoveClass& other) {
        ComprehensiveMoveClass temp(other);
        swap(*this, temp);
        std::cout << "Copy assigned: " << name_ << std::endl;
        return *this;
    }

    // Move assignment
    ComprehensiveMoveClass& operator=(ComprehensiveMoveClass&& other) noexcept {
        if (this != &other) {
            data_ = std::move(other.data_);
            size_ = other.size_;
            name_ = std::move(other.name_);
            other.size_ = 0;
            std::cout << "Move assigned: " << name_ << std::endl;
        }
        return *this;
    }

    // Destructor
    ~ComprehensiveMoveClass() {
        std::cout << "Destroyed: " << (name_.empty() ? "moved-from" : name_) << std::endl;
    }

    // Swap function
    friend void swap(ComprehensiveMoveClass& first, ComprehensiveMoveClass& second) noexcept {
        using std::swap;
        swap(first.data_, second.data_);
        swap(first.size_, second.size_);
        swap(first.name_, second.name_);
    }

    // Perfect forwarding factory method
    template<typename... Args>
    static ComprehensiveMoveClass create(Args&&... args) {
        return ComprehensiveMoveClass(std::forward<Args>(args)...);
    }

    // Move-based operations
    void append(ComprehensiveMoveClass&& other) {
        if (other.data_) {
            auto new_data = std::make_unique<int[]>(size_ + other.size_);
            std::copy(data_.get(), data_.get() + size_, new_data.get());
            std::copy(other.data_.get(), other.data_.get() + other.size_,
                     new_data.get() + size_);

            data_ = std::move(new_data);
            size_ += other.size_;
            name_ += "_" + other.name_;

            // Clear the moved-from object
            other.data_.reset();
            other.size_ = 0;
            other.name_.clear();
        }
    }

    // Accessors
    size_t size() const { return size_; }
    const std::string& name() const { return name_; }
    bool empty() const { return size_ == 0 || !data_; }

    void print() const {
        std::cout << name_ << " (size=" << size_ << "): ";
        if (data_) {
            for (size_t i = 0; i < std::min(size_, size_t(5)); ++i) {
                std::cout << data_[i] << " ";
            }
            if (size_ > 5) std::cout << "...";
        }
        std::cout << std::endl;
    }
};

// Perfect forwarding wrapper
template<typename T>
class UniversalWrapper {
private:
    T wrapped_;

public:
    // Perfect forwarding constructor
    template<typename U, typename = std::enable_if_t<!std::is_same_v<std::decay_t<U>, UniversalWrapper>>>
    UniversalWrapper(U&& value) : wrapped_(std::forward<U>(value)) {
        std::cout << "UniversalWrapper: perfect forwarding constructor" << std::endl;
    }

    // Perfect forwarding call operator
    template<typename... Args>
    auto operator()(Args&&... args) -> decltype(wrapped_(std::forward<Args>(args)...)) {
        std::cout << "UniversalWrapper: perfect forwarding call" << std::endl;
        return wrapped_(std::forward<Args>(args)...);
    }

    // Move-based operations
    T extract() && {  // Ref-qualifier: only callable on rvalue
        return std::move(wrapped_);
    }

    const T& peek() const & {  // Ref-qualifier: only callable on lvalue
        return wrapped_;
    }
};

// Function template with universal references
template<typename Container, typename Func>
auto apply_to_container(Container&& container, Func&& func)
    -> std::vector<decltype(func(*container.begin()))> {

    std::vector<decltype(func(*container.begin()))> result;
    result.reserve(container.size());

    for (auto&& element : std::forward<Container>(container)) {
        result.push_back(func(std::forward<decltype(element)>(element)));
    }

    return result;
}

// Move-only function wrapper
template<typename Func>
class MoveOnlyFunction {
private:
    mutable Func func_;

public:
    explicit MoveOnlyFunction(Func&& f) : func_(std::move(f)) {}

    // Move constructor
    MoveOnlyFunction(MoveOnlyFunction&& other) : func_(std::move(other.func_)) {}

    // Move assignment
    MoveOnlyFunction& operator=(MoveOnlyFunction&& other) {
        func_ = std::move(other.func_);
        return *this;
    }

    // Delete copy operations
    MoveOnlyFunction(const MoveOnlyFunction&) = delete;
    MoveOnlyFunction& operator=(const MoveOnlyFunction&) = delete;

    // Function call
    template<typename... Args>
    auto operator()(Args&&... args) const -> decltype(func_(std::forward<Args>(args)...)) {
        return func_(std::forward<Args>(args)...);
    }
};

// Factory for move-only functions
template<typename Func>
MoveOnlyFunction<Func> make_move_only_function(Func&& func) {
    return MoveOnlyFunction<Func>(std::forward<Func>(func));
}

void demonstrateComprehensiveMoveSemantics() {
    std::cout << "=== Comprehensive Move Semantics Demo ===" << std::endl;

    // Basic move operations
    auto obj1 = ComprehensiveMoveClass::create(5, std::string("Object1"));
    obj1.print();

    auto obj2 = std::move(obj1);  // Move construction
    obj2.print();

    std::cout << "obj1 after move: " << (obj1.empty() ? "empty" : "not empty") << std::endl;

    // Move assignment
    auto obj3 = ComprehensiveMoveClass::create(3, "Object3");
    obj3 = std::move(obj2);  // Move assignment
    obj3.print();

    // Append operation
    auto obj4 = ComprehensiveMoveClass::create(2, "Object4");
    obj3.append(std::move(obj4));
    obj3.print();
    std::cout << "obj4 after append: " << (obj4.empty() ? "empty" : "not empty") << std::endl;

    // Perfect forwarding wrapper
    auto multiplier = [](int x) { return x * 2; };
    UniversalWrapper wrapper(multiplier);

    int result = wrapper(21);
    std::cout << "Wrapper result: " << result << std::endl;

    // Extract from wrapper (only works on rvalue)
    auto extracted = std::move(wrapper).extract();
    result = extracted(15);
    std::cout << "Extracted function result: " << result << std::endl;

    // Container processing with universal references
    std::vector<int> numbers = {1, 2, 3, 4, 5};
    auto squared = apply_to_container(numbers, [](int x) { return x * x; });

    std::cout << "Squared numbers: ";
    for (int n : squared) {
        std::cout << n << " ";
    }
    std::cout << std::endl;

    // Move-only function
    auto move_only_func = make_move_only_function([](const std::string& s) {
        return "Processed: " + s;
    });

    std::string test_str = move_only_func("Test Input");
    std::cout << "Move-only function result: " << test_str << std::endl;

    // Move function to another variable
    auto another_func = std::move(move_only_func);
    std::string another_result = another_func("Another Test");
    std::cout << "Moved function result: " << another_result << std::endl;
}

// Advanced move patterns with type traits
template<typename T>
class SmartMove {
public:
    // Move if nothrow, copy otherwise
    template<typename U>
    static T smart_move(U&& value) {
        if constexpr (std::is_nothrow_move_constructible_v<T>) {
            return std::move(value);
        } else {
            return value;  // Copy for exception safety
        }
    }

    // Conditional move based on type
    template<typename U>
    static auto conditional_move(U&& value) {
        if constexpr (std::is_same_v<std::decay_t<U>, T>) {
            return std::move(value);  // Move same type
        } else {
            return T(std::forward<U>(value));  // Convert different type
        }
    }
};

// RAII with move semantics
class ResourceRAII {
private:
    std::unique_ptr<FILE, decltype(&fclose)> file_;
    std::string filename_;

public:
    explicit ResourceRAII(const std::string& filename)
        : file_(fopen(filename.c_str(), "w"), &fclose), filename_(filename) {
        if (!file_) {
            throw std::runtime_error("Failed to open file: " + filename);
        }
        std::cout << "Opened file: " << filename_ << std::endl;
    }

    // Move constructor
    ResourceRAII(ResourceRAII&& other) noexcept
        : file_(std::move(other.file_)), filename_(std::move(other.filename_)) {
        std::cout << "Moved file resource: " << filename_ << std::endl;
    }

    // Move assignment
    ResourceRAII& operator=(ResourceRAII&& other) noexcept {
        if (this != &other) {
            file_ = std::move(other.file_);
            filename_ = std::move(other.filename_);
            std::cout << "Move assigned file resource: " << filename_ << std::endl;
        }
        return *this;
    }

    // Delete copy operations
    ResourceRAII(const ResourceRAII&) = delete;
    ResourceRAII& operator=(const ResourceRAII&) = delete;

    ~ResourceRAII() {
        if (file_) {
            std::cout << "Closed file: " << filename_ << std::endl;
        } else {
            std::cout << "File resource was moved" << std::endl;
        }
    }

    void write(const std::string& data) {
        if (file_) {
            fprintf(file_.get(), "%s\\n", data.c_str());
            fflush(file_.get());
        }
    }
};

void demonstrateAdvancedMovePatterns() {
    std::cout << "=== Advanced Move Patterns ===" << std::endl;

    // Smart move demonstration
    std::string str1 = "Test String";
    auto moved_str = SmartMove<std::string>::smart_move(std::move(str1));
    std::cout << "Smart moved string: " << moved_str << std::endl;

    // Conditional move
    int number = 42;
    auto moved_num = SmartMove<std::string>::conditional_move(number);
    std::cout << "Conditionally moved number: " << moved_num << std::endl;

    // RAII with move semantics
    try {
        ResourceRAII resource("test_move.txt");
        resource.write("Hello from RAII");

        // Move the resource
        ResourceRAII moved_resource = std::move(resource);
        moved_resource.write("Hello after move");

    } catch (const std::exception& e) {
        std::cout << "Exception: " << e.what() << std::endl;
    }
}

void comprehensiveMoveDemo() {
    demonstrateComprehensiveMoveSemantics();
    demonstrateAdvancedMovePatterns();

    // Final demonstration: complex move chain
    std::cout << "=== Complex Move Chain ===" << std::endl;

    auto factory = []() {
        return ComprehensiveMoveClass::create(10, "Factory");
    };

    auto processor = [](ComprehensiveMoveClass obj) {
        obj.append(ComprehensiveMoveClass::create(5, "Processed"));
        return obj;
    };

    // Chain: factory -> move -> process -> move -> final
    auto final_obj = processor(factory());
    final_obj.print();
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_move_semantics_project,
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

    # Should have comprehensive move semantics coverage
    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_move_semantics" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 8, (
        f"Expected at least 8 comprehensive move semantics calls, found {len(comprehensive_calls)}"
    )

    # Test that move semantics parsing doesn't interfere with other relationships
    assert defines_relationships, "Should still have DEFINES relationships"
    print("✅ C++ move semantics relationship validation passed:")
    print(f"   - CALLS relationships: {len(call_relationships)}")
    print(f"   - DEFINES relationships: {len(defines_relationships)}")
    print(f"   - Comprehensive move semantics calls: {len(comprehensive_calls)}")
