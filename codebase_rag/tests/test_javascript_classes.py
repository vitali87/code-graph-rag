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
def javascript_classes_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with all class patterns."""
    project_path = temp_repo / "javascript_classes_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "utils").mkdir()
    (project_path / "models").mkdir()

    (project_path / "src" / "base.js").write_text(
        encoding="utf-8", data="export class BaseClass {}"
    )
    (project_path / "utils" / "helpers.js").write_text(
        encoding="utf-8", data="export function validateId(id) { return id > 0; }"
    )

    return project_path


def test_basic_class_declarations(
    javascript_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic ES6 class declaration parsing."""
    test_file = javascript_classes_project / "basic_classes.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic class declaration
class Person {
    constructor(name, age) {
        this.name = name;
        this.age = age;
    }

    greet() {
        return `Hello, I'm ${this.name}`;
    }

    getAge() {
        return this.age;
    }

    setAge(newAge) {
        this.age = newAge;
    }
}

// Class with static methods
class MathUtils {
    static add(a, b) {
        return a + b;
    }

    static multiply(a, b) {
        return a * b;
    }

    static PI = 3.14159;

    static getCircleArea(radius) {
        return this.PI * radius * radius;
    }
}

// Class with getters and setters
class Rectangle {
    constructor(width, height) {
        this._width = width;
        this._height = height;
    }

    get width() {
        return this._width;
    }

    set width(value) {
        if (value > 0) {
            this._width = value;
        }
    }

    get height() {
        return this._height;
    }

    set height(value) {
        if (value > 0) {
            this._height = value;
        }
    }

    get area() {
        return this.width * this.height;
    }

    get perimeter() {
        return 2 * (this.width + this.height);
    }
}

// Class with complex methods
class DataProcessor {
    constructor() {
        this.cache = new Map();
    }

    async processData(data) {
        const processed = await this.transform(data);
        return this.validate(processed);
    }

    transform(data) {
        return data.map(item => ({
            ...item,
            processed: true,
            timestamp: Date.now()
        }));
    }

    validate(data) {
        return data.filter(item => item.id && item.name);
    }

    clearCache() {
        this.cache.clear();
    }
}

// Using classes
const person = new Person('Alice', 30);
const greeting = person.greet();
const age = person.getAge();
person.setAge(31);

const sum = MathUtils.add(5, 3);
const area = MathUtils.getCircleArea(10);

const rect = new Rectangle(10, 20);
const rectArea = rect.area;
rect.width = 15;

const processor = new DataProcessor();
const result = processor.processData([
    { id: 1, name: 'Item 1' },
    { id: 2, name: 'Item 2' }
]);
""",
    )

    run_updater(javascript_classes_project, mock_ingestor)

    project_name = javascript_classes_project.name

    expected_classes = [
        f"{project_name}.basic_classes.Person",
        f"{project_name}.basic_classes.MathUtils",
        f"{project_name}.basic_classes.Rectangle",
        f"{project_name}.basic_classes.DataProcessor",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    for expected_qn in expected_classes:
        assert expected_qn in created_classes, f"Missing class: {expected_qn}"

    expected_methods = [
        f"{project_name}.basic_classes.Person.constructor",
        f"{project_name}.basic_classes.Person.greet",
        f"{project_name}.basic_classes.Person.getAge",
        f"{project_name}.basic_classes.Person.setAge",
        f"{project_name}.basic_classes.MathUtils.add",
        f"{project_name}.basic_classes.MathUtils.multiply",
        f"{project_name}.basic_classes.MathUtils.getCircleArea",
        f"{project_name}.basic_classes.Rectangle.constructor",
        f"{project_name}.basic_classes.DataProcessor.processData",
        f"{project_name}.basic_classes.DataProcessor.transform",
        f"{project_name}.basic_classes.DataProcessor.validate",
    ]

    created_methods = get_node_names(mock_ingestor, "Method")

    found_methods = [method for method in expected_methods if method in created_methods]
    assert len(found_methods) >= 8, (
        f"Expected at least 8 methods, found {len(found_methods)}: {found_methods}"
    )


def test_class_inheritance(
    javascript_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test class inheritance patterns with extends and super()."""
    test_file = javascript_classes_project / "class_inheritance.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Base class
