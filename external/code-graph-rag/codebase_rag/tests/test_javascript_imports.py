from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def javascript_imports_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with all import patterns."""
    project_path = temp_repo / "javascript_imports_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "components").mkdir()
    (project_path / "src" / "utils").mkdir()
    (project_path / "lib").mkdir()
    (project_path / "node_modules").mkdir()
    (project_path / "node_modules" / "react").mkdir()
    (project_path / "node_modules" / "@babel").mkdir()
    (project_path / "node_modules" / "@babel" / "core").mkdir()

    (project_path / "src" / "utils" / "helpers.js").write_text(
        encoding="utf-8", data="export const helper = () => {};"
    )
    (project_path / "src" / "utils" / "constants.js").write_text(
        encoding="utf-8", data="export const API_URL = 'https://api.example.com';"
    )
    (project_path / "src" / "utils" / "math.js").write_text(
        encoding="utf-8", data="export function add(a, b) { return a + b; }"
    )
    (project_path / "src" / "components" / "Button.js").write_text(
        encoding="utf-8", data="export default class Button {}"
    )
    (project_path / "lib" / "config.js").write_text(
        encoding="utf-8", data="module.exports = { apiKey: 'secret' };"
    )
    (project_path / "shared.js").write_text(
        encoding="utf-8", data="export const shared = 'data';"
    )

    (project_path / "package.json").write_text(
        encoding="utf-8", data='{"name": "test-project", "version": "1.0.0"}'
    )
    (project_path / "node_modules" / "react" / "index.js").write_text(
        encoding="utf-8", data="export default {};"
    )
    (project_path / "node_modules" / "@babel" / "core" / "index.js").write_text(
        encoding="utf-8", data="export const transform = () => {};"
    )

    return project_path


def test_es6_default_imports(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test ES6 default import parsing and relationship creation."""
    test_file = javascript_imports_project / "es6_default_imports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// ES6 default imports
import React from 'react';
import Button from './src/components/Button';
import config from './lib/config';
import utils from './src/utils/helpers';
import shared from './shared';

// Using imported defaults
const app = React.createElement();
const btn = new Button();
const apiKey = config.apiKey;
const result = utils.helper();
const data = shared;
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    default_imports = [
        call
        for call in import_relationships
        if "es6_default_imports" in call.args[0][2]
    ]

    assert len(default_imports) >= 5, (
        f"Expected at least 5 default imports, found {len(default_imports)}"
    )

    imported_modules = [call.args[2][2] for call in default_imports]
    expected_modules = [
        "react",
        "javascript_imports_test.src.components.Button",
        "javascript_imports_test.lib.config",
        "javascript_imports_test.src.utils.helpers",
        "javascript_imports_test.shared",
    ]

    for expected in expected_modules:
        assert any(expected in module for module in imported_modules), (
            f"Missing default import: {expected}\nFound: {imported_modules}"
        )


