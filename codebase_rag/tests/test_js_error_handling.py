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
def javascript_error_handling_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with error handling patterns."""
    project_path = temp_repo / "javascript_error_handling_test"
    project_path.mkdir()

    (project_path / "utils").mkdir()
    (project_path / "errors").mkdir()

    (project_path / "errors" / "custom.js").write_text(
        encoding="utf-8",
        data="""
export class CustomError extends Error {
    constructor(message, code) {
        super(message);
        this.name = 'CustomError';
        this.code = code;
    }
}
""",
    )

    return project_path


def test_try_catch_finally_blocks(
    javascript_error_handling_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test try/catch/finally block patterns."""
    test_file = javascript_error_handling_project / "try_catch_finally.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic try/catch patterns

function basicTryCatch() {
    try {
        const result = riskyOperation();
        return result;
    } catch (error) {
        console.error('Operation failed:', error.message);
        return null;
    }
}

function tryCatchFinally() {
    let resource = null;

    try {
        resource = acquireResource();
        const result = processResource(resource);
        return result;
    } catch (error) {
        console.error('Processing failed:', error);
        throw error; // Re-throw after logging
    } finally {
        if (resource) {
            releaseResource(resource);
        }
    }
}

// Multiple catch patterns (pre-ES2019)
function multipleCatch() {
    try {
        const data = parseData(input);
        const result = validateData(data);
        return result;
    } catch (error) {
        if (error instanceof SyntaxError) {
            console.error('Parse error:', error.message);
            return { error: 'PARSE_ERROR' };
        } else if (error instanceof TypeError) {
            console.error('Type error:', error.message);
            return { error: 'TYPE_ERROR' };
        } else if (error instanceof ReferenceError) {
            console.error('Reference error:', error.message);
            return { error: 'REFERENCE_ERROR' };
        } else {
            console.error('Unknown error:', error);
            return { error: 'UNKNOWN_ERROR' };
        }
    }
}

// Nested try/catch
function nestedTryCatch() {
    try {
        const config = loadConfig();

        try {
            const connection = connect(config);
            return processConnection(connection);
        } catch (connectionError) {
            console.error('Connection failed:', connectionError);

            try {
                const fallback = createFallbackConnection();
                return processConnection(fallback);
            } catch (fallbackError) {
                console.error('Fallback failed:', fallbackError);
                throw new Error('All connection attempts failed');
            }
        }
    } catch (configError) {
        console.error('Config loading failed:', configError);
        throw new Error('Cannot initialize without config');
    }
}

// Try/catch in loops
function tryCatchInLoop(items) {
    const results = [];
    const errors = [];

    for (const item of items) {
        try {
            const result = processItem(item);
            results.push(result);
        } catch (error) {
            console.error(`Failed to process item ${item.id}:`, error);
            errors.push({ item: item.id, error: error.message });
            continue; // Continue with next item
        }
    }

    return { results, errors };
}

// Try/catch with return in finally
function finallyWithReturn() {
    try {
        return performOperation();
    } catch (error) {
        console.error('Operation failed:', error);
        return null;
    } finally {
        cleanup();
        // Note: return here would override try/catch returns
    }
}

// Class methods with error handling
class DataProcessor {
    constructor(config) {
        this.config = config;
        this.errorCount = 0;
    }

    process(data) {
        try {
            this.validateInput(data);
            const transformed = this.transform(data);
            const validated = this.validate(transformed);
            return validated;
        } catch (error) {
            this.errorCount++;
            this.logError(error);
            throw error;
        } finally {
            this.updateStats();
        }
    }

    validateInput(data) {
        if (!data) {
            throw new Error('Data is required');
        }
        if (typeof data !== 'object') {
            throw new TypeError('Data must be an object');
        }
    }

    transform(data) {
        try {
            return this.config.transformer(data);
        } catch (error) {
            throw new Error(`Transform failed: ${error.message}`);
        }
    }

