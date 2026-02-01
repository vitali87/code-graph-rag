from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import SEPARATOR_DOT
from codebase_rag.tests.conftest import (
    get_node_names,
    get_nodes,
    get_qualified_names,
    get_relationships,
    run_updater,
)


@pytest.fixture
def javascript_functions_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with all function patterns."""
    project_path = temp_repo / "javascript_functions_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "utils").mkdir()

    (project_path / "src" / "helpers.js").write_text(
        encoding="utf-8", data="export const log = console.log;"
    )
    (project_path / "utils" / "common.js").write_text(
        encoding="utf-8",
        data="export function isString(value) { return typeof value === 'string'; }",
    )

    return project_path


def test_function_declarations(
    javascript_functions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test traditional function declaration parsing."""
    test_file = javascript_functions_project / "function_declarations.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Traditional function declarations
function greet(name) {
    return `Hello, ${name}!`;
}

function add(a, b) {
    return a + b;
}

function multiply(x, y) {
    return x * y;
}

// Function with default parameters
function createUser(name, age = 18, active = true) {
    return { name, age, active };
}

// Function with rest parameters
function sum(...numbers) {
    return numbers.reduce((acc, num) => acc + num, 0);
}

// Function with destructured parameters
function processUser({ name, email }, options = {}) {
    return { name, email, ...options };
}

// Nested function declarations
function outer() {
    function inner() {
        return "inner function";
    }

    function anotherInner(param) {
        return `inner with ${param}`;
    }

    return inner() + " " + anotherInner("param");
}

// Function calling other functions
function calculator(operation, a, b) {
    if (operation === 'add') {
        return add(a, b);
    } else if (operation === 'multiply') {
        return multiply(a, b);
    }
    return 0;
}

// Using all functions
const greeting = greet("World");
const result = calculator("add", 5, 3);
const user = createUser("John", 25);
const total = sum(1, 2, 3, 4, 5);
""",
    )

    run_updater(javascript_functions_project, mock_ingestor)

    project_name = javascript_functions_project.name

    expected_functions = [
        f"{project_name}.function_declarations.greet",
        f"{project_name}.function_declarations.add",
        f"{project_name}.function_declarations.multiply",
        f"{project_name}.function_declarations.createUser",
        f"{project_name}.function_declarations.sum",
        f"{project_name}.function_declarations.processUser",
        f"{project_name}.function_declarations.outer",
        f"{project_name}.function_declarations.outer.inner",
        f"{project_name}.function_declarations.outer.anotherInner",
        f"{project_name}.function_declarations.calculator",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    for expected_qn in expected_functions:
        assert expected_qn in created_functions, f"Missing function: {expected_qn}"

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    function_to_function_calls = [
        call
        for call in call_relationships
        if "function_declarations" in call.args[0][2]
        and "function_declarations" in call.args[2][2]
    ]

    assert len(function_to_function_calls) >= 2, (
        f"Expected at least 2 function-to-function calls, found {len(function_to_function_calls)}"
    )


def test_arrow_functions(
    javascript_functions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test arrow function parsing and various arrow function patterns."""
    test_file = javascript_functions_project / "arrow_functions.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Simple arrow functions
const double = x => x * 2;
const square = x => x * x;

// Arrow functions with multiple parameters
const add = (a, b) => a + b;
const subtract = (a, b) => a - b;

// Arrow functions with block body
const greet = name => {
    const message = `Hello, ${name}!`;
    return message;
};

// Arrow function with complex body
const processArray = arr => {
    const filtered = arr.filter(x => x > 0);
    const doubled = filtered.map(x => x * 2);
    return doubled.reduce((sum, x) => sum + x, 0);
};

// Arrow functions as callbacks
const numbers = [1, 2, 3, 4, 5];
const doubled = numbers.map(n => n * 2);
const evens = numbers.filter(n => n % 2 === 0);
const sum = numbers.reduce((acc, n) => acc + n, 0);

// Arrow functions in object methods
const calculator = {
    add: (a, b) => a + b,
    multiply: (a, b) => a * b,
    power: (base, exp) => Math.pow(base, exp)
};

