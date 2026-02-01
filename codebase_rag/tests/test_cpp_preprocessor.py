from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_preprocessor_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with preprocessor directives."""
    project_path = temp_repo / "cpp_preprocessor_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_define_macros(
    cpp_preprocessor_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test #define macros including object-like and function-like macros."""
    test_file = cpp_preprocessor_project / "define_macros.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>

// Object-like macros
#define PI 3.14159265359
#define MAX_SIZE 1000
#define DEBUG_MODE 1
#define VERSION_MAJOR 2
#define VERSION_MINOR 1
#define VERSION_PATCH 0

// String macros
#define COMPANY_NAME "TechCorp"
#define COPYRIGHT_YEAR "2024"
#define DEFAULT_CONFIG_FILE "config.ini"

// Function-like macros
#define SQUARE(x) ((x) * (x))
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define ABS(x) ((x) < 0 ? -(x) : (x))

// Multi-line macros using backslash
#define SWAP(x, y) do { \\
    auto temp = x; \\
    x = y; \\
    y = temp; \\
} while(0)

// Macro with multiple statements
#define DEBUG_PRINT(msg) do { \\
    if (DEBUG_MODE) { \\
        std::cout << "[DEBUG] " << msg << std::endl; \\
    } \\
} while(0)

// Stringification macro
#define STRINGIFY(x) #x
#define TOSTRING(x) STRINGIFY(x)

// Token concatenation macro
#define CONCAT(a, b) a##b

// Variadic macros (C99/C++)
#define LOG(format, ...) printf(format, ##__VA_ARGS__)
#define PRINT_ARGS(...) printf(__VA_ARGS__)

// Complex function-like macro
#define DECLARE_GETTER_SETTER(type, name) \\
    private: \\
        type name##_; \\
    public: \\
        type get##name() const { return name##_; } \\
        void set##name(const type& value) { name##_ = value; }

// Conditional compilation within macros
#if DEBUG_MODE
    #define DBG_MSG(msg) std::cout << "DEBUG: " << msg << std::endl
#else
    #define DBG_MSG(msg) // No debug output in release mode
#endif

// Macro using other macros
#define CIRCLE_AREA(radius) (PI * SQUARE(radius))
#define VERSION_STRING TOSTRING(VERSION_MAJOR) "." TOSTRING(VERSION_MINOR) "." TOSTRING(VERSION_PATCH)

class MacroExample {
    // Using macro to declare member variables and accessors
    DECLARE_GETTER_SETTER(std::string, Name)
    DECLARE_GETTER_SETTER(int, Age)
    DECLARE_GETTER_SETTER(double, Salary)

public:
    MacroExample(const std::string& name, int age, double salary) {
        setName(name);
        setAge(age);
        setSalary(salary);
    }

    void printInfo() const {
        std::cout << "Name: " << getName() << std::endl;
        std::cout << "Age: " << getAge() << std::endl;
        std::cout << "Salary: $" << getSalary() << std::endl;
    }
};

void testObjectLikeMacros() {
    std::cout << "=== Testing Object-like Macros ===" << std::endl;

    // Using simple macros
    double radius = 5.0;
    double area = PI * radius * radius;
    std::cout << "Circle area (r=" << radius << "): " << area << std::endl;

    // Using macro in array declaration
    int buffer[MAX_SIZE];
    std::cout << "Buffer size: " << MAX_SIZE << std::endl;

    // String macros
    std::cout << "Company: " << COMPANY_NAME << std::endl;
    std::cout << "Copyright: " << COPYRIGHT_YEAR << std::endl;
    std::cout << "Config file: " << DEFAULT_CONFIG_FILE << std::endl;

    // Version information
    std::cout << "Version: " << VERSION_STRING << std::endl;

    // Debug mode check
    if (DEBUG_MODE) {
        std::cout << "Debug mode is enabled" << std::endl;
    }
}

