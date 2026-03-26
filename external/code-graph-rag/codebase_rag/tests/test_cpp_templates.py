from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_templates_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with template patterns."""
    project_path = temp_repo / "cpp_templates_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_function_templates(
    cpp_templates_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test function template parsing and instantiation."""
    test_file = cpp_templates_project / "function_templates.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <vector>
#include <algorithm>

// Basic function template
template<typename T>
T maximum(T a, T b) {
    return (a > b) ? a : b;
}

// Function template with multiple parameters
template<typename T, typename U>
auto add(T a, U b) -> decltype(a + b) {
    return a + b;
}

// Function template with non-type parameters
template<typename T, size_t N>
void printArray(const T (&arr)[N]) {
    for (size_t i = 0; i < N; ++i) {
        std::cout << arr[i] << " ";
    }
    std::cout << std::endl;
}

// Variadic function template
template<typename... Args>
void print(Args... args) {
    ((std::cout << args << " "), ...);  // C++17 fold expression
    std::cout << std::endl;
}

// Template specialization
template<>
std::string maximum<std::string>(std::string a, std::string b) {
    return (a.length() > b.length()) ? a : b;
}

// Function template with constraints (C++20 style)
template<typename T>
requires std::is_arithmetic_v<T>
T multiply(T a, T b) {
    return a * b;
}

// Template with default arguments
template<typename T = int, size_t Size = 10>
class FixedArray {
private:
    T data_[Size];

public:
    FixedArray() {
        std::fill(data_, data_ + Size, T{});
    }

    T& operator[](size_t index) { return data_[index]; }
    const T& operator[](size_t index) const { return data_[index]; }

    size_t size() const { return Size; }

    void fill(const T& value) {
        std::fill(data_, data_ + Size, value);
    }
};

// Function template that works with the class template
template<typename T, size_t N>
void processArray(FixedArray<T, N>& arr) {
    for (size_t i = 0; i < arr.size(); ++i) {
        arr[i] = maximum(arr[i], T{});
    }
}

// Template function with template template parameter
template<template<typename> class Container, typename T>
void processContainer(Container<T>& container, const T& value) {
    container.push_back(value);
    std::sort(container.begin(), container.end());
}

// SFINAE (Substitution Failure Is Not An Error) example
template<typename T>
typename std::enable_if_t<std::is_integral_v<T>, T>
safeDivide(T a, T b) {
    return (b != 0) ? a / b : 0;
}

template<typename T>
typename std::enable_if_t<std::is_floating_point_v<T>, T>
safeDivide(T a, T b) {
    return (b != 0.0) ? a / b : std::numeric_limits<T>::quiet_NaN();
}

void demonstrateFunctionTemplates() {
    // Basic template usage
    int maxInt = maximum(10, 20);
    double maxDouble = maximum(3.14, 2.71);
    std::string maxString = maximum<std::string>("hello", "world");  // Uses specialization

    // Auto return type
    auto sum1 = add(5, 3.14);        // int + double -> double
    auto sum2 = add(std::string("Hello"), std::string(" World"));  // string + string -> string

    // Non-type template parameters
    int numbers[] = {1, 2, 3, 4, 5};
    printArray(numbers);  // Template deduction: T=int, N=5

    double values[] = {1.1, 2.2, 3.3};
    printArray(values);   // Template deduction: T=double, N=3

    // Variadic templates
    print("Hello", 42, 3.14, "World");
    print(1, 2, 3, 4, 5);

    // Template with constraints
    int product1 = multiply(5, 6);
    double product2 = multiply(2.5, 4.0);
    // multiply("hello", "world");  // Would cause compilation error

    // Class template usage
    FixedArray<int, 5> intArray;
    intArray.fill(42);
    processArray(intArray);

    FixedArray<double> defaultArray;  // Uses default template arguments: double, 10
    defaultArray[0] = 1.5;
    defaultArray[1] = 2.5;

    // Template template parameter
    std::vector<int> vec;
    processContainer(vec, 10);
    processContainer(vec, 5);
    processContainer(vec, 15);

    // SFINAE examples
    int intResult = safeDivide(10, 3);        // Uses integral version
    double doubleResult = safeDivide(10.0, 3.0);  // Uses floating-point version

    std::cout << "Max int: " << maxInt << std::endl;
    std::cout << "Max double: " << maxDouble << std::endl;
    std::cout << "Max string: " << maxString << std::endl;
    std::cout << "Sum1: " << sum1 << std::endl;
    std::cout << "Integer division: " << intResult << std::endl;
    std::cout << "Double division: " << doubleResult << std::endl;
}

