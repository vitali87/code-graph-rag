from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    get_node_names,
    get_nodes,
    get_qualified_names,
    get_relationships,
    run_updater,
)


@pytest.fixture
def cpp_basic_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with basic syntax patterns."""
    project_path = temp_repo / "cpp_basic_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()
    (project_path / "lib").mkdir()

    (project_path / "src" / "main.cpp").write_text(
        encoding="utf-8", data="int main() { return 0; }"
    )
    (project_path / "include" / "base.h").write_text(
        encoding="utf-8", data="#pragma once\nclass Base {};"
    )

    return project_path


def test_basic_class_declarations(
    cpp_basic_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic C++ class declaration parsing."""
    test_file = cpp_basic_project / "basic_classes.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic class declaration
class Person {
private:
    std::string name;
    int age;

public:
    Person(const std::string& name, int age);
    ~Person();

    void setName(const std::string& name);
    std::string getName() const;

    void setAge(int age);
    int getAge() const;

    void greet() const;
};

// Class with static members
class MathUtils {
public:
    static const double PI;
    static int instanceCount;

    static double add(double a, double b);
    static double multiply(double a, double b);
    static double calculateCircleArea(double radius);

    MathUtils();
    ~MathUtils();

private:
    static void incrementCount();
};

// Struct definition (class in C++)
struct Point {
    double x, y;

    Point();
    Point(double x, double y);

    double distance(const Point& other) const;
    Point operator+(const Point& other) const;
};

// Class with member functions
class Rectangle {
public:
    Rectangle(double width, double height);

    double getWidth() const { return width_; }
    double getHeight() const { return height_; }

    double area() const;
    double perimeter() const;

    void resize(double width, double height);
    bool isSquare() const;

private:
    double width_;
    double height_;

    void validateDimensions();
};

// Enum class (C++11)
enum class Color {
    RED,
    GREEN,
    BLUE,
    YELLOW
};

// Traditional enum
enum Status {
    ACTIVE,
    INACTIVE,
    PENDING
};

// Using classes and functions
void demonstrateClasses() {
    Person person("Alice", 30);
    person.greet();

    double sum = MathUtils::add(5.0, 3.0);
    double area = MathUtils::calculateCircleArea(10.0);

    Point p1(0, 0);
    Point p2(3, 4);
    double dist = p1.distance(p2);
    Point p3 = p1 + p2;

    Rectangle rect(10.0, 20.0);
    double rectArea = rect.area();
    bool isSquare = rect.isSquare();

    Color color = Color::RED;
    Status status = ACTIVE;
}
""",
    )

    run_updater(cpp_basic_project, mock_ingestor)

    project_name = cpp_basic_project.name

    expected_classes = [
        f"{project_name}.basic_classes.Person",
        f"{project_name}.basic_classes.MathUtils",
        f"{project_name}.basic_classes.Point",
        f"{project_name}.basic_classes.Rectangle",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    for expected_qn in expected_classes:
        assert expected_qn in created_classes, f"Missing class: {expected_qn}"

    expected_methods = [
        f"{project_name}.basic_classes.Person.Person",
        f"{project_name}.basic_classes.Person.setName",
        f"{project_name}.basic_classes.Person.getName",
        f"{project_name}.basic_classes.Person.greet",
        f"{project_name}.basic_classes.MathUtils.add",
        f"{project_name}.basic_classes.MathUtils.calculateCircleArea",
        f"{project_name}.basic_classes.Point.distance",
        f"{project_name}.basic_classes.Rectangle.area",
    ]

    expected_functions = [
        f"{project_name}.basic_classes.demonstrateClasses",
    ]

    method_calls = get_nodes(mock_ingestor, "Method")

    function_calls = get_nodes(mock_ingestor, "Function")

    created_methods = get_qualified_names(method_calls)
    created_functions = get_qualified_names(function_calls)

    missing_methods = set(expected_methods) - created_methods
    assert not missing_methods, (
        f"Missing expected methods: {sorted(list(missing_methods))}"
    )

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_basic_function_declarations(
    cpp_basic_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic C++ function declaration parsing."""
    test_file = cpp_basic_project / "basic_functions.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <vector>

// Global function declarations
int add(int a, int b);
double multiply(double x, double y);
void printMessage(const std::string& message);

// Function with default parameters
void greet(const std::string& name = "World");

// Function overloading
int max(int a, int b);
double max(double a, double b);
std::string max(const std::string& a, const std::string& b);

