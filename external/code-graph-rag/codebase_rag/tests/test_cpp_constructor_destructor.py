from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_constructor_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with constructor/destructor patterns."""
    project_path = temp_repo / "cpp_constructor_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_basic_constructors_destructors(
    cpp_constructor_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic constructor and destructor patterns."""
    test_file = cpp_constructor_project / "basic_constructors.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <memory>

// Basic class with all fundamental constructor types
class BasicResource {
private:
    std::string name_;
    int* data_;
    size_t size_;

public:
    // Default constructor
    BasicResource() : name_("default"), data_(nullptr), size_(0) {
        std::cout << "Default constructor called for " << name_ << std::endl;
    }

    // Parameterized constructor
    BasicResource(const std::string& name, size_t size)
        : name_(name), size_(size), data_(new int[size]) {
        std::cout << "Parameterized constructor called for " << name_ << std::endl;
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = static_cast<int>(i);
        }
    }

    // Copy constructor
    BasicResource(const BasicResource& other)
        : name_(other.name_ + "_copy"), size_(other.size_) {
        std::cout << "Copy constructor called for " << name_ << std::endl;
        if (other.data_ && size_ > 0) {
            data_ = new int[size_];
            for (size_t i = 0; i < size_; ++i) {
                data_[i] = other.data_[i];
            }
        } else {
            data_ = nullptr;
        }
    }

    // Move constructor
    BasicResource(BasicResource&& other) noexcept
        : name_(std::move(other.name_)), data_(other.data_), size_(other.size_) {
        std::cout << "Move constructor called for " << name_ << std::endl;
        other.data_ = nullptr;
        other.size_ = 0;
    }

    // Copy assignment operator
    BasicResource& operator=(const BasicResource& other) {
        std::cout << "Copy assignment called for " << name_ << std::endl;
        if (this != &other) {
            // Clean up existing resources
            delete[] data_;

            // Copy new data
            name_ = other.name_ + "_assigned";
            size_ = other.size_;
            if (other.data_ && size_ > 0) {
                data_ = new int[size_];
                for (size_t i = 0; i < size_; ++i) {
                    data_[i] = other.data_[i];
                }
            } else {
                data_ = nullptr;
            }
        }
        return *this;
    }

    // Move assignment operator
    BasicResource& operator=(BasicResource&& other) noexcept {
        std::cout << "Move assignment called for " << name_ << std::endl;
        if (this != &other) {
            // Clean up existing resources
            delete[] data_;

            // Move from other
            name_ = std::move(other.name_);
            data_ = other.data_;
            size_ = other.size_;

            // Reset other
            other.data_ = nullptr;
            other.size_ = 0;
        }
        return *this;
    }

    // Destructor
    ~BasicResource() {
        std::cout << "Destructor called for " << name_ << std::endl;
        delete[] data_;
        data_ = nullptr;
    }

    // Utility methods
    const std::string& getName() const { return name_; }
    size_t getSize() const { return size_; }

    void print() const {
        std::cout << "Resource " << name_ << " (size: " << size_ << "): ";
        if (data_) {
            for (size_t i = 0; i < size_; ++i) {
                std::cout << data_[i] << " ";
            }
        }
        std::cout << std::endl;
    }
};

// Test different constructor invocations
void testBasicConstructors() {
    std::cout << "=== Testing Basic Constructors ===" << std::endl;

    // Default constructor
    BasicResource default_resource;
    default_resource.print();

    // Parameterized constructor
    BasicResource param_resource("param", 5);
    param_resource.print();

    // Copy constructor
    BasicResource copied_resource = param_resource;  // Copy constructor
    copied_resource.print();

    // Copy constructor (explicit)
    BasicResource another_copy(param_resource);
    another_copy.print();

    // Move constructor
    BasicResource moved_resource = std::move(param_resource);  // Move constructor
    moved_resource.print();
    param_resource.print();  // Should be in moved-from state

    // Copy assignment
    BasicResource assigned_resource;
    assigned_resource = copied_resource;  // Copy assignment
    assigned_resource.print();

    // Move assignment
    BasicResource move_assigned;
    move_assigned = std::move(another_copy);  // Move assignment
    move_assigned.print();
    another_copy.print();  // Should be in moved-from state

    std::cout << "Exiting testBasicConstructors scope..." << std::endl;
}  // All objects destroyed here, demonstrating RAII

// Class demonstrating constructor initialization lists
class InitializationDemo {
private:
    const int constant_value_;
    int& reference_member_;
    std::string name_;
    BasicResource resource_;