void testFunctionLikeMacros() {
    std::cout << "=== Testing Function-like Macros ===" << std::endl;

    int a = 10, b = 20;

    // Basic function-like macros
    std::cout << "SQUARE(7) = " << SQUARE(7) << std::endl;
    std::cout << "MAX(a, b) = " << MAX(a, b) << std::endl;
    std::cout << "MIN(a, b) = " << MIN(a, b) << std::endl;
    std::cout << "ABS(-15) = " << ABS(-15) << std::endl;

    // Multi-statement macro
    std::cout << "Before swap: a=" << a << ", b=" << b << std::endl;
    SWAP(a, b);
    std::cout << "After swap: a=" << a << ", b=" << b << std::endl;

    // Debug printing
    DEBUG_PRINT("This is a debug message");
    DEBUG_PRINT("Values: a=" + std::to_string(a) + ", b=" + std::to_string(b));

    // Using macros with other macros
    double radius = 3.0;
    std::cout << "Circle area using macro: " << CIRCLE_AREA(radius) << std::endl;

    // Stringification
    std::cout << "Stringified PI: " << STRINGIFY(PI) << std::endl;
    std::cout << "Version as string: " << TOSTRING(VERSION_MAJOR) << std::endl;

    // Token concatenation
    int CONCAT(var, 123) = 456;
    std::cout << "Concatenated variable var123 = " << var123 << std::endl;
}

void testVariadicMacros() {
    std::cout << "=== Testing Variadic Macros ===" << std::endl;

    // Variadic macro usage
    LOG("Simple message\\n");
    LOG("Formatted message: %d + %d = %d\\n", 5, 3, 5 + 3);
    LOG("String: %s, Float: %.2f\\n", "Hello", 3.14159);

    PRINT_ARGS("Multiple arguments: %d, %s, %.1f\\n", 42, "world", 2.71);
}

void testMacroGeneration() {
    std::cout << "=== Testing Macro-Generated Code ===" << std::endl;

    // Using class with macro-generated getters/setters
    MacroExample person("Alice Johnson", 30, 75000.50);
    person.printInfo();

    // Modifying using generated setters
    person.setAge(31);
    person.setSalary(78000.00);

    std::cout << "After update:" << std::endl;
    person.printInfo();

    // Direct getter usage
    std::cout << "Direct access - Name: " << person.getName()
              << ", Age: " << person.getAge() << std::endl;
}

void testMacroEdgeCases() {
    std::cout << "=== Testing Macro Edge Cases ===" << std::endl;

    // Macro with side effects (demonstrates why parentheses are important)
    int x = 5;
    std::cout << "x = " << x << std::endl;
    std::cout << "SQUARE(x++) = " << SQUARE(x++) << std::endl;  // Dangerous! x is incremented twice
    std::cout << "x after SQUARE(x++) = " << x << std::endl;

    // Macro precedence issues
    int result1 = SQUARE(2 + 3);  // Should be (2+3)*(2+3) = 25, not 2+3*2+3 = 11
    std::cout << "SQUARE(2 + 3) = " << result1 << std::endl;

    // Multiple evaluation in MAX macro
    x = 10;
    int y = 5;
    std::cout << "Before MAX(++x, ++y): x=" << x << ", y=" << y << std::endl;
    int max_val = MAX(++x, ++y);  // Both x and y may be incremented multiple times
    std::cout << "MAX(++x, ++y) = " << max_val << std::endl;
    std::cout << "After MAX: x=" << x << ", y=" << y << std::endl;
}

void demonstrateDefineMacros() {
    testObjectLikeMacros();
    testFunctionLikeMacros();
    testVariadicMacros();
    testMacroGeneration();
    testMacroEdgeCases();
}
""",
    )

    run_updater(cpp_preprocessor_project, mock_ingestor)

    project_name = cpp_preprocessor_project.name

    expected_functions = [
        f"{project_name}.define_macros.testObjectLikeMacros",
        f"{project_name}.define_macros.testFunctionLikeMacros",
        f"{project_name}.define_macros.testVariadicMacros",
        f"{project_name}.define_macros.demonstrateDefineMacros",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_conditional_compilation(
    cpp_preprocessor_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test conditional compilation directives."""
    test_file = cpp_preprocessor_project / "conditional_compilation.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>

