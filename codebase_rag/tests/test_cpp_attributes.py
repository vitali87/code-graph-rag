from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def cpp_attributes_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with attribute patterns."""
    project_path = temp_repo / "cpp_attributes_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    (project_path / "include" / "attributes.h").write_text(
        encoding="utf-8", data="#pragma once\nnamespace attr_test {}"
    )

    return project_path


def test_standard_attributes(
    cpp_attributes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test standard C++ attributes parsing and relationship tracking."""
    test_file = cpp_attributes_project / "standard_attributes.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <memory>
#include <vector>
#include <optional>

// [[nodiscard]] attribute testing
[[nodiscard]] int calculateImportantValue() {
    return 42;
}

[[nodiscard("Don't ignore the result!")]]
std::string createImportantString() {
    return "Critical data";
}

[[nodiscard]] constexpr bool isValid(int value) noexcept {
    return value > 0;
}

// [[maybe_unused]] attribute testing
void processData([[maybe_unused]] int debugMode,
                 [[maybe_unused]] const std::string& logLevel) {
    // Implementation might not use all parameters in release builds
    #ifdef DEBUG
        std::cout << "Debug mode: " << debugMode << ", Level: " << logLevel << std::endl;
    #endif
}

[[maybe_unused]] static int globalCounter = 0;

[[maybe_unused]] static void helperFunction() {
    // Utility function that might not be used in all builds
    globalCounter++;
}

// [[deprecated]] attribute testing
[[deprecated]]
void oldFunction() {
    std::cout << "This function is deprecated" << std::endl;
}

[[deprecated("Use newImprovedFunction() instead")]]
int legacyCalculation(int x, int y) {
    return x + y;
}

[[deprecated("This class will be removed in v2.0")]]
class LegacyClass {
public:
    [[deprecated("Use getNewValue() instead")]]
    int getOldValue() const { return value_; }

    int getNewValue() const { return value_; }

private:
    int value_ = 0;
};

// [[fallthrough]] attribute testing
std::string processSwitch(int value) {
    std::string result;

    switch (value) {
        case 1:
            result += "one";
            [[fallthrough]];
        case 2:
            result += "two";
            [[fallthrough]];
        case 3:
            result += "three";
            break;
        case 4:
            result += "four";
            [[fallthrough]];
        default:
            result += "default";
            break;
    }

    return result;
}

// Complex switch with multiple fallthrough patterns
int complexSwitch(char c) {
    switch (c) {
        case 'a':
        case 'A':
            std::cout << "Letter A";
            [[fallthrough]];
        case 'e':
        case 'E':
            std::cout << " (vowel)";
            return 1;

        case 'b':
            std::cout << "Letter B";
            [[fallthrough]];
        case 'c':
            std::cout << " (consonant)";
            return 2;

        default:
            return 0;
    }
}

// [[likely]] and [[unlikely]] attributes (C++20)
int processConditions(int value) {
    if (value > 0) [[likely]] {
        // Most common case
        return value * 2;
    } else if (value < -100) [[unlikely]] {
        // Rare error case
        throw std::runtime_error("Invalid value");
    } else {
        // Uncommon but not rare
        return 0;
    }
}

// Complex branching with likelihood hints
std::optional<int> parseInput(const std::string& input) {
    if (input.empty()) [[unlikely]] {
        return std::nullopt;
    }

    if (input.length() > 1000) [[unlikely]] {
        // Very long input is rare
        return std::nullopt;
    }

    // Normal processing path
    if (std::isdigit(input[0])) [[likely]] {
        try {
            return std::stoi(input);
        } catch (...) [[unlikely]] {
            return std::nullopt;
        }
    }

    return std::nullopt;
}

// Combining multiple attributes
[[nodiscard]] [[deprecated("Use modernCalculation() instead")]]
int legacyImportantCalculation(int x) {
    return x * x;
}

[[nodiscard]]
constexpr int modernCalculation(int x) noexcept {
    return x * x;
}

// Attributes on class members
class AttributeDemo {
public:
    [[nodiscard]] bool isReady() const noexcept { return ready_; }