    static int next_id_;
    int id_;

public:
    // Constructor with initialization list
    InitializationDemo(int value, int& ref, const std::string& name)
        : constant_value_(value),           // Must be initialized in init list (const)
          reference_member_(ref),           // Must be initialized in init list (reference)
          name_(name),                      // Efficient initialization
          resource_(name + "_internal", 3), // Calls parameterized constructor
          id_(++next_id_)                   // Static member access
    {
        std::cout << "InitializationDemo constructor: " << name_
                  << " (id: " << id_ << ")" << std::endl;
    }

    // Delegating constructor (C++11)
    InitializationDemo(const std::string& name)
        : InitializationDemo(42, getStaticRef(), name)  // Delegates to main constructor
    {
        std::cout << "Delegating constructor completed for " << name_ << std::endl;
    }

    ~InitializationDemo() {
        std::cout << "InitializationDemo destructor: " << name_
                  << " (id: " << id_ << ")" << std::endl;
    }

    void display() const {
        std::cout << "InitializationDemo " << name_ << " (id: " << id_
                  << ", const: " << constant_value_
                  << ", ref: " << reference_member_ << ")" << std::endl;
        resource_.print();
    }

private:
    static int& getStaticRef() {
        static int static_value = 100;
        return static_value;
    }
};

int InitializationDemo::next_id_ = 0;

void testInitializationLists() {
    std::cout << "=== Testing Initialization Lists ===" << std::endl;

    int external_value = 200;

    // Main constructor
    InitializationDemo demo1(10, external_value, "demo1");
    demo1.display();

    // Delegating constructor
    InitializationDemo demo2("demo2");
    demo2.display();

    external_value = 300;  // Change referenced value
    demo1.display();  // Should show updated reference value
}

// Class demonstrating explicit constructors
class ExplicitDemo {
private:
    int value_;
    std::string description_;

public:
    // Explicit constructor prevents implicit conversions
    explicit ExplicitDemo(int value)
        : value_(value), description_("from int") {
        std::cout << "Explicit constructor (int): " << value_ << std::endl;
    }

    // Non-explicit constructor allows implicit conversions
    ExplicitDemo(const std::string& desc)
        : value_(0), description_(desc) {
        std::cout << "Non-explicit constructor (string): " << description_ << std::endl;
    }

    // Multi-parameter constructor can be explicit
    explicit ExplicitDemo(int value, const std::string& desc)
        : value_(value), description_(desc) {
        std::cout << "Explicit multi-param constructor: " << value_
                  << ", " << description_ << std::endl;
    }

    void display() const {
        std::cout << "ExplicitDemo: " << value_ << " - " << description_ << std::endl;
    }
};

void acceptExplicitDemo(const ExplicitDemo& demo) {
    demo.display();
}

void testExplicitConstructors() {
    std::cout << "=== Testing Explicit Constructors ===" << std::endl;

    // Direct initialization
    ExplicitDemo demo1(42);
    demo1.display();

    // Implicit conversion allowed for non-explicit constructor
    ExplicitDemo demo2 = std::string("implicit");  // Calls string constructor
    demo2.display();

    // Function call with implicit conversion
    acceptExplicitDemo("function_call");  // Implicit conversion from string

    // ExplicitDemo demo3 = 100;  // ERROR: explicit constructor prevents this
    // acceptExplicitDemo(200);   // ERROR: explicit constructor prevents this

    // These work with explicit conversion
    ExplicitDemo demo4 = ExplicitDemo(100);  // Explicit conversion
    acceptExplicitDemo(ExplicitDemo(300));   // Explicit conversion
}

void demonstrateBasicConstructorsDestructors() {
    testBasicConstructors();
    testInitializationLists();
    testExplicitConstructors();
}
""",
    )

    run_updater(cpp_constructor_project, mock_ingestor)

    project_name = cpp_constructor_project.name

    expected_classes = [
        f"{project_name}.basic_constructors.BasicResource",
        f"{project_name}.basic_constructors.InitializationDemo",
        f"{project_name}.basic_constructors.ExplicitDemo",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )

    expected_functions = [
        f"{project_name}.basic_constructors.testBasicConstructors",
        f"{project_name}.basic_constructors.testInitializationLists",
        f"{project_name}.basic_constructors.testExplicitConstructors",
        f"{project_name}.basic_constructors.demonstrateBasicConstructorsDestructors",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_raii_patterns(
    cpp_constructor_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test RAII (Resource Acquisition Is Initialization) patterns."""
    test_file = cpp_constructor_project / "raii_patterns.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <memory>
