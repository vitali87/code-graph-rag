from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_nodes, get_relationships, run_updater


@pytest.fixture
def typescript_advanced_types_project(temp_repo: Path) -> Path:
    """Create a comprehensive TypeScript project with advanced type patterns."""
    project_path = temp_repo / "typescript_advanced_types_test"
    project_path.mkdir()

    (project_path / "types").mkdir()
    (project_path / "utils").mkdir()
    (project_path / "examples").mkdir()

    (project_path / "types" / "base.ts").write_text(
        encoding="utf-8",
        data="""
// Base types for advanced type examples
export interface BaseEntity {
    id: string;
    createdAt: Date;
    updatedAt: Date;
}

export type EntityType = 'user' | 'product' | 'order';

export interface User extends BaseEntity {
    name: string;
    email: string;
    role: 'admin' | 'user' | 'guest';
}
""",
    )

    return project_path


def test_generic_types(
    typescript_advanced_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript generic types and constraints."""
    test_file = typescript_advanced_types_project / "generic_types.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Generic types and constraints

// Basic generic interface
interface Container<T> {
    value: T;
    getValue(): T;
    setValue(value: T): void;
}

// Generic class with constraints
class Repository<T extends { id: string }> {
    private items: T[] = [];

    add(item: T): void {
        this.items.push(item);
    }

    findById(id: string): T | undefined {
        return this.items.find(item => item.id === id);
    }

    getAll(): T[] {
        return [...this.items];
    }

    update(id: string, updates: Partial<T>): T | undefined {
        const item = this.findById(id);
        if (item) {
            Object.assign(item, updates);
            return item;
        }
        return undefined;
    }

    remove(id: string): boolean {
        const index = this.items.findIndex(item => item.id === id);
        if (index !== -1) {
            this.items.splice(index, 1);
            return true;
        }
        return false;
    }
}

// Multiple generic parameters
interface KeyValuePair<K, V> {
    key: K;
    value: V;
}

class Dictionary<K extends string | number, V> {
    private pairs: KeyValuePair<K, V>[] = [];

    set(key: K, value: V): void {
        const existing = this.pairs.find(pair => pair.key === key);
        if (existing) {
            existing.value = value;
        } else {
            this.pairs.push({ key, value });
        }
    }

    get(key: K): V | undefined {
        const pair = this.pairs.find(pair => pair.key === key);
        return pair ? pair.value : undefined;
    }

    has(key: K): boolean {
        return this.pairs.some(pair => pair.key === key);
    }

    keys(): K[] {
        return this.pairs.map(pair => pair.key);
    }

    values(): V[] {
        return this.pairs.map(pair => pair.value);
    }

    entries(): KeyValuePair<K, V>[] {
        return [...this.pairs];
    }
}

// Generic functions with constraints
function merge<T extends object, U extends object>(obj1: T, obj2: U): T & U {
    return { ...obj1, ...obj2 };
}

function pick<T, K extends keyof T>(obj: T, keys: K[]): Pick<T, K> {
    const result = {} as Pick<T, K>;
    keys.forEach(key => {
        result[key] = obj[key];
    });
    return result;
}

function omit<T, K extends keyof T>(obj: T, keys: K[]): Omit<T, K> {
    const result = { ...obj } as any;
    keys.forEach(key => {
        delete result[key];
    });
    return result;
}

// Generic function with multiple constraints
function compare<T extends { toString(): string }>(a: T, b: T): number {
    const aStr = a.toString();
    const bStr = b.toString();
    return aStr.localeCompare(bStr);
}

// Conditional generic types
type IsArray<T> = T extends any[] ? true : false;
type ArrayElement<T> = T extends (infer U)[] ? U : never;
type ReturnTypeOf<T> = T extends (...args: any[]) => infer R ? R : never;

// Generic utility class
class AsyncResult<T, E = Error> {
    constructor(
        private promise: Promise<T>
    ) {}

    async map<U>(fn: (value: T) => U): Promise<AsyncResult<U, E>> {
        try {
            const value = await this.promise;
            return new AsyncResult(Promise.resolve(fn(value)));
        } catch (error) {
            return new AsyncResult(Promise.reject(error));
        }
    }

    async flatMap<U>(fn: (value: T) => Promise<U>): Promise<AsyncResult<U, E>> {
        try {
            const value = await this.promise;
            return new AsyncResult(fn(value));
        } catch (error) {
            return new AsyncResult(Promise.reject(error));
        }
    }

    async catch<U>(fn: (error: E) => U): Promise<AsyncResult<T | U, never>> {
        try {
            const value = await this.promise;
            return new AsyncResult(Promise.resolve(value));
        } catch (error) {
            return new AsyncResult(Promise.resolve(fn(error as E)));
        }
    }

    async unwrap(): Promise<T> {
        return this.promise;
    }
}

// Generic builder pattern
class QueryBuilder<T> {
    private conditions: string[] = [];
    private orderBy: string[] = [];
    private limitValue?: number;

    where(condition: keyof T, operator: string, value: any): this {
        this.conditions.push(`${String(condition)} ${operator} ${JSON.stringify(value)}`);
        return this;
    }

    order(field: keyof T, direction: 'ASC' | 'DESC' = 'ASC'): this {
        this.orderBy.push(`${String(field)} ${direction}`);
        return this;
    }

    limit(count: number): this {
        this.limitValue = count;
        return this;
    }

    build(): string {
        let query = 'SELECT * FROM table';

        if (this.conditions.length > 0) {
            query += ` WHERE ${this.conditions.join(' AND ')}`;
        }

        if (this.orderBy.length > 0) {
            query += ` ORDER BY ${this.orderBy.join(', ')}`;
        }

        if (this.limitValue) {
            query += ` LIMIT ${this.limitValue}`;
        }

        return query;
    }
}

// Generic event emitter
interface EventMap {
    [event: string]: any[];
}

class TypedEventEmitter<T extends EventMap> {
    private listeners: { [K in keyof T]?: Array<(...args: T[K]) => void> } = {};

    on<K extends keyof T>(event: K, listener: (...args: T[K]) => void): this {
        if (!this.listeners[event]) {
            this.listeners[event] = [];
        }
        this.listeners[event]!.push(listener);
        return this;
    }

    emit<K extends keyof T>(event: K, ...args: T[K]): boolean {
        const eventListeners = this.listeners[event];
        if (eventListeners) {
            eventListeners.forEach(listener => listener(...args));
            return true;
        }
        return false;
    }

    off<K extends keyof T>(event: K, listener: (...args: T[K]) => void): this {
        const eventListeners = this.listeners[event];
        if (eventListeners) {
            const index = eventListeners.indexOf(listener);
            if (index !== -1) {
                eventListeners.splice(index, 1);
            }
        }
        return this;
    }

    removeAllListeners<K extends keyof T>(event?: K): this {
        if (event) {
            delete this.listeners[event];
        } else {
            this.listeners = {};
        }
        return this;
    }
}

// Using generic types
interface User {
    id: string;
    name: string;
    email: string;
    age: number;
}

interface Product {
    id: string;
    name: string;
    price: number;
    category: string;
}

// Repository usage
const userRepository = new Repository<User>();
userRepository.add({ id: '1', name: 'Alice', email: 'alice@example.com', age: 30 });
const user = userRepository.findById('1');
console.log(user?.name);

// Dictionary usage
const userDict = new Dictionary<string, User>();
userDict.set('alice', { id: '1', name: 'Alice', email: 'alice@example.com', age: 30 });
const storedUser = userDict.get('alice');
console.log(storedUser?.email);

// Utility functions
const user1: User = { id: '1', name: 'Alice', email: 'alice@example.com', age: 30 };
const product1: Product = { id: '1', name: 'Laptop', price: 999, category: 'Electronics' };

const merged = merge(user1, { active: true });
const picked = pick(user1, ['name', 'email']);
const omitted = omit(user1, ['age']);

console.log(merged.name, merged.active);
console.log(picked.name, picked.email);
console.log(omitted.name); // age is omitted

// Comparison
console.log(compare('hello', 'world')); // -1
console.log(compare(10, 5)); // 1

// Query builder
const query = new QueryBuilder<User>()
    .where('age', '>', 18)
    .where('name', 'LIKE', '%Alice%')
    .order('name', 'ASC')
    .limit(10)
    .build();

console.log(query);

// Event emitter
interface AppEvents {
    userLogin: [user: User];
    userLogout: [userId: string];
    dataUpdate: [type: string, data: any];
}

const eventEmitter = new TypedEventEmitter<AppEvents>();

eventEmitter.on('userLogin', (user) => {
    console.log(`User ${user.name} logged in`);
});

eventEmitter.on('userLogout', (userId) => {
    console.log(`User ${userId} logged out`);
});

eventEmitter.emit('userLogin', user1);
eventEmitter.emit('userLogout', '1');

// Async result
const asyncResult = new AsyncResult(Promise.resolve(42));
asyncResult
    .map(x => x * 2)
    .then(result => result.unwrap())
    .then(value => console.log('Async result:', value));

// Type assertions with generics
type IsUserArray = IsArray<User[]>; // true
type UserElement = ArrayElement<User[]>; // User
type FunctionReturn = ReturnTypeOf<() => string>; // string

// Container implementation
class StringContainer implements Container<string> {
    constructor(public value: string) {}

    getValue(): string {
        return this.value;
    }

    setValue(value: string): void {
        this.value = value;
    }
}

const container = new StringContainer('Hello');
console.log(container.getValue());
container.setValue('World');
console.log(container.getValue());
""",
    )

    run_updater(typescript_advanced_types_project, mock_ingestor)

    class_calls = get_nodes(mock_ingestor, "Class")

    generic_classes = [
        call
        for call in class_calls
        if "generic_types" in call[0][1]["qualified_name"]
        and any(
            class_name in call[0][1]["qualified_name"]
            for class_name in [
                "Repository",
                "Dictionary",
                "AsyncResult",
                "QueryBuilder",
                "TypedEventEmitter",
            ]
        )
    ]

    assert len(generic_classes) >= 4, (
        f"Expected at least 4 generic classes, found {len(generic_classes)}"
    )

    function_calls = get_nodes(mock_ingestor, "Function")

    generic_functions = [
        call
        for call in function_calls
        if "generic_types" in call[0][1]["qualified_name"]
        and any(
            func_name in call[0][1]["qualified_name"]
            for func_name in ["merge", "pick", "omit", "compare"]
        )
    ]

    assert len(generic_functions) >= 4, (
        f"Expected at least 4 generic functions, found {len(generic_functions)}"
    )

    interface_calls = get_nodes(mock_ingestor, "Interface")

    generic_interfaces = [
        call
        for call in interface_calls
        if "generic_types" in call[0][1]["qualified_name"]
        and any(
            interface_name in call[0][1]["qualified_name"]
            for interface_name in ["Container", "KeyValuePair"]
        )
    ]

    assert len(generic_interfaces) >= 2, (
        f"Expected at least 2 generic interfaces, found {len(generic_interfaces)}"
    )


