from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_casting_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with casting operator patterns."""
    project_path = temp_repo / "cpp_casting_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_static_cast_examples(
    cpp_casting_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test static_cast in various scenarios."""
    test_file = cpp_casting_project / "static_cast_examples.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <memory>
#include <vector>

// Base class hierarchy for casting demonstrations
class Shape {
protected:
    std::string name_;

public:
    Shape(const std::string& name) : name_(name) {}
    virtual ~Shape() = default;

    virtual void draw() const {
        std::cout << "Drawing " << name_ << std::endl;
    }

    virtual double area() const = 0;

    const std::string& getName() const { return name_; }
};

class Rectangle : public Shape {
private:
    double width_, height_;

public:
    Rectangle(double width, double height)
        : Shape("Rectangle"), width_(width), height_(height) {}

    void draw() const override {
        std::cout << "Drawing Rectangle (" << width_ << "x" << height_ << ")" << std::endl;
    }

    double area() const override {
        return width_ * height_;
    }

    double getWidth() const { return width_; }
    double getHeight() const { return height_; }

    void setDimensions(double w, double h) {
        width_ = w;
        height_ = h;
    }
};

class Circle : public Shape {
private:
    double radius_;

public:
    Circle(double radius) : Shape("Circle"), radius_(radius) {}

    void draw() const override {
        std::cout << "Drawing Circle (r=" << radius_ << ")" << std::endl;
    }

    double area() const override {
        return 3.14159 * radius_ * radius_;
    }

    double getRadius() const { return radius_; }
    void setRadius(double r) { radius_ = r; }
};

class StaticCastDemo {
public:
    void demonstrateNumericCasts() {
        std::cout << "=== Static Cast - Numeric Conversions ===" << std::endl;

        // Basic numeric conversions
        int int_val = 42;
        double double_val = static_cast<double>(int_val);
        float float_val = static_cast<float>(int_val);

        std::cout << "int to double: " << int_val << " -> " << double_val << std::endl;
        std::cout << "int to float: " << int_val << " -> " << float_val << std::endl;

        // Precision loss warning demonstration
        double precise_double = 3.14159265359;
        int truncated_int = static_cast<int>(precise_double);
        float less_precise = static_cast<float>(precise_double);

        std::cout << "double to int (truncated): " << precise_double << " -> " << truncated_int << std::endl;
        std::cout << "double to float: " << precise_double << " -> " << less_precise << std::endl;

        // Enum conversions
        enum class Color { RED = 1, GREEN = 2, BLUE = 3 };
        int color_value = static_cast<int>(Color::GREEN);
        Color color_from_int = static_cast<Color>(2);

        std::cout << "Enum to int: GREEN -> " << color_value << std::endl;
        std::cout << "Int to enum: 2 -> " << (color_from_int == Color::GREEN ? "GREEN" : "OTHER") << std::endl;
    }

    void demonstratePointerCasts() {
        std::cout << "=== Static Cast - Pointer Conversions ===" << std::endl;

        // Base to derived (unsafe without runtime check)
        Rectangle rect(10.0, 5.0);
        Shape* shape_ptr = &rect;

        // This is safe because we know shape_ptr points to a Rectangle
        Rectangle* rect_ptr = static_cast<Rectangle*>(shape_ptr);
        if (rect_ptr) {
            std::cout << "Successfully cast Shape* to Rectangle*" << std::endl;
            std::cout << "Rectangle width: " << rect_ptr->getWidth() << std::endl;
            rect_ptr->setDimensions(15.0, 7.5);
            std::cout << "Updated rectangle area: " << rect_ptr->area() << std::endl;
        }

        // Void pointer conversions
        void* void_ptr = &int_val_;
        int* int_ptr = static_cast<int*>(void_ptr);
        std::cout << "Void* to int*: " << *int_ptr << std::endl;

        // Smart pointer casting
        std::shared_ptr<Shape> shape_smart = std::make_shared<Circle>(3.0);

        // Using static_pointer_cast for smart pointers
        auto circle_smart = std::static_pointer_cast<Circle>(shape_smart);
        std::cout << "Smart pointer cast - Circle radius: " << circle_smart->getRadius() << std::endl;
    }

