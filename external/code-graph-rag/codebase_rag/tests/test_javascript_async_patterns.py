from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def javascript_async_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with all async patterns."""
    project_path = temp_repo / "javascript_async_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "utils").mkdir()
    (project_path / "api").mkdir()

    (project_path / "src" / "helpers.js").write_text(
        encoding="utf-8",
        data="export const delay = ms => new Promise(resolve => setTimeout(resolve, ms));",
    )
    (project_path / "utils" / "common.js").write_text(
        encoding="utf-8",
        data="export function handleError(error) { console.error(error); }",
    )

    return project_path


def test_promise_patterns(
    javascript_async_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Promise creation, chaining, and error handling patterns."""
    test_file = javascript_async_project / "promise_patterns.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic Promise creation
function createSimplePromise() {
    return new Promise((resolve, reject) => {
        const success = Math.random() > 0.5;
        setTimeout(() => {
            if (success) {
                resolve("Success!");
            } else {
                reject(new Error("Failed!"));
            }
        }, 1000);
    });
}

function fetchUserData(userId) {
    return new Promise((resolve, reject) => {
        // Simulate API call
        setTimeout(() => {
            if (userId > 0) {
                resolve({ id: userId, name: `User ${userId}`, active: true });
            } else {
                reject(new Error("Invalid user ID"));
            }
        }, 500);
    });
}

function loadConfig() {
    return new Promise((resolve) => {
        const config = {
            apiUrl: "https://api.example.com",
            timeout: 5000,
            retries: 3
        };
        resolve(config);
    });
}

// Promise chaining
function processUserData(userId) {
    return fetchUserData(userId)
        .then(user => {
            console.log("User fetched:", user);
            return user;
        })
        .then(user => {
            return { ...user, processed: true, timestamp: Date.now() };
        })
        .then(processedUser => {
            console.log("User processed:", processedUser);
            return processedUser;
        })
        .catch(error => {
            console.error("Error processing user:", error);
            throw error;
        });
}

function chainedApiCall() {
    return loadConfig()
        .then(config => {
            return fetch(config.apiUrl + "/users");
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            return response.json();
        })
        .then(users => {
            return users.map(user => ({ ...user, loaded: Date.now() }));
        })
        .catch(error => {
            console.error("Chained API call failed:", error);
            return [];
        });
}

// Promise.all patterns
function fetchAllUsers(userIds) {
    const promises = userIds.map(id => fetchUserData(id));

    return Promise.all(promises)
        .then(users => {
            console.log("All users fetched:", users);
            return users;
        })
        .catch(error => {
            console.error("Failed to fetch all users:", error);
            throw error;
        });
}

function loadApplicationData() {
    const configPromise = loadConfig();
    const usersPromise = fetchAllUsers([1, 2, 3]);
    const settingsPromise = Promise.resolve({ theme: "dark", lang: "en" });

    return Promise.all([configPromise, usersPromise, settingsPromise])
        .then(([config, users, settings]) => {
            return { config, users, settings, ready: true };
        });
}

// Promise.allSettled patterns
function fetchWithFallbacks(endpoints) {
    const promises = endpoints.map(endpoint => fetch(endpoint));

    return Promise.allSettled(promises)
        .then(results => {
            const successful = results
                .filter(result => result.status === "fulfilled")
                .map(result => result.value);

            const failed = results
                .filter(result => result.status === "rejected")
                .map(result => result.reason);

            return { successful, failed };
        });
}

// Promise.race patterns
function fetchWithTimeout(url, timeout = 5000) {
    const fetchPromise = fetch(url);
    const timeoutPromise = new Promise((_, reject) =>
        setTimeout(() => reject(new Error("Timeout")), timeout)
    );

    return Promise.race([fetchPromise, timeoutPromise])
        .then(response => response.json())
        .catch(error => {
            if (error.message === "Timeout") {
                console.log("Request timed out");
            }
            throw error;
        });
}

function quickestResponse(urls) {
    const promises = urls.map(url => fetch(url));

    return Promise.race(promises)
        .then(response => {
            console.log("First response received");
            return response.json();
        });
}

