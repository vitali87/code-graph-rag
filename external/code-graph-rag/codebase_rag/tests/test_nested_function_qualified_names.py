from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, run_updater


@pytest.fixture
def nested_functions_project(temp_repo: Path) -> Path:
    """Create a temporary project with nested functions test cases."""
    project_path = temp_repo / "nested_functions_test"
    project_path.mkdir()

    package_json = project_path / "package.json"
    package_json.write_text(
        encoding="utf-8",
        data="""
{
  "name": "nested-functions-test",
  "version": "1.0.0",
  "description": "Test nested function qualified names"
}
""",
    )

    return project_path


def test_object_methods_inside_functions(
    nested_functions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that object methods defined inside functions have correct qualified names."""
    test_file = nested_functions_project / "object_methods_nested.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Object methods defined inside functions
function createApiClient(baseUrl) {
    const client = {
        get: function(path) {
            return fetch(`${baseUrl}${path}`);
        },

        post: function(path, data) {
            return fetch(`${baseUrl}${path}`, {
                method: 'POST',
                body: JSON.stringify(data)
            });
        },

        // Arrow function method
        delete: (path) => {
            return fetch(`${baseUrl}${path}`, { method: 'DELETE' });
        }
    };

    return client;
}

class ServiceFactory {
    createService(config) {
        return {
            process: function(data) {
                return data.map(item => item.value);
            },

            validate: (input) => {
                return input !== null && input !== undefined;
            }
        };
    }
}
""",
    )

    run_updater(nested_functions_project, mock_ingestor)

    project_name = nested_functions_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_nested_functions = [
        f"{project_name}.object_methods_nested.createApiClient.get",
        f"{project_name}.object_methods_nested.createApiClient.post",
        f"{project_name}.object_methods_nested.createApiClient.delete",
        f"{project_name}.object_methods_nested.ServiceFactory.createService.process",
        f"{project_name}.object_methods_nested.ServiceFactory.createService.validate",
    ]

    missing_functions = []
    for expected in expected_nested_functions:
        if expected not in created_functions:
            missing_functions.append(expected)

    if missing_functions:
        print(f"❌ Missing correctly nested object methods: {missing_functions}")
        print(f"✅ Actually created functions: {sorted(created_functions)}")

    assert not missing_functions, f"Missing nested object methods: {missing_functions}"


def test_arrow_functions_in_constructors(
    nested_functions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test arrow functions assigned in constructors - should have correct nested qualified names."""
    test_file = nested_functions_project / "arrow_functions_in_constructors.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Arrow functions assigned inside constructors
class UserService {
    constructor(apiUrl) {
        this.apiUrl = apiUrl;

        // Arrow function assigned to instance property
        this.fetchUser = async (id) => {
            return await fetch(`${this.apiUrl}/users/${id}`);
        };

        this.processUser = (userData) => {
            return {
                ...userData,
                processed: true,
                timestamp: Date.now()
            };
        };

        // Nested arrow functions
        this.createValidator = (rules) => {
            return (data) => {
                return rules.every(rule => rule(data));
            };
        };
    }

    async initialize() {
        // Arrow function assigned in method
        this.onError = (error) => {
            console.error('Service error:', error);
        };

        this.retry = (fn, attempts = 3) => {
            return async (...args) => {
                for (let i = 0; i < attempts; i++) {
                    try {
                        return await fn(...args);
                    } catch (error) {
                        if (i === attempts - 1) throw error;
                    }
                }
            };
        };
    }
}

// Traditional constructor function
function DatabaseService(connectionString) {
    this.connectionString = connectionString;

    // Arrow functions assigned to prototype-like properties
    this.connect = async () => {
        return await connect(this.connectionString);
    };

    this.query = (sql, params) => {
        return this.connection.execute(sql, params);
    };

    // Method that assigns arrow functions
    this.createBatch = () => {
        const batch = [];

        // Arrow function inside method
        this.addToBatch = (item) => {
            batch.push(item);
        };

        this.executeBatch = async () => {
            return await this.query('BATCH', batch);
        };

        return { batch, addToBatch: this.addToBatch, executeBatch: this.executeBatch };
    };
}
""",
    )

    run_updater(nested_functions_project, mock_ingestor)

    project_name = nested_functions_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_nested_functions = [
        f"{project_name}.arrow_functions_in_constructors.DatabaseService.connect",
        f"{project_name}.arrow_functions_in_constructors.DatabaseService.query",
        f"{project_name}.arrow_functions_in_constructors.DatabaseService.createBatch",
        f"{project_name}.arrow_functions_in_constructors.DatabaseService.addToBatch",
        f"{project_name}.arrow_functions_in_constructors.DatabaseService.executeBatch",
        f"{project_name}.arrow_functions_in_constructors.UserService.constructor.fetchUser",
        f"{project_name}.arrow_functions_in_constructors.UserService.constructor.processUser",
        f"{project_name}.arrow_functions_in_constructors.UserService.constructor.createValidator",
        f"{project_name}.arrow_functions_in_constructors.UserService.initialize.onError",
        f"{project_name}.arrow_functions_in_constructors.UserService.initialize.retry",
    ]

    missing_functions = []
    for expected in expected_nested_functions:
        if expected not in created_functions:
            missing_functions.append(expected)

    incorrect_module_level_names = [
        f"{project_name}.arrow_functions_in_constructors.connect",
        f"{project_name}.arrow_functions_in_constructors.query",
        f"{project_name}.arrow_functions_in_constructors.addToBatch",
        f"{project_name}.arrow_functions_in_constructors.executeBatch",
        f"{project_name}.arrow_functions_in_constructors.fetchUser",
        f"{project_name}.arrow_functions_in_constructors.processUser",
        f"{project_name}.arrow_functions_in_constructors.createValidator",
        f"{project_name}.arrow_functions_in_constructors.onError",
        f"{project_name}.arrow_functions_in_constructors.retry",
    ]

    incorrectly_created = []
    for incorrect in incorrect_module_level_names:
        if incorrect in created_functions:
            incorrectly_created.append(incorrect)

    if missing_functions:
        print(f"❌ Missing correctly nested arrow functions: {missing_functions}")
        print(f"✅ Actually created functions: {sorted(created_functions)}")
        assert False, (
            f"Missing nested arrow functions with correct qualified names: {missing_functions}"
        )

    if incorrectly_created:
        print(f"❌ Incorrectly created module-level functions: {incorrectly_created}")
        assert False, (
            f"Arrow functions incorrectly created at module level: {incorrectly_created}"
        )