    [[deprecated("Use setStatus() instead")]]
    void setReady(bool ready) { ready_ = ready; }

    void setStatus([[maybe_unused]] bool status) {
        ready_ = status;
    }

private:
    [[maybe_unused]] static constexpr int VERSION = 1;
    bool ready_ = false;
};

// Namespace-level attributes
namespace [[deprecated("Use modern_api namespace instead")]] legacy_api {
    void oldStyleFunction() {
        std::cout << "Legacy API function" << std::endl;
    }
}

namespace modern_api {
    [[nodiscard]] bool newStyleFunction() {
        std::cout << "Modern API function" << std::endl;
        return true;
    }
}

// Function demonstrating attribute usage patterns
void demonstrateStandardAttributes() {
    // Test nodiscard attributes
    int result = calculateImportantValue();  // OK
    calculateImportantValue();  // Warning: discarding nodiscard return value

    std::string data = createImportantString();  // OK
    createImportantString();  // Warning: discarding nodiscard return value

    // Test maybe_unused
    processData(1, "DEBUG");  // Parameters marked maybe_unused

    // Test deprecated functions
    oldFunction();  // Warning: deprecated
    int sum = legacyCalculation(5, 3);  // Warning: deprecated with message

    LegacyClass legacy;  // Warning: deprecated class
    int oldVal = legacy.getOldValue();  // Warning: deprecated method
    int newVal = legacy.getNewValue();  // OK

    // Test fallthrough
    std::string switchResult = processSwitch(2);
    int charResult = complexSwitch('a');

    // Test likely/unlikely
    int condResult = processConditions(10);  // Likely path

    try {
        int errorResult = processConditions(-200);  // Unlikely path
    } catch (const std::runtime_error& e) {
        std::cerr << "Caught exception: " << e.what() << std::endl;
    }

    // Test optional parsing with likelihood
    auto parsed1 = parseInput("123");      // Likely success
    auto parsed2 = parseInput("");         // Unlikely empty input
    auto parsed3 = parseInput("not_number"); // Normal failure case

    // Test combined attributes
    int legacyResult = legacyImportantCalculation(5);  // Deprecated + nodiscard
    int modernResult = modernCalculation(5);           // Just nodiscard

    // Test class attributes
    AttributeDemo demo;
    bool ready = demo.isReady();  // nodiscard
    demo.setReady(true);          // deprecated
    demo.setStatus(true);         // modern method

    // Test namespace attributes
    legacy_api::oldStyleFunction();  // Deprecated namespace
    bool success = modern_api::newStyleFunction();  // Modern namespace
}

// Template with attributes
template<typename T>
[[nodiscard]] constexpr T multiply([[maybe_unused]] T a, T b) noexcept {
    // In some specializations, parameter 'a' might not be used
    return b * b;  // Simplified implementation
}

// Specialization with different attribute usage
template<>
[[nodiscard]] constexpr int multiply<int>(int a, int b) noexcept {
    return a * b;  // Both parameters used in this specialization
}

void testTemplateAttributes() {
    auto intResult = multiply<int>(3, 4);      // Uses specialization
    auto doubleResult = multiply<double>(2.5, 3.0);  // Uses general template

    // Test discarding nodiscard return
    multiply<int>(5, 6);  // Warning: discarding nodiscard return
}
""",
    )

    run_updater(cpp_attributes_project, mock_ingestor)

    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    call_relationships = get_relationships(mock_ingestor, "CALLS")

    attributed_functions = [
        call
        for call in defines_relationships
        if "standard_attributes" in call.args[0][2]
        and any(
            func in call.args[2][2]
            for func in [
                "calculateImportantValue",
                "createImportantString",
                "processData",
                "oldFunction",
                "legacyCalculation",
                "processSwitch",
                "processConditions",
            ]
        )
    ]

    assert len(attributed_functions) >= 7, (
        f"Expected at least 7 attributed functions, found {len(attributed_functions)}"
    )

    attribute_function_calls = [
        call
        for call in call_relationships
        if "standard_attributes" in call.args[0][2]
        and any(
            func in call.args[2][2]
            for func in [
                "calculateImportantValue",
                "createImportantString",
                "oldFunction",
            ]
        )
    ]

    assert len(attribute_function_calls) >= 3, (
        f"Expected at least 3 attributed function calls, found {len(attribute_function_calls)}"
    )


def test_compiler_specific_attributes(
    cpp_attributes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test compiler-specific attributes and non-standard extensions."""
    test_file = cpp_attributes_project / "compiler_attributes.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <vector>
