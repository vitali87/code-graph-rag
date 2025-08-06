"""
Comprehensive TypeScript declaration files (.d.ts) parsing and testing.
Tests ambient declarations, module declarations, global augmentations, and declaration file patterns.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def typescript_declarations_project(temp_repo: Path) -> Path:
    """Create a comprehensive TypeScript project with declaration file patterns."""
    project_path = temp_repo / "typescript_declarations_test"
    project_path.mkdir()

    # Create directory structure
    (project_path / "types").mkdir()
    (project_path / "lib").mkdir()
    (project_path / "external").mkdir()

    # Create base declaration file
    (project_path / "types" / "common.d.ts").write_text(
        """
// Common type declarations
declare namespace Common {
    interface BaseEntity {
        id: string;
        createdAt: Date;
        updatedAt: Date;
    }

    type EntityType = 'user' | 'product' | 'order';

    function createEntity<T extends BaseEntity>(type: EntityType, data: Omit<T, keyof BaseEntity>): T;
}

declare module 'common-types' {
    export = Common;
}
"""
    )

    return project_path


def test_ambient_declarations(
    typescript_declarations_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript ambient declarations."""
    test_file = typescript_declarations_project / "ambient_declarations.d.ts"
    test_file.write_text(
        """
// Ambient declarations for external libraries and global variables

// Global variables
declare var VERSION: string;
declare const API_URL: string;
declare let DEBUG_MODE: boolean;

// Global functions
declare function log(message: string): void;
declare function parseJSON<T>(json: string): T;
declare function formatDate(date: Date, format?: string): string;

// Global interfaces
declare interface Window {
    customProperty: string;
    customMethod(): void;
}

declare interface NodeJS {
    Global: {
        customGlobal: any;
    };
}

// Ambient namespace
declare namespace jQuery {
    interface JQuery {
        customPlugin(options?: any): JQuery;
    }

    function ajax(settings: AjaxSettings): XMLHttpRequest;

    interface AjaxSettings {
        url: string;
        method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
        data?: any;
        success?: (data: any) => void;
        error?: (xhr: XMLHttpRequest, status: string) => void;
    }
}

// Ambient class
declare class EventEmitter {
    on(event: string, listener: (...args: any[]) => void): this;
    emit(event: string, ...args: any[]): boolean;
    off(event: string, listener: (...args: any[]) => void): this;
    removeAllListeners(event?: string): this;
}

// Ambient enum
declare enum LogLevel {
    Debug = 0,
    Info = 1,
    Warn = 2,
    Error = 3
}

// Ambient module with export assignment
declare module "lodash" {
    interface LoDashStatic {
        map<T, U>(collection: T[], iteratee: (item: T) => U): U[];
        filter<T>(collection: T[], predicate: (item: T) => boolean): T[];
        find<T>(collection: T[], predicate: (item: T) => boolean): T | undefined;
        reduce<T, U>(collection: T[], iteratee: (acc: U, item: T) => U, initial: U): U;

        // Utility functions
        isArray(value: any): value is any[];
        isString(value: any): value is string;
        isNumber(value: any): value is number;
        isObject(value: any): value is object;

        // Object utilities
        keys(obj: object): string[];
        values<T>(obj: Record<string, T>): T[];
        merge<T, U>(target: T, source: U): T & U;
        clone<T>(obj: T): T;

        // String utilities
        capitalize(str: string): string;
        kebabCase(str: string): string;
        camelCase(str: string): string;
        snakeCase(str: string): string;
    }

    const _: LoDashStatic;
    export = _;
}

// Ambient module with named exports
declare module "axios" {
    export interface AxiosRequestConfig {
        url?: string;
        method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
        headers?: Record<string, string>;
        data?: any;
        params?: Record<string, any>;
        timeout?: number;
    }

    export interface AxiosResponse<T = any> {
        data: T;
        status: number;
        statusText: string;
        headers: Record<string, string>;
        config: AxiosRequestConfig;
    }

    export interface AxiosError {
        message: string;
        config: AxiosRequestConfig;
        code?: string;
        request?: any;
        response?: AxiosResponse;
    }

    export interface AxiosInstance {
        request<T = any>(config: AxiosRequestConfig): Promise<AxiosResponse<T>>;
        get<T = any>(url: string, config?: AxiosRequestConfig): Promise<AxiosResponse<T>>;
        post<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<AxiosResponse<T>>;
        put<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<AxiosResponse<T>>;
        delete<T = any>(url: string, config?: AxiosRequestConfig): Promise<AxiosResponse<T>>;
    }

    export function create(config?: AxiosRequestConfig): AxiosInstance;

    const axios: AxiosInstance;
    export default axios;
}

// Ambient module for Node.js modules
declare module "fs" {
    export interface Stats {
        isFile(): boolean;
        isDirectory(): boolean;
        isSymbolicLink(): boolean;
        size: number;
        mtime: Date;
        ctime: Date;
    }

    export function readFile(path: string, encoding: string, callback: (err: Error | null, data: string) => void): void;
    export function readFile(path: string, callback: (err: Error | null, data: Buffer) => void): void;
    export function readFileSync(path: string, encoding: string): string;
    export function readFileSync(path: string): Buffer;

    export function writeFile(path: string, data: string | Buffer, callback: (err: Error | null) => void): void;
    export function writeFileSync(path: string, data: string | Buffer): void;

    export function stat(path: string, callback: (err: Error | null, stats: Stats) => void): void;
    export function statSync(path: string): Stats;

    export function mkdir(path: string, callback: (err: Error | null) => void): void;
    export function mkdirSync(path: string): void;

    export function rmdir(path: string, callback: (err: Error | null) => void): void;
    export function rmdirSync(path: string): void;
}

// Ambient module for environment-specific APIs
declare module "browser-env" {
    export interface BrowserWindow extends Window {
        customBrowserMethod(): void;
    }

    export function getBrowserInfo(): {
        name: string;
        version: string;
        platform: string;
    };

    export const isBrowser: boolean;
}

declare module "node-env" {
    export interface NodeProcess extends NodeJS.Process {
        customNodeMethod(): void;
    }

    export function getNodeInfo(): {
        version: string;
        platform: string;
        arch: string;
    };

    export const isNode: boolean;
}

// Wildcard module declarations
declare module "*.json" {
    const content: any;
    export default content;
}

declare module "*.css" {
    const content: { [className: string]: string };
    export default content;
}

declare module "*.scss" {
    const content: { [className: string]: string };
    export default content;
}

declare module "*.png" {
    const content: string;
    export default content;
}

declare module "*.jpg" {
    const content: string;
    export default content;
}

declare module "*.svg" {
    const content: string;
    export default content;
}

// Plugin declarations
declare module "jquery-plugin" {
    interface JQuery {
        myPlugin(options?: {
            setting1?: string;
            setting2?: number;
            callback?: () => void;
        }): JQuery;
    }
}

// UMD module declaration
declare module "umd-library" {
    export interface UMDLibrary {
        init(config: any): void;
        destroy(): void;
        version: string;
    }

    const library: UMDLibrary;
    export default library;
    export as namespace UMDLib;
}

// Legacy global library
declare var LegacyLibrary: {
    version: string;
    init(options: any): void;
    utility: {
        helper1(arg: string): string;
        helper2(arg: number): number;
    };
};

// Conditional type declarations
declare module "conditional-lib" {
    export type ConditionalType<T> = T extends string ? StringHandler : T extends number ? NumberHandler : DefaultHandler;

    export interface StringHandler {
        handleString(value: string): void;
    }

    export interface NumberHandler {
        handleNumber(value: number): void;
    }

    export interface DefaultHandler {
        handleDefault(value: any): void;
    }

    export function processValue<T>(value: T): ConditionalType<T>;
}

// Augmenting existing modules
declare module "existing-module" {
    export interface ExistingInterface {
        newProperty: string;
        newMethod(): void;
    }

    export function newFunction(): void;
}

// Triple-slash directive references
/// <reference types="node" />
/// <reference types="jest" />
/// <reference path="./custom-types.d.ts" />

// Custom global types
declare global {
    interface GlobalThis {
        customGlobalProperty: string;
        customGlobalFunction(): void;
    }

    namespace NodeJS {
        interface ProcessEnv {
            CUSTOM_ENV_VAR: string;
            ANOTHER_ENV_VAR?: string;
        }
    }

    var customGlobalVariable: string;

    function customGlobalFunction(): void;
}

// Export declarations
export {};
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=typescript_declarations_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Check for ambient declarations
    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    # Look for declared functions, classes, interfaces, etc.
    ambient_nodes = [
        call
        for call in all_nodes
        if "ambient_declarations" in call[0][1].get("qualified_name", "")
        and call[0][0] in ["Function", "Class", "Interface", "Namespace", "Module"]
    ]

    assert len(ambient_nodes) >= 5, (
        f"Expected at least 5 ambient declaration nodes, found {len(ambient_nodes)}"
    )

    # Check for specific ambient declarations
    function_calls = [
        call
        for call in all_nodes
        if call[0][0] == "Function"
        and "ambient_declarations" in call[0][1].get("qualified_name", "")
    ]

    interface_calls = [
        call
        for call in all_nodes
        if call[0][0] == "Interface"
        and "ambient_declarations" in call[0][1].get("qualified_name", "")
    ]

    [
        call
        for call in all_nodes
        if call[0][0] == "Class"
        and "ambient_declarations" in call[0][1].get("qualified_name", "")
    ]

    # Should have ambient functions, interfaces, and classes
    assert len(function_calls) >= 2, (
        f"Expected at least 2 ambient functions, found {len(function_calls)}"
    )

    assert len(interface_calls) >= 3, (
        f"Expected at least 3 ambient interfaces, found {len(interface_calls)}"
    )


def test_module_declarations(
    typescript_declarations_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript module declarations and augmentations."""
    test_file = typescript_declarations_project / "module_declarations.d.ts"
    test_file.write_text(
        """
// Module declarations and augmentations

// Basic module declaration
declare module "basic-module" {
    export interface BasicConfig {
        name: string;
        version: string;
    }

    export function init(config: BasicConfig): void;
    export function destroy(): void;

    export const VERSION: string;
}

// Module with namespace
declare module "namespaced-module" {
    export namespace Utils {
        function helper1(arg: string): string;
        function helper2(arg: number): number;

        interface HelperConfig {
            option1: boolean;
            option2: string;
        }

        class HelperClass {
            constructor(config: HelperConfig);
            process(): void;
        }
    }

    export namespace Types {
        interface User {
            id: string;
            name: string;
            email: string;
        }

        interface Product {
            id: string;
            name: string;
            price: number;
        }

        type Entity = User | Product;
    }

    export function createUser(data: Types.User): Types.User;
    export function createProduct(data: Types.Product): Types.Product;
}

// Module with generic exports
declare module "generic-module" {
    export interface Container<T> {
        value: T;
        getValue(): T;
        setValue(value: T): void;
    }

    export class GenericClass<T, U> {
        constructor(first: T, second: U);
        getFirst(): T;
        getSecond(): U;
        combine(): [T, U];
    }

    export function process<T>(input: T, processor: (item: T) => T): T;
    export function map<T, U>(items: T[], mapper: (item: T) => U): U[];
    export function filter<T>(items: T[], predicate: (item: T) => boolean): T[];
}

// Module augmentation
declare module "react" {
    interface Component {
        customMethod(): void;
    }

    interface ComponentClass {
        customStaticMethod(): void;
    }

    namespace JSX {
        interface IntrinsicElements {
            'custom-element': {
                customProp?: string;
                onClick?: () => void;
            };
        }
    }
}

// Augmenting Node.js modules
declare module "fs" {
    interface Stats {
        customProperty: string;
    }

    function customFunction(path: string): Promise<string>;
}

declare module "path" {
    function customJoin(...segments: string[]): string;

    interface CustomPathObject {
        customDir: string;
        customBase: string;
    }

    function customParse(path: string): CustomPathObject;
}

// Module with conditional exports
declare module "conditional-exports" {
    export interface BaseConfig {
        type: 'base';
        name: string;
    }

    export interface AdvancedConfig {
        type: 'advanced';
        name: string;
        features: string[];
    }

    export type Config = BaseConfig | AdvancedConfig;

    export function createConfig<T extends Config['type']>(
        type: T
    ): T extends 'advanced' ? AdvancedConfig : BaseConfig;

    export function processConfig<T extends Config>(
        config: T
    ): T extends AdvancedConfig ? string[] : string;
}

// CSS modules
declare module "*.module.css" {
    const classes: { [key: string]: string };
    export default classes;
}

declare module "*.module.scss" {
    const classes: { [key: string]: string };
    export default classes;
}

declare module "*.module.less" {
    const classes: { [key: string]: string };
    export default classes;
}

// Asset modules
declare module "*.woff" {
    const src: string;
    export default src;
}

declare module "*.woff2" {
    const src: string;
    export default src;
}

declare module "*.ttf" {
    const src: string;
    export default src;
}

declare module "*.eot" {
    const src: string;
    export default src;
}

// JSON modules
declare module "*.json" {
    const value: any;
    export default value;
}

declare module "*package.json" {
    interface PackageJson {
        name: string;
        version: string;
        description?: string;
        main?: string;
        scripts?: Record<string, string>;
        dependencies?: Record<string, string>;
        devDependencies?: Record<string, string>;
        keywords?: string[];
        author?: string;
        license?: string;
    }

    const packageJson: PackageJson;
    export default packageJson;
}

// Environment-specific modules
declare module "web-env" {
    export interface WebAPIConfig {
        baseURL: string;
        timeout: number;
        credentials: 'include' | 'omit' | 'same-origin';
    }

    export class WebAPIClient {
        constructor(config: WebAPIConfig);
        get<T>(url: string): Promise<T>;
        post<T>(url: string, data: any): Promise<T>;
        put<T>(url: string, data: any): Promise<T>;
        delete(url: string): Promise<void>;
    }

    export function isWebEnvironment(): boolean;
    export const webGlobal: Window & typeof globalThis;
}

declare module "node-env" {
    export interface ServerConfig {
        port: number;
        host: string;
        ssl?: {
            cert: string;
            key: string;
        };
    }

    export class Server {
        constructor(config: ServerConfig);
        start(): Promise<void>;
        stop(): Promise<void>;
        getInfo(): { port: number; host: string; };
    }

    export function isNodeEnvironment(): boolean;
    export const nodeGlobal: NodeJS.Global & typeof globalThis;
}

// Plugin system modules
declare module "plugin-system" {
    export interface PluginContext {
        emit(event: string, data: any): void;
        on(event: string, handler: (data: any) => void): void;
        config: Record<string, any>;
    }

    export interface Plugin {
        name: string;
        version: string;
        init(context: PluginContext): void;
        destroy?(context: PluginContext): void;
    }

    export class PluginManager {
        register(plugin: Plugin): void;
        unregister(pluginName: string): void;
        getPlugin(name: string): Plugin | undefined;
        getAllPlugins(): Plugin[];
        initialize(): void;
        shutdown(): void;
    }

    export function createPlugin(definition: Omit<Plugin, 'version'> & { version?: string }): Plugin;
}

// Database connection modules
declare module "database-adapter" {
    export interface ConnectionConfig {
        host: string;
        port: number;
        database: string;
        username: string;
        password: string;
        ssl?: boolean;
        pool?: {
            min: number;
            max: number;
        };
    }

    export interface QueryResult<T = any> {
        rows: T[];
        rowCount: number;
        fields: Array<{ name: string; type: string; }>;
    }

    export class DatabaseConnection {
        constructor(config: ConnectionConfig);
        connect(): Promise<void>;
        disconnect(): Promise<void>;
        query<T = any>(sql: string, params?: any[]): Promise<QueryResult<T>>;
        transaction<T>(callback: (connection: DatabaseConnection) => Promise<T>): Promise<T>;
    }

    export function createConnection(config: ConnectionConfig): DatabaseConnection;
}

// Testing framework modules
declare module "test-framework" {
    export interface TestContext {
        name: string;
        timeout: number;
        skip: boolean;
        only: boolean;
    }

    export interface AssertionError extends Error {
        actual: any;
        expected: any;
        operator: string;
    }

    export function describe(name: string, fn: () => void): void;
    export function it(name: string, fn: (context: TestContext) => void | Promise<void>): void;
    export function beforeEach(fn: () => void | Promise<void>): void;
    export function afterEach(fn: () => void | Promise<void>): void;
    export function before(fn: () => void | Promise<void>): void;
    export function after(fn: () => void | Promise<void>): void;

    export namespace expect {
        interface Assertion {
            to: Assertion;
            be: Assertion;
            have: Assertion;
            equal(value: any): void;
            deep: Assertion;
            property(name: string): Assertion;
            length(value: number): void;
            throw(error?: string | RegExp | Error): void;
        }

        function expect(value: any): Assertion;
    }

    export { expect };
}

// Micro-frontend modules
declare module "micro-frontend" {
    export interface MicroFrontendConfig {
        name: string;
        entry: string;
        container: string;
        activeWhen: (location: Location) => boolean;
    }

    export interface MicroFrontendApp {
        mount(element: HTMLElement): Promise<void>;
        unmount(element: HTMLElement): Promise<void>;
        getStatus(): 'NOT_LOADED' | 'LOADING' | 'LOADED' | 'MOUNTING' | 'MOUNTED' | 'UNMOUNTING';
    }

    export function registerApplication(config: MicroFrontendConfig): void;
    export function start(): void;
    export function navigateToUrl(url: string): void;
    export function loadApp(name: string): Promise<MicroFrontendApp>;
}

// Worker modules
declare module "web-worker" {
    export interface WorkerMessage<T = any> {
        type: string;
        payload: T;
        id?: string;
    }

    export interface WorkerConfig {
        script: string;
        type?: 'classic' | 'module';
    }

    export class WorkerManager {
        constructor(config: WorkerConfig);
        postMessage<T>(message: WorkerMessage<T>): void;
        onMessage<T>(handler: (message: WorkerMessage<T>) => void): void;
        terminate(): void;
    }

    export function createWorker(script: string): WorkerManager;
}

export {};
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=typescript_declarations_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Check for module declarations
    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    module_nodes = [
        call
        for call in all_nodes
        if "module_declarations" in call[0][1].get("qualified_name", "")
        and call[0][0] in ["Module", "Namespace", "Interface", "Class", "Function"]
    ]

    assert len(module_nodes) >= 8, (
        f"Expected at least 8 module declaration nodes, found {len(module_nodes)}"
    )

    # Check for specific module patterns
    interface_calls = [
        call
        for call in all_nodes
        if call[0][0] == "Interface"
        and "module_declarations" in call[0][1].get("qualified_name", "")
    ]

    class_calls = [
        call
        for call in all_nodes
        if call[0][0] == "Class"
        and "module_declarations" in call[0][1].get("qualified_name", "")
    ]

    function_calls = [
        call
        for call in all_nodes
        if call[0][0] == "Function"
        and "module_declarations" in call[0][1].get("qualified_name", "")
    ]

    # Should have module interfaces, classes, and functions
    assert len(interface_calls) >= 5, (
        f"Expected at least 5 module interfaces, found {len(interface_calls)}"
    )

    assert len(class_calls) >= 3, (
        f"Expected at least 3 module classes, found {len(class_calls)}"
    )

    assert len(function_calls) >= 5, (
        f"Expected at least 5 module functions, found {len(function_calls)}"
    )


def test_global_augmentations(
    typescript_declarations_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript global augmentations and extensions."""
    test_file = typescript_declarations_project / "global_augmentations.d.ts"
    test_file.write_text(
        """
// Global augmentations and extensions

// Augmenting global scope
declare global {
    // Extending Window interface
    interface Window {
        customAPI: {
            version: string;
            init(): void;
            destroy(): void;
            utils: {
                format(value: any): string;
                parse(str: string): any;
            };
        };

        gtag?: (command: string, ...args: any[]) => void;
        dataLayer?: any[];

        // Custom global functions
        $: (selector: string) => HTMLElement | null;
        $$: (selector: string) => NodeListOf<HTMLElement>;
    }

    // Extending globalThis
    interface GlobalThis {
        APP_CONFIG: {
            environment: 'development' | 'staging' | 'production';
            apiUrl: string;
            features: Record<string, boolean>;
        };

        customLog: (level: 'info' | 'warn' | 'error', message: string) => void;
    }

    // Node.js globals
    namespace NodeJS {
        interface Global {
            customNodeGlobal: {
                startTime: Date;
                version: string;
                utils: {
                    memoryUsage(): NodeJS.MemoryUsage;
                    uptime(): number;
                };
            };
        }

        interface ProcessEnv {
            NODE_ENV: 'development' | 'staging' | 'production';
            PORT?: string;
            HOST?: string;
            DATABASE_URL?: string;
            JWT_SECRET?: string;

            // Custom environment variables
            CUSTOM_API_KEY?: string;
            CUSTOM_FEATURE_FLAG?: string;
            CUSTOM_DEBUG_MODE?: string;
        }

        interface Process {
            customMethod(): string;
            customProperty: boolean;
        }
    }

    // Array extensions
    interface Array<T> {
        customFind(predicate: (item: T, index: number) => boolean): T | undefined;
        customFilter(predicate: (item: T, index: number) => boolean): T[];
        customMap<U>(mapper: (item: T, index: number) => U): U[];

        // Utility methods
        first(): T | undefined;
        last(): T | undefined;
        isEmpty(): boolean;
        chunk(size: number): T[][];
    }

    interface ReadonlyArray<T> {
        customFind(predicate: (item: T, index: number) => boolean): T | undefined;
        first(): T | undefined;
        last(): T | undefined;
        isEmpty(): boolean;
    }

    // String extensions
    interface String {
        toCamelCase(): string;
        toSnakeCase(): string;
        toKebabCase(): string;
        capitalize(): string;
        truncate(length: number, suffix?: string): string;

        // Validation methods
        isEmail(): boolean;
        isUrl(): boolean;
        isNumeric(): boolean;
        isEmpty(): boolean;
    }

    // Number extensions
    interface Number {
        toCurrency(currency?: string, locale?: string): string;
        toPercent(decimals?: number): string;
        clamp(min: number, max: number): number;

        // Math utilities
        round(decimals?: number): number;
        isEven(): boolean;
        isOdd(): boolean;
    }

    // Object extensions
    interface Object {
        isEmpty(): boolean;
        hasProperty(key: string): boolean;
        getProperty<T>(key: string): T | undefined;
        deepClone<T>(this: T): T;
    }

    // Date extensions
    interface Date {
        addDays(days: number): Date;
        addMonths(months: number): Date;
        addYears(years: number): Date;

        format(pattern: string): string;
        isToday(): boolean;
        isYesterday(): boolean;
        isTomorrow(): boolean;

        // Comparison methods
        isBefore(date: Date): boolean;
        isAfter(date: Date): boolean;
        isSameDay(date: Date): boolean;
    }

    // Promise extensions
    interface Promise<T> {
        timeout(ms: number): Promise<T>;
        retry(attempts: number, delay?: number): Promise<T>;

        // Utility methods
        finally<U>(onFinally: () => U | Promise<U>): Promise<T>;
    }

    // Function extensions
    interface Function {
        debounce(delay: number): Function;
        throttle(delay: number): Function;
        memoize(): Function;

        // Binding utilities
        bindAll(context: any): Function;
        once(): Function;
    }

    // Math extensions
    interface Math {
        randomInt(min: number, max: number): number;
        randomFloat(min: number, max: number): number;
        clamp(value: number, min: number, max: number): number;

        // Additional utilities
        degrees(radians: number): number;
        radians(degrees: number): number;
        lerp(start: number, end: number, factor: number): number;
    }

    // JSON extensions
    interface JSON {
        tryParse<T>(text: string): T | null;
        tryStringify(value: any): string | null;

        // Safe parsing
        safeParse<T>(text: string, defaultValue: T): T;
        safeStringify(value: any, defaultValue?: string): string;
    }

    // Console extensions
    interface Console {
        group(label?: string): void;
        groupEnd(): void;
        groupCollapsed(label?: string): void;

        // Custom logging methods
        success(message?: any, ...optionalParams: any[]): void;
        debug(message?: any, ...optionalParams: any[]): void;
        trace(message?: any, ...optionalParams: any[]): void;

        // Performance methods
        timeStart(label: string): void;
        timeEnd(label: string): void;
        memory(): MemoryInfo;
    }

    // Storage extensions
    interface Storage {
        getObject<T>(key: string): T | null;
        setObject(key: string, value: any): void;
        removeObject(key: string): void;

        // Utility methods
        has(key: string): boolean;
        isEmpty(): boolean;
        size(): number;
    }

    // Custom global types
    type CustomEventMap = {
        'custom:ready': CustomEvent<{ timestamp: Date }>;
        'custom:error': CustomEvent<{ error: Error; context: string }>;
        'custom:data': CustomEvent<{ data: any; source: string }>;
    };

    // Custom global interfaces
    interface CustomEventTarget extends EventTarget {
        addEventListener<K extends keyof CustomEventMap>(
            type: K,
            listener: (event: CustomEventMap[K]) => void,
            options?: boolean | AddEventListenerOptions
        ): void;

        removeEventListener<K extends keyof CustomEventMap>(
            type: K,
            listener: (event: CustomEventMap[K]) => void,
            options?: boolean | EventListenerOptions
        ): void;

        dispatchEvent<K extends keyof CustomEventMap>(event: CustomEventMap[K]): boolean;
    }

    // Custom global constants
    const CUSTOM_VERSION: string;
    const CUSTOM_BUILD_TIME: string;
    const CUSTOM_FEATURES: readonly string[];

    // Custom global functions
    function customGlobalFunction(arg: string): string;
    function customAsyncFunction(arg: any): Promise<any>;
    function customUtilFunction<T>(input: T): T;

    // Custom global variables
    var customGlobalVar: {
        initialized: boolean;
        config: Record<string, any>;
        methods: {
            init(): void;
            reset(): void;
            getState(): any;
        };
    };

    // Custom global classes
    class CustomGlobalClass {
        constructor(options?: any);
        static create(options?: any): CustomGlobalClass;
        init(): void;
        destroy(): void;
    }

    // Custom error types
    class CustomError extends Error {
        code: string;
        context?: any;
        constructor(message: string, code: string, context?: any);
    }

    class ValidationError extends CustomError {
        field: string;
        value: any;
        constructor(message: string, field: string, value: any);
    }

    // Custom utility types
    type DeepPartial<T> = {
        [P in keyof T]?: T[P] extends object ? DeepPartial<T[P]> : T[P];
    };

    type DeepRequired<T> = {
        [P in keyof T]-?: T[P] extends object ? DeepRequired<T[P]> : T[P];
    };

    type StringKeys<T> = {
        [K in keyof T]: T[K] extends string ? K : never;
    }[keyof T];

    type NumberKeys<T> = {
        [K in keyof T]: T[K] extends number ? K : never;
    }[keyof T];
}

// Module augmentation for existing libraries
declare module "react" {
    interface Component<P = {}, S = {}, SS = any> {
        customReactMethod(): void;
    }

    interface ComponentClass<P = {}, S = ComponentState> {
        customStaticMethod(): void;
    }

    namespace JSX {
        interface IntrinsicElements {
            'custom-component': {
                customProp?: string;
                onCustomEvent?: (event: any) => void;
            };
        }

        interface Element {
            customElementProperty?: any;
        }
    }
}

declare module "lodash" {
    interface LoDashStatic {
        customUtility<T>(collection: T[]): T[];
        customTransform<T, U>(value: T, transformer: (value: T) => U): U;
    }
}

// CSS modules augmentation
declare module "*.css" {
    interface CSSModuleClasses {
        readonly [key: string]: string;
    }
    const classes: CSSModuleClasses;
    export default classes;
}

// Asset modules augmentation
declare module "*.png" {
    const content: string;
    export default content;
    export const width: number;
    export const height: number;
}

declare module "*.svg" {
    import { ReactElement, SVGProps } from 'react';
    const content: (props: SVGProps<SVGElement>) => ReactElement;
    export default content;
    export const ReactComponent: typeof content;
}

// Export to make this a module
export {};
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=typescript_declarations_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Check for global augmentation patterns
    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    global_nodes = [
        call
        for call in all_nodes
        if "global_augmentations" in call[0][1].get("qualified_name", "")
    ]

    assert len(global_nodes) >= 10, (
        f"Expected at least 10 global augmentation nodes, found {len(global_nodes)}"
    )

    # Check for interface extensions
    interface_calls = [
        call
        for call in all_nodes
        if call[0][0] == "Interface"
        and "global_augmentations" in call[0][1].get("qualified_name", "")
    ]

    # Check for class declarations in global scope
    [
        call
        for call in all_nodes
        if call[0][0] == "Class"
        and "global_augmentations" in call[0][1].get("qualified_name", "")
    ]

    # Check for function declarations in global scope
    [
        call
        for call in all_nodes
        if call[0][0] == "Function"
        and "global_augmentations" in call[0][1].get("qualified_name", "")
    ]

    # Should have global interfaces, classes, and functions
    assert len(interface_calls) >= 5, (
        f"Expected at least 5 global interfaces, found {len(interface_calls)}"
    )


def test_typescript_declarations_comprehensive(
    typescript_declarations_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all TypeScript declaration file patterns are covered."""
    test_file = typescript_declarations_project / "comprehensive_declarations.d.ts"
    test_file.write_text(
        """
// Every TypeScript declaration file pattern in one file

// Ambient declaration
declare var globalVar: string;
declare function globalFunction(): void;

// Ambient namespace
declare namespace MyLibrary {
    interface Config {
        name: string;
        version: string;
    }

    function init(config: Config): void;
}

// Module declaration
declare module "my-module" {
    export interface ModuleConfig {
        enabled: boolean;
    }

    export class ModuleClass {
        constructor(config: ModuleConfig);
        start(): void;
    }

    export function moduleFunction(): string;
}

// Global augmentation
declare global {
    interface Window {
        customProperty: string;
    }

    interface Array<T> {
        customMethod(): T[];
    }

    function customGlobal(): void;
}

// Wildcard module
declare module "*.json" {
    const content: any;
    export default content;
}

// Module augmentation
declare module "existing-lib" {
    interface ExistingInterface {
        newProperty: string;
    }

    function newFunction(): void;
}

// UMD module
declare module "umd-lib" {
    interface UMDInterface {
        method(): void;
    }

    const lib: UMDInterface;
    export default lib;
    export as namespace UMDLib;
}

// Triple-slash directive
/// <reference types="node" />

// Using all patterns
const config: MyLibrary.Config = { name: 'test', version: '1.0' };
MyLibrary.init(config);

export {};
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=typescript_declarations_project,
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

    # Should have comprehensive declaration-related calls
    comprehensive_calls = [
        call
        for call in calls_relationships
        if "comprehensive_declarations" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 1, (
        f"Expected at least 1 comprehensive declaration call, found {len(comprehensive_calls)}"
    )

    # Check all declaration patterns were created
    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    comprehensive_declarations = [
        call
        for call in all_nodes
        if "comprehensive_declarations" in call[0][1].get("qualified_name", "")
    ]

    assert len(comprehensive_declarations) >= 5, (
        f"Expected at least 5 declaration patterns, found {len(comprehensive_declarations)}"
    )
