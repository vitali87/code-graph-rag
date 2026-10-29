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
def typescript_classes_project(temp_repo: Path) -> Path:
    """Create a comprehensive TypeScript project with class features."""
    project_path = temp_repo / "typescript_classes_test"
    project_path.mkdir()

    (project_path / "models").mkdir()
    (project_path / "services").mkdir()
    (project_path / "utils").mkdir()

    (project_path / "models" / "base.ts").write_text(
        encoding="utf-8",
        data="""
export abstract class BaseModel {
    protected id: string;

    constructor(id: string) {
        this.id = id;
    }

    abstract validate(): boolean;
}
""",
    )

    return project_path


def test_access_modifiers(
    typescript_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript access modifiers (public, private, protected)."""
    test_file = typescript_classes_project / "access_modifiers.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// TypeScript access modifiers

class AccessModifierDemo {
    // Public members (default)
    public publicProperty: string;
    public readonly publicReadonly: number;

    // Private members
    private privateProperty: string;
    private readonly privateReadonly: number;

    // Protected members
    protected protectedProperty: string;
    protected readonly protectedReadonly: number;

    constructor(
        value: string,
        count: number
    ) {
        this.publicProperty = value;
        this.publicReadonly = count;
        this.privateProperty = value.toLowerCase();
        this.privateReadonly = count * 2;
        this.protectedProperty = value.toUpperCase();
        this.protectedReadonly = count * 3;
    }

    // Public methods
    public getPublicInfo(): string {
        return `Public: ${this.publicProperty}`;
    }

    public getFullInfo(): string {
        return this.getPrivateInfo() + ' | ' + this.getProtectedInfo();
    }

    // Private methods
    private getPrivateInfo(): string {
        return `Private: ${this.privateProperty}`;
    }

    private validatePrivate(): boolean {
        return this.privateProperty.length > 0;
    }

    // Protected methods
    protected getProtectedInfo(): string {
        return `Protected: ${this.protectedProperty}`;
    }

    protected validateProtected(): boolean {
        return this.protectedProperty.length > 0;
    }

    // Static members with modifiers
    public static publicStaticMethod(): string {
        return 'Public static';
    }

    private static privateStaticMethod(): string {
        return 'Private static';
    }

    protected static protectedStaticMethod(): string {
        return 'Protected static';
    }

    // Getters and setters with modifiers
    public get publicValue(): string {
        return this.publicProperty;
    }

    public set publicValue(value: string) {
        this.publicProperty = value;
    }

    private get privateValue(): string {
        return this.privateProperty;
    }

    private set privateValue(value: string) {
        this.privateProperty = value;
    }

    protected get protectedValue(): string {
        return this.protectedProperty;
    }

    protected set protectedValue(value: string) {
        this.protectedProperty = value;
    }
}

// Inheritance with access modifiers
class ExtendedDemo extends AccessModifierDemo {
    private extendedPrivate: boolean;

    constructor(value: string, count: number, flag: boolean) {
        super(value, count);
        this.extendedPrivate = flag;
    }

    // Can access protected members from parent
    public getInheritedInfo(): string {
        return this.getProtectedInfo(); // OK - protected
        // return this.getPrivateInfo(); // Error - private not accessible
    }

    // Override protected method
    protected getProtectedInfo(): string {
        const parentInfo = super.getProtectedInfo();
        return `${parentInfo} (Extended)`;
    }

    // Can call protected static method
    public static getProtectedStatic(): string {
        return AccessModifierDemo.protectedStaticMethod();
    }

    // Access protected properties
    public checkProtected(): boolean {
        return this.protectedProperty.length > 0; // OK
        // return this.privateProperty.length > 0; // Error - private
    }
}

// Interface with access-like patterns
interface ISecure {
    publicMethod(): void;
    // Interfaces don't have access modifiers, but show intent
}

// Implementation with specific access patterns
class SecureImplementation implements ISecure {
    private secretKey: string;
    protected config: object;
    public apiEndpoint: string;

    constructor(key: string, endpoint: string) {
        this.secretKey = key;
        this.config = {};
        this.apiEndpoint = endpoint;
    }

    // Interface method must be public
    public publicMethod(): void {
        this.processSecurely();
    }

    private processSecurely(): void {
        // Private implementation
        console.log('Processing with key:', this.secretKey);
    }

    protected configureSettings(settings: object): void {
        this.config = { ...this.config, ...settings };
    }
}

// Generic class with access modifiers
class Repository<T> {
    private items: T[] = [];
    protected connection: string;
    public readonly name: string;

    constructor(name: string, connection: string) {
        this.name = name;
        this.connection = connection;
    }

    public add(item: T): void {
        this.items.push(item);
        this.logChange('add');
    }

    public get(index: number): T | undefined {
        return this.items[index];
    }

    public getAll(): T[] {
        return [...this.items]; // Return copy
    }

    private logChange(operation: string): void {
        console.log(`${operation} performed on ${this.name}`);
    }

    protected validate(item: T): boolean {
        return item != null;
    }

    // Static factory with access modifiers
    public static create<U>(name: string): Repository<U> {
        return new Repository<U>(name, 'default');
    }

    private static validateName(name: string): boolean {
        return name.length > 0;
    }
}

// Using classes with access modifiers
const demo = new AccessModifierDemo('test', 10);
console.log(demo.publicProperty); // OK
console.log(demo.getPublicInfo()); // OK
// console.log(demo.privateProperty); // Error - private
// console.log(demo.getPrivateInfo()); // Error - private

const extended = new ExtendedDemo('extended', 20, true);
console.log(extended.getInheritedInfo()); // OK
console.log(extended.checkProtected()); // OK

const secure = new SecureImplementation('secret', 'https://api.example.com');
secure.publicMethod(); // OK
console.log(secure.apiEndpoint); // OK
// console.log(secure.secretKey); // Error - private

const repo = new Repository<string>('users', 'db://localhost');
repo.add('user1');
console.log(repo.getAll()); // OK
// console.log(repo.items); // Error - private

const newRepo = Repository.create<number>('numbers');
console.log(newRepo.name); // OK
""",
    )

    run_updater(typescript_classes_project, mock_ingestor)

    project_name = typescript_classes_project.name

    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = [
        f"{project_name}.access_modifiers.AccessModifierDemo",
        f"{project_name}.access_modifiers.ExtendedDemo",
        f"{project_name}.access_modifiers.SecureImplementation",
        f"{project_name}.access_modifiers.Repository",
    ]

    for expected in expected_classes:
        assert expected in created_classes, (
            f"Missing class with access modifiers: {expected}"
        )

    inheritance_relationships = get_relationships(mock_ingestor, "INHERITS")

    access_inheritance = [
        call
        for call in inheritance_relationships
        if "ExtendedDemo" in call.args[0][2] and "AccessModifierDemo" in call.args[2][2]
    ]

    assert len(access_inheritance) >= 1, (
        "Expected ExtendedDemo to inherit from AccessModifierDemo"
    )