// Define compilation flags
#define FEATURE_LOGGING 1
#define FEATURE_DEBUGGING 1
#define OPTIMIZATION_LEVEL 2
#define TARGET_PLATFORM_WINDOWS 1
// #define TARGET_PLATFORM_LINUX 1
// #define TARGET_PLATFORM_MAC 1

// Compiler detection
#ifdef _MSC_VER
    #define COMPILER_MSVC 1
#elif defined(__GNUC__)
    #define COMPILER_GCC 1
#elif defined(__clang__)
    #define COMPILER_CLANG 1
#endif

// Version checking
#define REQUIRED_CPP_VERSION 201703L  // C++17

#if __cplusplus >= REQUIRED_CPP_VERSION
    #define MODERN_CPP_AVAILABLE 1
#else
    #define MODERN_CPP_AVAILABLE 0
#endif

// Platform-specific code
#ifdef TARGET_PLATFORM_WINDOWS
    #include <windows.h>
    #define PLATFORM_NAME "Windows"
    #define PATH_SEPARATOR "\\\\"
#elif defined(TARGET_PLATFORM_LINUX)
    #include <unistd.h>
    #define PLATFORM_NAME "Linux"
    #define PATH_SEPARATOR "/"
#elif defined(TARGET_PLATFORM_MAC)
    #include <unistd.h>
    #define PLATFORM_NAME "macOS"
    #define PATH_SEPARATOR "/"
#else
    #define PLATFORM_NAME "Unknown"
    #define PATH_SEPARATOR "/"
#endif

// Feature toggles
#ifdef FEATURE_LOGGING
    #define LOG(msg) std::cout << "[LOG] " << msg << std::endl
#else
    #define LOG(msg) // Logging disabled
#endif

#ifdef FEATURE_DEBUGGING
    #define DEBUG(msg) std::cout << "[DEBUG] " << msg << std::endl
    #define ASSERT(condition) \\
        if (!(condition)) { \\
            std::cerr << "Assertion failed: " << #condition \\
                      << " at " << __FILE__ << ":" << __LINE__ << std::endl; \\
        }
#else
    #define DEBUG(msg) // Debugging disabled
    #define ASSERT(condition) // Assertions disabled
#endif

// Optimization level based code
#if OPTIMIZATION_LEVEL >= 2
    #define INLINE_HINT inline
    #define FORCE_INLINE __forceinline
#else
    #define INLINE_HINT
    #define FORCE_INLINE
#endif

// Compiler-specific features
#ifdef COMPILER_MSVC
    #define PRAGMA_WARNING_PUSH __pragma(warning(push))
    #define PRAGMA_WARNING_DISABLE(x) __pragma(warning(disable: x))
    #define PRAGMA_WARNING_POP __pragma(warning(pop))
#elif defined(COMPILER_GCC) || defined(COMPILER_CLANG)
    #define PRAGMA_WARNING_PUSH _Pragma("GCC diagnostic push")
    #define PRAGMA_WARNING_DISABLE(x) _Pragma("GCC diagnostic ignored " #x)
    #define PRAGMA_WARNING_POP _Pragma("GCC diagnostic pop")
#else
    #define PRAGMA_WARNING_PUSH
    #define PRAGMA_WARNING_DISABLE(x)
    #define PRAGMA_WARNING_POP
#endif

class ConditionalFeatures {
private:
    std::string name_;

public:
    ConditionalFeatures(const std::string& name) : name_(name) {
        LOG("ConditionalFeatures constructor called for: " + name);
        DEBUG("Debug info: Creating object with name length: " + std::to_string(name.length()));
    }