class Animal {
    constructor(name, species) {
        this.name = name;
        this.species = species;
    }

    speak() {
        return `${this.name} makes a sound`;
    }

    move() {
        return `${this.name} moves`;
    }

    getInfo() {
        return `${this.name} is a ${this.species}`;
    }
}

// Single inheritance
class Dog extends Animal {
    constructor(name, breed) {
        super(name, 'dog');
        this.breed = breed;
    }

    speak() {
        return `${this.name} barks`;
    }

    fetch() {
        return `${this.name} fetches the ball`;
    }

    wagTail() {
        return `${this.name} wags tail`;
    }
}

// Another inheritance level
class Poodle extends Dog {
    constructor(name, size) {
        super(name, 'poodle');
        this.size = size;
    }

    speak() {
        return `${this.name} yips elegantly`;
    }

    getGroomed() {
        return `${this.name} gets a fancy haircut`;
    }
}

// Different inheritance branch
class Cat extends Animal {
    constructor(name, breed) {
        super(name, 'cat');
        this.breed = breed;
    }

    speak() {
        return `${this.name} meows`;
    }

    climb() {
        return `${this.name} climbs a tree`;
    }

    purr() {
        return `${this.name} purrs contentedly`;
    }
}

// Class with method overrides and super calls
class Bird extends Animal {
    constructor(name, canFly = true) {
        super(name, 'bird');
        this.canFly = canFly;
    }

    speak() {
        return `${this.name} chirps`;
    }

    move() {
        if (this.canFly) {
            return super.move() + ' by flying';
        }
        return super.move() + ' by walking';
    }

    fly() {
        if (this.canFly) {
            return `${this.name} soars through the sky`;
        }
        return `${this.name} cannot fly`;
    }
}

// Class inheriting from Bird
class Eagle extends Bird {
    constructor(name) {
        super(name, true);
        this.isRaptor = true;
    }

    hunt() {
        return `${this.name} hunts for prey`;
    }

    fly() {
        return super.fly() + ' with powerful wings';
    }
}

// Using inheritance
const animal = new Animal('Generic', 'unknown');
const dog = new Dog('Buddy', 'golden retriever');
const poodle = new Poodle('Fifi', 'standard');
const cat = new Cat('Whiskers', 'tabby');
const bird = new Bird('Tweety', true);
const eagle = new Eagle('Majestic');

// Calling methods that demonstrate inheritance
const animalSound = animal.speak();
const dogSound = dog.speak(); // overridden
const poodleSound = poodle.speak(); // double override
const catSound = cat.speak(); // overridden
const birdMovement = bird.move(); // uses super
const eagleFlight = eagle.fly(); // uses super