def test_abstract_classes(
    typescript_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript abstract classes and methods."""
    test_file = typescript_classes_project / "abstract_classes.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// TypeScript abstract classes

abstract class Animal {
    protected name: string;
    protected age: number;

    constructor(name: string, age: number) {
        this.name = name;
        this.age = age;
    }

    // Concrete method
    public getName(): string {
        return this.name;
    }

    public getAge(): number {
        return this.age;
    }

    // Abstract methods - must be implemented by subclasses
    abstract makeSound(): string;
    abstract move(): string;

    // Abstract method with parameters
    abstract eat(food: string): boolean;

    // Concrete method using abstract methods
    public describe(): string {
        return `${this.name} is ${this.age} years old. ` +
               `It says "${this.makeSound()}" and ${this.move()}.`;
    }

    // Static method in abstract class
    static getSpecies(): string {
        return 'Unknown';
    }

    // Protected abstract method
    protected abstract getEnergyLevel(): number;

    // Concrete method using protected abstract
    public isActive(): boolean {
        return this.getEnergyLevel() > 50;
    }
}

// Concrete implementation
class Dog extends Animal {
    private breed: string;

    constructor(name: string, age: number, breed: string) {
        super(name, age);
        this.breed = breed;
    }

    // Implement abstract methods
    public makeSound(): string {
        return 'Woof!';
    }

    public move(): string {
        return 'runs on four legs';
    }

    public eat(food: string): boolean {
        return ['meat', 'kibble', 'bones'].includes(food.toLowerCase());
    }

    protected getEnergyLevel(): number {
        return this.age < 10 ? 80 : 60;
    }

    // Additional methods specific to Dog
    public getBreed(): string {
        return this.breed;
    }

    public wagTail(): string {
        return `${this.name} wags tail`;
    }

    // Override static method
    static getSpecies(): string {
        return 'Canis lupus';
    }
}

class Bird extends Animal {
    private wingspan: number;

    constructor(name: string, age: number, wingspan: number) {
        super(name, age);
        this.wingspan = wingspan;
    }

    public makeSound(): string {
        return 'Tweet!';
    }

    public move(): string {
        return 'flies through the air';
    }

    public eat(food: string): boolean {
        return ['seeds', 'insects', 'worms'].includes(food.toLowerCase());
    }

    protected getEnergyLevel(): number {
        return this.wingspan > 20 ? 90 : 70;
    }

    public getWingspan(): number {
        return this.wingspan;
    }

    public fly(): string {
        return `${this.name} spreads wings and flies`;
    }
}

// Abstract class with generic type
abstract class Repository<T> {
    protected items: T[] = [];

    // Abstract methods with generics
    abstract save(item: T): Promise<T>;
    abstract findById(id: string): Promise<T | null>;
    abstract delete(id: string): Promise<boolean>;

    // Concrete methods
    public getAll(): T[] {
        return [...this.items];
    }

    public count(): number {
        return this.items.length;
    }

    // Abstract method with complex signature
    abstract query(criteria: Partial<T>): Promise<T[]>;

    // Protected abstract for validation
    protected abstract validate(item: T): boolean;

    // Concrete method using abstract validation
    public add(item: T): boolean {
        if (this.validate(item)) {
            this.items.push(item);
            return true;
        }
        return false;
    }
}

// User model for repository
interface User {
    id: string;
    name: string;
    email: string;
    age: number;
}

// Concrete repository implementation
class UserRepository extends Repository<User> {
    async save(user: User): Promise<User> {
        // Simulate database save
        const existingIndex = this.items.findIndex(u => u.id === user.id);
        if (existingIndex >= 0) {
            this.items[existingIndex] = user;
        } else {
            this.items.push(user);
        }
        return user;
    }

    async findById(id: string): Promise<User | null> {
        const user = this.items.find(u => u.id === id);
        return user || null;
    }

    async delete(id: string): Promise<boolean> {
        const index = this.items.findIndex(u => u.id === id);
        if (index >= 0) {
            this.items.splice(index, 1);
            return true;
        }
        return false;
    }

    async query(criteria: Partial<User>): Promise<User[]> {
        return this.items.filter(user => {
            return Object.keys(criteria).every(key => {
                const criteriaKey = key as keyof User;
                return user[criteriaKey] === criteria[criteriaKey];
            });
        });
    }

    protected validate(user: User): boolean {
        return user.id?.length > 0 &&
               user.name?.length > 0 &&
               user.email?.includes('@');
    }

    // Additional methods for UserRepository
    async findByEmail(email: string): Promise<User | null> {
        const users = await this.query({ email });
        return users[0] || null;
    }

    async findByAgeRange(minAge: number, maxAge: number): Promise<User[]> {
        return this.items.filter(user =>
            user.age >= minAge && user.age <= maxAge
        );
    }
}

// Abstract class with abstract properties (using getters)
abstract class Vehicle {
    protected brand: string;

    constructor(brand: string) {
        this.brand = brand;
    }

    // Abstract getters (properties)
    abstract get maxSpeed(): number;
    abstract get fuelType(): string;

    // Abstract method
    abstract start(): string;
    abstract stop(): string;

    // Concrete method using abstract properties
    public getSpecs(): string {
        return `${this.brand}: ${this.maxSpeed} mph, ${this.fuelType}`;
    }
}

class Car extends Vehicle {
    private engine: string;

    constructor(brand: string, engine: string) {
        super(brand);
        this.engine = engine;
    }

    get maxSpeed(): number {
        return this.engine.includes('V8') ? 200 : 120;
    }

    get fuelType(): string {
        return 'Gasoline';
    }

    start(): string {
        return `${this.brand} car engine starts`;
    }

    stop(): string {
        return `${this.brand} car engine stops`;
    }
}

// Using abstract classes
const dog = new Dog('Rex', 5, 'Labrador');
const bird = new Bird('Tweety', 2, 15);

console.log(dog.describe());
console.log(dog.makeSound());
console.log(dog.eat('meat')); // true
console.log(dog.wagTail());

console.log(bird.describe());
console.log(bird.fly());
console.log(bird.eat('seeds')); // true

const userRepo = new UserRepository();
const user: User = {
    id: '1',
    name: 'Alice',
    email: 'alice@example.com',
    age: 30
};

userRepo.add(user);
userRepo.save(user);
console.log(userRepo.count()); // 1

const car = new Car('Toyota', 'V6');
console.log(car.getSpecs());
console.log(car.start());

// Cannot instantiate abstract class
// const animal = new Animal('Generic', 5); // Error
// const repo = new Repository<string>(); // Error
""",
    )

    run_updater(typescript_classes_project, mock_ingestor)

    project_name = typescript_classes_project.name

    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = [
        f"{project_name}.abstract_classes.Animal",
        f"{project_name}.abstract_classes.Dog",
        f"{project_name}.abstract_classes.Bird",
        f"{project_name}.abstract_classes.Repository",
        f"{project_name}.abstract_classes.UserRepository",
        f"{project_name}.abstract_classes.Vehicle",
        f"{project_name}.abstract_classes.Car",
    ]

    for expected in expected_classes:
        assert expected in created_classes, f"Missing abstract class: {expected}"

    inheritance_relationships = get_relationships(mock_ingestor, "INHERITS")

    abstract_inheritance = [
        call
        for call in inheritance_relationships
        if any(
            concrete in call.args[0][2]
            for concrete in ["Dog", "Bird", "UserRepository", "Car"]
        )
        and any(
            abstract in call.args[2][2]
            for abstract in ["Animal", "Repository", "Vehicle"]
        )
    ]

    assert len(abstract_inheritance) >= 2, (
        f"Expected at least 2 abstract class inheritance relationships, found {len(abstract_inheritance)}"
    )