// Arrow functions with destructuring
const getFullName = ({ firstName, lastName }) => `${firstName} ${lastName}`;
const processConfig = ({ host = 'localhost', port = 3000 }) => `${host}:${port}`;

// Nested arrow functions
const createMultiplier = factor => value => value * factor;
const createValidator = rule => data => rule(data);

// Arrow functions in arrays
const operations = [
    x => x + 1,
    x => x * 2,
    x => x - 1
];

// Using arrow functions
const result1 = double(5);
const result2 = add(3, 4);
const greeting = greet("World");
const arrayResult = processArray([1, -2, 3, -4, 5]);
const calcResult = calculator.add(10, 20);
const fullName = getFullName({ firstName: "John", lastName: "Doe" });
const doubler = createMultiplier(2);
const quadrupled = doubler(8);
""",
    )

    run_updater(javascript_functions_project, mock_ingestor)

    project_name = javascript_functions_project.name

    function_calls = get_nodes(mock_ingestor, "Function")

    arrow_functions = [
        call
        for call in function_calls
        if "arrow_functions" in call[0][1]["qualified_name"]
    ]

    assert len(arrow_functions) >= 10, (
        f"Expected at least 10 arrow functions, found {len(arrow_functions)}"
    )

    created_functions = get_qualified_names(function_calls)
    expected_arrow_functions = [
        f"{project_name}.arrow_functions.double",
        f"{project_name}.arrow_functions.add",
        f"{project_name}.arrow_functions.greet",
        f"{project_name}.arrow_functions.processArray",
    ]

    for expected in expected_arrow_functions:
        assert expected in created_functions, f"Missing arrow function: {expected}"


def test_async_functions(
    javascript_functions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test async function parsing and async/await patterns."""
    test_file = javascript_functions_project / "async_functions.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Async function declarations
async function fetchData(url) {
    const response = await fetch(url);
    const data = await response.json();
    return data;
}

async function processData(data) {
    const processed = await transformData(data);
    await saveToDatabase(processed);
    return processed;
}

// Async arrow functions
const loadUser = async (id) => {
    const user = await fetchUser(id);
    const profile = await fetchProfile(id);
    return { ...user, ...profile };
};

const uploadFile = async (file) => {
    try {
        const result = await upload(file);
        return result;
    } catch (error) {
        console.error('Upload failed:', error);
        throw error;
    }
};

// Async functions with complex logic
async function batchProcess(items) {
    const results = [];

    for (const item of items) {
        try {
            const processed = await processItem(item);
            results.push(processed);
        } catch (error) {
            console.error(`Failed to process item:`, error);
        }
    }

    return results;
}

// Async functions using Promise.all
async function fetchMultiple(urls) {
    const promises = urls.map(url => fetchData(url));
    const results = await Promise.all(promises);
    return results;
}

// Async function with timeout
async function fetchWithTimeout(url, timeout = 5000) {
    const timeoutPromise = new Promise((_, reject) =>
        setTimeout(() => reject(new Error('Timeout')), timeout)
    );

    const fetchPromise = fetch(url);

    return Promise.race([fetchPromise, timeoutPromise]);
}

// Async methods in objects
const api = {
    async get(endpoint) {
        return await fetchData(endpoint);
    },

    async post(endpoint, data) {
        const response = await fetch(endpoint, {
            method: 'POST',
            body: JSON.stringify(data)
        });
        return response.json();
    }
};

// Async IIFE
(async () => {
    const data = await fetchData('/api/initial');
    console.log('Initial data:', data);
})();

// Using async functions
fetchData('/api/users').then(users => console.log(users));
loadUser(123).then(user => console.log(user));
batchProcess([1, 2, 3]).then(results => console.log(results));

