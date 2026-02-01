from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    get_nodes,
    get_qualified_names,
    get_relationships,
    run_updater,
)


@pytest.fixture
def javascript_modules_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with module patterns."""
    project_path = temp_repo / "javascript_modules_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "lib").mkdir()
    (project_path / "utils").mkdir()
    (project_path / "models").mkdir()
    (project_path / "node_modules").mkdir()
    (project_path / "node_modules" / "lodash").mkdir()

    (project_path / "utils" / "constants.js").write_text(
        encoding="utf-8",
        data="""
module.exports.API_URL = 'https://api.example.com';
module.exports.TIMEOUT = 5000;
module.exports.VERSION = '1.0.0';
""",
    )

    (project_path / "models" / "User.js").write_text(
        encoding="utf-8",
        data="""
class User {
    constructor(name, email) {
        this.name = name;
        this.email = email;
    }
}

module.exports = User;
""",
    )

    (project_path / "lib" / "validators.js").write_text(
        encoding="utf-8",
        data=r"""
export function isEmail(email) {
    return /^[^@]+@[^@]+\.[^@]+$/.test(email);
}

export function isPhone(phone) {
    return /^\d{10}$/.test(phone);
}

export default function validate(type, value) {
    switch(type) {
        case 'email': return isEmail(value);
        case 'phone': return isPhone(value);
        default: return false;
    }
}
""",
    )

    return project_path


def test_commonjs_module_exports(
    javascript_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test CommonJS module.exports patterns."""
    test_file = javascript_modules_project / "commonjs_exports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Single export assignment
module.exports = function mainFunction() {
    return "main";
};

// This pattern overwrites the previous export
module.exports = {
    add: function(a, b) {
        return a + b;
    },
    subtract: function(a, b) {
        return a - b;
    },
    multiply: (a, b) => a * b,
    PI: 3.14159,
    config: {
        debug: true,
        version: "1.0.0"
    }
};

// Individual property exports (on a different file)
// These would be ignored since module.exports was reassigned above
module.exports.ignored = "This won't be exported";

// Another file demonstrating individual exports
const helperFile = "helper.js";
// In helper.js:
// module.exports.helper1 = function() { return "helper1"; };
// module.exports.helper2 = function() { return "helper2"; };
// exports.helper3 = function() { return "helper3"; };

// Conditional exports
if (process.env.NODE_ENV === 'production') {
    module.exports.productionOnly = function() {
        return "prod";
    };
}

// Function that returns module
function createModule() {
    return {
        method1: () => "method1",
        method2: () => "method2"
    };
}

// Dynamic module export
module.exports = createModule();

// Class export
class Calculator {
    add(a, b) { return a + b; }
    subtract(a, b) { return a - b; }
}

module.exports = Calculator;

// Mixed exports pattern (separate file example)
// exports.namedExport1 = function() {};
// exports.namedExport2 = function() {};
// module.exports.namedExport3 = function() {};
""",
    )

    exports_file = javascript_modules_project / "exports_shorthand.js"
    exports_file.write_text(
        encoding="utf-8",
        data="""
// Using exports shorthand
exports.utilityA = function utilityA() {
    return "A";
};

exports.utilityB = function utilityB() {
    return "B";
};

exports.constant = "CONSTANT_VALUE";

exports.objectExport = {
    nested: {
        method: function() {
            return "nested method";
        }
    }
};

// Arrow function exports
exports.arrowFunc = (x) => x * 2;

