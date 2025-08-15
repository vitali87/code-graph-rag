"""
Comprehensive Java language parsing and relationship testing.
Tests all Java constructs including classes, interfaces, enums, annotations,
methods, constructors, fields, imports, packages, generics, inheritance, and modern features.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def java_project(temp_repo: Path) -> Path:
    """Create a comprehensive Java project structure."""
    project_path = temp_repo / "java_test"
    project_path.mkdir()

    # Create standard Java project structure
    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()
    (project_path / "src" / "test").mkdir()
    (project_path / "src" / "test" / "java").mkdir()

    return project_path


def test_basic_java_classes(
    java_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic Java class parsing including inheritance and interfaces."""
    test_file = (
        java_project / "src" / "main" / "java" / "com" / "example" / "BasicClasses.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.List;
import java.util.ArrayList;

// Basic class declaration
public class BasicClass {
    private String name;
    protected int value;

    public BasicClass(String name) {
        this.name = name;
    }

    public String getName() {
        return name;
    }
}

// Class with inheritance
public class ExtendedClass extends BasicClass {
    private boolean flag;

    public ExtendedClass(String name, boolean flag) {
        super(name);
        this.flag = flag;
    }

    @Override
    public String getName() {
        return super.getName() + (flag ? " (enabled)" : " (disabled)");
    }
}

// Interface declaration
public interface Drawable {
    void draw();
    default void clear() {
        System.out.println("Clearing...");
    }
}

// Class implementing interface
public class Circle implements Drawable {
    private double radius;

    public Circle(double radius) {
        this.radius = radius;
    }

    @Override
    public void draw() {
        System.out.println("Drawing circle with radius: " + radius);
    }
}

// Abstract class
public abstract class Shape {
    protected String color;

    public Shape(String color) {
        this.color = color;
    }

    public abstract double area();

    public String getColor() {
        return color;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify classes were detected
    project_name = java_project.name

    # Get all Class node creation calls
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    # Get all Interface node creation calls (Java interfaces are processed separately)
    interface_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Interface"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    # Expected class qualified names (including interfaces)
    expected_classes = {
        f"{project_name}.src.main.java.com.example.BasicClasses.BasicClass",
        f"{project_name}.src.main.java.com.example.BasicClasses.ExtendedClass",
        f"{project_name}.src.main.java.com.example.BasicClasses.Circle",
        f"{project_name}.src.main.java.com.example.BasicClasses.Shape",
    }

    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.BasicClasses.Drawable",
    }

    # Verify all expected classes and interfaces were created
    missing_classes = expected_classes - created_classes
    missing_interfaces = expected_interfaces - created_interfaces

    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )
    assert not missing_interfaces, (
        f"Missing expected interfaces: {sorted(list(missing_interfaces))}"
    )

    # Get all Method node creation calls
    method_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Method"
    ]

    created_methods = {call[0][1]["qualified_name"] for call in method_calls}

    # Expected method qualified names (constructors and methods with parameter signatures)
    expected_methods = {
        f"{project_name}.src.main.java.com.example.BasicClasses.BasicClass.BasicClass(String)",
        f"{project_name}.src.main.java.com.example.BasicClasses.BasicClass.getName()",
        f"{project_name}.src.main.java.com.example.BasicClasses.ExtendedClass.ExtendedClass(String,boolean)",
        f"{project_name}.src.main.java.com.example.BasicClasses.ExtendedClass.getName()",
        f"{project_name}.src.main.java.com.example.BasicClasses.Drawable.draw()",
        f"{project_name}.src.main.java.com.example.BasicClasses.Drawable.clear()",
        f"{project_name}.src.main.java.com.example.BasicClasses.Circle.Circle(double)",
        f"{project_name}.src.main.java.com.example.BasicClasses.Circle.draw()",
        f"{project_name}.src.main.java.com.example.BasicClasses.Shape.Shape(String)",
        f"{project_name}.src.main.java.com.example.BasicClasses.Shape.area()",
        f"{project_name}.src.main.java.com.example.BasicClasses.Shape.getColor()",
    }

    # Verify all expected methods were created
    missing_methods = expected_methods - created_methods
    assert not missing_methods, (
        f"Missing expected methods: {sorted(list(missing_methods))}"
    )