// Async function calling other async functions
async function orchestrate() {
    const data = await fetchData('/api/data');
    const processed = await processData(data);
    const user = await loadUser(processed.userId);
    return { processed, user };
}
""",
    )

    run_updater(javascript_functions_project, mock_ingestor)

    project_name = javascript_functions_project.name

    expected_async_functions = [
        f"{project_name}.async_functions.fetchData",
        f"{project_name}.async_functions.processData",
        f"{project_name}.async_functions.loadUser",
        f"{project_name}.async_functions.uploadFile",
        f"{project_name}.async_functions.batchProcess",
        f"{project_name}.async_functions.fetchMultiple",
        f"{project_name}.async_functions.fetchWithTimeout",
        f"{project_name}.async_functions.orchestrate",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    for expected_qn in expected_async_functions:
        assert expected_qn in created_functions, (
            f"Missing async function: {expected_qn}"
        )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    async_to_async_calls = [
        call
        for call in call_relationships
        if "async_functions.orchestrate" in call.args[0][2]
        and "async_functions" in call.args[2][2]
    ]

    assert len(async_to_async_calls) >= 2, (
        f"Expected at least 2 async function calls, found {len(async_to_async_calls)}"
    )


def test_immediately_invoked_function_expressions(
    javascript_functions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test IIFE (Immediately Invoked Function Expression) parsing."""
    test_file = javascript_functions_project / "iife_patterns.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic IIFE
(function() {
    console.log('IIFE executed');
})();

// IIFE with parameters
(function(message) {
    console.log(message);
})('Hello from IIFE');

// IIFE with return value
const result = (function(a, b) {
    return a + b;
})(5, 3);

// Arrow function IIFE
((name) => {
    console.log(`Hello, ${name}!`);
})('World');

// IIFE with complex logic
const modulePattern = (function() {
    let privateVar = 0;

    function privateFunction() {
        return privateVar++;
    }

    return {
        increment: function() {
            return privateFunction();
        },
        getValue: function() {
            return privateVar;
        }
    };
})();

// Async IIFE
(async function() {
    try {
        const data = await fetch('/api/data');
        const json = await data.json();
        console.log(json);
    } catch (error) {
        console.error('Failed to fetch data:', error);
    }
})();

// IIFE for module initialization
const Config = (function() {
    const settings = {
        apiUrl: 'https://api.example.com',
        timeout: 5000
    };

    function validateSettings() {
        if (!settings.apiUrl) {
            throw new Error('API URL is required');
        }
    }

    validateSettings();

    return {
        get: function(key) {
            return settings[key];
        },
        set: function(key, value) {
            settings[key] = value;
            validateSettings();
        }
    };
})();

// IIFE with external dependencies
const Utils = (function($, _) {
    function helper(data) {
        return _.map(data, item => $(item).text());
    }

    return {
        processElements: helper
    };
})(jQuery, lodash);

// Using IIFE results
const incrementedValue = modulePattern.increment();
const configValue = Config.get('apiUrl');
""",
    )

    run_updater(javascript_functions_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    iife_functions = [
        call
        for call in function_calls
        if "iife_patterns" in call[0][1]["qualified_name"]
    ]

    assert len(iife_functions) >= 5, (
        f"Expected at least 5 IIFE functions, found {len(iife_functions)}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    iife_calls = [
        call for call in call_relationships if "iife_patterns" in call.args[0][2]
    ]

    assert len(iife_calls) >= 3, (
        f"Expected at least 3 IIFE calls, found {len(iife_calls)}"
    )


def test_higher_order_functions(
    javascript_functions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test higher-order functions (functions that take or return functions)."""
    test_file = javascript_functions_project / "higher_order_functions.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Functions that return functions
function createAdder(x) {
    return function(y) {
        return x + y;
    };
}

function createMultiplier(factor) {
    return function(value) {
        return value * factor;
    };
}

// Arrow function returning arrow function
const createValidator = rule => data => rule(data);
const createFormatter = template => value => template.replace('{}', value);

// Functions that take functions as parameters
function applyOperation(arr, operation) {
    return arr.map(operation);
}

function compose(f, g) {
    return function(x) {
        return f(g(x));
    };
}

function pipe(...functions) {
    return function(value) {
        return functions.reduce((acc, fn) => fn(acc), value);
    };
}

// Decorator pattern
function withLogging(fn) {
    return function(...args) {
        console.log(`Calling ${fn.name} with args:`, args);
        const result = fn.apply(this, args);
        console.log(`Result:`, result);
        return result;
    };
}

function withTiming(fn) {
    return function(...args) {
        const start = Date.now();
        const result = fn.apply(this, args);
        const end = Date.now();
        console.log(`${fn.name} took ${end - start}ms`);
        return result;
    };
}

