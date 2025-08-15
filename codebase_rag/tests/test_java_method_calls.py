"""
Java method call parsing and inheritance testing.
Tests method invocations, inheritance patterns, polymorphism,
and method overriding.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def java_methods_project(temp_repo: Path) -> Path:
    """Create a Java project with method call patterns."""
    project_path = temp_repo / "java_methods_test"
    project_path.mkdir()

    # Create package structure
    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_basic_method_calls(
    java_methods_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic Java method call parsing."""
    test_file = (
        java_methods_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "BasicMethodCalls.java"
    )
    test_file.write_text(
        """
package com.example;

public class BasicMethodCalls {
    private String name;

    public BasicMethodCalls(String name) {
        this.name = name;
    }

    public void instanceMethod() {
        System.out.println("Instance method called");
    }

    public static void staticMethod() {
        System.out.println("Static method called");
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public void demonstrateMethodCalls() {
        // Instance method calls
        instanceMethod();
        this.instanceMethod();

        // Getter/setter calls
        String currentName = getName();
        setName("New Name");

        // Static method calls
        staticMethod();
        BasicMethodCalls.staticMethod();

        // Method chaining
        String result = getName().toUpperCase().trim();

        // System method calls
        System.out.println("Name: " + getName());
        System.err.println("Error message");

        // Math method calls
        double sqrt = Math.sqrt(25);
        int max = Math.max(10, 20);
    }

    public void callOtherObject() {
        BasicMethodCalls other = new BasicMethodCalls("Other");
        other.instanceMethod();
        String otherName = other.getName();
        other.setName("Modified");
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_methods_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Check that method calls were detected by looking at CALLS relationships
    call_relationships = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c.args) > 1 and c.args[1] == "CALLS"
    ]

    assert len(call_relationships) > 0, "No method call relationships found"


def test_inheritance_and_polymorphism(
    java_methods_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test method calls in inheritance hierarchies."""
    test_file = (
        java_methods_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "InheritanceExample.java"
    )
    test_file.write_text(
        """
package com.example;

abstract class Animal {
    protected String name;

    public Animal(String name) {
        this.name = name;
    }

    public abstract void makeSound();

    public void eat() {
        System.out.println(name + " is eating");
    }

    public String getName() {
        return name;
    }
}

class Dog extends Animal {
    private String breed;

    public Dog(String name, String breed) {
        super(name);
        this.breed = breed;
    }

    @Override
    public void makeSound() {
        System.out.println(getName() + " barks: Woof!");
    }

    public void wagTail() {
        System.out.println(getName() + " wags tail");
    }

    public String getBreed() {
        return breed;
    }
}

class Cat extends Animal {
    public Cat(String name) {
        super(name);
    }

    @Override
    public void makeSound() {
        System.out.println(getName() + " meows: Meow!");
    }

    public void purr() {
        System.out.println(getName() + " purrs");
    }
}

public class InheritanceExample {

    public void demonstratePolymorphism() {
        Animal[] animals = {
            new Dog("Buddy", "Golden Retriever"),
            new Cat("Whiskers"),
            new Dog("Max", "German Shepherd")
        };

        for (Animal animal : animals) {
            animal.makeSound(); // Polymorphic method call
            animal.eat();       // Inherited method call

            if (animal instanceof Dog) {
                Dog dog = (Dog) animal;
                dog.wagTail();
                System.out.println("Breed: " + dog.getBreed());
            } else if (animal instanceof Cat) {
                Cat cat = (Cat) animal;
                cat.purr();
            }
        }
    }

    public void demonstrateSuper() {
        Dog dog = new Dog("Rex", "Labrador");

        // These will call overridden methods
        dog.makeSound();
        dog.eat();

        // Dog-specific method
        dog.wagTail();

        // Inherited getter
        String name = dog.getName();
        String breed = dog.getBreed();
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_methods_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Check class definitions
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Expected class qualified names
    project_name = java_methods_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.InheritanceExample.Animal",
        f"{project_name}.src.main.java.com.example.InheritanceExample.Dog",
        f"{project_name}.src.main.java.com.example.InheritanceExample.Cat",
        f"{project_name}.src.main.java.com.example.InheritanceExample.InheritanceExample",
    }

    # Verify all expected classes were created
    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )

    # Check method calls via CALLS relationships
    call_relationships = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c.args) > 1 and c.args[1] == "CALLS"
    ]

    assert len(call_relationships) > 0, "No method call relationships found"


def test_interface_method_calls(
    java_methods_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test method calls through interfaces."""
    test_file = (
        java_methods_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "InterfaceExample.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

interface Drawable {
    void draw();
    void setColor(String color);

    default void clear() {
        System.out.println("Clearing drawable");
    }
}

interface Resizable {
    void resize(double factor);
    double getArea();
}

class Rectangle implements Drawable, Resizable {
    private double width, height;
    private String color;

    public Rectangle(double width, double height) {
        this.width = width;
        this.height = height;
        this.color = "black";
    }

    @Override
    public void draw() {
        System.out.println("Drawing " + color + " rectangle: " + width + "x" + height);
    }

    @Override
    public void setColor(String color) {
        this.color = color;
    }

    @Override
    public void resize(double factor) {
        this.width *= factor;
        this.height *= factor;
    }

    @Override
    public double getArea() {
        return width * height;
    }
}

class Circle implements Drawable, Resizable {
    private double radius;
    private String color;

    public Circle(double radius) {
        this.radius = radius;
        this.color = "black";
    }

    @Override
    public void draw() {
        System.out.println("Drawing " + color + " circle with radius: " + radius);
    }

    @Override
    public void setColor(String color) {
        this.color = color;
    }

    @Override
    public void resize(double factor) {
        this.radius *= factor;
    }

    @Override
    public double getArea() {
        return Math.PI * radius * radius;
    }
}

public class InterfaceExample {

    public void demonstrateInterfaceCalls() {
        List<Drawable> drawables = new ArrayList<>();
        drawables.add(new Rectangle(10, 5));
        drawables.add(new Circle(3));

        for (Drawable drawable : drawables) {
            drawable.setColor("red");
            drawable.draw();
            drawable.clear(); // Default interface method
        }
    }

    public void demonstrateMultipleInterfaces() {
        Rectangle rect = new Rectangle(4, 6);
        Circle circle = new Circle(2);

        // Use as Drawable
        processDrawable(rect);
        processDrawable(circle);

        // Use as Resizable
        processResizable(rect);
        processResizable(circle);
    }

    private void processDrawable(Drawable drawable) {
        drawable.setColor("blue");
        drawable.draw();
    }

    private void processResizable(Resizable resizable) {
        double originalArea = resizable.getArea();
        resizable.resize(1.5);
        double newArea = resizable.getArea();
        System.out.println("Area changed from " + originalArea + " to " + newArea);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_methods_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Check interface and class definitions
    all_calls = mock_ingestor.ensure_node_batch.call_args_list

    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    # Expected type qualified names
    project_name = java_methods_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.InterfaceExample.Rectangle",
        f"{project_name}.src.main.java.com.example.InterfaceExample.Circle",
        f"{project_name}.src.main.java.com.example.InterfaceExample.InterfaceExample",
    }

    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.InterfaceExample.Drawable",
        f"{project_name}.src.main.java.com.example.InterfaceExample.Resizable",
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

    # Check interface method calls via CALLS relationships
    call_relationships = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c.args) > 1 and c.args[1] == "CALLS"
    ]

    assert len(call_relationships) > 0, "No method call relationships found"


def test_generic_method_calls(
    java_methods_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test method calls with generics."""
    test_file = (
        java_methods_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "GenericMethods.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

public class GenericMethods {

    public <T> T getFirst(List<T> list) {
        return list.isEmpty() ? null : list.get(0);
    }

    public <T> void swap(List<T> list, int i, int j) {
        T temp = list.get(i);
        list.set(i, list.get(j));
        list.set(j, temp);
    }

    public <K, V> Map<K, V> createMap(K[] keys, V[] values) {
        Map<K, V> map = new HashMap<>();
        for (int i = 0; i < Math.min(keys.length, values.length); i++) {
            map.put(keys[i], values[i]);
        }
        return map;
    }

    public void demonstrateGenericCalls() {
        // Generic method calls with type inference
        List<String> names = Arrays.asList("Alice", "Bob", "Charlie");
        String first = getFirst(names);
        swap(names, 0, 1);

        List<Integer> numbers = Arrays.asList(1, 2, 3, 4, 5);
        Integer firstNumber = getFirst(numbers);
        swap(numbers, 1, 3);

        // Generic method calls with explicit types
        String[] keyArray = {"a", "b", "c"};
        Integer[] valueArray = {1, 2, 3};
        Map<String, Integer> map = this.<String, Integer>createMap(keyArray, valueArray);

        // Collection method calls
        names.add("David");
        names.remove("Alice");
        boolean contains = names.contains("Bob");
        int size = names.size();

        // Map method calls
        map.put("d", 4);
        Integer value = map.get("a");
        boolean hasKey = map.containsKey("b");
        Set<String> keys = map.keySet();
        Collection<Integer> values = map.values();
    }

    public void demonstrateStreamAPI() {
        List<String> words = Arrays.asList("hello", "world", "java", "stream");

        // Stream method chaining
        List<String> result = words.stream()
            .filter(word -> word.length() > 4)
            .map(String::toUpperCase)
            .sorted()
            .collect(Collectors.toList());

        // Method references
        words.forEach(System.out::println);
        words.stream().mapToInt(String::length).sum();
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_methods_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Check generic method calls via CALLS relationships
    call_relationships = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c.args) > 1 and c.args[1] == "CALLS"
    ]

    assert len(call_relationships) > 0, "No generic method call relationships found"
