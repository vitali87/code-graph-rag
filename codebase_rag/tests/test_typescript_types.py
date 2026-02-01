from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def typescript_types_project(temp_repo: Path) -> Path:
    """Create a comprehensive TypeScript project with all type patterns."""
    project_path = temp_repo / "typescript_types_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "types").mkdir()
    (project_path / "models").mkdir()

    (project_path / "src" / "base.ts").write_text(
        encoding="utf-8", data="export interface BaseInterface {}"
    )
    (project_path / "types" / "common.ts").write_text(
        encoding="utf-8", data="export type ID = string | number;"
    )

    return project_path


def test_basic_type_annotations(
    typescript_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic type annotations on variables, parameters, and return types."""
    test_file = typescript_types_project / "basic_types.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic primitive type annotations
let name: string = "John";
let age: number = 30;
let isActive: boolean = true;
let items: number[] = [1, 2, 3];
let tuple: [string, number] = ["hello", 42];

// Object type annotations
let user: { name: string; age: number; email?: string } = {
    name: "Alice",
    age: 25
};

// Function type annotations
function greet(name: string): string {
    return `Hello, ${name}!`;
}

function add(a: number, b: number): number {
    return a + b;
}

function processUser(user: { id: number; name: string }): void {
    console.log(`Processing user ${user.name}`);
}

// Arrow function type annotations
const multiply = (a: number, b: number): number => a * b;
const formatName = (first: string, last?: string): string => {
    return last ? `${first} ${last}` : first;
};

// Array and object types
const numbers: number[] = [1, 2, 3, 4, 5];
const users: Array<{ id: number; name: string }> = [
    { id: 1, name: "Alice" },
    { id: 2, name: "Bob" }
];

// Union types
let value: string | number = "hello";
value = 42;

let status: "pending" | "approved" | "rejected" = "pending";

// Function types as variables
let calculator: (a: number, b: number) => number;
calculator = add;
calculator = multiply;

// Optional and default parameters
function createUser(name: string, age: number = 18, email?: string): { name: string; age: number; email?: string } {
    return { name, age, email };
}

// Rest parameters with types
function sum(...numbers: number[]): number {
    return numbers.reduce((acc, num) => acc + num, 0);
}

// Type assertions
let someValue: unknown = "this is a string";
let strLength: number = (someValue as string).length;
let altStrLength: number = (<string>someValue).length;

// Using typed functions
const greeting = greet("World");
const result = add(5, 3);
const product = multiply(4, 7);
const newUser = createUser("Charlie", 30, "charlie@example.com");
const total = sum(1, 2, 3, 4, 5);
""",
    )

    run_updater(typescript_types_project, mock_ingestor)

    project_name = typescript_types_project.name

    expected_functions = [
        f"{project_name}.basic_types.greet",
        f"{project_name}.basic_types.add",
        f"{project_name}.basic_types.processUser",
        f"{project_name}.basic_types.multiply",
        f"{project_name}.basic_types.formatName",
        f"{project_name}.basic_types.createUser",
        f"{project_name}.basic_types.sum",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    for expected_qn in expected_functions:
        assert expected_qn in created_functions, (
            f"Missing typed function: {expected_qn}"
        )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    function_calls_found = [
        call for call in call_relationships if "basic_types" in call.args[0][2]
    ]

    assert len(function_calls_found) >= 5, (
        f"Expected at least 5 function calls in typed code, found {len(function_calls_found)}"
    )


def test_interfaces_and_type_aliases(
    typescript_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test interface definitions and type aliases."""
    test_file = typescript_types_project / "interfaces_types.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic interface definitions
interface User {
    id: number;
    name: string;
    email: string;
    age?: number;
    readonly createdAt: Date;
}

interface Product {
    id: string;
    name: string;
    price: number;
    description?: string;
    categories: string[];
    inStock: boolean;
}

// Interface inheritance
interface Admin extends User {
    permissions: string[];
    role: "admin" | "super-admin";
}

interface Customer extends User {
    orders: Order[];
    preferredPayment: PaymentMethod;
}

// Multiple interface inheritance
interface Moderator extends User, Admin {
    moderatedSections: string[];
}

// Interface with methods
interface Calculator {
    add(a: number, b: number): number;
    subtract(a: number, b: number): number;
    multiply(a: number, b: number): number;
    divide(a: number, b: number): number;
}

// Interface with function type properties
interface EventHandler {
    onClick: (event: MouseEvent) => void;
    onSubmit: (data: FormData) => Promise<void>;
    onError: (error: Error) => void;
}

// Basic type aliases
type ID = string | number;
type Status = "pending" | "approved" | "rejected" | "cancelled";
type UserRole = "user" | "admin" | "moderator";

// Complex type aliases
type UserWithRole = User & { role: UserRole };
type PartialUser = Partial<User>;
type RequiredUser = Required<User>;

// Union and intersection types
type StringOrNumber = string | number;
type UserOrAdmin = User | Admin;
type UserAndAdmin = User & Admin;

// Function type aliases
type EventCallback = (event: Event) => void;
type AsyncHandler<T> = (data: T) => Promise<void>;
type Validator<T> = (value: T) => boolean;

// Mapped types
type ReadonlyUser = {
    readonly [K in keyof User]: User[K];
};

type OptionalUser = {
    [K in keyof User]?: User[K];
};

// Conditional types
type NonNullable<T> = T extends null | undefined ? never : T;
type ReturnType<T> = T extends (...args: any[]) => infer R ? R : any;

// Type guards
function isUser(obj: any): obj is User {
    return obj && typeof obj.id === 'number' && typeof obj.name === 'string';
}

function isString(value: any): value is string {
    return typeof value === 'string';
}

// Functions using interfaces and types
function createUser(userData: Partial<User>): User {
    return {
        id: Math.random(),
        name: userData.name || "Unknown",
        email: userData.email || "",
        createdAt: new Date()
    };
}

function processOrder(user: Customer, product: Product): Order {
    return {
        id: `order-${Date.now()}`,
        userId: user.id,
        productId: product.id,
        quantity: 1,
        status: "pending"
    };
}

function validateAdmin(user: User): user is Admin {
    return 'permissions' in user && 'role' in user;
}

// Classes implementing interfaces
class BasicCalculator implements Calculator {
    add(a: number, b: number): number {
        return a + b;
    }

    subtract(a: number, b: number): number {
        return a - b;
    }

    multiply(a: number, b: number): number {
        return a * b;
    }

    divide(a: number, b: number): number {
        if (b === 0) throw new Error("Division by zero");
        return a / b;
    }
}

// Using interfaces and types
const user: User = createUser({ name: "Alice", email: "alice@example.com" });
const admin: Admin = {
    ...user,
    permissions: ["read", "write", "delete"],
    role: "admin"
};

const calc = new BasicCalculator();
const sum = calc.add(5, 3);

if (isUser(user)) {
    console.log(user.name);
}

if (validateAdmin(admin)) {
    console.log(admin.permissions);
}

// Additional interface for testing
interface Order {
    id: string;
    userId: number;
    productId: string;
    quantity: number;
    status: Status;
}

interface PaymentMethod {
    type: "credit" | "debit" | "paypal";
    details: string;
}
""",
    )

    run_updater(typescript_types_project, mock_ingestor)

    project_name = typescript_types_project.name

    interface_nodes = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] in ["Class", "Interface"]
        and "interfaces_types" in call[0][1].get("qualified_name", "")
    ]

    assert len(interface_nodes) >= 3, (
        f"Expected at least 3 interface/type definitions, found {len(interface_nodes)}"
    )

    expected_functions = [
        f"{project_name}.interfaces_types.isUser",
        f"{project_name}.interfaces_types.isString",
        f"{project_name}.interfaces_types.createUser",
        f"{project_name}.interfaces_types.processOrder",
        f"{project_name}.interfaces_types.validateAdmin",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )

    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    interface_inheritance = [
        call for call in relationship_calls if "interfaces_types" in call[0][0][2]
    ]

    assert len(interface_inheritance) >= 1, (
        f"Expected at least 1 interface inheritance relationship, found {len(interface_inheritance)}"
    )