#include <string>

// GCC/Clang specific attributes using __attribute__ syntax
__attribute__((always_inline))
inline int fastFunction(int x) {
    return x * 2;
}

__attribute__((noinline))
void slowFunction() {
    std::cout << "This function should not be inlined" << std::endl;
}

__attribute__((pure))
int pureCalculation(int a, int b) {
    // Pure function - no side effects, result depends only on arguments
    return a + b * 2;
}

__attribute__((const))
constexpr int constantCalculation(int x) {
    // Const function - even stricter than pure, no memory access
    return x * x;
}

// Function alignment
__attribute__((aligned(32)))
void alignedFunction() {
    std::cout << "Function aligned to 32-byte boundary" << std::endl;
}

// Hot/Cold attributes for optimization
__attribute__((hot))
void frequentlyCalledFunction() {
    // This function is called frequently and should be optimized for speed
    for (int i = 0; i < 1000; ++i) {
        // Tight loop
    }
}

__attribute__((cold))
void rarelyCalledFunction() {
    // This function is rarely called, optimize for size instead of speed
    std::cerr << "Error: This should rarely happen" << std::endl;
}

// Visibility attributes for shared libraries
__attribute__((visibility("default")))
void publicApiFunction() {
    std::cout << "This function is part of the public API" << std::endl;
}

__attribute__((visibility("hidden")))
void internalFunction() {
    std::cout << "This function is internal to the library" << std::endl;
}

// Constructor/Destructor attributes
__attribute__((constructor))
void moduleInitializer() {
    std::cout << "Module constructor called" << std::endl;
}

__attribute__((destructor))
void moduleCleanup() {
    std::cout << "Module destructor called" << std::endl;
}

// Variable attributes
__attribute__((aligned(64)))
static int alignedVariable = 42;

__attribute__((section(".special_data")))
static int specialSectionVariable = 100;

__attribute__((weak))
int weakSymbol = 200;

// Weak function that can be overridden
__attribute__((weak))
void weakFunction() {
    std::cout << "Default weak implementation" << std::endl;
}

// Warning attributes
__attribute__((deprecated("Use newSecureFunction() instead")))
void insecureFunction(char* buffer) {
    // Unsafe string operations
    strcpy(buffer, "unsafe");
}

void newSecureFunction(std::string& buffer) {
    buffer = "safe";
}

// Format checking attributes
__attribute__((format(printf, 1, 2)))
void debugPrintf(const char* format, ...) {
    // Compiler will check printf-style format strings
    va_list args;
    va_start(args, format);
    vprintf(format, args);
    va_end(args);
}

// Return value checking
__attribute__((warn_unused_result))
bool criticalOperation() {
    // Return value should not be ignored
    return true;
}

// Sentinel attribute for variadic functions
__attribute__((sentinel))
void processStrings(const char* first, ...) {
    // Last argument must be NULL
    va_list args;
    va_start(args, first);

    const char* current = first;
    while (current != nullptr) {
        std::cout << current << " ";
        current = va_arg(args, const char*);
    }

    va_end(args);
    std::cout << std::endl;
}

// Microsoft Visual C++ specific attributes
#ifdef _MSC_VER
__declspec(noinline)
void msvcNoInline() {
    std::cout << "MSVC no-inline function" << std::endl;
}

__declspec(forceinline)
inline void msvcForceInline() {
    // Force inlining even if compiler thinks it's not beneficial
}

__declspec(dllexport)
void exportedFunction() {
    std::cout << "Function exported from DLL" << std::endl;
}