// Template function declaration
template<typename T>
T minimum(T a, T b);

// Function with complex return types
std::vector<int> createRange(int start, int end);
const std::string& getLongestString(const std::vector<std::string>& strings);

// Function implementations
int add(int a, int b) {
    return a + b;
}

double multiply(double x, double y) {
    return x * y;
}

void printMessage(const std::string& message) {
    std::cout << message << std::endl;
}

void greet(const std::string& name) {
    printMessage("Hello, " + name + "!");
}

int max(int a, int b) {
    return (a > b) ? a : b;
}

double max(double a, double b) {
    return (a > b) ? a : b;
}

std::string max(const std::string& a, const std::string& b) {
    return (a > b) ? a : b;
}

template<typename T>
T minimum(T a, T b) {
    return (a < b) ? a : b;
}

std::vector<int> createRange(int start, int end) {
    std::vector<int> result;
    for (int i = start; i <= end; ++i) {
        result.push_back(i);
    }
    return result;
}

// Function with function calls
void demonstrateFunctions() {
    int sum = add(10, 20);
    double product = multiply(3.14, 2.0);

    printMessage("Demonstrating functions");
    greet("C++");

    int maxInt = max(100, 200);
    double maxDouble = max(1.5, 2.7);

    auto minVal = minimum<int>(5, 10);
    std::vector<int> range = createRange(1, 10);
}
""",
    )

    run_updater(cpp_basic_project, mock_ingestor)

    project_name = cpp_basic_project.name

    expected_functions = [
        f"{project_name}.basic_functions.add",
        f"{project_name}.basic_functions.multiply",
        f"{project_name}.basic_functions.printMessage",
        f"{project_name}.basic_functions.greet",
        f"{project_name}.basic_functions.max",
        f"{project_name}.basic_functions.minimum",
        f"{project_name}.basic_functions.createRange",
        f"{project_name}.basic_functions.demonstrateFunctions",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    function_calls_relationships = [
        call
        for call in call_relationships
        if "basic_functions" in call.args[0][2]
        and any(
            func_name in call.args[2][2]
            for func_name in ["add", "multiply", "printMessage", "greet", "max"]
        )
    ]

    assert len(function_calls_relationships) >= 3, (
        f"Expected at least 3 function call relationships, found {len(function_calls_relationships)}"
    )


def test_basic_namespaces(
    cpp_basic_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic C++ namespace parsing and qualified names."""
    test_file = cpp_basic_project / "basic_namespaces.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Global namespace functions
void globalFunction();

// Basic namespace
namespace utils {
    void printDebug(const std::string& message);

    class Logger {
    public:
        static void log(const std::string& message);
        void info(const std::string& message);
        void error(const std::string& message);
    };

    namespace math {
        const double PI = 3.14159;

        double add(double a, double b);
        double subtract(double a, double b);

        class Calculator {
        public:
            double compute(double a, double b, char op);
            void reset();
        private:
            double result_;
        };
    }
}

// Another namespace
namespace graphics {
    struct Point {
        double x, y;
        Point(double x = 0, double y = 0);
    };

    struct Color {
        int r, g, b;
        Color(int r = 0, int g = 0, int b = 0);
    };

    class Shape {
    public:
        Shape(const Point& position);
        virtual ~Shape();

        virtual double area() const = 0;
        virtual void draw() const = 0;

        Point getPosition() const;
        void setPosition(const Point& pos);

    protected:
        Point position_;
    };

    class Circle : public Shape {
    public:
        Circle(const Point& center, double radius);

        double area() const override;
        void draw() const override;

        double getRadius() const;

    private:
        double radius_;
    };
}

// Using namespace declarations and qualified names
void demonstrateNamespaces() {
    // Qualified names
    utils::printDebug("Debug message");
    utils::Logger logger;
    logger.info("Info message");
    utils::Logger::log("Static log message");

    // Nested namespace access
    double sum = utils::math::add(10.0, 20.0);
    utils::math::Calculator calc;
    double result = calc.compute(5.0, 3.0, '+');

    // Different namespace
    graphics::Point origin(0, 0);
    graphics::Color red(255, 0, 0);
    graphics::Circle circle(origin, 5.0);
    double area = circle.area();
    circle.draw();
}

// Using directive
using namespace utils;
using utils::math::Calculator;

