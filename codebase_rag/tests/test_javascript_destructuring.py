from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def javascript_destructuring_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with all destructuring patterns."""
    project_path = temp_repo / "javascript_destructuring_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "utils").mkdir()

    (project_path / "src" / "data.js").write_text(
        encoding="utf-8", data="export const sampleData = { users: [], posts: [] };"
    )
    (project_path / "utils" / "helpers.js").write_text(
        encoding="utf-8", data="export function processArray(arr) { return arr; }"
    )

    return project_path


def test_object_destructuring(
    javascript_destructuring_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test object destructuring patterns."""
    test_file = javascript_destructuring_project / "object_destructuring.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic object destructuring
const user = { name: "Alice", age: 30, email: "alice@example.com" };
const { name, age } = user;
const { email } = user;

// Destructuring with renaming
const { name: userName, age: userAge } = user;
const { email: userEmail } = user;

// Destructuring with default values
const { name: fullName = "Unknown", country = "USA" } = user;
const { phone = "N/A", address = "Not provided" } = user;

// Nested object destructuring
const person = {
    info: { name: "Bob", age: 25 },
    address: { street: "123 Main St", city: "Anytown" },
    preferences: { theme: "dark", notifications: true }
};

const { info: { name: personName, age: personAge } } = person;
const { address: { street, city } } = person;
const { preferences: { theme, notifications } } = person;

// Deep nested destructuring
const data = {
    user: {
        profile: {
            personal: { firstName: "John", lastName: "Doe" },
            contact: { email: "john@example.com", phone: "555-1234" }
        }
    }
};

const {
    user: {
        profile: {
            personal: { firstName, lastName },
            contact: { email: contactEmail, phone: contactPhone }
        }
    }
} = data;

// Destructuring in variable declarations
let { x = 0, y = 0 } = { x: 10 };
var { width = 100, height = 200 } = { width: 50 };

// Rest operator in object destructuring
const config = { host: "localhost", port: 3000, debug: true, ssl: false };
const { host, port, ...otherOptions } = config;

// Destructuring computed properties
const key = "dynamicKey";
const obj = { [key]: "value", other: "data" };
const { [key]: dynamicValue, other } = obj;

// Function using destructured objects
function processUser({ name, age, email = "no-email" }) {
    return `User: ${name} (${age}) - ${email}`;
}

function createReport({ title, data, options = {} }) {
    const { format = "json", includeMetadata = true } = options;
    return { title, data, format, includeMetadata };
}

// Destructuring in function parameters with nested objects
function handleUserData({
    user: { name, age },
    settings: { theme = "light", lang = "en" } = {}
}) {
    return { name, age, theme, lang };
}

// Destructuring in arrow functions
const getUserInfo = ({ name, email }) => `${name}: ${email}`;
const getCoordinates = ({ x = 0, y = 0, z = 0 } = {}) => [x, y, z];

// Using destructured values
const userInfo = processUser({ name: "Charlie", age: 35 });
const report = createReport({
    title: "Monthly Report",
    data: [1, 2, 3],
    options: { format: "pdf" }
});

const userDataResult = handleUserData({
    user: { name: "Dave", age: 28 },
    settings: { theme: "dark" }
});

const info = getUserInfo({ name: "Eve", email: "eve@example.com" });
const coords = getCoordinates({ x: 10, y: 20 });

// Destructuring in loops
const users = [
    { id: 1, name: "User1", status: "active" },
    { id: 2, name: "User2", status: "inactive" }
];

for (const { id, name, status } of users) {
    console.log(`${id}: ${name} (${status})`);
}

users.forEach(({ name, status }) => {
    console.log(`${name} is ${status}`);
});