    validate(data) {
        try {
            if (!this.config.validator(data)) {
                throw new Error('Validation failed');
            }
            return data;
        } catch (error) {
            throw new Error(`Validation error: ${error.message}`);
        }
    }

    logError(error) {
        console.error(`[DataProcessor] Error #${this.errorCount}:`, error);
    }

    updateStats() {
        console.log(`Total errors: ${this.errorCount}`);
    }

    // Method with optional error handling
    safeProcess(data, throwOnError = false) {
        try {
            return this.process(data);
        } catch (error) {
            if (throwOnError) {
                throw error;
            }
            return { error: error.message };
        }
    }
}

// Error handling in different contexts
const asyncContext = {
    async processAsync(data) {
        try {
            const result = await this.asyncOperation(data);
            return result;
        } catch (error) {
            console.error('Async operation failed:', error);
            throw error;
        } finally {
            await this.cleanup();
        }
    },

    async asyncOperation(data) {
        // Simulate async operation
        return new Promise((resolve, reject) => {
            setTimeout(() => {
                if (data.valid) {
                    resolve(data);
                } else {
                    reject(new Error('Invalid data'));
                }
            }, 100);
        });
    },

    async cleanup() {
        // Cleanup logic
        console.log('Cleanup completed');
    }
};

// Error boundaries pattern
function withErrorBoundary(fn, fallback) {
    return function(...args) {
        try {
            return fn.apply(this, args);
        } catch (error) {
            console.error('Error boundary caught:', error);
            return fallback ? fallback(error, ...args) : undefined;
        }
    };
}

// Using error handling
const processor = new DataProcessor({
    transformer: data => ({ ...data, processed: true }),
    validator: data => data.processed === true
});

try {
    const result = processor.process({ input: 'test' });
    console.log('Success:', result);
} catch (error) {
    console.error('Processing failed:', error.message);
}

const safeResult = processor.safeProcess({ invalid: true });
console.log('Safe result:', safeResult);

// Protected function call
const protectedFunction = withErrorBoundary(
    (data) => {
        if (!data) throw new Error('No data provided');
        return data.value * 2;
    },
    (error) => {
        console.error('Fallback triggered:', error.message);
        return 0;
    }
);

console.log(protectedFunction({ value: 5 })); // 10
console.log(protectedFunction(null)); // 0 (fallback)

// Error handling utilities
function riskyOperation() {
    if (Math.random() > 0.5) {
        throw new Error('Random failure');
    }
    return 'Success';
}

function acquireResource() {
    return { id: Date.now(), active: true };
}

function releaseResource(resource) {
    resource.active = false;
    console.log(`Resource ${resource.id} released`);
}

function processResource(resource) {
    if (!resource.active) {
        throw new Error('Resource is not active');
    }
    return `Processed resource ${resource.id}`;
}

function parseData(input) {
    return JSON.parse(input);
}

function validateData(data) {
    if (!data.valid) {
        throw new Error('Data validation failed');
    }
    return data;
}

function loadConfig() {
    return { endpoint: 'https://api.example.com' };
}

function connect(config) {
    if (!config.endpoint) {
        throw new Error('No endpoint configured');
    }
    return { connected: true, endpoint: config.endpoint };
}

function processConnection(connection) {
    return `Connected to ${connection.endpoint}`;
}

function createFallbackConnection() {
    return { connected: true, endpoint: 'fallback' };
}

function processItem(item) {
    if (!item.id) {
        throw new Error('Item must have an ID');
    }
    return { ...item, processed: true };
}

function performOperation() {
    return 'Operation completed';
}

function cleanup() {
    console.log('Cleanup performed');
}
""",
    )

    run_updater(javascript_error_handling_project, mock_ingestor)

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_functions = [
        "basicTryCatch",
        "tryCatchFinally",
        "multipleCatch",
        "nestedTryCatch",
        "tryCatchInLoop",
        "withErrorBoundary",
    ]

    error_handling_functions = [
        func
        for func in created_functions
        if any(expected in func for expected in expected_functions)
    ]

    assert len(error_handling_functions) >= 4, (
        f"Expected at least 4 error handling functions, found {len(error_handling_functions)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    data_processor_class = [
        call for call in class_calls if "DataProcessor" in call[0][1]["qualified_name"]
    ]

    assert len(data_processor_class) >= 1, (
        f"Expected DataProcessor class with error handling, found {len(data_processor_class)}"
    )


def test_custom_error_classes(
    javascript_error_handling_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test custom error class definitions and usage."""
    test_file = javascript_error_handling_project / "custom_errors.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Custom error classes

// Basic custom error
class CustomError extends Error {
    constructor(message, code) {
        super(message);
        this.name = 'CustomError';
        this.code = code;

        // Maintain proper stack trace (V8)
        if (Error.captureStackTrace) {
            Error.captureStackTrace(this, CustomError);
        }
    }
}

// Validation error
class ValidationError extends Error {
    constructor(message, field, value) {
        super(message);
        this.name = 'ValidationError';
        this.field = field;
        this.value = value;
    }

    toString() {
        return `${this.name}: ${this.message} (field: ${this.field}, value: ${this.value})`;
    }
}

// Network error
class NetworkError extends Error {
    constructor(message, status, url) {
        super(message);
        this.name = 'NetworkError';
        this.status = status;
        this.url = url;
        this.timestamp = new Date();
    }

    isRetryable() {
        return this.status >= 500 || this.status === 429;
    }

    getDetails() {
        return {
            message: this.message,
            status: this.status,
            url: this.url,
            timestamp: this.timestamp,
            retryable: this.isRetryable()
        };
    }
}

// Application-specific error
class BusinessLogicError extends Error {
    constructor(message, errorCode, context) {
        super(message);
        this.name = 'BusinessLogicError';
        this.errorCode = errorCode;
        this.context = context;
    }

    static fromCode(code, context) {
        const messages = {
            'INSUFFICIENT_FUNDS': 'Insufficient funds for transaction',
            'INVALID_PERMISSIONS': 'User does not have required permissions',
            'RESOURCE_NOT_FOUND': 'Requested resource was not found',
            'DUPLICATE_ENTRY': 'Entry already exists'
        };

        return new BusinessLogicError(
            messages[code] || 'Unknown business logic error',
            code,
            context
        );
    }
}

// Async operation error
class AsyncOperationError extends Error {
    constructor(message, operation, cause) {
        super(message);
        this.name = 'AsyncOperationError';
        this.operation = operation;
        this.cause = cause;
    }

    static wrap(error, operation) {
        return new AsyncOperationError(
            `Operation '${operation}' failed: ${error.message}`,
            operation,
            error
        );
    }
}

// Error factory
class ErrorFactory {
    static createValidationError(field, value, rule) {
        const message = `Validation failed for field '${field}': ${rule}`;
        return new ValidationError(message, field, value);
    }

    static createNetworkError(response) {
        const message = `Network request failed: ${response.status} ${response.statusText}`;
        return new NetworkError(message, response.status, response.url);
    }

    static createBusinessError(code, context) {
        return BusinessLogicError.fromCode(code, context);
    }
}

// Error handling service
class ErrorHandler {
    constructor() {
        this.errorCounts = new Map();
        this.errorCallbacks = new Map();
    }

    handle(error) {
        this.logError(error);
        this.incrementCount(error.constructor.name);
        this.notifyCallbacks(error);
    }

    logError(error) {
        if (error instanceof NetworkError) {
            console.error('Network Error:', error.getDetails());
        } else if (error instanceof ValidationError) {
            console.error('Validation Error:', error.toString());
        } else if (error instanceof BusinessLogicError) {
            console.error('Business Logic Error:', {
                code: error.errorCode,
                message: error.message,
                context: error.context
            });
        } else {
            console.error('General Error:', error.message, error);
        }
    }

    incrementCount(errorType) {
        const current = this.errorCounts.get(errorType) || 0;
        this.errorCounts.set(errorType, current + 1);
    }

    notifyCallbacks(error) {
        const callbacks = this.errorCallbacks.get(error.constructor.name) || [];
        callbacks.forEach(callback => {
            try {
                callback(error);
            } catch (callbackError) {
                console.error('Error in error callback:', callbackError);
            }
        });
    }

    onError(errorType, callback) {
        const callbacks = this.errorCallbacks.get(errorType) || [];
        callbacks.push(callback);
        this.errorCallbacks.set(errorType, callbacks);
    }

    getErrorStats() {
        return Object.fromEntries(this.errorCounts);
    }

    reset() {
        this.errorCounts.clear();
        this.errorCallbacks.clear();
    }
}

// Functions that throw custom errors
function validateEmail(email) {
    if (!email) {
        throw new ValidationError('Email is required', 'email', email);
    }
    if (!email.includes('@')) {
        throw new ValidationError('Email must contain @', 'email', email);
    }
    return true;
}

function validateAge(age) {
    if (age === undefined || age === null) {
        throw new ValidationError('Age is required', 'age', age);
    }
    if (typeof age !== 'number') {
        throw new ValidationError('Age must be a number', 'age', age);
    }
    if (age < 0 || age > 150) {
        throw new ValidationError('Age must be between 0 and 150', 'age', age);
    }
    return true;
}

async function fetchData(url) {
    try {
        const response = await fetch(url);
        if (!response.ok) {
            throw ErrorFactory.createNetworkError(response);
        }
        return await response.json();
    } catch (error) {
        if (error instanceof NetworkError) {
            throw error;
        }
        throw AsyncOperationError.wrap(error, 'fetchData');
    }
}

function processTransaction(amount, balance) {
    if (amount > balance) {
        throw ErrorFactory.createBusinessError('INSUFFICIENT_FUNDS', {
            amount,
            balance,
            shortfall: amount - balance
        });
    }
    return balance - amount;
}

// User registration with multiple validation
function registerUser(userData) {
    try {
        validateEmail(userData.email);
        validateAge(userData.age);

        // Simulate duplicate check
        if (userData.email === 'existing@example.com') {
            throw ErrorFactory.createBusinessError('DUPLICATE_ENTRY', {
                field: 'email',
                value: userData.email
            });
        }

        return { id: Date.now(), ...userData, registered: true };
    } catch (error) {
        // Re-throw with additional context
        if (error instanceof ValidationError || error instanceof BusinessLogicError) {
            error.context = { ...error.context, operation: 'registerUser' };
        }
        throw error;
    }
}

// Using custom errors
const errorHandler = new ErrorHandler();

// Set up error callbacks
errorHandler.onError('ValidationError', (error) => {
    console.log(`Validation failed for ${error.field}`);
});

errorHandler.onError('NetworkError', (error) => {
    if (error.isRetryable()) {
        console.log('Network error is retryable');
    }
});

// Test custom errors
try {
    validateEmail('invalid-email');
} catch (error) {
    errorHandler.handle(error);
}

try {
    validateAge(-5);
} catch (error) {
    errorHandler.handle(error);
}

try {
    processTransaction(100, 50);
} catch (error) {
    errorHandler.handle(error);
}

try {
    registerUser({ email: 'existing@example.com', age: 25 });
} catch (error) {
    errorHandler.handle(error);
}

// Async error handling
fetchData('/api/invalid-endpoint')
    .catch(error => {
        errorHandler.handle(error);
    });

console.log('Error statistics:', errorHandler.getErrorStats());

// Error type checking
function handleError(error) {
    if (error instanceof ValidationError) {
        return `Validation failed: ${error.field}`;
    } else if (error instanceof NetworkError) {
        return `Network error: ${error.status}`;
    } else if (error instanceof BusinessLogicError) {
        return `Business error: ${error.errorCode}`;
    } else {
        return `Unknown error: ${error.message}`;
    }
}

// Error aggregation
class ErrorAggregator {
    constructor() {
        this.errors = [];
    }

    add(error) {
        this.errors.push({
            error,
            timestamp: new Date(),
            type: error.constructor.name
        });
    }

    getByType(errorType) {
        return this.errors.filter(entry => entry.type === errorType);
    }

    getRecent(minutes = 5) {
        const cutoff = new Date(Date.now() - minutes * 60 * 1000);
        return this.errors.filter(entry => entry.timestamp > cutoff);
    }

    clear() {
        this.errors = [];
    }

    hasErrors() {
        return this.errors.length > 0;
    }
}

const aggregator = new ErrorAggregator();

// Collect errors
try {
    validateEmail('');
} catch (error) {
    aggregator.add(error);
}

try {
    processTransaction(200, 100);
} catch (error) {
    aggregator.add(error);
}

console.log('Validation errors:', aggregator.getByType('ValidationError').length);
console.log('Business errors:', aggregator.getByType('BusinessLogicError').length);
console.log('Recent errors:', aggregator.getRecent(1).length);
""",
    )

    run_updater(javascript_error_handling_project, mock_ingestor)

    created_classes = get_node_names(mock_ingestor, "Class")

    expected_error_classes = [
        "CustomError",
        "ValidationError",
        "NetworkError",
        "BusinessLogicError",
        "AsyncOperationError",
        "ErrorFactory",
        "ErrorHandler",
        "ErrorAggregator",
    ]

    custom_error_classes = [
        cls
        for cls in created_classes
        if any(expected in cls for expected in expected_error_classes)
    ]

    assert len(custom_error_classes) >= 5, (
        f"Expected at least 5 custom error classes, found {len(custom_error_classes)}"
    )

    inheritance_relationships = get_relationships(mock_ingestor, "INHERITS")

    error_inheritance = [
        call
        for call in inheritance_relationships
        if any(
            error_name in call.args[0][2]
            for error_name in ["CustomError", "ValidationError", "NetworkError"]
        )
    ]

    assert len(error_inheritance) >= 2, (
        f"Expected at least 2 error inheritance relationships, found {len(error_inheritance)}"
    )


def test_async_error_handling(
    javascript_error_handling_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test async function error handling patterns."""
    test_file = javascript_error_handling_project / "async_errors.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Async error handling patterns

// Basic async/await error handling
async function basicAsync() {
    try {
        const result = await asyncOperation();
        return result;
    } catch (error) {
        console.error('Async operation failed:', error);
        throw error;
    }
}

// Multiple async operations
async function multipleAsync() {
    try {
        const data = await fetchData();
        const processed = await processData(data);
        const saved = await saveData(processed);
        return saved;
    } catch (error) {
        console.error('Pipeline failed at:', error.step || 'unknown');
        throw error;
    }
}

// Parallel async operations with error handling
async function parallelAsync() {
    try {
        const [user, posts, comments] = await Promise.all([
            fetchUser(),
            fetchPosts(),
            fetchComments()
        ]);

        return { user, posts, comments };
    } catch (error) {
        console.error('One or more parallel operations failed:', error);
        throw error;
    }
}

// Sequential error handling with partial success
async function sequentialWithPartial(items) {
    const results = [];
    const errors = [];

    for (const item of items) {
        try {
            const result = await processItem(item);
            results.push(result);
        } catch (error) {
            console.error(`Failed to process item ${item.id}:`, error);
            errors.push({ item: item.id, error: error.message });
        }
    }

    return { results, errors };
}

// Promise.allSettled pattern
async function allSettledPattern(urls) {
    const promises = urls.map(url => fetchData(url));
    const results = await Promise.allSettled(promises);

    const successful = [];
    const failed = [];

    results.forEach((result, index) => {
        if (result.status === 'fulfilled') {
            successful.push({ url: urls[index], data: result.value });
        } else {
            failed.push({ url: urls[index], error: result.reason });
        }
    });

    return { successful, failed };
}

// Retry mechanism with async
async function withRetry(operation, maxRetries = 3, delay = 1000) {
    let lastError;

    for (let attempt = 1; attempt <= maxRetries; attempt++) {
        try {
            return await operation();
        } catch (error) {
            lastError = error;
            console.warn(`Attempt ${attempt} failed:`, error.message);

            if (attempt === maxRetries) {
                break;
            }

            // Wait before retry
            await new Promise(resolve => setTimeout(resolve, delay));
        }
    }

    throw new Error(`Operation failed after ${maxRetries} attempts: ${lastError.message}`);
}

// Timeout wrapper for async operations
async function withTimeout(promise, timeoutMs) {
    const timeoutPromise = new Promise((_, reject) => {
        setTimeout(() => reject(new Error('Operation timed out')), timeoutMs);
    });

    try {
        return await Promise.race([promise, timeoutPromise]);
    } catch (error) {
        if (error.message === 'Operation timed out') {
            console.error(`Operation timed out after ${timeoutMs}ms`);
        }
        throw error;
    }
}

// Circuit breaker pattern
class CircuitBreaker {
    constructor(operation, options = {}) {
        this.operation = operation;
        this.failureThreshold = options.failureThreshold || 5;
        this.recoveryTimeout = options.recoveryTimeout || 60000;
        this.monitoringPeriod = options.monitoringPeriod || 10000;

        this.state = 'CLOSED'; // CLOSED, OPEN, HALF_OPEN
        this.failureCount = 0;
        this.lastFailureTime = null;
        this.successCount = 0;
    }

    async execute(...args) {
        if (this.state === 'OPEN') {
            if (Date.now() - this.lastFailureTime > this.recoveryTimeout) {
                this.state = 'HALF_OPEN';
                this.successCount = 0;
            } else {
                throw new Error('Circuit breaker is OPEN');
            }
        }

        try {
            const result = await this.operation(...args);
            this.onSuccess();
            return result;
        } catch (error) {
            this.onFailure();
            throw error;
        }
    }

    onSuccess() {
        this.failureCount = 0;

        if (this.state === 'HALF_OPEN') {
            this.successCount++;
            if (this.successCount >= 3) {
                this.state = 'CLOSED';
            }
        }
    }

    onFailure() {
        this.failureCount++;
        this.lastFailureTime = Date.now();

        if (this.failureCount >= this.failureThreshold) {
            this.state = 'OPEN';
        }
    }

    getState() {
        return {
            state: this.state,
            failureCount: this.failureCount,
            successCount: this.successCount,
            lastFailureTime: this.lastFailureTime
        };
    }
}

// Async error aggregation
class AsyncErrorCollector {
    constructor() {
        this.errors = [];
        this.isCollecting = false;
    }

    async collect(asyncOperations) {
        this.isCollecting = true;
        this.errors = [];

        const results = await Promise.allSettled(asyncOperations);

        results.forEach((result, index) => {
            if (result.status === 'rejected') {
                this.errors.push({
                    index,
                    error: result.reason,
                    timestamp: new Date()
                });
            }
        });

        this.isCollecting = false;
        return this.errors;
    }

    hasErrors() {
        return this.errors.length > 0;
    }

    getErrors() {
        return [...this.errors];
    }

    clear() {
        this.errors = [];
    }
}

// Graceful degradation with async
class GracefulService {
    constructor() {
        this.primaryEndpoint = 'https://api.primary.com';
        this.fallbackEndpoint = 'https://api.fallback.com';
        this.cache = new Map();
    }

    async getData(id) {
        // Try cache first
        if (this.cache.has(id)) {
            return this.cache.get(id);
        }

        try {
            // Try primary endpoint
            const data = await this.fetchFromPrimary(id);
            this.cache.set(id, data);
            return data;
        } catch (primaryError) {
            console.warn('Primary endpoint failed:', primaryError.message);

            try {
                // Try fallback endpoint
                const data = await this.fetchFromFallback(id);
                this.cache.set(id, data);
                return data;
            } catch (fallbackError) {
                console.error('Fallback endpoint failed:', fallbackError.message);

                // Return cached data if available
                const cachedData = this.getCachedData(id);
                if (cachedData) {
                    console.warn('Returning stale cached data');
                    return cachedData;
                }

                // Last resort: return minimal data
                return this.getMinimalData(id);
            }
        }
    }

    async fetchFromPrimary(id) {
        const response = await fetch(`${this.primaryEndpoint}/data/${id}`);
        if (!response.ok) {
            throw new Error(`Primary fetch failed: ${response.status}`);
        }
        return response.json();
    }

    async fetchFromFallback(id) {
        const response = await fetch(`${this.fallbackEndpoint}/data/${id}`);
        if (!response.ok) {
            throw new Error(`Fallback fetch failed: ${response.status}`);
        }
        return response.json();
    }

    getCachedData(id) {
        return this.cache.get(id);
    }

    getMinimalData(id) {
        return {
            id,
            error: 'Service unavailable',
            timestamp: new Date(),
            source: 'minimal'
        };
    }
}

// Using async error handling
async function demonstrateAsyncErrors() {
    try {
        // Basic usage
        const result1 = await basicAsync();
        console.log('Basic async result:', result1);

        // Multiple operations
        const result2 = await multipleAsync();
        console.log('Multiple async result:', result2);

        // Parallel operations
        const result3 = await parallelAsync();
        console.log('Parallel async result:', result3);

        // With retry
        const result4 = await withRetry(async () => {
            const data = await fetchData('/api/unreliable');
            return data;
        }, 3, 500);
        console.log('Retry result:', result4);

        // With timeout
        const result5 = await withTimeout(
            fetchData('/api/slow'),
            5000
        );
        console.log('Timeout result:', result5);

    } catch (error) {
        console.error('Demonstration failed:', error);
    }
}

// Circuit breaker usage
const unreliableService = async () => {
    if (Math.random() > 0.7) {
        throw new Error('Service unavailable');
    }
    return 'Service response';
};

const breaker = new CircuitBreaker(unreliableService, {
    failureThreshold: 3,
    recoveryTimeout: 5000
});

async function testCircuitBreaker() {
    for (let i = 0; i < 10; i++) {
        try {
            const result = await breaker.execute();
            console.log(`Attempt ${i + 1}: ${result}`);
        } catch (error) {
            console.error(`Attempt ${i + 1} failed: ${error.message}`);
            console.log('Circuit breaker state:', breaker.getState());
        }

        // Wait between attempts
        await new Promise(resolve => setTimeout(resolve, 1000));
    }
}

// Error collector usage
const collector = new AsyncErrorCollector();

async function testErrorCollection() {
    const operations = [
        fetchData('/api/endpoint1'),
        fetchData('/api/endpoint2'),
        fetchData('/api/endpoint3')
    ];

    const errors = await collector.collect(operations);

    if (collector.hasErrors()) {
        console.log('Collected errors:', errors);
    }
}

// Graceful service usage
const gracefulService = new GracefulService();

async function testGracefulDegradation() {
    try {
        const data = await gracefulService.getData('user123');
        console.log('Graceful service data:', data);
    } catch (error) {
        console.error('Even graceful degradation failed:', error);
    }
}

// Mock functions for testing
async function asyncOperation() {
    return 'Async result';
}

async function fetchData(url = '/api/data') {
    if (url.includes('invalid')) {
        throw new Error('Invalid endpoint');
    }
    return { data: 'mock data', url };
}

async function processData(data) {
    return { ...data, processed: true };
}

async function saveData(data) {
    return { ...data, saved: true, id: Date.now() };
}

async function fetchUser() {
    return { id: 1, name: 'User' };
}

async function fetchPosts() {
    return [{ id: 1, title: 'Post' }];
}

async function fetchComments() {
    return [{ id: 1, text: 'Comment' }];
}

async function processItem(item) {
    if (!item.id) {
        throw new Error('Item missing ID');
    }
    return { ...item, processed: true };
}

// Run demonstrations
demonstrateAsyncErrors();
testCircuitBreaker();
testErrorCollection();
testGracefulDegradation();
""",
    )

    run_updater(javascript_error_handling_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    async_error_functions = [
        call
        for call in function_calls
        if "async_errors" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in ["basicAsync", "multipleAsync", "withRetry", "withTimeout"]
        )
    ]

    assert len(async_error_functions) >= 4, (
        f"Expected at least 4 async error handling functions, found {len(async_error_functions)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    async_error_classes = [
        call
        for call in class_calls
        if "async_errors" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in ["CircuitBreaker", "AsyncErrorCollector", "GracefulService"]
        )
    ]

    assert len(async_error_classes) >= 3, (
        f"Expected at least 3 async error handling classes, found {len(async_error_classes)}"
    )


def test_error_handling_comprehensive(
    javascript_error_handling_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all error handling patterns are covered."""
    test_file = javascript_error_handling_project / "comprehensive_errors.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every JavaScript error handling pattern in one file

// Custom error class
class AppError extends Error {
    constructor(message, code) {
        super(message);
        this.name = 'AppError';
        this.code = code;
    }
}

// Try/catch with custom error
function validateInput(data) {
    try {
        if (!data) {
            throw new AppError('Data is required', 'MISSING_DATA');
        }
        if (typeof data !== 'object') {
            throw new AppError('Data must be an object', 'INVALID_TYPE');
        }
        return data;
    } catch (error) {
        console.error('Validation error:', error);
        throw error;
    }
}

// Async error handling
async function processAsync(data) {
    try {
        const validated = validateInput(data);
        const result = await performAsyncOperation(validated);
        return result;
    } catch (error) {
        if (error instanceof AppError) {
            console.error('App error:', error.code, error.message);
        } else {
            console.error('Unexpected error:', error);
        }
        throw error;
    }
}

// Error boundary function
function withErrorHandling(fn) {
    return function(...args) {
        try {
            return fn.apply(this, args);
        } catch (error) {
            console.error('Error boundary caught:', error);
            return null;
        }
    };
}

// Class with error handling
class ErrorHandler {
    constructor() {
        this.errors = [];
    }

    handle(error) {
        this.errors.push({ error, timestamp: new Date() });

        if (error instanceof AppError) {
            this.handleAppError(error);
        } else {
            this.handleGenericError(error);
        }
    }

    handleAppError(error) {
        console.log(`App error ${error.code}: ${error.message}`);
    }

    handleGenericError(error) {
        console.log(`Generic error: ${error.message}`);
    }

    getErrors() {
        return [...this.errors];
    }
}

// Using all error patterns
const handler = new ErrorHandler();

try {
    validateInput(null);
} catch (error) {
    handler.handle(error);
}

const safeFunction = withErrorHandling((data) => {
    if (!data.valid) {
        throw new Error('Invalid data');
    }
    return data.value;
});

processAsync({ valid: true }).catch(error => {
    handler.handle(error);
});

console.log(safeFunction({ valid: false })); // null (caught by boundary)
console.log('Total errors:', handler.getErrors().length);

// Mock async operation
async function performAsyncOperation(data) {
    return new Promise((resolve, reject) => {
        setTimeout(() => {
            if (data.valid === false) {
                reject(new Error('Async operation failed'));
            } else {
                resolve({ ...data, processed: true });
            }
        }, 100);
    });
}
""",
    )

    run_updater(javascript_error_handling_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    calls_relationships = get_relationships(mock_ingestor, "CALLS")
    [c for c in all_relationships if c.args[1] == "DEFINES"]

    comprehensive_calls = [
        call
        for call in calls_relationships
        if "comprehensive_errors" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 3, (
        f"Expected at least 3 comprehensive error calls, found {len(comprehensive_calls)}"
    )

    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    comprehensive_nodes = [
        call
        for call in all_nodes
        if "comprehensive_errors" in call[0][1].get("qualified_name", "")
    ]

    assert len(comprehensive_nodes) >= 5, (
        f"Expected at least 5 error handling nodes, found {len(comprehensive_nodes)}"
    )