#include <fstream>
#include <mutex>
#include <vector>

// RAII class for file operations
class FileManager {
private:
    std::string filename_;
    std::unique_ptr<std::ofstream> file_;
    bool is_open_;

public:
    explicit FileManager(const std::string& filename)
        : filename_(filename), is_open_(false) {
        std::cout << "FileManager constructor: " << filename_ << std::endl;
        file_ = std::make_unique<std::ofstream>(filename_);
        if (file_->is_open()) {
            is_open_ = true;
            std::cout << "File " << filename_ << " opened successfully" << std::endl;
        } else {
            std::cout << "Failed to open file " << filename_ << std::endl;
        }
    }

    ~FileManager() {
        std::cout << "FileManager destructor: " << filename_ << std::endl;
        if (is_open_ && file_) {
            file_->close();
            std::cout << "File " << filename_ << " closed" << std::endl;
        }
    }

    // Move constructor for transferring ownership
    FileManager(FileManager&& other) noexcept
        : filename_(std::move(other.filename_)),
          file_(std::move(other.file_)),
          is_open_(other.is_open_) {
        std::cout << "FileManager move constructor" << std::endl;
        other.is_open_ = false;
    }

    // Move assignment
    FileManager& operator=(FileManager&& other) noexcept {
        if (this != &other) {
            // Clean up current resources
            if (is_open_ && file_) {
                file_->close();
            }

            // Move from other
            filename_ = std::move(other.filename_);
            file_ = std::move(other.file_);
            is_open_ = other.is_open_;

            other.is_open_ = false;
            std::cout << "FileManager move assignment" << std::endl;
        }
        return *this;
    }

    // Delete copy operations to prevent resource duplication
    FileManager(const FileManager&) = delete;
    FileManager& operator=(const FileManager&) = delete;

    void write(const std::string& content) {
        if (is_open_ && file_) {
            *file_ << content << std::endl;
            file_->flush();
        }
    }

    bool isOpen() const { return is_open_; }
    const std::string& getFilename() const { return filename_; }
};

// RAII class for memory buffer management
class BufferManager {
private:
    char* buffer_;
    size_t size_;
    size_t capacity_;

public:
    explicit BufferManager(size_t initial_capacity = 1024)
        : size_(0), capacity_(initial_capacity) {
        std::cout << "BufferManager constructor (capacity: " << capacity_ << ")" << std::endl;
        buffer_ = new char[capacity_];
        std::fill(buffer_, buffer_ + capacity_, 0);
    }

    ~BufferManager() {
        std::cout << "BufferManager destructor (capacity: " << capacity_ << ")" << std::endl;
        delete[] buffer_;
        buffer_ = nullptr;
    }

    // Move constructor
    BufferManager(BufferManager&& other) noexcept
        : buffer_(other.buffer_), size_(other.size_), capacity_(other.capacity_) {
        std::cout << "BufferManager move constructor" << std::endl;
        other.buffer_ = nullptr;
        other.size_ = 0;
        other.capacity_ = 0;
    }

    // Move assignment
    BufferManager& operator=(BufferManager&& other) noexcept {
        if (this != &other) {
            delete[] buffer_;

            buffer_ = other.buffer_;
            size_ = other.size_;
            capacity_ = other.capacity_;

            other.buffer_ = nullptr;
            other.size_ = 0;
            other.capacity_ = 0;
            std::cout << "BufferManager move assignment" << std::endl;
        }
        return *this;
    }

    // Delete copy operations
    BufferManager(const BufferManager&) = delete;
    BufferManager& operator=(const BufferManager&) = delete;

    void append(const std::string& data) {
        if (size_ + data.length() >= capacity_) {
            resize(capacity_ * 2);
        }
        std::copy(data.begin(), data.end(), buffer_ + size_);
        size_ += data.length();
    }

    const char* data() const { return buffer_; }
    size_t size() const { return size_; }
    size_t capacity() const { return capacity_; }

private:
    void resize(size_t new_capacity) {
        std::cout << "BufferManager resizing from " << capacity_
                  << " to " << new_capacity << std::endl;
        char* new_buffer = new char[new_capacity];
        std::copy(buffer_, buffer_ + size_, new_buffer);
        std::fill(new_buffer + size_, new_buffer + new_capacity, 0);

        delete[] buffer_;
        buffer_ = new_buffer;
        capacity_ = new_capacity;
    }
};

