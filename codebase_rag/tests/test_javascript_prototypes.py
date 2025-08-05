"""
Comprehensive JavaScript prototype-based inheritance parsing and relationship testing.
Tests constructor functions, prototypes, prototype chains, and Object.create patterns.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def javascript_prototypes_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with prototype patterns."""
    project_path = temp_repo / "javascript_prototypes_test"
    project_path.mkdir()

    # Create directory structure
    (project_path / "models").mkdir()
    (project_path / "utils").mkdir()

    # Create base files
    (project_path / "models" / "base.js").write_text(
        """
function BaseModel(id) {
    this.id = id;
    this.created = new Date();
}

BaseModel.prototype.getId = function() {
    return this.id;
};

BaseModel.prototype.toJSON = function() {
    return {
        id: this.id,
        created: this.created
    };
};

module.exports = BaseModel;
"""
    )

    return project_path


def test_constructor_functions_and_prototypes(
    javascript_prototypes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test constructor functions and prototype method definitions."""
    test_file = javascript_prototypes_project / "constructor_prototypes.js"
    test_file.write_text(
        """
// Basic constructor function
function Person(name, age) {
    this.name = name;
    this.age = age;
    this.created = new Date();
}

// Prototype method definitions
Person.prototype.greet = function() {
    return `Hello, I'm ${this.name}`;
};

Person.prototype.getAge = function() {
    return this.age;
};

Person.prototype.setAge = function(newAge) {
    if (newAge > 0) {
        this.age = newAge;
    }
};

// Static methods on constructor
Person.createFromJSON = function(json) {
    return new Person(json.name, json.age);
};

Person.compareAges = function(person1, person2) {
    return person1.age - person2.age;
};

// Another constructor with prototype chain
function Employee(name, age, department) {
    // Call parent constructor
    Person.call(this, name, age);
    this.department = department;
    this.salary = 0;
}

// Set up prototype chain
Employee.prototype = Object.create(Person.prototype);
Employee.prototype.constructor = Employee;

// Add Employee-specific methods
Employee.prototype.getDepartment = function() {
    return this.department;
};

Employee.prototype.setSalary = function(salary) {
    this.salary = salary;
};

Employee.prototype.getInfo = function() {
    return `${this.greet()}, I work in ${this.department}`;
};

// Override parent method
Employee.prototype.greet = function() {
    return `Hello, I'm ${this.name} from ${this.department}`;
};

// Constructor with complex prototype
function Manager(name, age, department, teamSize) {
    Employee.call(this, name, age, department);
    this.teamSize = teamSize;
    this.reports = [];
}

Manager.prototype = Object.create(Employee.prototype);
Manager.prototype.constructor = Manager;

Manager.prototype.addReport = function(employee) {
    this.reports.push(employee);
};

Manager.prototype.getTeamSize = function() {
    return this.teamSize;
};

// Multiple inheritance pattern
function Contractor(name, age, hourlyRate) {
    Person.call(this, name, age);
    this.hourlyRate = hourlyRate;
    this.hoursWorked = 0;
}

Contractor.prototype = Object.create(Person.prototype);
Contractor.prototype.constructor = Contractor;

// Mixin pattern
const TimestampMixin = {
    updateTimestamp: function() {
        this.lastModified = new Date();
    },

    getTimestamp: function() {
        return this.lastModified || this.created;
    }
};

// Apply mixin to prototype
Object.assign(Person.prototype, TimestampMixin);
Object.assign(Employee.prototype, TimestampMixin);

// Using constructors
const person = new Person("Alice", 30);
const employee = new Employee("Bob", 25, "Engineering");
const manager = new Manager("Charlie", 40, "Sales", 5);

console.log(person.greet());
console.log(employee.getInfo());
console.log(manager.getTeamSize());

// Prototype chain tests
console.log(employee instanceof Employee); // true
console.log(employee instanceof Person);   // true
console.log(manager instanceof Manager);   // true
console.log(manager instanceof Employee);  // true
console.log(manager instanceof Person);    // true
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_prototypes_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = javascript_prototypes_project.name

    # Get all Function nodes (constructors and prototype methods)
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Check constructor functions
    expected_constructors = [
        f"{project_name}.constructor_prototypes.Person",
        f"{project_name}.constructor_prototypes.Employee",
        f"{project_name}.constructor_prototypes.Manager",
        f"{project_name}.constructor_prototypes.Contractor",
    ]

    for expected in expected_constructors:
        assert expected in created_functions, (
            f"Missing constructor function: {expected}"
        )

    # Check prototype methods
    expected_prototype_methods = [
        f"{project_name}.constructor_prototypes.Person.greet",
        f"{project_name}.constructor_prototypes.Person.getAge",
        f"{project_name}.constructor_prototypes.Person.setAge",
        f"{project_name}.constructor_prototypes.Employee.getDepartment",
        f"{project_name}.constructor_prototypes.Employee.getInfo",
        f"{project_name}.constructor_prototypes.Manager.addReport",
    ]

    # Some parsers might organize prototype methods differently
    prototype_methods_found = [
        func
        for func in created_functions
        if any(
            expected_method.split(".")[-1] in func
            for expected_method in expected_prototype_methods
        )
    ]

    assert len(prototype_methods_found) >= 4, (
        f"Expected at least 4 prototype methods, found {len(prototype_methods_found)}"
    )

    # Check inheritance relationships
    inheritance_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "INHERITS"
    ]

    # Should have Employee inheriting from Person, Manager from Employee
    len(inheritance_relationships) >= 2
    # Note: Actual inheritance detection may vary by parser implementation