// Class export via exports
exports.ExportedClass = class ExportedClass {
    constructor() {
        this.value = "exported";
    }
};
""",
    )

    run_updater(javascript_modules_project, mock_ingestor)

    project_name = javascript_modules_project.name

    function_calls = get_nodes(mock_ingestor, "Function")

    class_calls = get_nodes(mock_ingestor, "Class")

    all_nodes = function_calls + class_calls
    exported_nodes = [
        call
        for call in all_nodes
        if any(
            file in call[0][1]["qualified_name"]
            for file in ["commonjs_exports", "exports_shorthand"]
        )
    ]

    assert len(exported_nodes) >= 5, (
        f"Expected at least 5 exported functions/classes, found {len(exported_nodes)}"
    )

    created_functions = get_qualified_names(function_calls)
    expected_functions = [
        f"{project_name}.exports_shorthand.utilityA",
        f"{project_name}.exports_shorthand.utilityB",
        f"{project_name}.exports_shorthand.arrowFunc",
    ]

    for expected in expected_functions:
        assert expected in created_functions, f"Missing exported function: {expected}"


def test_es6_export_patterns(
    javascript_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test ES6 export patterns including default and named exports."""
    test_file = javascript_modules_project / "es6_exports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Named exports
export const API_URL = 'https://api.example.com';
export const TIMEOUT = 5000;

export let counter = 0;
export var deprecated = "old";

export function fetchData(url) {
    return fetch(url).then(res => res.json());
}

export async function fetchDataAsync(url) {
    const response = await fetch(url);
    return response.json();
}

export const arrowFunction = (x) => x * 2;

export class DataService {
    constructor(baseUrl) {
        this.baseUrl = baseUrl;
    }

    getData(endpoint) {
        return fetchData(this.baseUrl + endpoint);
    }
}

// Default export (function)
export default function processData(data) {
    return data.map(item => ({
        ...item,
        processed: true
    }));
}

// Can't have multiple defaults, but showing the pattern
// export default class DefaultClass {}  // This would be an error

// Export list
const helper1 = () => "helper1";
const helper2 = () => "helper2";
const helper3 = () => "helper3";

export { helper1, helper2, helper3 };

// Export with rename
const internalName = "internal";
export { internalName as externalName };

// Re-exports from other modules
export { isEmail, isPhone } from './lib/validators';
export { default as Validator } from './lib/validators';
export * from './utils/constants';

// Namespace export
export * as validationUtils from './lib/validators';

// Export at declaration
export let { destructured1, destructured2 } = {
    destructured1: "d1",
    destructured2: "d2"
};

// Export functions with different patterns
export function* generatorFunction() {
    yield 1;
    yield 2;
    yield 3;
}

export const asyncArrow = async () => {
    return await Promise.resolve("async arrow");
};

// Complex export expressions
export const complexExport = (() => {
    const private = "hidden";
    return {
        public: "visible",
        method() {
            return private;
        }
    };
})();

// Type-like exports (even in JS)
export const UserType = {
    ADMIN: 'admin',
    USER: 'user',
    GUEST: 'guest'
};
""",
    )

    reexport_file = javascript_modules_project / "reexports.js"
    reexport_file.write_text(
        encoding="utf-8",
        data="""
// Re-export patterns
export { fetchData, DataService } from './es6_exports';
export { default } from './es6_exports';
export { default as processDataRenamed } from './es6_exports';

// Aggregate exports
export * from './es6_exports';
export * from './lib/validators';

// Namespace re-exports
export * as exports from './es6_exports';
export * as validators from './lib/validators';

// Mixed re-exports and local exports
export { API_URL as API_ENDPOINT } from './es6_exports';

export const localExport = "local";

// Re-export with local processing
import { fetchData as originalFetch } from './es6_exports';