    // Platform-specific method
    void showPlatformInfo() const {
        std::cout << "Running on: " << PLATFORM_NAME << std::endl;
        std::cout << "Path separator: " << PATH_SEPARATOR << std::endl;

#ifdef TARGET_PLATFORM_WINDOWS
        std::cout << "Windows-specific code active" << std::endl;
        // Windows-specific implementation
#elif defined(TARGET_PLATFORM_LINUX)
        std::cout << "Linux-specific code active" << std::endl;
        // Linux-specific implementation
#elif defined(TARGET_PLATFORM_MAC)
        std::cout << "macOS-specific code active" << std::endl;
        // macOS-specific implementation
#endif
    }

    // Conditionally compiled method
#if MODERN_CPP_AVAILABLE
    auto getModernFeature() const -> std::string {
        return "Modern C++ features available";
    }
#else
    std::string getModernFeature() const {
        return "Using legacy C++ syntax";
    }
#endif

    // Optimization level dependent method
    INLINE_HINT void fastOperation() const {
        LOG("Performing fast operation");

#if OPTIMIZATION_LEVEL >= 3
        // Highly optimized version
        std::cout << "Using highly optimized implementation" << std::endl;
#elif OPTIMIZATION_LEVEL >= 2
        // Medium optimization
        std::cout << "Using medium optimization implementation" << std::endl;
#else
        // Debug version
        std::cout << "Using debug implementation" << std::endl;
#endif
    }

    void validateInput(int value) const {
        ASSERT(value >= 0);
        ASSERT(value <= 100);

        if (value >= 0 && value <= 100) {
            LOG("Input validation passed for value: " + std::to_string(value));
        } else {
            DEBUG("Input validation failed for value: " + std::to_string(value));
        }
    }
};

// Conditional function definitions
#ifdef FEATURE_DEBUGGING
void debugOnlyFunction() {
    std::cout << "This function only exists in debug builds" << std::endl;
    DEBUG("debugOnlyFunction called");
}
#endif

#if OPTIMIZATION_LEVEL >= 2
FORCE_INLINE int optimizedCalculation(int a, int b) {
    return a * a + b * b;
}
#else
int optimizedCalculation(int a, int b) {
    DEBUG("Using unoptimized calculation");
    return a * a + b * b;
}
#endif

void testConditionalCompilation() {
    std::cout << "=== Testing Conditional Compilation ===" << std::endl;

    ConditionalFeatures features("TestObject");
    features.showPlatformInfo();

    std::cout << "Modern feature status: " << features.getModernFeature() << std::endl;

    features.fastOperation();

    // Test assertions
    features.validateInput(50);   // Should pass
    features.validateInput(150);  // Should trigger assertion

    // Conditional function calls
#ifdef FEATURE_DEBUGGING
    debugOnlyFunction();
#endif

    int result = optimizedCalculation(3, 4);
    std::cout << "Calculation result: " << result << std::endl;

    // Compiler information
#ifdef COMPILER_MSVC
    std::cout << "Compiled with Microsoft Visual C++" << std::endl;
#elif defined(COMPILER_GCC)
    std::cout << "Compiled with GCC" << std::endl;
#elif defined(COMPILER_CLANG)
    std::cout << "Compiled with Clang" << std::endl;
#else
    std::cout << "Compiled with unknown compiler" << std::endl;
#endif

    // C++ version info
    std::cout << "C++ standard: " << __cplusplus << std::endl;
#if MODERN_CPP_AVAILABLE
    std::cout << "Modern C++ features are available" << std::endl;
#else
    std::cout << "Using legacy C++ features" << std::endl;
#endif
}

// Complex conditional compilation example
#if defined(FEATURE_LOGGING) && defined(FEATURE_DEBUGGING)
    #define ENHANCED_LOGGING 1
#endif

#ifdef ENHANCED_LOGGING
void enhancedLog(const std::string& message) {
    LOG("ENHANCED: " + message);
    DEBUG("Enhanced logging active");
}
#else
void enhancedLog(const std::string& message) {
    std::cout << message << std::endl;
}
#endif

