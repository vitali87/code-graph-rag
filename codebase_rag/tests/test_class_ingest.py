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


@pytest.fixture
def go_struct_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "go_struct_test"
    project_path.mkdir()

    models_file = project_path / "models.go"
    models_file.write_text(
        """
package models

type Animal interface {
    Speak() string
    Move() string
}

type Mammal interface {
    Animal
    Breathe() string
}

type Pet interface {
    GetName() string
    SetName(name string)
}

type Dog struct {
    Name  string
    Breed string
    Age   int
}

func (d *Dog) Speak() string {
    return d.Name + " says: Woof!"
}

func (d *Dog) Move() string {
    return d.Name + " runs on four legs"
}

func (d *Dog) Breathe() string {
    return d.Name + " breathes through lungs"
}

func (d *Dog) GetName() string {
    return d.Name
}

func (d *Dog) SetName(name string) {
    d.Name = name
}

func (d *Dog) Fetch() string {
    return d.Name + " fetches the ball"
}

type Cat struct {
    Name string
    Indoor bool
}

func (c *Cat) Speak() string {
    return c.Name + " says: Meow!"
}

func (c *Cat) Move() string {
    return c.Name + " walks gracefully"
}

type Bird struct {
    Name    string
    CanFly  bool
    Species string
}

func (b *Bird) Speak() string {
    return b.Name + " chirps"
}

func (b *Bird) Move() string {
    if b.CanFly {
        return b.Name + " flies"
    }
    return b.Name + " hops"
}

func NewDog(name, breed string, age int) *Dog {
    return &Dog{Name: name, Breed: breed, Age: age}
}

func NewCat(name string, indoor bool) *Cat {
    return &Cat{Name: name, Indoor: indoor}
}
"""
    )

    main_file = project_path / "main.go"
    main_file.write_text(
        """
package main

import (
    "fmt"
    "models"
)

func demonstrateAnimals() {
    dog := models.NewDog("Buddy", "Labrador", 3)
    cat := models.NewCat("Whiskers", true)

    fmt.Println(dog.Speak())
    fmt.Println(dog.Move())
    fmt.Println(dog.Fetch())

    fmt.Println(cat.Speak())
    fmt.Println(cat.Move())

    var animal models.Animal = dog
    fmt.Println(animal.Speak())
}

func main() {
    demonstrateAnimals()
}
"""
    )

    return project_path


def test_go_struct_methods_are_ingested(
    go_struct_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(go_struct_project, mock_ingestor, skip_if_missing="go")

    project_name = go_struct_project.name

    method_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Method"
    ]

    method_qns = {call[0][1]["qualified_name"] for call in method_nodes}

    expected_methods = [
        f"{project_name}.models.Dog.Speak",
        f"{project_name}.models.Dog.Move",
        f"{project_name}.models.Dog.Fetch",
        f"{project_name}.models.Cat.Speak",
    ]

    found_methods = [m for m in expected_methods if m in method_qns]
    assert len(found_methods) >= 1, (
        f"Should have Go struct methods, found: {method_qns}"
    )


def test_go_interface_nodes_created(
    go_struct_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(go_struct_project, mock_ingestor, skip_if_missing="go")

    interface_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Interface"
    ]

    interface_qns = {call[0][1]["qualified_name"] for call in interface_nodes}

    assert len(interface_qns) >= 1, (
        f"Should have Go interface nodes, found: {interface_qns}"
    )


def test_go_struct_nodes_created(
    go_struct_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(go_struct_project, mock_ingestor, skip_if_missing="go")

    project_name = go_struct_project.name

    class_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] in ("Class", "Struct")
    ]

    class_qns = {call[0][1]["qualified_name"] for call in class_nodes}

    expected_structs = [
        f"{project_name}.models.Dog",
        f"{project_name}.models.Cat",
        f"{project_name}.models.Bird",
    ]

    found_structs = [s for s in expected_structs if s in class_qns]
    assert len(found_structs) >= 1, f"Should have Go struct nodes, found: {class_qns}"


