from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_namespaces_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with namespace patterns."""
    project_path = temp_repo / "cpp_namespaces_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_basic_namespaces(
    cpp_namespaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic namespace declarations and usage."""
    test_file = cpp_namespaces_project / "basic_namespaces.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>

// Global namespace function
void globalFunction() {
    std::cout << "Global function called" << std::endl;
}

// Basic namespace
namespace utils {
    void printMessage(const std::string& message) {
        std::cout << "Utils: " << message << std::endl;
    }

    class Logger {
    public:
        static void log(const std::string& message) {
            std::cout << "[LOG] " << message << std::endl;
        }

        void info(const std::string& message) {
            std::cout << "[INFO] " << message << std::endl;
        }

        void error(const std::string& message) {
            std::cout << "[ERROR] " << message << std::endl;
        }
    };

    const int MAX_BUFFER_SIZE = 1024;

    double calculateArea(double radius) {
        const double PI = 3.14159;
        return PI * radius * radius;
    }
}

// Another namespace
namespace graphics {
    struct Point {
        double x, y;
        Point(double x = 0, double y = 0) : x(x), y(y) {}
    };

    struct Color {
        int r, g, b;
        Color(int r = 0, int g = 0, int b = 0) : r(r), g(g), b(b) {}
    };

    class Shape {
    public:
        Shape(const Point& pos, const Color& col) : position_(pos), color_(col) {}
        virtual ~Shape() = default;

        virtual void draw() const = 0;
        virtual double area() const = 0;

        Point getPosition() const { return position_; }
        Color getColor() const { return color_; }

    protected:
        Point position_;
        Color color_;
    };

    class Circle : public Shape {
    private:
        double radius_;

    public:
        Circle(const Point& pos, const Color& col, double radius)
            : Shape(pos, col), radius_(radius) {}

        void draw() const override {
            std::cout << "Drawing circle at (" << position_.x << ", " << position_.y
                      << ") with radius " << radius_ << std::endl;
        }

        double area() const override {
            return utils::calculateArea(radius_);  // Cross-namespace call
        }

        double getRadius() const { return radius_; }
    };
}

// Math utilities namespace
namespace math {
    namespace constants {
        const double PI = 3.141592653589793;
        const double E = 2.718281828459045;
    }

    double add(double a, double b) {
        return a + b;
    }

    double multiply(double a, double b) {
        return a * b;
    }

    class Calculator {
    public:
        static double compute(double a, double b, char op) {
            switch (op) {
                case '+': return add(a, b);
                case '*': return multiply(a, b);
                default: return 0.0;
            }
        }

        double power(double base, int exponent) {
            double result = 1.0;
            for (int i = 0; i < exponent; ++i) {
                result = multiply(result, base);
            }
            return result;
        }
    };
}

void demonstrateBasicNamespaces() {
    // Global function call
    globalFunction();

    // Qualified namespace calls
    utils::printMessage("Hello from utils namespace");
    utils::Logger::log("Static method call");

    utils::Logger logger;
    logger.info("Instance method call");
    logger.error("Error message");

    // Cross-namespace usage
    graphics::Point origin(0, 0);
    graphics::Color red(255, 0, 0);
    graphics::Circle circle(origin, red, 5.0);

    circle.draw();
    double area = circle.area();  // This calls utils::calculateArea internally
    std::cout << "Circle area: " << area << std::endl;

    // Math namespace usage
    double sum = math::add(10.0, 20.0);
    double product = math::multiply(5.0, 6.0);

    math::Calculator calc;
    double power = calc.power(2.0, 8);
    double computed = math::Calculator::compute(15.0, 3.0, '+');

    std::cout << "Sum: " << sum << std::endl;
    std::cout << "Product: " << product << std::endl;
    std::cout << "2^8 = " << power << std::endl;
    std::cout << "Computed: " << computed << std::endl;

    // Access nested namespace
    std::cout << "PI from math::constants: " << math::constants::PI << std::endl;
    std::cout << "E from math::constants: " << math::constants::E << std::endl;
}
""",
    )

    run_updater(cpp_namespaces_project, mock_ingestor)

    project_name = cpp_namespaces_project.name

    expected_classes = [
        f"{project_name}.basic_namespaces.utils.Logger",
        f"{project_name}.basic_namespaces.graphics.Point",
        f"{project_name}.basic_namespaces.graphics.Shape",
        f"{project_name}.basic_namespaces.graphics.Circle",
        f"{project_name}.basic_namespaces.math.Calculator",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )

    expected_functions = [
        f"{project_name}.basic_namespaces.globalFunction",
        f"{project_name}.basic_namespaces.utils.printMessage",
        f"{project_name}.basic_namespaces.utils.calculateArea",
        f"{project_name}.basic_namespaces.math.add",
        f"{project_name}.basic_namespaces.math.multiply",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_using_directives(
    cpp_namespaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test using directives and namespace aliases."""
    test_file = cpp_namespaces_project / "using_directives.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <vector>