// Template metaprogramming example
template<int N>
struct Factorial {
    static constexpr int value = N * Factorial<N - 1>::value;
};

template<>
struct Factorial<0> {
    static constexpr int value = 1;
};

// Type traits example
template<typename T>
struct TypeInfo {
    static void printInfo() {
        std::cout << "Generic type" << std::endl;
    }
};

template<>
struct TypeInfo<int> {
    static void printInfo() {
        std::cout << "Integer type" << std::endl;
    }
};

template<>
struct TypeInfo<std::string> {
    static void printInfo() {
        std::cout << "String type" << std::endl;
    }
};

void demonstrateTemplateMetaprogramming() {
    // Compile-time computation
    constexpr int fact5 = Factorial<5>::value;  // Computed at compile time
    constexpr int fact10 = Factorial<10>::value;

    std::cout << "5! = " << fact5 << std::endl;
    std::cout << "10! = " << fact10 << std::endl;

    // Type traits
    TypeInfo<int>::printInfo();
    TypeInfo<double>::printInfo();
    TypeInfo<std::string>::printInfo();
}
""",
    )

    run_updater(cpp_templates_project, mock_ingestor)

    project_name = cpp_templates_project.name

    expected_functions = [
        f"{project_name}.function_templates.maximum",
        f"{project_name}.function_templates.add",
        f"{project_name}.function_templates.printArray",
        f"{project_name}.function_templates.print",
        f"{project_name}.function_templates.multiply",
        f"{project_name}.function_templates.processArray",
        f"{project_name}.function_templates.processContainer",
        f"{project_name}.function_templates.safeDivide",
        f"{project_name}.function_templates.demonstrateFunctionTemplates",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )

    expected_classes = [
        f"{project_name}.function_templates.FixedArray",
        f"{project_name}.function_templates.Factorial",
        f"{project_name}.function_templates.TypeInfo",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_class_templates(
    cpp_templates_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test class template parsing and specialization."""
    test_file = cpp_templates_project / "class_templates.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <memory>
#include <vector>
#include <string>

// Basic class template
template<typename T>
class Container {
private:
    T* data_;
    size_t size_;
    size_t capacity_;

public:
    Container() : data_(nullptr), size_(0), capacity_(0) {}

    explicit Container(size_t capacity)
        : data_(new T[capacity]), size_(0), capacity_(capacity) {}

    ~Container() {
        delete[] data_;
    }

    // Copy constructor
    Container(const Container& other)
        : data_(new T[other.capacity_]), size_(other.size_), capacity_(other.capacity_) {
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = other.data_[i];
        }
    }

    // Assignment operator
    Container& operator=(const Container& other) {
        if (this != &other) {
            delete[] data_;
            data_ = new T[other.capacity_];
            size_ = other.size_;
            capacity_ = other.capacity_;
            for (size_t i = 0; i < size_; ++i) {
                data_[i] = other.data_[i];
            }
        }
        return *this;
    }

    void push_back(const T& item) {
        if (size_ >= capacity_) {
            resize(capacity_ == 0 ? 1 : capacity_ * 2);
        }
        data_[size_++] = item;
    }

    T& operator[](size_t index) { return data_[index]; }
    const T& operator[](size_t index) const { return data_[index]; }

    size_t size() const { return size_; }
    size_t capacity() const { return capacity_; }

    bool empty() const { return size_ == 0; }

    void clear() { size_ = 0; }

private:
    void resize(size_t newCapacity) {
        T* newData = new T[newCapacity];
        for (size_t i = 0; i < size_; ++i) {
            newData[i] = data_[i];
        }
        delete[] data_;
        data_ = newData;
        capacity_ = newCapacity;
    }
};

// Template specialization for bool (bit packing)
template<>
class Container<bool> {
private:
    std::vector<bool> data_;  // Use std::vector<bool> for bit packing

public:
    Container() = default;
    explicit Container(size_t capacity) { data_.reserve(capacity); }