const dogInfo = dog.getInfo(); // inherited from Animal
const poodleFetch = poodle.fetch(); // inherited from Dog
const eagleHunt = eagle.hunt(); // Eagle specific
""",
    )

    run_updater(javascript_classes_project, mock_ingestor)

    project_name = javascript_classes_project.name

    expected_inherits = [
        (
            ("Class", "qualified_name", f"{project_name}.class_inheritance.Dog"),
            ("Class", "qualified_name", f"{project_name}.class_inheritance.Animal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.class_inheritance.Poodle"),
            ("Class", "qualified_name", f"{project_name}.class_inheritance.Dog"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.class_inheritance.Cat"),
            ("Class", "qualified_name", f"{project_name}.class_inheritance.Animal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.class_inheritance.Bird"),
            ("Class", "qualified_name", f"{project_name}.class_inheritance.Animal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.class_inheritance.Eagle"),
            ("Class", "qualified_name", f"{project_name}.class_inheritance.Bird"),
        ),
    ]

    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    for expected_child, expected_parent in expected_inherits:
        found = any(
            call[0][0] == expected_child and call[0][2] == expected_parent
            for call in relationship_calls
        )
        assert found, (
            f"Missing INHERITS relationship: "
            f"{expected_child[2]} INHERITS {expected_parent[2]}"
        )

    call_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "CALLS"
    ]

    super_calls = [
        call
        for call in call_relationships
        if "constructor" in call.args[0][2]
        or "move" in call.args[0][2]
        or "fly" in call.args[0][2]
    ]

    assert len(super_calls) >= 3, (
        f"Expected at least 3 super() calls, found {len(super_calls)}"
    )


def test_static_methods_and_properties(
    javascript_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test static methods and static properties in classes."""
    test_file = javascript_classes_project / "static_features.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Class with static methods and properties
class MathHelper {
    static PI = 3.14159;
    static E = 2.71828;

    static add(a, b) {
        return a + b;
    }

    static subtract(a, b) {
        return a - b;
    }

    static multiply(a, b) {
        return a * b;
    }

    static divide(a, b) {
        if (b === 0) {
            throw new Error('Division by zero');
        }
        return a / b;
    }

    static circleArea(radius) {
        return this.PI * radius * radius;
    }

    static circleCircumference(radius) {
        return 2 * this.PI * radius;
    }

    // Static method calling other static methods
    static calculateCircleStats(radius) {
        return {
            area: this.circleArea(radius),
            circumference: this.circleCircumference(radius),
            diameter: this.multiply(radius, 2)
        };
    }
}

// Class with static factory methods
class User {
    constructor(name, email, role) {
        this.name = name;
        this.email = email;
        this.role = role;
        this.id = User.generateId();
    }

    static currentId = 0;

    static generateId() {
        return ++this.currentId;
    }

    static createAdmin(name, email) {
        return new User(name, email, 'admin');
    }

    static createUser(name, email) {
        return new User(name, email, 'user');
    }

    static createGuest() {
        return new User('Guest', 'guest@example.com', 'guest');
    }

    static fromJSON(json) {
        const data = JSON.parse(json);
        return new User(data.name, data.email, data.role);
    }

    // Instance method
    toJSON() {
        return JSON.stringify({
            id: this.id,
            name: this.name,
            email: this.email,
            role: this.role
        });
    }

    // Static validation method
    static isValidEmail(email) {
        return email.includes('@') && email.includes('.');
    }
}

// Class inheriting static methods
class PowerUser extends User {
    constructor(name, email, permissions) {
        super(name, email, 'power-user');
        this.permissions = permissions;
    }

    static createWithPermissions(name, email, permissions) {
        return new PowerUser(name, email, permissions);
    }

    // Override static method
    static createAdmin(name, email) {
        return new PowerUser(name, email, ['admin', 'power-user']);
    }
}

// Using static methods and properties
const sum = MathHelper.add(10, 5);
const product = MathHelper.multiply(4, 7);
const piValue = MathHelper.PI;
const circleStats = MathHelper.calculateCircleStats(5);

const admin = User.createAdmin('Alice', 'alice@example.com');
const user = User.createUser('Bob', 'bob@example.com');
const guest = User.createGuest();
const isValid = User.isValidEmail('test@example.com');