namespace company {
    namespace project {
        namespace module {
            class DataProcessor {
            public:
                void process(const std::string& data) {
                    std::cout << "Processing: " << data << std::endl;
                }

                std::vector<std::string> split(const std::string& input, char delimiter) {
                    std::vector<std::string> result;
                    // Simplified split implementation
                    result.push_back(input);
                    return result;
                }
            };

            void utility_function() {
                std::cout << "Utility function called" << std::endl;
            }

            const int VERSION = 1;
        }
    }
}

namespace utils {
    void helper() {
        std::cout << "Helper function" << std::endl;
    }

    class StringUtils {
    public:
        static std::string uppercase(const std::string& str) {
            std::string result = str;
            // Simplified uppercase implementation
            return result;
        }

        static bool isEmpty(const std::string& str) {
            return str.empty();
        }
    };
}

// Using namespace directive
using namespace std;

// Using specific declarations
using utils::helper;
using utils::StringUtils;

// Namespace alias
namespace cpm = company::project::module;

void demonstrateUsingDirectives() {
    // Can use std types without std:: prefix due to "using namespace std"
    string message = "Hello, World!";
    vector<int> numbers = {1, 2, 3, 4, 5};

    cout << "Message: " << message << endl;
    cout << "Numbers count: " << numbers.size() << endl;

    // Use function brought into scope with using declaration
    helper();  // Instead of utils::helper()

    // Use class brought into scope with using declaration
    string upper = StringUtils::uppercase("hello");  // Instead of utils::StringUtils::uppercase
    bool empty = StringUtils::isEmpty("");

    cout << "Uppercase: " << upper << endl;
    cout << "Is empty: " << (empty ? "yes" : "no") << endl;

    // Use namespace alias
    cpm::DataProcessor processor;  // Instead of company::project::module::DataProcessor
    processor.process("test data");

    auto parts = processor.split("a,b,c", ',');
    cout << "Split result count: " << parts.size() << endl;

    cpm::utility_function();  // Instead of company::project::module::utility_function
    cout << "Version: " << cpm::VERSION << endl;

    // Still can use fully qualified names
    company::project::module::DataProcessor fullProcessor;
    fullProcessor.process("fully qualified");
}

// Scoped using directives
void scopedUsingExample() {
    {
        using namespace utils;

        // Within this scope, can use utils members directly
        helper();
        string result = StringUtils::uppercase("scoped");
        cout << "Scoped result: " << result << endl;
    }

    // Outside the scope, need qualification again
    utils::helper();
    utils::StringUtils stringUtils;
}

// Using directive in function
void functionScopedUsing() {
    using company::project::module::DataProcessor;
    using company::project::module::utility_function;

    DataProcessor processor;
    processor.process("function scoped");

    utility_function();
}

// Class with using declarations
class MyClass {
public:
    using ProcessorType = company::project::module::DataProcessor;
    using StringType = std::string;

    void useTypes() {
        ProcessorType processor;
        StringType data = "class scoped types";
        processor.process(data);
    }

private:
    ProcessorType processor_;
    StringType name_;
};

void demonstrateClassUsing() {
    MyClass obj;
    obj.useTypes();
}

// Template with using
template<typename T>
class Container {
public:
    using value_type = T;
    using size_type = size_t;

    void add(const value_type& item) {
        // Add implementation
        cout << "Adding item to container" << endl;
    }

    size_type size() const {
        return 0;  // Simplified
    }
};