    void demonstrateReferenceCasts() {
        std::cout << "=== Static Cast - Reference Conversions ===" << std::endl;

        Rectangle rect(8.0, 6.0);
        Shape& shape_ref = rect;

        // Cast reference from base to derived
        Rectangle& rect_ref = static_cast<Rectangle&>(shape_ref);
        std::cout << "Reference cast successful" << std::endl;
        std::cout << "Rectangle dimensions: " << rect_ref.getWidth() << "x" << rect_ref.getHeight() << std::endl;

        rect_ref.setDimensions(12.0, 9.0);
        std::cout << "After modification: " << rect_ref.getWidth() << "x" << rect_ref.getHeight() << std::endl;
    }

    void demonstrateUpcasting() {
        std::cout << "=== Static Cast - Upcasting (Safe) ===" << std::endl;

        Rectangle rect(7.0, 4.0);
        Circle circle(2.5);

        // Upcast from derived to base (always safe)
        Shape* shape1 = static_cast<Shape*>(&rect);
        Shape* shape2 = static_cast<Shape*>(&circle);

        std::cout << "Upcast Rectangle to Shape:" << std::endl;
        shape1->draw();
        std::cout << "Area: " << shape1->area() << std::endl;

        std::cout << "Upcast Circle to Shape:" << std::endl;
        shape2->draw();
        std::cout << "Area: " << shape2->area() << std::endl;

        // Reference upcasting
        Shape& shape_ref1 = static_cast<Shape&>(rect);
        Shape& shape_ref2 = static_cast<Shape&>(circle);

        std::cout << "Reference upcast results:" << std::endl;
        shape_ref1.draw();
        shape_ref2.draw();
    }

private:
    int int_val_ = 100;
};

void testStaticCastExamples() {
    StaticCastDemo demo;
    demo.demonstrateNumericCasts();
    demo.demonstratePointerCasts();
    demo.demonstrateReferenceCasts();
    demo.demonstrateUpcasting();
}

void demonstrateStaticCastExamples() {
    testStaticCastExamples();
}
""",
    )

    run_updater(cpp_casting_project, mock_ingestor)

    project_name = cpp_casting_project.name

    expected_classes = [
        f"{project_name}.static_cast_examples.Shape",
        f"{project_name}.static_cast_examples.Rectangle",
        f"{project_name}.static_cast_examples.Circle",
        f"{project_name}.static_cast_examples.StaticCastDemo",
    ]

    expected_functions = [
        f"{project_name}.static_cast_examples.testStaticCastExamples",
        f"{project_name}.static_cast_examples.demonstrateStaticCastExamples",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_dynamic_cast_examples(
    cpp_casting_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test dynamic_cast with polymorphic hierarchies."""
    test_file = cpp_casting_project / "dynamic_cast_examples.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <memory>
#include <vector>
#include <typeinfo>

// Abstract base class with virtual functions (required for dynamic_cast)
class Node {
protected:
    int id_;
    std::string label_;

public:
    Node(int id, const std::string& label) : id_(id), label_(label) {}
    virtual ~Node() = default;

    virtual void process() const = 0;
    virtual std::string getType() const = 0;

    int getId() const { return id_; }
    const std::string& getLabel() const { return label_; }
};

class FunctionNode : public Node {
private:
    std::string return_type_;
    std::vector<std::string> parameters_;

public:
    FunctionNode(int id, const std::string& label, const std::string& return_type)
        : Node(id, label), return_type_(return_type) {}

    void addParameter(const std::string& param) {
        parameters_.push_back(param);
    }

    void process() const override {
        std::cout << "Processing function: " << label_ << std::endl;
        std::cout << "  Return type: " << return_type_ << std::endl;
        std::cout << "  Parameters: " << parameters_.size() << std::endl;
    }

    std::string getType() const override { return "Function"; }

    const std::string& getReturnType() const { return return_type_; }
    const std::vector<std::string>& getParameters() const { return parameters_; }

    void setReturnType(const std::string& type) { return_type_ = type; }
};