const powerUser = PowerUser.createAdmin('Charlie', 'charlie@example.com');
const customPowerUser = PowerUser.createWithPermissions('Dave', 'dave@example.com', ['read', 'write']);
""",
    )

    run_updater(javascript_classes_project, mock_ingestor)

    project_name = javascript_classes_project.name

    expected_static_methods = [
        f"{project_name}.static_features.MathHelper.add",
        f"{project_name}.static_features.MathHelper.subtract",
        f"{project_name}.static_features.MathHelper.multiply",
        f"{project_name}.static_features.MathHelper.divide",
        f"{project_name}.static_features.MathHelper.circleArea",
        f"{project_name}.static_features.MathHelper.calculateCircleStats",
        f"{project_name}.static_features.User.generateId",
        f"{project_name}.static_features.User.createAdmin",
        f"{project_name}.static_features.User.createUser",
        f"{project_name}.static_features.User.isValidEmail",
        f"{project_name}.static_features.PowerUser.createWithPermissions",
    ]

    created_methods = get_node_names(mock_ingestor, "Method")

    found_static_methods = [
        method for method in expected_static_methods if method in created_methods
    ]
    assert len(found_static_methods) >= 6, (
        f"Expected at least 6 static methods, found {len(found_static_methods)}: {found_static_methods}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    static_method_calls = [
        call
        for call in call_relationships
        if "static_features" in call.args[0][2]
        and any(
            static_method in call.args[2][2]
            for static_method in expected_static_methods
        )
    ]

    assert len(static_method_calls) >= 3, (
        f"Expected at least 3 static method calls, found {len(static_method_calls)}"
    )


def test_private_fields_and_methods(
    javascript_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test private fields and methods (# syntax)."""
    test_file = javascript_classes_project / "private_features.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Class with private fields and methods
class BankAccount {
    #balance = 0;
    #accountNumber;
    #pin;

    constructor(accountNumber, initialBalance = 0, pin) {
        this.#accountNumber = accountNumber;
        this.#balance = initialBalance;
        this.#pin = pin;
    }

    // Private method
    #validatePin(inputPin) {
        return this.#pin === inputPin;
    }

    // Private method for logging
    #logTransaction(type, amount) {
        console.log(`${type}: $${amount}, New balance: $${this.#balance}`);
    }

    // Public methods using private fields/methods
    deposit(amount, pin) {
        if (!this.#validatePin(pin)) {
            throw new Error('Invalid PIN');
        }

        if (amount <= 0) {
            throw new Error('Amount must be positive');
        }

        this.#balance += amount;
        this.#logTransaction('Deposit', amount);
        return this.#balance;
    }

    withdraw(amount, pin) {
        if (!this.#validatePin(pin)) {
            throw new Error('Invalid PIN');
        }

        if (amount <= 0) {
            throw new Error('Amount must be positive');
        }

        if (amount > this.#balance) {
            throw new Error('Insufficient funds');
        }

        this.#balance -= amount;
        this.#logTransaction('Withdrawal', amount);
        return this.#balance;
    }

    getBalance(pin) {
        if (!this.#validatePin(pin)) {
            throw new Error('Invalid PIN');
        }
        return this.#balance;
    }

    getAccountNumber() {
        // Only show last 4 digits
        return `****${this.#accountNumber.slice(-4)}`;
    }
}

// Class with private static fields and methods
class Counter {
    static #instanceCount = 0;
    #value = 0;

    constructor(initialValue = 0) {
        this.#value = initialValue;
        Counter.#incrementInstanceCount();
    }

    static #incrementInstanceCount() {
        this.#instanceCount++;
    }

    static getInstanceCount() {
        return this.#instanceCount;
    }

    increment() {
        this.#value++;
        return this.#value;
    }

    decrement() {
        this.#value--;
        return this.#value;
    }

    getValue() {
        return this.#value;
    }

    reset() {
        this.#value = 0;
    }
}

// Class inheriting from class with private fields
class SavingsAccount extends BankAccount {
    #interestRate;

    constructor(accountNumber, initialBalance, pin, interestRate) {
        super(accountNumber, initialBalance, pin);
        this.#interestRate = interestRate;
    }

    #calculateInterest() {
        // Cannot access parent's private #balance directly
        const currentBalance = this.getBalance(this.pin); // Would need pin access
        return currentBalance * this.#interestRate;
    }

    addInterest(pin) {
        const interest = this.#calculateInterest();
        return this.deposit(interest, pin);
    }

    getInterestRate() {
        return this.#interestRate;
    }
}