// Nested promises and complex chains
function complexPromiseChain(userId) {
    return fetchUserData(userId)
        .then(user => {
            return Promise.all([
                Promise.resolve(user),
                fetchUserPosts(user.id),
                fetchUserComments(user.id)
            ]);
        })
        .then(([user, posts, comments]) => {
            return processUserProfile({ user, posts, comments });
        })
        .then(profile => {
            return saveProfile(profile);
        })
        .catch(error => {
            return handleProfileError(error);
        });
}

// Promise-based utility functions
function retry(fn, maxAttempts = 3) {
    return new Promise((resolve, reject) => {
        let attempts = 0;

        function attempt() {
            attempts++;
            fn()
                .then(resolve)
                .catch(error => {
                    if (attempts >= maxAttempts) {
                        reject(error);
                    } else {
                        setTimeout(attempt, 1000 * attempts);
                    }
                });
        }

        attempt();
    });
}

function delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// Using all promise patterns
const simpleResult = createSimplePromise();
const userData = processUserData(123);
const allUsers = fetchAllUsers([1, 2, 3, 4]);
const appData = loadApplicationData();
const timeoutResult = fetchWithTimeout("https://api.example.com/data", 3000);
const complexResult = complexPromiseChain(456);

// Helper functions for complex chain
function fetchUserPosts(userId) {
    return Promise.resolve([{ id: 1, title: "Post 1" }, { id: 2, title: "Post 2" }]);
}

function fetchUserComments(userId) {
    return Promise.resolve([{ id: 1, text: "Comment 1" }]);
}

function processUserProfile(data) {
    return Promise.resolve({ ...data, processed: true });
}

function saveProfile(profile) {
    return Promise.resolve({ ...profile, saved: true });
}

function handleProfileError(error) {
    return Promise.resolve({ error: error.message, fallback: true });
}
""",
    )

    run_updater(javascript_async_project, mock_ingestor)

    project_name = javascript_async_project.name

    expected_promise_functions = [
        f"{project_name}.promise_patterns.createSimplePromise",
        f"{project_name}.promise_patterns.fetchUserData",
        f"{project_name}.promise_patterns.processUserData",
        f"{project_name}.promise_patterns.chainedApiCall",
        f"{project_name}.promise_patterns.fetchAllUsers",
        f"{project_name}.promise_patterns.loadApplicationData",
        f"{project_name}.promise_patterns.fetchWithFallbacks",
        f"{project_name}.promise_patterns.fetchWithTimeout",
        f"{project_name}.promise_patterns.complexPromiseChain",
        f"{project_name}.promise_patterns.retry",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_promise_functions = [
        func for func in expected_promise_functions if func in created_functions
    ]
    assert len(found_promise_functions) >= 7, (
        f"Expected at least 7 Promise functions, found {len(found_promise_functions)}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    promise_calls = [
        call for call in call_relationships if "promise_patterns" in call.args[0][2]
    ]

    assert len(promise_calls) >= 8, (
        f"Expected at least 8 function calls in Promise code, found {len(promise_calls)}"
    )


def test_async_await_patterns(
    javascript_async_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test async/await syntax and patterns."""
    test_file = javascript_async_project / "async_await_patterns.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic async/await functions
async function fetchUser(userId) {
    try {
        const response = await fetch(`/api/users/${userId}`);
        const user = await response.json();
        return user;
    } catch (error) {
        console.error("Failed to fetch user:", error);
        throw error;
    }
}