__declspec(align(16))
struct AlignedStruct {
    double data[2];
};
#endif

// Mixed standard and compiler-specific attributes
[[nodiscard]] __attribute__((pure))
int combinedAttributesFunction(int x, int y) {
    return x * y;
}

[[deprecated("Use modernCombinedFunction()")]] __attribute__((cold))
void legacyCombinedFunction() {
    std::cout << "Legacy function with mixed attributes" << std::endl;
}

// Class with compiler-specific attributes
class __attribute__((packed)) PackedClass {
    char a;
    int b;    // Normally would be aligned, but packed prevents padding
    char c;
};

// Attribute on class methods
class AttributedClass {
public:
    __attribute__((always_inline))
    inline int inlinedMethod() const { return value_; }

    __attribute__((noinline))
    void nonInlinedMethod() {
        value_++;
    }

    [[nodiscard]] __attribute__((pure))
    int pureMethod(int x) const {
        return value_ + x;
    }

private:
    mutable int value_ = 0;
};

// Function demonstrating compiler-specific attribute usage
void demonstrateCompilerAttributes() {
    // Test always_inline/noinline
    int fast = fastFunction(5);
    slowFunction();

    // Test pure/const functions
    int pureResult = pureCalculation(3, 4);
    int constResult = constantCalculation(6);

    // Test hot/cold functions
    frequentlyCalledFunction();  // Called in hot path

    try {
        throw std::runtime_error("Test error");
    } catch (...) {
        rarelyCalledFunction();  // Called in error path (cold)
    }

    // Test visibility functions
    publicApiFunction();
    internalFunction();

    // Test weak symbols
    weakFunction();  // May call overridden version if available

    // Test deprecated function
    char buffer[100];
    insecureFunction(buffer);  // Warning: deprecated

    std::string secureBuffer;
    newSecureFunction(secureBuffer);  // Modern alternative

    // Test format checking
    debugPrintf("Debug value: %d, string: %s\\n", 42, "test");

    // Test return value checking
    bool result = criticalOperation();  // OK
    criticalOperation();                // Warning: unused result

    // Test sentinel function
    processStrings("first", "second", "third", nullptr);  // Must end with nullptr

    // Test combined attributes
    int combined = combinedAttributesFunction(7, 8);  // nodiscard + pure
    legacyCombinedFunction();  // deprecated + cold

    // Test class attributes
    PackedClass packed;
    std::cout << "PackedClass size: " << sizeof(PackedClass) << std::endl;

    AttributedClass attributed;
    int inlined = attributed.inlinedMethod();    // always_inline
    attributed.nonInlinedMethod();               // noinline
    int pure = attributed.pureMethod(10);        // nodiscard + pure

#ifdef _MSC_VER
    // Test MSVC-specific attributes
    msvcNoInline();
    msvcForceInline();
    exportedFunction();

    AlignedStruct aligned;
    std::cout << "AlignedStruct alignment: " << alignof(AlignedStruct) << std::endl;
#endif
}

// Template function with attributes
template<typename T>
__attribute__((always_inline)) [[nodiscard]]
constexpr T attributedTemplate(T value) noexcept {
    return value * 2;
}

// Specialization with different attributes
template<>
__attribute__((noinline)) [[nodiscard]]
int attributedTemplate<int>(int value) noexcept {
    // Force no-inline for int specialization
    return value * 3;
}

void testAttributedTemplates() {
    auto floatResult = attributedTemplate<float>(2.5f);  // Inlined
    auto intResult = attributedTemplate<int>(5);         // Not inlined

    // Test discarding results
    attributedTemplate<double>(3.0);  // Warning: nodiscard
}

// Attribute inheritance in class hierarchies
class __attribute__((visibility("default"))) BaseClass {
public:
    virtual ~BaseClass() = default;

    [[nodiscard]] virtual bool virtualMethod() const = 0;

    __attribute__((hot))
    void hotMethod() const {
        // Frequently called base method
    }
};

class DerivedClass : public BaseClass {
public:
    [[nodiscard]] bool virtualMethod() const override {
        return true;
    }