    void push_back(bool item) {
        data_.push_back(item);
    }

    bool operator[](size_t index) const {
        return data_[index];
    }

    size_t size() const { return data_.size(); }
    size_t capacity() const { return data_.capacity(); }
    bool empty() const { return data_.empty(); }
    void clear() { data_.clear(); }

    // Special method for bool specialization
    size_t count_true() const {
        size_t count = 0;
        for (bool b : data_) {
            if (b) count++;
        }
        return count;
    }
};

// Template with multiple parameters
template<typename Key, typename Value, size_t MaxSize = 100>
class SimpleMap {
private:
    struct Pair {
        Key key;
        Value value;
        bool used = false;
    };

    Pair data_[MaxSize];
    size_t size_;

public:
    SimpleMap() : size_(0) {}

    bool insert(const Key& key, const Value& value) {
        // Check if key already exists
        for (size_t i = 0; i < MaxSize; ++i) {
            if (data_[i].used && data_[i].key == key) {
                data_[i].value = value;  // Update existing
                return true;
            }
        }

        // Find empty slot
        for (size_t i = 0; i < MaxSize; ++i) {
            if (!data_[i].used) {
                data_[i].key = key;
                data_[i].value = value;
                data_[i].used = true;
                size_++;
                return true;
            }
        }
        return false;  // Map is full
    }

    Value* find(const Key& key) {
        for (size_t i = 0; i < MaxSize; ++i) {
            if (data_[i].used && data_[i].key == key) {
                return &data_[i].value;
            }
        }
        return nullptr;
    }

    bool erase(const Key& key) {
        for (size_t i = 0; i < MaxSize; ++i) {
            if (data_[i].used && data_[i].key == key) {
                data_[i].used = false;
                size_--;
                return true;
            }
        }
        return false;
    }

    size_t size() const { return size_; }
    bool empty() const { return size_ == 0; }
};

// Template inheritance
template<typename T>
class Stack : public Container<T> {
public:
    explicit Stack(size_t capacity = 10) : Container<T>(capacity) {}

    void push(const T& item) {
        this->push_back(item);  // Use base class method
    }

    T pop() {
        if (this->empty()) {
            throw std::runtime_error("Stack is empty");
        }
        T item = (*this)[this->size() - 1];
        // Manually decrement size (accessing private member through protected method)
        const_cast<Container<T>*>(static_cast<const Container<T>*>(this))->clear();
        for (size_t i = 0; i < this->size() - 1; ++i) {
            this->push_back((*this)[i]);
        }
        return item;
    }

    const T& top() const {
        if (this->empty()) {
            throw std::runtime_error("Stack is empty");
        }
        return (*this)[this->size() - 1];
    }
};

// Partial template specialization
template<typename T>
class Container<T*> {
private:
    std::vector<T*> data_;

public:
    void push_back(T* item) {
        data_.push_back(item);
    }

    T*& operator[](size_t index) { return data_[index]; }
    T* const& operator[](size_t index) const { return data_[index]; }

    size_t size() const { return data_.size(); }
    bool empty() const { return data_.empty(); }
    void clear() { data_.clear(); }

    // Special methods for pointer specialization
    void cleanup() {
        for (T* ptr : data_) {
            delete ptr;
        }
        clear();
    }

    bool contains_null() const {
        for (T* ptr : data_) {
            if (ptr == nullptr) return true;
        }
        return false;
    }
};

