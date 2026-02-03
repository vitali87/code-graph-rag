from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    get_node_names,
    get_nodes,
    get_relationships,
    run_updater,
)


@pytest.fixture
def javascript_spread_rest_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with spread/rest patterns."""
    project_path = temp_repo / "javascript_spread_rest_test"
    project_path.mkdir()

    (project_path / "utils").mkdir()

    (project_path / "utils" / "arrays.js").write_text(
        encoding="utf-8",
        data="""
export function mergeArrays(arr1, arr2) {
    return [...arr1, ...arr2];
}

export function clone(arr) {
    return [...arr];
}
""",
    )

    return project_path


def test_spread_in_arrays(
    javascript_spread_rest_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test spread operator usage in arrays."""
    test_file = javascript_spread_rest_project / "array_spread.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Array spread patterns

// Basic array spreading
const arr1 = [1, 2, 3];
const arr2 = [4, 5, 6];
const combined = [...arr1, ...arr2];

// Spreading with literals
const withLiterals = [...arr1, 10, 11, ...arr2, 12];

// Cloning arrays
const original = [1, 2, 3, 4, 5];
const cloned = [...original];
const clonedWithModification = [...original, 6, 7];

// Converting iterables to arrays
const string = "hello";
const stringArray = [...string]; // ['h', 'e', 'l', 'l', 'o']

const set = new Set([1, 2, 3, 3, 4]);
const setArray = [...set]; // [1, 2, 3, 4]

const map = new Map([['a', 1], ['b', 2]]);
const mapArray = [...map]; // [['a', 1], ['b', 2]]

// NodeList to array
const nodeList = document.querySelectorAll('.items');
const nodeArray = [...nodeList];

// Function that returns spread arrays
function mergeArrays(...arrays) {
    return arrays.reduce((merged, arr) => [...merged, ...arr], []);
}

function createRange(start, end) {
    return [...Array(end - start + 1)].map((_, i) => start + i);
}

// Spread in array methods
const numbers = [1, 2, 3, 4, 5];

function findMax(arr) {
    return Math.max(...arr);
}

function findMin(arr) {
    return Math.min(...arr);
}

// Conditional spreading
function conditionalSpread(base, shouldInclude, additional) {
    return [
        ...base,
        ...(shouldInclude ? additional : [])
    ];
}

// Nested spreading
const nested = [
    [...arr1],
    [...arr2],
    [...arr1, ...arr2]
];

// Spreading with computed values
function spreadWithComputation(arr, multiplier) {
    return [
        ...arr.map(x => x * multiplier),
        ...arr.filter(x => x > 2)
    ];
}

// Spreading in different contexts
class ArrayProcessor {
    constructor(...initialArrays) {
        this.arrays = [...initialArrays];
    }

    addArray(arr) {
        this.arrays = [...this.arrays, arr];
    }

    getAllElements() {
        return this.arrays.reduce((all, arr) => [...all, ...arr], []);
    }

    getCombinedWithSeparator(separator) {
        return this.arrays.reduce((result, arr, index) => [
            ...result,
            ...(index > 0 ? [separator] : []),
            ...arr
        ], []);
    }
}

// Using array spread
const merged = mergeArrays([1, 2], [3, 4], [5, 6]);
const range = createRange(5, 10);
const max = findMax([10, 5, 8, 3, 9]);
const conditional = conditionalSpread([1, 2], true, [3, 4]);

const processor = new ArrayProcessor([1, 2], [3, 4]);
processor.addArray([5, 6]);
const all = processor.getAllElements();
const withSeparator = processor.getCombinedWithSeparator(0);

