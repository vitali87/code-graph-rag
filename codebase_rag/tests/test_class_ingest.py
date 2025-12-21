from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def typescript_mixin_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "ts_mixin_test"
    project_path.mkdir()

    mixin_file = project_path / "mixins.ts"
    mixin_file.write_text(
        """
interface Printable {
    print(): void;
}

interface Loggable {
    log(message: string): void;
}

class BaseClass {
    name: string;

    constructor(name: string) {
        this.name = name;
    }

    greet(): string {
        return `Hello, ${this.name}`;
    }
}

function Timestamped<TBase extends new (...args: any[]) => {}>(Base: TBase) {
    return class extends Base {
        timestamp = Date.now();

        getTimestamp(): number {
            return this.timestamp;
        }
    };
}

function Activatable<TBase extends new (...args: any[]) => {}>(Base: TBase) {
    return class extends Base {
        isActive = false;

        activate(): void {
            this.isActive = true;
        }

        deactivate(): void {
            this.isActive = false;
        }
    };
}

class User extends Timestamped(Activatable(BaseClass)) {
    email: string;

    constructor(name: string, email: string) {
        super(name);
        this.email = email;
    }

    sendEmail(): void {
        console.log(`Sending email to ${this.email}`);
    }
}

class Admin extends User {
    permissions: string[];

    constructor(name: string, email: string) {
        super(name, email);
        this.permissions = ['read', 'write', 'delete'];
    }

    grantPermission(permission: string): void {
        this.permissions.push(permission);
    }
}

interface Serializable {
    serialize(): string;
}

class Document extends BaseClass implements Serializable, Printable {
    content: string;

    constructor(name: string, content: string) {
        super(name);
        this.content = content;
    }

    serialize(): string {
        return JSON.stringify({ name: this.name, content: this.content });
    }

    print(): void {
        console.log(this.content);
    }
}

function demonstrateMixins(): void {
    const user = new User("Alice", "alice@example.com");
    user.activate();
    console.log(user.getTimestamp());
    user.greet();
    user.sendEmail();

    const admin = new Admin("Bob", "bob@example.com");
    admin.grantPermission("admin");

    const doc = new Document("Report", "This is a report");
    doc.print();
    doc.serialize();
}
"""
    )

    return project_path