void demonstrateClassTemplates() {
    // Basic template usage
    Container<int> intContainer(5);
    intContainer.push_back(1);
    intContainer.push_back(2);
    intContainer.push_back(3);

    std::cout << "Int container size: " << intContainer.size() << std::endl;
    for (size_t i = 0; i < intContainer.size(); ++i) {
        std::cout << intContainer[i] << " ";
    }
    std::cout << std::endl;

    // String template
    Container<std::string> stringContainer;
    stringContainer.push_back("Hello");
    stringContainer.push_back("World");
    stringContainer.push_back("Templates");

    std::cout << "String container size: " << stringContainer.size() << std::endl;
    for (size_t i = 0; i < stringContainer.size(); ++i) {
        std::cout << stringContainer[i] << " ";
    }
    std::cout << std::endl;

    // Bool specialization
    Container<bool> boolContainer;
    boolContainer.push_back(true);
    boolContainer.push_back(false);
    boolContainer.push_back(true);
    boolContainer.push_back(true);

    std::cout << "Bool container size: " << boolContainer.size() << std::endl;
    std::cout << "True count: " << boolContainer.count_true() << std::endl;

    // Multiple template parameters
    SimpleMap<std::string, int, 50> nameAges;
    nameAges.insert("Alice", 30);
    nameAges.insert("Bob", 25);
    nameAges.insert("Charlie", 35);

    int* aliceAge = nameAges.find("Alice");
    if (aliceAge) {
        std::cout << "Alice's age: " << *aliceAge << std::endl;
    }

    // Template inheritance
    Stack<double> doubleStack;
    doubleStack.push(1.1);
    doubleStack.push(2.2);
    doubleStack.push(3.3);

    std::cout << "Stack top: " << doubleStack.top() << std::endl;
    double popped = doubleStack.pop();
    std::cout << "Popped: " << popped << std::endl;
    std::cout << "New top: " << doubleStack.top() << std::endl;

    // Pointer specialization
    Container<int*> ptrContainer;
    ptrContainer.push_back(new int(42));
    ptrContainer.push_back(new int(84));
    ptrContainer.push_back(nullptr);

    std::cout << "Pointer container size: " << ptrContainer.size() << std::endl;
    std::cout << "Contains null: " << (ptrContainer.contains_null() ? "yes" : "no") << std::endl;

    // Access values through pointers
    for (size_t i = 0; i < ptrContainer.size(); ++i) {
        int* ptr = ptrContainer[i];
        if (ptr) {
            std::cout << "Value at index " << i << ": " << *ptr << std::endl;
        } else {
            std::cout << "Null pointer at index " << i << std::endl;
        }
    }

    // Cleanup
    ptrContainer.cleanup();
}

// Template template parameters
template<template<typename> class ContainerType, typename T>
class ContainerWrapper {
private:
    ContainerType<T> container_;

public:
    void add(const T& item) {
        container_.push_back(item);
    }

    size_t size() const {
        return container_.size();
    }

    bool empty() const {
        return container_.empty();
    }

    T& get(size_t index) {
        return container_[index];
    }

    const T& get(size_t index) const {
        return container_[index];
    }

    void process_all() {
        for (size_t i = 0; i < container_.size(); ++i) {
            // Process each item
            std::cout << "Processing item " << i << ": " << container_[i] << std::endl;
        }
    }
};

void demonstrateTemplateTemplateParameters() {
    // Use our Container template as the template parameter
    ContainerWrapper<Container, int> wrapper;
    wrapper.add(10);
    wrapper.add(20);
    wrapper.add(30);

    std::cout << "Wrapper size: " << wrapper.size() << std::endl;
    wrapper.process_all();

    // Could also use std::vector if it had the same interface
    // ContainerWrapper<std::vector, std::string> stringWrapper;
}
""",
    )

    run_updater(cpp_templates_project, mock_ingestor)

    project_name = cpp_templates_project.name

    expected_classes = [
        f"{project_name}.class_templates.Container",
        f"{project_name}.class_templates.SimpleMap",
        f"{project_name}.class_templates.Stack",
        f"{project_name}.class_templates.ContainerWrapper",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )

    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    stack_inheritance = any(
        "Stack" in call[0][0][2] and "Container" in call[0][2][2]
        for call in relationship_calls
    )
    assert stack_inheritance, "Expected inheritance relationship Stack -> Container"


def test_template_metaprogramming(
    cpp_templates_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test advanced template metaprogramming patterns."""
    test_file = cpp_templates_project / "template_metaprogramming.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <type_traits>
#include <string>

// Compile-time recursion - Fibonacci
template<int N>
struct Fibonacci {
    static constexpr int value = Fibonacci<N-1>::value + Fibonacci<N-2>::value;
};

template<>
struct Fibonacci<0> {
    static constexpr int value = 0;
};

template<>
struct Fibonacci<1> {
    static constexpr int value = 1;
};

// Type manipulation - Remove pointer
template<typename T>
struct RemovePointer {
    using type = T;
};

template<typename T>
struct RemovePointer<T*> {
    using type = T;
};

template<typename T>
using RemovePointer_t = typename RemovePointer<T>::type;