def test_utility_types(
    typescript_advanced_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript utility types and mapped types."""
    test_file = typescript_advanced_types_project / "utility_types.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Utility types and mapped types

// Base interface for examples
interface User {
    id: string;
    name: string;
    email: string;
    age: number;
    isActive: boolean;
    role: 'admin' | 'user' | 'guest';
    preferences: {
        theme: 'light' | 'dark';
        notifications: boolean;
    };
}

// Built-in utility types usage
type PartialUser = Partial<User>;
type RequiredUser = Required<PartialUser>;
type ReadonlyUser = Readonly<User>;
type UserEmail = Pick<User, 'email'>;
type UserWithoutAge = Omit<User, 'age'>;
type UserKeys = keyof User;
type UserRole = User['role'];

// Custom utility types
type Optional<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>;
type WithTimestamps<T> = T & {
    createdAt: Date;
    updatedAt: Date;
};

type Nullable<T> = T | null;
type NonNullable<T> = T extends null | undefined ? never : T;

// Deep utility types
type DeepReadonly<T> = {
    readonly [P in keyof T]: T[P] extends object ? DeepReadonly<T[P]> : T[P];
};

type DeepPartial<T> = {
    [P in keyof T]?: T[P] extends object ? DeepPartial<T[P]> : T[P];
};

// Mapped types
type StringifyValues<T> = {
    [K in keyof T]: string;
};

type BooleanFlags<T> = {
    [K in keyof T]: boolean;
};

type OptionalByKeys<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>;

type RequiredByKeys<T, K extends keyof T> = Omit<T, K> & Required<Pick<T, K>>;

// Conditional mapped types
type NonFunctionPropertyNames<T> = {
    [K in keyof T]: T[K] extends Function ? never : K;
}[keyof T];

type NonFunctionProperties<T> = Pick<T, NonFunctionPropertyNames<T>>;

type FunctionPropertyNames<T> = {
    [K in keyof T]: T[K] extends Function ? K : never;
}[keyof T];

type FunctionProperties<T> = Pick<T, FunctionPropertyNames<T>>;

// Advanced mapped types
type Mutable<T> = {
    -readonly [P in keyof T]: T[P];
};

type Required<T> = {
    [P in keyof T]-?: T[P];
};

type Getters<T> = {
    [K in keyof T as `get${Capitalize<string & K>}`]: () => T[K];
};

type Setters<T> = {
    [K in keyof T as `set${Capitalize<string & K>}`]: (value: T[K]) => void;
};

// Template literal types
type EventNames<T> = {
    [K in keyof T]: `${string & K}Changed`;
}[keyof T];

type HttpMethods = 'GET' | 'POST' | 'PUT' | 'DELETE';
type ApiEndpoints = `/api/${'users' | 'products' | 'orders'}`;
type ApiRoutes = `${HttpMethods} ${ApiEndpoints}`;

// Recursive utility types
type Paths<T> = T extends object ? {
    [K in keyof T]: K extends string
        ? T[K] extends object
            ? `${K}` | `${K}.${Paths<T[K]>}`
            : `${K}`
        : never;
}[keyof T] : never;

// Utility classes using mapped types
class EntityValidator<T> {
    private rules: { [K in keyof T]?: (value: T[K]) => boolean } = {};

    addRule<K extends keyof T>(key: K, rule: (value: T[K]) => boolean): this {
        this.rules[key] = rule;
        return this;
    }

    validate(entity: T): { [K in keyof T]?: string } {
        const errors: { [K in keyof T]?: string } = {};

        (Object.keys(this.rules) as Array<keyof T>).forEach(key => {
            const rule = this.rules[key];
            if (rule && !rule(entity[key])) {
                errors[key] = `Validation failed for ${String(key)}`;
            }
        });

        return errors;
    }
}

// Proxy builder with mapped types
class ProxyBuilder<T> {
    private handlers: { [K in keyof T]?: (value: T[K]) => T[K] } = {};

    intercept<K extends keyof T>(key: K, handler: (value: T[K]) => T[K]): this {
        this.handlers[key] = handler;
        return this;
    }

    build(target: T): T {
        return new Proxy(target, {
            get: (obj, prop) => {
                const key = prop as keyof T;
                const handler = this.handlers[key];
                const value = obj[key];
                return handler ? handler(value) : value;
            },
            set: (obj, prop, value) => {
                const key = prop as keyof T;
                const handler = this.handlers[key];
                obj[key] = handler ? handler(value) : value;
                return true;
            }
        });
    }
}

// Event system with utility types
interface EventPayloads {
    userCreated: User;
    userUpdated: { id: string; changes: Partial<User> };
    userDeleted: { id: string };
}

type EventListeners<T> = {
    [K in keyof T]: (payload: T[K]) => void;
};

class TypeSafeEventBus<T> {
    private listeners: { [K in keyof T]?: Array<(payload: T[K]) => void> } = {};

    on<K extends keyof T>(event: K, listener: (payload: T[K]) => void): this {
        if (!this.listeners[event]) {
            this.listeners[event] = [];
        }
        this.listeners[event]!.push(listener);
        return this;
    }

    emit<K extends keyof T>(event: K, payload: T[K]): void {
        const eventListeners = this.listeners[event];
        if (eventListeners) {
            eventListeners.forEach(listener => listener(payload));
        }
    }

    off<K extends keyof T>(event: K, listener: (payload: T[K]) => void): this {
        const eventListeners = this.listeners[event];
        if (eventListeners) {
            const index = eventListeners.indexOf(listener);
            if (index !== -1) {
                eventListeners.splice(index, 1);
            }
        }
        return this;
    }
}

// Form builder with utility types
type FormFields<T> = {
    [K in keyof T]: {
        value: T[K];
        error?: string;
        touched: boolean;
    };
};

type FormValidators<T> = {
    [K in keyof T]?: (value: T[K]) => string | undefined;
};

class FormBuilder<T> {
    private fields: FormFields<T>;
    private validators: FormValidators<T> = {};

    constructor(initialValues: T) {
        this.fields = {} as FormFields<T>;
        (Object.keys(initialValues) as Array<keyof T>).forEach(key => {
            this.fields[key] = {
                value: initialValues[key],
                touched: false
            };
        });
    }

    addValidator<K extends keyof T>(field: K, validator: (value: T[K]) => string | undefined): this {
        this.validators[field] = validator;
        return this;
    }

    setValue<K extends keyof T>(field: K, value: T[K]): this {
        this.fields[field].value = value;
        this.fields[field].touched = true;
        this.validateField(field);
        return this;
    }

    private validateField<K extends keyof T>(field: K): void {
        const validator = this.validators[field];
        if (validator) {
            const error = validator(this.fields[field].value);
            this.fields[field].error = error;
        }
    }

    getValues(): T {
        const values = {} as T;
        (Object.keys(this.fields) as Array<keyof T>).forEach(key => {
            values[key] = this.fields[key].value;
        });
        return values;
    }

    getErrors(): Partial<Record<keyof T, string>> {
        const errors: Partial<Record<keyof T, string>> = {};
        (Object.keys(this.fields) as Array<keyof T>).forEach(key => {
            if (this.fields[key].error) {
                errors[key] = this.fields[key].error;
            }
        });
        return errors;
    }

    isValid(): boolean {
        return Object.keys(this.getErrors()).length === 0;
    }
}

// Using utility types
type UserWithTimestamps = WithTimestamps<User>;
type OptionalEmailUser = Optional<User, 'email'>;
type DeepReadonlyUser = DeepReadonly<User>;
type UserPaths = Paths<User>;

// Validator usage
const userValidator = new EntityValidator<User>();
userValidator
    .addRule('email', (email) => email.includes('@'))
    .addRule('age', (age) => age >= 0 && age <= 150)
    .addRule('name', (name) => name.length > 0);

const user: User = {
    id: '1',
    name: 'Alice',
    email: 'alice@example.com',
    age: 30,
    isActive: true,
    role: 'user',
    preferences: { theme: 'light', notifications: true }
};

const validationErrors = userValidator.validate(user);
console.log('Validation errors:', validationErrors);

// Proxy builder usage
const proxyBuilder = new ProxyBuilder<Pick<User, 'name' | 'email'>>();
const proxiedUser = proxyBuilder
    .intercept('name', (name) => name.trim().toUpperCase())
    .intercept('email', (email) => email.toLowerCase())
    .build({ name: '  alice  ', email: 'ALICE@EXAMPLE.COM' });

console.log(proxiedUser.name); // 'ALICE'
console.log(proxiedUser.email); // 'alice@example.com'

// Event bus usage
const eventBus = new TypeSafeEventBus<EventPayloads>();

eventBus.on('userCreated', (user) => {
    console.log('User created:', user.name);
});

eventBus.on('userUpdated', ({ id, changes }) => {
    console.log(`User ${id} updated:`, changes);
});

eventBus.emit('userCreated', user);
eventBus.emit('userUpdated', { id: '1', changes: { name: 'Alice Smith' } });

// Form builder usage
const formBuilder = new FormBuilder({ name: '', email: '', age: 0 });
formBuilder
    .addValidator('name', (name) => name.length === 0 ? 'Name is required' : undefined)
    .addValidator('email', (email) => !email.includes('@') ? 'Invalid email' : undefined);

formBuilder.setValue('name', 'Alice');
formBuilder.setValue('email', 'alice@example.com');
formBuilder.setValue('age', 30);

console.log('Form values:', formBuilder.getValues());
console.log('Form errors:', formBuilder.getErrors());
console.log('Form valid:', formBuilder.isValid());

// Mapped type examples
type UserStringified = StringifyValues<User>;
type UserFlags = BooleanFlags<User>;
type UserGetters = Getters<Pick<User, 'name' | 'email'>>;
type UserSetters = Setters<Pick<User, 'name' | 'email'>>;

// API routes type usage
const apiRoutes: ApiRoutes[] = [
    'GET /api/users',
    'POST /api/users',
    'PUT /api/products',
    'DELETE /api/orders'
];

console.log('API routes:', apiRoutes);
""",
    )

    run_updater(typescript_advanced_types_project, mock_ingestor)

    class_calls = get_nodes(mock_ingestor, "Class")

    utility_classes = [
        call
        for call in class_calls
        if "utility_types" in call[0][1]["qualified_name"]
        and any(
            class_name in call[0][1]["qualified_name"]
            for class_name in [
                "EntityValidator",
                "ProxyBuilder",
                "TypeSafeEventBus",
                "FormBuilder",
            ]
        )
    ]

    assert len(utility_classes) >= 4, (
        f"Expected at least 4 utility type classes, found {len(utility_classes)}"
    )

    interface_calls = get_nodes(mock_ingestor, "Interface")

    utility_interfaces = [
        call
        for call in interface_calls
        if "utility_types" in call[0][1]["qualified_name"]
        and any(
            interface_name in call[0][1]["qualified_name"]
            for interface_name in ["User", "EventPayloads"]
        )
    ]

    assert len(utility_interfaces) >= 2, (
        f"Expected at least 2 utility type interfaces, found {len(utility_interfaces)}"
    )