def test_parameter_properties(
    typescript_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript parameter properties and constructor shortcuts."""
    test_file = typescript_classes_project / "parameter_properties.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// TypeScript parameter properties

class ParameterPropertiesDemo {
    // Traditional property declaration
    traditional: string;

    constructor(
        // Parameter properties - declared and assigned in constructor
        public name: string,
        private age: number,
        protected email: string,
        readonly id: string,
        public readonly createdAt: Date,
        private readonly hashedPassword: string,

        // Regular parameters (not properties)
        initialValue: string,
        options?: { debug?: boolean }
    ) {
        // Traditional assignment
        this.traditional = initialValue;

        // Parameter properties are automatically assigned
        // this.name = name; // Not needed - done automatically
        // this.age = age; // Not needed - done automatically

        // Can still access parameter properties in constructor
        console.log(`Creating user: ${this.name}`);

        if (options?.debug) {
            console.log('Debug mode enabled');
        }
    }

    // Methods can access parameter properties
    public getName(): string {
        return this.name; // public parameter property
    }

    public getAge(): number {
        return this.age; // private parameter property
    }

    public updateName(newName: string): void {
        this.name = newName; // public, so can be modified
    }

    private getEmail(): string {
        return this.email; // protected parameter property
    }

    public getInfo(): string {
        return `${this.name} (${this.age}): ${this.getEmail()}`;
    }

    public getId(): string {
        return this.id; // readonly parameter property
    }

    // Cannot modify readonly properties
    // public setId(id: string): void {
    //     this.id = id; // Error - readonly
    // }
}

// Inheritance with parameter properties
class ExtendedUser extends ParameterPropertiesDemo {
    constructor(
        // Can override and extend parameter properties
        name: string,
        age: number,
        email: string,
        id: string,
        createdAt: Date,
        hashedPassword: string,
        public role: string, // Additional parameter property
        private permissions: string[]
    ) {
        super(name, age, email, id, createdAt, hashedPassword, `Extended: ${name}`);
    }

    public getRole(): string {
        return this.role;
    }

    public hasPermission(permission: string): boolean {
        return this.permissions.includes(permission);
    }

    // Can access protected parameter property from parent
    public getContactInfo(): string {
        return `${this.name}: ${this.email}`; // email is protected
    }
}

// Generic class with parameter properties
class Container<T> {
    constructor(
        public readonly value: T,
        private metadata: Record<string, any> = {},
        public readonly timestamp: Date = new Date()
    ) {}

    public getValue(): T {
        return this.value;
    }

    public getMetadata(): Record<string, any> {
        return { ...this.metadata };
    }

    public addMetadata(key: string, value: any): void {
        this.metadata[key] = value;
    }
}

// Interface for parameter properties pattern
interface UserData {
    name: string;
    age: number;
    email: string;
}

class UserFromInterface implements UserData {
    constructor(
        public name: string,
        public age: number,
        public email: string,
        private internal: boolean = false
    ) {}

    public isInternal(): boolean {
        return this.internal;
    }
}

// Service class with dependency injection pattern
interface Logger {
    log(message: string): void;
}

interface Database {
    save(data: any): Promise<void>;
}

class UserService {
    constructor(
        private logger: Logger,
        private database: Database,
        public readonly config: { maxUsers: number }
    ) {}

    async createUser(userData: UserData): Promise<void> {
        this.logger.log(`Creating user: ${userData.name}`);

        if (this.config.maxUsers > 0) {
            await this.database.save(userData);
        }
    }
}

// Complex parameter properties with defaults and optionals
class ConfigurableService {
    constructor(
        public readonly name: string,
        private readonly config: {
            timeout: number;
            retries: number;
            debug: boolean;
        } = {
            timeout: 5000,
            retries: 3,
            debug: false
        },
        protected logger?: Logger,
        public readonly version: string = '1.0.0'
    ) {}

    public getConfig() {
        return { ...this.config };
    }

    protected log(message: string): void {
        if (this.logger) {
            this.logger.log(`[${this.name}] ${message}`);
        }
    }

    public process(): void {
        this.log(`Processing with version ${this.version}`);
    }
}

// Using parameter properties
const demo = new ParameterPropertiesDemo(
    'Alice',
    30,
    'alice@example.com',
    'user-123',
    new Date(),
    'hashed-secret',
    'Initial value',
    { debug: true }
);

console.log(demo.getName()); // Alice
console.log(demo.getId()); // user-123
console.log(demo.getInfo()); // Alice (30): alice@example.com
demo.updateName('Alice Smith');

const extended = new ExtendedUser(
    'Bob',
    25,
    'bob@example.com',
    'user-456',
    new Date(),
    'another-secret',
    'admin',
    ['read', 'write', 'delete']
);

console.log(extended.getRole()); // admin
console.log(extended.hasPermission('write')); // true
console.log(extended.getContactInfo()); // Bob: bob@example.com

const container = new Container<string>('Hello World', { type: 'greeting' });
console.log(container.getValue()); // Hello World
container.addMetadata('language', 'English');

const userFromInterface = new UserFromInterface('Charlie', 35, 'charlie@example.com');
console.log(userFromInterface.name); // Charlie (public property)

// Mock implementations for service
const mockLogger: Logger = { log: (msg) => console.log(msg) };
const mockDb: Database = { save: async (data) => console.log('Saved:', data) };

const userService = new UserService(mockLogger, mockDb, { maxUsers: 100 });
userService.createUser({ name: 'Dave', age: 40, email: 'dave@example.com' });

const service = new ConfigurableService('MyService');
service.process();
""",
    )

    run_updater(typescript_classes_project, mock_ingestor)

    project_name = typescript_classes_project.name

    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = [
        f"{project_name}.parameter_properties.ParameterPropertiesDemo",
        f"{project_name}.parameter_properties.ExtendedUser",
        f"{project_name}.parameter_properties.Container",
        f"{project_name}.parameter_properties.UserFromInterface",
        f"{project_name}.parameter_properties.UserService",
        f"{project_name}.parameter_properties.ConfigurableService",
    ]

    for expected in expected_classes:
        assert expected in created_classes, (
            f"Missing parameter properties class: {expected}"
        )

    method_calls = get_nodes(mock_ingestor, "Method")

    parameter_property_methods = [
        call
        for call in method_calls
        if "parameter_properties" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in ["getName", "getAge", "getValue", "getConfig"]
        )
    ]

    assert len(parameter_property_methods) >= 4, (
        f"Expected at least 4 parameter property accessor methods, found {len(parameter_property_methods)}"
    )