void demonstrateUsingDirectives() {
    printDebug("Using directive test");
    Logger logger;
    logger.error("Error message");

    Calculator calc;
    calc.reset();
}
""",
    )

    run_updater(cpp_basic_project, mock_ingestor)

    project_name = cpp_basic_project.name

    expected_classes = [
        f"{project_name}.basic_namespaces.utils.Logger",
        f"{project_name}.basic_namespaces.utils.math.Calculator",
        f"{project_name}.basic_namespaces.graphics.Point",
        f"{project_name}.basic_namespaces.graphics.Shape",
        f"{project_name}.basic_namespaces.graphics.Circle",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )

    expected_methods = [
        f"{project_name}.basic_namespaces.graphics.Circle.area",
    ]

    expected_functions = [
        f"{project_name}.basic_namespaces.globalFunction",
        f"{project_name}.basic_namespaces.utils.printDebug",
        f"{project_name}.basic_namespaces.utils.math.add",
        f"{project_name}.basic_namespaces.demonstrateNamespaces",
    ]

    method_calls = get_nodes(mock_ingestor, "Method")

    function_calls = get_nodes(mock_ingestor, "Function")

    created_methods = get_qualified_names(method_calls)
    created_functions = get_qualified_names(function_calls)

    missing_methods = set(expected_methods) - created_methods
    assert not missing_methods, (
        f"Missing expected methods: {sorted(list(missing_methods))}"
    )

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_basic_member_functions(
    cpp_basic_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test C++ member function calls and method relationships."""
    test_file = cpp_basic_project / "member_functions.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
class BankAccount {
private:
    double balance_;
    std::string accountNumber_;

public:
    BankAccount(const std::string& accountNumber, double initialBalance = 0.0);
    ~BankAccount();

    // Accessor methods
    double getBalance() const;
    std::string getAccountNumber() const;

    // Mutator methods
    void deposit(double amount);
    bool withdraw(double amount);
    void transfer(BankAccount& toAccount, double amount);

    // Utility methods
    void printStatement() const;
    bool isOverdrawn() const;

private:
    void validateAmount(double amount) const;
    void updateBalance(double newBalance);
};

class SavingsAccount : public BankAccount {
private:
    double interestRate_;

public:
    SavingsAccount(const std::string& accountNumber,
                   double initialBalance = 0.0,
                   double interestRate = 0.01);

    // Override methods
    void printStatement() const override;

    // New methods
    void calculateInterest();
    double getInterestRate() const;
    void setInterestRate(double rate);

private:
    void compoundInterest();
};

// Implementation with method calls
BankAccount::BankAccount(const std::string& accountNumber, double initialBalance)
    : accountNumber_(accountNumber), balance_(initialBalance) {
    validateAmount(initialBalance);
}

void BankAccount::deposit(double amount) {
    validateAmount(amount);
    updateBalance(balance_ + amount);
    printStatement();
}

bool BankAccount::withdraw(double amount) {
    validateAmount(amount);

    if (balance_ >= amount) {
        updateBalance(balance_ - amount);
        printStatement();
        return true;
    }
    return false;
}

void BankAccount::transfer(BankAccount& toAccount, double amount) {
    if (withdraw(amount)) {
        toAccount.deposit(amount);
    }
}

void SavingsAccount::calculateInterest() {
    double interest = getBalance() * interestRate_;
    deposit(interest);
    compoundInterest();
}

// Function demonstrating method calls
void bankingDemo() {
    BankAccount checking("CHK-001", 1000.0);
    SavingsAccount savings("SAV-001", 5000.0, 0.025);

    // Method calls on objects
    double checkingBalance = checking.getBalance();
    checking.deposit(500.0);
    bool success = checking.withdraw(200.0);

    // Method calls on different object types
    double savingsBalance = savings.getBalance();
    savings.calculateInterest();
    savings.setInterestRate(0.03);

    // Transfer between accounts
    checking.transfer(savings, 300.0);

    // Polymorphic method calls
    BankAccount* account = &savings;
    account->printStatement();  // Calls SavingsAccount::printStatement
}
""",
    )

    run_updater(cpp_basic_project, mock_ingestor)

    project_name = cpp_basic_project.name

    expected_classes = [
        f"{project_name}.member_functions.BankAccount",
        f"{project_name}.member_functions.SavingsAccount",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    for expected_qn in expected_classes:
        assert expected_qn in created_classes, f"Missing class: {expected_qn}"

    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    inheritance_found = any(
        "SavingsAccount" in call[0][0][2] and "BankAccount" in call[0][2][2]
        for call in relationship_calls
    )
    assert inheritance_found, (
        "Expected inheritance relationship SavingsAccount -> BankAccount"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    method_call_relationships = [
        call
        for call in call_relationships
        if "member_functions" in call.args[0][2]
        and any(
            method_name in call.args[2][2]
            for method_name in [
                "deposit",
                "withdraw",
                "getBalance",
                "transfer",
                "calculateInterest",
            ]
        )
    ]

    assert len(method_call_relationships) >= 5, (
        f"Expected at least 5 method call relationships, found {len(method_call_relationships)}"
    )


def test_cpp_basic_comprehensive(
    cpp_basic_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all basic C++ patterns create proper relationships."""
    test_file = cpp_basic_project / "comprehensive_basic.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every basic C++ pattern in one file