// RAII class for scoped locking
class ScopedLock {
private:
    std::mutex& mutex_;
    bool locked_;

public:
    explicit ScopedLock(std::mutex& m) : mutex_(m), locked_(false) {
        std::cout << "ScopedLock constructor - acquiring lock" << std::endl;
        mutex_.lock();
        locked_ = true;
    }

    ~ScopedLock() {
        std::cout << "ScopedLock destructor - releasing lock" << std::endl;
        if (locked_) {
            mutex_.unlock();
        }
    }

    // Delete copy operations to prevent double unlocking
    ScopedLock(const ScopedLock&) = delete;
    ScopedLock& operator=(const ScopedLock&) = delete;

    // Move operations for transferring lock ownership
    ScopedLock(ScopedLock&& other) noexcept
        : mutex_(other.mutex_), locked_(other.locked_) {
        other.locked_ = false;
        std::cout << "ScopedLock move constructor" << std::endl;
    }

    ScopedLock& operator=(ScopedLock&& other) noexcept {
        if (this != &other) {
            if (locked_) {
                mutex_.unlock();
            }
            mutex_ = other.mutex_;
            locked_ = other.locked_;
            other.locked_ = false;
            std::cout << "ScopedLock move assignment" << std::endl;
        }
        return *this;
    }
};

// Factory functions demonstrating RAII
std::unique_ptr<FileManager> createFileManager(const std::string& filename) {
    return std::make_unique<FileManager>(filename);
}

std::unique_ptr<BufferManager> createBufferManager(size_t capacity) {
    return std::make_unique<BufferManager>(capacity);
}

void testRAIIPatterns() {
    std::cout << "=== Testing RAII Patterns ===" << std::endl;

    // File management with RAII
    {
        std::cout << "--- File RAII scope start ---" << std::endl;
        FileManager file_mgr("test_raii.txt");
        if (file_mgr.isOpen()) {
            file_mgr.write("RAII test content");
            file_mgr.write("Second line");
        }
        std::cout << "--- File RAII scope end ---" << std::endl;
    }  // FileManager destructor called here, file automatically closed

    // Memory buffer management with RAII
    {
        std::cout << "--- Buffer RAII scope start ---" << std::endl;
        BufferManager buffer(512);
        buffer.append("Hello, ");
        buffer.append("RAII ");
        buffer.append("World!");
        std::cout << "Buffer content: " << std::string(buffer.data(), buffer.size()) << std::endl;
        std::cout << "--- Buffer RAII scope end ---" << std::endl;
    }  // BufferManager destructor called here, memory automatically freed

    // Mutex locking with RAII
    std::mutex test_mutex;
    {
        std::cout << "--- Lock RAII scope start ---" << std::endl;
        ScopedLock lock(test_mutex);
        std::cout << "Critical section - mutex is locked" << std::endl;
        // Simulate some work
        std::cout << "--- Lock RAII scope end ---" << std::endl;
    }  // ScopedLock destructor called here, mutex automatically unlocked

    // Factory functions with RAII
    auto file_ptr = createFileManager("factory_test.txt");
    auto buffer_ptr = createBufferManager(2048);

    if (file_ptr->isOpen()) {
        file_ptr->write("Created via factory");
    }
    buffer_ptr->append("Factory-created buffer");

    std::cout << "Factory objects will be destroyed when unique_ptrs go out of scope" << std::endl;
}

// Exception safety with RAII
class ExceptionSafeResource {
private:
    std::string name_;
    int* data_;
    size_t size_;

public:
    ExceptionSafeResource(const std::string& name, size_t size)
        : name_(name), size_(size) {
        std::cout << "ExceptionSafeResource constructor: " << name_ << std::endl;

        // This could throw an exception
        data_ = new int[size_];

        // Initialize data - this could also throw
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = static_cast<int>(i * i);
        }