class ClassNode : public Node {
private:
    std::vector<std::string> methods_;
    std::vector<std::string> members_;
    bool is_abstract_;

public:
    ClassNode(int id, const std::string& label, bool is_abstract = false)
        : Node(id, label), is_abstract_(is_abstract) {}

    void addMethod(const std::string& method) { methods_.push_back(method); }
    void addMember(const std::string& member) { members_.push_back(member); }

    void process() const override {
        std::cout << "Processing class: " << label_ << std::endl;
        std::cout << "  Methods: " << methods_.size() << std::endl;
        std::cout << "  Members: " << members_.size() << std::endl;
        std::cout << "  Abstract: " << std::boolalpha << is_abstract_ << std::endl;
    }

    std::string getType() const override { return "Class"; }

    bool isAbstract() const { return is_abstract_; }
    const std::vector<std::string>& getMethods() const { return methods_; }
    const std::vector<std::string>& getMembers() const { return members_; }
};

class VariableNode : public Node {
private:
    std::string data_type_;
    bool is_const_;
    bool is_static_;

public:
    VariableNode(int id, const std::string& label, const std::string& data_type)
        : Node(id, label), data_type_(data_type), is_const_(false), is_static_(false) {}

    void process() const override {
        std::cout << "Processing variable: " << label_ << std::endl;
        std::cout << "  Type: " << data_type_ << std::endl;
        std::cout << "  Const: " << std::boolalpha << is_const_ << std::endl;
        std::cout << "  Static: " << std::boolalpha << is_static_ << std::endl;
    }

    std::string getType() const override { return "Variable"; }

    const std::string& getDataType() const { return data_type_; }
    bool isConst() const { return is_const_; }
    bool isStatic() const { return is_static_; }

    void setConst(bool is_const) { is_const_ = is_const; }
    void setStatic(bool is_static) { is_static_ = is_static; }
};

class DynamicCastDemo {
private:
    std::vector<std::unique_ptr<Node>> nodes_;

public:
    DynamicCastDemo() {
        // Create different types of nodes
        auto func_node = std::make_unique<FunctionNode>(1, "calculateArea", "double");
        func_node->addParameter("double width");
        func_node->addParameter("double height");
        nodes_.push_back(std::move(func_node));

        auto class_node = std::make_unique<ClassNode>(2, "Rectangle", false);
        class_node->addMethod("getWidth");
        class_node->addMethod("getHeight");
        class_node->addMethod("area");
        class_node->addMember("width_");
        class_node->addMember("height_");
        nodes_.push_back(std::move(class_node));

        auto var_node = std::make_unique<VariableNode>(3, "globalCounter", "int");
        var_node->setStatic(true);
        nodes_.push_back(std::move(var_node));
    }

    void demonstrateBasicDynamicCast() {
        std::cout << "=== Dynamic Cast - Basic Usage ===" << std::endl;

        for (const auto& node : nodes_) {
            std::cout << "\\nProcessing node ID " << node->getId() << ":" << std::endl;
            node->process();

            // Try to cast to specific derived types
            if (auto* func_ptr = dynamic_cast<FunctionNode*>(node.get())) {
                std::cout << "  -> Successfully cast to FunctionNode" << std::endl;
                std::cout << "     Return type: " << func_ptr->getReturnType() << std::endl;
                std::cout << "     Parameter count: " << func_ptr->getParameters().size() << std::endl;
            }
            else if (auto* class_ptr = dynamic_cast<ClassNode*>(node.get())) {
                std::cout << "  -> Successfully cast to ClassNode" << std::endl;
                std::cout << "     Method count: " << class_ptr->getMethods().size() << std::endl;
                std::cout << "     Is abstract: " << std::boolalpha << class_ptr->isAbstract() << std::endl;
            }
            else if (auto* var_ptr = dynamic_cast<VariableNode*>(node.get())) {
                std::cout << "  -> Successfully cast to VariableNode" << std::endl;
                std::cout << "     Data type: " << var_ptr->getDataType() << std::endl;
                std::cout << "     Is static: " << std::boolalpha << var_ptr->isStatic() << std::endl;
            }
        }
    }