// Using classes with private features
const account = new BankAccount('123456789', 1000, '1234');
const balance1 = account.deposit(500, '1234');
const balance2 = account.withdraw(200, '1234');
const currentBalance = account.getBalance('1234');
const accountNum = account.getAccountNumber();

const counter1 = new Counter(10);
const counter2 = new Counter(20);
const count1 = counter1.increment();
const count2 = counter2.increment();
const instanceCount = Counter.getInstanceCount();

// Note: Private field access from outside would cause errors
// console.log(account.#balance); // SyntaxError
// account.#validatePin('1234'); // SyntaxError
""",
    )

    run_updater(javascript_classes_project, mock_ingestor)

    project_name = javascript_classes_project.name

    expected_classes = [
        f"{project_name}.private_features.BankAccount",
        f"{project_name}.private_features.Counter",
        f"{project_name}.private_features.SavingsAccount",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    for expected_qn in expected_classes:
        assert expected_qn in created_classes, (
            f"Missing class with private features: {expected_qn}"
        )

    expected_methods = [
        f"{project_name}.private_features.BankAccount.deposit",
        f"{project_name}.private_features.BankAccount.withdraw",
        f"{project_name}.private_features.BankAccount.getBalance",
        f"{project_name}.private_features.Counter.increment",
        f"{project_name}.private_features.Counter.getInstanceCount",
        f"{project_name}.private_features.SavingsAccount.addInterest",
    ]

    created_methods = get_node_names(mock_ingestor, "Method")

    found_methods = [method for method in expected_methods if method in created_methods]
    assert len(found_methods) >= 4, (
        f"Expected at least 4 methods in classes with private features, found {len(found_methods)}"
    )


def test_class_expressions_and_mixins(
    javascript_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test class expressions and mixin patterns."""
    test_file = javascript_classes_project / "class_expressions.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Anonymous class expression
const Animal = class {
    constructor(name) {
        this.name = name;
    }

    speak() {
        return `${this.name} makes a sound`;
    }
};

// Named class expression
const Vehicle = class Vehicle {
    constructor(make, model) {
        this.make = make;
        this.model = model;
    }

    getInfo() {
        return `${this.make} ${this.model}`;
    }

    start() {
        return `${this.getInfo()} starts`;
    }
};

// Class expression assigned to variable
const Shape = class Rectangle {
    constructor(width, height) {
        this.width = width;
        this.height = height;
    }

    area() {
        return this.width * this.height;
    }
};

// Mixin function
function Flyable(Base) {
    return class extends Base {
        fly() {
            return `${this.name} flies`;
        }

        land() {
            return `${this.name} lands`;
        }
    };
}

// Another mixin
function Swimmable(Base) {
    return class extends Base {
        swim() {
            return `${this.name} swims`;
        }

        dive() {
            return `${this.name} dives underwater`;
        }
    };
}

// Using mixins
class Fish extends Swimmable(Animal) {
    constructor(name, species) {
        super(name);
        this.species = species;
    }

    speak() {
        return `${this.name} makes bubbles`;
    }
}

class Bird extends Flyable(Animal) {
    constructor(name, wingspan) {
        super(name);
        this.wingspan = wingspan;
    }

    speak() {
        return `${this.name} chirps`;
    }
}

// Multiple mixins
class Duck extends Swimmable(Flyable(Animal)) {
    constructor(name) {
        super(name);
    }

    speak() {
        return `${this.name} quacks`;
    }
}

// Factory function returning class
function createModel(type) {
    return class {
        constructor(data) {
            this.type = type;
            this.data = data;
        }

        getType() {
            return this.type;
        }

        getData() {
            return this.data;
        }

        toString() {
            return `${this.type}: ${JSON.stringify(this.data)}`;
        }
    };
}