def test_export_functions_in_modules(
    nested_functions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test ES6 export functions defined inside other functions - should be nested."""
    test_file = nested_functions_project / "export_functions_nested.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// ES6 exports defined inside functions
function createModule() {
    // These should NOT be treated as module-level exports
    export const helper = function(data) {
        return data.map(item => item.value);
    };

    export const validator = (input) => {
        return input && typeof input === 'string';
    };

    export function formatter(text) {
        return text.trim().toLowerCase();
    }

    // IIFE with exports
    export const iife_function = (function() {
        return function(x) { return x * 2; };
    })();

    export async function iife_async() {
        return await processData();
    }
}

class ServiceFactory {
    createService() {
        return {
            process: function processData() {
                return 'processed';
            },

            validate: (data) => {
                return data.length > 0;
            }
        };
    }
}
""",
    )

    run_updater(nested_functions_project, mock_ingestor)

    project_name = nested_functions_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    incorrect_module_level_names = [
        f"{project_name}.export_functions_nested.helper",
        f"{project_name}.export_functions_nested.validator",
        f"{project_name}.export_functions_nested.formatter",
        f"{project_name}.export_functions_nested.iife_function",
        f"{project_name}.export_functions_nested.iife_async",
    ]

    incorrectly_created = []
    for incorrect in incorrect_module_level_names:
        if incorrect in created_functions:
            incorrectly_created.append(incorrect)

    if incorrectly_created:
        print(
            f"❌ Incorrectly created module-level export functions (should be nested): {incorrectly_created}"
        )
        print(f"✅ Actually created functions: {sorted(created_functions)}")
        assert False, (
            f"Export functions should be nested, not at module level: {incorrectly_created}"
        )


def test_commonjs_exports_in_functions(
    nested_functions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test CommonJS exports defined inside functions - should have correct nested qualified names."""
    test_file = nested_functions_project / "commonjs_exports_nested.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// CommonJS exports defined inside functions
function createUtilities() {
    // These should NOT be treated as module-level exports
    module.exports.helper = function(data) {
        return data.filter(item => item.active);
    };

    module.exports.formatter = (text) => {
        return text.toUpperCase();
    };

    exports.validator = function(input) {
        return input && typeof input === 'string';
    };

    // Object assignment
    module.exports = {
        processor: (items) => {
            return items.map(item => ({ ...item, processed: true }));
        },

        async loader(path) {
            return await import(path);
        }
    };
}

class ModuleFactory {
    generateModule() {
        // CommonJS exports inside method
        module.exports.generated = () => {
            return 'generated function';
        };

        exports.methodGenerated = function(param) {
            return param * 2;
        };
    }
}

// IIFE with CommonJS exports
(function() {
    module.exports.iife_export = function() {
        return 'from IIFE module.exports';
    };

    exports.iife_function = (data) => {
        return data.reverse();
    };
})();
""",
    )

    run_updater(nested_functions_project, mock_ingestor)

    project_name = nested_functions_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_nested_functions = [
        f"{project_name}.commonjs_exports_nested.createUtilities.helper",
        f"{project_name}.commonjs_exports_nested.createUtilities.formatter",
        f"{project_name}.commonjs_exports_nested.createUtilities.validator",
        f"{project_name}.commonjs_exports_nested.createUtilities.processor",
        f"{project_name}.commonjs_exports_nested.createUtilities.loader",
        f"{project_name}.commonjs_exports_nested.ModuleFactory.generateModule.generated",
        f"{project_name}.commonjs_exports_nested.ModuleFactory.generateModule.methodGenerated",
        f"{project_name}.commonjs_exports_nested.iife_export",
        f"{project_name}.commonjs_exports_nested.iife_function",
    ]

    missing_functions = []
    for expected in expected_nested_functions:
        if expected not in created_functions:
            missing_functions.append(expected)

    incorrect_module_level_names = [
        f"{project_name}.commonjs_exports_nested.helper",
        f"{project_name}.commonjs_exports_nested.formatter",
        f"{project_name}.commonjs_exports_nested.validator",
        f"{project_name}.commonjs_exports_nested.processor",
        f"{project_name}.commonjs_exports_nested.loader",
        f"{project_name}.commonjs_exports_nested.generated",
        f"{project_name}.commonjs_exports_nested.methodGenerated",
    ]

    incorrectly_created = []
    for incorrect in incorrect_module_level_names:
        if incorrect in created_functions:
            incorrectly_created.append(incorrect)

    if missing_functions:
        print(
            f"❌ Missing correctly nested CommonJS export functions: {missing_functions}"
        )
        print(f"✅ Actually created functions: {sorted(created_functions)}")
        assert False, (
            f"Missing nested CommonJS export functions with correct qualified names: {missing_functions}"
        )

    if incorrectly_created:
        print(
            f"❌ Incorrectly created module-level CommonJS exports: {incorrectly_created}"
        )
        assert False, (
            f"CommonJS exports should be nested, not at module level: {incorrectly_created}"
        )