// Curried functions
const add = a => b => a + b;
const multiply = a => b => a * b;
const subtract = a => b => a - b;

// Partial application
function partial(fn, ...args1) {
    return function(...args2) {
        return fn.apply(this, args1.concat(args2));
    };
}

// Memoization
function memoize(fn) {
    const cache = new Map();

    return function(...args) {
        const key = JSON.stringify(args);

        if (cache.has(key)) {
            return cache.get(key);
        }

        const result = fn.apply(this, args);
        cache.set(key, result);
        return result;
    };
}

// Using higher-order functions
const add5 = createAdder(5);
const double = createMultiplier(2);
const isPositive = createValidator(x => x > 0);
const formatMessage = createFormatter('Message: {}');

const numbers = [1, 2, 3, 4, 5];
const doubled = applyOperation(numbers, double);
const added = applyOperation(numbers, add5);

const addThenDouble = compose(double, add5);
const pipeline = pipe(add5, double, x => x - 1);

const loggedAdd = withLogging(add5);
const timedDouble = withTiming(double);

const add10 = add(10);
const multiply3 = multiply(3);

const partialAdd = partial(add5, 10);
const memoizedAdd = memoize(add5);
""",
    )

    run_updater(javascript_functions_project, mock_ingestor)

    project_name = javascript_functions_project.name

    expected_hof_functions = [
        f"{project_name}.higher_order_functions.createAdder",
        f"{project_name}.higher_order_functions.createMultiplier",
        f"{project_name}.higher_order_functions.createValidator",
        f"{project_name}.higher_order_functions.applyOperation",
        f"{project_name}.higher_order_functions.compose",
        f"{project_name}.higher_order_functions.pipe",
        f"{project_name}.higher_order_functions.withLogging",
        f"{project_name}.higher_order_functions.withTiming",
        f"{project_name}.higher_order_functions.add",
        f"{project_name}.higher_order_functions.partial",
        f"{project_name}.higher_order_functions.memoize",
    ]

    function_calls = get_nodes(mock_ingestor, "Function")
    created_functions = get_qualified_names(function_calls)

    for expected_qn in expected_hof_functions:
        assert expected_qn in created_functions, (
            f"Missing higher-order function: {expected_qn}"
        )

    nested_functions = [
        call
        for call in function_calls
        if "higher_order_functions" in call[0][1]["qualified_name"]
        and len(call[0][1]["qualified_name"].split(SEPARATOR_DOT)) > 3
    ]

    assert len(nested_functions) >= 5, (
        f"Expected at least 5 nested functions from higher-order functions, found {len(nested_functions)}"
    )


def test_method_definitions(
    javascript_functions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test object method definitions and class method parsing."""
    test_file = javascript_functions_project / "method_definitions.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Object with method definitions
const calculator = {
    // Regular method
    add(a, b) {
        return a + b;
    },

    // Arrow function method
    subtract: (a, b) => a - b,

    // Method with complex logic
    multiply(a, b) {
        const result = a * b;
        this.logOperation('multiply', a, b, result);
        return result;
    },

    // Method calling other methods
    power(base, exponent) {
        let result = 1;
        for (let i = 0; i < exponent; i++) {
            result = this.multiply(result, base);
        }
        return result;
    },

    // Async method
    async fetchAndAdd(url, value) {
        const data = await fetch(url);
        const number = await data.json();
        return this.add(number, value);
    },

    // Helper method
    logOperation(operation, a, b, result) {
        console.log(`${operation}(${a}, ${b}) = ${result}`);
    }
};

// Object with getters and setters
const user = {
    firstName: 'John',
    lastName: 'Doe',

    get fullName() {
        return `${this.firstName} ${this.lastName}`;
    },

    set fullName(value) {
        const parts = value.split(' ');
        this.firstName = parts[0];
        this.lastName = parts[1];
    },

    get initials() {
        return `${this.firstName[0]}.${this.lastName[0]}.`;
    }
};

// Prototype methods
function Person(name, age) {
    this.name = name;
    this.age = age;
}

Person.prototype.greet = function() {
    return `Hello, I'm ${this.name}`;
};

Person.prototype.getAge = function() {
    return this.age;
};

Person.prototype.setAge = function(newAge) {
    this.age = newAge;
};