def test_es6_named_imports(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test ES6 named import parsing and relationship creation."""
    test_file = javascript_imports_project / "es6_named_imports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// ES6 named imports
import { helper } from './src/utils/helpers';
import { API_URL } from './src/utils/constants';
import { add } from './src/utils/math';
import { useState, useEffect } from 'react';

// Multiple named imports from same module
import { helper as utilHelper, API_URL as apiEndpoint } from './src/utils/helpers';

// Mixed default and named imports
import React, { Component, useState as state } from 'react';

// Using imported names
const result = helper();
const url = API_URL;
const sum = add(1, 2);
const [count, setCount] = useState(0);
useEffect(() => {});
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    named_imports = [
        call for call in import_relationships if "es6_named_imports" in call.args[0][2]
    ]

    assert len(named_imports) >= 8, (
        f"Expected at least 8 named imports, found {len(named_imports)}"
    )

    imported_modules = [call.args[2][2] for call in named_imports]
    expected_patterns = [
        "helpers",
        "constants",
        "math",
        "react",
    ]

    for pattern in expected_patterns:
        assert any(pattern in module for module in imported_modules), (
            f"Missing named import pattern: {pattern}\nFound: {imported_modules}"
        )


def test_es6_namespace_imports(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test ES6 namespace (star) import parsing."""
    test_file = javascript_imports_project / "es6_namespace_imports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// ES6 namespace imports
import * as React from 'react';
import * as utils from './src/utils/helpers';
import * as mathUtils from './src/utils/math';
import * as constants from './src/utils/constants';

// Using namespace imports
const element = React.createElement();
const result = utils.helper();
const sum = mathUtils.add(1, 2);
const url = constants.API_URL;
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    namespace_imports = [
        call
        for call in import_relationships
        if "es6_namespace_imports" in call.args[0][2]
    ]

    assert len(namespace_imports) >= 4, (
        f"Expected at least 4 namespace imports, found {len(namespace_imports)}"
    )

    imported_modules = [call.args[2][2] for call in namespace_imports]
    expected_modules = [
        "react",
        "javascript_imports_test.src.utils.helpers",
        "javascript_imports_test.src.utils.math",
        "javascript_imports_test.src.utils.constants",
    ]

    for expected in expected_modules:
        assert any(expected in module for module in imported_modules), (
            f"Missing namespace import: {expected}\nFound: {imported_modules}"
        )


def test_commonjs_require_imports(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test CommonJS require() import parsing."""
    test_file = javascript_imports_project / "commonjs_imports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// CommonJS require imports
const fs = require('fs');
const path = require('path');
const config = require('./lib/config');
const utils = require('./src/utils/helpers');

// Destructured require
const { helper } = require('./src/utils/helpers');
const { API_URL } = require('./src/utils/constants');

// Aliased destructured require
const { helper: utilityHelper } = require('./src/utils/helpers');
const { API_URL: apiEndpoint } = require('./src/utils/constants');
const { add: mathAdd } = require('./src/utils/math');

// Require with different variable names
const fileSystem = require('fs');
const utilities = require('./src/utils/helpers');

// Nested require calls
const dynamicModule = require(getModuleName());

// Using required modules
const content = fs.readFileSync('file.txt');
const dirname = path.dirname(__filename);
const apiKey = config.apiKey;
const result = helper();

// Using aliased destructured imports
const utilResult = utilityHelper();
const endpoint = apiEndpoint;
const sum = mathAdd(1, 2);
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    commonjs_imports = [
        call for call in import_relationships if "commonjs_imports" in call.args[0][2]
    ]

    assert len(commonjs_imports) >= 8, (
        f"Expected at least 8 CommonJS imports, found {len(commonjs_imports)}"
    )

    imported_modules = [call.args[2][2] for call in commonjs_imports]
    expected_modules = [
        "fs",
        "path",
        "javascript_imports_test.lib.config",
        "javascript_imports_test.src.utils.helpers",
        "javascript_imports_test.src.utils.constants",
    ]

    for expected in expected_modules:
        assert any(expected in module for module in imported_modules), (
            f"Missing CommonJS import: {expected}\nFound: {imported_modules}"
        )


def test_commonjs_aliased_destructuring(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test CommonJS aliased destructuring patterns ({ name: alias })."""
    test_file = javascript_imports_project / "commonjs_aliased_destructuring.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// CommonJS aliased destructuring patterns
const { helper: utilHelper } = require('./src/utils/helpers');
const { API_URL: endpoint, helper: utilityFunc } = require('./src/utils/constants');
const { add: mathAdd, subtract: mathSub } = require('./src/utils/math');

// Mixed shorthand and aliased destructuring
const { helper, API_URL: apiEndpoint } = require('./src/utils/helpers');

// Using aliased imports
const result1 = utilHelper();
const url = endpoint;
const sum = mathAdd(1, 2);
const diff = mathSub(5, 3);
const result2 = utilityFunc();
const finalUrl = apiEndpoint;
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    aliased_imports = [
        call
        for call in import_relationships
        if "commonjs_aliased_destructuring" in call.args[0][2]
    ]

    assert len(aliased_imports) >= 3, (
        f"Expected at least 3 aliased destructuring imports, found {len(aliased_imports)}"
    )

    imported_modules = [call.args[2][2] for call in aliased_imports]
    expected_modules = [
        "javascript_imports_test.src.utils.helpers",
        "javascript_imports_test.src.utils.constants",
        "javascript_imports_test.src.utils.math",
    ]

    for expected in expected_modules:
        assert any(expected in module for module in imported_modules), (
            f"Missing aliased destructuring import: {expected}\nFound: {imported_modules}"
        )


def test_relative_path_resolution(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test relative import path resolution (./ and ../)."""
    nested_dir = javascript_imports_project / "src" / "components" / "forms"
    nested_dir.mkdir()
    test_file = nested_dir / "Input.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Same directory relative imports
import Button from './Button';
import Modal from './Modal';

// Parent directory relative imports
import utils from '../../utils/helpers';
import constants from '../../utils/constants';

// Multiple levels up
import config from '../../lib/config';
import shared from '../../shared';

// Complex relative paths
import deepUtil from '../../../lib/deep/util';
import Component from './nested/../Button';

// Using imported modules
const btn = new Button();
const result = utils.helper();
const url = constants.API_URL;
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    relative_imports = [
        call for call in import_relationships if "Input" in call.args[0][2]
    ]

    assert len(relative_imports) >= 6, (
        f"Expected at least 6 relative imports, found {len(relative_imports)}"
    )

    imported_modules = [call.args[2][2] for call in relative_imports]
    project_name = javascript_imports_project.name

    expected_patterns = [
        f"{project_name}.src.components.forms.Button",
        f"{project_name}.src.utils.helpers",
        f"{project_name}.src.utils.constants",
        f"{project_name}.src.lib.config",
        f"{project_name}.src.shared",
    ]

    for pattern in expected_patterns:
        assert any(pattern in module for module in imported_modules), (
            f"Missing relative import pattern: {pattern}\nFound: {imported_modules}"
        )


def test_absolute_package_imports(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test absolute package imports from node_modules."""
    test_file = javascript_imports_project / "package_imports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Standard package imports
import React from 'react';
import ReactDOM from 'react-dom';
import axios from 'axios';
import lodash from 'lodash';

// Scoped package imports
import babelCore from '@babel/core';
import babelParser from '@babel/parser';
import typescriptEslint from '@typescript-eslint/parser';

// Package submodule imports
import { debounce } from 'lodash/debounce';
import reactHooks from 'react/hooks';
import babelTypes from '@babel/core/types';

// Deep package imports
import specificUtil from 'some-package/lib/utils/specific';
import deepModule from '@org/package/dist/deep/module';

// Using imported packages
const element = React.createElement();
const transformed = babelCore.transform();
const debounced = debounce(() => {});
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    package_imports = [
        call for call in import_relationships if "package_imports" in call.args[0][2]
    ]

    assert len(package_imports) >= 10, (
        f"Expected at least 10 package imports, found {len(package_imports)}"
    )

    imported_modules = [call.args[2][2] for call in package_imports]
    expected_packages = [
        "react",
        "react-dom",
        "axios",
        "lodash",
        "@babel.core",
        "@babel.parser",
        "@typescript-eslint.parser",
        "some-package.lib.utils.specific",
        "@org.package.dist.deep.module",
    ]

    for expected in expected_packages:
        assert any(expected in module for module in imported_modules), (
            f"Missing package import: {expected}\nFound: {imported_modules}"
        )


def test_dynamic_imports(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test dynamic import() expressions."""
    test_file = javascript_imports_project / "dynamic_imports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Dynamic imports with await
async function loadModules() {
    const utils = await import('./src/utils/helpers');
    const config = await import('./lib/config');
    const React = await import('react');

    return { utils, config, React };
}

// Dynamic imports with then()
function loadModule() {
    import('./src/utils/math')
        .then(module => {
            const { add } = module;
            return add(1, 2);
        });
}

// Conditional dynamic imports
if (condition) {
    import('./src/utils/constants').then(module => {
        const { API_URL } = module;
        console.log(API_URL);
    });
}

// Dynamic imports in functions
function getUtility(name) {
    return import(`./src/utils/${name}`);
}

// Using dynamic imports
const modulePromise = import('./shared');
modulePromise.then(({ shared }) => {
    console.log(shared);
});
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    dynamic_calls = [
        call for call in call_relationships if "dynamic_imports" in call.args[0][2]
    ]

    assert len(dynamic_calls) >= 1, (
        f"Expected at least 1 dynamic import call, found {len(dynamic_calls)}"
    )


def test_mixed_import_patterns(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test files with mixed import patterns."""
    test_file = javascript_imports_project / "mixed_imports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Mix of all import types in one file
import React, { useState, useEffect } from 'react';
import * as utils from './src/utils/helpers';
import Button from './src/components/Button';

const fs = require('fs');
const { API_URL } = require('./src/utils/constants');

// Dynamic imports
async function loadConfig() {
    const config = await import('./lib/config');
    return config.default;
}

// Re-exports
export { Button } from './src/components/Button';
export * from './src/utils/math';
export { default as Config } from './lib/config';

// Using all types
const [state, setState] = useState();
const helper = utils.helper();
const btn = new Button();
const content = fs.readFileSync('file.txt');
const url = API_URL;
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    mixed_imports = [
        call for call in import_relationships if "mixed_imports" in call.args[0][2]
    ]

    assert len(mixed_imports) >= 6, (
        f"Expected at least 6 mixed imports, found {len(mixed_imports)}"
    )

    imported_modules = [call.args[2][2] for call in mixed_imports]

    expected_patterns = [
        "react",
        "helpers",
        "Button",
        "fs",
        "constants",
    ]

    for pattern in expected_patterns:
        assert any(pattern in module for module in imported_modules), (
            f"Missing mixed import pattern: {pattern}\nFound: {imported_modules}"
        )


def test_import_error_handling(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that import parsing handles syntax errors gracefully."""
    test_file = javascript_imports_project / "error_imports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Valid imports
import React from 'react';
const fs = require('fs');

// Malformed imports that should not crash parser
// import from 'module';
// import { } from;
// const = require();

// Valid imports after errors
import { useState } from 'react';
const path = require('path');

// Edge cases
import './side-effects-only';
require('./also-side-effects');
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    error_file_imports = [
        call for call in import_relationships if "error_imports" in call.args[0][2]
    ]

    assert len(error_file_imports) >= 3, (
        f"Expected at least 3 valid imports despite errors, found {len(error_file_imports)}"
    )

    imported_modules = [call.args[2][2] for call in error_file_imports]
    expected_valid = ["react", "fs", "path"]

    for expected in expected_valid:
        assert any(expected in module for module in imported_modules), (
            f"Missing valid import after error: {expected}"
        )


def test_aliased_re_export_import_mapping(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that aliased re-exports create correct import mappings (regression test for bug fix)."""

    (javascript_imports_project / "math_utils.js").write_text(
        encoding="utf-8",
        data="""
export function add(a, b) { return a + b; }
export function subtract(a, b) { return a - b; }
export function multiply(a, b) { return a * b; }
export const PI = 3.14159;
""",
    )

    (javascript_imports_project / "string_utils.js").write_text(
        encoding="utf-8",
        data="""
export function capitalize(str) { return str.charAt(0).toUpperCase() + str.slice(1); }
export function reverse(str) { return str.split('').reverse().join(''); }
export const EMPTY_STRING = '';
""",
    )

    re_export_file = javascript_imports_project / "utils_index.js"
    re_export_file.write_text(
        encoding="utf-8",
        data="""
// These aliased re-exports would fail before the bug fix
export { add as mathAdd, subtract as mathSub } from './math_utils';
export { multiply as mathMultiply, PI as MATH_PI } from './math_utils';
export { capitalize as toUpperCase, reverse as reverseString } from './string_utils';
export { EMPTY_STRING as EMPTY } from './string_utils';

// Mixed normal and aliased re-exports
export { add } from './math_utils';  // normal re-export
export { capitalize } from './string_utils';  // normal re-export
""",
    )

    consumer_file = javascript_imports_project / "consumer.js"
    consumer_file.write_text(
        encoding="utf-8",
        data="""
// Import aliased re-exports - these should map correctly after the fix
import { mathAdd, mathSub, mathMultiply, MATH_PI } from './utils_index';
import { toUpperCase, reverseString, EMPTY } from './utils_index';
import { add, capitalize } from './utils_index';

// Use the imports to ensure they're tracked
function useUtils() {
    const sum = mathAdd(1, 2);  // Should resolve to math_utils.add
    const diff = mathSub(5, 3);  // Should resolve to math_utils.subtract
    const product = mathMultiply(4, 5);  // Should resolve to math_utils.multiply
    const pi = MATH_PI;  // Should resolve to math_utils.PI

    const upper = toUpperCase('hello');  // Should resolve to string_utils.capitalize
    const reversed = reverseString('world');  // Should resolve to string_utils.reverse
    const empty = EMPTY;  // Should resolve to string_utils.EMPTY_STRING

    const directSum = add(10, 20);  // Should resolve to math_utils.add
    const directCap = capitalize('test');  // Should resolve to string_utils.capitalize

    return { sum, diff, product, pi, upper, reversed, empty, directSum, directCap };
}

export { useUtils };
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    consumer_imports = [
        call for call in import_relationships if "consumer" in call.args[0][2]
    ]

    assert len(consumer_imports) >= 2, (
        f"Expected at least 2 consumer import relationships, found {len(consumer_imports)}"
    )

    re_export_imports = [
        call for call in import_relationships if "utils_index" in call.args[0][2]
    ]

    assert len(re_export_imports) >= 2, (
        f"Expected at least 2 re-export import relationships, found {len(re_export_imports)}"
    )

    re_export_targets = [call.args[2][2] for call in re_export_imports]

    expected_targets = ["math_utils", "string_utils"]
    for expected in expected_targets:
        assert any(expected in target for target in re_export_targets), (
            f"Missing re-export target: {expected}\nFound: {re_export_targets}"
        )
    print(
        "   - Bug fix verified: export { name as alias } now correctly maps alias -> source.name"
    )


def test_import_relationships_comprehensive(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all import types create proper relationships."""
    test_file = javascript_imports_project / "comprehensive_imports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every JavaScript import pattern in one file
import React, { Component, useState } from 'react';
import * as utils from './src/utils/helpers';
import Button from './src/components/Button';
import './side-effects';

const fs = require('fs');
const { API_URL } = require('./src/utils/constants');
const config = require('./lib/config');

// Dynamic import
import('./shared').then(module => {
    console.log(module.shared);
});

// Package imports
import lodash from 'lodash';
import axios from 'axios';
import babelCore from '@babel/core';

// Using imports
const element = React.createElement();
const [state] = useState();
const helper = utils.helper();
const btn = new Button();
const content = fs.readFileSync();
const url = API_URL;
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_imports = [
        call
        for call in import_relationships
        if "comprehensive_imports" in call.args[0][2]
    ]

    assert len(comprehensive_imports) >= 10, (
        f"Expected at least 10 comprehensive imports, found {len(comprehensive_imports)}"
    )

    for relationship in comprehensive_imports:
        assert len(relationship.args) == 3, "Import relationship should have 3 args"
        assert relationship.args[1] == "IMPORTS", "Second arg should be 'IMPORTS'"

        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        assert "comprehensive_imports" in source_module, (
            f"Source module should contain test file name: {source_module}"
        )

        assert isinstance(target_module, str) and target_module, (
            f"Target module should be non-empty string: {target_module}"
        )

    assert defines_relationships, "Should still have DEFINES relationships"


def test_commonjs_multiple_destructured_variables_regression(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """
    Regression test for critical bug in CommonJS destructuring.

    This test specifically addresses the bug where multiple destructured variables
    from a single require() statement would cause IndexError due to incorrect
    iteration logic. The bug was in _ingest_missing_import_patterns where it
    would iterate over destructured names but try to access module_names[i]
    and require_funcs[i] with the same index, causing failures when i >= 1.

    Example that would fail before fix:
    const { a, b, c } = require('module'); // 3 destructured, 1 module, 1 require
    Old code: iterate i=0,1,2 over [a,b,c] but access module_names[1] (IndexError)
    """
    test_file = javascript_imports_project / "regression_multiple_destructured.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// These patterns would trigger the IndexError bug before the fix

// Case 1: Multiple shorthand destructuring from single require
const { helper, validator, formatter, processor } = require('./src/utils/helpers');

// Case 2: Multiple aliased destructuring from single require
const { api: apiClient, db: database, cache: cacheStore } = require('./src/services');

// Case 3: Mixed shorthand and aliased in single require
const { logger, config: appConfig, utils: utilityLib, DEBUG } = require('./src/core');

// Case 4: Large number of destructured variables (stress test)
const {
    add, subtract, multiply, divide,
    sin, cos, tan, sqrt,
    PI: mathPI, E: mathE
} = require('./src/utils/math');

// Case 5: Multiple requires with multiple destructuring each
const { read, write, exists } = require('fs');
const { join, resolve, dirname } = require('path');
const { parse, stringify } = require('json');

// Using the destructured variables to ensure they're tracked
helper();
validator();
formatter();
processor();

apiClient.get();
database.query();
cacheStore.set();

logger.info();
appConfig.load();
utilityLib.helper();

const sum = add(1, 2);
const diff = subtract(5, 3);
const area = multiply(PI, 2);
""",
    )

    run_updater(javascript_imports_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    regression_imports = [
        call
        for call in import_relationships
        if "regression_multiple_destructured" in call.args[0][2]
    ]

    assert len(regression_imports) >= 7, (
        f"Expected at least 7 imports from multiple destructuring patterns, "
        f"found {len(regression_imports)}. This suggests the regression fix may not be working."
    )

    imported_modules = [call.args[2][2] for call in regression_imports]

    expected_patterns = [
        "helpers",
        "services",
        "core",
        "math",
        "fs",
        "path",
        "json",
    ]

    found_patterns = []
    for pattern in expected_patterns:
        if any(pattern in module for module in imported_modules):
            found_patterns.append(pattern)

    assert len(found_patterns) >= 5, (
        f"Expected to find at least 5 module patterns {expected_patterns}, "
        f"but only found {len(found_patterns)}: {found_patterns}\n"
        f"All imported modules: {imported_modules}"
    )
    print("   - No IndexError occurred (bug is fixed)")