    void demonstrateFailedCasts() {
        std::cout << "=== Dynamic Cast - Failed Casts ===" << std::endl;

        Node* base_ptr = nodes_[0].get(); // Points to FunctionNode

        // Try to cast FunctionNode to ClassNode (should fail)
        ClassNode* class_ptr = dynamic_cast<ClassNode*>(base_ptr);
        if (class_ptr) {
            std::cout << "Unexpected success: cast to ClassNode" << std::endl;
        } else {
            std::cout << "Expected failure: cannot cast FunctionNode to ClassNode" << std::endl;
        }

        // Try to cast FunctionNode to VariableNode (should fail)
        VariableNode* var_ptr = dynamic_cast<VariableNode*>(base_ptr);
        if (var_ptr) {
            std::cout << "Unexpected success: cast to VariableNode" << std::endl;
        } else {
            std::cout << "Expected failure: cannot cast FunctionNode to VariableNode" << std::endl;
        }
    }

    void demonstrateReferenceCasts() {
        std::cout << "=== Dynamic Cast - Reference Casts ===" << std::endl;

        Node& base_ref = *nodes_[1]; // Reference to ClassNode

        try {
            // This should succeed
            ClassNode& class_ref = dynamic_cast<ClassNode&>(base_ref);
            std::cout << "Success: cast reference to ClassNode" << std::endl;
            std::cout << "Class has " << class_ref.getMethods().size() << " methods" << std::endl;
        }
        catch (const std::bad_cast& e) {
            std::cout << "Failed to cast reference to ClassNode: " << e.what() << std::endl;
        }

        try {
            // This should fail and throw std::bad_cast
            FunctionNode& func_ref = dynamic_cast<FunctionNode&>(base_ref);
            std::cout << "Unexpected success: cast reference to FunctionNode" << std::endl;
        }
        catch (const std::bad_cast& e) {
            std::cout << "Expected exception: " << e.what() << std::endl;
        }
    }

    void demonstrateSmartPointerCasts() {
        std::cout << "=== Dynamic Cast - Smart Pointer Casts ===" << std::endl;

        std::shared_ptr<Node> shared_node = std::make_shared<FunctionNode>(10, "testFunction", "void");

        // Use dynamic_pointer_cast for shared_ptr
        if (auto func_shared = std::dynamic_pointer_cast<FunctionNode>(shared_node)) {
            std::cout << "Successfully cast shared_ptr to FunctionNode" << std::endl;
            std::cout << "Function name: " << func_shared->getLabel() << std::endl;
            func_shared->addParameter("int value");
            std::cout << "Added parameter, now has: " << func_shared->getParameters().size() << " parameters" << std::endl;
        }

        if (auto class_shared = std::dynamic_pointer_cast<ClassNode>(shared_node)) {
            std::cout << "Unexpected: cast shared_ptr to ClassNode succeeded" << std::endl;
        } else {
            std::cout << "Expected: cannot cast FunctionNode shared_ptr to ClassNode" << std::endl;
        }
    }

    void demonstrateTypeIdentification() {
        std::cout << "=== Dynamic Cast - Type Identification ===" << std::endl;

        for (const auto& node : nodes_) {
            std::cout << "\\nNode " << node->getId() << " (" << node->getLabel() << "):" << std::endl;

            // Check what type this node actually is
            if (dynamic_cast<FunctionNode*>(node.get())) {
                std::cout << "  Type: FunctionNode" << std::endl;
            } else if (dynamic_cast<ClassNode*>(node.get())) {
                std::cout << "  Type: ClassNode" << std::endl;
            } else if (dynamic_cast<VariableNode*>(node.get())) {
                std::cout << "  Type: VariableNode" << std::endl;
            } else {
                std::cout << "  Type: Unknown Node type" << std::endl;
            }

            // Use typeid for comparison
            std::cout << "  typeid name: " << typeid(*node).name() << std::endl;
        }
    }
};

void testDynamicCastExamples() {
    DynamicCastDemo demo;
    demo.demonstrateBasicDynamicCast();
    demo.demonstrateFailedCasts();
    demo.demonstrateReferenceCasts();
    demo.demonstrateSmartPointerCasts();
    demo.demonstrateTypeIdentification();
}