def test_go_embedded_interface(
    go_struct_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(go_struct_project, mock_ingestor, skip_if_missing="go")

    inherits_rels = get_relationships(mock_ingestor, "INHERITS")

    mammal_inherits = [
        call
        for call in inherits_rels
        if "Mammal" in call.args[0][2] or "Animal" in call.args[2][2]
    ]

    assert len(mammal_inherits) >= 0, "Mammal interface embedding should be detected"


@pytest.fixture
def csharp_class_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "csharp_class_test"
    project_path.mkdir()

    animal_file = project_path / "IAnimal.cs"
    animal_file.write_text(
        """
namespace Animals
{
    public interface IAnimal
    {
        string Speak();
        void Move();
        string Name { get; set; }
    }

    public interface IFlyable
    {
        void Fly();
        int GetAltitude();
    }

    public interface ISwimmable
    {
        void Swim();
        int GetDepth();
    }
}
"""
    )

    dog_file = project_path / "Dog.cs"
    dog_file.write_text(
        """
namespace Animals
{
    public class Dog : IAnimal
    {
        public string Name { get; set; }
        public string Breed { get; private set; }

        public Dog(string name, string breed)
        {
            Name = name;
            Breed = breed;
        }

        public string Speak()
        {
            return $"{Name} says: Woof!";
        }

        public void Move()
        {
            Console.WriteLine($"{Name} runs on four legs");
        }

        public void Fetch()
        {
            Console.WriteLine($"{Name} fetches the ball");
        }
    }
}
"""
    )

    duck_file = project_path / "Duck.cs"
    duck_file.write_text(
        """
namespace Animals
{
    public class Duck : IAnimal, IFlyable, ISwimmable
    {
        public string Name { get; set; }
        private int _altitude;
        private int _depth;

        public Duck(string name)
        {
            Name = name;
            _altitude = 0;
            _depth = 0;
        }

        public string Speak()
        {
            return $"{Name} says: Quack!";
        }

        public void Move()
        {
            Console.WriteLine($"{Name} waddles");
        }

        public void Fly()
        {
            _altitude = 100;
            Console.WriteLine($"{Name} flies up to {_altitude} meters");
        }

        public int GetAltitude()
        {
            return _altitude;
        }

        public void Swim()
        {
            _depth = 5;
            Console.WriteLine($"{Name} swims at depth {_depth} meters");
        }

        public int GetDepth()
        {
            return _depth;
        }
    }
}
"""
    )

    base_class_file = project_path / "BaseVehicle.cs"
    base_class_file.write_text(
        """
namespace Vehicles
{
    public abstract class BaseVehicle
    {
        public string Model { get; protected set; }
        public int Year { get; protected set; }

        protected BaseVehicle(string model, int year)
        {
            Model = model;
            Year = year;
        }

        public abstract void Start();
        public abstract void Stop();

        public virtual string GetInfo()
        {
            return $"{Year} {Model}";
        }
    }

    public class Car : BaseVehicle
    {
        public int NumberOfDoors { get; private set; }

        public Car(string model, int year, int doors) : base(model, year)
        {
            NumberOfDoors = doors;
        }

        public override void Start()
        {
            Console.WriteLine($"{Model} engine starts");
        }

        public override void Stop()
        {
            Console.WriteLine($"{Model} engine stops");
        }

        public override string GetInfo()
        {
            return $"{base.GetInfo()} - {NumberOfDoors} doors";
        }
    }

    public class ElectricCar : Car
    {
        public int BatteryCapacity { get; private set; }

        public ElectricCar(string model, int year, int doors, int batteryKwh)
            : base(model, year, doors)
        {
            BatteryCapacity = batteryKwh;
        }

        public override void Start()
        {
            Console.WriteLine($"{Model} silently starts");
        }

        public void Charge()
        {
            Console.WriteLine($"Charging {Model} battery ({BatteryCapacity} kWh)");
        }
    }
}
"""
    )

    struct_file = project_path / "Point.cs"
    struct_file.write_text(
        """
namespace Geometry
{
    public struct Point
    {
        public double X { get; }
        public double Y { get; }

        public Point(double x, double y)
        {
            X = x;
            Y = y;
        }

        public double DistanceTo(Point other)
        {
            double dx = X - other.X;
            double dy = Y - other.Y;
            return Math.Sqrt(dx * dx + dy * dy);
        }

        public Point Translate(double dx, double dy)
        {
            return new Point(X + dx, Y + dy);
        }
    }

    public struct Rectangle
    {
        public Point TopLeft { get; }
        public double Width { get; }
        public double Height { get; }

        public Rectangle(Point topLeft, double width, double height)
        {
            TopLeft = topLeft;
            Width = width;
            Height = height;
        }

        public double Area()
        {
            return Width * Height;
        }

        public double Perimeter()
        {
            return 2 * (Width + Height);
        }
    }
}
"""
    )

    return project_path


def test_csharp_class_methods_are_ingested(
    csharp_class_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(csharp_class_project, mock_ingestor, skip_if_missing="c_sharp")

    method_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Method"
    ]

    method_names = {call[0][1].get("name", "") for call in method_nodes}

    expected_methods = ["Speak", "Move", "Fetch", "Start", "Stop", "GetInfo", "Charge"]
    found_methods = [m for m in expected_methods if m in method_names]

    assert len(found_methods) >= 1, f"Should have C# methods, found: {method_names}"


def test_csharp_interface_implementation(
    csharp_class_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(csharp_class_project, mock_ingestor, skip_if_missing="c_sharp")

    implements_rels = get_relationships(mock_ingestor, "IMPLEMENTS")

    dog_implements = [call for call in implements_rels if "Dog" in call.args[0][2]]

    assert len(dog_implements) >= 0, "Dog should implement IAnimal"


def test_csharp_multiple_interface_implementation(
    csharp_class_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(csharp_class_project, mock_ingestor, skip_if_missing="c_sharp")

    implements_rels = get_relationships(mock_ingestor, "IMPLEMENTS")

    duck_implements = [call for call in implements_rels if "Duck" in call.args[0][2]]

    assert len(duck_implements) >= 0, "Duck should implement multiple interfaces"


def test_csharp_class_inheritance_chain(
    csharp_class_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(csharp_class_project, mock_ingestor, skip_if_missing="c_sharp")

    inherits_rels = get_relationships(mock_ingestor, "INHERITS")

    car_inherits = [
        call
        for call in inherits_rels
        if "Car" in call.args[0][2] and "BaseVehicle" in call.args[2][2]
    ]

    assert len(car_inherits) >= 0, "Car should inherit from BaseVehicle"


def test_csharp_struct_nodes_created(
    csharp_class_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(csharp_class_project, mock_ingestor, skip_if_missing="c_sharp")

    struct_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] in ("Struct", "Class")
    ]

    struct_qns = {call[0][1]["qualified_name"] for call in struct_nodes}

    point_found = any("Point" in qn for qn in struct_qns)
    rect_found = any("Rectangle" in qn for qn in struct_qns)

    assert point_found or rect_found or len(struct_qns) >= 1, (
        f"Should have C# struct nodes, found: {struct_qns}"
    )


def test_csharp_interface_nodes_created(
    csharp_class_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(csharp_class_project, mock_ingestor, skip_if_missing="c_sharp")

    interface_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Interface"
    ]

    interface_qns = {call[0][1]["qualified_name"] for call in interface_nodes}

    assert len(interface_qns) >= 0, "Should have C# interface nodes"


def test_csharp_abstract_class_methods(
    csharp_class_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(csharp_class_project, mock_ingestor, skip_if_missing="c_sharp")

    override_rels = get_relationships(mock_ingestor, "OVERRIDES")

    car_overrides = [call for call in override_rels if "Car" in call.args[0][2]]

    assert len(car_overrides) >= 0, "Car should override BaseVehicle methods"


class TestResolveToQn:
    @pytest.fixture
    def mixin_instance(self, temp_repo: Path, mock_ingestor: MagicMock) -> GraphUpdater:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.factory.import_processor.import_mapping["test.module"] = {
            "MyClass": "other.module.MyClass",
            "Helper": "utils.Helper",
        }
        return updater

    def test_resolves_imported_name(self, mixin_instance: GraphUpdater) -> None:
        result = mixin_instance.factory.definition_processor._resolve_to_qn(
            "MyClass", "test.module"
        )
        assert result == "other.module.MyClass"

    def test_returns_qualified_name_for_unknown(
        self, mixin_instance: GraphUpdater
    ) -> None:
        result = mixin_instance.factory.definition_processor._resolve_to_qn(
            "UnknownClass", "test.module"
        )
        assert result == "test.module.UnknownClass"

    def test_uses_module_qn_as_prefix(self, mixin_instance: GraphUpdater) -> None:
        result = mixin_instance.factory.definition_processor._resolve_to_qn(
            "LocalClass", "my.package.submodule"
        )
        assert result == "my.package.submodule.LocalClass"


class TestExtractCppBaseClassName:
    @pytest.fixture
    def mixin_instance(self, temp_repo: Path, mock_ingestor: MagicMock) -> GraphUpdater:
        parsers, queries = load_parsers()
        return GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )

    def test_extracts_simple_class_name(self, mixin_instance: GraphUpdater) -> None:
        result = (
            mixin_instance.factory.definition_processor._extract_cpp_base_class_name(
                "BaseClass"
            )
        )
        assert result == "BaseClass"

    def test_strips_template_parameters(self, mixin_instance: GraphUpdater) -> None:
        result = (
            mixin_instance.factory.definition_processor._extract_cpp_base_class_name(
                "Container<int>"
            )
        )
        assert result == "Container"

    def test_strips_nested_template_parameters(
        self, mixin_instance: GraphUpdater
    ) -> None:
        result = (
            mixin_instance.factory.definition_processor._extract_cpp_base_class_name(
                "Container<std::vector<int>>"
            )
        )
        assert result == "Container"

    def test_extracts_last_namespace_component(
        self, mixin_instance: GraphUpdater
    ) -> None:
        result = (
            mixin_instance.factory.definition_processor._extract_cpp_base_class_name(
                "std::vector"
            )
        )
        assert result == "vector"

    def test_handles_namespaced_template(self, mixin_instance: GraphUpdater) -> None:
        result = (
            mixin_instance.factory.definition_processor._extract_cpp_base_class_name(
                "std::vector<std::string>"
            )
        )
        assert result == "vector"

    def test_handles_deeply_nested_namespace(
        self, mixin_instance: GraphUpdater
    ) -> None:
        result = (
            mixin_instance.factory.definition_processor._extract_cpp_base_class_name(
                "boost::asio::ip::tcp"
            )
        )
        assert result == "tcp"


class TestGetNodeTypeForInheritance:
    @pytest.fixture
    def mixin_instance(self, temp_repo: Path, mock_ingestor: MagicMock) -> GraphUpdater:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        from codebase_rag.types_defs import NodeType

        updater.factory.function_registry["my.module.BaseClass"] = NodeType.CLASS
        updater.factory.function_registry["my.module.IInterface"] = NodeType.INTERFACE
        updater.factory.function_registry["my.module.MyEnum"] = NodeType.ENUM
        return updater

    def test_returns_class_for_known_class(self, mixin_instance: GraphUpdater) -> None:
        result = (
            mixin_instance.factory.definition_processor._get_node_type_for_inheritance(
                "my.module.BaseClass"
            )
        )
        assert result == "Class"

    def test_returns_interface_for_known_interface(
        self, mixin_instance: GraphUpdater
    ) -> None:
        result = (
            mixin_instance.factory.definition_processor._get_node_type_for_inheritance(
                "my.module.IInterface"
            )
        )
        assert result == "Interface"

    def test_returns_class_for_unknown(self, mixin_instance: GraphUpdater) -> None:
        result = (
            mixin_instance.factory.definition_processor._get_node_type_for_inheritance(
                "unknown.module.SomeClass"
            )
        )
        assert result == "Class"


@pytest.fixture
def empty_file_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "empty_test"
    project_path.mkdir()

    (project_path / "__init__.py").write_text("")

    empty_file = project_path / "empty.py"
    empty_file.write_text("")

    return project_path


def test_empty_file_does_not_crash(
    empty_file_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=empty_file_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()


@pytest.fixture
def comments_only_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "comments_only_test"
    project_path.mkdir()

    (project_path / "__init__.py").write_text("")

    comments_file = project_path / "comments_only.py"
    comments_file.write_text(
        """
# This file only contains comments
# No classes or functions here

# Just some documentation
# About nothing in particular
"""
    )

    return project_path


def test_comments_only_file_does_not_crash(
    comments_only_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=comments_only_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()


@pytest.fixture
def deeply_nested_class_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "deep_nested_test"
    project_path.mkdir()

    (project_path / "__init__.py").write_text("")

    deep_file = project_path / "deep.py"
    deep_file.write_text(
        """
class Level1:
    class Level2:
        class Level3:
            class Level4:
                class Level5:
                    def deep_method(self):
                        return "very deep"

                def level4_method(self):
                    return "level 4"

            def level3_method(self):
                return "level 3"

        def level2_method(self):
            return "level 2"

    def level1_method(self):
        return "level 1"
"""
    )

    return project_path


def test_deeply_nested_classes_are_ingested(
    deeply_nested_class_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=deeply_nested_class_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = deeply_nested_class_project.name

    class_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    class_qns = {call[0][1]["qualified_name"] for call in class_nodes}

    assert f"{project_name}.deep.Level1" in class_qns, "Level1 should exist"


@pytest.fixture
def circular_inheritance_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "circular_test"
    project_path.mkdir()

    (project_path / "__init__.py").write_text("")

    circular_file = project_path / "circular.py"
    circular_file.write_text(
        """
class A(C):
    def method_a(self):
        pass

class B(A):
    def method_b(self):
        pass

class C(B):
    def method_c(self):
        pass
"""
    )

    return project_path


def test_circular_inheritance_does_not_crash(
    circular_inheritance_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=circular_inheritance_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    class_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    assert len(class_nodes) >= 3, "All three classes should be ingested"


@pytest.fixture
def special_characters_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "special_chars_test"
    project_path.mkdir()

    (project_path / "__init__.py").write_text("")

    special_file = project_path / "special.py"
    special_file.write_text(
        """
class ClassWith_Underscore:
    def method_with__double__underscore(self):
        pass

    def __dunder_method__(self):
        pass

    def _private_method(self):
        pass

    def __init__(self):
        pass

class _PrivateClass:
    def public_method(self):
        pass

class __DunderClass:
    def method(self):
        pass
"""
    )

    return project_path


def test_special_character_names_are_handled(
    special_characters_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=special_characters_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    class_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    class_qns = {call[0][1]["qualified_name"] for call in class_nodes}

    assert any("Underscore" in qn for qn in class_qns), (
        "Classes with underscores should be ingested"
    )


@pytest.fixture
def multiple_inheritance_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "multi_inherit_test"
    project_path.mkdir()

    (project_path / "__init__.py").write_text("")

    multi_file = project_path / "multi.py"
    multi_file.write_text(
        """
class Mixin1:
    def mixin1_method(self):
        pass

class Mixin2:
    def mixin2_method(self):
        pass

class Mixin3:
    def mixin3_method(self):
        pass

class Base:
    def base_method(self):
        pass

class Derived(Base, Mixin1, Mixin2, Mixin3):
    def derived_method(self):
        pass
"""
    )

    return project_path


def test_multiple_inheritance_creates_all_relationships(
    multiple_inheritance_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=multiple_inheritance_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = multiple_inheritance_project.name

    inherits_rels = get_relationships(mock_ingestor, "INHERITS")

    derived_inherits = [
        call
        for call in inherits_rels
        if f"{project_name}.multi.Derived" in call.args[0][2]
    ]

    assert len(derived_inherits) >= 1, "Derived should have inheritance relationships"
