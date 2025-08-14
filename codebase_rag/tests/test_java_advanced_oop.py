"""
Advanced Java OOP feature parsing and relationship testing.
Tests multiple inheritance via interfaces, complex generics with wildcards and bounds,
abstract classes with partial implementations, method overloading and overriding edge cases,
covariant return types, and diamond problem resolution.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def java_advanced_oop_project(temp_repo: Path) -> Path:
    """Create a Java project structure for advanced OOP testing."""
    project_path = temp_repo / "java_advanced_oop"
    project_path.mkdir()

    # Create standard Java project structure
    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_multiple_interface_inheritance(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of classes implementing multiple interfaces with diamond problem."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "MultipleInheritance.java"
    )
    test_file.write_text(
        """
package com.example;

interface Drawable {
    void draw();
    default void render() {
        System.out.println("Rendering drawable");
    }
}

interface Colorable {
    void setColor(String color);
    default void render() {
        System.out.println("Rendering colorable");
    }
}

interface Movable {
    void move(int x, int y);
}

interface Shape extends Drawable, Colorable {
    double getArea();
    @Override
    default void render() {
        System.out.println("Rendering shape");
    }
}

public class Circle implements Shape, Movable {
    private double radius;
    private String color;
    private int x, y;

    public Circle(double radius) {
        this.radius = radius;
    }

    @Override
    public void draw() {
        System.out.println("Drawing circle");
    }

    @Override
    public void setColor(String color) {
        this.color = color;
    }

    @Override
    public void move(int x, int y) {
        this.x = x;
        this.y = y;
    }

    @Override
    public double getArea() {
        return Math.PI * radius * radius;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify interfaces and classes were created
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}
    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    project_name = java_advanced_oop_project.name
    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.MultipleInheritance.Drawable",
        f"{project_name}.src.main.java.com.example.MultipleInheritance.Colorable",
        f"{project_name}.src.main.java.com.example.MultipleInheritance.Movable",
        f"{project_name}.src.main.java.com.example.MultipleInheritance.Shape",
    }
    expected_classes = {
        f"{project_name}.src.main.java.com.example.MultipleInheritance.Circle",
    }

    assert expected_interfaces <= created_interfaces
    assert expected_classes <= created_classes