void testComplexConditionals() {
    std::cout << "=== Testing Complex Conditionals ===" << std::endl;

    enhancedLog("This message uses conditional logging");

    // Nested conditionals
#if OPTIMIZATION_LEVEL >= 2
    #ifdef FEATURE_DEBUGGING
        std::cout << "High optimization with debugging enabled" << std::endl;
    #else
        std::cout << "High optimization, no debugging" << std::endl;
    #endif
#else
    std::cout << "Low optimization level" << std::endl;
#endif

    // Multiple condition check
#if defined(TARGET_PLATFORM_WINDOWS) && (OPTIMIZATION_LEVEL >= 2)
    std::cout << "Windows platform with high optimization" << std::endl;
#elif defined(TARGET_PLATFORM_LINUX) || defined(TARGET_PLATFORM_MAC)
    std::cout << "Unix-like platform" << std::endl;
#else
    std::cout << "Other platform configuration" << std::endl;
#endif
}

void demonstrateConditionalCompilation() {
    testConditionalCompilation();
    testComplexConditionals();
}
""",
    )

    run_updater(cpp_preprocessor_project, mock_ingestor)

    project_name = cpp_preprocessor_project.name

    expected_classes = [
        f"{project_name}.conditional_compilation.ConditionalFeatures",
    ]

    expected_functions = [
        f"{project_name}.conditional_compilation.testConditionalCompilation",
        f"{project_name}.conditional_compilation.testComplexConditionals",
        f"{project_name}.conditional_compilation.demonstrateConditionalCompilation",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 conditional compilation class, found {len(found_classes)}: {found_classes}"
    )

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_pragma_directives(
    cpp_preprocessor_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test #pragma directives and include guards."""
    test_file = cpp_preprocessor_project / "pragma_directives.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Include guard using traditional method
#ifndef PRAGMA_DIRECTIVES_H
#define PRAGMA_DIRECTIVES_H

#include <iostream>
#include <string>
#include <vector>

// Pragma once (modern include guard)
#pragma once

// Compiler-specific pragmas
#pragma warning(push)
#pragma warning(disable: 4996)  // Disable deprecated function warnings

// Optimization pragmas
#pragma optimize("gt", on)  // Enable global optimization and fast code

// Packing pragmas for struct alignment
#pragma pack(push, 1) // Pack structures to 1-byte boundaries

struct PackedStruct {
    char a;      // 1 byte
    int b;       // 4 bytes (normally would be padded)
    short c;     // 2 bytes
};  // Total: 7 bytes instead of 12

#pragma pack(pop) // Restore original packing

// Loop optimization pragmas
#pragma loop(hint_parallel(4))
#pragma loop(ivdep) // Ignore vector dependencies

// Function inlining pragmas
#pragma inline_depth(10)
#pragma inline_recursion(on)

// Custom pragma for code analysis
#pragma code_seg(".MYCODE")
#pragma data_seg(".MYDATA")

class PragmaExample {
private:
    std::vector<int> data_;

public:
    PragmaExample(size_t size) : data_(size) {
        // Initialize data with pragma-optimized loop
#pragma loop(hint_parallel(0))
        for (size_t i = 0; i < size; ++i) {
            data_[i] = static_cast<int>(i * i);
        }
    }

    // Function with pragma-controlled inlining
#pragma inline
    int getValue(size_t index) const {
        return (index < data_.size()) ? data_[index] : -1;
    }

    // Function with pragma-disabled warnings
#pragma warning(push)
#pragma warning(disable: 4101) // Unreferenced local variable
    void demonstrateWarningControl() {
        int unused_variable;  // This would normally generate a warning

        std::cout << "Warning control demonstration" << std::endl;
    }
#pragma warning(pop)

    // Vectorization hints
    void processDataParallel() {
#pragma ivdep
#pragma vector always
        for (size_t i = 0; i < data_.size(); ++i) {
            data_[i] = data_[i] * 2 + 1;
        }
    }

    // OpenMP pragma (if supported)
#ifdef _OPENMP
#pragma omp parallel for
#endif
    void processDataOpenMP() {
        for (int i = 0; i < static_cast<int>(data_.size()); ++i) {
            data_[i] = data_[i] * 3;
        }
    }