// SFINAE and enable_if patterns
template<typename T>
struct IsPointer {
    static constexpr bool value = false;
};

template<typename T>
struct IsPointer<T*> {
    static constexpr bool value = true;
};

// Conditional compilation based on type traits
template<typename T>
typename std::enable_if_t<std::is_integral_v<T>, std::string>
describeType(T) {
    return "This is an integral type";
}

template<typename T>
typename std::enable_if_t<std::is_floating_point_v<T>, std::string>
describeType(T) {
    return "This is a floating point type";
}

template<typename T>
typename std::enable_if_t<std::is_pointer_v<T>, std::string>
describeType(T) {
    return "This is a pointer type";
}

// Variadic templates for type lists
template<typename... Types>
struct TypeList {
    static constexpr size_t size = sizeof...(Types);
};

template<typename T, typename... Rest>
struct TypeList<T, Rest...> {
    using head = T;
    using tail = TypeList<Rest...>;
    static constexpr size_t size = 1 + sizeof...(Rest);
};

// Template for checking if type is in list
template<typename T, typename... Types>
struct Contains;

template<typename T>
struct Contains<T> {
    static constexpr bool value = false;
};

template<typename T, typename First, typename... Rest>
struct Contains<T, First, Rest...> {
    static constexpr bool value = std::is_same_v<T, First> || Contains<T, Rest...>::value;
};

// Advanced: Template-based visitor pattern
template<typename... Types>
class Variant {
private:
    union Storage {
        // Empty constructor for union
        Storage() {}
        ~Storage() {}
        // Add all types to union...
        char dummy;  // Simplified for example
    } storage_;

    size_t type_index_;

public:
    template<typename T>
    Variant(T&& value) {
        static_assert(Contains<std::decay_t<T>, Types...>::value, "Type not in variant");
        // Implementation would store the value and set type_index_
        type_index_ = 0;  // Simplified
    }

    template<typename Visitor>
    auto visit(Visitor&& visitor) -> decltype(visitor(std::declval<typename TypeList<Types...>::head>())) {
        // Implementation would dispatch to correct type
        // This is a simplified version
        if (type_index_ == 0) {
            // Return visitor with first type
        }
        // More implementation needed...
        return visitor(typename TypeList<Types...>::head{});
    }
};

// Template for compile-time string manipulation
template<size_t N>
struct ConstString {
    char data[N];

    constexpr ConstString(const char (&str)[N]) {
        for (size_t i = 0; i < N; ++i) {
            data[i] = str[i];
        }
    }

    constexpr size_t length() const {
        return N - 1;  // Exclude null terminator
    }
};

template<ConstString str>
void printConstString() {
    std::cout << "Compile-time string: " << str.data << " (length: " << str.length() << ")" << std::endl;
}

// Perfect forwarding
template<typename T>
class Wrapper {
private:
    T value_;

public:
    template<typename U>
    Wrapper(U&& value) : value_(std::forward<U>(value)) {}

    T& get() { return value_; }
    const T& get() const { return value_; }

    // Perfect forwarding method
    template<typename Func, typename... Args>
    auto callWith(Func&& func, Args&&... args) -> decltype(func(value_, std::forward<Args>(args)...)) {
        return func(value_, std::forward<Args>(args)...);
    }
};

// CRTP (Curiously Recurring Template Pattern)
template<typename Derived>
class Counter {
private:
    static int count_;

public:
    Counter() { ++count_; }
    Counter(const Counter&) { ++count_; }
    ~Counter() { --count_; }

    static int getCount() { return count_; }

    // Interface that derived class must implement
    void interface_method() {
        static_cast<Derived*>(this)->implementation();
    }
};

template<typename Derived>
int Counter<Derived>::count_ = 0;

class MyClass : public Counter<MyClass> {
public:
    void implementation() {
        std::cout << "MyClass implementation called" << std::endl;
    }
};

class AnotherClass : public Counter<AnotherClass> {
public:
    void implementation() {
        std::cout << "AnotherClass implementation called" << std::endl;
    }
};