void demonstrateTemplateUsing() {
    Container<string> stringContainer;
    Container<string>::value_type item = "test";
    stringContainer.add(item);

    Container<string>::size_type count = stringContainer.size();
    cout << "Container size: " << count << endl;
}
""",
    )

    run_updater(cpp_namespaces_project, mock_ingestor)

    project_name = cpp_namespaces_project.name

    expected_nested_classes = [
        f"{project_name}.using_directives.company.project.module.DataProcessor",
        f"{project_name}.using_directives.utils.StringUtils",
        f"{project_name}.using_directives.MyClass",
        f"{project_name}.using_directives.Container",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_nested_classes = [
        cls for cls in expected_nested_classes if cls in created_classes
    ]
    assert len(found_nested_classes) >= 2, (
        f"Expected at least 2 nested namespace classes, found {len(found_nested_classes)}: {found_nested_classes}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    namespace_calls = [
        call
        for call in call_relationships
        if "using_directives" in call.args[0][2]
        and any(
            name in call.args[2][2]
            for name in ["helper", "process", "utility_function", "uppercase"]
        )
    ]

    assert len(namespace_calls) >= 4, (
        f"Expected at least 4 namespace-related calls, found {len(namespace_calls)}"
    )


def test_anonymous_namespaces(
    cpp_namespaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test anonymous namespaces and internal linkage."""
    test_file = cpp_namespaces_project / "anonymous_namespaces.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>

// Anonymous namespace (internal linkage)
namespace {
    void internal_function() {
        std::cout << "Internal function called" << std::endl;
    }

    class InternalClass {
    public:
        void method() {
            std::cout << "InternalClass method called" << std::endl;
        }

        static void static_method() {
            std::cout << "InternalClass static method called" << std::endl;
        }
    };

    const int INTERNAL_CONSTANT = 42;

    // Nested anonymous namespace
    namespace {
        void deeply_internal() {
            std::cout << "Deeply internal function" << std::endl;
        }
    }
}

// Named namespace with anonymous namespace inside
namespace outer {
    namespace {
        void outer_internal() {
            std::cout << "Outer internal function" << std::endl;
        }

        class OuterInternalClass {
        public:
            void process() {
                std::cout << "OuterInternalClass processing" << std::endl;
            }
        };
    }

    void public_function() {
        std::cout << "Outer public function" << std::endl;
        outer_internal();  // Can call anonymous namespace function
    }

    class PublicClass {
    public:
        void use_internal() {
            OuterInternalClass internal;
            internal.process();
        }
    };
}

// Another file-level anonymous namespace
namespace {
    class AnotherInternalClass {
    public:
        void work() {
            std::cout << "AnotherInternalClass working" << std::endl;
        }
    };

    void another_internal_function() {
        std::cout << "Another internal function" << std::endl;
    }
}

void demonstrateAnonymousNamespaces() {
    // Can use anonymous namespace members directly
    internal_function();
    deeply_internal();
    another_internal_function();

    InternalClass internal;
    internal.method();
    InternalClass::static_method();

    AnotherInternalClass another;
    another.work();

    std::cout << "Internal constant: " << INTERNAL_CONSTANT << std::endl;

    // Use named namespace with internal anonymous members
    outer::public_function();  // This calls outer_internal() internally

    outer::PublicClass publicObj;
    publicObj.use_internal();  // This uses OuterInternalClass internally
}

// Template in anonymous namespace
namespace {
    template<typename T>
    class InternalTemplate {
    public:
        void process(const T& value) {
            std::cout << "Processing internal template with value" << std::endl;
        }
    };

    template<typename T>
    T internal_max(T a, T b) {
        return (a > b) ? a : b;
    }
}

void demonstrateInternalTemplates() {
    InternalTemplate<int> intTemplate;
    intTemplate.process(42);

    InternalTemplate<std::string> stringTemplate;
    stringTemplate.process("hello");

    int max_int = internal_max(10, 20);
    double max_double = internal_max(3.14, 2.71);

    std::cout << "Max int: " << max_int << std::endl;
    std::cout << "Max double: " << max_double << std::endl;
}

// Static vs anonymous namespace comparison
static void static_function() {
    std::cout << "Static function (C-style internal linkage)" << std::endl;
}

namespace {
    void modern_internal_function() {
        std::cout << "Anonymous namespace function (C++ style internal linkage)" << std::endl;
    }
}