export function enhancedFetch(url, options) {
    console.log('Fetching:', url);
    return originalFetch(url, options);
}
""",
    )

    run_updater(javascript_modules_project, mock_ingestor)

    project_name = javascript_modules_project.name

    function_calls = get_nodes(mock_ingestor, "Function")

    class_calls = get_nodes(mock_ingestor, "Class")

    created_functions = get_qualified_names(function_calls)
    created_classes = get_qualified_names(class_calls)

    expected_functions = [
        f"{project_name}.es6_exports.fetchData",
        f"{project_name}.es6_exports.fetchDataAsync",
        f"{project_name}.es6_exports.arrowFunction",
        f"{project_name}.es6_exports.processData",
        f"{project_name}.es6_exports.generatorFunction",
        f"{project_name}.es6_exports.asyncArrow",
        f"{project_name}.reexports.enhancedFetch",
    ]

    for expected in expected_functions:
        assert expected in created_functions, (
            f"Missing ES6 exported function: {expected}"
        )

    expected_classes = [
        f"{project_name}.es6_exports.DataService",
    ]

    for expected in expected_classes:
        assert expected in created_classes, f"Missing ES6 exported class: {expected}"

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    reexport_imports = [
        call for call in import_relationships if "reexports" in call.args[0][2]
    ]

    assert len(reexport_imports) >= 3, (
        f"Expected at least 3 re-export imports, found {len(reexport_imports)}"
    )


def test_mixed_module_systems(
    javascript_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test files using both CommonJS and ES6 modules."""
    test_file = javascript_modules_project / "mixed_modules.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Mixed CommonJS and ES6 - This is technically invalid but sometimes seen
// ES6 imports
import React from 'react';
import { useState, useEffect } from 'react';
import * as utils from './utils';

// CommonJS require
const fs = require('fs');
const path = require('path');
const { readFile, writeFile } = require('fs').promises;

// ES6 exports
export const config = {
    apiUrl: process.env.API_URL || 'http://localhost:3000',
    timeout: 5000
};

export function processFile(filename) {
    const fullPath = path.join(__dirname, filename);
    return readFile(fullPath, 'utf8');
}

// CommonJS exports (would override ES6 in practice)
module.exports.legacyFunction = function() {
    return "legacy";
};

// Dynamic imports
export async function loadModule(moduleName) {
    const module = await import(`./${moduleName}`);
    return module.default || module;
}

// Conditional exports based on environment
if (typeof window !== 'undefined') {
    // Browser environment
    export const browserOnly = true;
} else {
    // Node environment
    module.exports.nodeOnly = true;
}

// Class using both module systems
export class MixedService {
    constructor() {
        // Using CommonJS require
        this.validator = require('./lib/validators');
        // Using ES6 import result
        this.React = React;
    }

    async loadDynamic(name) {
        // Dynamic import
        const mod = await import(`./dynamic/${name}`);
        return mod;
    }
}

// Function that uses both
export function hybridFunction() {
    const commonjsModule = require('./models/User');
    const [state, setState] = useState(null);

    return {
        User: commonjsModule,
        state: state
    };
}
""",
    )

    umd_file = javascript_modules_project / "umd_module.js"
    umd_file.write_text(
        encoding="utf-8",
        data="""