#include <iostream>
#include <string>
#include <vector>

// Forward declarations
class Vehicle;
void globalUtility();

// Basic namespace
namespace app {
    // Global function in namespace
    void initialize();

    // Basic class
    class Animal {
    protected:
        std::string name_;

    public:
        Animal(const std::string& name);
        virtual ~Animal();

        virtual void speak() const = 0;
        std::string getName() const;
        void setName(const std::string& name);
    };

    // Inheritance
    class Dog : public Animal {
    private:
        std::string breed_;

    public:
        Dog(const std::string& name, const std::string& breed);

        void speak() const override;
        void fetch() const;
        std::string getBreed() const;
    };

    // Static members
    class Counter {
    private:
        static int count_;
        int value_;

    public:
        Counter();
        static int getCount();
        static void resetCount();

        void increment();
        int getValue() const;
    };

    // Struct (public class)
    struct Point {
        double x, y;

        Point(double x = 0, double y = 0);
        double distance(const Point& other) const;
    };
}

// Implementation
namespace app {
    Animal::Animal(const std::string& name) : name_(name) {}

    void Animal::setName(const std::string& name) {
        name_ = name;
    }

    std::string Animal::getName() const {
        return name_;
    }

    Dog::Dog(const std::string& name, const std::string& breed)
        : Animal(name), breed_(breed) {}

    void Dog::speak() const {
        std::cout << getName() << " barks!" << std::endl;
    }

    void Dog::fetch() const {
        std::cout << getName() << " fetches the ball!" << std::endl;
    }

    int Counter::count_ = 0;

    Counter::Counter() : value_(0) {
        count_++;
    }

    int Counter::getCount() {
        return count_;
    }

    void Counter::increment() {
        value_++;
    }
}

// Using all patterns
void demonstrateBasicCpp() {
    app::initialize();

    // Object creation and method calls
    app::Dog dog("Buddy", "Golden Retriever");
    dog.speak();  // Virtual function call
    dog.fetch();  // Regular method call

    std::string name = dog.getName();  // Inherited method
    dog.setName("Max");  // Inherited method

    // Static method calls
    int count1 = app::Counter::getCount();
    app::Counter counter1;
    app::Counter counter2;
    counter1.increment();
    counter2.increment();
    int count2 = app::Counter::getCount();

    // Struct usage
    app::Point p1(0, 0);
    app::Point p2(3, 4);
    double dist = p1.distance(p2);

    // Polymorphic usage
    app::Animal* animal = &dog;
    animal->speak();  // Virtual call through pointer

    globalUtility();
}

void globalUtility() {
    std::cout << "Global utility function called" << std::endl;
}
""",
    )

    run_updater(cpp_basic_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")

    comprehensive_calls = [
        call for call in call_relationships if "comprehensive_basic" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 8, (
        f"Expected at least 8 comprehensive basic calls, found {len(comprehensive_calls)}"
    )

    basic_inheritance = [
        call
        for call in inherits_relationships
        if "comprehensive_basic" in call.args[0][2]
    ]

    assert len(basic_inheritance) >= 1, (
        f"Expected at least 1 inheritance relationship, found {len(basic_inheritance)}"
    )

    for relationship in comprehensive_calls:
        assert len(relationship.args) == 3, "Call relationship should have 3 args"
        assert relationship.args[1] == "CALLS", "Second arg should be 'CALLS'"

        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        assert "comprehensive_basic" in source_module, (
            f"Source module should contain test file name: {source_module}"
        )

        assert isinstance(target_module, str) and target_module, (
            f"Target should be non-empty string: {target_module}"
        )

    assert defines_relationships, "Should still have DEFINES relationships"