void compareLinkageStyles() {
    static_function();  // C-style internal linkage
    modern_internal_function();  // C++ style internal linkage
}
""",
    )

    run_updater(cpp_namespaces_project, mock_ingestor)

    expected_internal_classes = [
        "InternalClass",
        "OuterInternalClass",
        "AnotherInternalClass",
        "InternalTemplate",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_internal_classes = [
        cls
        for cls in created_classes
        if any(internal_name in cls for internal_name in expected_internal_classes)
    ]

    assert len(found_internal_classes) >= 2, (
        f"Expected at least 2 anonymous namespace classes, found {len(found_internal_classes)}: {found_internal_classes}"
    )

    expected_internal_functions = [
        "internal_function",
        "deeply_internal",
        "outer_internal",
        "another_internal_function",
        "static_function",
        "modern_internal_function",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_internal_functions = [
        func
        for func in created_functions
        if any(internal_name in func for internal_name in expected_internal_functions)
    ]

    assert len(found_internal_functions) >= 4, (
        f"Expected at least 4 internal functions, found {len(found_internal_functions)}: {found_internal_functions}"
    )


def test_cpp_namespaces_comprehensive(
    cpp_namespaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all namespace patterns create proper relationships."""
    test_file = cpp_namespaces_project / "comprehensive_namespaces.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every C++ namespace pattern in one file
#include <iostream>
#include <string>

// Global namespace
void global_func() {
    std::cout << "Global function" << std::endl;
}

// Basic namespace
namespace basic {
    void func() {
        std::cout << "Basic namespace function" << std::endl;
    }

    class Class {
    public:
        void method() {
            std::cout << "Basic namespace class method" << std::endl;
        }
    };
}

// Nested namespaces
namespace outer {
    namespace inner {
        void nested_func() {
            std::cout << "Nested namespace function" << std::endl;
        }

        class NestedClass {
        public:
            void process() {
                std::cout << "Nested class processing" << std::endl;
            }
        };
    }

    void outer_func() {
        inner::nested_func();  // Cross-namespace call
    }
}

// Anonymous namespace
namespace {
    void internal_func() {
        std::cout << "Internal function" << std::endl;
    }

    class InternalClass {
    public:
        void work() {
            std::cout << "Internal class working" << std::endl;
        }
    };
}

// Using directives
using namespace std;
using basic::Class;
namespace alias = outer::inner;

void demonstrateComprehensiveNamespaces() {
    // Global namespace
    global_func();

    // Basic namespace - qualified calls
    basic::func();
    basic::Class basicObj;
    basicObj.method();

    // Nested namespace - qualified calls
    outer::inner::nested_func();
    outer::inner::NestedClass nestedObj;
    nestedObj.process();

    // Cross-namespace call
    outer::outer_func();  // This calls inner::nested_func internally

    // Anonymous namespace - direct calls
    internal_func();
    InternalClass internalObj;
    internalObj.work();

    // Using declarations
    Class usingObj;  // Uses basic::Class
    usingObj.method();

    // Namespace alias
    alias::NestedClass aliasObj;
    aliasObj.process();

    // std namespace usage (from using namespace std)
    string text = "Hello";
    cout << text << endl;
}

// Template in namespace
namespace templates {
    template<typename T>
    class Container {
    public:
        void store(const T& item) {
            cout << "Storing item in namespace template" << endl;
        }
    };

    template<typename T>
    T max_value(T a, T b) {
        return (a > b) ? a : b;
    }
}

// Namespace reopening
namespace basic {
    void additional_func() {
        cout << "Additional function in reopened namespace" << endl;
    }

    class AnotherClass {
    public:
        void work() {
            cout << "Another class in reopened namespace" << endl;
        }
    };
}

void testNamespaceFeatures() {
    // Template namespace usage
    templates::Container<int> container;
    container.store(42);

    int max_val = templates::max_value(10, 20);
    cout << "Max value: " << max_val << endl;

    // Reopened namespace
    basic::additional_func();
    basic::AnotherClass another;
    another.work();

    // All namespace types together
    basic::func();              // Basic namespace
    outer::inner::nested_func(); // Nested namespace
    internal_func();            // Anonymous namespace
    global_func();              // Global namespace
}
""",
    )

    run_updater(cpp_namespaces_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_namespaces" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 12, (
        f"Expected at least 12 comprehensive namespace calls, found {len(comprehensive_calls)}"
    )

    cross_namespace_calls = [
        call
        for call in comprehensive_calls
        if any(
            namespace_func in call.args[2][2]
            for namespace_func in [
                "basic.func",
                "inner.nested_func",
                "internal_func",
                "global_func",
            ]
        )
    ]

    assert len(cross_namespace_calls) >= 4, (
        f"Expected at least 4 cross-namespace calls, found {len(cross_namespace_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