def test_typescript_mixin_inheritance(
    typescript_mixin_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(typescript_mixin_project, mock_ingestor, skip_if_missing="typescript")

    project_name = typescript_mixin_project.name

    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    expected_inherits = [
        f"{project_name}.mixins.Admin",
        f"{project_name}.mixins.User",
    ]

    admin_inherits = [
        call
        for call in relationship_calls
        if call[0][0][2] == expected_inherits[0]
        and call[0][2][2] == expected_inherits[1]
    ]

    assert len(admin_inherits) >= 1, "Admin should inherit from User"


def test_typescript_interface_implementation(
    typescript_mixin_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(typescript_mixin_project, mock_ingestor, skip_if_missing="typescript")

    project_name = typescript_mixin_project.name

    interface_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Interface"
    ]

    interface_qns = {call[0][1]["qualified_name"] for call in interface_nodes}

    expected_interfaces = [
        f"{project_name}.mixins.Printable",
        f"{project_name}.mixins.Loggable",
        f"{project_name}.mixins.Serializable",
    ]

    found_interfaces = [
        iface for iface in expected_interfaces if iface in interface_qns
    ]
    assert len(found_interfaces) >= 1, (
        f"Should have at least one interface node, found: {interface_qns}"
    )


@pytest.fixture
def rust_impl_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "rust_impl_test"
    project_path.mkdir()

    lib_file = project_path / "lib.rs"
    lib_file.write_text(
        """
pub struct Point {
    x: f64,
    y: f64,
}

impl Point {
    pub fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }

    pub fn distance(&self, other: &Point) -> f64 {
        let dx = self.x - other.x;
        let dy = self.y - other.y;
        (dx * dx + dy * dy).sqrt()
    }

    pub fn translate(&mut self, dx: f64, dy: f64) {
        self.x += dx;
        self.y += dy;
    }
}

pub trait Drawable {
    fn draw(&self);
    fn area(&self) -> f64;
}

impl Drawable for Point {
    fn draw(&self) {
        println!("Drawing point at ({}, {})", self.x, self.y);
    }

    fn area(&self) -> f64 {
        0.0
    }
}

pub struct Rectangle {
    top_left: Point,
    width: f64,
    height: f64,
}

impl Rectangle {
    pub fn new(x: f64, y: f64, width: f64, height: f64) -> Self {
        Rectangle {
            top_left: Point::new(x, y),
            width,
            height,
        }
    }

    pub fn contains(&self, point: &Point) -> bool {
        point.x >= self.top_left.x
            && point.x <= self.top_left.x + self.width
            && point.y >= self.top_left.y
            && point.y <= self.top_left.y + self.height
    }
}

impl Drawable for Rectangle {
    fn draw(&self) {
        println!("Drawing rectangle at ({}, {})", self.top_left.x, self.top_left.y);
    }

    fn area(&self) -> f64 {
        self.width * self.height
    }
}

pub fn demonstrate_impl_blocks() {
    let p1 = Point::new(0.0, 0.0);
    let p2 = Point::new(3.0, 4.0);

    let distance = p1.distance(&p2);
    println!("Distance: {}", distance);

    p1.draw();

    let rect = Rectangle::new(0.0, 0.0, 10.0, 5.0);
    rect.draw();
    println!("Area: {}", rect.area());
    println!("Contains origin: {}", rect.contains(&p1));
}
"""
    )

    return project_path


def test_rust_impl_methods_are_ingested(
    rust_impl_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(rust_impl_project, mock_ingestor, skip_if_missing="rust")

    project_name = rust_impl_project.name

    method_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Method"
    ]

    expected_methods = [
        f"{project_name}.lib.Point.new",
        f"{project_name}.lib.Point.distance",
        f"{project_name}.lib.Point.translate",
        f"{project_name}.lib.Point.draw",
        f"{project_name}.lib.Point.area",
        f"{project_name}.lib.Rectangle.new",
        f"{project_name}.lib.Rectangle.contains",
        f"{project_name}.lib.Rectangle.draw",
        f"{project_name}.lib.Rectangle.area",
    ]

    method_qns = {call[0][1]["qualified_name"] for call in method_nodes}

    for expected in expected_methods:
        assert expected in method_qns, f"Method {expected} should be ingested"


def test_rust_impl_method_calls(
    rust_impl_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(rust_impl_project, mock_ingestor, skip_if_missing="rust")

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    rust_calls = [call for call in call_relationships if "lib" in call.args[0][2]]

    assert len(rust_calls) >= 3, "Expected at least 3 function calls in Rust code"


@pytest.fixture
def java_interface_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "java_interface_test"
    project_path.mkdir()

    animal_file = project_path / "Animal.java"
    animal_file.write_text(
        """
package animals;

public interface Animal {
    void makeSound();
    void move();
    String getName();
}
"""
    )

    flyable_file = project_path / "Flyable.java"
    flyable_file.write_text(
        """
package animals;

public interface Flyable {
    void fly();
    int getAltitude();
}
"""
    )

    swimmable_file = project_path / "Swimmable.java"
    swimmable_file.write_text(
        """
package animals;

public interface Swimmable {
    void swim();
    int getDepth();
}
"""
    )

    dog_file = project_path / "Dog.java"
    dog_file.write_text(
        """
package animals;

public class Dog implements Animal {
    private String name;

    public Dog(String name) {
        this.name = name;
    }

    @Override
    public void makeSound() {
        System.out.println(name + " barks: Woof!");
    }

    @Override
    public void move() {
        System.out.println(name + " runs on four legs");
    }

    @Override
    public String getName() {
        return this.name;
    }

    public void fetch() {
        System.out.println(name + " fetches the ball");
    }
}
"""
    )

    duck_file = project_path / "Duck.java"
    duck_file.write_text(
        """
package animals;

public class Duck implements Animal, Flyable, Swimmable {
    private String name;
    private int altitude;
    private int depth;

    public Duck(String name) {
        this.name = name;
        this.altitude = 0;
        this.depth = 0;
    }

    @Override
    public void makeSound() {
        System.out.println(name + " quacks: Quack!");
    }

    @Override
    public void move() {
        System.out.println(name + " waddles");
    }

    @Override
    public String getName() {
        return this.name;
    }

    @Override
    public void fly() {
        this.altitude = 100;
        System.out.println(name + " flies up to " + altitude + " meters");
    }

    @Override
    public int getAltitude() {
        return this.altitude;
    }

    @Override
    public void swim() {
        this.depth = 5;
        System.out.println(name + " swims at depth " + depth + " meters");
    }

    @Override
    public int getDepth() {
        return this.depth;
    }
}
"""
    )

    main_file = project_path / "Main.java"
    main_file.write_text(
        """
package animals;

public class Main {
    public static void main(String[] args) {
        Dog dog = new Dog("Buddy");
        dog.makeSound();
        dog.move();
        dog.fetch();

        Duck duck = new Duck("Daffy");
        duck.makeSound();
        duck.fly();
        duck.swim();

        Animal[] animals = {dog, duck};
        for (Animal animal : animals) {
            animal.makeSound();
            animal.move();
        }
    }
}
"""
    )

    return project_path


def test_java_single_interface_implementation(
    java_interface_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(java_interface_project, mock_ingestor, skip_if_missing="java")

    project_name = java_interface_project.name

    implements_rels = get_relationships(mock_ingestor, "IMPLEMENTS")

    dog_implements = [
        call
        for call in implements_rels
        if f"{project_name}.Dog.Dog" in call.args[0][2]
        or f"{project_name}.Dog" in call.args[0][2]
    ]

    assert len(dog_implements) >= 1, "Dog should implement Animal interface"


def test_java_multiple_interface_implementation(
    java_interface_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(java_interface_project, mock_ingestor, skip_if_missing="java")

    project_name = java_interface_project.name

    implements_rels = get_relationships(mock_ingestor, "IMPLEMENTS")

    duck_implements = [
        call
        for call in implements_rels
        if f"{project_name}.Duck.Duck" in call.args[0][2]
        or f"{project_name}.Duck" in call.args[0][2]
    ]

    assert len(duck_implements) >= 1, "Duck should implement multiple interfaces"


def test_java_interface_nodes_created(
    java_interface_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(java_interface_project, mock_ingestor, skip_if_missing="java")

    interface_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Interface"
    ]

    interface_qns = {call[0][1]["qualified_name"] for call in interface_nodes}

    assert len(interface_qns) >= 1, "Should have at least one interface node"


@pytest.fixture
def inline_module_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "inline_module_test"
    project_path.mkdir()

    main_rs = project_path / "main.rs"
    main_rs.write_text(
        """
mod utils {
    pub fn helper() -> i32 {
        42
    }

    pub mod nested {
        pub fn deep_helper() -> i32 {
            100
        }
    }
}

mod config {
    pub struct Settings {
        pub debug: bool,
    }

    impl Settings {
        pub fn new() -> Self {
            Settings { debug: false }
        }
    }
}

fn main() {
    let value = utils::helper();
    let deep_value = utils::nested::deep_helper();
    let settings = config::Settings::new();

    println!("Value: {}", value);
    println!("Deep value: {}", deep_value);
    println!("Debug: {}", settings.debug);
}
"""
    )

    return project_path


def test_rust_inline_modules_are_ingested(
    inline_module_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(inline_module_project, mock_ingestor, skip_if_missing="rust")

    module_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Module"
    ]

    module_names = {call[0][1].get("name", "") for call in module_nodes}

    assert "main" in module_names or any("main" in name for name in module_names), (
        "main module should be ingested"
    )


@pytest.fixture
def method_override_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "override_test"
    project_path.mkdir()

    (project_path / "__init__.py").write_text("")

    base_file = project_path / "base.py"
    base_file.write_text(
        """
class BaseClass:
    def method_a(self):
        return "BaseClass.method_a"

    def method_b(self):
        return "BaseClass.method_b"

    def method_c(self):
        return "BaseClass.method_c"


class MiddleClass(BaseClass):
    def method_a(self):
        return "MiddleClass.method_a"

    def method_d(self):
        return "MiddleClass.method_d"


class DerivedClass(MiddleClass):
    def method_a(self):
        return "DerivedClass.method_a"

    def method_b(self):
        return "DerivedClass.method_b"

    def method_e(self):
        return "DerivedClass.method_e"
"""
    )

    return project_path


def test_method_override_chain(
    method_override_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=method_override_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = method_override_project.name

    override_rels = get_relationships(mock_ingestor, "OVERRIDES")

    derived_method_a_overrides = [
        call
        for call in override_rels
        if call.args[0][2] == f"{project_name}.base.DerivedClass.method_a"
    ]

    assert len(derived_method_a_overrides) == 1, (
        "DerivedClass.method_a should override MiddleClass.method_a (not BaseClass)"
    )

    if derived_method_a_overrides:
        parent_qn = derived_method_a_overrides[0].args[2][2]
        assert f"{project_name}.base.MiddleClass.method_a" == parent_qn, (
            f"Should override MiddleClass.method_a, not {parent_qn}"
        )


def test_method_override_skips_non_overriding_methods(
    method_override_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=method_override_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = method_override_project.name

    override_rels = get_relationships(mock_ingestor, "OVERRIDES")

    method_e_overrides = [
        call
        for call in override_rels
        if call.args[0][2] == f"{project_name}.base.DerivedClass.method_e"
    ]

    assert len(method_e_overrides) == 0, (
        "DerivedClass.method_e should not have any override relationships"
    )


@pytest.fixture
def nested_class_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "nested_class_test"
    project_path.mkdir()

    (project_path / "__init__.py").write_text("")

    nested_file = project_path / "nested.py"
    nested_file.write_text(
        """
class OuterClass:
    class InnerClass:
        def inner_method(self):
            return "inner"

        class DeepNestedClass:
            def deep_method(self):
                return "deep"

    def outer_method(self):
        return "outer"

    class AnotherInner:
        def another_method(self):
            return "another"
"""
    )

    return project_path


def test_nested_class_qualified_names(
    nested_class_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=nested_class_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = nested_class_project.name

    class_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    class_qns = {call[0][1]["qualified_name"] for call in class_nodes}

    assert f"{project_name}.nested.OuterClass" in class_qns, "OuterClass should exist"


def test_nested_class_method_qualified_names(
    nested_class_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=nested_class_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = nested_class_project.name

    method_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Method"
    ]

    method_qns = {call[0][1]["qualified_name"] for call in method_nodes}

    assert f"{project_name}.nested.OuterClass.outer_method" in method_qns, (
        "outer_method should have correct qualified name"
    )


@pytest.fixture
def abstract_class_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "abstract_test"
    project_path.mkdir()

    (project_path / "__init__.py").write_text("")

    abstract_file = project_path / "abstract_classes.py"
    abstract_file.write_text(
        """
from abc import ABC, abstractmethod


class Shape(ABC):
    @abstractmethod
    def area(self):
        pass

    @abstractmethod
    def perimeter(self):
        pass

    def describe(self):
        return f"Shape with area {self.area()}"


class Circle(Shape):
    def __init__(self, radius):
        self.radius = radius

    def area(self):
        return 3.14159 * self.radius ** 2

    def perimeter(self):
        return 2 * 3.14159 * self.radius


class Rectangle(Shape):
    def __init__(self, width, height):
        self.width = width
        self.height = height

    def area(self):
        return self.width * self.height

    def perimeter(self):
        return 2 * (self.width + self.height)

    def describe(self):
        return f"Rectangle {self.width}x{self.height}"
"""
    )

    return project_path


def test_abstract_method_overrides(
    abstract_class_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=abstract_class_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = abstract_class_project.name

    override_rels = get_relationships(mock_ingestor, "OVERRIDES")

    circle_area_overrides = [
        call
        for call in override_rels
        if call.args[0][2] == f"{project_name}.abstract_classes.Circle.area"
    ]

    assert len(circle_area_overrides) == 1, "Circle.area should override Shape.area"


def test_non_abstract_method_override(
    abstract_class_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=abstract_class_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = abstract_class_project.name

    override_rels = get_relationships(mock_ingestor, "OVERRIDES")

    rect_describe_overrides = [
        call
        for call in override_rels
        if call.args[0][2] == f"{project_name}.abstract_classes.Rectangle.describe"
    ]

    assert len(rect_describe_overrides) == 1, (
        "Rectangle.describe should override Shape.describe"
    )


@pytest.fixture
def js_class_expression_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "js_class_expr_test"
    project_path.mkdir()

    class_expr_file = project_path / "class_expressions.js"
    class_expr_file.write_text(
        """
const Animal = class {
    constructor(name) {
        this.name = name;
    }

    speak() {
        console.log(`${this.name} makes a sound`);
    }
};

const Dog = class extends Animal {
    constructor(name, breed) {
        super(name);
        this.breed = breed;
    }

    speak() {
        console.log(`${this.name} barks`);
    }

    fetch() {
        console.log(`${this.name} fetches the ball`);
    }
};

const NamedClass = class MyClass {
    constructor(value) {
        this.value = value;
    }

    getValue() {
        return this.value;
    }
};

function createAnimal() {
    const dog = new Dog("Buddy", "Labrador");
    dog.speak();
    dog.fetch();

    const animal = new Animal("Generic");
    animal.speak();
}
"""
    )

    return project_path


def test_js_class_expression_inheritance(
    js_class_expression_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(
        js_class_expression_project, mock_ingestor, skip_if_missing="javascript"
    )

    class_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    class_qns = {call[0][1]["qualified_name"] for call in class_nodes}

    assert len(class_qns) >= 1, "Should have at least one class from expressions"


def test_js_class_expression_methods(
    js_class_expression_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(
        js_class_expression_project, mock_ingestor, skip_if_missing="javascript"
    )

    method_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Method"
    ]

    method_names = {call[0][1].get("name", "") for call in method_nodes}

    assert (
        "speak" in method_names or "fetch" in method_names or len(method_names) > 0
    ), "Should have methods from class expressions"


@pytest.fixture
def cpp_template_class_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "cpp_template_test"
    project_path.mkdir()

    template_file = project_path / "templates.cpp"
    template_file.write_text(
        """
#include <iostream>
#include <vector>

template<typename T>
class Container {
private:
    std::vector<T> items;

public:
    void add(const T& item) {
        items.push_back(item);
    }

    T get(size_t index) const {
        return items[index];
    }

    size_t size() const {
        return items.size();
    }
};

template<typename T>
class SortedContainer : public Container<T> {
public:
    void addSorted(const T& item) {
        this->add(item);
    }
};

class StringContainer : public Container<std::string> {
public:
    void addString(const std::string& s) {
        add(s);
    }

    std::string getFirst() const {
        return get(0);
    }
};

void demonstrateTemplates() {
    Container<int> intContainer;
    intContainer.add(1);
    intContainer.add(2);
    std::cout << "Size: " << intContainer.size() << std::endl;

    SortedContainer<double> sortedContainer;
    sortedContainer.addSorted(3.14);

    StringContainer strContainer;
    strContainer.addString("Hello");
    std::cout << "First: " << strContainer.getFirst() << std::endl;
}
"""
    )

    return project_path


def test_cpp_template_class_methods(
    cpp_template_class_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(cpp_template_class_project, mock_ingestor, skip_if_missing="cpp")

    method_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Method"
    ]

    method_qns = {call[0][1]["qualified_name"] for call in method_nodes}

    template_methods = [qn for qn in method_qns if "Container" in qn]

    assert len(template_methods) >= 1, "Should have methods from template classes"


def test_cpp_template_inheritance(
    cpp_template_class_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(cpp_template_class_project, mock_ingestor, skip_if_missing="cpp")

    inherits_rels = get_relationships(mock_ingestor, "INHERITS")

    template_inherits = [
        call
        for call in inherits_rels
        if "Container" in call.args[0][2] or "Container" in call.args[2][2]
    ]

    assert len(template_inherits) >= 1, "Should have template class inheritance"