// Destructuring in try-catch
try {
    const response = { data: "result", error: null };
    const { data, error } = response;
    if (error) throw error;
    console.log(data);
} catch ({ message = "Unknown error" }) {
    console.error(message);
}
""",
    )

    run_updater(javascript_destructuring_project, mock_ingestor)

    project_name = javascript_destructuring_project.name

    expected_functions = [
        f"{project_name}.object_destructuring.processUser",
        f"{project_name}.object_destructuring.createReport",
        f"{project_name}.object_destructuring.handleUserData",
        f"{project_name}.object_destructuring.getUserInfo",
        f"{project_name}.object_destructuring.getCoordinates",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    destructuring_calls = [
        call for call in call_relationships if "object_destructuring" in call.args[0][2]
    ]

    assert len(destructuring_calls) >= 4, (
        f"Expected at least 4 function calls in destructuring code, found {len(destructuring_calls)}"
    )


def test_array_destructuring(
    javascript_destructuring_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test array destructuring patterns."""
    test_file = javascript_destructuring_project / "array_destructuring.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic array destructuring
const numbers = [1, 2, 3, 4, 5];
const [first, second] = numbers;
const [, , third] = numbers; // Skip elements

// Destructuring with default values
const [a = 0, b = 0, c = 0] = [10, 20];
const [x, y, z = 100] = [1, 2];

// Rest operator in array destructuring
const [head, ...tail] = numbers;
const [start, middle, ...end] = [1, 2, 3, 4, 5, 6];

// Nested array destructuring
const matrix = [[1, 2], [3, 4], [5, 6]];
const [[firstRow1, firstRow2], [secondRow1, secondRow2]] = matrix;

// Swapping variables
let var1 = "first";
let var2 = "second";
[var1, var2] = [var2, var1];

// Destructuring function returns
function getCoordinates() {
    return [10, 20, 30];
}

function getMinMax(arr) {
    return [Math.min(...arr), Math.max(...arr)];
}

function getNameAndAge() {
    return ["Alice", 30];
}

const [x1, y1, z1] = getCoordinates();
const [min, max] = getMinMax([5, 1, 9, 3]);
const [name, age] = getNameAndAge();

// Array destructuring in function parameters
function processCoordinates([x, y, z = 0]) {
    return { x, y, z };
}

function calculateDistance([x1, y1], [x2, y2]) {
    return Math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2);
}

function sumFirstTwo([first, second, ...rest]) {
    return first + second;
}

// Array destructuring with objects
const points = [
    { x: 1, y: 2 },
    { x: 3, y: 4 },
    { x: 5, y: 6 }
];

const [{ x: point1X, y: point1Y }, { x: point2X, y: point2Y }] = points;

// Destructuring in loops
const coordinates = [[1, 2], [3, 4], [5, 6]];

for (const [x, y] of coordinates) {
    console.log(`Point: (${x}, ${y})`);
}

coordinates.forEach(([x, y]) => {
    console.log(`Coordinate: ${x}, ${y}`);
});

// Destructuring Promise results
async function fetchUserData() {
    return ["John", "john@example.com", 25];
}

async function processData() {
    const [username, email, userAge] = await fetchUserData();
    return { username, email, userAge };
}

// Using array destructuring functions
const coords = processCoordinates([5, 10, 15]);
const distance = calculateDistance([0, 0], [3, 4]);
const sum = sumFirstTwo([10, 20, 30, 40]);

// Complex array destructuring
const data = [
    [1, [2, 3]],
    [4, [5, 6]],
    [7, [8, 9]]
];

const [[first1, [first2, first3]], [second1, [second2, second3]]] = data;

// Array destructuring with function calls
function getArray() {
    return [1, 2, 3];
}

function processArray(arr) {
    const [firstElement, ...otherElements] = arr;
    return { firstElement, otherElements };
}