// Static-like methods on constructor
Person.createAdult = function(name) {
    return new Person(name, 18);
};

Person.isValidAge = function(age) {
    return age >= 0 && age <= 150;
};

// Methods in nested objects
const api = {
    users: {
        async getAll() {
            return await fetch('/api/users');
        },

        async getById(id) {
            return await fetch(`/api/users/${id}`);
        },

        create(userData) {
            return fetch('/api/users', {
                method: 'POST',
                body: JSON.stringify(userData)
            });
        }
    },

    posts: {
        getAll() {
            return fetch('/api/posts');
        },

        getByUser(userId) {
            return this.getAll()
                .then(response => response.json())
                .then(posts => posts.filter(post => post.userId === userId));
        }
    }
};

// Using methods
const sum = calculator.add(5, 3);
const difference = calculator.subtract(10, 4);
const product = calculator.multiply(6, 7);
const powerResult = calculator.power(2, 3);

const fullName = user.fullName;
user.fullName = 'Jane Smith';
const initials = user.initials;

const person = new Person('Alice', 25);
const greeting = person.greet();
const age = person.getAge();
person.setAge(26);

const adult = Person.createAdult('Bob');
const isValid = Person.isValidAge(30);
""",
    )

    run_updater(javascript_functions_project, mock_ingestor)

    method_calls = get_nodes(mock_ingestor, "Method")

    function_calls = get_nodes(mock_ingestor, "Function")

    all_methods = method_calls + function_calls
    method_definitions = [
        call
        for call in all_methods
        if "method_definitions" in call[0][1]["qualified_name"]
    ]

    assert len(method_definitions) >= 10, (
        f"Expected at least 10 method definitions, found {len(method_definitions)}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    method_calls = [
        call for call in call_relationships if "method_definitions" in call.args[0][2]
    ]

    assert len(method_calls) >= 5, (
        f"Expected at least 5 method calls, found {len(method_calls)}"
    )


def test_function_comprehensive(
    javascript_functions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all function types create proper relationships."""
    test_file = javascript_functions_project / "comprehensive_functions.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every JavaScript function pattern in one file

// Function declaration
function regularFunction(param) {
    return param * 2;
}

// Arrow functions
const arrowFunction = x => x + 1;
const blockArrowFunction = (a, b) => {
    const sum = a + b;
    return sum * 2;
};

// Async functions
async function asyncFunction() {
    const data = await fetch('/api/data');
    return data.json();
}

const asyncArrow = async (id) => {
    return await getData(id);
};

// IIFE
(function() {
    console.log('IIFE executed');
})();

// Higher-order function
function createProcessor(operation) {
    return function(value) {
        return operation(value);
    };
}

// Object methods
const obj = {
    method() {
        return 'object method';
    },

    arrowMethod: () => 'arrow method',

    async asyncMethod() {
        return await this.method();
    }
};

// Constructor function
function Constructor(value) {
    this.value = value;
}

Constructor.prototype.getValue = function() {
    return this.value;
};

// Using functions
const result1 = regularFunction(5);
const result2 = arrowFunction(10);
const result3 = blockArrowFunction(3, 4);
const processor = createProcessor(x => x * 3);
const processed = processor(7);
const methodResult = obj.method();
const instance = new Constructor(42);
const value = instance.getValue();

// Function calling other functions
function orchestrator() {
    const regular = regularFunction(5);
    const arrow = arrowFunction(regular);
    const object = obj.method();
    return { regular, arrow, object };
}

const orchestrated = orchestrator();
""",
    )

    run_updater(javascript_functions_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_functions" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 8, (
        f"Expected at least 8 comprehensive function calls, found {len(comprehensive_calls)}"
    )

    for relationship in comprehensive_calls:
        assert len(relationship.args) == 3, "Call relationship should have 3 args"
        assert relationship.args[1] == "CALLS", "Second arg should be 'CALLS'"

        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        assert "comprehensive_functions" in source_module, (
            f"Source module should contain test file name: {source_module}"
        )

        assert isinstance(target_module, str) and target_module, (
            f"Target should be non-empty string: {target_module}"
        )

    assert defines_relationships, "Should still have DEFINES relationships"