def test_generic_types(
    typescript_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test generic types, constraints, and generic functions/classes."""
    test_file = typescript_types_project / "generics.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Generic interfaces
interface Container<T> {
    value: T;
    getValue(): T;
    setValue(value: T): void;
}

interface Pair<T, U> {
    first: T;
    second: U;
    swap(): Pair<U, T>;
}

interface Repository<T> {
    findById(id: string): Promise<T | null>;
    save(entity: T): Promise<T>;
    delete(id: string): Promise<void>;
    findAll(): Promise<T[]>;
}

// Generic type aliases
type Result<T, E = Error> = {
    success: true;
    data: T;
} | {
    success: false;
    error: E;
};

type ApiResponse<T> = {
    status: number;
    message: string;
    data?: T;
};

type EventMap<T> = {
    [K in keyof T]: (payload: T[K]) => void;
};

// Generic functions
function identity<T>(value: T): T {
    return value;
}

function createArray<T>(length: number, value: T): T[] {
    return Array(length).fill(value);
}

function map<T, U>(array: T[], mapper: (item: T) => U): U[] {
    return array.map(mapper);
}

function filter<T>(array: T[], predicate: (item: T) => boolean): T[] {
    return array.filter(predicate);
}

// Generic functions with constraints
function getProperty<T, K extends keyof T>(obj: T, key: K): T[K] {
    return obj[key];
}

function merge<T extends object, U extends object>(obj1: T, obj2: U): T & U {
    return { ...obj1, ...obj2 };
}

function stringifyKeys<T extends Record<string, any>>(obj: T): Record<keyof T, string> {
    const result = {} as Record<keyof T, string>;
    for (const key in obj) {
        result[key] = String(obj[key]);
    }
    return result;
}

// Generic classes
class GenericContainer<T> implements Container<T> {
    constructor(private _value: T) {}

    getValue(): T {
        return this._value;
    }

    setValue(value: T): void {
        this._value = value;
    }

    get value(): T {
        return this._value;
    }
}

class Cache<K, V> {
    private storage = new Map<K, V>();

    get(key: K): V | undefined {
        return this.storage.get(key);
    }

    set(key: K, value: V): void {
        this.storage.set(key, value);
    }

    has(key: K): boolean {
        return this.storage.has(key);
    }

    delete(key: K): boolean {
        return this.storage.delete(key);
    }

    clear(): void {
        this.storage.clear();
    }

    size(): number {
        return this.storage.size;
    }
}

// Generic class with constraints
class Serializer<T extends { toString(): string }> {
    serialize(value: T): string {
        return value.toString();
    }

    serializeArray(values: T[]): string[] {
        return values.map(v => v.toString());
    }
}

// Generic class inheritance
class StringContainer extends GenericContainer<string> {
    constructor(value: string) {
        super(value);
    }

    toUpperCase(): string {
        return this.getValue().toUpperCase();
    }

    toLowerCase(): string {
        return this.getValue().toLowerCase();
    }
}

// Conditional types with generics
type Flatten<T> = T extends (infer U)[] ? U : T;
type Awaited<T> = T extends Promise<infer U> ? U : T;

// Utility type implementations
type MyPick<T, K extends keyof T> = {
    [P in K]: T[P];
};

type MyOmit<T, K extends keyof T> = {
    [P in Exclude<keyof T, K>]: T[P];
};

// Advanced generic patterns
interface State<T> {
    data: T;
    loading: boolean;
    error: string | null;
}

type AsyncState<T> = {
    [K in keyof State<T>]: State<T>[K];
};

// Functions using generics
function createResult<T>(data: T): Result<T> {
    return { success: true, data };
}

function createError<E = Error>(error: E): Result<never, E> {
    return { success: false, error };
}

async function fetchData<T>(url: string): Promise<ApiResponse<T>> {
    try {
        const response = await fetch(url);
        const data = await response.json();
        return {
            status: response.status,
            message: "Success",
            data
        };
    } catch (error) {
        return {
            status: 500,
            message: error instanceof Error ? error.message : "Unknown error"
        };
    }
}

// Using generic types and functions
const stringContainer = new GenericContainer<string>("Hello");
const numberContainer = new GenericContainer<number>(42);
const cache = new Cache<string, number>();

const numbers = createArray<number>(5, 0);
const strings = map(numbers, n => n.toString());
const evens = filter(numbers, n => n % 2 === 0);

const userProp = getProperty({ name: "Alice", age: 30 }, "name");
const merged = merge({ a: 1 }, { b: 2 });

const stringValue = stringContainer.getValue();
const upperValue = new StringContainer("hello").toUpperCase();

cache.set("key1", 100);
const cachedValue = cache.get("key1");

const result = createResult("success");
const errorResult = createError(new Error("Failed"));
""",
    )

    run_updater(typescript_types_project, mock_ingestor)

    project_name = typescript_types_project.name

    expected_generic_functions = [
        f"{project_name}.generics.identity",
        f"{project_name}.generics.createArray",
        f"{project_name}.generics.map",
        f"{project_name}.generics.filter",
        f"{project_name}.generics.getProperty",
        f"{project_name}.generics.merge",
        f"{project_name}.generics.createResult",
        f"{project_name}.generics.fetchData",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_generic_functions = [
        func for func in expected_generic_functions if func in created_functions
    ]
    assert len(found_generic_functions) >= 5, (
        f"Expected at least 5 generic functions, found {len(found_generic_functions)}"
    )

    expected_generic_classes = [
        f"{project_name}.generics.GenericContainer",
        f"{project_name}.generics.Cache",
        f"{project_name}.generics.Serializer",
        f"{project_name}.generics.StringContainer",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_generic_classes = [
        cls for cls in expected_generic_classes if cls in created_classes
    ]
    assert len(found_generic_classes) >= 3, (
        f"Expected at least 3 generic classes, found {len(found_generic_classes)}"
    )


def test_utility_types(
    typescript_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript utility types like Partial, Required, Pick, Omit, etc."""
    test_file = typescript_types_project / "utility_types.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Base interface for utility type testing
interface User {
    id: number;
    name: string;
    email: string;
    age?: number;
    address?: {
        street: string;
        city: string;
        country: string;
    };
    preferences: {
        theme: "light" | "dark";
        notifications: boolean;
    };
}

// Utility type usage
type PartialUser = Partial<User>;
type RequiredUser = Required<User>;
type UserIdAndName = Pick<User, "id" | "name">;
type UserWithoutId = Omit<User, "id">;
type UserKeys = keyof User;

// More utility types
type UserRecord = Record<string, User>;
type UserArray = Array<User>;
type UserOrNull = User | null;
type NonNullableUser = NonNullable<UserOrNull>;

// Function utility types
type UserValidator = (user: User) => boolean;
type ValidatorParameters = Parameters<UserValidator>;
type ValidatorReturnType = ReturnType<UserValidator>;

// Template literal types
type EmailDomains = "gmail.com" | "yahoo.com" | "hotmail.com";
type Email<T extends string> = `${string}@${T}`;
type GmailEmail = Email<"gmail.com">;

// Conditional utility types
type IsArray<T> = T extends any[] ? true : false;
type ArrayElement<T> = T extends (infer U)[] ? U : never;
type FunctionReturnType<T> = T extends (...args: any[]) => infer R ? R : never;

// Mapped types
type ReadonlyUser = {
    readonly [K in keyof User]: User[K];
};

type OptionalUser = {
    [K in keyof User]?: User[K];
};

type StringifiedUser = {
    [K in keyof User]: string;
};

// Advanced mapped types
type NullableProperties<T> = {
    [K in keyof T]: T[K] | null;
};

type FunctionProperties<T> = {
    [K in keyof T]: T[K] extends Function ? T[K] : never;
};

type NonFunctionProperties<T> = {
    [K in keyof T]: T[K] extends Function ? never : T[K];
};

// Functions using utility types
function updateUser(user: User, updates: PartialUser): User {
    return { ...user, ...updates };
}

function createUser(userData: RequiredUser): User {
    return userData;
}

function getUserBasicInfo(user: User): UserIdAndName {
    return { id: user.id, name: user.name };
}

function createUserRecord(): UserRecord {
    return {};
}

function validateUser(user: User): user is Required<User> {
    return !!(user.id && user.name && user.email && user.age && user.address);
}

// Type assertion functions
function assertIsUser(value: unknown): asserts value is User {
    if (!value || typeof value !== 'object') {
        throw new Error('Not a user object');
    }
}

function assertIsString(value: unknown): asserts value is string {
    if (typeof value !== 'string') {
        throw new Error('Not a string');
    }
}

// Generic utility type functions
function deepPartial<T>(obj: T): Partial<T> {
    const result: Partial<T> = {};
    for (const key in obj) {
        if (obj[key] !== null && typeof obj[key] === 'object') {
            result[key] = deepPartial(obj[key]) as T[Extract<keyof T, string>];
        } else {
            result[key] = obj[key];
        }
    }
    return result;
}

function pick<T, K extends keyof T>(obj: T, ...keys: K[]): Pick<T, K> {
    const result = {} as Pick<T, K>;
    for (const key of keys) {
        result[key] = obj[key];
    }
    return result;
}

function omit<T, K extends keyof T>(obj: T, ...keys: K[]): Omit<T, K> {
    const result = { ...obj };
    for (const key of keys) {
        delete result[key];
    }
    return result;
}

// Classes using utility types
class UserService {
    private users: UserRecord = {};

    create(userData: RequiredUser): User {
        const user = createUser(userData);
        this.users[user.id] = user;
        return user;
    }

    update(id: number, updates: PartialUser): User | null {
        const user = this.users[id];
        if (!user) return null;

        const updatedUser = updateUser(user, updates);
        this.users[id] = updatedUser;
        return updatedUser;
    }

    getBasicInfo(id: number): UserIdAndName | null {
        const user = this.users[id];
        return user ? getUserBasicInfo(user) : null;
    }

    getAll(): User[] {
        return Object.values(this.users);
    }
}

// Using utility types
const partialUser: PartialUser = { name: "Alice" };
const requiredUser: RequiredUser = {
    id: 1,
    name: "Bob",
    email: "bob@example.com",
    age: 30,
    address: {
        street: "123 Main St",
        city: "Anytown",
        country: "USA"
    },
    preferences: {
        theme: "light",
        notifications: true
    }
};

const userService = new UserService();
const createdUser = userService.create(requiredUser);
const updatedUser = userService.update(1, { name: "Robert" });
const basicInfo = userService.getBasicInfo(1);

const picked = pick(requiredUser, "id", "name");
const omitted = omit(requiredUser, "id");
const partial = deepPartial(requiredUser);
""",
    )

    run_updater(typescript_types_project, mock_ingestor)

    project_name = typescript_types_project.name

    expected_utility_functions = [
        f"{project_name}.utility_types.updateUser",
        f"{project_name}.utility_types.createUser",
        f"{project_name}.utility_types.getUserBasicInfo",
        f"{project_name}.utility_types.validateUser",
        f"{project_name}.utility_types.assertIsUser",
        f"{project_name}.utility_types.deepPartial",
        f"{project_name}.utility_types.pick",
        f"{project_name}.utility_types.omit",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_utility_functions = [
        func for func in expected_utility_functions if func in created_functions
    ]
    assert len(found_utility_functions) >= 5, (
        f"Expected at least 5 utility type functions, found {len(found_utility_functions)}"
    )

    expected_utility_classes = [
        f"{project_name}.utility_types.UserService",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_utility_classes = [
        cls for cls in expected_utility_classes if cls in created_classes
    ]
    assert len(found_utility_classes) >= 1, (
        f"Expected at least 1 utility type class, found {len(found_utility_classes)}"
    )


def test_type_comprehensive(
    typescript_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all TypeScript type features create proper relationships."""
    test_file = typescript_types_project / "comprehensive_types.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every TypeScript type pattern in one file

// Basic types
let str: string = "hello";
let num: number = 42;
let bool: boolean = true;

// Interface
interface Person {
    name: string;
    age: number;
}

// Type alias
type ID = string | number;

// Generic interface
interface Container<T> {
    value: T;
    getValue(): T;
}

// Generic function
function identity<T>(value: T): T {
    return value;
}

// Generic class
class Box<T> implements Container<T> {
    constructor(public value: T) {}

    getValue(): T {
        return this.value;
    }
}

// Utility types
type PartialPerson = Partial<Person>;
type RequiredPerson = Required<Person>;

// Function with type annotations
function greet(person: Person): string {
    return `Hello, ${person.name}!`;
}

// Class implementing interface
class Employee implements Person {
    constructor(public name: string, public age: number, public role: string) {}

    getInfo(): string {
        return `${this.name} is ${this.age} years old`;
    }
}

// Using all type features
const person: Person = { name: "Alice", age: 30 };
const id: ID = "user-123";
const box = new Box<string>("content");
const employee = new Employee("Bob", 25, "Developer");

const greeting = greet(person);
const value = identity<number>(42);
const boxValue = box.getValue();
const empInfo = employee.getInfo();

// Type guards and assertions
function isPerson(obj: any): obj is Person {
    return obj && typeof obj.name === 'string' && typeof obj.age === 'number';
}

if (isPerson(person)) {
    console.log(person.name);
}

// Advanced patterns
type EventMap = {
    click: MouseEvent;
    keydown: KeyboardEvent;
    submit: SubmitEvent;
};

type EventHandler<K extends keyof EventMap> = (event: EventMap[K]) => void;

function addEventListener<K extends keyof EventMap>(
    type: K,
    handler: EventHandler<K>
): void {
    // Implementation would go here
}

// Using advanced patterns
addEventListener('click', (event) => {
    console.log(event.clientX, event.clientY);
});
""",
    )

    run_updater(typescript_types_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    implements_relationships = [
        c for c in all_relationships if c.args[1] == "IMPLEMENTS"
    ]

    comprehensive_calls = [
        call for call in call_relationships if "comprehensive_types" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 5, (
        f"Expected at least 5 comprehensive type calls, found {len(comprehensive_calls)}"
    )

    [
        call
        for call in implements_relationships
        if "comprehensive_types" in call.args[0][2]
    ]

    for relationship in comprehensive_calls:
        assert len(relationship.args) == 3, "Call relationship should have 3 args"
        assert relationship.args[1] == "CALLS", "Second arg should be 'CALLS'"

        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        assert "comprehensive_types" in source_module, (
            f"Source module should contain test file name: {source_module}"
        )

        assert isinstance(target_module, str) and target_module, (
            f"Target should be non-empty string: {target_module}"
        )

    assert defines_relationships, "Should still have DEFINES relationships"