    size_t size() const { return data_.size(); }

    void printData() const {
        std::cout << "Data: ";
        for (size_t i = 0; i < std::min(data_.size(), size_t(10)); ++i) {
            std::cout << data_[i] << " ";
        }
        if (data_.size() > 10) {
            std::cout << "... (" << data_.size() << " total)";
        }
        std::cout << std::endl;
    }
};

// Function with optimization pragmas
#pragma optimize("", off)  // Disable optimization for debugging
void debugFunction() {
    std::cout << "This function has optimizations disabled" << std::endl;

    // Intentionally inefficient code for debugging
    volatile int sum = 0;
    for (volatile int i = 0; i < 1000; ++i) {
        sum += i;
    }

    std::cout << "Debug sum: " << sum << std::endl;
}
#pragma optimize("", on)   // Re-enable optimization

// Deprecated function with pragma
#pragma deprecated(oldFunction)
void oldFunction() {
    std::cout << "This function is deprecated" << std::endl;
}

// Function with custom pragma messages
void testPragmaMessages() {
#pragma message("Compiling pragma message test function")

    std::cout << "=== Testing Pragma Messages ===" << std::endl;

#ifdef DEBUG
    #pragma message("Debug mode is enabled")
#else
    #pragma message("Release mode compilation")
#endif

    // Conditional pragma based on compiler
#ifdef _MSC_VER
    #pragma message("Compiling with Microsoft Visual C++")
#elif defined(__GNUC__)
    #pragma message("Compiling with GCC")
#endif
}

// Memory alignment pragmas
#pragma align(16)
struct AlignedStruct {
    double values[4];  // Will be 16-byte aligned
};

void testStructPacking() {
    std::cout << "=== Testing Struct Packing ===" << std::endl;

    // Test packed struct
    PackedStruct packed;
    std::cout << "PackedStruct size: " << sizeof(PackedStruct) << " bytes" << std::endl;

    // Test aligned struct
    AlignedStruct aligned;
    std::cout << "AlignedStruct size: " << sizeof(AlignedStruct) << " bytes" << std::endl;
    std::cout << "AlignedStruct alignment: " << alignof(AlignedStruct) << " bytes" << std::endl;
}

void testOptimizationPragmas() {
    std::cout << "=== Testing Optimization Pragmas ===" << std::endl;

    PragmaExample example(1000);

    // Test regular processing
    auto start = std::chrono::high_resolution_clock::now();
    example.processDataParallel();
    auto end = std::chrono::high_resolution_clock::now();

    std::cout << "Parallel processing completed" << std::endl;
    example.printData();

    // Test OpenMP processing (if available)
#ifdef _OPENMP
    example.processDataOpenMP();
    std::cout << "OpenMP processing completed" << std::endl;
#else
    std::cout << "OpenMP not available" << std::endl;
#endif

    // Test debug function (unoptimized)
    debugFunction();

    // Test deprecated function (may generate warnings)
    oldFunction();
}

// Template with pragma specialization
template<typename T>
class PragmaTemplate {
private:
    T value_;

public:
    PragmaTemplate(T value) : value_(value) {}

#pragma inline
    T getValue() const { return value_; }

    void setValue(T value) { value_ = value; }
};

// Specialized version with different pragma settings
#pragma pack(push, 4)
template<>
class PragmaTemplate<double> {
private:
    double value_;
    int padding_;  // Explicit padding control

public:
    PragmaTemplate(double value) : value_(value), padding_(0) {}

    double getValue() const { return value_; }
    void setValue(double value) { value_ = value; }
};
#pragma pack(pop)

void testTemplatePragmas() {
    std::cout << "=== Testing Template Pragmas ===" << std::endl;

    PragmaTemplate<int> intTemplate(42);
    PragmaTemplate<double> doubleTemplate(3.14159);

    std::cout << "Int template value: " << intTemplate.getValue() << std::endl;
    std::cout << "Double template value: " << doubleTemplate.getValue() << std::endl;
    std::cout << "Double template size: " << sizeof(doubleTemplate) << " bytes" << std::endl;
}