def test_typescript_class_comprehensive(
    typescript_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all TypeScript class features are covered."""
    test_file = typescript_classes_project / "comprehensive_classes.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every TypeScript class feature in one file

// Abstract class with access modifiers and generics
abstract class BaseService<T> {
    protected abstract readonly serviceName: string;

    constructor(
        protected readonly config: T,
        private readonly logger?: (msg: string) => void
    ) {}

    abstract process(data: T): Promise<T>;

    protected log(message: string): void {
        if (this.logger) {
            this.logger(`[${this.serviceName}] ${message}`);
        }
    }

    public getConfig(): T {
        return this.config;
    }

    static create<U>(config: U): BaseService<U> {
        throw new Error('Must be implemented by subclass');
    }
}

// Interface for implementation
interface ProcessingConfig {
    timeout: number;
    retries: number;
}

// Concrete implementation with all features
class ProcessingService extends BaseService<ProcessingConfig> {
    protected readonly serviceName = 'ProcessingService';

    constructor(
        config: ProcessingConfig,
        private readonly processor: (data: any) => any,
        logger?: (msg: string) => void
    ) {
        super(config, logger);
    }

    async process(data: ProcessingConfig): Promise<ProcessingConfig> {
        this.log('Starting process');

        try {
            const result = this.processor(data);
            this.log('Process completed');
            return result;
        } catch (error) {
            this.log(`Process failed: ${error}`);
            throw error;
        }
    }

    // Static factory method
    static create<ProcessingConfig>(
        config: ProcessingConfig
    ): ProcessingService {
        return new ProcessingService(
            config as any,
            (data) => data,
            (msg) => console.log(msg)
        );
    }

    // Getter/setter with access modifiers
    private _status: 'idle' | 'processing' | 'done' = 'idle';

    public get status(): string {
        return this._status;
    }

    private set status(value: 'idle' | 'processing' | 'done') {
        this._status = value;
    }
}

// Generic class with constraints
class Repository<T extends { id: string }> {
    constructor(
        private readonly items: Map<string, T> = new Map(),
        public readonly name: string
    ) {}

    public add(item: T): void {
        this.items.set(item.id, item);
    }

    public get(id: string): T | undefined {
        return this.items.get(id);
    }

    public getAll(): T[] {
        return Array.from(this.items.values());
    }

    // Static method with generics
    static empty<U extends { id: string }>(name: string): Repository<U> {
        return new Repository<U>(new Map(), name);
    }
}

// Interface to implement
interface Identifiable {
    id: string;
}

// Class implementing interface with parameter properties
class User implements Identifiable {
    constructor(
        public readonly id: string,
        public name: string,
        private email: string,
        protected readonly createdAt: Date = new Date()
    ) {}

    public getEmail(): string {
        return this.email;
    }

    public updateEmail(email: string): void {
        this.email = email;
    }

    // Static factory
    static fromJSON(json: any): User {
        return new User(json.id, json.name, json.email, new Date(json.createdAt));
    }
}

// Using all features
const service = ProcessingService.create({ timeout: 5000, retries: 3 });
const userRepo = Repository.empty<User>('users');

const user = new User('1', 'Alice', 'alice@example.com');
userRepo.add(user);

const retrieved = userRepo.get('1');
console.log(retrieved?.name); // Alice

// Process some data
service.process({ timeout: 1000, retries: 1 }).then(result => {
    console.log('Processed:', result);
});

console.log(service.status); // idle
console.log(userRepo.name); // users
""",
    )

    run_updater(typescript_classes_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    calls_relationships = get_relationships(mock_ingestor, "CALLS")
    [c for c in all_relationships if c.args[1] == "DEFINES"]
    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")
    implements_relationships = [
        c for c in all_relationships if c.args[1] == "IMPLEMENTS"
    ]

    comprehensive_calls = [
        call
        for call in calls_relationships
        if "comprehensive_classes" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 3, (
        f"Expected at least 3 comprehensive class calls, found {len(comprehensive_calls)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    comprehensive_classes = [
        call
        for call in class_calls
        if "comprehensive_classes" in call[0][1]["qualified_name"]
    ]

    assert len(comprehensive_classes) >= 4, (
        f"Expected at least 4 classes in comprehensive test, found {len(comprehensive_classes)}"
    )

    ts_inheritance = [
        call
        for call in inherits_relationships
        if "comprehensive_classes" in call.args[0][2]
    ]

    [
        call
        for call in implements_relationships
        if "comprehensive_classes" in call.args[0][2]
    ]

    assert len(ts_inheritance) >= 1, (
        f"Expected at least 1 inheritance relationship, found {len(ts_inheritance)}"
    )