    __attribute__((cold))
    void coldMethod() const {
        // Rarely called derived method
    }
};

void testAttributeInheritance() {
    DerivedClass derived;
    bool result = derived.virtualMethod();  // nodiscard from override
    derived.hotMethod();   // Inherited hot method
    derived.coldMethod();  // Cold derived method

    // Polymorphic call
    BaseClass* base = &derived;
    bool polyResult = base->virtualMethod();  // Virtual dispatch with nodiscard
    base->hotMethod();  // Virtual call to hot method
}
""",
    )

    run_updater(cpp_attributes_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    [c for c in all_relationships if c.args[1] == "CALLS"]

    compiler_attributed_functions = [
        call
        for call in defines_relationships
        if "compiler_attributes" in call.args[0][2]
        and any(
            func in call.args[2][2]
            for func in [
                "fastFunction",
                "slowFunction",
                "pureCalculation",
                "constantCalculation",
                "frequentlyCalledFunction",
                "rarelyCalledFunction",
                "publicApiFunction",
            ]
        )
    ]

    assert len(compiler_attributed_functions) >= 6, (
        f"Expected at least 6 compiler-attributed functions, found {len(compiler_attributed_functions)}"
    )

    attributed_classes = [
        call
        for call in defines_relationships
        if "compiler_attributes" in call.args[0][2]
        and any(
            cls in call.args[2][2]
            for cls in ["PackedClass", "AttributedClass", "BaseClass", "DerivedClass"]
        )
    ]

    assert len(attributed_classes) >= 3, (
        f"Expected at least 3 attributed classes, found {len(attributed_classes)}"
    )


def test_attribute_combinations_and_edge_cases(
    cpp_attributes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test complex attribute combinations and edge cases."""
    test_file = cpp_attributes_project / "attribute_edge_cases.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <functional>
#include <memory>
#include <type_traits>

// Multiple standard attributes on same declaration
[[nodiscard, deprecated("Use newMultiAttributeFunction() instead"), maybe_unused]]
int multiAttributeFunction(int x) {
    return x * 2;
}

// Attributes on different parts of function signature
[[nodiscard]]
auto complexAttributeFunction(
    [[maybe_unused]] int param1,
    const std::string& param2
) noexcept [[deprecated("Complex signature deprecated")]] -> int {
    return static_cast<int>(param2.length());
}

// Attributes in template contexts
template<typename T>
[[nodiscard]] constexpr bool templateWithAttributes(
    [[maybe_unused]] T&& value,
    [[maybe_unused]] int context = 0
) noexcept {
    if constexpr (std::is_integral_v<T>) {
        return true;
    } else [[likely]] {
        return false;
    }
}

// Conditional attributes based on compilation context
#ifdef DEBUG
    #define DEBUG_DEPRECATED [[deprecated("Debug-only function")]]
    #define DEBUG_MAYBE_UNUSED [[maybe_unused]]
#else
    #define DEBUG_DEPRECATED
    #define DEBUG_MAYBE_UNUSED
#endif

DEBUG_DEPRECATED
void debugOnlyFunction(DEBUG_MAYBE_UNUSED int debugLevel) {
    std::cout << "Debug function" << std::endl;
}

// Attributes on lambda expressions
void testLambdaAttributes() {
    // Lambda with attributes on parameters
    auto lambda1 = [](
        [[maybe_unused]] int unused_param,
        int used_param
    ) [[nodiscard]] -> int {
        return used_param * 2;
    };

    // Lambda with attributes on capture
    int captured = 42;
    auto lambda2 = [captured]
    [[deprecated("Use lambda3 instead")]]
    [[nodiscard]] (int x) -> int {
        return captured + x;
    };

    // Use lambdas
    int result1 = lambda1(0, 5);     // maybe_unused param, nodiscard result
    int result2 = lambda2(10);       // deprecated + nodiscard

    lambda1(1, 2);  // Warning: discarding nodiscard result
}