console.log(combined);      // [1, 2, 3, 4, 5, 6]
console.log(withLiterals);  // [1, 2, 3, 10, 11, 4, 5, 6, 12]
console.log(cloned);        // [1, 2, 3, 4, 5]
console.log(stringArray);   // ['h', 'e', 'l', 'l', 'o']
console.log(setArray);      // [1, 2, 3, 4]
console.log(merged);        // [1, 2, 3, 4, 5, 6]
console.log(range);         // [5, 6, 7, 8, 9, 10]
console.log(max);           // 10
""",
    )

    run_updater(javascript_spread_rest_project, mock_ingestor)

    project_name = javascript_spread_rest_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_functions = [
        f"{project_name}.array_spread.mergeArrays",
        f"{project_name}.array_spread.createRange",
        f"{project_name}.array_spread.findMax",
        f"{project_name}.array_spread.findMin",
        f"{project_name}.array_spread.conditionalSpread",
        f"{project_name}.array_spread.spreadWithComputation",
    ]

    for expected in expected_functions:
        assert expected in created_functions, (
            f"Missing array spread function: {expected}"
        )

    class_calls = get_nodes(mock_ingestor, "Class")

    array_processor_class = [
        call for call in class_calls if "ArrayProcessor" in call[0][1]["qualified_name"]
    ]

    assert len(array_processor_class) >= 1, (
        f"Expected ArrayProcessor class, found {len(array_processor_class)}"
    )


def test_spread_in_objects(
    javascript_spread_rest_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test spread operator usage in objects."""
    test_file = javascript_spread_rest_project / "object_spread.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Object spread patterns

// Basic object spreading
const obj1 = { a: 1, b: 2 };
const obj2 = { c: 3, d: 4 };
const combined = { ...obj1, ...obj2 };

// Spreading with property overrides
const base = { name: 'John', age: 30, city: 'NYC' };
const updated = { ...base, age: 31, country: 'USA' };

// Cloning objects
const original = { x: 1, y: 2, z: 3 };
const cloned = { ...original };
const clonedWithExtra = { ...original, w: 4 };

// Conditional object spreading
function createUser(name, options = {}) {
    return {
        name,
        id: Math.random(),
        ...options,
        created: new Date()
    };
}

function mergeConfigs(defaultConfig, userConfig) {
    return {
        ...defaultConfig,
        ...userConfig,
        // Nested merge for complex properties
        features: {
            ...defaultConfig.features,
            ...userConfig.features
        }
    };
}

// Spreading with computed properties
function createObjectWithComputed(base, key, value) {
    return {
        ...base,
        [key]: value,
        [`${key}_timestamp`]: Date.now()
    };
}

// Spreading in function returns
function getApiConfig(env) {
    const baseConfig = {
        timeout: 5000,
        retries: 3
    };

    return {
        ...baseConfig,
        ...(env === 'development' && { debug: true }),
        ...(env === 'production' && {
            timeout: 10000,
            compression: true
        })
    };
}

// Method definitions with object spread
class ConfigManager {
    constructor(defaultConfig = {}) {
        this.config = { ...defaultConfig };
    }

    updateConfig(updates) {
        this.config = {
            ...this.config,
            ...updates,
            lastModified: new Date()
        };
        return this.config;
    }

    getSection(section, overrides = {}) {
        return {
            ...this.config[section],
            ...overrides
        };
    }

    merge(...configs) {
        return configs.reduce((merged, config) => ({
            ...merged,
            ...config
        }), {});
    }

    // Spread in method parameters and returns
    createState(initial, ...updates) {
        return updates.reduce((state, update) => ({
            ...state,
            ...update
        }), { ...initial });
    }
}

// Spreading with destructuring
function processOptions({ name, age, ...rest }) {
    return {
        user: { name, age },
        metadata: { ...rest }
    };
}

// Spread with arrays as object values
function createArrayConfig(arrays) {
    return {
        ...arrays,
        combined: [...(arrays.arr1 || []), ...(arrays.arr2 || [])],
        lengths: Object.keys(arrays).reduce((acc, key) => ({
            ...acc,
            [key]: arrays[key].length
        }), {})
    };
}

// Nested object spreading
const nestedExample = {
    level1: {
        ...obj1,
        level2: {
            ...obj2,
            level3: {
                ...base
            }
        }
    }
};

// Spreading with null/undefined handling
function safeSpread(obj) {
    return {
        default: 'value',
        ...(obj && typeof obj === 'object' ? obj : {})
    };
}

// Using object spread
const user = createUser('Alice', { age: 25, role: 'admin' });
const config = mergeConfigs(
    { timeout: 5000, features: { cache: true } },
    { retries: 3, features: { logging: true } }
);

const withComputed = createObjectWithComputed(
    { existing: 'value' },
    'dynamic',
    'computed'
);

const apiConfig = getApiConfig('production');

const manager = new ConfigManager({ theme: 'dark', lang: 'en' });
manager.updateConfig({ theme: 'light', notifications: true });
const section = manager.getSection('features', { enabled: true });

const options = processOptions({
    name: 'Test',
    age: 30,
    extra: 'data',
    more: 'info'
});