void demonstrateDynamicCastExamples() {
    testDynamicCastExamples();
}
""",
    )

    run_updater(cpp_casting_project, mock_ingestor)

    project_name = cpp_casting_project.name

    expected_classes = [
        f"{project_name}.dynamic_cast_examples.Node",
        f"{project_name}.dynamic_cast_examples.FunctionNode",
        f"{project_name}.dynamic_cast_examples.ClassNode",
        f"{project_name}.dynamic_cast_examples.VariableNode",
        f"{project_name}.dynamic_cast_examples.DynamicCastDemo",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_other_cast_operators(
    cpp_casting_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test const_cast, reinterpret_cast and C-style casts."""
    test_file = cpp_casting_project / "other_cast_operators.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <cstring>

class OtherCastsDemo {
private:
    mutable int mutable_value_ = 42;
    const int const_value_ = 100;

public:
    void demonstrateConstCast() {
        std::cout << "=== const_cast Demonstrations ===" << std::endl;

        const int* const_ptr = &const_value_;
        std::cout << "Original const value: " << *const_ptr << std::endl;

        // Remove const-ness (dangerous if original was truly const)
        int* mutable_ptr = const_cast<int*>(const_ptr);

        // This is undefined behavior since const_value_ is truly const
        // *mutable_ptr = 200; // DON'T DO THIS!

        std::cout << "const_cast performed (but not modifying truly const data)" << std::endl;

        // Safe example: removing const from mutable data
        const int* safe_const_ptr = &mutable_value_;
        int* safe_mutable_ptr = const_cast<int*>(safe_const_ptr);
        *safe_mutable_ptr = 84;

        std::cout << "Safe const_cast and modification: " << mutable_value_ << std::endl;

        // const_cast with references
        const std::string const_str = "Hello";
        std::string& mutable_str_ref = const_cast<std::string&>(const_str);
        // mutable_str_ref += " World"; // This would be undefined behavior

        std::cout << "const_cast with reference (not modifying): " << const_str << std::endl;

        // Practical example: working with C APIs
        const char* c_str = "Test string";
        char* writable_str = const_cast<char*>(c_str);
        // Can use writable_str with C functions that expect char* but don't modify
        std::cout << "String length via C function: " << strlen(writable_str) << std::endl;
    }

    void demonstrateReinterpretCast() {
        std::cout << "=== reinterpret_cast Demonstrations ===" << std::endl;

        // Pointer to integer conversion (platform-specific)
        int value = 12345;
        int* int_ptr = &value;

        // Convert pointer to integer type
        uintptr_t ptr_as_int = reinterpret_cast<uintptr_t>(int_ptr);
        std::cout << "Pointer as integer: 0x" << std::hex << ptr_as_int << std::dec << std::endl;

        // Convert back to pointer
        int* recovered_ptr = reinterpret_cast<int*>(ptr_as_int);
        std::cout << "Recovered value: " << *recovered_ptr << std::endl;

        // Array of bytes interpretation
        int array_value = 0x12345678;
        unsigned char* byte_ptr = reinterpret_cast<unsigned char*>(&array_value);

        std::cout << "Integer as bytes: ";
        for (size_t i = 0; i < sizeof(int); ++i) {
            std::cout << "0x" << std::hex << static_cast<int>(byte_ptr[i]) << " ";
        }
        std::cout << std::dec << std::endl;

        // Function pointer casting (dangerous but sometimes necessary)
        void (*func_ptr)() = reinterpret_cast<void(*)()>(ptr_as_int);
        // Don't actually call this - it's just for demonstration
        std::cout << "Function pointer created (not calling it)" << std::endl;

        // Different types with same memory layout
        struct Point2D { int x, y; };
        struct Vector2D { int dx, dy; };

        Point2D point{10, 20};
        Vector2D* vector_ptr = reinterpret_cast<Vector2D*>(&point);

        std::cout << "Point as Vector: dx=" << vector_ptr->dx << ", dy=" << vector_ptr->dy << std::endl;
    }

