from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    get_node_names,
    get_nodes,
    get_relationships,
    run_updater,
)


@pytest.fixture
def java_relationships_project(temp_repo: Path) -> Path:
    """Create a Java project structure for relationship testing."""
    project_path = temp_repo / "java_relationships_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_composition_and_aggregation_relationships(
    java_relationships_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that composition and aggregation relationships are correctly captured."""
    test_file = (
        java_relationships_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "CompositionExample.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.List;
import java.util.ArrayList;

// Engine is owned by Car (composition)
class Engine {
    private int horsepower;

    public Engine(int horsepower) {
        this.horsepower = horsepower;
    }

    public void start() {
        System.out.println("Engine starting with " + horsepower + " HP");
    }

    public void stop() {
        System.out.println("Engine stopping");
    }
}

// Passenger is not owned by Car (aggregation)
class Passenger {
    private String name;

    public Passenger(String name) {
        this.name = name;
    }

    public String getName() {
        return name;
    }

    public void enterCar() {
        System.out.println(name + " enters the car");
    }

    public void exitCar() {
        System.out.println(name + " exits the car");
    }
}

public class Car {
    private Engine engine; // Composition - Car owns Engine
    private List<Passenger> passengers; // Aggregation - Car uses Passengers
    private String brand;

    public Car(String brand, int engineHP) {
        this.brand = brand;
        this.engine = new Engine(engineHP); // Car creates and owns Engine
        this.passengers = new ArrayList<>();
    }

    public void start() {
        engine.start(); // CALLS relationship to Engine.start()
    }

    public void stop() {
        engine.stop(); // CALLS relationship to Engine.stop()
    }

    public void addPassenger(Passenger passenger) {
        passengers.add(passenger);
        passenger.enterCar(); // CALLS relationship to Passenger.enterCar()
    }

    public void removePassenger(Passenger passenger) {
        if (passengers.remove(passenger)) {
            passenger.exitCar(); // CALLS relationship to Passenger.exitCar()
        }
    }

    public void drive() {
        if (!passengers.isEmpty()) {
            start();
            System.out.println(brand + " is driving with " + passengers.size() + " passengers");
            // Call passenger methods
            for (Passenger p : passengers) {
                System.out.println("Passenger: " + p.getName());
            }
        }
    }
}
""",
    )

    run_updater(java_relationships_project, mock_ingestor, skip_if_missing="java")

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, (
        "No method call relationships found for composition/aggregation"
    )