async function saveUser(userData) {
    try {
        const response = await fetch("/api/users", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(userData)
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const savedUser = await response.json();
        return savedUser;
    } catch (error) {
        console.error("Failed to save user:", error);
        throw error;
    }
}

// Async function with multiple awaits
async function processUserWorkflow(userId) {
    console.log("Starting user workflow for:", userId);

    try {
        // Sequential operations
        const user = await fetchUser(userId);
        console.log("User fetched:", user);

        const profile = await fetchUserProfile(user.id);
        console.log("Profile fetched:", profile);

        const settings = await fetchUserSettings(user.id);
        console.log("Settings fetched:", settings);

        // Process all data
        const processedData = await processUserData({
            user,
            profile,
            settings
        });

        // Save processed data
        const result = await saveProcessedData(processedData);
        console.log("Workflow completed:", result);

        return result;
    } catch (error) {
        console.error("Workflow failed:", error);
        throw error;
    }
}

// Parallel async operations with Promise.all
async function fetchUserDataParallel(userId) {
    try {
        const [user, profile, settings] = await Promise.all([
            fetchUser(userId),
            fetchUserProfile(userId),
            fetchUserSettings(userId)
        ]);

        return { user, profile, settings };
    } catch (error) {
        console.error("Parallel fetch failed:", error);
        throw error;
    }
}

// Async function with conditional logic
async function smartUserFetch(userId, includeDetails = false) {
    const user = await fetchUser(userId);

    if (!includeDetails) {
        return user;
    }

    try {
        const [profile, settings, posts] = await Promise.all([
            fetchUserProfile(userId),
            fetchUserSettings(userId),
            fetchUserPosts(userId)
        ]);

        return {
            ...user,
            profile,
            settings,
            posts,
            complete: true
        };
    } catch (error) {
        console.log("Details fetch failed, returning basic user");
        return { ...user, complete: false };
    }
}

// Async generators
async function* fetchUsersGenerator(userIds) {
    for (const userId of userIds) {
        try {
            const user = await fetchUser(userId);
            yield user;
        } catch (error) {
            console.error(`Failed to fetch user ${userId}:`, error);
            yield { id: userId, error: error.message };
        }
    }
}

async function* dataStream(endpoint) {
    let page = 1;
    let hasMore = true;

    while (hasMore) {
        try {
            const response = await fetch(`${endpoint}?page=${page}`);
            const data = await response.json();

            for (const item of data.items) {
                yield item;
            }

            hasMore = data.hasMore;
            page++;
        } catch (error) {
            console.error("Stream error:", error);
            break;
        }
    }
}

// Async arrow functions
const quickFetchUser = async (userId) => {
    const response = await fetch(`/api/users/${userId}`);
    return await response.json();
};

const processDataAsync = async (data) => {
    const processed = await transformData(data);
    await saveData(processed);
    return processed;
};

// Async methods in objects
const api = {
    async getUser(id) {
        const response = await fetch(`/api/users/${id}`);
        return await response.json();
    },

    async createUser(userData) {
        const response = await fetch("/api/users", {
            method: "POST",
            body: JSON.stringify(userData)
        });
        return await response.json();
    },

    async updateUser(id, updates) {
        const response = await fetch(`/api/users/${id}`, {
            method: "PUT",
            body: JSON.stringify(updates)
        });
        return await response.json();
    },

    async deleteUser(id) {
        await fetch(`/api/users/${id}`, { method: "DELETE" });
        return { deleted: true, id };
    }
};

// Error handling patterns
async function robustApiCall(url, options = {}) {
    const maxRetries = options.maxRetries || 3;
    let lastError;

    for (let attempt = 1; attempt <= maxRetries; attempt++) {
        try {
            const response = await fetch(url, options);

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            return await response.json();
        } catch (error) {
            lastError = error;
            console.log(`Attempt ${attempt} failed:`, error.message);

            if (attempt < maxRetries) {
                await delay(1000 * attempt); // Exponential backoff
            }
        }
    }

    throw lastError;
}

// Async IIFE
(async () => {
    try {
        const user = await fetchUser(1);
        console.log("IIFE user:", user);
    } catch (error) {
        console.error("IIFE error:", error);
    }
})();

// Class with async methods
class UserService {
    constructor(baseUrl) {
        this.baseUrl = baseUrl;
    }

    async getUser(id) {
        const response = await fetch(`${this.baseUrl}/users/${id}`);
        return await response.json();
    }

    async getAllUsers() {
        const response = await fetch(`${this.baseUrl}/users`);
        const users = await response.json();
        return users;
    }

    async batchProcess(userIds) {
        const results = [];

        for (const id of userIds) {
            try {
                const user = await this.getUser(id);
                const processed = await this.processUser(user);
                results.push(processed);
            } catch (error) {
                results.push({ id, error: error.message });
            }
        }

        return results;
    }

    async processUser(user) {
        // Simulate processing
        await delay(100);
        return { ...user, processed: true, timestamp: Date.now() };
    }
}

// Using async patterns
const user1 = await fetchUser(123);
const workflow = await processUserWorkflow(456);
const parallel = await fetchUserDataParallel(789);
const smart = await smartUserFetch(321, true);

const apiUser = await api.getUser(111);
const newUser = await api.createUser({ name: "Test User" });

const service = new UserService("https://api.example.com");
const serviceUser = await service.getUser(222);
const batch = await service.batchProcess([1, 2, 3]);

// Helper functions
async function fetchUserProfile(userId) {
    await delay(200);
    return { userId, bio: "User bio", avatar: "avatar.jpg" };
}

async function fetchUserSettings(userId) {
    await delay(150);
    return { userId, theme: "dark", notifications: true };
}

async function fetchUserPosts(userId) {
    await delay(300);
    return [{ id: 1, title: "Post 1" }, { id: 2, title: "Post 2" }];
}

async function processUserData(data) {
    await delay(100);
    return { ...data, processed: true };
}

async function saveProcessedData(data) {
    await delay(250);
    return { ...data, saved: true, id: Math.random() };
}

async function transformData(data) {
    await delay(50);
    return { ...data, transformed: true };
}

async function saveData(data) {
    await delay(100);
    return { ...data, saved: true };
}

function delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}
""",
    )

    run_updater(javascript_async_project, mock_ingestor)

    project_name = javascript_async_project.name

    expected_async_functions = [
        f"{project_name}.async_await_patterns.fetchUser",
        f"{project_name}.async_await_patterns.saveUser",
        f"{project_name}.async_await_patterns.processUserWorkflow",
        f"{project_name}.async_await_patterns.fetchUserDataParallel",
        f"{project_name}.async_await_patterns.smartUserFetch",
        f"{project_name}.async_await_patterns.fetchUsersGenerator",
        f"{project_name}.async_await_patterns.quickFetchUser",
        f"{project_name}.async_await_patterns.robustApiCall",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_async_functions = [
        func for func in expected_async_functions if func in created_functions
    ]
    assert len(found_async_functions) >= 6, (
        f"Expected at least 6 async functions, found {len(found_async_functions)}"
    )

    expected_classes = [
        f"{project_name}.async_await_patterns.UserService",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 class with async methods, found {len(found_classes)}"
    )


def test_callback_patterns(
    javascript_async_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test callback patterns and callback-based async code."""
    test_file = javascript_async_project / "callback_patterns.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic callback patterns