const arrayConfig = createArrayConfig({
    arr1: [1, 2, 3],
    arr2: [4, 5, 6]
});

console.log(combined);     // { a: 1, b: 2, c: 3, d: 4 }
console.log(updated);      // { name: 'John', age: 31, city: 'NYC', country: 'USA' }
console.log(user);         // { name: 'Alice', id: ..., age: 25, role: 'admin', created: ... }
console.log(config);       // Merged configuration
console.log(apiConfig);    // Environment-specific config
""",
    )

    run_updater(javascript_spread_rest_project, mock_ingestor)

    project_name = javascript_spread_rest_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_functions = [
        f"{project_name}.object_spread.createUser",
        f"{project_name}.object_spread.mergeConfigs",
        f"{project_name}.object_spread.createObjectWithComputed",
        f"{project_name}.object_spread.getApiConfig",
        f"{project_name}.object_spread.processOptions",
        f"{project_name}.object_spread.safeSpread",
    ]

    for expected in expected_functions:
        assert expected in created_functions, (
            f"Missing object spread function: {expected}"
        )

    class_calls = get_nodes(mock_ingestor, "Class")

    config_manager_class = [
        call for call in class_calls if "ConfigManager" in call[0][1]["qualified_name"]
    ]

    assert len(config_manager_class) >= 1, (
        f"Expected ConfigManager class, found {len(config_manager_class)}"
    )


def test_rest_parameters(
    javascript_spread_rest_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test rest parameters in function definitions."""
    test_file = javascript_spread_rest_project / "rest_parameters.js"
    test_file.write_text(
        encoding="utf-8",
        data=r"""
// Rest parameters patterns

// Basic rest parameters
function sum(...numbers) {
    return numbers.reduce((total, num) => total + num, 0);
}

function multiply(multiplier, ...numbers) {
    return numbers.map(num => num * multiplier);
}

// Rest with other parameters
function greet(greeting, ...names) {
    return names.map(name => `${greeting}, ${name}!`).join(' ');
}

function processData(operation, ...data) {
    switch (operation) {
        case 'sum':
            return data.reduce((a, b) => a + b, 0);
        case 'product':
            return data.reduce((a, b) => a * b, 1);
        case 'max':
            return Math.max(...data);
        case 'min':
            return Math.min(...data);
        default:
            return data;
    }
}

// Rest in arrow functions
const combine = (...arrays) => arrays.flat();
const logger = (level, ...messages) => console.log(`[${level}]`, ...messages);

// Rest with destructuring
function handleRequest({ method, url }, ...middleware) {
    console.log(`${method} ${url}`);
    return middleware.reduce((req, fn) => fn(req), { method, url });
}

// Variadic function patterns
function curry(fn, ...args1) {
    return function(...args2) {
        return fn(...args1, ...args2);
    };
}

function compose(...functions) {
    return function(value) {
        return functions.reduceRight((acc, fn) => fn(acc), value);
    };
}

function pipe(...functions) {
    return function(value) {
        return functions.reduce((acc, fn) => fn(acc), value);
    };
}

// Class methods with rest parameters
class Calculator {
    constructor(...initialValues) {
        this.values = [...initialValues];
    }

    add(...numbers) {
        this.values.push(...numbers);
        return this;
    }

    compute(operation, ...operands) {
        const values = operands.length > 0 ? operands : this.values;

        switch (operation) {
            case 'sum':
                return values.reduce((a, b) => a + b, 0);
            case 'average':
                return values.reduce((a, b) => a + b, 0) / values.length;
            case 'range':
                return Math.max(...values) - Math.min(...values);
            default:
                return values;
        }
    }

    static createWithValues(...values) {
        return new Calculator(...values);
    }
}

// Rest with default parameters
function createConfig(name, version = '1.0.0', ...options) {
    return {
        name,
        version,
        options: options.reduce((config, option) => ({
            ...config,
            ...option
        }), {})
    };
}

// Rest in object methods
const utils = {
    merge(...objects) {
        return objects.reduce((merged, obj) => ({ ...merged, ...obj }), {});
    },

    format(template, ...values) {
        return template.replace(/{(\d+)}/g, (match, index) => values[index] || match);
    },

    debounce(fn, delay, ...args) {
        let timeoutId;
        return function(...newArgs) {
            clearTimeout(timeoutId);
            timeoutId = setTimeout(() => fn(...args, ...newArgs), delay);
        };
    }
};

// Rest with async functions
async function fetchMultiple(...urls) {
    const promises = urls.map(url => fetch(url));
    const responses = await Promise.all(promises);
    return Promise.all(responses.map(response => response.json()));
}

async function processSequentially(processor, ...items) {
    const results = [];
    for (const item of items) {
        const result = await processor(item);
        results.push(result);
    }
    return results;
}

// Generator with rest parameters
function* generateSequence(...sequences) {
    for (const sequence of sequences) {
        yield* sequence;
    }
}

function* combine(...generators) {
    for (const generator of generators) {
        yield* generator;
    }
}

// Rest in callbacks
function forEach(array, callback, ...extraArgs) {
    for (let i = 0; i < array.length; i++) {
        callback(array[i], i, array, ...extraArgs);
    }
}

// Using rest parameters
console.log(sum(1, 2, 3, 4, 5)); // 15
console.log(multiply(2, 1, 2, 3, 4)); // [2, 4, 6, 8]
console.log(greet('Hello', 'Alice', 'Bob', 'Charlie'));

const calc = new Calculator(1, 2, 3);
calc.add(4, 5, 6);
console.log(calc.compute('sum')); // 21

const merged = utils.merge({ a: 1 }, { b: 2 }, { c: 3 });
const formatted = utils.format('Hello {0}, you have {1} messages', 'Alice', 5);

// Function composition
const addOne = x => x + 1;
const double = x => x * 2;
const square = x => x * x;

const composed = compose(square, double, addOne);
const piped = pipe(addOne, double, square);

console.log(composed(3)); // ((3 + 1) * 2)^2 = 64
console.log(piped(3));    // ((3 + 1) * 2)^2 = 64

// Using with spread
const numbers = [1, 2, 3, 4, 5];
console.log(sum(...numbers));
console.log(processData('max', ...numbers));

// Variadic generator
const gen1 = function*() { yield 1; yield 2; };
const gen2 = function*() { yield 3; yield 4; };
const combined = combine(gen1(), gen2());

for (const value of combined) {
    console.log(value); // 1, 2, 3, 4
}
""",
    )

    run_updater(javascript_spread_rest_project, mock_ingestor)

    project_name = javascript_spread_rest_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_rest_functions = [
        f"{project_name}.rest_parameters.sum",
        f"{project_name}.rest_parameters.multiply",
        f"{project_name}.rest_parameters.greet",
        f"{project_name}.rest_parameters.processData",
        f"{project_name}.rest_parameters.combine",
        f"{project_name}.rest_parameters.curry",
        f"{project_name}.rest_parameters.compose",
        f"{project_name}.rest_parameters.pipe",
        f"{project_name}.rest_parameters.fetchMultiple",
        f"{project_name}.rest_parameters.generateSequence",
    ]

    found_rest_functions = [
        func for func in expected_rest_functions if func in created_functions
    ]

    assert len(found_rest_functions) >= 7, (
        f"Expected at least 7 rest parameter functions, found {len(found_rest_functions)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    calculator_class = [
        call for call in class_calls if "Calculator" in call[0][1]["qualified_name"]
    ]

    assert len(calculator_class) >= 1, (
        f"Expected Calculator class, found {len(calculator_class)}"
    )