    void demonstrateCStyleCasts() {
        std::cout << "=== C-Style Cast Demonstrations ===" << std::endl;

        // Basic numeric conversion (acts like static_cast)
        double double_val = 3.14159;
        int int_val = (int)double_val;
        std::cout << "C-style numeric cast: " << double_val << " -> " << int_val << std::endl;

        // Pointer conversion (acts like static_cast or reinterpret_cast)
        void* void_ptr = &int_val;
        int* int_ptr = (int*)void_ptr;
        std::cout << "C-style pointer cast: " << *int_ptr << std::endl;

        // const removal (acts like const_cast)
        const int const_int = 500;
        int* non_const_ptr = (int*)&const_int;
        std::cout << "C-style const removal: " << *non_const_ptr << std::endl;

        // Inheritance casting (acts like static_cast)
        struct Base { virtual ~Base() = default; int base_val = 1; };
        struct Derived : Base { int derived_val = 2; };

        Derived derived_obj;
        Base* base_ptr = (Base*)&derived_obj;  // Upcast
        Derived* derived_ptr = (Derived*)base_ptr;  // Downcast (unsafe without runtime check)

        std::cout << "C-style inheritance cast: base=" << derived_ptr->base_val
                  << ", derived=" << derived_ptr->derived_val << std::endl;

        // Functional cast syntax
        double functional_cast_result = double(int_val);
        std::cout << "Functional cast syntax: " << functional_cast_result << std::endl;
    }

    void demonstrateCastComparisons() {
        std::cout << "=== Cast Operator Comparisons ===" << std::endl;

        double source_val = 42.7;

        // Same conversion using different cast operators
        int static_result = static_cast<int>(source_val);
        int c_style_result = (int)source_val;
        int functional_result = int(source_val);

        std::cout << "Source: " << source_val << std::endl;
        std::cout << "static_cast result: " << static_result << std::endl;
        std::cout << "C-style cast result: " << c_style_result << std::endl;
        std::cout << "Functional cast result: " << functional_result << std::endl;

        // Pointer conversions
        struct TestStruct { int value = 123; };
        TestStruct test_obj;
        void* void_ptr = &test_obj;

        // All these achieve the same result for this case
        TestStruct* static_ptr = static_cast<TestStruct*>(void_ptr);
        TestStruct* reinterpret_ptr = reinterpret_cast<TestStruct*>(void_ptr);
        TestStruct* c_style_ptr = (TestStruct*)void_ptr;

        std::cout << "Pointer cast results:" << std::endl;
        std::cout << "  static_cast: " << static_ptr->value << std::endl;
        std::cout << "  reinterpret_cast: " << reinterpret_ptr->value << std::endl;
        std::cout << "  C-style cast: " << c_style_ptr->value << std::endl;
    }

    void demonstrateCastSafety() {
        std::cout << "=== Cast Safety Guidelines ===" << std::endl;

        std::cout << "Cast safety ranking (safest to most dangerous):" << std::endl;
        std::cout << "1. static_cast - Compile-time checked, explicit conversions" << std::endl;
        std::cout << "2. dynamic_cast - Runtime checked, safe polymorphic casting" << std::endl;
        std::cout << "3. const_cast - Only removes/adds const/volatile qualifiers" << std::endl;
        std::cout << "4. reinterpret_cast - Unsafe, reinterprets bit patterns" << std::endl;
        std::cout << "5. C-style cast - Tries multiple cast types, hard to predict" << std::endl;

        std::cout << "\\nRecommendations:" << std::endl;
        std::cout << "- Prefer static_cast for most conversions" << std::endl;
        std::cout << "- Use dynamic_cast for polymorphic downcasting" << std::endl;
        std::cout << "- Only use const_cast when interfacing with const-incorrect APIs" << std::endl;
        std::cout << "- Avoid reinterpret_cast unless doing low-level system programming" << std::endl;
        std::cout << "- Avoid C-style casts in C++ code" << std::endl;
    }
};