function fetchUserWithCallback(userId, callback) {
    setTimeout(() => {
        if (userId > 0) {
            const user = { id: userId, name: `User ${userId}` };
            callback(null, user);
        } else {
            callback(new Error("Invalid user ID"));
        }
    }, 500);
}

function saveUserWithCallback(userData, callback) {
    setTimeout(() => {
        if (userData && userData.name) {
            const savedUser = { ...userData, id: Math.random(), saved: true };
            callback(null, savedUser);
        } else {
            callback(new Error("Invalid user data"));
        }
    }, 300);
}

// Node.js style error-first callbacks
function readFileCallback(filename, callback) {
    setTimeout(() => {
        if (filename.endsWith('.txt')) {
            callback(null, `Content of ${filename}`);
        } else {
            callback(new Error("Only .txt files supported"));
        }
    }, 200);
}

function writeFileCallback(filename, content, callback) {
    setTimeout(() => {
        if (filename && content) {
            callback(null, { filename, bytesWritten: content.length });
        } else {
            callback(new Error("Filename and content required"));
        }
    }, 150);
}

// Callback composition and chaining
function processUserWithCallbacks(userId, callback) {
    fetchUserWithCallback(userId, (err, user) => {
        if (err) {
            return callback(err);
        }

        // Process user data
        const processedUser = { ...user, processed: true, timestamp: Date.now() };

        saveUserWithCallback(processedUser, (saveErr, savedUser) => {
            if (saveErr) {
                return callback(saveErr);
            }

            callback(null, savedUser);
        });
    });
}

// Parallel callbacks with manual coordination
function fetchMultipleUsersWithCallbacks(userIds, callback) {
    const results = [];
    let completed = 0;
    let hasError = false;

    if (userIds.length === 0) {
        return callback(null, []);
    }

    userIds.forEach((userId, index) => {
        fetchUserWithCallback(userId, (err, user) => {
            if (hasError) return;

            if (err) {
                hasError = true;
                return callback(err);
            }

            results[index] = user;
            completed++;

            if (completed === userIds.length) {
                callback(null, results);
            }
        });
    });
}