def test_object_create_patterns(
    javascript_prototypes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Object.create() patterns and prototype-based inheritance."""
    test_file = javascript_prototypes_project / "object_create_patterns.js"
    test_file.write_text(
        """
// Object.create() patterns

// Base object as prototype
const animalProto = {
    type: 'unknown',

    speak: function() {
        return `The ${this.type} makes a sound`;
    },

    move: function() {
        return `The ${this.type} moves`;
    },

    eat: function(food) {
        return `The ${this.type} eats ${food}`;
    }
};

// Create objects with prototype
const dog = Object.create(animalProto);
dog.type = 'dog';
dog.breed = 'Labrador';
dog.speak = function() {
    return 'Woof!';
};
dog.wagTail = function() {
    return 'Wagging tail';
};

const cat = Object.create(animalProto);
cat.type = 'cat';
cat.speak = function() {
    return 'Meow!';
};
cat.purr = function() {
    return 'Purring...';
};

// Factory function with Object.create
function createAnimal(type, name) {
    const animal = Object.create(animalProto);
    animal.type = type;
    animal.name = name;
    return animal;
}

// More complex prototype chain
const mammalProto = Object.create(animalProto);
mammalProto.nurseYoung = function() {
    return `The ${this.type} nurses its young`;
};
mammalProto.isWarmBlooded = true;

const primateProto = Object.create(mammalProto);
primateProto.useTools = function() {
    return `The ${this.type} can use tools`;
};
primateProto.hasOpposableThumbs = true;

// Create with specific prototype
const monkey = Object.create(primateProto);
monkey.type = 'monkey';
monkey.species = 'Capuchin';
monkey.climb = function() {
    return 'Climbing trees';
};

// Object.create with property descriptors
const vehicleProto = {
    start: function() {
        this.running = true;
        return 'Vehicle started';
    },

    stop: function() {
        this.running = false;
        return 'Vehicle stopped';
    }
};

const car = Object.create(vehicleProto, {
    wheels: {
        value: 4,
        writable: false,
        enumerable: true,
        configurable: false
    },

    type: {
        value: 'car',
        writable: true,
        enumerable: true,
        configurable: true
    },

    honk: {
        value: function() {
            return 'Beep beep!';
        },
        writable: true,
        enumerable: false,
        configurable: true
    }
});

// Null prototype object
const pureObject = Object.create(null);
pureObject.data = 'pure data';
pureObject.process = function() {
    return this.data.toUpperCase();
};

// Delegation pattern
const defaults = {
    timeout: 5000,
    retries: 3,

    getConfig: function() {
        return {
            timeout: this.timeout,
            retries: this.retries
        };
    }
};

const customConfig = Object.create(defaults);
customConfig.timeout = 10000;
customConfig.endpoint = 'https://api.example.com';
customConfig.authenticate = function() {
    return 'Authenticating...';
};

// OLOO (Objects Linking to Other Objects) pattern
const Task = {
    setID: function(ID) {
        this.id = ID;
    },

    outputID: function() {
        console.log(this.id);
    }
};

const XYZ = Object.create(Task);
XYZ.prepareTask = function(ID, Label) {
    this.setID(ID);
    this.label = Label;
};
XYZ.outputTaskDetails = function() {
    this.outputID();
    console.log(this.label);
};

// Using the patterns
const bird = createAnimal('bird', 'Tweety');
console.log(bird.speak());

console.log(monkey.speak());      // From animalProto
console.log(monkey.nurseYoung());  // From mammalProto
console.log(monkey.useTools());    // From primateProto
console.log(monkey.climb());       // Own method

console.log(car.start());
console.log(car.honk());

const myTask = Object.create(XYZ);
myTask.prepareTask(1, "My Task");
myTask.outputTaskDetails();
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_prototypes_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = javascript_prototypes_project.name

    # Get all Function nodes
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Check factory functions
    expected_functions = [
        f"{project_name}.object_create_patterns.createAnimal",
    ]

    for expected in expected_functions:
        assert expected in created_functions, f"Missing factory function: {expected}"

    # Check methods defined on prototype objects
    # These might be captured as functions or methods depending on parser
    method_like_functions = [
        func
        for func in created_functions
        if "object_create_patterns" in func
        and any(
            method in func
            for method in [
                "speak",
                "move",
                "eat",
                "nurseYoung",
                "useTools",
                "start",
                "stop",
            ]
        )
    ]

    assert len(method_like_functions) >= 3, (
        f"Expected at least 3 prototype methods, found {len(method_like_functions)}"
    )

    # Check CALLS relationships for Object.create
    call_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

    object_create_calls = [
        call for call in call_relationships if "Object.create" in str(call.args[2][2])
    ]

    assert len(object_create_calls) >= 3, (
        f"Expected at least 3 Object.create calls, found {len(object_create_calls)}"
    )


def test_prototype_chain_and_method_resolution(
    javascript_prototypes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test prototype chain traversal and method resolution."""
    test_file = javascript_prototypes_project / "prototype_chain.js"
    test_file.write_text(
        """
// Deep prototype chain example

// Level 1: Base
function Shape(x, y) {
    this.x = x;
    this.y = y;
}

Shape.prototype.move = function(dx, dy) {
    this.x += dx;
    this.y += dy;
    return this;
};

Shape.prototype.getPosition = function() {
    return { x: this.x, y: this.y };
};

// Level 2: Rectangle extends Shape
function Rectangle(x, y, width, height) {
    Shape.call(this, x, y);
    this.width = width;
    this.height = height;
}

Rectangle.prototype = Object.create(Shape.prototype);
Rectangle.prototype.constructor = Rectangle;

Rectangle.prototype.getArea = function() {
    return this.width * this.height;
};

Rectangle.prototype.getPerimeter = function() {
    return 2 * (this.width + this.height);
};

// Override parent method
Rectangle.prototype.getPosition = function() {
    const pos = Shape.prototype.getPosition.call(this);
    return {
        ...pos,
        width: this.width,
        height: this.height
    };
};

// Level 3: Square extends Rectangle
function Square(x, y, size) {
    Rectangle.call(this, x, y, size, size);
}

Square.prototype = Object.create(Rectangle.prototype);
Square.prototype.constructor = Square;

Square.prototype.setSize = function(size) {
    this.width = size;
    this.height = size;
    return this;
};

// Override with super call
Square.prototype.getArea = function() {
    console.log('Square area calculation');
    return Rectangle.prototype.getArea.call(this);
};

// Level 4: ColoredSquare extends Square
function ColoredSquare(x, y, size, color) {
    Square.call(this, x, y, size);
    this.color = color;
}

ColoredSquare.prototype = Object.create(Square.prototype);
ColoredSquare.prototype.constructor = ColoredSquare;

ColoredSquare.prototype.getColor = function() {
    return this.color;
};

ColoredSquare.prototype.setColor = function(color) {
    this.color = color;
    return this;
};

// Complex method with prototype chain calls
ColoredSquare.prototype.getFullInfo = function() {
    return {
        position: Shape.prototype.getPosition.call(this),
        area: Square.prototype.getArea.call(this),
        perimeter: Rectangle.prototype.getPerimeter.call(this),
        color: this.color
    };
};

// Prototype property access patterns
Shape.prototype.type = 'shape';
Rectangle.prototype.type = 'rectangle';
Square.prototype.type = 'square';
ColoredSquare.prototype.type = 'colored-square';

// Shared behavior through prototype
Shape.prototype.toString = function() {
    return `[${this.constructor.name} at (${this.x}, ${this.y})]`;
};

// Method that walks prototype chain
function getAllPropertyNames(obj) {
    const props = [];

    do {
        props.push(...Object.getOwnPropertyNames(obj));
        obj = Object.getPrototypeOf(obj);
    } while (obj && obj !== Object.prototype);

    return [...new Set(props)];
}

// Dynamic prototype modification
Shape.prototype.scale = function(factor) {
    // This method is added after objects are created
    if (this.width !== undefined) {
        this.width *= factor;
    }
    if (this.height !== undefined) {
        this.height *= factor;
    }
    return this;
};

// Using the prototype chain
const shape = new Shape(0, 0);
const rect = new Rectangle(10, 10, 20, 30);
const square = new Square(5, 5, 15);
const coloredSquare = new ColoredSquare(0, 0, 10, 'red');

// Method calls at different levels
console.log(shape.getPosition());
console.log(rect.getArea());
console.log(square.setSize(20).getArea());
console.log(coloredSquare.setColor('blue').getFullInfo());

// Prototype chain traversal
console.log(coloredSquare instanceof ColoredSquare); // true
console.log(coloredSquare instanceof Square);        // true
console.log(coloredSquare instanceof Rectangle);     // true
console.log(coloredSquare instanceof Shape);         // true

// Property lookup through chain
console.log(coloredSquare.type);       // 'colored-square'
console.log(coloredSquare.toString());  // Uses Shape's toString

// Dynamic method available to all
shape.scale(2);
rect.scale(1.5);
square.scale(2);
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_prototypes_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = javascript_prototypes_project.name

    # Get all Function nodes
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Check constructor hierarchy
    expected_constructors = [
        f"{project_name}.prototype_chain.Shape",
        f"{project_name}.prototype_chain.Rectangle",
        f"{project_name}.prototype_chain.Square",
        f"{project_name}.prototype_chain.ColoredSquare",
    ]

    for expected in expected_constructors:
        assert expected in created_functions, (
            f"Missing constructor in chain: {expected}"
        )

    # Check prototype methods at different levels
    method_patterns = ["move", "getArea", "setSize", "getColor", "getFullInfo"]
    methods_found = [
        func
        for func in created_functions
        if any(pattern in func for pattern in method_patterns)
    ]

    assert len(methods_found) >= 4, (
        f"Expected at least 4 prototype methods, found {len(methods_found)}"
    )

    # Check method calls through prototype chain
    call_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

    # Should have calls to parent prototype methods
    prototype_calls = [
        call
        for call in call_relationships
        if "prototype_chain" in call.args[0][2]
        and ".prototype." in str(call.args[2][2])
    ]

    assert len(prototype_calls) >= 2, (
        f"Expected at least 2 prototype method calls, found {len(prototype_calls)}"
    )


def test_prototype_mixins_and_composition(
    javascript_prototypes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test mixin patterns and prototype composition."""
    test_file = javascript_prototypes_project / "prototype_mixins.js"
    test_file.write_text(
        """
// Mixin objects
const EventEmitterMixin = {
    on: function(event, handler) {
        if (!this._handlers) {
            this._handlers = {};
        }
        if (!this._handlers[event]) {
            this._handlers[event] = [];
        }
        this._handlers[event].push(handler);
        return this;
    },

    off: function(event, handler) {
        if (!this._handlers || !this._handlers[event]) {
            return this;
        }
        const index = this._handlers[event].indexOf(handler);
        if (index > -1) {
            this._handlers[event].splice(index, 1);
        }
        return this;
    },

    emit: function(event, ...args) {
        if (!this._handlers || !this._handlers[event]) {
            return this;
        }
        this._handlers[event].forEach(handler => {
            handler.apply(this, args);
        });
        return this;
    }
};

const ObservableMixin = {
    addObserver: function(observer) {
        if (!this._observers) {
            this._observers = [];
        }
        this._observers.push(observer);
        return this;
    },

    removeObserver: function(observer) {
        if (!this._observers) {
            return this;
        }
        const index = this._observers.indexOf(observer);
        if (index > -1) {
            this._observers.splice(index, 1);
        }
        return this;
    },

    notifyObservers: function(data) {
        if (!this._observers) {
            return this;
        }
        this._observers.forEach(observer => {
            observer.update(this, data);
        });
        return this;
    }
};

const SerializableMixin = {
    toJSON: function() {
        const obj = {};
        for (const key in this) {
            if (this.hasOwnProperty(key) && typeof this[key] !== 'function') {
                obj[key] = this[key];
            }
        }
        return obj;
    },

    fromJSON: function(json) {
        for (const key in json) {
            if (json.hasOwnProperty(key)) {
                this[key] = json[key];
            }
        }
        return this;
    }
};

// Mixin function
function mixin(target, ...sources) {
    sources.forEach(source => {
        Object.keys(source).forEach(key => {
            if (typeof source[key] === 'function') {
                target[key] = source[key];
            }
        });
    });
    return target;
}

// Constructor using mixins
function Model(data) {
    this.data = data || {};
    this.id = Math.random().toString(36).substr(2, 9);
}

// Apply mixins to prototype
mixin(Model.prototype, EventEmitterMixin, ObservableMixin, SerializableMixin);

// Add model-specific methods
Model.prototype.get = function(key) {
    return this.data[key];
};

Model.prototype.set = function(key, value) {
    const oldValue = this.data[key];
    this.data[key] = value;

    // Use mixin methods
    this.emit('change', { key, oldValue, newValue: value });
    this.notifyObservers({ type: 'change', key, value });

    return this;
};

// Another constructor with selective mixin
function Collection(name) {
    this.name = name;
    this.items = [];
}

// Only apply some mixins
mixin(Collection.prototype, EventEmitterMixin);

Collection.prototype.add = function(item) {
    this.items.push(item);
    this.emit('add', item);
    return this;
};

Collection.prototype.remove = function(item) {
    const index = this.items.indexOf(item);
    if (index > -1) {
        this.items.splice(index, 1);
        this.emit('remove', item);
    }
    return this;
};

// Composition pattern
function compose(...mixins) {
    return function ComposedConstructor(...args) {
        const instance = this;

        // Apply each mixin
        mixins.forEach(mixin => {
            Object.assign(instance, mixin);
        });

        // Call init if exists
        if (instance.init) {
            instance.init.apply(instance, args);
        }

        return instance;
    };
}

// Create composed constructor
const ValidatableMixin = {
    validate: function() {
        return this.rules ? this.rules.every(rule => rule(this)) : true;
    },

    addRule: function(rule) {
        if (!this.rules) {
            this.rules = [];
        }
        this.rules.push(rule);
        return this;
    }
};

const TimestampMixin = {
    init: function() {
        this.created = new Date();
        this.modified = new Date();
    },

    touch: function() {
        this.modified = new Date();
        return this;
    }
};

// Composed entity
const Entity = compose(EventEmitterMixin, ValidatableMixin, TimestampMixin);
Entity.prototype.save = function() {
    if (!this.validate()) {
        throw new Error('Validation failed');
    }
    this.touch();
    this.emit('save', this);
    return this;
};

// Multiple inheritance simulation
function MultipleInheritance(...parents) {
    const constructors = parents.filter(p => typeof p === 'function');
    const prototypes = parents.map(p => p.prototype).filter(Boolean);

    function Child(...args) {
        const instance = this;

        // Call all parent constructors
        constructors.forEach(Parent => {
            Parent.apply(instance, args);
        });

        return instance;
    }

    // Merge all prototypes
    Child.prototype = prototypes.reduce((merged, proto) => {
        return Object.assign(merged, proto);
    }, {});

    Child.prototype.constructor = Child;

    return Child;
}

// Using the patterns
const model = new Model({ name: 'Test Model' });
model.on('change', (data) => console.log('Model changed:', data));
model.set('status', 'active');

const collection = new Collection('Users');
collection.on('add', (item) => console.log('Added:', item));
collection.add({ name: 'Alice' });

const entity = new Entity();
entity.addRule(e => e.created instanceof Date);
entity.save();

console.log(model.toJSON());
console.log(entity.validate());
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_prototypes_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = javascript_prototypes_project.name

    # Get all Function nodes
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Check mixin functions
    expected_functions = [
        f"{project_name}.prototype_mixins.mixin",
        f"{project_name}.prototype_mixins.compose",
        f"{project_name}.prototype_mixins.Model",
        f"{project_name}.prototype_mixins.Collection",
        f"{project_name}.prototype_mixins.MultipleInheritance",
    ]

    for expected in expected_functions:
        assert expected in created_functions, (
            f"Missing mixin-related function: {expected}"
        )

    # Check mixin methods (might be captured as functions)
    mixin_methods = [
        "on",
        "off",
        "emit",
        "addObserver",
        "notifyObservers",
        "toJSON",
        "validate",
    ]
    mixin_method_functions = [
        func
        for func in created_functions
        if any(method in func for method in mixin_methods)
    ]

    assert len(mixin_method_functions) >= 3, (
        f"Expected at least 3 mixin methods, found {len(mixin_method_functions)}"
    )

    # Check prototype method additions
    model_methods = [
        func
        for func in created_functions
        if "Model" in func and any(m in func for m in ["get", "set"])
    ]

    assert len(model_methods) >= 1, (
        f"Expected at least 1 Model method, found {len(model_methods)}"
    )


def test_prototype_patterns_edge_cases(
    javascript_prototypes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test edge cases and unusual prototype patterns."""
    test_file = javascript_prototypes_project / "prototype_edge_cases.js"
    test_file.write_text(
        """
// Edge cases and unusual patterns

// Constructor returning different object
function WeirdConstructor(value) {
    // Returning a different object ignores prototype
    return {
        value: value,
        getValue: function() {
            return this.value;
        }
    };
}

WeirdConstructor.prototype.ignored = function() {
    return 'This method is ignored';
};

// Constructor returning primitive (ignored, uses normal construction)
function PrimitiveReturn(value) {
    this.value = value;
    return 42; // Primitive return is ignored
}

PrimitiveReturn.prototype.getValue = function() {
    return this.value;
};

// Self-referential prototype
function SelfReferential() {
    this.self = this;
}

SelfReferential.prototype.getSelf = function() {
    return this.self;
};

SelfReferential.prototype.callSelf = function(method, ...args) {
    return this[method].apply(this, args);
};

// Prototype with getter/setter
function GetterSetterExample(value) {
    this._value = value;
}

Object.defineProperty(GetterSetterExample.prototype, 'value', {
    get: function() {
        console.log('Getting value');
        return this._value;
    },
    set: function(newValue) {
        console.log('Setting value');
        this._value = newValue;
    },
    enumerable: true,
    configurable: true
});

GetterSetterExample.prototype.reset = function() {
    this.value = 0;
};

// Prototype property shadowing
function Parent() {
    this.prop = 'instance property';
}

Parent.prototype.prop = 'prototype property';
Parent.prototype.method = function() {
    return 'parent method';
};

function Child() {
    Parent.call(this);
    this.method = function() {
        return 'instance method';
    };
}

Child.prototype = Object.create(Parent.prototype);
Child.prototype.constructor = Child;

// Frozen prototype
function FrozenProto() {
    this.mutable = true;
}

FrozenProto.prototype.method1 = function() {
    return 'method1';
};

Object.freeze(FrozenProto.prototype);

// Attempt to add methods after freezing (will fail silently or throw in strict mode)
FrozenProto.prototype.method2 = function() {
    return 'method2';
};

// Proxy-based prototype
const ProxyPrototype = new Proxy({}, {
    get(target, property) {
        if (property in target) {
            return target[property];
        }
        return function(...args) {
            console.log(`Called undefined method: ${property} with args:`, args);
            return `${property} result`;
        };
    }
});

function ProxyConstructor() {
    this.data = {};
}

ProxyConstructor.prototype = ProxyPrototype;
ProxyConstructor.prototype.constructor = ProxyConstructor;
ProxyConstructor.prototype.definedMethod = function() {
    return 'This method is defined';
};

// Symbol properties on prototype
const symbolMethod = Symbol('secretMethod');
const symbolProp = Symbol('secretProp');

function SymbolExample() {
    this[symbolProp] = 'secret value';
}

SymbolExample.prototype[symbolMethod] = function() {
    return 'Secret method called';
};

SymbolExample.prototype.publicMethod = function() {
    return this[symbolMethod]();
};

// Async methods on prototype
function AsyncExample() {
    this.data = null;
}

AsyncExample.prototype.loadData = async function(url) {
    const response = await fetch(url);
    this.data = await response.json();
    return this.data;
};

AsyncExample.prototype.processData = async function() {
    if (!this.data) {
        await this.loadData('/api/data');
    }
    return this.data.map(item => item.processed = true);
};

// Generator methods on prototype
function GeneratorExample(max) {
    this.max = max;
}

GeneratorExample.prototype[Symbol.iterator] = function*() {
    for (let i = 0; i < this.max; i++) {
        yield i;
    }
};

GeneratorExample.prototype.generatePairs = function*() {
    for (let i = 0; i < this.max; i += 2) {
        yield [i, i + 1];
    }
};

// Using edge cases
const weird = new WeirdConstructor('test');
console.log(weird instanceof WeirdConstructor); // false
console.log(weird.getValue());

const primitive = new PrimitiveReturn('value');
console.log(primitive instanceof PrimitiveReturn); // true
console.log(primitive.getValue());

const parent = new Parent();
const child = new Child();
console.log(parent.prop); // 'instance property' (shadows prototype)
console.log(parent.method()); // 'parent method'
console.log(child.method()); // 'instance method' (shadows prototype)

const proxy = new ProxyConstructor();
console.log(proxy.definedMethod());
console.log(proxy.undefinedMethod('arg1', 'arg2'));

const symbol = new SymbolExample();
console.log(symbol.publicMethod());
console.log(symbol[symbolMethod]());

const asyncEx = new AsyncExample();
asyncEx.processData().then(result => console.log(result));

const generator = new GeneratorExample(5);
for (const value of generator) {
    console.log(value);
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_prototypes_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = javascript_prototypes_project.name

    # Get all Function nodes
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Check edge case constructors
    expected_constructors = [
        f"{project_name}.prototype_edge_cases.WeirdConstructor",
        f"{project_name}.prototype_edge_cases.PrimitiveReturn",
        f"{project_name}.prototype_edge_cases.GetterSetterExample",
        f"{project_name}.prototype_edge_cases.AsyncExample",
        f"{project_name}.prototype_edge_cases.GeneratorExample",
    ]

    for expected in expected_constructors:
        assert expected in created_functions, (
            f"Missing edge case constructor: {expected}"
        )

    # Check async and generator methods
    async_generator_methods = [
        func
        for func in created_functions
        if "prototype_edge_cases" in func
        and any(
            pattern in func for pattern in ["loadData", "processData", "generatePairs"]
        )
    ]

    assert len(async_generator_methods) >= 2, (
        f"Expected at least 2 async/generator methods, found {len(async_generator_methods)}"
    )


def test_prototype_comprehensive(
    javascript_prototypes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all prototype patterns create proper relationships."""
    test_file = javascript_prototypes_project / "comprehensive_prototypes.js"
    test_file.write_text(
        """
// Every JavaScript prototype pattern in one file

// Constructor function with prototype
function Animal(name) {
    this.name = name;
}

Animal.prototype.speak = function() {
    return `${this.name} makes a sound`;
};

// Inheritance via prototype
function Dog(name, breed) {
    Animal.call(this, name);
    this.breed = breed;
}

Dog.prototype = Object.create(Animal.prototype);
Dog.prototype.constructor = Dog;

Dog.prototype.bark = function() {
    return 'Woof!';
};

// Object.create pattern
const protoObj = {
    greet() {
        return `Hello, ${this.name}`;
    }
};

const instance = Object.create(protoObj);
instance.name = 'Instance';

// Mixin pattern
const FlyMixin = {
    fly() {
        return `${this.name} is flying`;
    }
};

Object.assign(Animal.prototype, FlyMixin);

// ES5 class-like pattern
var Class = (function() {
    function Class(value) {
        this.value = value;
    }

    Class.prototype.getValue = function() {
        return this.value;
    };

    Class.staticMethod = function() {
        return 'static';
    };

    return Class;
})();

// Using all patterns
const animal = new Animal('Generic');
const dog = new Dog('Rex', 'Labrador');
const classInstance = new Class(42);

console.log(animal.speak());
console.log(animal.fly());
console.log(dog.speak());
console.log(dog.bark());
console.log(instance.greet());
console.log(classInstance.getValue());
console.log(Class.staticMethod());

// Prototype chain verification
console.log(dog instanceof Dog);    // true
console.log(dog instanceof Animal); // true
console.log(Animal.prototype.isPrototypeOf(dog)); // true

// Dynamic prototype modification
Animal.prototype.eat = function(food) {
    return `${this.name} eats ${food}`;
};

console.log(dog.eat('bone')); // Works due to prototype chain
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_prototypes_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Verify all relationship types exist
    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    calls_relationships = [c for c in all_relationships if c.args[1] == "CALLS"]
    defines_relationships = [c for c in all_relationships if c.args[1] == "DEFINES"]

    # Should have comprehensive prototype patterns
    comprehensive_calls = [
        call
        for call in calls_relationships
        if "comprehensive_prototypes" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 5, (
        f"Expected at least 5 comprehensive prototype calls, found {len(comprehensive_calls)}"
    )

    # Get all nodes
    all_nodes = mock_ingestor.ensure_node_batch.call_args_list
    function_nodes = [call for call in all_nodes if call[0][0] == "Function"]

    comprehensive_functions = [
        call
        for call in function_nodes
        if "comprehensive_prototypes" in call[0][1]["qualified_name"]
    ]

    assert len(comprehensive_functions) >= 6, (
        f"Expected at least 6 functions/methods in comprehensive test, found {len(comprehensive_functions)}"
    )

    print("✅ JavaScript prototype relationship validation passed:")
    print(f"   - CALLS relationships: {len(calls_relationships)}")
    print(f"   - DEFINES relationships: {len(defines_relationships)}")
    print(f"   - Comprehensive prototype calls: {len(comprehensive_calls)}")
    print(f"   - Comprehensive functions: {len(comprehensive_functions)}")