        std::cout << "ExceptionSafeResource successfully constructed: " << name_ << std::endl;
    }

    ~ExceptionSafeResource() {
        std::cout << "ExceptionSafeResource destructor: " << name_ << std::endl;
        delete[] data_;
    }

    // Exception-safe copy constructor
    ExceptionSafeResource(const ExceptionSafeResource& other)
        : name_(other.name_ + "_copy"), size_(other.size_) {
        std::cout << "ExceptionSafeResource copy constructor: " << name_ << std::endl;

        // Allocate memory first
        data_ = new int[size_];  // Could throw

        try {
            // Copy data
            for (size_t i = 0; i < size_; ++i) {
                data_[i] = other.data_[i];  // Could throw
            }
        } catch (...) {
            // Clean up on exception
            delete[] data_;
            throw;  // Re-throw the exception
        }
    }

    // Exception-safe assignment operator (copy-and-swap idiom)
    ExceptionSafeResource& operator=(const ExceptionSafeResource& other) {
        std::cout << "ExceptionSafeResource assignment: " << name_ << std::endl;

        if (this != &other) {
            // Create temporary copy (could throw, but our state is unchanged)
            ExceptionSafeResource temp(other);

            // Swap with temporary (no-throw operations)
            std::swap(name_, temp.name_);
            std::swap(data_, temp.data_);
            std::swap(size_, temp.size_);
        }

        return *this;
    }

    void display() const {
        std::cout << "ExceptionSafeResource " << name_ << " (size: " << size_ << "): ";
        for (size_t i = 0; i < std::min(size_, size_t(5)); ++i) {
            std::cout << data_[i] << " ";
        }
        if (size_ > 5) std::cout << "...";
        std::cout << std::endl;
    }
};

void testExceptionSafety() {
    std::cout << "=== Testing Exception Safety with RAII ===" << std::endl;

    try {
        ExceptionSafeResource resource1("safe1", 10);
        resource1.display();

        ExceptionSafeResource resource2 = resource1;  // Copy constructor
        resource2.display();

        ExceptionSafeResource resource3("safe3", 5);
        resource3 = resource1;  // Assignment operator
        resource3.display();

    } catch (const std::exception& e) {
        std::cout << "Exception caught: " << e.what() << std::endl;
        // Even if exceptions occur, RAII ensures proper cleanup
    }
}

void demonstrateRAIIPatterns() {
    testRAIIPatterns();
    testExceptionSafety();
}
""",
    )

    run_updater(cpp_constructor_project, mock_ingestor)

    project_name = cpp_constructor_project.name

    expected_classes = [
        f"{project_name}.raii_patterns.FileManager",
        f"{project_name}.raii_patterns.BufferManager",
        f"{project_name}.raii_patterns.ScopedLock",
        f"{project_name}.raii_patterns.ExceptionSafeResource",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_special_member_functions(
    cpp_constructor_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Rule of Three, Rule of Five, and special member function generation."""
    test_file = cpp_constructor_project / "special_members.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <utility>
#include <memory>

// Rule of Three demonstration
class RuleOfThree {
private:
    int* data_;
    size_t size_;
    std::string name_;

public:
    // Constructor
    RuleOfThree(const std::string& name, size_t size)
        : name_(name), size_(size), data_(new int[size]) {
        std::cout << "RuleOfThree constructor: " << name_ << std::endl;
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = static_cast<int>(i);
        }
    }

    // 1. Destructor
    ~RuleOfThree() {
        std::cout << "RuleOfThree destructor: " << name_ << std::endl;
        delete[] data_;
    }

    // 2. Copy constructor
    RuleOfThree(const RuleOfThree& other)
        : name_(other.name_ + "_copy"), size_(other.size_), data_(new int[size_]) {
        std::cout << "RuleOfThree copy constructor: " << name_ << std::endl;
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = other.data_[i];
        }
    }

    // 3. Copy assignment operator
    RuleOfThree& operator=(const RuleOfThree& other) {
        std::cout << "RuleOfThree copy assignment: " << name_ << std::endl;
        if (this != &other) {
            // Clean up existing resources
            delete[] data_;

            // Copy from other
            name_ = other.name_ + "_assigned";
            size_ = other.size_;
            data_ = new int[size_];
            for (size_t i = 0; i < size_; ++i) {
                data_[i] = other.data_[i];
            }
        }
        return *this;
    }

    void display() const {
        std::cout << "RuleOfThree " << name_ << ": ";
        for (size_t i = 0; i < size_; ++i) {
            std::cout << data_[i] << " ";
        }
        std::cout << std::endl;
    }
};