def test_destructuring_with_spread_rest(
    javascript_spread_rest_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test destructuring combined with spread and rest operators."""
    test_file = javascript_spread_rest_project / "destructuring_spread_rest.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Destructuring with spread and rest

// Array destructuring with rest
function processArray(arr) {
    const [first, second, ...rest] = arr;

    return {
        first,
        second,
        rest,
        restLength: rest.length
    };
}

function swapAndRest(arr) {
    const [a, b, ...others] = arr;
    return [b, a, ...others];
}

// Object destructuring with rest
function extractUserInfo(user) {
    const { name, email, ...profile } = user;

    return {
        essential: { name, email },
        additional: profile
    };
}

function processConfig({ host, port, ...options }) {
    return {
        connection: `${host}:${port}`,
        options
    };
}

// Nested destructuring with rest
function processNestedData({ user: { name, age }, ...metadata }) {
    return {
        userName: name,
        userAge: age,
        metadata
    };
}

// Function parameters with destructuring and rest
function createComponent({ type, props = {}, ...config }, ...children) {
    return {
        type,
        props: { ...props, children },
        config
    };
}

function handleEvent({ target, type, ...eventData }, ...handlers) {
    const event = { target, type, data: eventData };
    handlers.forEach(handler => handler(event));
    return event;
}

// Array destructuring with default values and rest
function parseCoordinates([x = 0, y = 0, z = 0, ...extra] = []) {
    return {
        coordinates: { x, y, z },
        extra
    };
}

// Object destructuring with renaming and rest
function transformUser({
    name: userName,
    email: userEmail,
    age: userAge = null,
    ...additionalInfo
}) {
    return {
        user: { userName, userEmail, userAge },
        info: additionalInfo
    };
}

// Complex destructuring patterns
function processApiResponse({
    data: { users, posts, ...resources },
    meta: { total, page, ...pagination },
    ...response
}) {
    return {
        content: { users, posts },
        resources,
        pagination: { total, page, ...pagination },
        response
    };
}

// Destructuring in loops with rest
function groupByProperty(items, property) {
    const grouped = {};

    for (const { [property]: key, ...item } of items) {
        if (!grouped[key]) {
            grouped[key] = [];
        }
        grouped[key].push(item);
    }

    return grouped;
}

// Destructuring assignment with spread
function mergeObjects(obj1, obj2) {
    const { ...merged } = { ...obj1, ...obj2 };
    return merged;
}

// Class methods with destructuring and rest
class DataProcessor {
    constructor({ config = {}, ...options }) {
        this.config = config;
        this.options = options;
    }

    process({ data, transform = x => x, ...processingOptions }, ...middlewares) {
        let processed = data.map(transform);

        // Apply middlewares
        for (const middleware of middlewares) {
            processed = middleware(processed, processingOptions);
        }

        return processed;
    }

    extract({ source, fields, ...extractOptions }) {
        const { [fields[0]]: first, [fields[1]]: second, ...rest } = source;

        return {
            extracted: { first, second },
            remaining: rest,
            options: extractOptions
        };
    }

    // Static method with complex destructuring
    static fromConfig({
        processor: { type, config, ...processorOptions },
        data: { source, format, ...dataOptions },
        ...globalOptions
    }) {
        return new DataProcessor({
            config: { type, ...config },
            source,
            format,
            ...processorOptions,
            ...dataOptions,
            ...globalOptions
        });
    }
}

// Async functions with destructuring and rest
async function fetchAndProcess({ url, method = 'GET', ...fetchOptions }, ...processors) {
    const response = await fetch(url, { method, ...fetchOptions });
    const data = await response.json();

    let result = data;
    for (const processor of processors) {
        result = await processor(result);
    }

    return result;
}

// Generator with destructuring
function* processEntries(entries) {
    for (const [key, value, ...metadata] of entries) {
        yield {
            key,
            value,
            metadata: metadata.length > 0 ? metadata : null
        };
    }
}

// Using destructuring with spread/rest
const array = [1, 2, 3, 4, 5, 6, 7];
const processed = processArray(array);
const swapped = swapAndRest(array);

const user = {
    name: 'Alice',
    email: 'alice@example.com',
    age: 30,
    role: 'admin',
    department: 'Engineering',
    location: 'NYC'
};

const userInfo = extractUserInfo(user);
const transformed = transformUser(user);

const config = {
    host: 'localhost',
    port: 3000,
    ssl: true,
    timeout: 5000,
    retries: 3
};

const configResult = processConfig(config);

const component = createComponent(
    { type: 'div', props: { className: 'container' }, id: 'main' },
    'Hello',
    'World'
);

const processor = new DataProcessor({
    config: { mode: 'fast' },
    cache: true,
    logging: false
});

const coordinates = parseCoordinates([10, 20, 30, 40, 50]);

console.log(processed);     // { first: 1, second: 2, rest: [3,4,5,6,7], restLength: 5 }
console.log(userInfo);      // { essential: {...}, additional: {...} }
console.log(configResult);  // { connection: 'localhost:3000', options: {...} }
console.log(coordinates);   // { coordinates: {x:10, y:20, z:30}, extra: [40,50] }
""",
    )

    run_updater(javascript_spread_rest_project, mock_ingestor)

    project_name = javascript_spread_rest_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_destructuring_functions = [
        f"{project_name}.destructuring_spread_rest.processArray",
        f"{project_name}.destructuring_spread_rest.extractUserInfo",
        f"{project_name}.destructuring_spread_rest.processConfig",
        f"{project_name}.destructuring_spread_rest.createComponent",
        f"{project_name}.destructuring_spread_rest.parseCoordinates",
        f"{project_name}.destructuring_spread_rest.transformUser",
        f"{project_name}.destructuring_spread_rest.processApiResponse",
        f"{project_name}.destructuring_spread_rest.fetchAndProcess",
    ]

    found_destructuring_functions = [
        func for func in expected_destructuring_functions if func in created_functions
    ]

    assert len(found_destructuring_functions) >= 6, (
        f"Expected at least 6 destructuring functions, found {len(found_destructuring_functions)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    data_processor_class = [
        call for call in class_calls if "DataProcessor" in call[0][1]["qualified_name"]
    ]

    assert len(data_processor_class) >= 1, (
        f"Expected DataProcessor class, found {len(data_processor_class)}"
    )


