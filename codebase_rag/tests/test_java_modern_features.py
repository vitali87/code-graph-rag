"""
Java modern features parsing testing (Java 8+).
Tests records, sealed classes, pattern matching, switch expressions, text blocks,
var keyword, and other modern Java language features.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def java_modern_project(temp_repo: Path) -> Path:
    """Create a Java project for testing modern features."""
    project_path = temp_repo / "java_modern_test"
    project_path.mkdir()

    # Create standard Java project structure
    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_java_records(
    java_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java record parsing (Java 14+)."""
    test_file = (
        java_modern_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "Records.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.Objects;

// Simple record
public record Point(int x, int y) {}

// Record with validation
public record Person(String name, int age) {
    public Person {
        if (age < 0) {
            throw new IllegalArgumentException("Age cannot be negative");
        }
        Objects.requireNonNull(name, "Name cannot be null");
    }

    public String getDisplayName() {
        return name.toUpperCase();
    }
}

// Record with static methods
public record Rectangle(double width, double height) {
    public static Rectangle square(double side) {
        return new Rectangle(side, side);
    }

    public double area() {
        return width * height;
    }

    public boolean isSquare() {
        return Double.compare(width, height) == 0;
    }
}

// Generic record
public record Pair<T, U>(T first, U second) {
    public static <T, U> Pair<T, U> of(T first, U second) {
        return new Pair<>(first, second);
    }
}

// Record implementing interface
public record Circle(double radius) implements Shape {
    @Override
    public double area() {
        return Math.PI * radius * radius;
    }

    @Override
    public double perimeter() {
        return 2 * Math.PI * radius;
    }
}

interface Shape {
    double area();
    double perimeter();
}

// Record with annotations
public record Employee(
    @NotNull String name,
    @Positive int salary,
    @Email String email
) {}

@interface NotNull {}
@interface Positive {}
@interface Email {}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_modern_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify records were detected as classes (records are special classes)
    project_name = java_modern_project.name
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    expected_records = {
        f"{project_name}.src.main.java.com.example.Records.Point",
        f"{project_name}.src.main.java.com.example.Records.Person",
        f"{project_name}.src.main.java.com.example.Records.Rectangle",
        f"{project_name}.src.main.java.com.example.Records.Pair",
        f"{project_name}.src.main.java.com.example.Records.Circle",
        f"{project_name}.src.main.java.com.example.Records.Employee",
    }

    missing_records = expected_records - created_classes
    assert not missing_records, (
        f"Missing expected records: {sorted(list(missing_records))}"
    )


def test_java_sealed_classes(
    java_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java sealed classes parsing (Java 17+)."""
    test_file = (
        java_modern_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "SealedClasses.java"
    )
    test_file.write_text(
        """
package com.example;

// Sealed class with permitted subclasses
public sealed class Vehicle permits Car, Truck, Motorcycle {
    protected final String brand;

    protected Vehicle(String brand) {
        this.brand = brand;
    }

    public abstract double fuelEfficiency();
}

// Final implementation
public final class Car extends Vehicle {
    private final int doors;

    public Car(String brand, int doors) {
        super(brand);
        this.doors = doors;
    }

    @Override
    public double fuelEfficiency() {
        return 30.0; // mpg
    }
}

// Non-sealed implementation (can be extended further)
public non-sealed class Truck extends Vehicle {
    private final double payloadCapacity;

    public Truck(String brand, double payloadCapacity) {
        super(brand);
        this.payloadCapacity = payloadCapacity;
    }

    @Override
    public double fuelEfficiency() {
        return 15.0; // mpg
    }
}

// Sealed implementation
public sealed class Motorcycle extends Vehicle permits SportBike, Cruiser {
    public Motorcycle(String brand) {
        super(brand);
    }

    @Override
    public double fuelEfficiency() {
        return 50.0; // mpg
    }
}

public final class SportBike extends Motorcycle {
    public SportBike(String brand) {
        super(brand);
    }
}

public final class Cruiser extends Motorcycle {
    public Cruiser(String brand) {
        super(brand);
    }
}

// Sealed interface
public sealed interface Expression permits Value, Addition, Multiplication {
    int evaluate();
}

public record Value(int value) implements Expression {
    @Override
    public int evaluate() {
        return value;
    }
}

public record Addition(Expression left, Expression right) implements Expression {
    @Override
    public int evaluate() {
        return left.evaluate() + right.evaluate();
    }
}

public record Multiplication(Expression left, Expression right) implements Expression {
    @Override
    public int evaluate() {
        return left.evaluate() * right.evaluate();
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_modern_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify sealed classes were detected
    project_name = java_modern_project.name
    all_calls = mock_ingestor.ensure_node_batch.call_args_list

    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    expected_classes = {
        f"{project_name}.src.main.java.com.example.SealedClasses.Vehicle",
        f"{project_name}.src.main.java.com.example.SealedClasses.Car",
        f"{project_name}.src.main.java.com.example.SealedClasses.Truck",
        f"{project_name}.src.main.java.com.example.SealedClasses.Motorcycle",
        f"{project_name}.src.main.java.com.example.SealedClasses.SportBike",
        f"{project_name}.src.main.java.com.example.SealedClasses.Cruiser",
        f"{project_name}.src.main.java.com.example.SealedClasses.Value",
        f"{project_name}.src.main.java.com.example.SealedClasses.Addition",
        f"{project_name}.src.main.java.com.example.SealedClasses.Multiplication",
    }

    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.SealedClasses.Expression",
    }

    missing_classes = expected_classes - created_classes
    missing_interfaces = expected_interfaces - created_interfaces

    assert not missing_classes, (
        f"Missing expected sealed classes: {sorted(list(missing_classes))}"
    )
    assert not missing_interfaces, (
        f"Missing expected sealed interfaces: {sorted(list(missing_interfaces))}"
    )


def test_java_switch_expressions(
    java_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java switch expressions parsing (Java 14+)."""
    test_file = (
        java_modern_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "SwitchExpressions.java"
    )
    test_file.write_text(
        """
package com.example;

public class SwitchExpressions {

    // Classic switch expression with arrow syntax
    public String getDayType(String day) {
        return switch (day) {
            case "Monday", "Tuesday", "Wednesday", "Thursday", "Friday" -> "Weekday";
            case "Saturday", "Sunday" -> "Weekend";
            default -> "Invalid day";
        };
    }

    // Switch expression with yield
    public int getQuarter(int month) {
        return switch (month) {
            case 1, 2, 3 -> {
                System.out.println("First quarter");
                yield 1;
            }
            case 4, 5, 6 -> {
                System.out.println("Second quarter");
                yield 2;
            }
            case 7, 8, 9 -> {
                System.out.println("Third quarter");
                yield 3;
            }
            case 10, 11, 12 -> {
                System.out.println("Fourth quarter");
                yield 4;
            }
            default -> {
                System.err.println("Invalid month: " + month);
                yield -1;
            }
        };
    }

    // Switch with enums
    public double getDiscount(CustomerType type) {
        return switch (type) {
            case REGULAR -> 0.0;
            case PREMIUM -> 0.1;
            case VIP -> 0.2;
            case EMPLOYEE -> 0.5;
        };
    }

    // Switch with patterns (conceptual - for future Java versions)
    public String processObject(Object obj) {
        return switch (obj) {
            case String s -> "String: " + s;
            case Integer i -> "Integer: " + i;
            case Double d -> "Double: " + d;
            case null -> "null value";
            default -> "Unknown type: " + obj.getClass().getSimpleName();
        };
    }

    // Nested switch expressions
    public String complexLogic(int x, int y) {
        return switch (x) {
            case 1 -> switch (y) {
                case 1 -> "both one";
                case 2 -> "x=1, y=2";
                default -> "x=1, y=" + y;
            };
            case 2 -> switch (y) {
                case 1 -> "x=2, y=1";
                case 2 -> "both two";
                default -> "x=2, y=" + y;
            };
            default -> "x=" + x + ", y=" + y;
        };
    }

    // Switch statement (traditional)
    public void traditionalSwitch(CustomerType type) {
        switch (type) {
            case REGULAR:
                System.out.println("Regular customer");
                break;
            case PREMIUM:
                System.out.println("Premium customer");
                break;
            case VIP:
                System.out.println("VIP customer");
                // fall through
            case EMPLOYEE:
                System.out.println("Special handling");
                break;
            default:
                System.out.println("Unknown customer type");
        }
    }
}

enum CustomerType {
    REGULAR, PREMIUM, VIP, EMPLOYEE
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_modern_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify the class and enum were detected
    project_name = java_modern_project.name
    all_calls = mock_ingestor.ensure_node_batch.call_args_list

    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    enum_calls = [call for call in all_calls if call[0][0] == "Enum"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_enums = {call[0][1]["qualified_name"] for call in enum_calls}

    expected_classes = {
        f"{project_name}.src.main.java.com.example.SwitchExpressions.SwitchExpressions",
    }

    expected_enums = {
        f"{project_name}.src.main.java.com.example.SwitchExpressions.CustomerType",
    }

    missing_classes = expected_classes - created_classes
    missing_enums = expected_enums - created_enums

    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )
    assert not missing_enums, f"Missing expected enums: {sorted(list(missing_enums))}"


def test_java_text_blocks(
    java_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java text blocks parsing (Java 15+)."""
    test_file = (
        java_modern_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "TextBlocks.java"
    )
    test_file.write_text(
        '''
package com.example;

public class TextBlocks {

    // Simple text block
    public String getHtmlTemplate() {
        return """
               <html>
                   <head>
                       <title>My Page</title>
                   </head>
                   <body>
                       <h1>Welcome!</h1>
                   </body>
               </html>
               """;
    }

    // JSON text block
    public String getJsonTemplate() {
        return """
               {
                   "name": "John Doe",
                   "age": 30,
                   "address": {
                       "street": "123 Main St",
                       "city": "Anytown",
                       "zipCode": "12345"
                   },
                   "hobbies": ["reading", "swimming", "coding"]
               }
               """;
    }

    // SQL text block
    public String getSqlQuery() {
        return """
               SELECT u.name, u.email, p.title
               FROM users u
               JOIN posts p ON u.id = p.author_id
               WHERE u.active = true
                 AND p.published_date > ?
               ORDER BY p.published_date DESC
               LIMIT 10
               """;
    }

    // Text block with interpolation-like formatting
    public String getFormattedMessage(String name, int age) {
        return """
               Dear %s,

               We are pleased to inform you that your account has been created.
               Your age on file is %d years.

               Best regards,
               The Team
               """.formatted(name, age);
    }

    // Text block with escape sequences
    public String getTextWithEscapes() {
        return """
               This text contains:
               - A tab: \\t
               - A newline: \\n
               - A quote: \\"
               - A backslash: \\\\
               """;
    }

    // Concatenated text blocks
    public String getCombinedText() {
        String header = """
                        ===============
                        REPORT HEADER
                        ===============
                        """;

        String body = """
                      Content goes here...
                      More content...
                      """;

        String footer = """
                        ===============
                        END OF REPORT
                        ===============
                        """;

        return header + body + footer;
    }

    // Text block in array
    public String[] getTemplates() {
        return new String[] {
            """
            Template 1:
            Hello, World!
            """,
            """
            Template 2:
            Goodbye, World!
            """,
            """
            Template 3:
            See you later!
            """
        };
    }
}
'''
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_modern_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify the class was detected despite text blocks
    project_name = java_modern_project.name
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    expected_classes = {
        f"{project_name}.src.main.java.com.example.TextBlocks.TextBlocks",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_java_var_keyword(
    java_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java var keyword parsing (Java 10+)."""
    test_file = (
        java_modern_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "VarKeyword.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.stream.Collectors;

public class VarKeyword {

    public void demonstrateVar() {
        // Basic var usage
        var message = "Hello, World!";
        var number = 42;
        var decimal = 3.14;
        var flag = true;

        // Collections with var
        var list = new ArrayList<String>();
        var map = new HashMap<String, Integer>();
        var set = new HashSet<String>();

        // Generic type inference
        var stringList = List.of("apple", "banana", "cherry");
        var numberMap = Map.of("one", 1, "two", 2, "three", 3);

        // Complex generic types
        var complexMap = new HashMap<String, List<Integer>>();
        complexMap.put("numbers", List.of(1, 2, 3, 4, 5));

        // Method return types
        var result = processData();
        var optional = findUser("john");

        // Lambda expressions with var
        var predicate = (String s) -> s.length() > 5;
        var function = (Integer i) -> i * 2;

        // Stream operations
        var filtered = stringList.stream()
            .filter(s -> s.startsWith("a"))
            .collect(Collectors.toList());
    }

    public void varInLoops() {
        var numbers = List.of(1, 2, 3, 4, 5);

        // Enhanced for loop
        for (var num : numbers) {
            System.out.println(num);
        }

        // Traditional for loop
        for (var i = 0; i < numbers.size(); i++) {
            var element = numbers.get(i);
            System.out.println("Index " + i + ": " + element);
        }

        // Try-with-resources
        try (var scanner = new Scanner(System.in)) {
            var input = scanner.nextLine();
            System.out.println("You entered: " + input);
        }
    }

    public void varWithAnonymousClasses() {
        // Anonymous class with var
        var runnable = new Runnable() {
            @Override
            public void run() {
                System.out.println("Running...");
            }
        };

        // Anonymous class with complex type
        var comparator = new Comparator<String>() {
            @Override
            public int compare(String s1, String s2) {
                return s1.length() - s2.length();
            }
        };
    }

    // Method with var parameters (Java 11+)
    public void processValues(var... values) {
        for (var value : values) {
            System.out.println("Processing: " + value);
        }
    }

    private String processData() {
        return "processed";
    }

    private Optional<String> findUser(String name) {
        return Optional.of("User: " + name);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_modern_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify the class was detected despite var usage
    project_name = java_modern_project.name
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    expected_classes = {
        f"{project_name}.src.main.java.com.example.VarKeyword.VarKeyword",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_java_instanceof_patterns(
    java_modern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java instanceof pattern matching (Java 16+)."""
    test_file = (
        java_modern_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "InstanceofPatterns.java"
    )
    test_file.write_text(
        """
package com.example;

public class InstanceofPatterns {

    // Traditional instanceof
    public String processObjectTraditional(Object obj) {
        if (obj instanceof String) {
            String str = (String) obj;
            return "String: " + str.toUpperCase();
        } else if (obj instanceof Integer) {
            Integer num = (Integer) obj;
            return "Integer: " + (num * 2);
        } else if (obj instanceof Double) {
            Double d = (Double) obj;
            return "Double: " + String.format("%.2f", d);
        }
        return "Unknown type";
    }

    // Pattern matching instanceof (Java 16+)
    public String processObjectModern(Object obj) {
        if (obj instanceof String str) {
            return "String: " + str.toUpperCase();
        } else if (obj instanceof Integer num) {
            return "Integer: " + (num * 2);
        } else if (obj instanceof Double d) {
            return "Double: " + String.format("%.2f", d);
        }
        return "Unknown type";
    }

    // Complex pattern matching
    public boolean isLongString(Object obj) {
        return obj instanceof String str && str.length() > 10;
    }

    public double getNumericValue(Object obj) {
        if (obj instanceof Integer i) {
            return i.doubleValue();
        } else if (obj instanceof Double d) {
            return d;
        } else if (obj instanceof Float f) {
            return f.doubleValue();
        } else if (obj instanceof String s && s.matches("\\\\d+")) {
            return Double.parseDouble(s);
        }
        throw new IllegalArgumentException("Cannot convert to numeric value");
    }

    // Nested instanceof patterns
    public String processNestedObject(Object obj) {
        if (obj instanceof java.util.List<?> list) {
            if (!list.isEmpty() && list.get(0) instanceof String firstElement) {
                return "List of strings starting with: " + firstElement;
            } else if (!list.isEmpty() && list.get(0) instanceof Integer firstNumber) {
                return "List of integers starting with: " + firstNumber;
            }
            return "Non-empty list of unknown type";
        } else if (obj instanceof java.util.Map<?, ?> map) {
            if (!map.isEmpty()) {
                var firstEntry = map.entrySet().iterator().next();
                if (firstEntry.getKey() instanceof String key &&
                    firstEntry.getValue() instanceof String value) {
                    return "String map: " + key + " -> " + value;
                }
            }
            return "Map with unknown types";
        }
        return processObjectModern(obj);
    }

    // Pattern matching in loops
    public void processCollection(java.util.Collection<?> collection) {
        for (var item : collection) {
            if (item instanceof String str) {
                System.out.println("String: " + str.length() + " characters");
            } else if (item instanceof Number num) {
                System.out.println("Number: " + num.doubleValue());
            } else if (item instanceof Boolean bool) {
                System.out.println("Boolean: " + bool);
            } else {
                System.out.println("Other: " + item.getClass().getSimpleName());
            }
        }
    }

    // Pattern guards
    public String categorizeString(Object obj) {
        if (obj instanceof String str) {
            if (str.isEmpty()) {
                return "Empty string";
            } else if (str.length() < 10) {
                return "Short string: " + str;
            } else if (str.length() < 50) {
                return "Medium string (" + str.length() + " chars)";
            } else {
                return "Long string (" + str.length() + " chars)";
            }
        }
        return "Not a string";
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_modern_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify the class was detected
    project_name = java_modern_project.name
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    expected_classes = {
        f"{project_name}.src.main.java.com.example.InstanceofPatterns.InstanceofPatterns",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )
