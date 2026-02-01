from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_nodes, get_relationships, run_updater


@pytest.fixture
def typescript_decorators_project(temp_repo: Path) -> Path:
    """Create a comprehensive TypeScript project with decorator patterns."""
    project_path = temp_repo / "typescript_decorators_test"
    project_path.mkdir()

    (project_path / "decorators").mkdir()
    (project_path / "examples").mkdir()

    (project_path / "decorators" / "common.ts").write_text(
        encoding="utf-8",
        data="""
// Common decorators
export function log(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
    const originalMethod = descriptor.value;

    descriptor.value = function(...args: any[]) {
        console.log(`Calling ${propertyKey} with args:`, args);
        const result = originalMethod.apply(this, args);
        console.log(`Result:`, result);
        return result;
    };
}

export function readonly(target: any, propertyKey: string) {
    Object.defineProperty(target, propertyKey, {
        writable: false,
        configurable: false
    });
}
""",
    )

    return project_path


def test_class_decorators(
    typescript_decorators_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript class decorators."""
    test_file = typescript_decorators_project / "class_decorators.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Class decorators

// Simple class decorator
function Component(target: any) {
    target.prototype.isComponent = true;
    target.prototype.render = function() {
        return `<${target.name.toLowerCase()}></${target.name.toLowerCase()}>`;
    };
}

// Class decorator with parameters
function Injectable(token?: string) {
    return function(target: any) {
        target.prototype.injectionToken = token || target.name;
        target.prototype.isInjectable = true;
    };
}

// Class decorator factory
function Entity(tableName: string, schema?: string) {
    return function<T extends new (...args: any[]) => {}>(constructor: T) {
        return class extends constructor {
            tableName = tableName;
            schema = schema || 'public';

            save() {
                console.log(`Saving entity to ${this.schema}.${this.tableName}`);
            }

            delete() {
                console.log(`Deleting entity from ${this.schema}.${this.tableName}`);
            }
        };
    };
}

// Metadata decorator
function Metadata(data: Record<string, any>) {
    return function(target: any) {
        target.metadata = { ...target.metadata, ...data };
    };
}

// Validation decorator
function Validate() {
    return function<T extends new (...args: any[]) => {}>(constructor: T) {
        return class extends constructor {
            validate(): boolean {
                console.log('Validating instance');
                return true;
            }

            getValidationErrors(): string[] {
                return [];
            }
        };
    };
}

// Singleton decorator
function Singleton<T extends new (...args: any[]) => {}>(constructor: T) {
    let instance: T | null = null;

    return class extends constructor {
        constructor(...args: any[]) {
            if (instance) {
                return instance;
            }
            super(...args);
            instance = this as any;
        }
    } as T;
}

// Performance monitoring decorator
function Monitor(name?: string) {
    return function<T extends new (...args: any[]) => {}>(constructor: T) {
        const className = name || constructor.name;

        return class extends constructor {
            constructor(...args: any[]) {
                console.log(`Creating instance of ${className}`);
                const start = performance.now();
                super(...args);
                const end = performance.now();
                console.log(`${className} instantiation took ${end - start}ms`);
            }
        };
    };
}

// Multiple decorators on one class
@Component
@Injectable('UserService')
@Entity('users', 'auth')
@Metadata({ version: '1.0', author: 'Developer' })
@Validate()
@Monitor('UserClass')
class User {
    constructor(
        public id: string,
        public name: string,
        public email: string
    ) {}

    getName(): string {
        return this.name;
    }

    setName(name: string): void {
        this.name = name;
    }
}

// Decorator with complex logic
function Cacheable(ttl: number = 60000) {
    return function<T extends new (...args: any[]) => {}>(constructor: T) {
        const cache = new Map<string, { value: any; expiry: number }>();

        return class extends constructor {
            private getCacheKey(args: any[]): string {
                return JSON.stringify(args);
            }

            private isExpired(entry: { value: any; expiry: number }): boolean {
                return Date.now() > entry.expiry;
            }

            getCached(method: string, args: any[]): any {
                const key = `${method}:${this.getCacheKey(args)}`;
                const entry = cache.get(key);

                if (entry && !this.isExpired(entry)) {
                    return entry.value;
                }

                return null;
            }

            setCache(method: string, args: any[], value: any): void {
                const key = `${method}:${this.getCacheKey(args)}`;
                cache.set(key, { value, expiry: Date.now() + ttl });
            }

            clearCache(): void {
                cache.clear();
            }
        };
    };
}

// Abstract decorator
function Abstract<T extends new (...args: any[]) => {}>(constructor: T) {
    return class extends constructor {
        constructor(...args: any[]) {
            if (new.target === constructor) {
                throw new Error('Cannot instantiate abstract class');
            }
            super(...args);
        }
    };
}

// Configuration decorator
function Config(config: {
    autoSave?: boolean;
    encryption?: boolean;
    compression?: boolean;
}) {
    return function<T extends new (...args: any[]) => {}>(constructor: T) {
        return class extends constructor {
            config = config;

            getConfig() {
                return this.config;
            }

            updateConfig(newConfig: Partial<typeof config>) {
                this.config = { ...this.config, ...newConfig };
            }
        };
    };
}

// Applied decorators
@Singleton
class DatabaseConnection {
    private connectionString: string;

    constructor(connectionString: string) {
        this.connectionString = connectionString;
    }

    connect(): void {
        console.log(`Connecting to ${this.connectionString}`);
    }
}

@Cacheable(30000)
class DataService {
    constructor(private connection: DatabaseConnection) {}

    getData(id: string): any {
        // Simulate data fetching
        console.log(`Fetching data for ID: ${id}`);
        return { id, data: 'some data' };
    }
}

@Abstract
class BaseModel {
    constructor(public id: string) {}

    abstract save(): void;
    abstract delete(): void;
}

@Config({ autoSave: true, encryption: true })
class SecureModel extends BaseModel {
    constructor(id: string, private data: any) {
        super(id);
    }

    save(): void {
        console.log('Saving secure model');
    }

    delete(): void {
        console.log('Deleting secure model');
    }
}

// Using decorated classes
const user = new User('1', 'Alice', 'alice@example.com');
console.log((user as any).isComponent); // true
console.log((user as any).injectionToken); // 'UserService'
console.log((user as any).tableName); // 'users'
console.log((user as any).metadata); // { version: '1.0', author: 'Developer' }

// Singleton test
const db1 = new DatabaseConnection('postgresql://localhost:5432/db');
const db2 = new DatabaseConnection('mysql://localhost:3306/db');
console.log(db1 === db2); // true (same instance)

// Cacheable test
const dataService = new DataService(db1);
const data1 = dataService.getData('123');
const data2 = dataService.getData('123'); // Should use cache

// Config test
const secureModel = new SecureModel('model1', { secret: 'data' });
console.log((secureModel as any).getConfig()); // { autoSave: true, encryption: true }

// Abstract test (should throw error)
try {
    new BaseModel('test');
} catch (error) {
    console.log('Cannot instantiate abstract class');
}

// Validation test
console.log((user as any).validate()); // true
console.log((user as any).getValidationErrors()); // []
""",
    )

    run_updater(typescript_decorators_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    decorator_functions = [
        call
        for call in function_calls
        if "class_decorators" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in ["Component", "Injectable", "Entity", "Singleton", "Monitor"]
        )
    ]

    assert len(decorator_functions) >= 5, (
        f"Expected at least 5 decorator functions, found {len(decorator_functions)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    decorated_classes = [
        call
        for call in class_calls
        if "class_decorators" in call[0][1]["qualified_name"]
        and any(
            class_name in call[0][1]["qualified_name"]
            for class_name in [
                "User",
                "DatabaseConnection",
                "DataService",
                "BaseModel",
                "SecureModel",
            ]
        )
    ]

    assert len(decorated_classes) >= 4, (
        f"Expected at least 4 decorated classes, found {len(decorated_classes)}"
    )

    inheritance_relationships = get_relationships(mock_ingestor, "INHERITS")

    decorator_inheritance = [
        call
        for call in inheritance_relationships
        if "class_decorators" in call.args[0][2]
    ]

    assert len(decorator_inheritance) >= 1, (
        f"Expected decorator inheritance relationships, found {len(decorator_inheritance)}"
    )


def test_method_decorators(
    typescript_decorators_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript method decorators."""
    test_file = typescript_decorators_project / "method_decorators.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Method decorators

// Simple method decorator
function Log(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
    const originalMethod = descriptor.value;

    descriptor.value = function(...args: any[]) {
        console.log(`Calling ${propertyKey} with args:`, args);
        const result = originalMethod.apply(this, args);
        console.log(`Result:`, result);
        return result;
    };

    return descriptor;
}

// Timing decorator
function Time(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
    const originalMethod = descriptor.value;

    descriptor.value = function(...args: any[]) {
        const start = performance.now();
        const result = originalMethod.apply(this, args);
        const end = performance.now();
        console.log(`${propertyKey} took ${end - start} milliseconds`);
        return result;
    };

    return descriptor;
}

// Retry decorator
function Retry(maxAttempts: number = 3, delay: number = 1000) {
    return function(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
        const originalMethod = descriptor.value;

        descriptor.value = async function(...args: any[]) {
            let attempts = 0;
            let lastError: any;

            while (attempts < maxAttempts) {
                try {
                    return await originalMethod.apply(this, args);
                } catch (error) {
                    attempts++;
                    lastError = error;

                    if (attempts < maxAttempts) {
                        console.log(`Attempt ${attempts} failed, retrying in ${delay}ms...`);
                        await new Promise(resolve => setTimeout(resolve, delay));
                    }
                }
            }

            throw lastError;
        };

        return descriptor;
    };
}

// Cache decorator
function Cache(ttl: number = 60000) {
    const cache = new Map<string, { value: any; expiry: number }>();

    return function(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
        const originalMethod = descriptor.value;

        descriptor.value = function(...args: any[]) {
            const key = `${propertyKey}:${JSON.stringify(args)}`;
            const cached = cache.get(key);

            if (cached && Date.now() < cached.expiry) {
                console.log(`Cache hit for ${propertyKey}`);
                return cached.value;
            }

            const result = originalMethod.apply(this, args);
            cache.set(key, { value: result, expiry: Date.now() + ttl });
            console.log(`Cache miss for ${propertyKey}, result cached`);

            return result;
        };

        return descriptor;
    };
}

// Validation decorator
function Validate(validator: (args: any[]) => boolean, errorMessage: string = 'Invalid arguments') {
    return function(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
        const originalMethod = descriptor.value;

        descriptor.value = function(...args: any[]) {
            if (!validator(args)) {
                throw new Error(`${propertyKey}: ${errorMessage}`);
            }

            return originalMethod.apply(this, args);
        };

        return descriptor;
    };
}

// Authorization decorator
function Authorize(roles: string[]) {
    return function(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
        const originalMethod = descriptor.value;

        descriptor.value = function(...args: any[]) {
            const userRole = (this as any).getCurrentUserRole?.() || 'guest';

            if (!roles.includes(userRole)) {
                throw new Error(`Access denied. Required roles: ${roles.join(', ')}`);
            }

            return originalMethod.apply(this, args);
        };

        return descriptor;
    };
}

// Debounce decorator
function Debounce(delay: number) {
    let timeoutId: NodeJS.Timeout;

    return function(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
        const originalMethod = descriptor.value;

        descriptor.value = function(...args: any[]) {
            clearTimeout(timeoutId);

            timeoutId = setTimeout(() => {
                originalMethod.apply(this, args);
            }, delay);
        };

        return descriptor;
    };
}

// Throttle decorator
function Throttle(delay: number) {
    let lastCallTime = 0;

    return function(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
        const originalMethod = descriptor.value;

        descriptor.value = function(...args: any[]) {
            const now = Date.now();

            if (now - lastCallTime >= delay) {
                lastCallTime = now;
                return originalMethod.apply(this, args);
            }
        };

        return descriptor;
    };
}

// Deprecated decorator
function Deprecated(message?: string) {
    return function(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
        const originalMethod = descriptor.value;

        descriptor.value = function(...args: any[]) {
            console.warn(`Warning: ${propertyKey} is deprecated. ${message || ''}`);
            return originalMethod.apply(this, args);
        };

        return descriptor;
    };
}

// Class using method decorators
class ApiService {
    private userRole: string = 'user';

    getCurrentUserRole(): string {
        return this.userRole;
    }

    setUserRole(role: string): void {
        this.userRole = role;
    }

    @Log
    @Time
    getData(id: string): any {
        // Simulate API call
        return { id, data: `Data for ${id}` };
    }

    @Retry(3, 500)
    async fetchExternalData(url: string): Promise<any> {
        // Simulate unreliable external API
        if (Math.random() < 0.7) {
            throw new Error('Network error');
        }

        return { url, data: 'External data' };
    }

    @Cache(30000)
    expensiveCalculation(input: number): number {
        console.log('Performing expensive calculation...');
        // Simulate expensive operation
        let result = 0;
        for (let i = 0; i < input * 1000; i++) {
            result += Math.random();
        }
        return result;
    }

    @Validate((args) => args[0] && typeof args[0] === 'string', 'Name must be a non-empty string')
    @Log
    createUser(name: string, email: string): any {
        return { name, email, id: Math.random().toString() };
    }

    @Authorize(['admin', 'moderator'])
    deleteUser(id: string): boolean {
        console.log(`Deleting user ${id}`);
        return true;
    }

    @Debounce(1000)
    onSearch(query: string): void {
        console.log(`Searching for: ${query}`);
    }

    @Throttle(2000)
    onScroll(): void {
        console.log('Scroll event handled');
    }

    @Deprecated('Use getDataV2 instead')
    getDataV1(id: string): any {
        return this.getData(id);
    }
}

// More complex decorator combinations
class DataProcessor {
    @Log
    @Time
    @Cache(60000)
    @Validate((args) => args[0] && Array.isArray(args[0]), 'Input must be an array')
    processData(data: any[]): any[] {
        console.log('Processing data...');
        return data.map(item => ({ ...item, processed: true }));
    }

    @Retry(5, 2000)
    @Log
    @Time
    async saveToDatabase(data: any): Promise<boolean> {
        // Simulate database save with potential failures
        if (Math.random() < 0.3) {
            throw new Error('Database connection failed');
        }

        console.log('Data saved to database');
        return true;
    }

    @Authorize(['admin'])
    @Log
    @Validate((args) => args.length === 2, 'Exactly two arguments required')
    adminOperation(action: string, target: string): void {
        console.log(`Admin ${action} on ${target}`);
    }
}

// Using decorated methods
const apiService = new ApiService();

// Simple method call
const data = apiService.getData('123');
console.log(data);

// Expensive calculation (will be cached)
console.log(apiService.expensiveCalculation(100));
console.log(apiService.expensiveCalculation(100)); // Should use cache

// Validation test
try {
    apiService.createUser('', 'test@example.com'); // Should throw error
} catch (error) {
    console.log(error.message);
}

// Authorization test
apiService.setUserRole('admin');
try {
    apiService.deleteUser('456'); // Should work
} catch (error) {
    console.log(error.message);
}

apiService.setUserRole('user');
try {
    apiService.deleteUser('789'); // Should throw error
} catch (error) {
    console.log(error.message);
}

// Debounce test
apiService.onSearch('query1');
apiService.onSearch('query2');
apiService.onSearch('query3'); // Only this will execute after 1000ms

// Throttle test
apiService.onScroll(); // Will execute
apiService.onScroll(); // Will be ignored (within 2000ms)

// Deprecated method
apiService.getDataV1('deprecated'); // Will show warning

// Data processor
const processor = new DataProcessor();
const processedData = processor.processData([{ id: 1 }, { id: 2 }]);
console.log(processedData);

// Retry mechanism
processor.saveToDatabase({ important: 'data' })
    .then(() => console.log('Save successful'))
    .catch(error => console.log('Save failed after retries'));
""",
    )

    run_updater(typescript_decorators_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    method_decorators = [
        call
        for call in function_calls
        if "method_decorators" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in ["Log", "Time", "Retry", "Cache", "Validate", "Authorize"]
        )
    ]

    assert len(method_decorators) >= 6, (
        f"Expected at least 6 method decorator functions, found {len(method_decorators)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    decorated_method_classes = [
        call
        for call in class_calls
        if "method_decorators" in call[0][1]["qualified_name"]
        and any(
            class_name in call[0][1]["qualified_name"]
            for class_name in ["ApiService", "DataProcessor"]
        )
    ]

    assert len(decorated_method_classes) >= 2, (
        f"Expected at least 2 classes with decorated methods, found {len(decorated_method_classes)}"
    )

    method_calls = get_nodes(mock_ingestor, "Method")

    decorated_methods = [
        call
        for call in method_calls
        if "method_decorators" in call[0][1]["qualified_name"]
        and any(
            method_name in call[0][1]["qualified_name"]
            for method_name in ["getData", "createUser", "deleteUser", "processData"]
        )
    ]

    assert len(decorated_methods) >= 3, (
        f"Expected at least 3 decorated methods, found {len(decorated_methods)}"
    )


def test_property_decorators(
    typescript_decorators_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript property decorators."""
    test_file = typescript_decorators_project / "property_decorators.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Property decorators

// Simple property decorator
function ReadOnly(target: any, propertyKey: string) {
    let value: any;

    Object.defineProperty(target, propertyKey, {
        get() {
            return value;
        },
        set(newValue: any) {
            if (value === undefined) {
                value = newValue;
            } else {
                console.warn(`Property ${propertyKey} is read-only`);
            }
        },
        enumerable: true,
        configurable: true
    });
}

// Observable property decorator
function Observable(target: any, propertyKey: string) {
    const privateKey = `_${propertyKey}`;
    const listenersKey = `_${propertyKey}_listeners`;

    target[listenersKey] = [];

    Object.defineProperty(target, propertyKey, {
        get() {
            return this[privateKey];
        },
        set(newValue: any) {
            const oldValue = this[privateKey];
            this[privateKey] = newValue;

            // Notify listeners
            this[listenersKey].forEach((listener: Function) => {
                listener(newValue, oldValue, propertyKey);
            });
        },
        enumerable: true,
        configurable: true
    });

    // Add listener method
    target[`addListener_${propertyKey}`] = function(listener: Function) {
        this[listenersKey].push(listener);
    };

    // Add remove listener method
    target[`removeListener_${propertyKey}`] = function(listener: Function) {
        const index = this[listenersKey].indexOf(listener);
        if (index > -1) {
            this[listenersKey].splice(index, 1);
        }
    };
}

// Validation decorator
function ValidateType(type: string) {
    return function(target: any, propertyKey: string) {
        const privateKey = `_${propertyKey}`;

        Object.defineProperty(target, propertyKey, {
            get() {
                return this[privateKey];
            },
            set(value: any) {
                if (typeof value !== type) {
                    throw new Error(`Property ${propertyKey} must be of type ${type}`);
                }
                this[privateKey] = value;
            },
            enumerable: true,
            configurable: true
        });
    };
}

// Range validation decorator
function Range(min: number, max: number) {
    return function(target: any, propertyKey: string) {
        const privateKey = `_${propertyKey}`;

        Object.defineProperty(target, propertyKey, {
            get() {
                return this[privateKey];
            },
            set(value: number) {
                if (typeof value !== 'number' || value < min || value > max) {
                    throw new Error(`Property ${propertyKey} must be between ${min} and ${max}`);
                }
                this[privateKey] = value;
            },
            enumerable: true,
            configurable: true
        });
    };
}

// Required property decorator
function Required(target: any, propertyKey: string) {
    const privateKey = `_${propertyKey}`;

    Object.defineProperty(target, propertyKey, {
        get() {
            if (this[privateKey] === undefined || this[privateKey] === null) {
                throw new Error(`Property ${propertyKey} is required`);
            }
            return this[privateKey];
        },
        set(value: any) {
            this[privateKey] = value;
        },
        enumerable: true,
        configurable: true
    });
}

// Computed property decorator
function Computed(dependencies: string[]) {
    return function(target: any, propertyKey: string) {
        const computeKey = `_compute_${propertyKey}`;
        const cacheKey = `_cache_${propertyKey}`;
        const dirtyKey = `_dirty_${propertyKey}`;

        // Mark as dirty initially
        target[dirtyKey] = true;

        // Override dependency setters to mark as dirty
        dependencies.forEach(dep => {
            const depPrivateKey = `_${dep}`;
            const originalDescriptor = Object.getOwnPropertyDescriptor(target, dep) || {};

            Object.defineProperty(target, dep, {
                get: originalDescriptor.get || function() { return this[depPrivateKey]; },
                set: function(value: any) {
                    if (originalDescriptor.set) {
                        originalDescriptor.set.call(this, value);
                    } else {
                        this[depPrivateKey] = value;
                    }
                    this[dirtyKey] = true; // Mark computed property as dirty
                },
                enumerable: true,
                configurable: true
            });
        });

        Object.defineProperty(target, propertyKey, {
            get() {
                if (this[dirtyKey] || this[cacheKey] === undefined) {
                    this[cacheKey] = this[computeKey]();
                    this[dirtyKey] = false;
                }
                return this[cacheKey];
            },
            enumerable: true,
            configurable: true
        });
    };
}

// Formatter decorator
function Format(formatter: (value: any) => string) {
    return function(target: any, propertyKey: string) {
        const privateKey = `_${propertyKey}`;
        const formattedKey = `${propertyKey}Formatted`;

        Object.defineProperty(target, propertyKey, {
            get() {
                return this[privateKey];
            },
            set(value: any) {
                this[privateKey] = value;
            },
            enumerable: true,
            configurable: true
        });

        Object.defineProperty(target, formattedKey, {
            get() {
                return formatter(this[privateKey]);
            },
            enumerable: true,
            configurable: true
        });
    };
}

// Lazy loading decorator
function Lazy(initializer: () => any) {
    return function(target: any, propertyKey: string) {
        const privateKey = `_${propertyKey}`;
        const initializedKey = `_${propertyKey}_initialized`;

        Object.defineProperty(target, propertyKey, {
            get() {
                if (!this[initializedKey]) {
                    this[privateKey] = initializer();
                    this[initializedKey] = true;
                }
                return this[privateKey];
            },
            set(value: any) {
                this[privateKey] = value;
                this[initializedKey] = true;
            },
            enumerable: true,
            configurable: true
        });
    };
}

// Class using property decorators
class User {
    @ReadOnly
    id: string;

    @Observable
    name: string;

    @ValidateType('string')
    email: string;

    @Range(0, 150)
    age: number;

    @Required
    username: string;

    @Format((value: Date) => value ? value.toLocaleDateString() : 'Not set')
    birthDate: Date;

    @Lazy(() => new Date())
    createdAt: Date;

    // Computed property based on other properties
    @Computed(['name', 'age'])
    displayName: string;

    // Compute method for computed property
    _compute_displayName(): string {
        return `${this.name} (${this.age} years old)`;
    }

    constructor(id: string, name: string, email: string) {
        this.id = id;
        this.name = name;
        this.email = email;
    }

    setAge(age: number): void {
        this.age = age;
    }

    setBirthDate(date: Date): void {
        this.birthDate = date;
    }
}

// Product class with more complex property decorators
class Product {
    @ReadOnly
    sku: string;

    @Observable
    @ValidateType('string')
    name: string;

    @Range(0, Infinity)
    @Observable
    price: number;

    @Range(0, Infinity)
    quantity: number;

    @Format((value: number) => `$${value.toFixed(2)}`)
    @Computed(['price', 'quantity'])
    totalValue: number;

    @Lazy(() => ({ created: new Date(), modified: new Date() }))
    timestamps: { created: Date; modified: Date };

    _compute_totalValue(): number {
        return this.price * this.quantity;
    }

    constructor(sku: string, name: string, price: number, quantity: number) {
        this.sku = sku;
        this.name = name;
        this.price = price;
        this.quantity = quantity;
    }

    updatePrice(newPrice: number): void {
        this.price = newPrice;
    }

    updateQuantity(newQuantity: number): void {
        this.quantity = newQuantity;
    }
}

// Using property decorators
const user = new User('1', 'Alice', 'alice@example.com');

// Read-only property test
user.id = '2'; // Should show warning and not change
console.log(user.id); // Still '1'

// Observable property test
(user as any).addListener_name((newValue: string, oldValue: string) => {
    console.log(`Name changed from ${oldValue} to ${newValue}`);
});

user.name = 'Alice Smith'; // Should trigger listener

// Validation test
try {
    (user as any).email = 123; // Should throw error
} catch (error) {
    console.log(error.message);
}

// Range validation test
try {
    user.setAge(200); // Should throw error
} catch (error) {
    console.log(error.message);
}

user.setAge(25); // Should work
console.log(user.displayName); // 'Alice Smith (25 years old)'

// Required property test
try {
    console.log(user.username); // Should throw error (not set)
} catch (error) {
    console.log(error.message);
}

user.username = 'alice'; // Set required property
console.log(user.username); // 'alice'

// Format test
user.setBirthDate(new Date('1998-05-15'));
console.log((user as any).birthDateFormatted); // Formatted date

// Lazy loading test
console.log(user.createdAt); // Should initialize on first access

// Product example
const product = new Product('SKU123', 'Laptop', 999.99, 5);

// Observable properties
(product as any).addListener_name((newValue: string) => {
    console.log(`Product name updated to: ${newValue}`);
});

(product as any).addListener_price((newValue: number) => {
    console.log(`Product price updated to: $${newValue}`);
});

product.name = 'Gaming Laptop'; // Should trigger listener
product.updatePrice(1299.99); // Should trigger listener

// Computed property
console.log(product.totalValue); // Should be price * quantity
console.log((product as any).totalValueFormatted); // Formatted total value

product.updateQuantity(3);
console.log(product.totalValue); // Should recalculate

// Lazy timestamps
console.log(product.timestamps); // Should initialize on first access
""",
    )

    run_updater(typescript_decorators_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    property_decorators = [
        call
        for call in function_calls
        if "property_decorators" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in [
                "ReadOnly",
                "Observable",
                "ValidateType",
                "Range",
                "Required",
                "Computed",
            ]
        )
    ]

    assert len(property_decorators) >= 6, (
        f"Expected at least 6 property decorator functions, found {len(property_decorators)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    decorated_property_classes = [
        call
        for call in class_calls
        if "property_decorators" in call[0][1]["qualified_name"]
        and any(
            class_name in call[0][1]["qualified_name"]
            for class_name in ["User", "Product"]
        )
    ]

    assert len(decorated_property_classes) >= 2, (
        f"Expected at least 2 classes with decorated properties, found {len(decorated_property_classes)}"
    )


def test_parameter_decorators(
    typescript_decorators_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript parameter decorators."""
    test_file = typescript_decorators_project / "parameter_decorators.ts"
    test_file.write_text(
        encoding="utf-8",
        data=r"""
// Parameter decorators

// Simple parameter decorator
function Log(target: any, propertyKey: string | symbol | undefined, parameterIndex: number) {
    const existingMetadata = Reflect.getMetadata('log-parameters', target, propertyKey) || [];
    existingMetadata.push(parameterIndex);
    Reflect.defineMetadata('log-parameters', existingMetadata, target, propertyKey);
}

// Validation parameter decorator
function Validate(validator: (value: any) => boolean, message?: string) {
    return function(target: any, propertyKey: string | symbol | undefined, parameterIndex: number) {
        const existingValidators = Reflect.getMetadata('parameter-validators', target, propertyKey) || {};
        existingValidators[parameterIndex] = { validator, message };
        Reflect.defineMetadata('parameter-validators', existingValidators, target, propertyKey);
    };
}

// Required parameter decorator
function Required(target: any, propertyKey: string | symbol | undefined, parameterIndex: number) {
    const existingRequired = Reflect.getMetadata('required-parameters', target, propertyKey) || [];
    existingRequired.push(parameterIndex);
    Reflect.defineMetadata('required-parameters', existingRequired, target, propertyKey);
}

// Type validation decorator
function ValidateParam(type: string) {
    return function(target: any, propertyKey: string | symbol | undefined, parameterIndex: number) {
        const existingTypes = Reflect.getMetadata('parameter-types', target, propertyKey) || {};
        existingTypes[parameterIndex] = type;
        Reflect.defineMetadata('parameter-types', existingTypes, target, propertyKey);
    };
}

// Injection decorator (for dependency injection)
function Inject(token: string) {
    return function(target: any, propertyKey: string | symbol | undefined, parameterIndex: number) {
        const existingTokens = Reflect.getMetadata('inject-tokens', target, propertyKey) || {};
        existingTokens[parameterIndex] = token;
        Reflect.defineMetadata('inject-tokens', existingTokens, target, propertyKey);
    };
}

// Transform parameter decorator
function Transform(transformer: (value: any) => any) {
    return function(target: any, propertyKey: string | symbol | undefined, parameterIndex: number) {
        const existingTransformers = Reflect.getMetadata('parameter-transformers', target, propertyKey) || {};
        existingTransformers[parameterIndex] = transformer;
        Reflect.defineMetadata('parameter-transformers', existingTransformers, target, propertyKey);
    };
}

// Method decorator to process parameter metadata
function ProcessParams(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
    const originalMethod = descriptor.value;

    descriptor.value = function(...args: any[]) {
        // Get metadata
        const logParams = Reflect.getMetadata('log-parameters', target, propertyKey) || [];
        const validators = Reflect.getMetadata('parameter-validators', target, propertyKey) || {};
        const requiredParams = Reflect.getMetadata('required-parameters', target, propertyKey) || [];
        const typeValidators = Reflect.getMetadata('parameter-types', target, propertyKey) || {};
        const transformers = Reflect.getMetadata('parameter-transformers', target, propertyKey) || {};

        // Log parameters
        logParams.forEach((index: number) => {
            console.log(`Parameter ${index}: ${args[index]}`);
        });

        // Validate required parameters
        requiredParams.forEach((index: number) => {
            if (args[index] === undefined || args[index] === null) {
                throw new Error(`Parameter at index ${index} is required`);
            }
        });

        // Validate parameter types
        Object.keys(typeValidators).forEach(index => {
            const expectedType = typeValidators[index];
            const actualType = typeof args[index];
            if (actualType !== expectedType) {
                throw new Error(`Parameter at index ${index} must be of type ${expectedType}, got ${actualType}`);
            }
        });

        // Run custom validators
        Object.keys(validators).forEach(index => {
            const { validator, message } = validators[index];
            if (!validator(args[index])) {
                throw new Error(message || `Validation failed for parameter at index ${index}`);
            }
        });

        // Transform parameters
        Object.keys(transformers).forEach(index => {
            args[index] = transformers[index](args[index]);
        });

        return originalMethod.apply(this, args);
    };

    return descriptor;
}

// Mock Reflect implementation for metadata (normally would use reflect-metadata)
const Reflect = {
    metadata: new Map(),

    defineMetadata(key: string, value: any, target: any, propertyKey?: string | symbol) {
        const targetKey = `${target.constructor?.name || 'unknown'}.${String(propertyKey)}`;
        if (!this.metadata.has(targetKey)) {
            this.metadata.set(targetKey, new Map());
        }
        this.metadata.get(targetKey).set(key, value);
    },

    getMetadata(key: string, target: any, propertyKey?: string | symbol) {
        const targetKey = `${target.constructor?.name || 'unknown'}.${String(propertyKey)}`;
        return this.metadata.get(targetKey)?.get(key);
    }
};

// Service class using parameter decorators
class UserService {
    @ProcessParams
    createUser(
        @Required
        @ValidateParam('string')
        @Log
        name: string,

        @Required
        @Validate((email: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email), 'Invalid email format')
        @Log
        email: string,

        @ValidateParam('number')
        @Validate((age: number) => age >= 0 && age <= 150, 'Age must be between 0 and 150')
        age: number,

        @Transform((role: string) => role.toLowerCase())
        role: string = 'user'
    ): any {
        return { name, email, age, role, id: Math.random().toString() };
    }

    @ProcessParams
    updateUserRole(
        @Required
        @ValidateParam('string')
        userId: string,

        @Required
        @Transform((role: string) => role.toLowerCase())
        @Validate((role: string) => ['admin', 'user', 'moderator'].includes(role), 'Invalid role')
        newRole: string
    ): boolean {
        console.log(`Updated user ${userId} role to ${newRole}`);
        return true;
    }

    @ProcessParams
    searchUsers(
        @Transform((query: string) => query.trim().toLowerCase())
        @ValidateParam('string')
        query: string,

        @Validate((limit: number) => limit > 0 && limit <= 100, 'Limit must be between 1 and 100')
        @ValidateParam('number')
        limit: number = 10,

        @ValidateParam('number')
        offset: number = 0
    ): any[] {
        console.log(`Searching users: query="${query}", limit=${limit}, offset=${offset}`);
        return []; // Mock implementation
    }
}

// Dependency injection example
class DatabaseService {
    constructor(private connectionString: string) {}

    query(sql: string): any {
        console.log(`Executing query: ${sql}`);
        return { results: [] };
    }
}

class Logger {
    log(message: string): void {
        console.log(`[LOG] ${message}`);
    }
}

class OrderService {
    constructor(
        @Inject('DatabaseService') private db: DatabaseService,
        @Inject('Logger') private logger: Logger
    ) {}

    @ProcessParams
    createOrder(
        @Required
        @ValidateParam('string')
        customerId: string,

        @Required
        @Validate((items: any[]) => Array.isArray(items) && items.length > 0, 'Order must have at least one item')
        items: any[],

        @Transform((total: number) => Math.round(total * 100) / 100) // Round to 2 decimal places
        @ValidateParam('number')
        total: number
    ): any {
        this.logger.log(`Creating order for customer ${customerId}`);
        const order = { customerId, items, total, id: Math.random().toString() };
        this.db.query(`INSERT INTO orders ...`);
        return order;
    }
}

// API Controller with parameter decorators
class ApiController {
    @ProcessParams
    getUserById(
        @Required
        @ValidateParam('string')
        @Validate((id: string) => /^[a-zA-Z0-9]+$/.test(id), 'ID must be alphanumeric')
        @Transform((id: string) => id.toLowerCase())
        id: string
    ): any {
        return { id, name: 'User', email: 'user@example.com' };
    }

    @ProcessParams
    createPost(
        @Required
        @ValidateParam('string')
        @Transform((title: string) => title.trim())
        title: string,

        @Required
        @ValidateParam('string')
        @Validate((content: string) => content.length >= 10, 'Content must be at least 10 characters')
        content: string,

        @ValidateParam('string')
        @Validate((category: string) => ['tech', 'lifestyle', 'business'].includes(category), 'Invalid category')
        category: string = 'tech',

        @Validate((tags: string[]) => Array.isArray(tags) && tags.every(tag => typeof tag === 'string'), 'Tags must be an array of strings')
        tags: string[] = []
    ): any {
        return { title, content, category, tags, id: Math.random().toString() };
    }
}

// Using parameter decorators
const userService = new UserService();

// Test user creation
try {
    const user = userService.createUser('John Doe', 'john@example.com', 30, 'ADMIN');
    console.log('Created user:', user);
} catch (error) {
    console.error('Error creating user:', error.message);
}

// Test validation errors
try {
    userService.createUser('', 'invalid-email', 200); // Should fail validation
} catch (error) {
    console.error('Validation error:', error.message);
}

// Test role update
try {
    userService.updateUserRole('user123', 'MODERATOR'); // Should transform to lowercase
} catch (error) {
    console.error('Role update error:', error.message);
}

// Test search with transformation
const searchResults = userService.searchUsers('  JOHN DOE  ', 5, 0); // Should trim and lowercase query
console.log('Search results:', searchResults);

// Test API controller
const apiController = new ApiController();

try {
    const user = apiController.getUserById('USER123'); // Should transform to lowercase
    console.log('Retrieved user:', user);
} catch (error) {
    console.error('API error:', error.message);
}

try {
    const post = apiController.createPost('  My Blog Post  ', 'This is a great blog post about TypeScript decorators!', 'tech', ['typescript', 'decorators']);
    console.log('Created post:', post);
} catch (error) {
    console.error('Post creation error:', error.message);
}

// Mock dependency injection container
const container = new Map();
container.set('DatabaseService', new DatabaseService('postgresql://localhost:5432/mydb'));
container.set('Logger', new Logger());

// Manual dependency injection for demonstration
const orderService = new OrderService(
    container.get('DatabaseService'),
    container.get('Logger')
);

try {
    const order = orderService.createOrder('customer123', [{ item: 'laptop', price: 999.99 }], 999.989);
    console.log('Created order:', order);
} catch (error) {
    console.error('Order creation error:', error.message);
}
""",
    )

    run_updater(typescript_decorators_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    parameter_decorators = [
        call
        for call in function_calls
        if "parameter_decorators" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in [
                "Log",
                "Validate",
                "Required",
                "ValidateParam",
                "Inject",
                "Transform",
            ]
        )
    ]

    assert len(parameter_decorators) >= 6, (
        f"Expected at least 6 parameter decorator functions, found {len(parameter_decorators)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    parameter_decorated_classes = [
        call
        for call in class_calls
        if "parameter_decorators" in call[0][1]["qualified_name"]
        and any(
            class_name in call[0][1]["qualified_name"]
            for class_name in ["UserService", "OrderService", "ApiController"]
        )
    ]

    assert len(parameter_decorated_classes) >= 3, (
        f"Expected at least 3 classes with parameter decorators, found {len(parameter_decorated_classes)}"
    )

    method_calls = get_nodes(mock_ingestor, "Method")

    decorated_parameter_methods = [
        call
        for call in method_calls
        if "parameter_decorators" in call[0][1]["qualified_name"]
        and any(
            method_name in call[0][1]["qualified_name"]
            for method_name in [
                "createUser",
                "updateUserRole",
                "createOrder",
                "createPost",
            ]
        )
    ]

    assert len(decorated_parameter_methods) >= 3, (
        f"Expected at least 3 methods with decorated parameters, found {len(decorated_parameter_methods)}"
    )


def test_typescript_decorators_comprehensive(
    typescript_decorators_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all TypeScript decorator patterns are covered."""
    test_file = typescript_decorators_project / "comprehensive_decorators.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every TypeScript decorator pattern in one file

// Class decorator
function Entity(tableName: string) {
    return function<T extends new (...args: any[]) => {}>(constructor: T) {
        return class extends constructor {
            tableName = tableName;
        };
    };
}

// Method decorator
function Log(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
    const originalMethod = descriptor.value;
    descriptor.value = function(...args: any[]) {
        console.log(`Calling ${propertyKey}`);
        return originalMethod.apply(this, args);
    };
}

// Property decorator
function ReadOnly(target: any, propertyKey: string) {
    Object.defineProperty(target, propertyKey, {
        writable: false
    });
}

// Parameter decorator
function Required(target: any, propertyKey: string | symbol | undefined, parameterIndex: number) {
    console.log(`Parameter ${parameterIndex} in ${String(propertyKey)} is required`);
}

// Comprehensive decorated class
@Entity('comprehensive_items')
class ComprehensiveExample {
    @ReadOnly
    id: string = 'readonly-id';

    name: string;

    constructor(name: string) {
        this.name = name;
    }

    @Log
    processItem(@Required itemId: string): string {
        return `Processing ${itemId}`;
    }

    @Log
    updateName(@Required newName: string): void {
        this.name = newName;
    }
}

// Using all decorator types
const example = new ComprehensiveExample('Test Item');
console.log(example.processItem('item123'));
example.updateName('Updated Item');
console.log((example as any).tableName); // 'comprehensive_items'
""",
    )

    run_updater(typescript_decorators_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    calls_relationships = get_relationships(mock_ingestor, "CALLS")
    [c for c in all_relationships if c.args[1] == "DEFINES"]

    comprehensive_calls = [
        call
        for call in calls_relationships
        if "comprehensive_decorators" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 2, (
        f"Expected at least 2 comprehensive decorator calls, found {len(comprehensive_calls)}"
    )

    function_calls = get_nodes(mock_ingestor, "Function")

    comprehensive_decorators = [
        call
        for call in function_calls
        if "comprehensive_decorators" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in ["Entity", "Log", "ReadOnly", "Required"]
        )
    ]

    assert len(comprehensive_decorators) >= 4, (
        f"Expected at least 4 decorator functions, found {len(comprehensive_decorators)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    comprehensive_class = [
        call
        for call in class_calls
        if "ComprehensiveExample" in call[0][1]["qualified_name"]
    ]

    assert len(comprehensive_class) >= 1, (
        f"Expected ComprehensiveExample class, found {len(comprehensive_class)}"
    )