// Rule of Five demonstration (extends Rule of Three with move semantics)
class RuleOfFive {
private:
    int* data_;
    size_t size_;
    std::string name_;

public:
    // Constructor
    RuleOfFive(const std::string& name, size_t size)
        : name_(name), size_(size), data_(new int[size]) {
        std::cout << "RuleOfFive constructor: " << name_ << std::endl;
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = static_cast<int>(i * 2);
        }
    }

    // 1. Destructor
    ~RuleOfFive() {
        std::cout << "RuleOfFive destructor: " << name_ << std::endl;
        delete[] data_;
    }

    // 2. Copy constructor
    RuleOfFive(const RuleOfFive& other)
        : name_(other.name_ + "_copy"), size_(other.size_), data_(new int[size_]) {
        std::cout << "RuleOfFive copy constructor: " << name_ << std::endl;
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = other.data_[i];
        }
    }

    // 3. Copy assignment operator
    RuleOfFive& operator=(const RuleOfFive& other) {
        std::cout << "RuleOfFive copy assignment: " << name_ << std::endl;
        if (this != &other) {
            delete[] data_;
            name_ = other.name_ + "_assigned";
            size_ = other.size_;
            data_ = new int[size_];
            for (size_t i = 0; i < size_; ++i) {
                data_[i] = other.data_[i];
            }
        }
        return *this;
    }

    // 4. Move constructor
    RuleOfFive(RuleOfFive&& other) noexcept
        : name_(std::move(other.name_)), size_(other.size_), data_(other.data_) {
        std::cout << "RuleOfFive move constructor: " << name_ << std::endl;
        other.data_ = nullptr;
        other.size_ = 0;
    }

    // 5. Move assignment operator
    RuleOfFive& operator=(RuleOfFive&& other) noexcept {
        std::cout << "RuleOfFive move assignment: " << name_ << std::endl;
        if (this != &other) {
            delete[] data_;
            name_ = std::move(other.name_);
            size_ = other.size_;
            data_ = other.data_;
            other.data_ = nullptr;
            other.size_ = 0;
        }
        return *this;
    }

    void display() const {
        std::cout << "RuleOfFive " << name_ << " (size: " << size_ << "): ";
        if (data_) {
            for (size_t i = 0; i < size_; ++i) {
                std::cout << data_[i] << " ";
            }
        } else {
            std::cout << "[moved-from state]";
        }
        std::cout << std::endl;
    }
};

// Rule of Zero demonstration (using smart pointers)
class RuleOfZero {
private:
    std::unique_ptr<int[]> data_;
    size_t size_;
    std::string name_;

public:
    RuleOfZero(const std::string& name, size_t size)
        : name_(name), size_(size), data_(std::make_unique<int[]>(size)) {
        std::cout << "RuleOfZero constructor: " << name_ << std::endl;
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = static_cast<int>(i * 3);
        }
    }

    // Compiler-generated special members work correctly with smart pointers
    // No need to explicitly define destructor, copy/move constructors/assignments

    void display() const {
        std::cout << "RuleOfZero " << name_ << ": ";
        for (size_t i = 0; i < size_; ++i) {
            std::cout << data_[i] << " ";
        }
        std::cout << std::endl;
    }

    // Custom copy constructor if deep copy is needed
    RuleOfZero(const RuleOfZero& other)
        : name_(other.name_ + "_copy"), size_(other.size_),
          data_(std::make_unique<int[]>(size_)) {
        std::cout << "RuleOfZero copy constructor: " << name_ << std::endl;
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = other.data_[i];
        }
    }

    // Custom copy assignment if deep copy is needed
    RuleOfZero& operator=(const RuleOfZero& other) {
        std::cout << "RuleOfZero copy assignment: " << name_ << std::endl;
        if (this != &other) {
            name_ = other.name_ + "_assigned";
            size_ = other.size_;
            data_ = std::make_unique<int[]>(size_);
            for (size_t i = 0; i < size_; ++i) {
                data_[i] = other.data_[i];
            }
        }
        return *this;
    }

    // Move operations are automatically generated and work correctly
};

// Class with deleted special members
class NonCopyable {
private:
    std::string name_;
    std::unique_ptr<int[]> data_;

public:
    explicit NonCopyable(const std::string& name)
        : name_(name), data_(std::make_unique<int[]>(10)) {
        std::cout << "NonCopyable constructor: " << name_ << std::endl;
    }

    // Explicitly delete copy operations
    NonCopyable(const NonCopyable&) = delete;
    NonCopyable& operator=(const NonCopyable&) = delete;

    // Move operations are still available (or can be explicitly defaulted)
    NonCopyable(NonCopyable&&) = default;
    NonCopyable& operator=(NonCopyable&&) = default;

    void display() const {
        std::cout << "NonCopyable: " << name_ << std::endl;
    }
};