// Callback with timeout
function fetchWithTimeoutCallback(userId, timeout, callback) {
    let completed = false;

    const timeoutId = setTimeout(() => {
        if (!completed) {
            completed = true;
            callback(new Error("Operation timed out"));
        }
    }, timeout);

    fetchUserWithCallback(userId, (err, user) => {
        if (!completed) {
            completed = true;
            clearTimeout(timeoutId);
            callback(err, user);
        }
    });
}

// Retry with callbacks
function retryWithCallback(fn, args, maxRetries, callback) {
    let attempts = 0;

    function attempt() {
        attempts++;
        fn(...args, (err, result) => {
            if (!err) {
                return callback(null, result);
            }

            if (attempts >= maxRetries) {
                return callback(err);
            }

            setTimeout(attempt, 1000 * attempts);
        });
    }

    attempt();
}

// Event-style callbacks
class EventEmitter {
    constructor() {
        this.events = {};
    }

    on(event, callback) {
        if (!this.events[event]) {
            this.events[event] = [];
        }
        this.events[event].push(callback);
    }

    emit(event, ...args) {
        if (this.events[event]) {
            this.events[event].forEach(callback => {
                try {
                    callback(...args);
                } catch (error) {
                    console.error("Callback error:", error);
                }
            });
        }
    }

    off(event, callback) {
        if (this.events[event]) {
            this.events[event] = this.events[event].filter(cb => cb !== callback);
        }
    }
}

// Callback-based data processing
function processDataWithCallback(data, transformCallback, resultCallback) {
    setTimeout(() => {
        try {
            const transformed = transformCallback(data);
            resultCallback(null, transformed);
        } catch (error) {
            resultCallback(error);
        }
    }, 100);
}

function aggregateDataWithCallback(datasets, callback) {
    const results = [];
    let processed = 0;

    datasets.forEach((dataset, index) => {
        processDataWithCallback(
            dataset,
            (data) => data.map(item => ({ ...item, processed: true })),
            (err, result) => {
                if (err) {
                    return callback(err);
                }

                results[index] = result;
                processed++;

                if (processed === datasets.length) {
                    const aggregated = results.flat();
                    callback(null, aggregated);
                }
            }
        );
    });
}

// Callback to Promise conversion
function promisifyCallback(fn) {
    return function(...args) {
        return new Promise((resolve, reject) => {
            fn(...args, (err, result) => {
                if (err) {
                    reject(err);
                } else {
                    resolve(result);
                }
            });
        });
    };
}

// Higher-order functions with callbacks
function mapWithCallback(array, mapperCallback, doneCallback) {
    const results = [];
    let completed = 0;

    if (array.length === 0) {
        return doneCallback(null, []);
    }

    array.forEach((item, index) => {
        mapperCallback(item, (err, mapped) => {
            if (err) {
                return doneCallback(err);
            }

            results[index] = mapped;
            completed++;

            if (completed === array.length) {
                doneCallback(null, results);
            }
        });
    });
}

function filterWithCallback(array, predicateCallback, doneCallback) {
    const results = [];
    let completed = 0;

    if (array.length === 0) {
        return doneCallback(null, []);
    }

    array.forEach((item) => {
        predicateCallback(item, (err, shouldInclude) => {
            if (err) {
                return doneCallback(err);
            }

            if (shouldInclude) {
                results.push(item);
            }

            completed++;

            if (completed === array.length) {
                doneCallback(null, results);
            }
        });
    });
}

// Using callback patterns
fetchUserWithCallback(123, (err, user) => {
    if (err) {
        console.error("Error:", err);
    } else {
        console.log("User:", user);
    }
});

processUserWithCallbacks(456, (err, result) => {
    if (err) {
        console.error("Process error:", err);
    } else {
        console.log("Processed:", result);
    }
});

fetchMultipleUsersWithCallbacks([1, 2, 3], (err, users) => {
    if (err) {
        console.error("Batch error:", err);
    } else {
        console.log("All users:", users);
    }
});