const [val1, val2, val3] = getArray();
const result = processArray([10, 20, 30, 40]);
""",
    )

    run_updater(javascript_destructuring_project, mock_ingestor)

    project_name = javascript_destructuring_project.name

    expected_functions = [
        f"{project_name}.array_destructuring.getCoordinates",
        f"{project_name}.array_destructuring.getMinMax",
        f"{project_name}.array_destructuring.getNameAndAge",
        f"{project_name}.array_destructuring.processCoordinates",
        f"{project_name}.array_destructuring.calculateDistance",
        f"{project_name}.array_destructuring.sumFirstTwo",
        f"{project_name}.array_destructuring.fetchUserData",
        f"{project_name}.array_destructuring.processData",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    array_destructuring_calls = [
        call for call in call_relationships if "array_destructuring" in call.args[0][2]
    ]

    assert len(array_destructuring_calls) >= 5, (
        f"Expected at least 5 function calls in array destructuring code, found {len(array_destructuring_calls)}"
    )


def test_parameter_destructuring(
    javascript_destructuring_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test destructuring in function parameters."""
    test_file = javascript_destructuring_project / "parameter_destructuring.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Object parameter destructuring
function createUser({ name, email, age = 18, role = "user" }) {
    return { name, email, age, role, id: Math.random() };
}

function updateProfile({ userId, updates: { name, email, preferences = {} } }) {
    return { userId, name, email, preferences };
}

function configureApp({
    api: { baseURL, timeout = 5000 } = {},
    ui: { theme = "light", language = "en" } = {}
}) {
    return { baseURL, timeout, theme, language };
}

// Array parameter destructuring
function addVectors([x1, y1], [x2, y2]) {
    return [x1 + x2, y1 + y2];
}

function processMatrix([[a, b], [c, d]]) {
    return { determinant: a * d - b * c, trace: a + d };
}

function calculateStats([first, ...rest]) {
    const sum = first + rest.reduce((acc, val) => acc + val, 0);
    return { count: rest.length + 1, sum, average: sum / (rest.length + 1) };
}

// Mixed destructuring patterns
function handleRequest({
    method = "GET",
    url,
    headers = {},
    body,
    params: [endpoint, ...queryParams] = []
}) {
    return { method, url, headers, body, endpoint, queryParams };
}

function processData({
    data: [firstItem, ...otherItems],
    meta: { total, page = 1 } = {}
}) {
    return { firstItem, otherItems, total, page };
}

// Destructuring with rest parameters
function combineObjects(target, ...sources) {
    return sources.reduce((acc, { ...source }) => ({ ...acc, ...source }), target);
}

function mergeArrays([...first], [...second], [...third]) {
    return [...first, ...second, ...third];
}

// Arrow function parameter destructuring
const formatUser = ({ name, email, isActive = true }) =>
    `${name} (${email}) - ${isActive ? "Active" : "Inactive"}`;

const calculateArea = ({ width, height, unit = "px" }) =>
    `${width * height} ${unit}²`;

const getFullName = ({ firstName, lastName, middleName = "" }) =>
    middleName ? `${firstName} ${middleName} ${lastName}` : `${firstName} ${lastName}`;

// Destructuring in async functions
async function fetchUserProfile({ userId, include: { posts = false, comments = false } = {} }) {
    const profile = await getUserData(userId);

    if (posts) {
        profile.posts = await getUserPosts(userId);
    }

    if (comments) {
        profile.comments = await getUserComments(userId);
    }

    return profile;
}

async function saveUserData({ user: { id, ...userData }, options: { validate = true } = {} }) {
    if (validate) {
        await validateUser(userData);
    }

    return await updateUser(id, userData);
}

// Destructuring with default functions
function processFile({
    filename,
    processor = (content) => content.toUpperCase(),
    validator = (file) => file.size > 0
}) {
    if (!validator({ size: filename.length })) {
        throw new Error("Invalid file");
    }

    return processor(filename);
}

// Class methods with destructuring parameters
class DataProcessor {
    process({ data, options: { format = "json", compress = false } = {} }) {
        let result = this.formatData(data, format);

        if (compress) {
            result = this.compress(result);
        }

        return result;
    }

    formatData(data, format) {
        return format === "json" ? JSON.stringify(data) : data.toString();
    }

    compress(data) {
        return data; // Mock compression
    }

    static create({ type = "default", config = {} }) {
        return new DataProcessor();
    }
}