void demonstratePragmaDirectives() {
    testPragmaMessages();
    testStructPacking();
    testOptimizationPragmas();
    testTemplatePragmas();
}

// Restore original pragma settings
#pragma warning(pop)
#pragma optimize("", on)

#endif // PRAGMA_DIRECTIVES_H
""",
    )

    run_updater(cpp_preprocessor_project, mock_ingestor)

    project_name = cpp_preprocessor_project.name

    expected_classes = [
        f"{project_name}.pragma_directives.PragmaExample",
    ]

    expected_functions = [
        f"{project_name}.pragma_directives.testPragmaMessages",
        f"{project_name}.pragma_directives.testStructPacking",
        f"{project_name}.pragma_directives.demonstratePragmaDirectives",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 pragma directive class, found {len(found_classes)}: {found_classes}"
    )

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_cpp_preprocessor_comprehensive(
    cpp_preprocessor_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all preprocessor features create proper relationships."""
    test_file = cpp_preprocessor_project / "comprehensive_preprocessor.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Comprehensive preprocessor example combining all features
#ifndef COMPREHENSIVE_PREPROCESSOR_H
#define COMPREHENSIVE_PREPROCESSOR_H

#pragma once

#include <iostream>
#include <string>

// Configuration macros
#define ENABLE_FEATURE_A 1
#define ENABLE_FEATURE_B 0
#define DEBUG_LEVEL 2
#define VERSION_MAJOR 3
#define VERSION_MINOR 1

// Function-like macros
#define LOG_DEBUG(msg) do { \\
    if (DEBUG_LEVEL >= 2) { \\
        std::cout << "[DEBUG] " << msg << std::endl; \\
    } \\
} while(0)

#define STRINGIFY(x) #x
#define COMBINE(a, b) a##b

// Conditional compilation with complex logic
#if (ENABLE_FEATURE_A && DEBUG_LEVEL >= 1) || defined(FORCE_DEBUG)
    #define ADVANCED_DEBUGGING 1
#else
    #define ADVANCED_DEBUGGING 0
#endif

// Platform detection and adaptation
#ifdef _WIN32
    #define PLATFORM_SPECIFIC_CODE windows_implementation
#elif defined(__linux__)
    #define PLATFORM_SPECIFIC_CODE linux_implementation
#else
    #define PLATFORM_SPECIFIC_CODE generic_implementation
#endif

#pragma pack(push, 1)
struct ComprehensiveConfig {
    char feature_flags;
    int version;
    short debug_level;
};
#pragma pack(pop)

class ComprehensivePreprocessor {
private:
    std::string name_;
    ComprehensiveConfig config_;

public:
    ComprehensivePreprocessor(const std::string& name) : name_(name) {
        config_.feature_flags = (ENABLE_FEATURE_A ? 1 : 0) | (ENABLE_FEATURE_B ? 2 : 0);
        config_.version = (VERSION_MAJOR << 16) | VERSION_MINOR;
        config_.debug_level = DEBUG_LEVEL;

        LOG_DEBUG("ComprehensivePreprocessor created: " + name);
    }