// Event emitter usage
const emitter = new EventEmitter();

emitter.on("userCreated", (user) => {
    console.log("User created:", user);
});

emitter.on("userDeleted", (userId) => {
    console.log("User deleted:", userId);
});

emitter.emit("userCreated", { id: 1, name: "Alice" });
emitter.emit("userDeleted", 1);

// Promisified versions
const fetchUserPromise = promisifyCallback(fetchUserWithCallback);
const saveUserPromise = promisifyCallback(saveUserWithCallback);

fetchUserPromise(789)
    .then(user => console.log("Promisified user:", user))
    .catch(err => console.error("Promisified error:", err));
""",
    )

    run_updater(javascript_async_project, mock_ingestor)

    project_name = javascript_async_project.name

    expected_callback_functions = [
        f"{project_name}.callback_patterns.fetchUserWithCallback",
        f"{project_name}.callback_patterns.saveUserWithCallback",
        f"{project_name}.callback_patterns.processUserWithCallbacks",
        f"{project_name}.callback_patterns.fetchMultipleUsersWithCallbacks",
        f"{project_name}.callback_patterns.fetchWithTimeoutCallback",
        f"{project_name}.callback_patterns.retryWithCallback",
        f"{project_name}.callback_patterns.processDataWithCallback",
        f"{project_name}.callback_patterns.promisifyCallback",
        f"{project_name}.callback_patterns.mapWithCallback",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_callback_functions = [
        func for func in expected_callback_functions if func in created_functions
    ]
    assert len(found_callback_functions) >= 6, (
        f"Expected at least 6 callback functions, found {len(found_callback_functions)}"
    )

    expected_classes = [
        f"{project_name}.callback_patterns.EventEmitter",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 callback-based class, found {len(found_classes)}"
    )


def test_generator_patterns(
    javascript_async_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test generator functions and async generators."""
    test_file = javascript_async_project / "generator_patterns.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic generator functions
function* simpleGenerator() {
    yield 1;
    yield 2;
    yield 3;
    return "done";
}

function* rangeGenerator(start, end) {
    for (let i = start; i <= end; i++) {
        yield i;
    }
}

function* fibonacciGenerator(limit = 10) {
    let a = 0, b = 1;
    let count = 0;

    while (count < limit) {
        yield a;
        [a, b] = [b, a + b];
        count++;
    }
}

// Generator with yield*
function* combinedGenerator() {
    yield* rangeGenerator(1, 3);
    yield "separator";
    yield* rangeGenerator(4, 6);
}

function* nestedGenerator() {
    yield 1;
    yield* simpleGenerator();
    yield 2;
}

// Infinite generators
function* infiniteCounter(start = 0) {
    let current = start;
    while (true) {
        yield current++;
    }
}

function* randomNumberGenerator() {
    while (true) {
        yield Math.random();
    }
}

// Generator that takes input
function* inputGenerator() {
    const a = yield "Give me a number";
    const b = yield "Give me another number";
    yield `Sum: ${a + b}`;
    return "calculation complete";
}

function* echoGenerator() {
    let input;
    while (true) {
        input = yield `Echo: ${input}`;
    }
}

// Async generators
async function* asyncNumberGenerator(count) {
    for (let i = 0; i < count; i++) {
        await delay(100);
        yield i;
    }
}

async function* asyncDataStream(urls) {
    for (const url of urls) {
        try {
            const response = await fetch(url);
            const data = await response.json();
            yield data;
        } catch (error) {
            yield { error: error.message, url };
        }
    }
}

async function* fetchUsersAsyncGenerator(userIds) {
    for (const userId of userIds) {
        try {
            await delay(50);
            const user = { id: userId, name: `User ${userId}`, timestamp: Date.now() };
            yield user;
        } catch (error) {
            yield { id: userId, error: error.message };
        }
    }
}

// Generator-based data processing
function* mapGenerator(iterable, mapFn) {
    for (const item of iterable) {
        yield mapFn(item);
    }
}

function* filterGenerator(iterable, predicate) {
    for (const item of iterable) {
        if (predicate(item)) {
            yield item;
        }
    }
}

function* takeGenerator(iterable, count) {
    let taken = 0;
    for (const item of iterable) {
        if (taken >= count) break;
        yield item;
        taken++;
    }
}