// Using parameter destructuring functions
const user = createUser({ name: "Alice", email: "alice@example.com", age: 25 });
const profile = updateProfile({
    userId: 123,
    updates: { name: "Alice Smith", email: "alice.smith@example.com" }
});

const appConfig = configureApp({
    api: { baseURL: "https://api.example.com", timeout: 3000 },
    ui: { theme: "dark" }
});

const vector = addVectors([1, 2], [3, 4]);
const matrix = processMatrix([[1, 2], [3, 4]]);
const stats = calculateStats([10, 20, 30, 40, 50]);

const formatted = formatUser({ name: "Bob", email: "bob@example.com" });
const area = calculateArea({ width: 100, height: 200, unit: "cm" });
const fullName = getFullName({ firstName: "John", lastName: "Doe" });

const processor = new DataProcessor();
const processed = processor.process({
    data: { key: "value" },
    options: { format: "json", compress: true }
});

// Helper functions (mocked)
async function getUserData(id) { return { id, name: "User" }; }
async function getUserPosts(id) { return []; }
async function getUserComments(id) { return []; }
async function validateUser(data) { return true; }
async function updateUser(id, data) { return { id, ...data }; }
""",
    )

    run_updater(javascript_destructuring_project, mock_ingestor)

    project_name = javascript_destructuring_project.name

    expected_functions = [
        f"{project_name}.parameter_destructuring.createUser",
        f"{project_name}.parameter_destructuring.updateProfile",
        f"{project_name}.parameter_destructuring.configureApp",
        f"{project_name}.parameter_destructuring.addVectors",
        f"{project_name}.parameter_destructuring.processMatrix",
        f"{project_name}.parameter_destructuring.handleRequest",
        f"{project_name}.parameter_destructuring.processData",
        f"{project_name}.parameter_destructuring.formatUser",
        f"{project_name}.parameter_destructuring.fetchUserProfile",
        f"{project_name}.parameter_destructuring.processFile",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )

    expected_classes = [
        f"{project_name}.parameter_destructuring.DataProcessor",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 class with destructuring methods, found {len(found_classes)}"
    )


def test_destructuring_with_imports(
    javascript_destructuring_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test destructuring combined with import statements."""
    test_file = javascript_destructuring_project / "destructuring_imports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Destructuring imports
import { useState, useEffect, useCallback } from 'react';
import { map, filter, reduce } from 'lodash';
import { createStore, combineReducers } from 'redux';

// Import with destructuring and renaming
import {
    fetchData as getData,
    saveData as storeData,
    validateInput as validate
} from './api';

// Import default and destructured
import React, { Component, PureComponent } from 'react';
import axios, { get, post } from 'axios';

// Functions using destructured imports
function UserComponent({ userId, onUpdate }) {
    const [user, setUser] = useState(null);
    const [loading, setLoading] = useEffect(false);

    const fetchUser = useCallback(async () => {
        setLoading(true);
        try {
            const userData = await getData(`/users/${userId}`);
            setUser(userData);
        } catch (error) {
            console.error(error);
        } finally {
            setLoading(false);
        }
    }, [userId]);

    useEffect(() => {
        fetchUser();
    }, [fetchUser]);

    return { user, loading, fetchUser };
}

function DataProcessor({ data, filters }) {
    const filtered = filter(data, filters.predicate);
    const mapped = map(filtered, filters.transform);
    const result = reduce(mapped, filters.accumulator, filters.initial);

    return result;
}

// Class using destructured imports
class ApiService extends Component {
    async fetchData(endpoint) {
        const response = await get(endpoint);
        return response.data;
    }

    async saveData(endpoint, data) {
        if (!validate(data)) {
            throw new Error('Invalid data');
        }

        const response = await post(endpoint, data);
        return response.data;
    }
}

// Using imported functions with destructuring
function processApiResponse({ data, status, headers }) {
    if (status >= 200 && status < 300) {
        return { success: true, data };
    }

    return { success: false, error: data.message };
}