def test_dependency_injection_relationships(
    java_relationships_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that dependency injection relationships are correctly captured."""
    test_file = (
        java_relationships_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "DependencyInjection.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

// Service interfaces
interface EmailService {
    void sendEmail(String to, String subject, String body);
}

interface LoggingService {
    void log(String message);
}

interface DatabaseService {
    void save(Object entity);
    Object findById(Long id);
}

// Service implementations
class GmailService implements EmailService {
    @Override
    public void sendEmail(String to, String subject, String body) {
        System.out.println("Sending Gmail: " + subject + " to " + to);
    }
}

class FileLoggingService implements LoggingService {
    @Override
    public void log(String message) {
        System.out.println("Logging to file: " + message);
    }
}

class PostgreSQLService implements DatabaseService {
    @Override
    public void save(Object entity) {
        System.out.println("Saving to PostgreSQL: " + entity);
    }

    @Override
    public Object findById(Long id) {
        System.out.println("Finding by ID in PostgreSQL: " + id);
        return new Object();
    }
}

// Business service that depends on other services
public class UserService {
    private final EmailService emailService;
    private final LoggingService loggingService;
    private final DatabaseService databaseService;

    // Constructor injection - dependencies passed in
    public UserService(EmailService emailService, LoggingService loggingService, DatabaseService databaseService) {
        this.emailService = emailService;
        this.loggingService = loggingService;
        this.databaseService = databaseService;
    }

    public void createUser(String name, String email) {
        loggingService.log("Creating user: " + name); // CALLS LoggingService.log()

        Object user = new Object(); // Simplified user creation
        databaseService.save(user); // CALLS DatabaseService.save()

        emailService.sendEmail(email, "Welcome", "Welcome to our service!"); // CALLS EmailService.sendEmail()

        loggingService.log("User created successfully: " + name); // CALLS LoggingService.log()
    }

    public void deleteUser(Long userId) {
        loggingService.log("Deleting user: " + userId); // CALLS LoggingService.log()

        Object user = databaseService.findById(userId); // CALLS DatabaseService.findById()

        if (user != null) {
            // Database deletion logic would go here
            loggingService.log("User deleted: " + userId); // CALLS LoggingService.log()
        }
    }

    public void sendNotification(String userEmail, String message) {
        emailService.sendEmail(userEmail, "Notification", message); // CALLS EmailService.sendEmail()
        loggingService.log("Notification sent to: " + userEmail); // CALLS LoggingService.log()
    }
}

// Application entry point that wires dependencies
class Application {
    public static void main(String[] args) {
        // Create service implementations
        EmailService emailService = new GmailService(); // CALLS GmailService constructor
        LoggingService loggingService = new FileLoggingService(); // CALLS FileLoggingService constructor
        DatabaseService databaseService = new PostgreSQLService(); // CALLS PostgreSQLService constructor

        // Inject dependencies
        UserService userService = new UserService(emailService, loggingService, databaseService);

        // Use the service
        userService.createUser("John Doe", "john@example.com"); // CALLS UserService.createUser()
        userService.sendNotification("john@example.com", "Your account is ready!"); // CALLS UserService.sendNotification()
    }
}
""",
    )

    run_updater(java_relationships_project, mock_ingestor, skip_if_missing="java")

    implements_relationships = get_relationships(mock_ingestor, "IMPLEMENTS")

    assert len(implements_relationships) > 0, (
        "No interface implementation relationships found"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, (
        "No method call relationships found for dependency injection"
    )


def test_cross_package_relationships(
    java_relationships_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that relationships across different packages are correctly captured."""
    (
        java_relationships_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "service"
    ).mkdir()
    (
        java_relationships_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "model"
    ).mkdir()

    model_file = (
        java_relationships_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "model"
        / "User.java"
    )
    model_file.write_text(
        encoding="utf-8",
        data="""
package com.example.model;

public class User {
    private Long id;
    private String name;
    private String email;

    public User() {}

    public User(String name, String email) {
        this.name = name;
        this.email = email;
    }

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public String getEmail() {
        return email;
    }

    public void setEmail(String email) {
        this.email = email;
    }

    public String getDisplayName() {
        return name + " (" + email + ")";
    }
}
""",
    )

    service_file = (
        java_relationships_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "service"
        / "UserRepository.java"
    )
    service_file.write_text(
        encoding="utf-8",
        data="""
package com.example.service;

import com.example.model.User;
import java.util.List;
import java.util.ArrayList;

public class UserRepository {
    private List<User> users;

    public UserRepository() {
        this.users = new ArrayList<>();
        initializeDefaultUsers();
    }

    private void initializeDefaultUsers() {
        // Cross-package CALLS - creates User objects
        User admin = new User("Admin", "admin@example.com");
        admin.setId(1L);
        users.add(admin);

        User guest = new User("Guest", "guest@example.com");
        guest.setId(2L);
        users.add(guest);
    }

    public User findById(Long id) {
        for (User user : users) {
            if (user.getId().equals(id)) { // CALLS User.getId()
                return user;
            }
        }
        return null;
    }

    public User findByEmail(String email) {
        for (User user : users) {
            if (user.getEmail().equals(email)) { // CALLS User.getEmail()
                return user;
            }
        }
        return null;
    }

    public void save(User user) {
        if (user.getId() == null) { // CALLS User.getId()
            user.setId((long) (users.size() + 1)); // CALLS User.setId()
        }
        users.add(user);
    }

    public void update(User user) {
        User existing = findById(user.getId()); // CALLS this.findById() and User.getId()
        if (existing != null) {
            existing.setName(user.getName()); // CALLS User.setName() and User.getName()
            existing.setEmail(user.getEmail()); // CALLS User.setEmail() and User.getEmail()
        }
    }

    public List<User> findAll() {
        return new ArrayList<>(users);
    }

    public String getUserSummary(Long userId) {
        User user = findById(userId); // CALLS this.findById()
        if (user != null) {
            return user.getDisplayName(); // CALLS User.getDisplayName()
        }
        return "User not found";
    }
}
""",
    )

    controller_file = (
        java_relationships_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "UserController.java"
    )
    controller_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import com.example.model.User;
import com.example.service.UserRepository;

public class UserController {
    private UserRepository userRepository;

    public UserController() {
        this.userRepository = new UserRepository(); // CALLS UserRepository constructor
    }

    public String createUser(String name, String email) {
        User newUser = new User(name, email); // CALLS User constructor
        userRepository.save(newUser); // CALLS UserRepository.save()

        return "User created: " + newUser.getDisplayName(); // CALLS User.getDisplayName()
    }

    public String getUserInfo(Long userId) {
        User user = userRepository.findById(userId); // CALLS UserRepository.findById()
        if (user != null) {
            return "User: " + user.getName() + " - " + user.getEmail(); // CALLS User.getName() and User.getEmail()
        }
        return "User not found";
    }

    public String updateUserEmail(Long userId, String newEmail) {
        User user = userRepository.findById(userId); // CALLS UserRepository.findById()
        if (user != null) {
            String oldEmail = user.getEmail(); // CALLS User.getEmail()
            user.setEmail(newEmail); // CALLS User.setEmail()
            userRepository.update(user); // CALLS UserRepository.update()

            return "Email updated from " + oldEmail + " to " + newEmail;
        }
        return "User not found";
    }

    public String getAllUserSummaries() {
        StringBuilder result = new StringBuilder();
        for (Long id = 1L; id <= 10L; id++) {
            String summary = userRepository.getUserSummary(id); // CALLS UserRepository.getUserSummary()
            if (!summary.equals("User not found")) {
                result.append(summary).append("\\n");
            }
        }
        return result.toString();
    }
}
""",
    )

    run_updater(java_relationships_project, mock_ingestor, skip_if_missing="java")

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, (
        "No cross-package method call relationships found"
    )

    created_classes = get_node_names(mock_ingestor, "Class")
    project_name = java_relationships_project.name

    expected_user_class = f"{project_name}.src.main.java.com.example.model.User.User"
    expected_repo_class = f"{project_name}.src.main.java.com.example.service.UserRepository.UserRepository"
    expected_controller_class = (
        f"{project_name}.src.main.java.com.example.UserController.UserController"
    )

    assert any(expected_user_class in qn for qn in created_classes), (
        "User model class not found"
    )
    assert any(expected_repo_class in qn for qn in created_classes), (
        "UserRepository service class not found"
    )
    assert any(expected_controller_class in qn for qn in created_classes), (
        "UserController class not found"
    )


def test_method_overriding_relationships(
    java_relationships_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that method overriding relationships are correctly captured."""
    test_file = (
        java_relationships_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "OverrideExample.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

abstract class Shape {
    protected String color;

    public Shape(String color) {
        this.color = color;
    }

    public abstract double getArea();
    public abstract double getPerimeter();

    public String getColor() {
        return color;
    }

    public void setColor(String color) {
        this.color = color;
    }

    public String getDescription() {
        return "A " + color + " shape with area " + getArea(); // CALLS this.getArea()
    }
}

class Rectangle extends Shape {
    private double width;
    private double height;

    public Rectangle(String color, double width, double height) {
        super(color); // CALLS Shape constructor
        this.width = width;
        this.height = height;
    }

    @Override
    public double getArea() {
        return width * height;
    }

    @Override
    public double getPerimeter() {
        return 2 * (width + height);
    }

    public double getWidth() {
        return width;
    }

    public double getHeight() {
        return height;
    }

    @Override
    public String getDescription() {
        return super.getDescription() + " (Rectangle: " + width + "x" + height + ")"; // CALLS Shape.getDescription()
    }
}

class Circle extends Shape {
    private double radius;

    public Circle(String color, double radius) {
        super(color); // CALLS Shape constructor
        this.radius = radius;
    }

    @Override
    public double getArea() {
        return Math.PI * radius * radius; // CALLS Math.PI
    }

    @Override
    public double getPerimeter() {
        return 2 * Math.PI * radius; // CALLS Math.PI
    }

    public double getRadius() {
        return radius;
    }

    @Override
    public String getDescription() {
        return super.getDescription() + " (Circle: radius " + radius + ")"; // CALLS Shape.getDescription()
    }
}

public class ShapeCalculator {

    public void demonstratePolymorphism() {
        Shape[] shapes = {
            new Rectangle("red", 5, 3), // CALLS Rectangle constructor
            new Circle("blue", 2), // CALLS Circle constructor
            new Rectangle("green", 4, 4) // CALLS Rectangle constructor
        };

        for (Shape shape : shapes) {
            // These calls will resolve to overridden methods
            double area = shape.getArea(); // CALLS overridden getArea()
            double perimeter = shape.getPerimeter(); // CALLS overridden getPerimeter()
            String description = shape.getDescription(); // CALLS overridden getDescription()
            String color = shape.getColor(); // CALLS inherited getColor()

            System.out.println("Shape: " + description);
            System.out.println("Area: " + area + ", Perimeter: " + perimeter);
        }
    }

    public void compareShapes(Shape shape1, Shape shape2) {
        double area1 = shape1.getArea(); // CALLS polymorphic getArea()
        double area2 = shape2.getArea(); // CALLS polymorphic getArea()

        if (area1 > area2) {
            System.out.println("First shape is larger: " + shape1.getDescription()); // CALLS polymorphic getDescription()
        } else if (area2 > area1) {
            System.out.println("Second shape is larger: " + shape2.getDescription()); // CALLS polymorphic getDescription()
        } else {
            System.out.println("Both shapes have equal area");
        }
    }
}
""",
    )

    run_updater(java_relationships_project, mock_ingestor, skip_if_missing="java")

    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")

    assert len(inherits_relationships) > 0, "No inheritance relationships found"

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, (
        "No method call relationships found for method overriding"
    )


def test_static_method_and_field_relationships(
    java_relationships_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that static method and field access relationships are correctly captured."""
    test_file = (
        java_relationships_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "StaticExample.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

class MathUtils {
    public static final double PI = 3.14159;
    public static final double E = 2.71828;
    private static int calculationCount = 0;

    public static double square(double x) {
        calculationCount++; // Access static field
        return x * x;
    }

    public static double cube(double x) {
        calculationCount++; // Access static field
        return x * x * x;
    }

    public static double circleArea(double radius) {
        calculationCount++; // Access static field
        return PI * square(radius); // CALLS static method square()
    }

    public static double sphereVolume(double radius) {
        calculationCount++; // Access static field
        double radiusCubed = cube(radius); // CALLS static method cube()
        return (4.0 / 3.0) * PI * radiusCubed; // Access static field PI
    }

    public static int getCalculationCount() {
        return calculationCount; // Access static field
    }

    public static void resetCount() {
        calculationCount = 0; // Access static field
    }
}

class StringUtils {
    public static final String EMPTY = "";

    public static boolean isEmpty(String str) {
        return str == null || str.equals(EMPTY); // Access static field EMPTY
    }

    public static String reverse(String str) {
        if (isEmpty(str)) { // CALLS static method isEmpty()
            return EMPTY; // Access static field EMPTY
        }
        return new StringBuilder(str).reverse().toString();
    }

    public static String capitalize(String str) {
        if (isEmpty(str)) { // CALLS static method isEmpty()
            return EMPTY; // Access static field EMPTY
        }
        return str.substring(0, 1).toUpperCase() + str.substring(1).toLowerCase();
    }
}

public class StaticUsageExample {

    public void demonstrateStaticCalls() {
        // Static method calls to MathUtils
        double area = MathUtils.circleArea(5.0); // CALLS MathUtils.circleArea()
        double volume = MathUtils.sphereVolume(3.0); // CALLS MathUtils.sphereVolume()
        double squared = MathUtils.square(4.0); // CALLS MathUtils.square()

        // Static field access
        double pi = MathUtils.PI; // Access static field
        double e = MathUtils.E; // Access static field

        // Static method calls to get information
        int count = MathUtils.getCalculationCount(); // CALLS MathUtils.getCalculationCount()

        System.out.println("Area: " + area + ", Volume: " + volume);
        System.out.println("Squared: " + squared + ", Count: " + count);
        System.out.println("Constants: PI=" + pi + ", E=" + e);

        // Reset static state
        MathUtils.resetCount(); // CALLS MathUtils.resetCount()
    }

    public void demonstrateStringUtils() {
        String[] testStrings = {"hello", "", null, "WORLD", "Java"};

        for (String str : testStrings) {
            boolean empty = StringUtils.isEmpty(str); // CALLS StringUtils.isEmpty()

            if (!empty) {
                String reversed = StringUtils.reverse(str); // CALLS StringUtils.reverse()
                String capitalized = StringUtils.capitalize(str); // CALLS StringUtils.capitalize()

                System.out.println("Original: " + str);
                System.out.println("Reversed: " + reversed);
                System.out.println("Capitalized: " + capitalized);
            } else {
                System.out.println("Empty string detected");
            }
        }
    }

    public static void staticMethodDemo() {
        // Static method calling other static methods
        double result = MathUtils.circleArea(2.0) + MathUtils.sphereVolume(1.5); // CALLS multiple MathUtils methods

        String message = StringUtils.capitalize("static methods are useful"); // CALLS StringUtils.capitalize()

        System.out.println(message + ": " + result);
    }
}
""",
    )

    run_updater(java_relationships_project, mock_ingestor, skip_if_missing="java")

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, "No static method call relationships found"


def test_inner_class_relationships(
    java_relationships_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that inner class relationships are correctly captured."""
    test_file = (
        java_relationships_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "InnerClassExample.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

public class OuterClass {
    private String outerField = "Outer";
    private static String staticOuterField = "Static Outer";

    public void outerMethod() {
        System.out.println("Outer method called");
    }

    public static void staticOuterMethod() {
        System.out.println("Static outer method called");
    }

    // Non-static inner class
    public class InnerClass {
        private String innerField = "Inner";

        public void innerMethod() {
            // Access outer class members
            System.out.println("Inner method: " + outerField); // Access outer field
            outerMethod(); // CALLS outer method

            // Access static outer members
            System.out.println("Static field: " + staticOuterField); // Access static outer field
            staticOuterMethod(); // CALLS static outer method
        }

        public void demonstrateOuterAccess() {
            // Use outer class reference
            OuterClass.this.outerMethod(); // CALLS outer method via explicit reference
            System.out.println("Outer field via this: " + OuterClass.this.outerField); // Access outer field
        }
    }

    // Static nested class
    public static class StaticNestedClass {
        private String nestedField = "Nested";

        public void nestedMethod() {
            // Can only access static outer members
            System.out.println("Nested method: " + staticOuterField); // Access static outer field
            staticOuterMethod(); // CALLS static outer method

            // Cannot access non-static outer members directly
            // outerField; // This would be a compile error
        }

        public void createOuterInstance() {
            // Can create outer class instance
            OuterClass outer = new OuterClass(); // CALLS OuterClass constructor
            outer.outerMethod(); // CALLS outer method
            System.out.println("Created outer: " + outer.outerField); // Access field via instance
        }
    }

    // Method-local inner class
    public void methodWithLocalClass() {
        final String localVar = "Local Variable";

        class LocalInnerClass {
            public void localMethod() {
                // Access outer members
                System.out.println("Local inner: " + outerField); // Access outer field
                outerMethod(); // CALLS outer method

                // Access local variables (must be final or effectively final)
                System.out.println("Local var: " + localVar); // Access local variable
            }
        }

        LocalInnerClass localInner = new LocalInnerClass(); // Create local inner instance
        localInner.localMethod(); // CALLS local inner method
    }

    // Anonymous inner class
    public void demonstrateAnonymousClass() {
        Runnable task = new Runnable() {
            @Override
            public void run() {
                // Access outer members
                System.out.println("Anonymous inner: " + outerField); // Access outer field
                outerMethod(); // CALLS outer method

                // Access static members
                System.out.println("Static from anonymous: " + staticOuterField); // Access static field
                staticOuterMethod(); // CALLS static outer method
            }
        };

        task.run(); // CALLS anonymous inner run method
    }

    public void demonstrateInnerClassUsage() {
        // Create and use inner class
        InnerClass inner = new InnerClass(); // Create inner class instance
        inner.innerMethod(); // CALLS inner method
        inner.demonstrateOuterAccess(); // CALLS inner method

        // Create and use static nested class
        StaticNestedClass nested = new StaticNestedClass(); // Create static nested instance
        nested.nestedMethod(); // CALLS nested method
        nested.createOuterInstance(); // CALLS nested method

        // Use method with local class
        methodWithLocalClass(); // CALLS method that creates local inner class

        // Use anonymous class
        demonstrateAnonymousClass(); // CALLS method that creates anonymous class
    }
}
""",
    )

    run_updater(java_relationships_project, mock_ingestor, skip_if_missing="java")

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, (
        "No method call relationships found for inner classes"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    assert len(class_calls) > 0, "No class nodes found for inner class example"