function* zipGenerator(iter1, iter2) {
    const it1 = iter1[Symbol.iterator]();
    const it2 = iter2[Symbol.iterator]();

    while (true) {
        const result1 = it1.next();
        const result2 = it2.next();

        if (result1.done || result2.done) break;

        yield [result1.value, result2.value];
    }
}

// Generator-based state machine
function* stateMachine() {
    let state = "idle";
    let input;

    while (true) {
        switch (state) {
            case "idle":
                input = yield "Waiting for start command";
                if (input === "start") {
                    state = "running";
                }
                break;

            case "running":
                input = yield "Running... send 'pause' or 'stop'";
                if (input === "pause") {
                    state = "paused";
                } else if (input === "stop") {
                    state = "stopped";
                }
                break;

            case "paused":
                input = yield "Paused... send 'resume' or 'stop'";
                if (input === "resume") {
                    state = "running";
                } else if (input === "stop") {
                    state = "stopped";
                }
                break;

            case "stopped":
                return "State machine stopped";
        }
    }
}

// Tree traversal generator
function* depthFirstTraversal(node) {
    yield node.value;

    if (node.children) {
        for (const child of node.children) {
            yield* depthFirstTraversal(child);
        }
    }
}

function* breadthFirstTraversal(root) {
    const queue = [root];

    while (queue.length > 0) {
        const node = queue.shift();
        yield node.value;

        if (node.children) {
            queue.push(...node.children);
        }
    }
}

// Generator utilities
function* cycle(iterable) {
    const items = [...iterable];
    while (true) {
        yield* items;
    }
}

function* repeat(value, times) {
    for (let i = 0; i < times; i++) {
        yield value;
    }
}

function* chain(...iterables) {
    for (const iterable of iterables) {
        yield* iterable;
    }
}

// Using generators
const simple = simpleGenerator();
console.log(simple.next().value); // 1
console.log(simple.next().value); // 2

const range = rangeGenerator(5, 8);
for (const num of range) {
    console.log(num); // 5, 6, 7, 8
}

const fib = fibonacciGenerator(5);
const fibNumbers = [...fib]; // [0, 1, 1, 2, 3]

// Using input generator
const inputGen = inputGenerator();
console.log(inputGen.next().value); // "Give me a number"
console.log(inputGen.next(10).value); // "Give me another number"
console.log(inputGen.next(20).value); // "Sum: 30"

// Using async generators
async function consumeAsyncGenerator() {
    const asyncGen = asyncNumberGenerator(3);

    for await (const num of asyncGen) {
        console.log("Async:", num);
    }
}

// Generator composition
const mapped = mapGenerator([1, 2, 3, 4, 5], x => x * 2);
const filtered = filterGenerator(mapped, x => x > 5);
const taken = takeGenerator(filtered, 2);

const result = [...taken]; // [6, 8]

// Tree example
const tree = {
    value: 1,
    children: [
        {
            value: 2,
            children: [
                { value: 4 },
                { value: 5 }
            ]
        },
        {
            value: 3,
            children: [
                { value: 6 }
            ]
        }
    ]
};

const dfsValues = [...depthFirstTraversal(tree)]; // [1, 2, 4, 5, 3, 6]
const bfsValues = [...breadthFirstTraversal(tree)]; // [1, 2, 3, 4, 5, 6]

// Helper function
function delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

consumeAsyncGenerator();
""",
    )

    run_updater(javascript_async_project, mock_ingestor)

    project_name = javascript_async_project.name

    expected_generator_functions = [
        f"{project_name}.generator_patterns.simpleGenerator",
        f"{project_name}.generator_patterns.rangeGenerator",
        f"{project_name}.generator_patterns.fibonacciGenerator",
        f"{project_name}.generator_patterns.combinedGenerator",
        f"{project_name}.generator_patterns.infiniteCounter",
        f"{project_name}.generator_patterns.inputGenerator",
        f"{project_name}.generator_patterns.asyncNumberGenerator",
        f"{project_name}.generator_patterns.asyncDataStream",
        f"{project_name}.generator_patterns.mapGenerator",
        f"{project_name}.generator_patterns.filterGenerator",
        f"{project_name}.generator_patterns.stateMachine",
        f"{project_name}.generator_patterns.depthFirstTraversal",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_generator_functions = [
        func for func in expected_generator_functions if func in created_functions
    ]
    assert len(found_generator_functions) >= 8, (
        f"Expected at least 8 generator functions, found {len(found_generator_functions)}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    generator_calls = [
        call for call in call_relationships if "generator_patterns" in call.args[0][2]
    ]

    assert len(generator_calls) >= 5, (
        f"Expected at least 5 function calls in generator code, found {len(generator_calls)}"
    )


def test_async_comprehensive(
    javascript_async_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all async patterns create proper relationships."""
    test_file = javascript_async_project / "comprehensive_async.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every JavaScript async pattern in one file