def test_conditional_types(
    typescript_advanced_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript conditional types and type inference."""
    test_file = typescript_advanced_types_project / "conditional_types.ts"
    test_file.write_text(
        encoding="utf-8",
        data=r"""
// Conditional types and type inference

// Basic conditional types
type IsString<T> = T extends string ? true : false;
type IsNumber<T> = T extends number ? true : false;
type IsArray<T> = T extends any[] ? true : false;
type IsFunction<T> = T extends (...args: any[]) => any ? true : false;

// Conditional types with infer
type ReturnType<T> = T extends (...args: any[]) => infer R ? R : never;
type Parameters<T> = T extends (...args: infer P) => any ? P : never;
type ArrayElement<T> = T extends (infer U)[] ? U : never;
type PromiseType<T> = T extends Promise<infer U> ? U : never;

// Advanced conditional types
type NonNullable<T> = T extends null | undefined ? never : T;
type Exclude<T, U> = T extends U ? never : T;
type Extract<T, U> = T extends U ? T : never;

// Recursive conditional types
type Flatten<T> = T extends (infer U)[] ? Flatten<U> : T;
type DeepReadonly<T> = T extends object
    ? { readonly [K in keyof T]: DeepReadonly<T[K]> }
    : T;

// Conditional types for object manipulation
type OptionalKeys<T> = {
    [K in keyof T]: {} extends Pick<T, K> ? K : never;
}[keyof T];

type RequiredKeys<T> = {
    [K in keyof T]: {} extends Pick<T, K> ? never : K;
}[keyof T];

type OptionalProperties<T> = Pick<T, OptionalKeys<T>>;
type RequiredProperties<T> = Pick<T, RequiredKeys<T>>;

// Function type utilities
type FirstParameter<T> = T extends (first: infer P, ...rest: any[]) => any ? P : never;
type LastParameter<T> = T extends (...args: [...any[], infer L]) => any ? L : never;
type FunctionName<T> = T extends { name: infer N } ? N : never;

// Conditional type for API responses
type ApiResponse<T> = T extends { success: true }
    ? { data: T; error: null }
    : { data: null; error: string };

type ApiResult<T> = T extends Promise<infer U>
    ? Promise<ApiResponse<U>>
    : ApiResponse<T>;

// Advanced pattern matching with conditional types
type ParseRoute<T> = T extends `/${infer Path}`
    ? Path extends `${infer Segment}/${infer Rest}`
        ? [Segment, ...ParseRoute<`/${Rest}`>]
        : [Path]
    : [];

type RouteParams<T> = T extends `${string}:${infer Param}/${infer Rest}`
    ? { [K in Param]: string } & RouteParams<Rest>
    : T extends `${string}:${infer Param}`
    ? { [K in Param]: string }
    : {};

// Database query builder with conditional types
type WhereCondition<T> = {
    [K in keyof T]?: T[K] | {
        $eq?: T[K];
        $ne?: T[K];
        $gt?: T[K] extends number ? T[K] : never;
        $gte?: T[K] extends number ? T[K] : never;
        $lt?: T[K] extends number ? T[K] : never;
        $lte?: T[K] extends number ? T[K] : never;
        $in?: T[K][];
        $nin?: T[K][];
        $regex?: T[K] extends string ? RegExp : never;
    };
};

type SortOrder<T> = {
    [K in keyof T]?: T[K] extends string | number ? 1 | -1 : never;
};

// Conditional type utility functions
function isString<T>(value: T): value is Extract<T, string> {
    return typeof value === 'string';
}

function isNumber<T>(value: T): value is Extract<T, number> {
    return typeof value === 'number';
}

function isArray<T>(value: T): value is Extract<T, any[]> {
    return Array.isArray(value);
}

// Type guards with conditional types
type TypeGuard<T, U> = (value: T) => value is U;

function createTypeGuard<T, U extends T>(
    predicate: (value: T) => boolean
): TypeGuard<T, U> {
    return (value: T): value is U => predicate(value);
}

// Advanced conditional type examples
class ConditionalTypeProcessor<T> {
    process<U>(
        value: U
    ): U extends string
        ? string
        : U extends number
        ? number
        : U extends boolean
        ? boolean
        : unknown {
        if (typeof value === 'string') {
            return value.toUpperCase() as any;
        } else if (typeof value === 'number') {
            return (value * 2) as any;
        } else if (typeof value === 'boolean') {
            return (!value) as any;
        } else {
            return value as any;
        }
    }

    filter<U>(
        array: U[],
        predicate: (item: U) => boolean
    ): U extends any[] ? Flatten<U>[] : U[] {
        return array.filter(predicate) as any;
    }

    transform<U, V>(
        value: U,
        transformer: (value: NonNullable<U>) => V
    ): U extends null | undefined ? null : V {
        return value != null ? transformer(value as NonNullable<U>) as any : null as any;
    }
}

// Event system with conditional types
type EventMap = {
    click: { x: number; y: number };
    keypress: { key: string; code: string };
    resize: { width: number; height: number };
    custom: any;
};

type EventHandler<T extends keyof EventMap> = (event: EventMap[T]) => void;

class ConditionalEventEmitter {
    private handlers: {
        [K in keyof EventMap]?: EventHandler<K>[];
    } = {};

    on<T extends keyof EventMap>(
        event: T,
        handler: EventHandler<T>
    ): this {
        if (!this.handlers[event]) {
            this.handlers[event] = [];
        }
        this.handlers[event]!.push(handler);
        return this;
    }

    emit<T extends keyof EventMap>(
        event: T,
        ...args: EventMap[T] extends undefined ? [] : [EventMap[T]]
    ): void {
        const eventHandlers = this.handlers[event];
        if (eventHandlers) {
            eventHandlers.forEach(handler => {
                if (args.length > 0) {
                    handler(args[0]);
                } else {
                    handler(undefined as any);
                }
            });
        }
    }
}

// REST API client with conditional types
type HttpMethod = 'GET' | 'POST' | 'PUT' | 'DELETE';

type RequestConfig<T extends HttpMethod> = {
    method: T;
    url: string;
} & (T extends 'POST' | 'PUT' ? { body: any } : {})
  & (T extends 'GET' ? { params?: Record<string, string> } : {});

class ApiClient {
    async request<T extends HttpMethod>(
        config: RequestConfig<T>
    ): Promise<T extends 'DELETE' ? void : any> {
        // Mock implementation
        console.log(`${config.method} ${config.url}`);

        if ('body' in config) {
            console.log('Body:', config.body);
        }

        if ('params' in config && config.params) {
            console.log('Params:', config.params);
        }

        // Simulate response
        if (config.method === 'DELETE') {
            return undefined as any;
        }

        return { data: 'mock response' } as any;
    }

    async get(url: string, params?: Record<string, string>) {
        return this.request({ method: 'GET', url, params });
    }

    async post(url: string, body: any) {
        return this.request({ method: 'POST', url, body });
    }

    async put(url: string, body: any) {
        return this.request({ method: 'PUT', url, body });
    }

    async delete(url: string) {
        return this.request({ method: 'DELETE', url });
    }
}

// Validation system with conditional types
type ValidationRule<T> = T extends string
    ? { minLength?: number; maxLength?: number; pattern?: RegExp }
    : T extends number
    ? { min?: number; max?: number }
    : T extends boolean
    ? { required?: boolean }
    : { custom?: (value: T) => boolean };

type ValidationSchema<T> = {
    [K in keyof T]?: ValidationRule<T[K]>;
};

class ConditionalValidator<T> {
    constructor(private schema: ValidationSchema<T>) {}

    validate(data: T): { [K in keyof T]?: string } {
        const errors: { [K in keyof T]?: string } = {};

        (Object.keys(this.schema) as Array<keyof T>).forEach(key => {
            const rule = this.schema[key];
            const value = data[key];

            if (rule && typeof value === 'string') {
                const stringRule = rule as ValidationRule<string>;
                if (stringRule.minLength && value.length < stringRule.minLength) {
                    errors[key] = `Minimum length is ${stringRule.minLength}`;
                }
                if (stringRule.maxLength && value.length > stringRule.maxLength) {
                    errors[key] = `Maximum length is ${stringRule.maxLength}`;
                }
                if (stringRule.pattern && !stringRule.pattern.test(value)) {
                    errors[key] = 'Pattern does not match';
                }
            } else if (rule && typeof value === 'number') {
                const numberRule = rule as ValidationRule<number>;
                if (numberRule.min !== undefined && value < numberRule.min) {
                    errors[key] = `Minimum value is ${numberRule.min}`;
                }
                if (numberRule.max !== undefined && value > numberRule.max) {
                    errors[key] = `Maximum value is ${numberRule.max}`;
                }
            }
        });

        return errors;
    }
}

// Using conditional types
type StringCheck = IsString<string>; // true
type NumberCheck = IsNumber<string>; // false
type ArrayCheck = IsArray<number[]>; // true

type FuncReturn = ReturnType<() => string>; // string
type FuncParams = Parameters<(a: number, b: string) => void>; // [number, string]

type NestedArray = number[][][];
type FlattenedType = Flatten<NestedArray>; // number

// API client usage
const apiClient = new ApiClient();

apiClient.get('/users', { page: '1' });
apiClient.post('/users', { name: 'Alice', email: 'alice@example.com' });
apiClient.put('/users/1', { name: 'Alice Smith' });
apiClient.delete('/users/1'); // Returns void

// Event emitter usage
const eventEmitter = new ConditionalEventEmitter();

eventEmitter.on('click', (event) => {
    console.log(`Clicked at ${event.x}, ${event.y}`);
});

eventEmitter.on('keypress', (event) => {
    console.log(`Key pressed: ${event.key}`);
});

eventEmitter.emit('click', { x: 100, y: 200 });
eventEmitter.emit('keypress', { key: 'Enter', code: 'Enter' });

// Validation usage
interface UserData {
    name: string;
    age: number;
    active: boolean;
}

const validator = new ConditionalValidator<UserData>({
    name: { minLength: 2, maxLength: 50, pattern: /^[A-Za-z\s]+$/ },
    age: { min: 0, max: 150 },
    active: { required: true }
});

const userData: UserData = { name: 'A', age: 200, active: true };
const validationErrors = validator.validate(userData);
console.log('Validation errors:', validationErrors);

// Type processor usage
const processor = new ConditionalTypeProcessor();

console.log(processor.process('hello')); // 'HELLO'
console.log(processor.process(42)); // 84
console.log(processor.process(true)); // false

const transformedValue = processor.transform('test', (str) => str.length);
console.log(transformedValue); // 4

// Route parsing examples
type UserRoute = ParseRoute<'/users/:id/posts/:postId'>; // ['users', ':id', 'posts', ':postId']
type UserParams = RouteParams<'/users/:id/posts/:postId'>; // { id: string; postId: string }

console.log('Route parsing and conditional types working correctly');
""",
    )

    run_updater(typescript_advanced_types_project, mock_ingestor)

    class_calls = get_nodes(mock_ingestor, "Class")

    conditional_classes = [
        call
        for call in class_calls
        if "conditional_types" in call[0][1]["qualified_name"]
        and any(
            class_name in call[0][1]["qualified_name"]
            for class_name in [
                "ConditionalTypeProcessor",
                "ConditionalEventEmitter",
                "ApiClient",
                "ConditionalValidator",
            ]
        )
    ]

    assert len(conditional_classes) >= 4, (
        f"Expected at least 4 conditional type classes, found {len(conditional_classes)}"
    )

    function_calls = get_nodes(mock_ingestor, "Function")

    conditional_functions = [
        call
        for call in function_calls
        if "conditional_types" in call[0][1]["qualified_name"]
        and any(
            func_name in call[0][1]["qualified_name"]
            for func_name in ["isString", "isNumber", "isArray", "createTypeGuard"]
        )
    ]

    assert len(conditional_functions) >= 4, (
        f"Expected at least 4 conditional type functions, found {len(conditional_functions)}"
    )


def test_template_literal_types(
    typescript_advanced_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript template literal types and string manipulation."""
    test_file = typescript_advanced_types_project / "template_literal_types.ts"
    test_file.write_text(
        encoding="utf-8",
        data=r"""
// Template literal types and string manipulation

// Basic template literal types
type Greeting = `Hello, ${string}!`;
type NumberString = `${number}`;
type BooleanString = `${boolean}`;

// Template literal types with unions
type Theme = 'light' | 'dark';
type Size = 'small' | 'medium' | 'large';
type ButtonClass = `btn-${Theme}-${Size}`;

// Event names with template literals
type EventName<T extends string> = `on${Capitalize<T>}`;
type ChangeEvent<T extends string> = `${T}Changed`;

// API endpoints with template literals
type Entity = 'user' | 'product' | 'order';
type Action = 'create' | 'read' | 'update' | 'delete';
type ApiEndpoint = `/api/${Entity}/${Action}`;

// Route patterns
type HttpMethod = 'GET' | 'POST' | 'PUT' | 'DELETE';
type Route<T extends string> = `${HttpMethod} ${T}`;
type RestRoute<T extends Entity> = Route<`/api/${T}` | `/api/${T}/:id`>;

// CSS property names
type CSSProperty =
    | 'margin' | 'padding' | 'border' | 'background'
    | 'font' | 'color' | 'display' | 'position';
type CSSDirection = 'top' | 'right' | 'bottom' | 'left';
type CSSPropertyWithDirection = `${Extract<CSSProperty, 'margin' | 'padding' | 'border'>}-${CSSDirection}`;

// Database column types
type SQLType = 'VARCHAR' | 'INTEGER' | 'BOOLEAN' | 'TIMESTAMP';
type ColumnDefinition<T extends string, U extends SQLType> = `${T} ${U}`;

// String manipulation utilities
type Uppercase<S extends string> = Intrinsic;
type Lowercase<S extends string> = Intrinsic;
type Capitalize<S extends string> = Intrinsic;
type Uncapitalize<S extends string> = Intrinsic;

// Custom string manipulation types
type Join<T extends readonly string[], D extends string> = T extends readonly [infer F, ...infer R]
    ? F extends string
        ? R extends readonly string[]
            ? R['length'] extends 0
                ? F
                : `${F}${D}${Join<R, D>}`
            : never
        : never
    : '';

type Split<S extends string, D extends string> = S extends `${infer T}${D}${infer U}`
    ? [T, ...Split<U, D>]
    : [S];

type Replace<S extends string, From extends string, To extends string> = S extends `${infer L}${From}${infer R}`
    ? `${L}${To}${Replace<R, From, To>}`
    : S;

// Path manipulation
type PathSegments<T extends string> = Split<T, '/'>;
type JoinPath<T extends readonly string[]> = Join<T, '/'>;

// Query string types
type QueryParam<K extends string, V extends string | number | boolean> = `${K}=${V}`;
type QueryString<T extends Record<string, string | number | boolean>> = {
    [K in keyof T]: QueryParam<K & string, T[K]>
}[keyof T];

// Configuration system with template literals
type ConfigKey<T extends string> = `config.${T}`;
type NestedConfigKey<T extends string, U extends string> = `config.${T}.${U}`;

// Database query builder with template literals
class QueryBuilder<T extends string> {
    private tableName: T;
    private selectFields: string[] = ['*'];
    private whereConditions: string[] = [];
    private orderByFields: string[] = [];

    constructor(tableName: T) {
        this.tableName = tableName;
    }

    select<K extends string>(...fields: K[]): QueryBuilder<T> {
        this.selectFields = fields;
        return this;
    }

    where<K extends string, V extends string | number>(
        field: K,
        operator: '=' | '!=' | '>' | '<' | '>=' | '<=',
        value: V
    ): QueryBuilder<T> {
        this.whereConditions.push(`${field} ${operator} ${JSON.stringify(value)}`);
        return this;
    }

    orderBy<K extends string>(field: K, direction: 'ASC' | 'DESC' = 'ASC'): QueryBuilder<T> {
        this.orderByFields.push(`${field} ${direction}`);
        return this;
    }

    build(): `SELECT ${string} FROM ${T}${string}` {
        let query = `SELECT ${this.selectFields.join(', ')} FROM ${this.tableName}`;

        if (this.whereConditions.length > 0) {
            query += ` WHERE ${this.whereConditions.join(' AND ')}`;
        }

        if (this.orderByFields.length > 0) {
            query += ` ORDER BY ${this.orderByFields.join(', ')}`;
        }

        return query as `SELECT ${string} FROM ${T}${string}`;
    }
}

// Event system with template literal types
type EventHandler<T extends string> = {
    [K in EventName<T>]: (event: any) => void;
};

class TemplateEventEmitter<T extends string> {
    private handlers: Partial<EventHandler<T>> = {};

    on<K extends T>(event: EventName<K>, handler: (event: any) => void): this {
        const eventName = `on${event.charAt(0).toUpperCase() + event.slice(1)}` as EventName<K>;
        this.handlers[eventName] = handler;
        return this;
    }

    emit<K extends T>(event: K, data: any): void {
        const eventName = `on${event.charAt(0).toUpperCase() + event.slice(1)}` as EventName<K>;
        const handler = this.handlers[eventName];
        if (handler) {
            handler(data);
        }
    }
}

// CSS-in-JS system with template literals
type CSSValue = string | number;
type CSSRule<P extends string> = `${P}: ${CSSValue}`;

class StyleBuilder {
    private rules: string[] = [];

    add<P extends CSSProperty>(property: P, value: CSSValue): this {
        this.rules.push(`${property}: ${value}`);
        return this;
    }

    addMargin(direction: CSSDirection, value: CSSValue): this {
        this.rules.push(`margin-${direction}: ${value}`);
        return this;
    }

    addPadding(direction: CSSDirection, value: CSSValue): this {
        this.rules.push(`padding-${direction}: ${value}`);
        return this;
    }

    build(): string {
        return `{ ${this.rules.join('; ')} }`;
    }
}

// REST API client with template literal endpoints
class RestClient<T extends Entity> {
    constructor(private baseUrl: string, private entity: T) {}

    async get(): Promise<any> {
        const url = `${this.baseUrl}/api/${this.entity}`;
        console.log(`GET ${url}`);
        return { data: [] };
    }

    async getById(id: string): Promise<any> {
        const url = `${this.baseUrl}/api/${this.entity}/${id}`;
        console.log(`GET ${url}`);
        return { data: {} };
    }

    async create(data: any): Promise<any> {
        const url = `${this.baseUrl}/api/${this.entity}`;
        console.log(`POST ${url}`, data);
        return { data: { id: '1', ...data } };
    }

    async update(id: string, data: any): Promise<any> {
        const url = `${this.baseUrl}/api/${this.entity}/${id}`;
        console.log(`PUT ${url}`, data);
        return { data: { id, ...data } };
    }

    async delete(id: string): Promise<void> {
        const url = `${this.baseUrl}/api/${this.entity}/${id}`;
        console.log(`DELETE ${url}`);
    }
}

// Configuration manager with template literal keys
type AppConfig = {
    'database.host': string;
    'database.port': number;
    'database.name': string;
    'server.port': number;
    'server.host': string;
    'logging.level': 'debug' | 'info' | 'warn' | 'error';
    'cache.ttl': number;
};

class ConfigManager {
    private config: Partial<AppConfig> = {};

    set<K extends keyof AppConfig>(key: K, value: AppConfig[K]): this {
        this.config[key] = value;
        return this;
    }

    get<K extends keyof AppConfig>(key: K): AppConfig[K] | undefined {
        return this.config[key];
    }

    has<K extends keyof AppConfig>(key: K): boolean {
        return key in this.config;
    }

    getSection<T extends string>(section: T): Partial<Pick<AppConfig, Extract<keyof AppConfig, `${T}.${string}`>>> {
        const result: any = {};
        const prefix = `${section}.`;

        Object.keys(this.config).forEach(key => {
            if (key.startsWith(prefix)) {
                result[key] = this.config[key as keyof AppConfig];
            }
        });

        return result;
    }
}

// URL builder with template literals
class UrlBuilder {
    private baseUrl: string;
    private pathSegments: string[] = [];
    private queryParams: Record<string, string> = {};

    constructor(baseUrl: string) {
        this.baseUrl = baseUrl.replace(/\/$/, '');
    }

    path<T extends string>(segment: T): this {
        this.pathSegments.push(segment);
        return this;
    }

    param<K extends string, V extends string | number | boolean>(key: K, value: V): this {
        this.queryParams[key] = String(value);
        return this;
    }

    build(): string {
        let url = this.baseUrl;

        if (this.pathSegments.length > 0) {
            url += '/' + this.pathSegments.join('/');
        }

        const queryString = Object.entries(this.queryParams)
            .map(([key, value]) => `${key}=${encodeURIComponent(value)}`)
            .join('&');

        if (queryString) {
            url += '?' + queryString;
        }

        return url;
    }
}

// Using template literal types
type MyGreeting = Greeting; // `Hello, ${string}!`
type ButtonClasses = ButtonClass; // 'btn-light-small' | 'btn-light-medium' | ... etc
type UserApiEndpoints = ApiEndpoint; // `/api/${'user' | 'product' | 'order'}/${'create' | 'read' | 'update' | 'delete'}`

// Query builder usage
const userQuery = new QueryBuilder('users');
const query = userQuery
    .select('id', 'name', 'email')
    .where('age', '>', 18)
    .where('active', '=', true)
    .orderBy('name', 'ASC')
    .build();

console.log('Generated query:', query);

// Event emitter usage
const eventEmitter = new TemplateEventEmitter<'click' | 'hover' | 'focus'>();

eventEmitter.on('click', (event) => {
    console.log('Click event:', event);
});

eventEmitter.on('hover', (event) => {
    console.log('Hover event:', event);
});

eventEmitter.emit('click', { x: 100, y: 200 });
eventEmitter.emit('hover', { element: 'button' });

// Style builder usage
const styles = new StyleBuilder()
    .add('display', 'flex')
    .add('color', '#333')
    .addMargin('top', '10px')
    .addPadding('left', '20px')
    .build();

console.log('Generated styles:', styles);

// REST client usage
const userClient = new RestClient('https://api.example.com', 'user');
const productClient = new RestClient('https://api.example.com', 'product');

userClient.get();
userClient.getById('123');
userClient.create({ name: 'Alice', email: 'alice@example.com' });

productClient.get();
productClient.update('456', { name: 'Updated Product' });

// Config manager usage
const configManager = new ConfigManager();
configManager
    .set('database.host', 'localhost')
    .set('database.port', 5432)
    .set('database.name', 'myapp')
    .set('server.port', 3000)
    .set('logging.level', 'info');

console.log('Database config:', configManager.getSection('database'));
console.log('Server port:', configManager.get('server.port'));

// URL builder usage
const urlBuilder = new UrlBuilder('https://api.example.com');
const apiUrl = urlBuilder
    .path('api')
    .path('users')
    .param('page', 1)
    .param('limit', 10)
    .param('active', true)
    .build();

console.log('Built URL:', apiUrl);

// String manipulation examples
type UppercaseHello = Uppercase<'hello'>; // 'HELLO'
type LowercaseWORLD = Lowercase<'WORLD'>; // 'world'
type CapitalizedName = Capitalize<'alice'>; // 'Alice'

type JoinedStrings = Join<['hello', 'world'], '-'>; // 'hello-world'
type SplitString = Split<'hello-world-test', '-'>; // ['hello', 'world', 'test']
type ReplacedString = Replace<'hello world hello', 'hello', 'hi'>; // 'hi world hi'

console.log('Template literal types are working correctly');
""",
    )

    run_updater(typescript_advanced_types_project, mock_ingestor)

    class_calls = get_nodes(mock_ingestor, "Class")

    template_literal_classes = [
        call
        for call in class_calls
        if "template_literal_types" in call[0][1]["qualified_name"]
        and any(
            class_name in call[0][1]["qualified_name"]
            for class_name in [
                "QueryBuilder",
                "TemplateEventEmitter",
                "StyleBuilder",
                "RestClient",
                "ConfigManager",
                "UrlBuilder",
            ]
        )
    ]

    assert len(template_literal_classes) >= 5, (
        f"Expected at least 5 template literal type classes, found {len(template_literal_classes)}"
    )


def test_typescript_advanced_types_comprehensive(
    typescript_advanced_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all TypeScript advanced type patterns are covered."""
    test_file = typescript_advanced_types_project / "comprehensive_advanced_types.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every TypeScript advanced type pattern in one file

// Generic constraint
interface HasId {
    id: string;
}

// Generic class with constraint
class Repository<T extends HasId> {
    private items: T[] = [];

    add(item: T): void {
        this.items.push(item);
    }

    findById(id: string): T | undefined {
        return this.items.find(item => item.id === id);
    }
}

// Utility types
type PartialUser = Partial<{ name: string; email: string }>;
type RequiredUser = Required<PartialUser>;

// Conditional type
type IsString<T> = T extends string ? true : false;

// Template literal type
type EventName<T extends string> = `on${Capitalize<T>}`;

// Mapped type
type Readonly<T> = {
    readonly [P in keyof T]: T[P];
};

// Advanced generic function
function transform<T, U>(value: T, mapper: (input: T) => U): U {
    return mapper(value);
}

// Comprehensive example class
class AdvancedTypeExample<T extends HasId> {
    constructor(private repository: Repository<T>) {}

    process<U>(
        item: T,
        transformer: (input: T) => U
    ): U extends string ? string : U {
        const result = transformer(item);
        if (typeof result === 'string') {
            return result.toUpperCase() as any;
        }
        return result as any;
    }

    getEventName<K extends string>(event: K): EventName<K> {
        return `on${event.charAt(0).toUpperCase() + event.slice(1)}` as EventName<K>;
    }
}

// Using all advanced type patterns
interface User extends HasId {
    name: string;
    email: string;
}

const userRepo = new Repository<User>();
const example = new AdvancedTypeExample(userRepo);

const user: User = { id: '1', name: 'Alice', email: 'alice@example.com' };
userRepo.add(user);

const processedName = example.process(user, u => u.name);
const eventName = example.getEventName('click');

console.log(processedName); // 'ALICE'
console.log(eventName); // 'onClick'

// Type checks
type StringCheck = IsString<string>; // true
type NumberCheck = IsString<number>; // false

const transformed = transform('hello', str => str.length);
console.log(transformed); // 5

// Utility type usage
const partialUser: PartialUser = { name: 'Bob' };
const requiredUser: RequiredUser = { name: 'Charlie', email: 'charlie@example.com' };

console.log('All advanced type patterns working correctly');
""",
    )

    run_updater(typescript_advanced_types_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    calls_relationships = get_relationships(mock_ingestor, "CALLS")
    [c for c in all_relationships if c.args[1] == "DEFINES"]

    comprehensive_calls = [
        call
        for call in calls_relationships
        if "comprehensive_advanced_types" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 3, (
        f"Expected at least 3 comprehensive advanced type calls, found {len(comprehensive_calls)}"
    )

    function_calls = get_nodes(mock_ingestor, "Function")

    comprehensive_functions = [
        call
        for call in function_calls
        if "comprehensive_advanced_types" in call[0][1]["qualified_name"]
        and "transform" in call[0][1]["qualified_name"]
    ]

    assert len(comprehensive_functions) >= 1, (
        f"Expected at least 1 advanced type function, found {len(comprehensive_functions)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    comprehensive_classes = [
        call
        for call in class_calls
        if "comprehensive_advanced_types" in call[0][1]["qualified_name"]
        and any(
            class_name in call[0][1]["qualified_name"]
            for class_name in ["Repository", "AdvancedTypeExample"]
        )
    ]

    assert len(comprehensive_classes) >= 2, (
        f"Expected at least 2 comprehensive advanced type classes, found {len(comprehensive_classes)}"
    )