// Attributes on structured bindings (C++17)
void testStructuredBindingAttributes() {
    std::pair<int, std::string> data{42, "test"};

    // Attributes on structured binding declaration
    [[maybe_unused]] auto [value, text] = data;

    // Only use one binding
    std::cout << "Text: " << text << std::endl;
    // 'value' is maybe_unused, so no warning
}

// Custom attribute-like macros
#define FORCE_INLINE __attribute__((always_inline)) inline
#define NO_RETURN [[noreturn]]
#define PURE_FUNCTION [[nodiscard]] __attribute__((pure))

FORCE_INLINE
int customInlineFunction(int x) {
    return x + 1;
}

NO_RETURN
void terminateProgram() {
    std::terminate();
}

PURE_FUNCTION
int customPureFunction(int a, int b) {
    return a * b + 1;
}

// Attributes on member function pointers
class AttributePointerTest {
public:
    [[nodiscard]] int normalMethod() const { return 42; }

    [[deprecated("Use newMethod() instead")]]
    void oldMethod() const {}

    void newMethod() const {}
};

void testMemberFunctionPointers() {
    AttributePointerTest obj;

    // Pointer to attributed member function
    auto normalPtr = &AttributePointerTest::normalMethod;
    auto oldPtr = &AttributePointerTest::oldMethod;
    auto newPtr = &AttributePointerTest::newMethod;

    // Call through pointers
    int result = (obj.*normalPtr)();  // nodiscard
    (obj.*oldPtr)();                  // deprecated
    (obj.*newPtr)();                  // normal

    // Discard nodiscard result
    (obj.*normalPtr)();  // Warning: discarding nodiscard
}

// Attributes on friend functions
class FriendAttributeTest {
    friend [[deprecated("Friend function deprecated")]]
    void deprecatedFriend(const FriendAttributeTest& obj) {
        std::cout << "Deprecated friend: " << obj.value_ << std::endl;
    }

    friend [[nodiscard]]
    bool modernFriend(const FriendAttributeTest& obj) {
        return obj.value_ > 0;
    }

private:
    int value_ = 100;
};

void testFriendAttributes() {
    FriendAttributeTest obj;

    deprecatedFriend(obj);    // Warning: deprecated
    bool result = modernFriend(obj);  // nodiscard
    modernFriend(obj);        // Warning: discarding nodiscard
}

// Attributes on type aliases and using declarations
[[deprecated("Use ModernType instead")]]
using LegacyType = int;

using ModernType = int;

[[maybe_unused]]
using OptionalType = std::optional<int>;

// Attributes on variable templates
template<typename T>
[[maybe_unused]] constexpr bool is_special_v = false;

template<>
constexpr bool is_special_v<int> = true;

// Attributes on concepts (C++20)
#if __cpp_concepts >= 201907L
template<typename T>
[[deprecated("Use modern_concept instead")]]
concept legacy_concept = std::is_integral_v<T>;

template<typename T>
concept modern_concept = std::is_arithmetic_v<T>;
#endif

// Function with attributes on exception specification
[[nodiscard]]
int riskyFunction() noexcept(false) [[deprecated("May throw exceptions")]] {
    if (rand() % 2) {
        throw std::runtime_error("Random error");
    }
    return 42;
}

// Nested attribute scenarios
namespace [[deprecated("Legacy namespace")]] legacy {
    [[deprecated("Nested deprecated function")]]
    void nestedDeprecated() {
        std::cout << "Doubly deprecated" << std::endl;
    }

    class [[deprecated("Nested deprecated class")]] NestedClass {
    public:
        [[deprecated("Triply nested deprecated method")]]
        void method() const {
            std::cout << "Triply deprecated" << std::endl;
        }
    };
}