void testOtherCastOperators() {
    OtherCastsDemo demo;
    demo.demonstrateConstCast();
    demo.demonstrateReinterpretCast();
    demo.demonstrateCStyleCasts();
    demo.demonstrateCastComparisons();
    demo.demonstrateCastSafety();
}

void demonstrateOtherCastOperators() {
    testOtherCastOperators();
}
""",
    )

    run_updater(cpp_casting_project, mock_ingestor)

    project_name = cpp_casting_project.name

    expected_classes = [
        f"{project_name}.other_cast_operators.OtherCastsDemo",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 other cast class, found {len(found_classes)}: {found_classes}"
    )


def test_cpp_casting_comprehensive(
    cpp_casting_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all casting patterns create proper relationships."""
    test_file = cpp_casting_project / "comprehensive_casting.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Comprehensive casting demonstration for graph applications
#include <iostream>
#include <memory>
#include <vector>

// Base graph element
class GraphElement {
protected:
    int id_;
    std::string name_;

public:
    GraphElement(int id, const std::string& name) : id_(id), name_(name) {}
    virtual ~GraphElement() = default;

    virtual void process() const = 0;
    virtual std::string getElementType() const = 0;

    int getId() const { return id_; }
    const std::string& getName() const { return name_; }
};

// Derived element types
class GraphNode : public GraphElement {
public:
    GraphNode(int id, const std::string& name) : GraphElement(id, name) {}

    void process() const override {
        std::cout << "Processing graph node: " << name_ << std::endl;
    }

    std::string getElementType() const override { return "Node"; }
};

class GraphEdge : public GraphElement {
private:
    int source_id_, target_id_;

public:
    GraphEdge(int id, const std::string& name, int source, int target)
        : GraphElement(id, name), source_id_(source), target_id_(target) {}

    void process() const override {
        std::cout << "Processing graph edge: " << name_
                  << " (" << source_id_ << " -> " << target_id_ << ")" << std::endl;
    }

    std::string getElementType() const override { return "Edge"; }

    int getSource() const { return source_id_; }
    int getTarget() const { return target_id_; }
};

class ComprehensiveCastingDemo {
private:
    std::vector<std::shared_ptr<GraphElement>> elements_;

public:
    ComprehensiveCastingDemo() {
        // Create mixed graph elements
        elements_.push_back(std::make_shared<GraphNode>(1, "FunctionA"));
        elements_.push_back(std::make_shared<GraphEdge>(2, "calls", 1, 3));
        elements_.push_back(std::make_shared<GraphNode>(3, "FunctionB"));
    }

    void demonstrateComprehensiveCasting() {
        std::cout << "=== Comprehensive Casting Demo for Graph Processing ===" << std::endl;

        for (const auto& element : elements_) {
            std::cout << "\\nProcessing element " << element->getId() << ":" << std::endl;

            // Use dynamic_cast for safe type identification
            if (auto node = std::dynamic_pointer_cast<GraphNode>(element)) {
                std::cout << "  -> This is a GraphNode" << std::endl;
                processNode(node.get());
            }
            else if (auto edge = std::dynamic_pointer_cast<GraphEdge>(element)) {
                std::cout << "  -> This is a GraphEdge" << std::endl;
                processEdge(edge.get());
            }

            // Demonstrate static_cast for known safe conversions
            GraphElement* base_ptr = element.get();
            if (element->getElementType() == "Node") {
                GraphNode* node_ptr = static_cast<GraphNode*>(base_ptr);
                std::cout << "  -> static_cast to GraphNode successful" << std::endl;
            }
        }
    }

private:
    void processNode(const GraphNode* node) {
        std::cout << "    Node-specific processing for: " << node->getName() << std::endl;
    }

    void processEdge(const GraphEdge* edge) {
        std::cout << "    Edge-specific processing: " << edge->getSource()
                  << " -> " << edge->getTarget() << std::endl;
    }
};

void demonstrateComprehensiveCasting() {
    ComprehensiveCastingDemo demo;
    demo.demonstrateComprehensiveCasting();
}
""",
    )

    run_updater(cpp_casting_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_casting" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 2, (
        f"Expected at least 2 comprehensive casting calls, found {len(comprehensive_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