void demonstrateTemplateMetaprogramming() {
    // Compile-time computations
    constexpr int fib10 = Fibonacci<10>::value;
    constexpr int fib15 = Fibonacci<15>::value;

    std::cout << "Fibonacci(10) = " << fib10 << std::endl;
    std::cout << "Fibonacci(15) = " << fib15 << std::endl;

    // Type manipulation
    using IntPtr = int*;
    using Int = RemovePointer_t<IntPtr>;
    static_assert(std::is_same_v<Int, int>, "RemovePointer failed");

    std::cout << "IsPointer<int>::value = " << IsPointer<int>::value << std::endl;
    std::cout << "IsPointer<int*>::value = " << IsPointer<int*>::value << std::endl;

    // SFINAE
    int i = 42;
    double d = 3.14;
    int* p = &i;

    std::cout << describeType(i) << std::endl;
    std::cout << describeType(d) << std::endl;
    std::cout << describeType(p) << std::endl;

    // Type lists
    using MyTypes = TypeList<int, double, std::string>;
    std::cout << "TypeList size: " << MyTypes::size << std::endl;

    constexpr bool hasInt = Contains<int, int, double, std::string>::value;
    constexpr bool hasFloat = Contains<float, int, double, std::string>::value;

    std::cout << "Contains int: " << hasInt << std::endl;
    std::cout << "Contains float: " << hasFloat << std::endl;

    // Variant (simplified example)
    Variant<int, double, std::string> var(42);
    auto result = var.visit([](const auto& value) {
        std::cout << "Visiting variant with value" << std::endl;
        return 0;
    });

    // Compile-time string
    printConstString<"Hello, World!">();

    // Perfect forwarding
    Wrapper<std::string> stringWrapper("Hello");
    std::cout << "Wrapped string: " << stringWrapper.get() << std::endl;

    stringWrapper.callWith([](const std::string& str, const std::string& suffix) {
        std::cout << "Called with: " << str << suffix << std::endl;
        return str + suffix;
    }, " World!");

    // CRTP
    MyClass obj1;
    MyClass obj2;
    AnotherClass obj3;

    std::cout << "MyClass count: " << MyClass::getCount() << std::endl;
    std::cout << "AnotherClass count: " << AnotherClass::getCount() << std::endl;

    obj1.interface_method();
    obj3.interface_method();
}

// Template concepts (C++20 style)
template<typename T>
concept Incrementable = requires(T t) {
    ++t;
    t++;
};

template<typename T>
concept HasSize = requires(T t) {
    t.size();
};

template<Incrementable T>
void increment_twice(T& value) {
    ++value;
    ++value;
}

template<HasSize T>
void print_size(const T& container) {
    std::cout << "Size: " << container.size() << std::endl;
}

void demonstrateConcepts() {
    int number = 5;
    increment_twice(number);
    std::cout << "Incremented number: " << number << std::endl;

    std::string str = "Hello";
    print_size(str);

    // increment_twice(str);  // Would fail to compile - string doesn't satisfy Incrementable
}
""",
    )

    run_updater(cpp_templates_project, mock_ingestor)

    project_name = cpp_templates_project.name

    expected_classes = [
        f"{project_name}.template_metaprogramming.Fibonacci",
        f"{project_name}.template_metaprogramming.RemovePointer",
        f"{project_name}.template_metaprogramming.IsPointer",
        f"{project_name}.template_metaprogramming.TypeList",
        f"{project_name}.template_metaprogramming.Contains",
        f"{project_name}.template_metaprogramming.Variant",
        f"{project_name}.template_metaprogramming.Wrapper",
        f"{project_name}.template_metaprogramming.Counter",
        f"{project_name}.template_metaprogramming.MyClass",
        f"{project_name}.template_metaprogramming.AnotherClass",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )

    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    crtp_inheritance = [
        call
        for call in relationship_calls
        if ("MyClass" in call[0][0][2] and "Counter" in call[0][2][2])
        or ("AnotherClass" in call[0][0][2] and "Counter" in call[0][2][2])
    ]

    assert len(crtp_inheritance) >= 1, (
        f"Expected CRTP inheritance relationships, found {len(crtp_inheritance)}"
    )


def test_cpp_templates_comprehensive(
    cpp_templates_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all template patterns create proper relationships."""
    test_file = cpp_templates_project / "comprehensive_templates.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every C++ template pattern in one file
#include <iostream>
#include <vector>
#include <string>
#include <memory>

// Function templates
template<typename T>
T max_value(T a, T b) {
    return (a > b) ? a : b;
}