// Comprehensive demonstration function
void demonstrateAttributeEdgeCases() {
    // Test multi-attribute function
    int multi = multiAttributeFunction(5);    // deprecated + nodiscard
    multiAttributeFunction(10);               // Warning: discarding nodiscard

    // Test complex signature attributes
    int complex = complexAttributeFunction(0, "test");  // deprecated + nodiscard

    // Test template attributes
    bool intResult = templateWithAttributes(42);        // likely branch
    bool stringResult = templateWithAttributes(std::string("test"));  // unlikely branch

    // Test debug attributes
    debugOnlyFunction(3);  // May be deprecated in debug builds

    // Test lambda attributes
    testLambdaAttributes();

    // Test structured bindings
    testStructuredBindingAttributes();

    // Test custom attribute macros
    int inlined = customInlineFunction(5);
    int pure = customPureFunction(3, 4);

    // terminateProgram();  // Would terminate - commented out

    // Test member function pointer attributes
    testMemberFunctionPointers();

    // Test friend function attributes
    testFriendAttributes();

    // Test type alias attributes
    LegacyType legacy = 42;     // Warning: deprecated type
    ModernType modern = 24;     // OK

    // Test risky function
    try {
        int risky = riskyFunction();  // deprecated + nodiscard + may throw
        std::cout << "Risky result: " << risky << std::endl;
    } catch (const std::exception& e) {
        std::cerr << "Caught: " << e.what() << std::endl;
    }

    // Test nested deprecations
    legacy::nestedDeprecated();       // Deprecated namespace + function
    legacy::NestedClass nested;       // Deprecated namespace + class
    nested.method();                  // Deprecated namespace + class + method

#if __cpp_concepts >= 201907L
    // Test concept attributes
    static_assert(legacy_concept<int>);   // Warning: deprecated concept
    static_assert(modern_concept<int>);   // OK
#endif
}

// Final comprehensive test with all attribute types
[[nodiscard]] [[deprecated("Ultimate test function")]]
__attribute__((cold)) __attribute__((warn_unused_result))
bool ultimateAttributeTest(
    [[maybe_unused]] int param1,
    const std::string& param2
) noexcept [[likely]] {

    if (param2.empty()) [[unlikely]] {
        return false;
    }

    switch (param2[0]) {
        case 'a':
            [[fallthrough]];
        case 'e':
            return true;
        default:
            return false;
    }
}

void testUltimateAttributes() {
    // Use ultimate function
    bool result = ultimateAttributeTest(0, "test");  // All attributes apply

    // Ignore result - multiple warnings
    ultimateAttributeTest(1, "example");  // Deprecated + nodiscard + warn_unused_result
}
""",
    )

    run_updater(cpp_attributes_project, mock_ingestor)

    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    call_relationships = get_relationships(mock_ingestor, "CALLS")

    edge_case_functions = [
        call
        for call in defines_relationships
        if "attribute_edge_cases" in call.args[0][2]
        and any(
            func in call.args[2][2]
            for func in [
                "multiAttributeFunction",
                "complexAttributeFunction",
                "templateWithAttributes",
                "debugOnlyFunction",
                "customInlineFunction",
                "ultimateAttributeTest",
            ]
        )
    ]

    assert len(edge_case_functions) >= 5, (
        f"Expected at least 5 edge case attributed functions, found {len(edge_case_functions)}"
    )

    complex_calls = [
        call for call in call_relationships if "attribute_edge_cases" in call.args[0][2]
    ]

    assert len(complex_calls) >= 8, (
        f"Expected at least 8 complex attribute-related calls, found {len(complex_calls)}"
    )


def test_cpp_attributes_comprehensive(
    cpp_attributes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test combining all C++ attribute patterns."""
    test_file = cpp_attributes_project / "comprehensive_attributes.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Comprehensive C++ attributes test combining all patterns
#include <iostream>
#include <memory>
#include <vector>
#include <optional>
#include <functional>

// Standard attributes section
[[nodiscard]] int getValue() { return 42; }
[[maybe_unused]] static int unusedVar = 0;
[[deprecated("Use getNewValue()")]] int getOldValue() { return 24; }

// Compiler-specific attributes section
__attribute__((always_inline)) inline void fastCode() {}
__attribute__((cold)) void errorPath() {}
__attribute__((pure)) int pureCalc(int x) { return x * x; }

// Complex combinations
[[nodiscard]] __attribute__((warn_unused_result))
bool criticalFunction([[maybe_unused]] int debug) {
    return true;
}