    void demonstrateFeatures() {
        std::cout << "=== Comprehensive Preprocessor Demo ===" << std::endl;
        std::cout << "Object: " << name_ << std::endl;

        // Conditional feature demonstration
#if ENABLE_FEATURE_A
        std::cout << "Feature A is enabled" << std::endl;
        featureAImplementation();
#else
        std::cout << "Feature A is disabled" << std::endl;
#endif

#if ENABLE_FEATURE_B
        std::cout << "Feature B is enabled" << std::endl;
        featureBImplementation();
#else
        std::cout << "Feature B is disabled" << std::endl;
#endif

        // Platform-specific code
        PLATFORM_SPECIFIC_CODE();

        // Debug level dependent behavior
#if DEBUG_LEVEL >= 2
        std::cout << "High debug level - showing detailed info" << std::endl;
        showDetailedInfo();
#elif DEBUG_LEVEL >= 1
        std::cout << "Medium debug level - showing basic info" << std::endl;
        showBasicInfo();
#else
        std::cout << "No debug output" << std::endl;
#endif

        // Advanced debugging if enabled
#if ADVANCED_DEBUGGING
        std::cout << "Advanced debugging features active" << std::endl;
        performAdvancedDebugging();
#endif
    }

private:
#if ENABLE_FEATURE_A
    void featureAImplementation() {
        LOG_DEBUG("Executing Feature A");
        std::cout << "  Feature A processing..." << std::endl;
    }
#endif

#if ENABLE_FEATURE_B
    void featureBImplementation() {
        LOG_DEBUG("Executing Feature B");
        std::cout << "  Feature B processing..." << std::endl;
    }
#endif

    void windows_implementation() {
        std::cout << "  Using Windows-specific implementation" << std::endl;
    }

    void linux_implementation() {
        std::cout << "  Using Linux-specific implementation" << std::endl;
    }

    void generic_implementation() {
        std::cout << "  Using generic implementation" << std::endl;
    }

#if DEBUG_LEVEL >= 1
    void showBasicInfo() {
        std::cout << "  Config size: " << sizeof(config_) << " bytes" << std::endl;
        std::cout << "  Version: " << STRINGIFY(VERSION_MAJOR) "." STRINGIFY(VERSION_MINOR) << std::endl;
    }
#endif

#if DEBUG_LEVEL >= 2
    void showDetailedInfo() {
        showBasicInfo();
        std::cout << "  Feature flags: " << static_cast<int>(config_.feature_flags) << std::endl;
        std::cout << "  Debug level: " << config_.debug_level << std::endl;
    }
#endif

#if ADVANCED_DEBUGGING
    void performAdvancedDebugging() {
        std::cout << "  Advanced debugging: Memory layout analysis" << std::endl;
        std::cout << "  Object address: " << this << std::endl;
        std::cout << "  Name address: " << &name_ << std::endl;
        std::cout << "  Config address: " << &config_ << std::endl;
    }
#endif
};

// Macro-generated helper functions
#define DECLARE_HELPER_FUNCTION(name, type) \\
    type get##name##Value() { \\
        LOG_DEBUG("Getting " STRINGIFY(name) " value"); \\
        return static_cast<type>(0); \\
    }

DECLARE_HELPER_FUNCTION(Integer, int)
DECLARE_HELPER_FUNCTION(Double, double)
DECLARE_HELPER_FUNCTION(String, std::string)

void demonstrateComprehensivePreprocessor() {
    ComprehensivePreprocessor processor("MainProcessor");
    processor.demonstrateFeatures();

    // Test macro-generated functions
    auto intVal = getIntegerValue();
    auto doubleVal = getDoubleValue();
    auto stringVal = getStringValue();

    std::cout << "Macro-generated values: " << intVal << ", " << doubleVal << std::endl;

    // Show compilation information
    std::cout << "Compilation info:" << std::endl;
    std::cout << "  Date: " << __DATE__ << std::endl;
    std::cout << "  Time: " << __TIME__ << std::endl;
    std::cout << "  File: " << __FILE__ << std::endl;
    std::cout << "  C++ Standard: " << __cplusplus << std::endl;

#ifdef __GNUC__
    std::cout << "  GCC Version: " << __GNUC__ << "." << __GNUC_MINOR__ << std::endl;
#endif

#ifdef _MSC_VER
    std::cout << "  MSVC Version: " << _MSC_VER << std::endl;
#endif
}

#endif // COMPREHENSIVE_PREPROCESSOR_H
""",
    )

    run_updater(cpp_preprocessor_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_preprocessor" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 3, (
        f"Expected at least 3 comprehensive preprocessor calls, found {len(comprehensive_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