def test_spread_rest_comprehensive(
    javascript_spread_rest_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all spread/rest patterns are covered."""
    test_file = javascript_spread_rest_project / "comprehensive_spread_rest.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every JavaScript spread/rest pattern in one file

// Array spread
const arr1 = [1, 2, 3];
const arr2 = [...arr1, 4, 5];

// Object spread
const obj1 = { a: 1, b: 2 };
const obj2 = { ...obj1, c: 3 };

// Rest parameters
function restFunction(...args) {
    return args.length;
}

// Spread in function calls
function max(a, b, c) {
    return Math.max(a, b, c);
}
const numbers = [10, 5, 8];
const result = max(...numbers);

// Destructuring with rest
function destructureArray([first, ...rest]) {
    return { first, rest };
}

function destructureObject({ name, ...props }) {
    return { name, props };
}

// Combined patterns
function combinePatterns({ data, ...config }, ...transforms) {
    return transforms.reduce((result, transform) => ({
        ...result,
        ...transform({ ...data }, config)
    }), {});
}

// Class with all patterns
class SpreadRestDemo {
    constructor(...values) {
        this.values = [...values];
    }

    add(...newValues) {
        this.values = [...this.values, ...newValues];
    }

    process({ operation, ...options }, ...processors) {
        let result = [...this.values];

        for (const processor of processors) {
            result = processor(result, { operation, ...options });
        }

        return result;
    }

    extract([first, second, ...rest]) {
        return {
            selected: [first, second],
            remaining: [...rest]
        };
    }
}

// Using all patterns
const demo = new SpreadRestDemo(1, 2, 3);
demo.add(4, 5, 6);

const processed = demo.process(
    { operation: 'filter', threshold: 3 },
    (arr, opts) => arr.filter(x => x > opts.threshold),
    (arr, opts) => arr.map(x => x * 2)
);

const extracted = demo.extract([10, 20, 30, 40, 50]);

console.log(restFunction(1, 2, 3, 4)); // 4
console.log(result); // 10
console.log(destructureArray([1, 2, 3, 4])); // { first: 1, rest: [2,3,4] }
console.log(processed); // Filtered and doubled values
console.log(extracted); // { selected: [10,20], remaining: [30,40,50] }

// Complex nested example
const complex = combinePatterns(
    {
        data: { values: [1, 2, 3] },
        multiplier: 2,
        offset: 10
    },
    (data, config) => ({
        multiplied: data.values.map(v => v * config.multiplier)
    }),
    (data, config) => ({
        withOffset: data.multiplied?.map(v => v + config.offset) || []
    })
);

console.log(complex);
""",
    )

    run_updater(javascript_spread_rest_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    calls_relationships = get_relationships(mock_ingestor, "CALLS")
    [c for c in all_relationships if c.args[1] == "DEFINES"]

    comprehensive_calls = [
        call
        for call in calls_relationships
        if "comprehensive_spread_rest" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 5, (
        f"Expected at least 5 comprehensive spread/rest calls, found {len(comprehensive_calls)}"
    )

    function_calls = get_nodes(mock_ingestor, "Function")

    comprehensive_functions = [
        call
        for call in function_calls
        if "comprehensive_spread_rest" in call[0][1]["qualified_name"]
    ]

    assert len(comprehensive_functions) >= 6, (
        f"Expected at least 6 functions in comprehensive test, found {len(comprehensive_functions)}"
    )