template<typename T, size_t N>
void print_array(const T (&arr)[N]) {
    for (size_t i = 0; i < N; ++i) {
        std::cout << arr[i] << " ";
    }
    std::cout << std::endl;
}

// Class templates
template<typename T>
class Vector {
private:
    T* data_;
    size_t size_;
    size_t capacity_;

public:
    Vector() : data_(nullptr), size_(0), capacity_(0) {}

    ~Vector() { delete[] data_; }

    void push_back(const T& item) {
        if (size_ >= capacity_) {
            resize(capacity_ == 0 ? 1 : capacity_ * 2);
        }
        data_[size_++] = item;
    }

    T& operator[](size_t index) { return data_[index]; }
    size_t size() const { return size_; }

private:
    void resize(size_t new_capacity) {
        T* new_data = new T[new_capacity];
        for (size_t i = 0; i < size_; ++i) {
            new_data[i] = data_[i];
        }
        delete[] data_;
        data_ = new_data;
        capacity_ = new_capacity;
    }
};

// Template specialization
template<>
class Vector<bool> {
private:
    std::vector<bool> data_;

public:
    void push_back(bool item) { data_.push_back(item); }
    bool operator[](size_t index) const { return data_[index]; }
    size_t size() const { return data_.size(); }
};

// Template inheritance
template<typename T>
class Stack : public Vector<T> {
public:
    void push(const T& item) {
        this->push_back(item);
    }

    T pop() {
        T item = (*this)[this->size() - 1];
        // Simplified pop implementation
        return item;
    }
};

// Multiple template parameters
template<typename Key, typename Value>
class Pair {
public:
    Key first;
    Value second;

    Pair(const Key& k, const Value& v) : first(k), second(v) {}
};

// Template metaprogramming
template<int N>
struct Power2 {
    static constexpr int value = 2 * Power2<N-1>::value;
};

template<>
struct Power2<0> {
    static constexpr int value = 1;
};

// CRTP
template<typename Derived>
class Base {
public:
    void interface() {
        static_cast<Derived*>(this)->implementation();
    }
};

class Concrete : public Base<Concrete> {
public:
    void implementation() {
        std::cout << "Concrete implementation" << std::endl;
    }
};

// Variadic templates
template<typename... Args>
void print_all(Args... args) {
    ((std::cout << args << " "), ...);
    std::cout << std::endl;
}

void demonstrateComprehensiveTemplates() {
    // Function template usage
    int max_int = max_value(10, 20);
    double max_double = max_value(3.14, 2.71);

    int arr[] = {1, 2, 3, 4, 5};
    print_array(arr);

    // Class template usage
    Vector<int> int_vec;
    int_vec.push_back(1);
    int_vec.push_back(2);
    int_vec.push_back(3);

    Vector<std::string> string_vec;
    string_vec.push_back("Hello");
    string_vec.push_back("World");

    // Specialization
    Vector<bool> bool_vec;
    bool_vec.push_back(true);
    bool_vec.push_back(false);

    // Template inheritance
    Stack<double> double_stack;
    double_stack.push(1.1);
    double_stack.push(2.2);
    double d = double_stack.pop();

    // Multiple parameters
    Pair<std::string, int> name_age("Alice", 30);

    // Metaprogramming
    constexpr int power8 = Power2<8>::value;

    // CRTP
    Concrete concrete;
    concrete.interface();

    // Variadic templates
    print_all("Hello", 42, 3.14, "World");

    std::cout << "Max int: " << max_int << std::endl;
    std::cout << "Max double: " << max_double << std::endl;
    std::cout << "Int vector size: " << int_vec.size() << std::endl;
    std::cout << "Bool vector size: " << bool_vec.size() << std::endl;
    std::cout << "Pair: " << name_age.first << ", " << name_age.second << std::endl;
    std::cout << "2^8 = " << power8 << std::endl;
}
""",
    )

    run_updater(cpp_templates_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_templates" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 10, (
        f"Expected at least 10 comprehensive template calls, found {len(comprehensive_calls)}"
    )

    template_inheritance = [
        call
        for call in inherits_relationships
        if "comprehensive_templates" in call.args[0][2]
    ]

    assert len(template_inheritance) >= 2, (
        f"Expected at least 2 template inheritance relationships, found {len(template_inheritance)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