// UMD (Universal Module Definition) pattern
(function (root, factory) {
    if (typeof define === 'function' && define.amd) {
        // AMD
        define(['exports', 'react'], factory);
    } else if (typeof exports === 'object' && typeof exports.nodeName !== 'string') {
        // CommonJS
        factory(exports, require('react'));
    } else {
        // Browser globals
        factory((root.MyModule = {}), root.React);
    }
}(typeof self !== 'undefined' ? self : this, function (exports, React) {
    'use strict';

    // Module implementation
    function MyComponent(props) {
        return React.createElement('div', null, props.children);
    }

    function utility() {
        return "utility function";
    }

    class MyClass {
        constructor() {
            this.name = "MyClass";
        }

        render() {
            return MyComponent({ children: this.name });
        }
    }

    // Exports
    exports.MyComponent = MyComponent;
    exports.utility = utility;
    exports.MyClass = MyClass;
    exports.default = MyComponent;

    // Also attach to exports for CommonJS default export pattern
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = exports;
    }
}));
""",
    )

    run_updater(javascript_modules_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    mixed_imports = [
        call for call in import_relationships if "mixed_modules" in call.args[0][2]
    ]

    imported_modules = [call.args[2][2] for call in mixed_imports]

    assert any("react" in module.lower() for module in imported_modules), (
        "Missing ES6 React import"
    )

    assert any("fs" in module for module in imported_modules), (
        "Missing CommonJS fs require"
    )

    function_calls = get_nodes(mock_ingestor, "Function")

    class_calls = get_nodes(mock_ingestor, "Class")

    mixed_functions = [
        call
        for call in function_calls
        if "mixed_modules" in call[0][1]["qualified_name"]
    ]

    assert len(mixed_functions) >= 3, (
        f"Expected at least 3 functions in mixed modules, found {len(mixed_functions)}"
    )

    umd_functions = [
        call for call in function_calls if "umd_module" in call[0][1]["qualified_name"]
    ]

    umd_classes = [
        call for call in class_calls if "umd_module" in call[0][1]["qualified_name"]
    ]

    assert len(umd_functions) >= 2, (
        f"Expected at least 2 UMD functions, found {len(umd_functions)}"
    )

    assert len(umd_classes) >= 1, (
        f"Expected at least 1 UMD class, found {len(umd_classes)}"
    )


def test_circular_dependencies(
    javascript_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test circular dependency handling in modules."""
    module_a = javascript_modules_project / "circular_a.js"
    module_a.write_text(
        encoding="utf-8",
        data="""
// Circular dependency - A imports B
const B = require('./circular_b');

class A {
    constructor() {
        this.name = "A";
        this.b = null;
    }

    setB(b) {
        this.b = b;
    }

    callB() {
        if (this.b) {
            return this.b.getName();
        }
        return "No B";
    }

    getName() {
        return this.name;
    }
}

module.exports = A;

// Using B after export to handle circular dependency
if (B) {
    const bInstance = new B();
    console.log("B instance in A:", bInstance.getName());
}
""",
    )

    module_b = javascript_modules_project / "circular_b.js"
    module_b.write_text(
        encoding="utf-8",
        data="""
// Circular dependency - B imports A
const A = require('./circular_a');

class B {
    constructor() {
        this.name = "B";
        this.a = null;
    }

    setA(a) {
        this.a = a;
    }

    callA() {
        if (this.a) {
            return this.a.getName();
        }
        return "No A";
    }

    getName() {
        return this.name;
    }
}

module.exports = B;

// Using A after export to handle circular dependency
if (A) {
    const aInstance = new A();
    console.log("A instance in B:", aInstance.getName());
}
""",
    )

    es6_circular_a = javascript_modules_project / "es6_circular_a.js"
    es6_circular_a.write_text(
        encoding="utf-8",
        data="""
// ES6 Circular dependency - A imports B
import { B, createB } from './es6_circular_b.js';

export class A {
    constructor() {
        this.name = "A";
        this.b = null;
    }

    setB(b) {
        this.b = b;
    }

    getName() {
        return this.name;
    }
}

export function createA() {
    return new A();
}

// Function that uses B
export function useB() {
    const b = createB();
    return b.getName();
}
""",
    )

    es6_circular_b = javascript_modules_project / "es6_circular_b.js"
    es6_circular_b.write_text(
        encoding="utf-8",
        data="""
// ES6 Circular dependency - B imports A
import { A, createA } from './es6_circular_a.js';

export class B {
    constructor() {
        this.name = "B";
        this.a = null;
    }

    setA(a) {
        this.a = a;
    }

    getName() {
        return this.name;
    }
}

export function createB() {
    return new B();
}

// Function that uses A
export function useA() {
    const a = createA();
    return a.getName();
}
""",
    )

    run_updater(javascript_modules_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    circular_imports = [
        call
        for call in import_relationships
        if "circular_a" in call.args[0][2] or "circular_b" in call.args[0][2]
    ]

    a_imports_b = any(
        "circular_a" in call.args[0][2] and "circular_b" in call.args[2][2]
        for call in circular_imports
    )
    b_imports_a = any(
        "circular_b" in call.args[0][2] and "circular_a" in call.args[2][2]
        for call in circular_imports
    )

    assert a_imports_b, "circular_a should import circular_b"
    assert b_imports_a, "circular_b should import circular_a"

    es6_circular_imports = [
        call
        for call in import_relationships
        if "es6_circular_a" in call.args[0][2] or "es6_circular_b" in call.args[0][2]
    ]

    es6_a_imports_b = any(
        "es6_circular_a" in call.args[0][2] and "es6_circular_b" in call.args[2][2]
        for call in es6_circular_imports
    )
    es6_b_imports_a = any(
        "es6_circular_b" in call.args[0][2] and "es6_circular_a" in call.args[2][2]
        for call in es6_circular_imports
    )

    assert es6_a_imports_b, "es6_circular_a should import es6_circular_b"
    assert es6_b_imports_a, "es6_circular_b should import es6_circular_a"


def test_dynamic_exports(
    javascript_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test dynamic and conditional export patterns."""
    test_file = javascript_modules_project / "dynamic_exports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Dynamic property exports
const methods = ['get', 'post', 'put', 'delete'];

methods.forEach(method => {
    exports[method] = function(url, data) {
        return fetch(url, {
            method: method.toUpperCase(),
            body: JSON.stringify(data)
        });
    };
});

// Conditional exports based on environment
if (process.env.NODE_ENV === 'development') {
    exports.debug = function(message) {
        console.log('[DEBUG]:', message);
    };

    exports.DevTools = class DevTools {
        constructor() {
            this.enabled = true;
        }

        log(message) {
            if (this.enabled) {
                console.log('[DevTools]:', message);
            }
        }
    };
}

// Dynamic module.exports assignment
const moduleConfig = {
    version: '1.0.0',
    features: []
};

if (process.env.FEATURE_A) {
    moduleConfig.features.push('featureA');
    moduleConfig.featureA = function() {
        return "Feature A enabled";
    };
}

if (process.env.FEATURE_B) {
    moduleConfig.features.push('featureB');
    moduleConfig.featureB = function() {
        return "Feature B enabled";
    };
}

module.exports = moduleConfig;

// Factory pattern exports
function createAPI(config) {
    return {
        get(endpoint) {
            return fetch(config.baseUrl + endpoint);
        },
        post(endpoint, data) {
            return fetch(config.baseUrl + endpoint, {
                method: 'POST',
                body: JSON.stringify(data)
            });
        }
    };
}

module.exports = createAPI({ baseUrl: 'https://api.example.com' });

// Computed property exports
const operations = {
    ADD: 'add',
    SUBTRACT: 'subtract',
    MULTIPLY: 'multiply',
    DIVIDE: 'divide'
};

Object.entries(operations).forEach(([key, value]) => {
    exports[value] = function(a, b) {
        switch(value) {
            case 'add': return a + b;
            case 'subtract': return a - b;
            case 'multiply': return a * b;
            case 'divide': return a / b;
        }
    };

    exports[`${value}Async`] = async function(a, b) {
        return new Promise(resolve => {
            setTimeout(() => resolve(exports[value](a, b)), 100);
        });
    };
});

// Proxy-based exports
const handler = {
    get(target, property) {
        if (property in target) {
            return target[property];
        }
        return function() {
            console.log(`Method ${property} not implemented`);
        };
    }
};

module.exports = new Proxy({
    implementedMethod() {
        return "This method exists";
    }
}, handler);
""",
    )

    es6_dynamic = javascript_modules_project / "es6_dynamic_exports.js"
    es6_dynamic.write_text(
        encoding="utf-8",
        data="""
// ES6 dynamic exports using export declarations
const endpoints = {
    users: '/api/users',
    posts: '/api/posts',
    comments: '/api/comments'
};

// Can't dynamically create named exports, but can export dynamic object
export const api = Object.entries(endpoints).reduce((acc, [key, endpoint]) => {
    acc[key] = {
        getAll: () => fetch(endpoint),
        getById: (id) => fetch(`${endpoint}/${id}`),
        create: (data) => fetch(endpoint, {
            method: 'POST',
            body: JSON.stringify(data)
        }),
        update: (id, data) => fetch(`${endpoint}/${id}`, {
            method: 'PUT',
            body: JSON.stringify(data)
        }),
        delete: (id) => fetch(`${endpoint}/${id}`, {
            method: 'DELETE'
        })
    };
    return acc;
}, {});

// Dynamic default export based on conditions
let DefaultExport;

if (typeof window !== 'undefined') {
    DefaultExport = class BrowserComponent {
        render() {
            return "<div>Browser Component</div>";
        }
    };
} else {
    DefaultExport = class ServerComponent {
        render() {
            return "<div>Server Component</div>";
        }
    };
}

export default DefaultExport;

// Factory function for dynamic exports
export function createService(type) {
    switch(type) {
        case 'http':
            return {
                request(url) {
                    return fetch(url);
                }
            };
        case 'websocket':
            return {
                connect(url) {
                    return new WebSocket(url);
                }
            };
        default:
            throw new Error(`Unknown service type: ${type}`);
    }
}

// Re-export dynamically
const modules = ['validators', 'helpers', 'constants'];

// This is not valid ES6, but showing the concept
// In practice, you'd need to list each export explicitly
export * from './lib/validators';

// Namespace object with dynamic properties
export const utils = new Proxy({}, {
    get(target, property) {
        return function(...args) {
            console.log(`Calling util: ${property} with args:`, args);
            return `${property} result`;
        };
    }
});
""",
    )

    run_updater(javascript_modules_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    class_calls = get_nodes(mock_ingestor, "Class")

    dynamic_functions = [
        call
        for call in function_calls
        if "dynamic_exports" in call[0][1]["qualified_name"]
    ]

    assert len(dynamic_functions) >= 2, (
        f"Expected at least 2 functions from dynamic exports, found {len(dynamic_functions)}"
    )

    es6_dynamic_nodes = [
        call
        for call in function_calls + class_calls
        if "es6_dynamic_exports" in call[0][1]["qualified_name"]
    ]

    assert len(es6_dynamic_nodes) >= 2, (
        f"Expected at least 2 nodes from ES6 dynamic exports, found {len(es6_dynamic_nodes)}"
    )


def test_aliased_re_exports(
    javascript_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test aliased re-export patterns to verify the fix for export { name as alias } from './module'."""

    (javascript_modules_project / "source_module.js").write_text(
        encoding="utf-8",
        data="""
export const originalName = "original value";
export const anotherExport = "another value";
export function originalFunction() {
    return "original function";
}
export class OriginalClass {
    constructor() {
        this.name = "original";
    }
}
""",
    )

    (javascript_modules_project / "utils_module.js").write_text(
        encoding="utf-8",
        data="""
export const utilA = "utility A";
export const utilB = "utility B";
export function helperFunc() {
    return "helper";
}
""",
    )

    test_file = javascript_modules_project / "aliased_re_exports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic aliased re-export: export { name as alias } from './module'
export { originalName as aliasedName } from './source_module';
export { anotherExport as renamedExport } from './source_module';

// Multiple aliased re-exports from same module
export {
    originalFunction as aliasedFunction,
    OriginalClass as RenamedClass
} from './source_module';

// Mixed normal and aliased re-exports
export {
    utilA,  // normal re-export
    utilB as renamedUtilB,  // aliased re-export
    helperFunc as helper  // aliased re-export
} from './utils_module';

// Single line multiple aliases
export { originalName as firstAlias, anotherExport as secondAlias } from './source_module';

// Complex nested aliasing patterns
export {
    originalFunction as func,
    OriginalClass as MyClass,
    originalName as value
} from './source_module';

// Using the re-exported items to ensure they're tracked correctly
import { aliasedName, renamedExport, aliasedFunction, RenamedClass } from './aliased_re_exports';

function useReExports() {
    console.log(aliasedName);  // Should map to source_module.originalName
    console.log(renamedExport);  // Should map to source_module.anotherExport
    const result = aliasedFunction();  // Should map to source_module.originalFunction
    const instance = new RenamedClass();  // Should map to source_module.OriginalClass
    return { result, instance };
}

export { useReExports };
""",
    )

    run_updater(javascript_modules_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    aliased_re_export_imports = [
        call for call in import_relationships if "aliased_re_exports" in call.args[0][2]
    ]

    assert len(aliased_re_export_imports) >= 3, (
        f"Expected at least 3 aliased re-export import relationships, "
        f"found {len(aliased_re_export_imports)}"
    )

    imported_modules = [call.args[2][2] for call in aliased_re_export_imports]
    expected_modules = [
        "source_module",
        "utils_module",
    ]

    for expected in expected_modules:
        assert any(expected in module for module in imported_modules), (
            f"Missing import relationship for module: {expected}\n"
            f"Found imported modules: {imported_modules}"
        )

    print(
        "   - Aliased re-exports are correctly parsed (export { name as alias } bug fixed)"
    )


def test_module_comprehensive(
    javascript_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all module patterns create proper relationships."""
    test_file = javascript_modules_project / "comprehensive_modules.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every JavaScript module pattern in one file

// CommonJS requires
const fs = require('fs');
const path = require('path');
const { User } = require('./models/User');

// ES6 imports
import React, { useState, useEffect } from 'react';
import * as validators from './lib/validators';
import defaultValidator from './lib/validators';

// Dynamic import
import('./utils/helpers').then(helpers => {
    console.log('Helpers loaded:', helpers);
});

// CommonJS exports
module.exports.commonjsExport = function() {
    return "commonjs";
};

// ES6 exports
export const es6Export = function() {
    return "es6";
};

export default function defaultExport() {
    return "default";
}

// Mixed class with imports
export class MixedModule {
    constructor() {
        this.fs = fs;
        this.React = React;
        this.User = User;
    }

    async loadDynamic() {
        const helpers = await import('./utils/helpers');
        return helpers;
    }

    useValidator(email) {
        return validators.isEmail(email);
    }
}

// Re-exports
export { isPhone } from './lib/validators';
export * from './utils/constants';

// Using all imports
const [state, setState] = useState(null);
const filePath = path.join(__dirname, 'file.txt');
const user = new User('John', 'john@example.com');
const isValid = defaultValidator('email', 'test@test.com');

// Function using imports
export function useImports() {
    const content = fs.readFileSync(filePath);
    const valid = validators.isEmail(user.email);

    useEffect(() => {
        console.log('Effect ran');
    }, []);

    return {
        content,
        valid,
        state
    };
}
""",
    )

    run_updater(javascript_modules_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    calls_relationships = get_relationships(mock_ingestor, "CALLS")

    comprehensive_imports = [
        call
        for call in import_relationships
        if "comprehensive_modules" in call.args[0][2]
    ]

    assert len(comprehensive_imports) >= 6, (
        f"Expected at least 6 comprehensive imports, found {len(comprehensive_imports)}"
    )

    imported_modules = [call.args[2][2] for call in comprehensive_imports]

    assert any("fs" in module for module in imported_modules), "Missing fs import"
    assert any("path" in module for module in imported_modules), "Missing path import"

    assert any("react" in module.lower() for module in imported_modules), (
        "Missing React import"
    )
    assert any("validators" in module for module in imported_modules), (
        "Missing validators import"
    )

    function_calls = get_nodes(mock_ingestor, "Function")

    comprehensive_functions = [
        call
        for call in function_calls
        if "comprehensive_modules" in call[0][1]["qualified_name"]
    ]

    created_functions = {
        call[0][1]["qualified_name"] for call in comprehensive_functions
    }
    project_name = javascript_modules_project.name

    expected_functions = [
        f"{project_name}.comprehensive_modules.commonjsExport",
        f"{project_name}.comprehensive_modules.es6Export",
        f"{project_name}.comprehensive_modules.defaultExport",
        f"{project_name}.comprehensive_modules.useImports",
    ]

    for expected in expected_functions:
        assert expected in created_functions, f"Missing exported function: {expected}"

    assert defines_relationships, "Should still have DEFINES relationships"
    assert calls_relationships, "Should still have CALLS relationships"