// Using class expressions and mixins
const animal = new Animal('Generic');
const car = new Vehicle('Toyota', 'Camry');
const rectangle = new Shape(10, 20);

const fish = new Fish('Nemo', 'clownfish');
const bird = new Bird('Eagle', 6);
const duck = new Duck('Donald');

const fishSound = fish.speak();
const fishSwim = fish.swim();

const birdSound = bird.speak();
const birdFly = bird.fly();

const duckSound = duck.speak();
const duckSwim = duck.swim();
const duckFly = duck.fly();

const UserModel = createModel('User');
const user = new UserModel({ name: 'John', age: 30 });
const userInfo = user.toString();
""",
    )

    run_updater(javascript_classes_project, mock_ingestor)

    class_calls = get_nodes(mock_ingestor, "Class")

    class_expression_classes = [
        call
        for call in class_calls
        if "class_expressions" in call[0][1]["qualified_name"]
    ]

    assert len(class_expression_classes) >= 3, (
        f"Expected at least 3 class expressions, found {len(class_expression_classes)}"
    )

    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    inheritance_relationships = [
        call for call in relationship_calls if "class_expressions" in call[0][0][2]
    ]

    assert len(inheritance_relationships) >= 2, (
        f"Expected at least 2 inheritance relationships from mixins, found {len(inheritance_relationships)}"
    )


def test_class_comprehensive(
    javascript_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all class types create proper relationships."""
    test_file = javascript_classes_project / "comprehensive_classes.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every JavaScript class pattern in one file

// Basic class
class Animal {
    constructor(name) {
        this.name = name;
    }

    speak() {
        return `${this.name} makes a sound`;
    }
}

// Inheritance
class Dog extends Animal {
    constructor(name, breed) {
        super(name);
        this.breed = breed;
    }

    speak() {
        return `${this.name} barks`;
    }

    fetch() {
        return `${this.name} fetches`;
    }
}

// Static methods
class MathUtils {
    static add(a, b) {
        return a + b;
    }

    static PI = 3.14159;
}

// Private fields
class Counter {
    #count = 0;

    increment() {
        this.#count++;
        return this.#count;
    }

    getCount() {
        return this.#count;
    }
}

// Class expression
const Rectangle = class {
    constructor(width, height) {
        this.width = width;
        this.height = height;
    }

    area() {
        return this.width * this.height;
    }
};

// Using all class types
const animal = new Animal('Generic');
const dog = new Dog('Buddy', 'Golden');
const sum = MathUtils.add(5, 3);
const counter = new Counter();
const rect = new Rectangle(10, 20);

const animalSound = animal.speak();
const dogSound = dog.speak(); // override
const dogFetch = dog.fetch();
const count = counter.increment();
const area = rect.area();

// Method calls demonstrating relationships
function testClasses() {
    const testDog = new Dog('Test', 'Test');
    const sound = testDog.speak();
    const fetch = testDog.fetch();
    return { sound, fetch };
}

const testResult = testClasses();
""",
    )

    run_updater(javascript_classes_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_classes" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 5, (
        f"Expected at least 5 comprehensive class calls, found {len(comprehensive_calls)}"
    )

    class_inheritance = [
        call
        for call in inherits_relationships
        if "comprehensive_classes" in call.args[0][2]
    ]

    assert len(class_inheritance) >= 1, (
        f"Expected at least 1 inheritance relationship, found {len(class_inheritance)}"
    )

    for relationship in comprehensive_calls:
        assert len(relationship.args) == 3, "Call relationship should have 3 args"
        assert relationship.args[1] == "CALLS", "Second arg should be 'CALLS'"

        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        assert "comprehensive_classes" in source_module, (
            f"Source module should contain test file name: {source_module}"
        )

        assert isinstance(target_module, str) and target_module, (
            f"Target should be non-empty string: {target_module}"
        )

    assert defines_relationships, "Should still have DEFINES relationships"