// Destructuring with dynamic imports
async function loadModule(moduleName) {
    const { default: defaultExport, ...namedExports } = await import(moduleName);
    return { defaultExport, namedExports };
}

// Using destructured values
const userComponent = UserComponent({ userId: 123, onUpdate: () => {} });
const processedData = DataProcessor({
    data: [1, 2, 3, 4, 5],
    filters: {
        predicate: x => x > 2,
        transform: x => x * 2,
        accumulator: (acc, val) => acc + val,
        initial: 0
    }
});

const apiService = new ApiService();
const response = processApiResponse({
    data: { message: "Success" },
    status: 200,
    headers: {}
});
""",
    )

    run_updater(javascript_destructuring_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    destructuring_imports = [
        call
        for call in import_relationships
        if "destructuring_imports" in call.args[0][2]
    ]

    assert len(destructuring_imports) >= 5, (
        f"Expected at least 5 destructuring imports, found {len(destructuring_imports)}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    destructuring_calls = [
        call
        for call in call_relationships
        if "destructuring_imports" in call.args[0][2]
    ]

    assert len(destructuring_calls) >= 3, (
        f"Expected at least 3 function calls in destructuring import code, found {len(destructuring_calls)}"
    )


def test_destructuring_comprehensive(
    javascript_destructuring_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all destructuring patterns create proper relationships."""
    test_file = javascript_destructuring_project / "comprehensive_destructuring.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every JavaScript destructuring pattern in one file

// Object destructuring
const user = { name: "Alice", age: 30, email: "alice@example.com" };
const { name, age } = user;
const { email: userEmail } = user;

// Array destructuring
const coordinates = [10, 20, 30];
const [x, y, z] = coordinates;
const [first, ...rest] = [1, 2, 3, 4, 5];

// Function parameter destructuring
function processUser({ name, age, email = "no-email" }) {
    return `${name} (${age}) - ${email}`;
}

function addPoints([x1, y1], [x2, y2]) {
    return [x1 + x2, y1 + y2];
}

// Nested destructuring
const data = {
    users: [
        { id: 1, profile: { name: "John", settings: { theme: "dark" } } }
    ]
};

const { users: [{ profile: { name: userName, settings: { theme } } }] } = data;

// Destructuring with default values
function configure({
    host = "localhost",
    port = 3000,
    options: { ssl = false, debug = true } = {}
}) {
    return { host, port, ssl, debug };
}

// Using all destructuring patterns
const userInfo = processUser({ name: "Bob", age: 25 });
const point = addPoints([1, 2], [3, 4]);
const config = configure({ host: "example.com", options: { ssl: true } });

// Destructuring in loops
const items = [{ id: 1, name: "Item1" }, { id: 2, name: "Item2" }];
for (const { id, name } of items) {
    console.log(`${id}: ${name}`);
}

// Class with destructuring
class DataHandler {
    process({ data, options: { format = "json" } = {} }) {
        return format === "json" ? JSON.stringify(data) : data;
    }
}

const handler = new DataHandler();
const result = handler.process({ data: { key: "value" }, options: { format: "json" } });

// Arrow function with destructuring
const formatData = ({ title, content, meta: { author = "Unknown" } = {} }) =>
    `${title} by ${author}: ${content}`;

const formatted = formatData({
    title: "Article",
    content: "Content",
    meta: { author: "Alice" }
});
""",
    )

    run_updater(javascript_destructuring_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_destructuring" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 3, (
        f"Expected at least 3 comprehensive destructuring calls, found {len(comprehensive_calls)}"
    )

    for relationship in comprehensive_calls:
        assert len(relationship.args) == 3, "Call relationship should have 3 args"
        assert relationship.args[1] == "CALLS", "Second arg should be 'CALLS'"

        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        assert "comprehensive_destructuring" in source_module, (
            f"Source module should contain test file name: {source_module}"
        )

        assert isinstance(target_module, str) and target_module, (
            f"Target should be non-empty string: {target_module}"
        )

    assert defines_relationships, "Should still have DEFINES relationships"