// Class with defaulted special members
class DefaultedMembers {
private:
    std::string name_;
    int value_;

public:
    DefaultedMembers(const std::string& name, int value)
        : name_(name), value_(value) {
        std::cout << "DefaultedMembers constructor: " << name_ << std::endl;
    }

    // Explicitly default all special members
    ~DefaultedMembers() = default;
    DefaultedMembers(const DefaultedMembers&) = default;
    DefaultedMembers& operator=(const DefaultedMembers&) = default;
    DefaultedMembers(DefaultedMembers&&) = default;
    DefaultedMembers& operator=(DefaultedMembers&&) = default;

    void display() const {
        std::cout << "DefaultedMembers: " << name_ << " = " << value_ << std::endl;
    }
};

void testRuleOfThree() {
    std::cout << "=== Testing Rule of Three ===" << std::endl;

    RuleOfThree obj1("original", 5);
    obj1.display();

    RuleOfThree obj2 = obj1;  // Copy constructor
    obj2.display();

    RuleOfThree obj3("temp", 3);
    obj3 = obj1;  // Copy assignment
    obj3.display();
}

void testRuleOfFive() {
    std::cout << "=== Testing Rule of Five ===" << std::endl;

    RuleOfFive obj1("original", 4);
    obj1.display();

    RuleOfFive obj2 = obj1;  // Copy constructor
    obj2.display();

    RuleOfFive obj3 = std::move(obj1);  // Move constructor
    obj3.display();
    obj1.display();  // Should show moved-from state

    RuleOfFive obj4("temp", 2);
    obj4 = std::move(obj2);  // Move assignment
    obj4.display();
    obj2.display();  // Should show moved-from state
}

void testRuleOfZero() {
    std::cout << "=== Testing Rule of Zero ===" << std::endl;

    RuleOfZero obj1("smart", 3);
    obj1.display();

    RuleOfZero obj2 = obj1;  // Custom copy constructor
    obj2.display();

    RuleOfZero obj3("temp", 2);
    obj3 = obj1;  // Custom copy assignment
    obj3.display();

    // Move operations work automatically
    RuleOfZero obj4 = std::move(obj1);  // Move constructor
    obj4.display();
}

void testSpecialMemberVariations() {
    std::cout << "=== Testing Special Member Variations ===" << std::endl;

    // Non-copyable class
    NonCopyable nc1("movable");
    nc1.display();

    NonCopyable nc2 = std::move(nc1);  // Move constructor works
    nc2.display();

    // NonCopyable nc3 = nc2;  // ERROR: copy constructor deleted
    // nc1 = nc2;              // ERROR: copy assignment deleted

    // Defaulted members
    DefaultedMembers dm1("default", 42);
    dm1.display();

    DefaultedMembers dm2 = dm1;  // Copy constructor (defaulted)
    dm2.display();

    DefaultedMembers dm3 = std::move(dm1);  // Move constructor (defaulted)
    dm3.display();
}

void demonstrateSpecialMemberFunctions() {
    testRuleOfThree();
    testRuleOfFive();
    testRuleOfZero();
    testSpecialMemberVariations();
}
""",
    )

    run_updater(cpp_constructor_project, mock_ingestor)

    project_name = cpp_constructor_project.name

    expected_classes = [
        f"{project_name}.special_members.RuleOfThree",
        f"{project_name}.special_members.RuleOfFive",
        f"{project_name}.special_members.RuleOfZero",
        f"{project_name}.special_members.NonCopyable",
        f"{project_name}.special_members.DefaultedMembers",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_cpp_constructor_destructor_comprehensive(
    cpp_constructor_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all constructor/destructor patterns create proper relationships."""
    test_file = cpp_constructor_project / "comprehensive_constructors.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every C++ constructor/destructor pattern in one file
#include <iostream>
#include <memory>
#include <utility>

class ComprehensiveClass {
private:
    std::string name_;
    std::unique_ptr<int[]> data_;
    size_t size_;
    static int instance_count_;

public:
    // Default constructor
    ComprehensiveClass() : name_("default"), size_(0) {
        ++instance_count_;
        std::cout << "Default constructor: " << name_ << " (count: " << instance_count_ << ")" << std::endl;
    }