// Template with attributes
template<typename T>
[[nodiscard]] constexpr T processValue(T value) noexcept {
    if constexpr (std::is_integral_v<T>) {
        return value * 2;
    } else [[likely]] {
        return value;
    }
}

// Class with comprehensive attributes
class [[deprecated("Use ModernClass")]] AttributedClass {
public:
    [[nodiscard]] bool isValid() const noexcept { return valid_; }

    [[deprecated("Use setStatus()")]]
    void setValid([[maybe_unused]] bool v) { valid_ = v; }

    __attribute__((hot))
    void frequentMethod() const { /* frequently called */ }

private:
    [[maybe_unused]] static constexpr int VERSION = 1;
    bool valid_ = true;
};

// Control flow attributes
std::string switchWithAttributes(int value) {
    switch (value) {
        case 1:
            if (value > 0) [[likely]] {
                return "positive";
            }
            [[fallthrough]];
        case 2:
            return "two";
        default:
            if (value < 0) [[unlikely]] {
                return "negative";
            }
            return "default";
    }
}

void comprehensiveAttributeDemo() {
    // Standard attributes
    int val = getValue();           // nodiscard
    getValue();                     // Warning: discarded
    int old = getOldValue();        // deprecated

    // Compiler-specific
    fastCode();                     // always_inline
    pureCalc(5);                   // pure function

    // Combined attributes
    bool critical = criticalFunction(1);  // nodiscard + warn_unused_result
    criticalFunction(2);           // Multiple warnings

    // Template attributes
    auto intResult = processValue<int>(10);     // likely branch
    auto floatResult = processValue<float>(2.5f); // unlikely branch

    // Class attributes
    AttributedClass obj;           // deprecated class
    bool valid = obj.isValid();    // nodiscard method
    obj.setValid(true);           // deprecated method
    obj.frequentMethod();         // hot method

    // Control flow
    std::string result = switchWithAttributes(1);  // likely/unlikely/fallthrough

    // Error path testing
    try {
        throw std::runtime_error("Test");
    } catch (...) [[unlikely]] {
        errorPath();              // cold function in unlikely catch
    }
}

// Attribute inheritance and polymorphism
class BaseWithAttributes {
public:
    virtual ~BaseWithAttributes() = default;
    [[nodiscard]] virtual bool virtualAttributed() const = 0;

    __attribute__((hot))
    void baseHotMethod() const {}
};

class DerivedWithAttributes : public BaseWithAttributes {
public:
    [[nodiscard]] bool virtualAttributed() const override { return true; }

    [[deprecated("Override in further derived classes")]]
    virtual void deprecatedVirtual() const {}
};

void testAttributePolymorphism() {
    DerivedWithAttributes derived;
    BaseWithAttributes* base = &derived;

    bool result = base->virtualAttributed();  // Virtual + nodiscard
    base->baseHotMethod();                   // Hot method
    derived.deprecatedVirtual();             // Deprecated virtual

    // Polymorphic collection
    std::vector<std::unique_ptr<BaseWithAttributes>> objects;
    objects.push_back(std::make_unique<DerivedWithAttributes>());

    for (const auto& obj : objects) {
        bool val = obj->virtualAttributed();  // Virtual dispatch + nodiscard
        obj->baseHotMethod();                // Hot method call
    }
}
""",
    )

    run_updater(cpp_attributes_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_attributes" in call.args[0][2]
    ]

    comprehensive_defines = [
        call
        for call in defines_relationships
        if "comprehensive_attributes" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 10, (
        f"Expected at least 10 comprehensive attribute calls, found {len(comprehensive_calls)}"
    )

    assert len(comprehensive_defines) >= 8, (
        f"Expected at least 8 comprehensive attribute definitions, found {len(comprehensive_defines)}"
    )

    attribute_inherits = [
        call
        for call in inherits_relationships
        if "comprehensive_attributes" in call.args[0][2]
    ]

    assert len(attribute_inherits) >= 1, (
        f"Expected at least 1 inheritance with attributes, found {len(attribute_inherits)}"
    )