// Promise
function createPromise() {
    return new Promise((resolve, reject) => {
        setTimeout(() => resolve("promise result"), 100);
    });
}

// Async/await
async function asyncFunction() {
    const result = await createPromise();
    return result;
}

// Callback
function callbackFunction(callback) {
    setTimeout(() => callback(null, "callback result"), 100);
}

// Generator
function* generatorFunction() {
    yield 1;
    yield 2;
    yield 3;
}

// Async generator
async function* asyncGeneratorFunction() {
    for (let i = 0; i < 3; i++) {
        await delay(50);
        yield i;
    }
}

// Promise chaining
function chainedPromise() {
    return createPromise()
        .then(result => result.toUpperCase())
        .then(upper => upper + "!")
        .catch(error => "error: " + error);
}

// Parallel async operations
async function parallelAsync() {
    const [result1, result2, result3] = await Promise.all([
        createPromise(),
        asyncFunction(),
        Promise.resolve("direct result")
    ]);

    return { result1, result2, result3 };
}

// Mixed patterns
async function mixedPatterns(callback) {
    try {
        // Use Promise
        const promiseResult = await createPromise();

        // Use generator
        const gen = generatorFunction();
        const genValues = [...gen];

        // Use callback
        const callbackResult = await new Promise((resolve, reject) => {
            callbackFunction((err, result) => {
                if (err) reject(err);
                else resolve(result);
            });
        });

        // Use async generator
        const asyncGenValues = [];
        for await (const value of asyncGeneratorFunction()) {
            asyncGenValues.push(value);
        }

        const finalResult = {
            promise: promiseResult,
            generator: genValues,
            callback: callbackResult,
            asyncGenerator: asyncGenValues
        };

        // Call the callback with result
        callback(null, finalResult);

        return finalResult;
    } catch (error) {
        callback(error);
        throw error;
    }
}

// Class with async methods
class AsyncService {
    async process() {
        const result = await asyncFunction();
        return this.transform(result);
    }

    transform(data) {
        return data.toUpperCase();
    }

    * dataGenerator() {
        yield* generatorFunction();
    }

    async* asyncDataGenerator() {
        yield* asyncGeneratorFunction();
    }
}

// Using all patterns
const promise = createPromise();
const asyncResult = asyncFunction();
const chained = chainedPromise();
const parallel = parallelAsync();

callbackFunction((err, result) => {
    console.log("Callback result:", result);
});

const gen = generatorFunction();
for (const value of gen) {
    console.log("Generator value:", value);
}

mixedPatterns((err, result) => {
    if (err) {
        console.error("Mixed patterns error:", err);
    } else {
        console.log("Mixed patterns result:", result);
    }
});

const service = new AsyncService();
const serviceResult = service.process();

// Helper function
function delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}
""",
    )

    run_updater(javascript_async_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call for call in call_relationships if "comprehensive_async" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 5, (
        f"Expected at least 5 comprehensive async calls, found {len(comprehensive_calls)}"
    )

    for relationship in comprehensive_calls:
        assert len(relationship.args) == 3, "Call relationship should have 3 args"
        assert relationship.args[1] == "CALLS", "Second arg should be 'CALLS'"

        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        assert "comprehensive_async" in source_module, (
            f"Source module should contain test file name: {source_module}"
        )

        assert isinstance(target_module, str) and target_module, (
            f"Target should be non-empty string: {target_module}"
        )

    assert defines_relationships, "Should still have DEFINES relationships"