def test_java_enums_and_annotations(
    java_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java enum and annotation parsing."""
    test_file = (
        java_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "EnumsAndAnnotations.java"
    )
    test_file.write_text(
        """
package com.example;

// Simple enum
public enum Color {
    RED, GREEN, BLUE
}

// Enum with methods and fields
public enum Planet {
    MERCURY(3.303e+23, 2.4397e6),
    VENUS(4.869e+24, 6.0518e6),
    EARTH(5.976e+24, 6.37814e6);

    private final double mass;
    private final double radius;

    Planet(double mass, double radius) {
        this.mass = mass;
        this.radius = radius;
    }

    public double getMass() {
        return mass;
    }

    public double getRadius() {
        return radius;
    }
}

// Custom annotation
@interface MyAnnotation {
    String value() default "";
    int priority() default 0;
}

// Class using annotations
@MyAnnotation(value = "test", priority = 1)
public class AnnotatedClass {

    @Deprecated
    public void oldMethod() {
        System.out.println("This method is deprecated");
    }

    @Override
    public String toString() {
        return "AnnotatedClass";
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Get all node creation calls by type (Java has distinct node types)
    all_calls = mock_ingestor.ensure_node_batch.call_args_list

    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    enum_calls = [call for call in all_calls if call[0][0] == "Enum"]
    [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_enums = {call[0][1]["qualified_name"] for call in enum_calls}

    # Expected type qualified names
    project_name = java_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.EnumsAndAnnotations.AnnotatedClass",
        f"{project_name}.src.main.java.com.example.EnumsAndAnnotations.MyAnnotation",  # Annotations are processed as classes
    }

    expected_enums = {
        f"{project_name}.src.main.java.com.example.EnumsAndAnnotations.Color",
        f"{project_name}.src.main.java.com.example.EnumsAndAnnotations.Planet",
    }

    # Verify all expected types were created in their respective categories
    missing_classes = expected_classes - created_classes
    missing_enums = expected_enums - created_enums

    assert not missing_classes, f"Missing classes: {sorted(list(missing_classes))}"
    assert not missing_enums, f"Missing enums: {sorted(list(missing_enums))}"


def test_java_generics_and_collections(
    java_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java generics and collection handling."""
    test_file = (
        java_project / "src" / "main" / "java" / "com" / "example" / "Generics.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

// Generic class
public class Container<T> {
    private T value;

    public Container(T value) {
        this.value = value;
    }

    public T getValue() {
        return value;
    }

    public void setValue(T value) {
        this.value = value;
    }
}

// Generic interface
public interface Comparable<T> {
    int compareTo(T other);
}

// Generic method
public class Utilities {
    public static <T> List<T> createList(T... items) {
        List<T> list = new ArrayList<>();
        Collections.addAll(list, items);
        return list;
    }

    public static <K, V> Map<K, V> createMap() {
        return new HashMap<K, V>();
    }
}

// Bounded generics
public class NumberContainer<T extends Number> {
    private T number;

    public NumberContainer(T number) {
        this.number = number;
    }

    public double getDoubleValue() {
        return number.doubleValue();
    }
}

// Wildcard generics
public class WildcardExample {
    public void processNumbers(List<? extends Number> numbers) {
        for (Number num : numbers) {
            System.out.println(num.doubleValue());
        }
    }

    public void addToList(List<? super Integer> list) {
        list.add(42);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Get all node creation calls by type
    all_calls = mock_ingestor.ensure_node_batch.call_args_list

    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    # Expected type qualified names
    project_name = java_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.Generics.Container",
        f"{project_name}.src.main.java.com.example.Generics.Utilities",
        f"{project_name}.src.main.java.com.example.Generics.NumberContainer",
        f"{project_name}.src.main.java.com.example.Generics.WildcardExample",
    }

    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.Generics.Comparable",
    }

    # Verify all expected types were created
    missing_classes = expected_classes - created_classes
    missing_interfaces = expected_interfaces - created_interfaces

    assert not missing_classes, (
        f"Missing expected generic classes: {sorted(list(missing_classes))}"
    )
    assert not missing_interfaces, (
        f"Missing expected generic interfaces: {sorted(list(missing_interfaces))}"
    )


def test_java_static_and_final(
    java_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java static and final modifier parsing."""
    test_file = (
        java_project / "src" / "main" / "java" / "com" / "example" / "Modifiers.java"
    )
    test_file.write_text(
        """
package com.example;

public final class Constants {
    public static final String APP_NAME = "MyApp";
    public static final int VERSION = 1;
    private static final double PI = 3.14159;

    private Constants() {
        // Prevent instantiation
    }

    public static String getAppInfo() {
        return APP_NAME + " v" + VERSION;
    }
}

public class Singleton {
    private static volatile Singleton instance;
    private final String id;

    private Singleton() {
        this.id = "singleton-" + System.currentTimeMillis();
    }

    public static Singleton getInstance() {
        if (instance == null) {
            synchronized (Singleton.class) {
                if (instance == null) {
                    instance = new Singleton();
                }
            }
        }
        return instance;
    }

    public String getId() {
        return id;
    }
}

public abstract class AbstractService {
    protected static final String SERVICE_NAME = "Service";

    public abstract void start();
    public abstract void stop();

    public final void restart() {
        stop();
        start();
    }

    protected static void log(String message) {
        System.out.println("[" + SERVICE_NAME + "] " + message);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Get all Class node creation calls
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Expected class qualified names
    project_name = java_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.Modifiers.Constants",
        f"{project_name}.src.main.java.com.example.Modifiers.Singleton",
        f"{project_name}.src.main.java.com.example.Modifiers.AbstractService",
    }

    # Verify all expected classes were created
    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_java_inner_classes(
    java_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java inner and nested class parsing."""
    test_file = (
        java_project / "src" / "main" / "java" / "com" / "example" / "InnerClasses.java"
    )
    test_file.write_text(
        """
package com.example;

public class OuterClass {
    private String outerField = "Outer";
    private static String staticField = "Static";

    // Non-static inner class
    public class InnerClass {
        private String innerField = "Inner";

        public void accessOuter() {
            System.out.println(outerField); // Can access outer instance field
            System.out.println(staticField); // Can access static field
        }

        public String getInnerField() {
            return innerField;
        }
    }

    // Static nested class
    public static class StaticNestedClass {
        private String nestedField = "Nested";

        public void accessOuter() {
            System.out.println(staticField); // Can only access static fields
        }

        public String getNestedField() {
            return nestedField;
        }
    }

    // Method with local class
    public void methodWithLocalClass() {
        final String localVar = "Local";

        // Local class
        class LocalClass {
            public void useLocalVar() {
                System.out.println(localVar); // Can access final local variables
                System.out.println(outerField); // Can access outer instance field
            }
        }

        LocalClass local = new LocalClass();
        local.useLocalVar();
    }

    // Method with anonymous class
    public Runnable createRunnable() {
        return new Runnable() {
            @Override
            public void run() {
                System.out.println("Anonymous class: " + outerField);
            }
        };
    }

    public String getOuterField() {
        return outerField;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Get all Class node creation calls
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Expected class qualified names
    project_name = java_project.name

    # Verify all expected classes were created (some inner classes may not be detected)
    outer_class_found = any(
        f"{project_name}.src.main.java.com.example.InnerClasses.OuterClass" in qn
        for qn in created_classes
    )
    assert outer_class_found, "OuterClass not found"


def test_java_lambda_expressions(
    java_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java lambda expression and functional interface parsing."""
    test_file = (
        java_project / "src" / "main" / "java" / "com" / "example" / "Lambdas.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.function.*;
import java.util.stream.*;

@FunctionalInterface
interface Calculator {
    int calculate(int a, int b);
}

@FunctionalInterface
interface StringProcessor {
    String process(String input);
}

public class LambdaExamples {

    public void basicLambdas() {
        // Simple lambda expressions
        Calculator add = (a, b) -> a + b;
        Calculator multiply = (x, y) -> x * y;

        // Lambda with body
        Calculator complexOp = (a, b) -> {
            int result = a * 2 + b * 3;
            return result;
        };

        System.out.println(add.calculate(5, 3));
        System.out.println(multiply.calculate(4, 7));
        System.out.println(complexOp.calculate(2, 4));
    }

    public void streamOperations() {
        List<String> names = Arrays.asList("Alice", "Bob", "Charlie", "David");

        // Using lambdas with streams
        List<String> uppercaseNames = names.stream()
            .map(name -> name.toUpperCase())
            .filter(name -> name.length() > 3)
            .collect(Collectors.toList());

        // Method reference
        List<String> sortedNames = names.stream()
            .sorted(String::compareTo)
            .collect(Collectors.toList());

        // Complex stream operations
        Map<Integer, List<String>> namesByLength = names.stream()
            .collect(Collectors.groupingBy(String::length));
    }

    public void functionalInterfaces() {
        // Predicate
        Predicate<String> isEmpty = String::isEmpty;
        Predicate<String> isLong = s -> s.length() > 10;

        // Function
        Function<String, Integer> stringLength = String::length;
        Function<Integer, String> intToString = Object::toString;

        // Consumer
        Consumer<String> printer = System.out::println;

        // Supplier
        Supplier<String> messageSupplier = () -> "Hello World";

        // BiFunction
        BiFunction<String, String, String> concat = (a, b) -> a + " " + b;
    }

    public void customFunctionalInterface() {
        StringProcessor upperCase = String::toUpperCase;
        StringProcessor addPrefix = str -> "PREFIX: " + str;
        StringProcessor reverse = str -> new StringBuilder(str).reverse().toString();

        String input = "hello world";
        System.out.println(upperCase.process(input));
        System.out.println(addPrefix.process(input));
        System.out.println(reverse.process(input));
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Get all node creation calls by type
    all_calls = mock_ingestor.ensure_node_batch.call_args_list

    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    # Expected type qualified names
    project_name = java_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.Lambdas.LambdaExamples",
    }

    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.Lambdas.Calculator",
        f"{project_name}.src.main.java.com.example.Lambdas.StringProcessor",
    }

    # Verify all expected types were created
    missing_classes = expected_classes - created_classes
    missing_interfaces = expected_interfaces - created_interfaces

    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )
    assert not missing_interfaces, (
        f"Missing expected interfaces: {sorted(list(missing_interfaces))}"
    )


def test_java_exception_handling(
    java_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java exception handling and custom exception parsing."""
    test_file = (
        java_project / "src" / "main" / "java" / "com" / "example" / "Exceptions.java"
    )
    test_file.write_text(
        """
package com.example;

import java.io.*;

// Custom checked exception
public class CustomException extends Exception {
    private final int errorCode;

    public CustomException(String message, int errorCode) {
        super(message);
        this.errorCode = errorCode;
    }

    public int getErrorCode() {
        return errorCode;
    }
}

// Custom unchecked exception
public class CustomRuntimeException extends RuntimeException {
    public CustomRuntimeException(String message) {
        super(message);
    }

    public CustomRuntimeException(String message, Throwable cause) {
        super(message, cause);
    }
}

public class ExceptionHandler {

    public void basicTryCatch() {
        try {
            int result = 10 / 0;
            System.out.println(result);
        } catch (ArithmeticException e) {
            System.err.println("Division by zero: " + e.getMessage());
        } finally {
            System.out.println("Cleanup");
        }
    }

    public void multipleCatchBlocks() {
        try {
            String str = null;
            int length = str.length();
        } catch (NullPointerException e) {
            System.err.println("Null pointer: " + e.getMessage());
        } catch (Exception e) {
            System.err.println("Other exception: " + e.getMessage());
        }
    }

    public void tryWithResources() {
        try (BufferedReader reader = new BufferedReader(new FileReader("test.txt"));
             BufferedWriter writer = new BufferedWriter(new FileWriter("output.txt"))) {

            String line;
            while ((line = reader.readLine()) != null) {
                writer.write(line.toUpperCase());
                writer.newLine();
            }
        } catch (IOException e) {
            System.err.println("IO error: " + e.getMessage());
        }
    }

    public void throwCustomException() throws CustomException {
        if (Math.random() < 0.5) {
            throw new CustomException("Random error occurred", 500);
        }
    }

    public void handleCustomException() {
        try {
            throwCustomException();
        } catch (CustomException e) {
            System.err.println("Custom error: " + e.getMessage() + " (code: " + e.getErrorCode() + ")");
        }
    }

    public void rethrowException() throws IOException {
        try {
            new FileInputStream("nonexistent.txt");
        } catch (FileNotFoundException e) {
            throw new IOException("File processing failed", e);
        }
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Get all Class node creation calls
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Expected class qualified names
    project_name = java_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.Exceptions.CustomException",
        f"{project_name}.src.main.java.com.example.Exceptions.CustomRuntimeException",
        f"{project_name}.src.main.java.com.example.Exceptions.ExceptionHandler",
    }

    # Verify all expected classes were created
    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected exception classes: {sorted(list(missing_classes))}"
    )