    // Parameterized constructor
    explicit ComprehensiveClass(const std::string& name, size_t size = 10)
        : name_(name), size_(size), data_(std::make_unique<int[]>(size)) {
        ++instance_count_;
        std::cout << "Parameterized constructor: " << name_ << " (count: " << instance_count_ << ")" << std::endl;
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = static_cast<int>(i);
        }
    }

    // Delegating constructor
    ComprehensiveClass(int value) : ComprehensiveClass("delegated_" + std::to_string(value), 5) {
        std::cout << "Delegating constructor completed: " << name_ << std::endl;
    }

    // Copy constructor
    ComprehensiveClass(const ComprehensiveClass& other)
        : name_(other.name_ + "_copy"), size_(other.size_) {
        ++instance_count_;
        std::cout << "Copy constructor: " << name_ << " (count: " << instance_count_ << ")" << std::endl;
        if (other.data_ && size_ > 0) {
            data_ = std::make_unique<int[]>(size_);
            for (size_t i = 0; i < size_; ++i) {
                data_[i] = other.data_[i];
            }
        }
    }

    // Move constructor
    ComprehensiveClass(ComprehensiveClass&& other) noexcept
        : name_(std::move(other.name_)), data_(std::move(other.data_)), size_(other.size_) {
        ++instance_count_;
        other.size_ = 0;
        std::cout << "Move constructor: " << name_ << " (count: " << instance_count_ << ")" << std::endl;
    }

    // Copy assignment
    ComprehensiveClass& operator=(const ComprehensiveClass& other) {
        std::cout << "Copy assignment: " << name_ << " <- " << other.name_ << std::endl;
        if (this != &other) {
            name_ = other.name_ + "_assigned";
            size_ = other.size_;
            if (other.data_ && size_ > 0) {
                data_ = std::make_unique<int[]>(size_);
                for (size_t i = 0; i < size_; ++i) {
                    data_[i] = other.data_[i];
                }
            } else {
                data_.reset();
            }
        }
        return *this;
    }

    // Move assignment
    ComprehensiveClass& operator=(ComprehensiveClass&& other) noexcept {
        std::cout << "Move assignment: " << name_ << " <- " << other.name_ << std::endl;
        if (this != &other) {
            name_ = std::move(other.name_);
            data_ = std::move(other.data_);
            size_ = other.size_;
            other.size_ = 0;
        }
        return *this;
    }

    // Destructor
    ~ComprehensiveClass() {
        --instance_count_;
        std::cout << "Destructor: " << name_ << " (count: " << instance_count_ << ")" << std::endl;
    }

    void display() const {
        std::cout << "ComprehensiveClass " << name_ << " (size: " << size_ << ")";
        if (data_ && size_ > 0) {
            std::cout << " data: ";
            for (size_t i = 0; i < std::min(size_, size_t(5)); ++i) {
                std::cout << data_[i] << " ";
            }
        }
        std::cout << std::endl;
    }

    static int getInstanceCount() { return instance_count_; }
};

int ComprehensiveClass::instance_count_ = 0;

// Factory functions
ComprehensiveClass createObject(const std::string& name) {
    return ComprehensiveClass(name, 8);  // Return by value, move constructor called
}

std::unique_ptr<ComprehensiveClass> createUniqueObject(const std::string& name) {
    return std::make_unique<ComprehensiveClass>(name, 6);
}

void demonstrateComprehensiveConstructors() {
    std::cout << "=== Comprehensive Constructor/Destructor Test ===" << std::endl;

    // Default constructor
    ComprehensiveClass obj1;
    obj1.display();

    // Parameterized constructor
    ComprehensiveClass obj2("param", 5);
    obj2.display();

    // Delegating constructor
    ComprehensiveClass obj3(42);
    obj3.display();

    // Copy constructor
    ComprehensiveClass obj4 = obj2;
    obj4.display();

    // Move constructor
    ComprehensiveClass obj5 = std::move(obj3);
    obj5.display();
    obj3.display();  // Should show moved-from state

    // Copy assignment
    obj1 = obj2;
    obj1.display();

    // Move assignment
    obj1 = std::move(obj4);
    obj1.display();
    obj4.display();  // Should show moved-from state

    // Factory function calls
    auto obj6 = createObject("factory");
    obj6.display();

    auto obj7 = createUniqueObject("unique_factory");
    obj7->display();

    std::cout << "Current instance count: " << ComprehensiveClass::getInstanceCount() << std::endl;
    std::cout << "Exiting scope - all objects will be destroyed" << std::endl;
}
""",
    )

    run_updater(cpp_constructor_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_constructors" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 5, (
        f"Expected at least 5 comprehensive constructor calls, found {len(comprehensive_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"


def test_constructor_destructor_complete() -> None:
    """Mark the constructor/destructor task as completed."""
    pass
