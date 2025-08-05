"""
Comprehensive TypeScript namespaces and modules parsing and testing.
Tests namespace declarations, module patterns, namespace merging, and module interop.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def typescript_namespaces_project(temp_repo: Path) -> Path:
    """Create a comprehensive TypeScript project with namespace/module patterns."""
    project_path = temp_repo / "typescript_namespaces_test"
    project_path.mkdir()

    # Create directory structure
    (project_path / "namespaces").mkdir()
    (project_path / "modules").mkdir()
    (project_path / "types").mkdir()

    # Create base files
    (project_path / "types" / "common.ts").write_text(
        """
export namespace Common {
    export interface User {
        id: string;
        name: string;
    }

    export function createUser(id: string, name: string): User {
        return { id, name };
    }
}
"""
    )

    return project_path


def test_namespace_declarations(
    typescript_namespaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript namespace declarations and structure."""
    test_file = typescript_namespaces_project / "namespace_declarations.ts"
    test_file.write_text(
        r"""
// Basic namespace declarations

// Simple namespace
namespace Utilities {
    export function formatString(str: string): string {
        return str.trim().toLowerCase();
    }

    export function parseNumber(str: string): number {
        return parseInt(str, 10);
    }

    export const VERSION = '1.0.0';

    // Internal (non-exported) function
    function internalHelper(value: string): string {
        return value.toUpperCase();
    }

    export function processString(input: string): string {
        return internalHelper(formatString(input));
    }
}

// Nested namespaces
namespace DataProcessing {
    export namespace Validators {
        export function isEmail(email: string): boolean {
            return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
        }

        export function isPhoneNumber(phone: string): boolean {
            return /^\+?[\d\s-()]+$/.test(phone);
        }

        export function isRequired(value: any): boolean {
            return value !== null && value !== undefined && value !== '';
        }
    }

    export namespace Formatters {
        export function formatCurrency(amount: number, currency = 'USD'): string {
            return new Intl.NumberFormat('en-US', {
                style: 'currency',
                currency
            }).format(amount);
        }

        export function formatDate(date: Date, locale = 'en-US'): string {
            return date.toLocaleDateString(locale);
        }

        export function formatPercentage(value: number): string {
            return `${(value * 100).toFixed(2)}%`;
        }
    }

    export namespace Transformers {
        export function toSnakeCase(str: string): string {
            return str.replace(/([A-Z])/g, '_$1').toLowerCase();
        }

        export function toCamelCase(str: string): string {
            return str.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
        }

        export function capitalize(str: string): string {
            return str.charAt(0).toUpperCase() + str.slice(1);
        }
    }

    // Function using nested namespaces
    export function processUserData(data: any): any {
        const isValidEmail = Validators.isEmail(data.email);
        const formattedName = Transformers.capitalize(data.name);
        const formattedDate = Formatters.formatDate(new Date());

        return {
            ...data,
            name: formattedName,
            emailValid: isValidEmail,
            processedAt: formattedDate
        };
    }
}

// Namespace with interfaces and classes
namespace Models {
    export interface BaseEntity {
        id: string;
        createdAt: Date;
        updatedAt: Date;
    }

    export interface User extends BaseEntity {
        name: string;
        email: string;
        role: UserRole;
    }

    export enum UserRole {
        Admin = 'admin',
        User = 'user',
        Guest = 'guest'
    }

    export class UserModel implements User {
        id: string;
        createdAt: Date;
        updatedAt: Date;
        name: string;
        email: string;
        role: UserRole;

        constructor(name: string, email: string, role: UserRole = UserRole.User) {
            this.id = Math.random().toString(36).substr(2, 9);
            this.createdAt = new Date();
            this.updatedAt = new Date();
            this.name = name;
            this.email = email;
            this.role = role;
        }

        updateName(name: string): void {
            this.name = name;
            this.updatedAt = new Date();
        }

        updateEmail(email: string): void {
            this.email = email;
            this.updatedAt = new Date();
        }

        hasRole(role: UserRole): boolean {
            return this.role === role;
        }

        isAdmin(): boolean {
            return this.hasRole(UserRole.Admin);
        }
    }

    export namespace Repository {
        const users: UserModel[] = [];

        export function create(name: string, email: string, role?: UserRole): UserModel {
            const user = new UserModel(name, email, role);
            users.push(user);
            return user;
        }

        export function findById(id: string): UserModel | undefined {
            return users.find(user => user.id === id);
        }

        export function findByEmail(email: string): UserModel | undefined {
            return users.find(user => user.email === email);
        }

        export function getAll(): UserModel[] {
            return [...users];
        }

        export function remove(id: string): boolean {
            const index = users.findIndex(user => user.id === id);
            if (index !== -1) {
                users.splice(index, 1);
                return true;
            }
            return false;
        }
    }
}

// Namespace with generics
namespace Collections {
    export interface Collection<T> {
        items: T[];
        size(): number;
        add(item: T): void;
        remove(item: T): boolean;
        contains(item: T): boolean;
        clear(): void;
    }

    export class ArrayList<T> implements Collection<T> {
        items: T[] = [];

        size(): number {
            return this.items.length;
        }

        add(item: T): void {
            this.items.push(item);
        }

        remove(item: T): boolean {
            const index = this.items.indexOf(item);
            if (index !== -1) {
                this.items.splice(index, 1);
                return true;
            }
            return false;
        }

        contains(item: T): boolean {
            return this.items.includes(item);
        }

        clear(): void {
            this.items = [];
        }

        get(index: number): T | undefined {
            return this.items[index];
        }

        toArray(): T[] {
            return [...this.items];
        }
    }

    export class HashSet<T> implements Collection<T> {
        private set = new Set<T>();

        get items(): T[] {
            return Array.from(this.set);
        }

        size(): number {
            return this.set.size;
        }

        add(item: T): void {
            this.set.add(item);
        }

        remove(item: T): boolean {
            return this.set.delete(item);
        }

        contains(item: T): boolean {
            return this.set.has(item);
        }

        clear(): void {
            this.set.clear();
        }

        forEach(callback: (item: T) => void): void {
            this.set.forEach(callback);
        }
    }

    export namespace Utils {
        export function merge<T>(...collections: Collection<T>[]): Collection<T> {
            const result = new ArrayList<T>();
            collections.forEach(collection => {
                collection.items.forEach(item => result.add(item));
            });
            return result;
        }

        export function filter<T>(collection: Collection<T>, predicate: (item: T) => boolean): Collection<T> {
            const result = new ArrayList<T>();
            collection.items.forEach(item => {
                if (predicate(item)) {
                    result.add(item);
                }
            });
            return result;
        }

        export function map<T, U>(collection: Collection<T>, mapper: (item: T) => U): Collection<U> {
            const result = new ArrayList<U>();
            collection.items.forEach(item => {
                result.add(mapper(item));
            });
            return result;
        }
    }
}

// Using namespaces
const formattedString = Utilities.formatString('  Hello World  ');
const parsedNumber = Utilities.parseNumber('42');

console.log(Utilities.VERSION); // 1.0.0
console.log(formattedString); // hello world
console.log(parsedNumber); // 42

// Using nested namespaces
const isValidEmail = DataProcessing.Validators.isEmail('user@example.com');
const formattedCurrency = DataProcessing.Formatters.formatCurrency(1234.56);
const snakeCase = DataProcessing.Transformers.toSnakeCase('camelCaseString');

console.log(isValidEmail); // true
console.log(formattedCurrency); // $1,234.56
console.log(snakeCase); // camel_case_string

// Using models
const user = new Models.UserModel('Alice', 'alice@example.com', Models.UserRole.Admin);
const savedUser = Models.Repository.create('Bob', 'bob@example.com');

console.log(user.isAdmin()); // true
console.log(Models.Repository.getAll().length); // 1

// Using collections
const list = new Collections.ArrayList<string>();
list.add('item1');
list.add('item2');

const set = new Collections.HashSet<number>();
set.add(1);
set.add(2);
set.add(1); // Duplicate

console.log(list.size()); // 2
console.log(set.size()); // 2

const merged = Collections.Utils.merge(list, new Collections.ArrayList<string>());
console.log(merged.size()); // 2
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=typescript_namespaces_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # TypeScript namespaces might be captured as special namespace nodes or as modules
    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    namespace_like_nodes = [
        call
        for call in all_nodes
        if call[0][0] in ["Namespace", "Module", "Class", "Interface"]
        and "namespace_declarations" in call[0][1].get("qualified_name", "")
        and any(
            ns_name in call[0][1].get("qualified_name", "")
            for ns_name in ["Utilities", "DataProcessing", "Models", "Collections"]
        )
    ]

    assert len(namespace_like_nodes) >= 3, (
        f"Expected at least 3 namespace-like nodes, found {len(namespace_like_nodes)}"
    )

    # Check for functions within namespaces
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0]
        in ["Function", "Method"]  # Namespace functions are detected as methods
    ]

    namespace_functions = [
        call
        for call in function_calls
        if "namespace_declarations" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in [
                "formatString",
                "isEmail",
                "formatCurrency",
                "processUserData",
            ]
        )
    ]

    assert len(namespace_functions) >= 3, (
        f"Expected at least 3 namespace functions, found {len(namespace_functions)}"
    )

    # Check for classes within namespaces
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    namespace_classes = [
        call
        for call in class_calls
        if "namespace_declarations" in call[0][1]["qualified_name"]
        and any(
            class_name in call[0][1]["qualified_name"]
            for class_name in ["UserModel", "ArrayList", "HashSet"]
        )
    ]

    assert len(namespace_classes) >= 2, (
        f"Expected at least 2 namespace classes, found {len(namespace_classes)}"
    )


def test_namespace_merging(
    typescript_namespaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript namespace merging and declaration merging."""
    test_file = typescript_namespaces_project / "namespace_merging.ts"
    test_file.write_text(
        r"""
// Namespace merging examples

// First declaration of Utils namespace
namespace Utils {
    export function stringUtils(str: string): string {
        return str.trim();
    }

    export const STRING_CONSTANT = 'string';
}

// Second declaration of Utils namespace (will be merged)
namespace Utils {
    export function numberUtils(num: number): number {
        return Math.round(num);
    }

    export const NUMBER_CONSTANT = 42;
}

// Third declaration of Utils namespace
namespace Utils {
    export function arrayUtils<T>(arr: T[]): T[] {
        return [...arr].reverse();
    }

    export const ARRAY_CONSTANT = [];

    // Nested namespace in merged namespace
    export namespace DateUtils {
        export function formatDate(date: Date): string {
            return date.toISOString();
        }

        export function parseDate(dateString: string): Date {
            return new Date(dateString);
        }

        export const DATE_FORMAT = 'YYYY-MM-DD';
    }
}

// Interface and namespace merging
interface User {
    id: string;
    name: string;
}

namespace User {
    export function create(id: string, name: string): User {
        return { id, name };
    }

    export function isValid(user: User): boolean {
        return user.id.length > 0 && user.name.length > 0;
    }

    export const DEFAULT_USER: User = { id: 'default', name: 'Default User' };
}

// Additional interface declaration (will be merged)
interface User {
    email?: string;
    createdAt?: Date;
}

// Additional namespace declaration (will be merged)
namespace User {
    export function createWithEmail(id: string, name: string, email: string): User {
        return { id, name, email, createdAt: new Date() };
    }

    export function hasEmail(user: User): boolean {
        return !!user.email;
    }

    export const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
}

// Enum and namespace merging
enum Status {
    Active = 'active',
    Inactive = 'inactive'
}

namespace Status {
    export function isActive(status: Status): boolean {
        return status === Status.Active;
    }

    export function toggle(status: Status): Status {
        return status === Status.Active ? Status.Inactive : Status.Active;
    }

    export const ALL_STATUSES = [Status.Active, Status.Inactive];
}

// Class and namespace merging
class Calculator {
    private result: number = 0;

    add(value: number): this {
        this.result += value;
        return this;
    }

    subtract(value: number): this {
        this.result -= value;
        return this;
    }

    getResult(): number {
        return this.result;
    }

    reset(): this {
        this.result = 0;
        return this;
    }
}

namespace Calculator {
    export function create(): Calculator {
        return new Calculator();
    }

    export function createWithValue(initialValue: number): Calculator {
        return new Calculator().add(initialValue);
    }

    export const PI = Math.PI;
    export const E = Math.E;

    export namespace MathUtils {
        export function factorial(n: number): number {
            if (n <= 1) return 1;
            return n * factorial(n - 1);
        }

        export function fibonacci(n: number): number {
            if (n <= 1) return n;
            return fibonacci(n - 1) + fibonacci(n - 2);
        }

        export function isPrime(n: number): boolean {
            if (n <= 1) return false;
            for (let i = 2; i * i <= n; i++) {
                if (n % i === 0) return false;
            }
            return true;
        }
    }
}

// Function and namespace merging
function Logger(message: string): void {
    console.log(`[LOG] ${message}`);
}

namespace Logger {
    export function info(message: string): void {
        console.log(`[INFO] ${message}`);
    }

    export function warn(message: string): void {
        console.warn(`[WARN] ${message}`);
    }

    export function error(message: string): void {
        console.error(`[ERROR] ${message}`);
    }

    export function debug(message: string): void {
        console.debug(`[DEBUG] ${message}`);
    }

    export enum Level {
        Debug = 0,
        Info = 1,
        Warn = 2,
        Error = 3
    }

    export interface LogEntry {
        message: string;
        level: Level;
        timestamp: Date;
    }

    let currentLevel: Level = Level.Info;

    export function setLevel(level: Level): void {
        currentLevel = level;
    }

    export function getLevel(): Level {
        return currentLevel;
    }

    export function log(message: string, level: Level): void {
        if (level >= currentLevel) {
            const entry: LogEntry = {
                message,
                level,
                timestamp: new Date()
            };

            switch (level) {
                case Level.Debug:
                    debug(entry.message);
                    break;
                case Level.Info:
                    info(entry.message);
                    break;
                case Level.Warn:
                    warn(entry.message);
                    break;
                case Level.Error:
                    error(entry.message);
                    break;
            }
        }
    }
}

// Complex merging scenario
namespace API {
    export interface RequestConfig {
        url: string;
        method: string;
        headers?: Record<string, string>;
    }

    export function request(config: RequestConfig): Promise<Response> {
        return fetch(config.url, {
            method: config.method,
            headers: config.headers
        });
    }
}

namespace API {
    export interface RequestConfig {
        timeout?: number;
        retries?: number;
    }

    export async function get(url: string, config?: Partial<RequestConfig>): Promise<Response> {
        return request({
            url,
            method: 'GET',
            ...config
        });
    }

    export async function post(url: string, data: any, config?: Partial<RequestConfig>): Promise<Response> {
        return request({
            url,
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            ...config
        });
    }
}

namespace API {
    export namespace Http {
        export const GET = 'GET';
        export const POST = 'POST';
        export const PUT = 'PUT';
        export const DELETE = 'DELETE';

        export function isValidMethod(method: string): boolean {
            return [GET, POST, PUT, DELETE].includes(method);
        }
    }

    export namespace Status {
        export const OK = 200;
        export const NOT_FOUND = 404;
        export const SERVER_ERROR = 500;

        export function isSuccess(status: number): boolean {
            return status >= 200 && status < 300;
        }

        export function isError(status: number): boolean {
            return status >= 400;
        }
    }
}

// Using merged namespaces
console.log(Utils.stringUtils('  test  ')); // 'test'
console.log(Utils.numberUtils(3.7)); // 4
console.log(Utils.arrayUtils([1, 2, 3])); // [3, 2, 1]
console.log(Utils.DateUtils.formatDate(new Date()));

const user = User.create('1', 'Alice');
const userWithEmail = User.createWithEmail('2', 'Bob', 'bob@example.com');
console.log(User.isValid(user)); // true
console.log(User.hasEmail(userWithEmail)); // true

console.log(Status.isActive(Status.Active)); // true
console.log(Status.toggle(Status.Active)); // Status.Inactive

const calc = Calculator.create();
calc.add(10).subtract(3);
console.log(calc.getResult()); // 7

console.log(Calculator.MathUtils.factorial(5)); // 120
console.log(Calculator.MathUtils.fibonacci(7)); // 13

Logger('Basic log');
Logger.info('Info message');
Logger.setLevel(Logger.Level.Warn);
Logger.log('Debug message', Logger.Level.Debug); // Won't show

API.get('https://api.example.com/users')
    .then(response => {
        console.log('Status:', response.status);
        console.log('Success:', API.Status.isSuccess(response.status));
    });

console.log('Valid method:', API.Http.isValidMethod('GET')); // true
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=typescript_namespaces_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Check for merged namespace functionality
    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    merged_namespace_nodes = [
        call
        for call in all_nodes
        if "namespace_merging" in call[0][1].get("qualified_name", "")
        and any(
            ns_name in call[0][1].get("qualified_name", "")
            for ns_name in ["Utils", "User", "Status", "Calculator", "Logger", "API"]
        )
    ]

    assert len(merged_namespace_nodes) >= 4, (
        f"Expected at least 4 merged namespace-related nodes, found {len(merged_namespace_nodes)}"
    )

    # Check for merged functions
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    merged_functions = [
        call
        for call in function_calls
        if "namespace_merging" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in [
                "stringUtils",
                "numberUtils",
                "arrayUtils",
                "createWithEmail",
            ]
        )
    ]

    assert len(merged_functions) >= 3, (
        f"Expected at least 3 functions from merged namespaces, found {len(merged_functions)}"
    )

    # Check for interface/enum/class merging
    interface_calls = [
        call
        for call in all_nodes
        if call[0][0] == "Interface"
        and "namespace_merging" in call[0][1].get("qualified_name", "")
    ]

    enum_calls = [
        call
        for call in all_nodes
        if call[0][0] == "Enum"
        and "namespace_merging" in call[0][1].get("qualified_name", "")
    ]

    class_calls = [
        call
        for call in all_nodes
        if call[0][0] == "Class"
        and "namespace_merging" in call[0][1].get("qualified_name", "")
    ]

    merging_nodes = len(interface_calls) + len(enum_calls) + len(class_calls)

    assert merging_nodes >= 2, (
        f"Expected at least 2 interface/enum/class nodes for merging, found {merging_nodes}"
    )


def test_module_patterns(
    typescript_namespaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript module patterns and ES6 modules."""
    test_file = typescript_namespaces_project / "module_patterns.ts"
    test_file.write_text(
        r"""
// Module patterns in TypeScript

// Internal module (namespace)
namespace InternalUtils {
    export function formatName(firstName: string, lastName: string): string {
        return `${firstName} ${lastName}`.trim();
    }

    export function validateEmail(email: string): boolean {
        return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
    }

    export const VALIDATION_MESSAGES = {
        REQUIRED: 'This field is required',
        INVALID_EMAIL: 'Please enter a valid email address',
        TOO_SHORT: 'This field is too short'
    };

    // Private to namespace
    function sanitizeInput(input: string): string {
        return input.replace(/[<>]/g, '');
    }

    export function processInput(input: string): string {
        return sanitizeInput(input).trim();
    }
}

// Module with export declarations
export namespace ExportedUtils {
    export interface ValidationResult {
        isValid: boolean;
        errors: string[];
    }

    export class Validator {
        private rules: Array<(value: any) => string | null> = [];

        addRule(rule: (value: any) => string | null): this {
            this.rules.push(rule);
            return this;
        }

        validate(value: any): ValidationResult {
            const errors: string[] = [];

            for (const rule of this.rules) {
                const error = rule(value);
                if (error) {
                    errors.push(error);
                }
            }

            return {
                isValid: errors.length === 0,
                errors
            };
        }

        static required(value: any): string | null {
            return value == null || value === '' ? 'This field is required' : null;
        }

        static minLength(min: number) {
            return (value: string): string | null => {
                return value && value.length < min ? `Minimum length is ${min}` : null;
            };
        }

        static email(value: string): string | null {
            const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
            return value && !emailRegex.test(value) ? 'Invalid email format' : null;
        }
    }

    export function createEmailValidator(): Validator {
        return new Validator()
            .addRule(Validator.required)
            .addRule(Validator.email);
    }

    export function createPasswordValidator(): Validator {
        return new Validator()
            .addRule(Validator.required)
            .addRule(Validator.minLength(8));
    }

    export namespace Presets {
        export const EMAIL_VALIDATOR = createEmailValidator();
        export const PASSWORD_VALIDATOR = createPasswordValidator();

        export function validateUser(userData: { email: string; password: string }): {
            email: ValidationResult;
            password: ValidationResult;
        } {
            return {
                email: EMAIL_VALIDATOR.validate(userData.email),
                password: PASSWORD_VALIDATOR.validate(userData.password)
            };
        }
    }
}

// Ambient module declaration
declare namespace ThirdPartyLib {
    interface Config {
        apiKey: string;
        endpoint: string;
    }

    function initialize(config: Config): void;
    function getData(id: string): Promise<any>;

    namespace Utils {
        function format(data: any): string;
        function parse(str: string): any;
    }
}

// Module augmentation pattern
namespace GlobalExtensions {
    export interface StringExtensions {
        toCamelCase(): string;
        toSnakeCase(): string;
        capitalize(): string;
    }

    export interface ArrayExtensions<T> {
        last(): T | undefined;
        first(): T | undefined;
        shuffle(): T[];
    }
}

// Extending global types (simulated)
interface String extends GlobalExtensions.StringExtensions {}
interface Array<T> extends GlobalExtensions.ArrayExtensions<T> {}

// Implementation of extensions (would typically be in separate files)
namespace StringUtils {
    export function toCamelCase(str: string): string {
        return str.replace(/[-_](.)/g, (_, char) => char.toUpperCase());
    }

    export function toSnakeCase(str: string): string {
        return str.replace(/([A-Z])/g, '_$1').toLowerCase();
    }

    export function capitalize(str: string): string {
        return str.charAt(0).toUpperCase() + str.slice(1).toLowerCase();
    }
}

namespace ArrayUtils {
    export function last<T>(arr: T[]): T | undefined {
        return arr[arr.length - 1];
    }

    export function first<T>(arr: T[]): T | undefined {
        return arr[0];
    }

    export function shuffle<T>(arr: T[]): T[] {
        const result = [...arr];
        for (let i = result.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [result[i], result[j]] = [result[j], result[i]];
        }
        return result;
    }
}

// UMD pattern simulation
namespace UMDModule {
    export interface ModuleExports {
        createService: (config: any) => Service;
        Service: typeof Service;
        version: string;
    }

    export class Service {
        private config: any;

        constructor(config: any) {
            this.config = config;
        }

        start(): void {
            console.log('Service starting with config:', this.config);
        }

        stop(): void {
            console.log('Service stopping');
        }

        getStatus(): string {
            return 'running';
        }
    }

    export function createService(config: any): Service {
        return new Service(config);
    }

    export const version = '1.0.0';

    // Export for different module systems
    export const moduleExports: ModuleExports = {
        createService,
        Service,
        version
    };
}

// Triple-slash directives simulation
/// <reference types="node" />

namespace NodeUtils {
    export interface ProcessInfo {
        pid: number;
        platform: string;
        version: string;
    }

    export function getProcessInfo(): ProcessInfo {
        return {
            pid: process?.pid || 0,
            platform: process?.platform || 'unknown',
            version: process?.version || '0.0.0'
        };
    }

    export function getEnvironmentVariable(name: string): string | undefined {
        return process?.env?.[name];
    }

    export function setEnvironmentVariable(name: string, value: string): void {
        if (process?.env) {
            process.env[name] = value;
        }
    }
}

// Module with conditional exports
namespace ConditionalModule {
    export const IS_BROWSER = typeof window !== 'undefined';
    export const IS_NODE = typeof process !== 'undefined' && process.versions?.node;

    export namespace Browser {
        export function getWindowSize(): { width: number; height: number } {
            if (!IS_BROWSER) {
                throw new Error('Browser environment required');
            }
            return {
                width: window.innerWidth,
                height: window.innerHeight
            };
        }

        export function redirectTo(url: string): void {
            if (IS_BROWSER) {
                window.location.href = url;
            }
        }
    }

    export namespace Node {
        export function getProcessId(): number {
            if (!IS_NODE) {
                throw new Error('Node.js environment required');
            }
            return process.pid;
        }

        export function exit(code: number = 0): void {
            if (IS_NODE) {
                process.exit(code);
            }
        }
    }

    export function getPlatformInfo(): { platform: string; runtime: string } {
        if (IS_BROWSER) {
            return { platform: 'browser', runtime: 'browser' };
        } else if (IS_NODE) {
            return { platform: process.platform, runtime: 'node' };
        } else {
            return { platform: 'unknown', runtime: 'unknown' };
        }
    }
}

// Using internal namespace
const fullName = InternalUtils.formatName('John', 'Doe');
const isValidEmail = InternalUtils.validateEmail('test@example.com');
const processedInput = InternalUtils.processInput('<script>alert("xss")</script>');

console.log(fullName); // 'John Doe'
console.log(isValidEmail); // true
console.log(processedInput); // 'alert("xss")'

// Using exported namespace
const emailValidator = ExportedUtils.createEmailValidator();
const result = emailValidator.validate('user@example.com');
console.log(result.isValid); // true

const userValidation = ExportedUtils.Presets.validateUser({
    email: 'test@example.com',
    password: 'password123'
});

console.log(userValidation.email.isValid); // true
console.log(userValidation.password.isValid); // true

// Using utility functions
const camelCase = StringUtils.toCamelCase('hello-world');
const snakeCase = StringUtils.toSnakeCase('HelloWorld');
const capitalized = StringUtils.capitalize('hello');

console.log(camelCase); // 'helloWorld'
console.log(snakeCase); // 'hello_world'
console.log(capitalized); // 'Hello'

const numbers = [1, 2, 3, 4, 5];
const lastNumber = ArrayUtils.last(numbers);
const shuffled = ArrayUtils.shuffle(numbers);

console.log(lastNumber); // 5
console.log(shuffled); // Random order

// Using UMD module
const service = UMDModule.createService({ host: 'localhost', port: 3000 });
service.start();

console.log(UMDModule.version); // '1.0.0'

// Platform-specific usage
const platformInfo = ConditionalModule.getPlatformInfo();
console.log(platformInfo);

if (ConditionalModule.IS_NODE) {
    console.log('Process ID:', ConditionalModule.Node.getProcessId());
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=typescript_namespaces_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Check for module patterns
    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    module_pattern_nodes = [
        call
        for call in all_nodes
        if "module_patterns" in call[0][1].get("qualified_name", "")
        and any(
            pattern in call[0][1].get("qualified_name", "")
            for pattern in [
                "InternalUtils",
                "ExportedUtils",
                "UMDModule",
                "ConditionalModule",
            ]
        )
    ]

    assert len(module_pattern_nodes) >= 3, (
        f"Expected at least 3 module pattern nodes, found {len(module_pattern_nodes)}"
    )

    # Check for exported classes and interfaces
    class_calls = [
        call
        for call in all_nodes
        if call[0][0] == "Class"
        and "module_patterns" in call[0][1].get("qualified_name", "")
    ]

    interface_calls = [
        call
        for call in all_nodes
        if call[0][0] == "Interface"
        and "module_patterns" in call[0][1].get("qualified_name", "")
    ]

    module_classes_interfaces = len(class_calls) + len(interface_calls)

    assert module_classes_interfaces >= 2, (
        f"Expected at least 2 classes/interfaces in modules, found {module_classes_interfaces}"
    )

    # Check for utility functions
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    module_functions = [
        call
        for call in function_calls
        if "module_patterns" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in ["formatName", "createEmailValidator", "createService"]
        )
    ]

    assert len(module_functions) >= 3, (
        f"Expected at least 3 module functions, found {len(module_functions)}"
    )


def test_typescript_namespaces_comprehensive(
    typescript_namespaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all TypeScript namespace/module patterns are covered."""
    test_file = typescript_namespaces_project / "comprehensive_namespaces.ts"
    test_file.write_text(
        """
// Every TypeScript namespace/module pattern in one file

// Basic namespace
namespace Basic {
    export function helper(): string {
        return 'basic';
    }
}

// Nested namespace
namespace Nested {
    export namespace Inner {
        export function helper(): string {
            return 'nested';
        }
    }
}

// Namespace merging
namespace Merged {
    export function first(): string {
        return 'first';
    }
}

namespace Merged {
    export function second(): string {
        return 'second';
    }
}

// Interface and namespace merging
interface Entity {
    id: string;
}

namespace Entity {
    export function create(id: string): Entity {
        return { id };
    }
}

// Exported namespace
export namespace Exported {
    export function utility(): void {
        console.log('exported utility');
    }

    export class Helper {
        help(): string {
            return 'helping';
        }
    }
}

// Module with conditional export
export namespace Conditional {
    export const IS_DEV = process.env.NODE_ENV === 'development';

    export function log(message: string): void {
        if (IS_DEV) {
            console.log(message);
        }
    }
}

// Using all patterns
console.log(Basic.helper());
console.log(Nested.Inner.helper());
console.log(Merged.first());
console.log(Merged.second());

const entity = Entity.create('123');
console.log(entity.id);

Exported.utility();
const helper = new Exported.Helper();
console.log(helper.help());

Conditional.log('Debug message');
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=typescript_namespaces_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Verify all relationship types exist
    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    calls_relationships = [c for c in all_relationships if c.args[1] == "CALLS"]
    [c for c in all_relationships if c.args[1] == "DEFINES"]

    # Should have comprehensive namespace-related calls
    comprehensive_calls = [
        call
        for call in calls_relationships
        if "comprehensive_namespaces" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 5, (
        f"Expected at least 5 comprehensive namespace calls, found {len(comprehensive_calls)}"
    )

    # Check all namespace patterns were created
    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    comprehensive_namespaces = [
        call
        for call in all_nodes
        if "comprehensive_namespaces" in call[0][1].get("qualified_name", "")
        and any(
            ns_name in call[0][1].get("qualified_name", "")
            for ns_name in [
                "Basic",
                "Nested",
                "Merged",
                "Entity",
                "Exported",
                "Conditional",
            ]
        )
    ]

    assert len(comprehensive_namespaces) >= 4, (
        f"Expected at least 4 namespace patterns, found {len(comprehensive_namespaces)}"
    )