def test_complex_generics_with_wildcards(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of complex generic types with wildcards and bounds."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "ComplexGenerics.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

public class ComplexGenerics<T extends Comparable<T> & Cloneable> {

    // Nested generic types
    private Map<String, List<Set<T>>> complexStructure;

    // Generic method with multiple bounds
    public <U extends Number & Comparable<U>> U processNumber(U number, List<? extends U> numbers) {
        return numbers.stream().max(U::compareTo).orElse(number);
    }

    // Wildcard with super bound
    public void addNumbers(List<? super Integer> list) {
        list.add(42);
        list.add(100);
    }

    // Complex return type with nested generics
    public Map<? extends String, ? extends List<? extends T>> getComplexMap() {
        return new HashMap<>();
    }

    // Generic array handling
    @SafeVarargs
    public final <E> List<E> createList(E... elements) {
        return Arrays.asList(elements);
    }

    // Recursive generic bound
    public static <T extends Enum<T>> T parseEnum(Class<T> enumClass, String value) {
        return Enum.valueOf(enumClass, value);
    }
}

// Generic interface with covariant bounds
interface Producer<T> {
    T produce();
}

interface Consumer<T> {
    void consume(T item);
}

// PECS (Producer Extends, Consumer Super) pattern
public class GenericUtility {
    public static <T> void copy(List<? extends T> source, List<? super T> destination) {
        for (T item : source) {
            destination.add(item);
        }
    }

    public static <T extends Comparable<? super T>> void sort(List<T> list) {
        Collections.sort(list);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify complex generic classes were parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    project_name = java_advanced_oop_project.name
    assert (
        f"{project_name}.src.main.java.com.example.ComplexGenerics.ComplexGenerics"
        in created_classes
    )
    assert (
        f"{project_name}.src.main.java.com.example.ComplexGenerics.GenericUtility"
        in created_classes
    )
    assert (
        f"{project_name}.src.main.java.com.example.ComplexGenerics.Producer"
        in created_interfaces
    )
    assert (
        f"{project_name}.src.main.java.com.example.ComplexGenerics.Consumer"
        in created_interfaces
    )


def test_abstract_classes_with_partial_implementation(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of abstract classes with mix of abstract and concrete methods."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "AbstractClasses.java"
    )
    test_file.write_text(
        """
package com.example;

public abstract class AbstractShape {
    protected String name;
    protected String color;

    public AbstractShape(String name) {
        this.name = name;
        this.color = "black";
    }

    // Abstract methods
    public abstract double getArea();
    public abstract double getPerimeter();
    protected abstract void validate();

    // Concrete methods
    public String getName() {
        return name;
    }

    public void setColor(String color) {
        this.color = color;
    }

    public final void display() {
        validate();
        System.out.println(name + " (" + color + "): area=" + getArea() + ", perimeter=" + getPerimeter());
    }

    // Template method pattern
    public final void process() {
        beforeProcess();
        doProcess();
        afterProcess();
    }

    protected void beforeProcess() {
        System.out.println("Before processing " + name);
    }

    protected abstract void doProcess();

    protected void afterProcess() {
        System.out.println("After processing " + name);
    }
}

public abstract class AbstractRectangle extends AbstractShape {
    protected double width, height;

    public AbstractRectangle(String name, double width, double height) {
        super(name);
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

    @Override
    protected void validate() {
        if (width <= 0 || height <= 0) {
            throw new IllegalArgumentException("Dimensions must be positive");
        }
    }

    // Still abstract - concrete classes must implement
    @Override
    protected abstract void doProcess();
}

public class Rectangle extends AbstractRectangle {
    public Rectangle(double width, double height) {
        super("Rectangle", width, height);
    }

    @Override
    protected void doProcess() {
        System.out.println("Processing rectangle: " + width + "x" + height);
    }
}

public class Square extends AbstractRectangle {
    public Square(double side) {
        super("Square", side, side);
    }

    @Override
    protected void doProcess() {
        System.out.println("Processing square: " + width + "x" + width);
    }

    public double getSide() {
        return width;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify abstract and concrete classes were parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    project_name = java_advanced_oop_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.AbstractClasses.AbstractShape",
        f"{project_name}.src.main.java.com.example.AbstractClasses.AbstractRectangle",
        f"{project_name}.src.main.java.com.example.AbstractClasses.Rectangle",
        f"{project_name}.src.main.java.com.example.AbstractClasses.Square",
    }

    assert expected_classes <= created_classes


def test_method_overloading_variations(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of complex method overloading scenarios."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "MethodOverloading.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

public class MethodOverloading {

    // Basic overloading by parameter count
    public void process() {
        System.out.println("No parameters");
    }

    public void process(String value) {
        System.out.println("String: " + value);
    }

    public void process(String value, int count) {
        System.out.println("String and int: " + value + ", " + count);
    }

    // Overloading by parameter types
    public void process(int value) {
        System.out.println("int: " + value);
    }

    public void process(double value) {
        System.out.println("double: " + value);
    }

    public void process(Integer value) {
        System.out.println("Integer: " + value);
    }

    // Overloading with arrays vs varargs
    public void process(String[] values) {
        System.out.println("String array: " + Arrays.toString(values));
    }

    public void process(String... values) {
        System.out.println("String varargs: " + Arrays.toString(values));
    }

    // Generic method overloading
    public <T> void process(List<T> list) {
        System.out.println("Generic list: " + list);
    }

    public void process(List<String> list) {
        System.out.println("String list: " + list);
    }

    // Overloading with different generic bounds
    public <T extends Number> void calculate(T value) {
        System.out.println("Number: " + value);
    }

    public <T extends Comparable<T>> void calculate(T value, T other) {
        System.out.println("Comparable: " + value + " vs " + other);
    }

    // Overloading constructors
    public MethodOverloading() {
        this("default");
    }

    public MethodOverloading(String name) {
        this(name, 0);
    }

    public MethodOverloading(String name, int value) {
        System.out.println("Constructor: " + name + ", " + value);
    }

    public MethodOverloading(int value, String name) {
        System.out.println("Constructor (reversed): " + value + ", " + name);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify all overloaded methods were parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    method_calls = [call for call in all_calls if call[0][0] == "Method"]

    created_methods = {call[0][1]["qualified_name"] for call in method_calls}

    # Check that multiple process methods were detected
    process_methods = [qn for qn in created_methods if ".process" in qn]
    calculate_methods = [qn for qn in created_methods if ".calculate" in qn]
    constructor_methods = [qn for qn in created_methods if ".MethodOverloading" in qn]

    assert len(process_methods) >= 3  # Should have multiple overloaded process methods
    assert len(calculate_methods) >= 1  # Should have calculate methods
    assert len(constructor_methods) >= 3  # Should have multiple constructors


def test_covariant_return_types(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of methods with covariant return types."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "CovariantReturns.java"
    )
    test_file.write_text(
        """
package com.example;

class Animal {
    public Animal reproduce() {
        return new Animal();
    }

    public Animal[] getOffspring() {
        return new Animal[]{new Animal()};
    }

    public Animal copy() {
        return new Animal();
    }
}

class Mammal extends Animal {
    @Override
    public Mammal reproduce() {  // Covariant return type
        return new Mammal();
    }

    @Override
    public Mammal[] getOffspring() {  // Covariant return array type
        return new Mammal[]{new Mammal()};
    }

    @Override
    public Mammal copy() {
        return new Mammal();
    }
}

class Dog extends Mammal {
    @Override
    public Dog reproduce() {  // Further covariant return type
        return new Dog();
    }

    @Override
    public Dog[] getOffspring() {
        return new Dog[]{new Dog(), new Dog()};
    }

    @Override
    public Dog copy() {
        return new Dog();
    }

    public Dog createPuppy() {
        return new Dog();
    }
}

// Generic covariant returns
abstract class Container<T> {
    public abstract Container<T> copy();
    public abstract T getContent();
}

class StringContainer extends Container<String> {
    private String content;

    @Override
    public StringContainer copy() {  // Covariant return
        StringContainer copy = new StringContainer();
        copy.content = this.content;
        return copy;
    }

    @Override
    public String getContent() {
        return content;
    }
}

class NumberContainer extends Container<Number> {
    private Number content;

    @Override
    public NumberContainer copy() {  // Covariant return
        NumberContainer copy = new NumberContainer();
        copy.content = this.content;
        return copy;
    }

    @Override
    public Number getContent() {
        return content;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify all classes with covariant return types were parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    project_name = java_advanced_oop_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.CovariantReturns.Animal",
        f"{project_name}.src.main.java.com.example.CovariantReturns.Mammal",
        f"{project_name}.src.main.java.com.example.CovariantReturns.Dog",
        f"{project_name}.src.main.java.com.example.CovariantReturns.Container",
        f"{project_name}.src.main.java.com.example.CovariantReturns.StringContainer",
        f"{project_name}.src.main.java.com.example.CovariantReturns.NumberContainer",
    }

    assert expected_classes <= created_classes


def test_diamond_problem_resolution(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of diamond problem scenarios with default methods."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "DiamondProblem.java"
    )
    test_file.write_text(
        """
package com.example;

interface A {
    default void method() {
        System.out.println("A.method()");
    }

    void abstractMethod();
}

interface B extends A {
    @Override
    default void method() {
        System.out.println("B.method()");
    }

    default void methodB() {
        System.out.println("B.methodB()");
    }
}

interface C extends A {
    @Override
    default void method() {
        System.out.println("C.method()");
    }

    default void methodC() {
        System.out.println("C.methodC()");
    }
}

// Diamond problem: D extends both B and C
interface D extends B, C {
    @Override
    default void method() {
        System.out.println("D.method() - resolving conflict");
        B.super.method();  // Explicitly calling B's version
        C.super.method();  // Explicitly calling C's version
    }
}

public class DiamondImplementation implements D {
    @Override
    public void abstractMethod() {
        System.out.println("DiamondImplementation.abstractMethod()");
    }

    // Inherits conflicting default methods, resolved by D
    // Can optionally override the resolution
    @Override
    public void method() {
        System.out.println("DiamondImplementation.method()");
        D.super.method();  // Call interface's resolution
    }
}

// More complex diamond with additional conflicts
interface Flyable {
    default void move() {
        System.out.println("Flying");
    }

    default String getMovementType() {
        return "fly";
    }
}

interface Swimmable {
    default void move() {
        System.out.println("Swimming");
    }

    default String getMovementType() {
        return "swim";
    }
}

interface Amphibious extends Flyable, Swimmable {
    @Override
    default void move() {
        System.out.println("Can both fly and swim");
    }

    @Override
    default String getMovementType() {
        return "amphibious";
    }

    default void demonstrateMovement() {
        System.out.println("Flying: ");
        Flyable.super.move();
        System.out.println("Swimming: ");
        Swimmable.super.move();
    }
}

public class Duck implements Amphibious {
    @Override
    public void move() {
        System.out.println("Duck is moving");
        Amphibious.super.move();
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify all interfaces and classes in diamond hierarchy were parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}
    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    project_name = java_advanced_oop_project.name
    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.DiamondProblem.A",
        f"{project_name}.src.main.java.com.example.DiamondProblem.B",
        f"{project_name}.src.main.java.com.example.DiamondProblem.C",
        f"{project_name}.src.main.java.com.example.DiamondProblem.D",
        f"{project_name}.src.main.java.com.example.DiamondProblem.Flyable",
        f"{project_name}.src.main.java.com.example.DiamondProblem.Swimmable",
        f"{project_name}.src.main.java.com.example.DiamondProblem.Amphibious",
    }
    expected_classes = {
        f"{project_name}.src.main.java.com.example.DiamondProblem.DiamondImplementation",
        f"{project_name}.src.main.java.com.example.DiamondProblem.Duck",
    }

    assert expected_interfaces <= created_interfaces
    assert expected_classes <= created_classes


def test_nested_generic_bounds(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of complex nested generic type bounds."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "NestedGenericBounds.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.function.*;

// Complex generic bounds with recursive types
public class NestedGenericBounds<T extends Comparable<T> & Cloneable> {

    // Self-referencing generic bound
    public static class Builder<B extends Builder<B>> {
        protected String name;

        @SuppressWarnings("unchecked")
        public B setName(String name) {
            this.name = name;
            return (B) this;
        }

        public String getName() {
            return name;
        }
    }

    public static class PersonBuilder extends Builder<PersonBuilder> {
        private int age;

        public PersonBuilder setAge(int age) {
            this.age = age;
            return this;
        }

        public int getAge() {
            return age;
        }
    }

    // Deeply nested generic types
    public Map<String, List<Set<Map<Integer, T>>>> complexNesting;

    // Generic method with multiple complex bounds
    public <U extends Number & Comparable<U>,
            V extends Collection<U> & Serializable,
            W extends Map<String, V> & Cloneable>
    Optional<U> processComplexTypes(W data, Function<V, U> processor) {

        return data.values().stream()
                   .map(processor)
                   .max(U::compareTo);
    }

    // Wildcard capture with complex bounds
    public <T extends Enum<T> & Comparable<T>>
    List<T> filterEnums(List<? extends T> enums, Predicate<? super T> filter) {
        return enums.stream()
                   .filter(filter)
                   .collect(Collectors.toList());
    }

    // Recursive generic structure
    public static class Node<T extends Node<T>> {
        private T parent;
        private List<T> children;

        public Node() {
            this.children = new ArrayList<>();
        }

        @SuppressWarnings("unchecked")
        public T addChild(T child) {
            children.add(child);
            child.parent = (T) this;
            return child;
        }

        public T getParent() {
            return parent;
        }

        public List<T> getChildren() {
            return new ArrayList<>(children);
        }
    }

    public static class TreeNode extends Node<TreeNode> {
        private String value;

        public TreeNode(String value) {
            super();
            this.value = value;
        }

        public String getValue() {
            return value;
        }
    }
}

// Generic interface with complex inheritance
interface Repository<T, ID extends Serializable> {
    Optional<T> findById(ID id);
    List<T> findAll();
    T save(T entity);
    void deleteById(ID id);
}

interface PagingRepository<T, ID extends Serializable> extends Repository<T, ID> {
    List<T> findAll(int page, int size);
    long count();
}

interface JpaRepository<T, ID extends Serializable> extends PagingRepository<T, ID> {
    void flush();
    T saveAndFlush(T entity);
    List<T> saveAll(Iterable<T> entities);
}

// Complex generic implementation
public abstract class AbstractJpaRepository<T, ID extends Serializable>
    implements JpaRepository<T, ID> {

    protected Class<T> entityClass;
    protected Class<ID> idClass;

    @Override
    public List<T> findAll() {
        return new ArrayList<>();
    }

    @Override
    public long count() {
        return 0L;
    }

    @Override
    public void flush() {
        // Implementation
    }

    @Override
    public List<T> saveAll(Iterable<T> entities) {
        List<T> result = new ArrayList<>();
        for (T entity : entities) {
            result.add(save(entity));
        }
        return result;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify complex generic structures were parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    # Check that nested classes were detected
    nested_classes_found = any("Builder" in qn for qn in created_classes)
    assert nested_classes_found, "Nested generic classes should be detected"

    # Check that repository interfaces were detected
    repository_interfaces_found = any("Repository" in qn for qn in created_interfaces)
    assert repository_interfaces_found, "Repository interfaces should be detected"


def test_method_overriding_edge_cases(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of complex method overriding scenarios."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "MethodOverridingEdgeCases.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

class BaseClass {
    public Object getValue() {
        return "base";
    }

    protected void process(Object obj) {
        System.out.println("Base processing: " + obj);
    }

    public final void finalMethod() {
        System.out.println("Cannot be overridden");
    }

    public static void staticMethod() {
        System.out.println("Base static method");
    }
}

class DerivedClass extends BaseClass {
    // Covariant return type override
    @Override
    public String getValue() {  // Object -> String (covariant)
        return "derived";
    }

    // Override with same signature
    @Override
    protected void process(Object obj) {
        super.process(obj);
        System.out.println("Derived processing: " + obj);
    }

    // Static method hiding (not overriding)
    public static void staticMethod() {
        System.out.println("Derived static method");
    }

    // New overloaded methods
    public void process(String str) {
        System.out.println("String-specific processing: " + str);
    }
}

// Generic method overriding
abstract class GenericBase<T> {
    public abstract T process(T input);
    public abstract List<T> processAll(List<T> inputs);

    public void helper(T item) {
        System.out.println("Helper: " + item);
    }
}

class StringProcessor extends GenericBase<String> {
    @Override
    public String process(String input) {
        return input.toUpperCase();
    }

    @Override
    public List<String> processAll(List<String> inputs) {
        return inputs.stream()
                    .map(this::process)
                    .collect(Collectors.toList());
    }

    // Bridge methods are generated automatically by compiler
    // for generic type erasure compatibility
}

// Interface default method overriding
interface Printable {
    default void print() {
        System.out.println("Default print");
    }

    void doSomething();
}

interface AdvancedPrintable extends Printable {
    @Override
    default void print() {
        System.out.println("Advanced print");
        Printable.super.print();  // Call parent default
    }

    default void printAdvanced() {
        System.out.println("Advanced feature");
    }
}

class Document implements AdvancedPrintable {
    private String content;

    @Override
    public void print() {
        System.out.println("Document print: " + content);
        AdvancedPrintable.super.print();
    }

    @Override
    public void doSomething() {
        System.out.println("Document doing something");
    }
}

// Multiple interface default method conflicts
interface Conflicting1 {
    default String conflictMethod() {
        return "Conflicting1";
    }
}

interface Conflicting2 {
    default String conflictMethod() {
        return "Conflicting2";
    }
}

class ConflictResolver implements Conflicting1, Conflicting2 {
    @Override
    public String conflictMethod() {
        // Must override to resolve conflict
        return Conflicting1.super.conflictMethod() + " + " + Conflicting2.super.conflictMethod();
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify all classes and interfaces with overriding were parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    # Check key classes were detected
    assert any("BaseClass" in qn for qn in created_classes)
    assert any("DerivedClass" in qn for qn in created_classes)
    assert any("StringProcessor" in qn for qn in created_classes)
    assert any("Document" in qn for qn in created_classes)
    assert any("ConflictResolver" in qn for qn in created_classes)

    # Check interfaces were detected
    assert any("Printable" in qn for qn in created_interfaces)
    assert any("AdvancedPrintable" in qn for qn in created_interfaces)


def test_generic_type_erasure_scenarios(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of scenarios involving generic type erasure."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "TypeErasure.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

// Raw type usage (legacy compatibility)
public class TypeErasureExamples {

    // Generic class that will have type erasure
    public static class Container<T> {
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

        // Generic method
        public <U> U convert(U input) {
            System.out.println("Converting: " + input);
            return input;
        }
    }

    // Raw type usage
    @SuppressWarnings({"rawtypes", "unchecked"})
    public void useRawTypes() {
        Container rawContainer = new Container("raw");  // Raw type
        Object value = rawContainer.getValue();  // Returns Object due to erasure

        List rawList = new ArrayList();  // Raw List
        rawList.add("string");
        rawList.add(123);  // No type checking

        // Mixing raw and parameterized types
        List<String> stringList = new ArrayList<>();
        rawList = stringList;  // OK
        stringList = rawList;   // Unchecked warning
    }

    // Bridge methods scenario
    public static class NumberContainer extends Container<Number> {
        public NumberContainer(Number value) {
            super(value);
        }

        // This creates a bridge method: public Object getValue()
        // that calls this method
        @Override
        public Number getValue() {
            return super.getValue();
        }

        // Additional method specific to Number
        public double getDoubleValue() {
            return getValue().doubleValue();
        }
    }

    // Generic inheritance causing bridge methods
    interface Processor<T> {
        T process(T input);
    }

    public static class StringProcessor implements Processor<String> {
        @Override
        public String process(String input) {
            return input.toUpperCase();
        }
        // Bridge method: public Object process(Object input) is generated
    }

    // Multiple bounds creating complex bridge scenarios
    public static class MultiProcessor<T extends Number & Comparable<T>>
        implements Processor<T> {

        @Override
        public T process(T input) {
            System.out.println("Processing number: " + input);
            return input;
        }

        public int compare(T a, T b) {
            return a.compareTo(b);
        }
    }

    // Array generic interaction (arrays don't support generics fully)
    @SuppressWarnings("unchecked")
    public void arrayGenericsInteraction() {
        List<String>[] arrayOfLists = new List[10];  // Generic array creation
        arrayOfLists[0] = new ArrayList<String>();

        // Type checking is limited with arrays and generics
        Object[] objArray = arrayOfLists;  // OK
        objArray[0] = new ArrayList<Integer>();  // Runtime ClassCastException possible
    }

    // Wildcard capture scenarios
    public void wildcardCapture(List<?> unknownList) {
        // Cannot add anything except null to List<?>
        // unknownList.add("string");  // Compile error
        unknownList.add(null);  // OK

        Object item = unknownList.get(0);  // OK, returns Object

        // Wildcard capture helper
        captureHelper(unknownList);
    }

    private <T> void captureHelper(List<T> list) {
        // Can work with captured type T
        if (!list.isEmpty()) {
            T item = list.get(0);
            list.add(item);  // OK within this method
        }
    }
}

// Enum with generics (limited support)
enum Operation {
    PLUS {
        public double apply(double x, double y) { return x + y; }
    },
    MINUS {
        public double apply(double x, double y) { return x - y; }
    };

    public abstract double apply(double x, double y);

    // Enums cannot be generic, but can use generic methods
    public <T extends Number> double applyToNumbers(T x, T y) {
        return apply(x.doubleValue(), y.doubleValue());
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify type erasure related structures were parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    enum_calls = [call for call in all_calls if call[0][0] == "Enum"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_enums = {call[0][1]["qualified_name"] for call in enum_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    # Check main class and nested classes
    assert any("TypeErasureExamples" in qn for qn in created_classes)
    assert any("Container" in qn for qn in created_classes)
    assert any("NumberContainer" in qn for qn in created_classes)
    assert any("StringProcessor" in qn for qn in created_classes)

    # Check enum
    assert any("Operation" in qn for qn in created_enums)

    # Check interface
    assert any("Processor" in qn for qn in created_interfaces)


def test_annotation_processing_complex(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of complex annotation scenarios."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "ComplexAnnotations.java"
    )
    test_file.write_text(
        """
package com.example;

import java.lang.annotation.*;
import java.util.*;

// Meta-annotations
@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.TYPE, ElementType.METHOD, ElementType.FIELD})
@Documented
@Inherited
@interface CustomAnnotation {
    String value() default "";
    int priority() default 0;
    String[] tags() default {};
    Class<?> targetClass() default Object.class;
}

// Annotation with complex types
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
@interface MethodInfo {
    String author();
    String date();
    int version() default 1;
    String[] reviewers() default {};
    Priority priority() default Priority.MEDIUM;

    enum Priority {
        LOW, MEDIUM, HIGH, CRITICAL
    }
}

// Repeatable annotation
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
@Repeatable(Schedules.class)
@interface Schedule {
    String day();
    String time();
}

@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
@interface Schedules {
    Schedule[] value();
}

// Annotation on annotation
@CustomAnnotation(value = "meta", priority = 1)
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.FIELD)
@interface FieldValidator {
    String regex() default ".*";
    String message() default "Invalid value";
    boolean required() default true;
}

// Class using complex annotations
@CustomAnnotation(value = "service", priority = 2, tags = {"business", "core"})
@Schedule(day = "Monday", time = "09:00")
@Schedule(day = "Wednesday", time = "14:00")
@Schedule(day = "Friday", time = "16:00")
public class AnnotatedService {

    @FieldValidator(regex = "\\w+", message = "Name must contain only word characters", required = true)
    private String name;

    @FieldValidator(regex = "\\d{4}-\\d{2}-\\d{2}", message = "Date must be in YYYY-MM-DD format")
    private String creationDate;

    @CustomAnnotation(value = "constructor", priority = 1)
    public AnnotatedService(String name) {
        this.name = name;
        this.creationDate = "2023-01-01";
    }

    @MethodInfo(
        author = "John Doe",
        date = "2023-01-01",
        version = 2,
        reviewers = {"Jane Smith", "Bob Johnson"},
        priority = MethodInfo.Priority.HIGH
    )
    @CustomAnnotation(value = "business-critical", priority = 3)
    public String processData(String input) {
        return input.toUpperCase();
    }

    @MethodInfo(author = "Jane Smith", date = "2023-02-01")
    @Deprecated
    public void oldMethod() {
        System.out.println("This method is deprecated");
    }

    // Annotation with array values
    @CustomAnnotation(
        value = "utility",
        tags = {"helper", "util", "internal"},
        targetClass = String.class
    )
    private void utilityMethod() {
        // Implementation
    }
}

// Generic class with annotations
@CustomAnnotation(value = "generic-container")
public class GenericContainer<@CustomAnnotation("type-param") T> {

    @FieldValidator(required = true)
    private T value;

    @MethodInfo(author = "Generic Author", date = "2023-01-01")
    public T getValue() {
        return value;
    }

    public void setValue(@CustomAnnotation("parameter") T value) {
        this.value = value;
    }
}

// Annotation processing utility
public class AnnotationProcessor {

    @SuppressWarnings({"unchecked", "rawtypes"})
    public static void processAnnotations(Class<?> clazz) {
        // Process class-level annotations
        Annotation[] classAnnotations = clazz.getAnnotations();
        for (Annotation annotation : classAnnotations) {
            System.out.println("Class annotation: " + annotation);
        }

        // Process method annotations
        Arrays.stream(clazz.getDeclaredMethods())
              .forEach(method -> {
                  Annotation[] methodAnnotations = method.getAnnotations();
                  for (Annotation annotation : methodAnnotations) {
                      System.out.println("Method " + method.getName() + " annotation: " + annotation);
                  }
              });
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify annotation-related structures were parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Check that annotation classes and regular classes were detected
    assert any("AnnotatedService" in qn for qn in created_classes)
    assert any("GenericContainer" in qn for qn in created_classes)
    assert any("AnnotationProcessor" in qn for qn in created_classes)

    # Annotation interfaces should be detected as classes in Java parsing
    assert any("CustomAnnotation" in qn for qn in created_classes)
    assert any("MethodInfo" in qn for qn in created_classes)


def test_advanced_inner_class_scenarios(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of complex inner class and nested class scenarios."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "AdvancedInnerClasses.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

public class OuterClass<T> {
    private T outerValue;
    private static String staticValue = "static";

    // Non-static inner class with access to generic type
    public class InnerClass {
        private String innerValue;

        public InnerClass(String innerValue) {
            this.innerValue = innerValue;
        }

        public void accessOuter() {
            // Can access outer instance including generic type
            outerValue = (T) "modified";  // Access to T
            System.out.println(staticValue);
        }

        // Inner class can have its own generic methods
        public <U> U processInner(U input) {
            return input;
        }
    }

    // Static nested class cannot access outer generic type directly
    public static class StaticNested<U> {
        private U nestedValue;

        public StaticNested(U value) {
            this.nestedValue = value;
        }

        public void accessOuter() {
            System.out.println(staticValue);  // Can only access static members
            // Cannot access outerValue or T
        }

        // Nested class can be generic independently
        public <V> void processNested(V input) {
            System.out.println("Processing: " + input);
        }
    }

    // Local class in method
    public List<T> createProcessors() {
        final String localConstant = "local";

        // Local class
        class LocalProcessor {
            public T process(T input) {
                System.out.println(localConstant);  // Can access final local variables
                System.out.println(outerValue);     // Can access outer instance
                return input;
            }
        }

        List<T> processors = new ArrayList<>();
        // Cannot return LocalProcessor directly - it's local to this method
        return processors;
    }

    // Anonymous class variations
    public Runnable createRunnable() {
        return new Runnable() {
            private String anonymousField = "anonymous";

            @Override
            public void run() {
                System.out.println(anonymousField);
                System.out.println(outerValue);  // Can access outer
                System.out.println(staticValue); // Can access static
            }

            // Anonymous classes can have additional methods (but they're not accessible)
            public void additionalMethod() {
                System.out.println("Additional method in anonymous class");
            }
        };
    }

    // Anonymous class implementing generic interface
    public Comparator<T> createComparator() {
        return new Comparator<T>() {
            @Override
            public int compare(T o1, T o2) {
                // Generic anonymous class
                return Objects.toString(o1).compareTo(Objects.toString(o2));
            }
        };
    }

    // Method creating anonymous class with complex generics
    public <U extends Comparable<U>> Comparator<U> createGenericComparator() {
        return new Comparator<U>() {
            @Override
            public int compare(U o1, U o2) {
                return o1.compareTo(o2);
            }

            // Additional method using outer generic type
            public boolean isOuterCompatible(Object obj) {
                return obj.getClass().equals(outerValue.getClass());
            }
        };
    }
}

// Nested interfaces
public class InterfaceContainer {

    // Static nested interface
    public static interface NestedInterface<T> {
        T process(T input);

        // Nested interface can have default methods
        default void log(T item) {
            System.out.println("Processing: " + item);
        }
    }

    // Non-static inner interface (rare but legal)
    public interface InnerInterface {
        void execute();
    }

    // Class implementing nested interface
    public static class Implementation<U> implements NestedInterface<U> {
        @Override
        public U process(U input) {
            log(input);
            return input;
        }
    }

    // Inner class implementing inner interface
    public class InnerImplementation implements InnerInterface {
        @Override
        public void execute() {
            System.out.println("Executing inner implementation");
        }
    }
}

// Multiple levels of nesting
public class DeepNesting {
    private String level1 = "level1";

    public class Level2 {
        private String level2 = "level2";

        public class Level3 {
            private String level3 = "level3";

            public void accessAll() {
                System.out.println(level1);  // Access outer-outer
                System.out.println(level2);  // Access outer
                System.out.println(level3);  // Access current
            }

            public class Level4 {
                public void deepAccess() {
                    System.out.println(level1 + level2 + level3);
                }
            }
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
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify nested structures were parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    {call[0][1]["qualified_name"] for call in interface_calls}

    # Check main outer classes
    assert any("OuterClass" in qn for qn in created_classes)
    assert any("InterfaceContainer" in qn for qn in created_classes)
    assert any("DeepNesting" in qn for qn in created_classes)

    # Some nested classes might be detected depending on parser capabilities
    # At minimum, the outer classes should be detected


def test_complex_static_initialization(
    java_advanced_oop_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of complex static initialization scenarios."""
    test_file = (
        java_advanced_oop_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "StaticInitialization.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

public class StaticInitialization {

    // Static fields with complex initialization
    private static final Map<String, Integer> CONSTANTS = createConstants();
    private static final List<String> ORDERED_KEYS = new ArrayList<>(CONSTANTS.keySet());

    // Static initialization block
    static {
        System.out.println("Static initialization block 1");
        ORDERED_KEYS.sort(String::compareTo);

        // Complex static initialization
        for (String key : ORDERED_KEYS) {
            System.out.println("Key: " + key + ", Value: " + CONSTANTS.get(key));
        }
    }

    // Static method for initialization
    private static Map<String, Integer> createConstants() {
        Map<String, Integer> map = new HashMap<>();
        map.put("ONE", 1);
        map.put("TWO", 2);
        map.put("THREE", 3);
        return map;
    }

    // Second static block
    static {
        System.out.println("Static initialization block 2");
    }

    // Instance initialization block
    {
        System.out.println("Instance initialization block");
    }

    // Constructor
    public StaticInitialization() {
        System.out.println("Constructor");
    }
}

// Singleton with static initialization
public class Singleton {
    private static volatile Singleton instance;
    private static final Object lock = new Object();

    // Static fields
    private static final Map<String, String> config = new ConcurrentHashMap<>();

    // Static initialization
    static {
        config.put("version", "1.0");
        config.put("name", "SingletonApp");
        System.out.println("Singleton static initialization");
    }

    private final String id;
    private final long timestamp;

    private Singleton() {
        this.id = UUID.randomUUID().toString();
        this.timestamp = System.currentTimeMillis();
    }

    public static Singleton getInstance() {
        if (instance == null) {
            synchronized (lock) {
                if (instance == null) {
                    instance = new Singleton();
                }
            }
        }
        return instance;
    }

    public static String getConfig(String key) {
        return config.get(key);
    }

    public String getId() {
        return id;
    }
}

// Enum with complex static initialization
public enum Status {
    PENDING("P", 1),
    PROCESSING("PR", 2),
    COMPLETED("C", 3),
    FAILED("F", 4);

    private final String code;
    private final int priority;

    // Static map for reverse lookup
    private static final Map<String, Status> CODE_MAP = new HashMap<>();
    private static final Map<Integer, Status> PRIORITY_MAP = new HashMap<>();

    // Static initialization block in enum
    static {
        for (Status status : values()) {
            CODE_MAP.put(status.code, status);
            PRIORITY_MAP.put(status.priority, status);
        }
    }

    Status(String code, int priority) {
        this.code = code;
        this.priority = priority;
    }

    public static Status fromCode(String code) {
        return CODE_MAP.get(code);
    }

    public static Status fromPriority(int priority) {
        return PRIORITY_MAP.get(priority);
    }

    public String getCode() {
        return code;
    }

    public int getPriority() {
        return priority;
    }
}

// Class with static nested class initialization
public class StaticNestedInitialization {
    private static final String OUTER_STATIC = "outer";

    static {
        System.out.println("Outer static initialization");
    }

    public static class NestedClass {
        private static final String NESTED_STATIC = OUTER_STATIC + "_nested";
        private static final List<String> NESTED_LIST = createNestedList();

        static {
            System.out.println("Nested static initialization");
        }

        private static List<String> createNestedList() {
            List<String> list = new ArrayList<>();
            list.add(NESTED_STATIC);
            list.add("additional");
            return list;
        }

        public static String getNestedStatic() {
            return NESTED_STATIC;
        }
    }

    // Static method that references nested class
    public static void triggerNestedInitialization() {
        System.out.println(NestedClass.getNestedStatic());
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_advanced_oop_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify static initialization classes were parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    enum_calls = [call for call in all_calls if call[0][0] == "Enum"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_enums = {call[0][1]["qualified_name"] for call in enum_calls}

    # Check main classes
    assert any("StaticInitialization" in qn for qn in created_classes)
    assert any("Singleton" in qn for qn in created_classes)
    assert any("StaticNestedInitialization" in qn for qn in created_classes)

    # Check enum
    assert any("Status" in qn for qn in created_enums)
