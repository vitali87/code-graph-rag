"""
Comprehensive JavaScript import parsing and relationship testing.
Tests all possible JavaScript import patterns and verifies IMPORTS relationships.
"""

import os
import sys
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def javascript_imports_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with all import patterns."""
    project_path = temp_repo / "javascript_imports_test"
    project_path.mkdir()

    # Create package structure
    (project_path / "src").mkdir()
    (project_path / "src" / "components").mkdir()
    (project_path / "src" / "utils").mkdir()
    (project_path / "lib").mkdir()
    (project_path / "node_modules").mkdir()
    (project_path / "node_modules" / "react").mkdir()
    (project_path / "node_modules" / "@babel").mkdir()
    (project_path / "node_modules" / "@babel" / "core").mkdir()

    # Create module files for testing
    (project_path / "src" / "utils" / "helpers.js").write_text(
        "export const helper = () => {};"
    )
    (project_path / "src" / "utils" / "constants.js").write_text(
        "export const API_URL = 'https://api.example.com';"
    )
    (project_path / "src" / "utils" / "math.js").write_text(
        "export function add(a, b) { return a + b; }"
    )
    (project_path / "src" / "components" / "Button.js").write_text(
        "export default class Button {}"
    )
    (project_path / "lib" / "config.js").write_text(
        "module.exports = { apiKey: 'secret' };"
    )
    (project_path / "shared.js").write_text("export const shared = 'data';")

    # Create package.json and node_modules structure
    (project_path / "package.json").write_text(
        '{"name": "test-project", "version": "1.0.0"}'
    )
    (project_path / "node_modules" / "react" / "index.js").write_text(
        "export default {};"
    )
    (project_path / "node_modules" / "@babel" / "core" / "index.js").write_text(
        "export const transform = () => {};"
    )

    return project_path


def test_es6_default_imports(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test ES6 default import parsing and relationship creation."""
    test_file = javascript_imports_project / "es6_default_imports.js"
    test_file.write_text(
        """
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
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_imports_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    import_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "IMPORTS"
    ]

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
        """
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
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_imports_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    import_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "IMPORTS"
    ]

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
        """
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
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_imports_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    import_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "IMPORTS"
    ]

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
        """
// CommonJS require imports
const fs = require('fs');
const path = require('path');
const config = require('./lib/config');
const utils = require('./src/utils/helpers');

// Destructured require
const { helper } = require('./src/utils/helpers');
const { API_URL } = require('./src/utils/constants');

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
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_imports_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    import_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "IMPORTS"
    ]

    commonjs_imports = [
        call for call in import_relationships if "commonjs_imports" in call.args[0][2]
    ]

    assert len(commonjs_imports) >= 6, (
        f"Expected at least 6 CommonJS imports, found {len(commonjs_imports)}"
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


def test_relative_path_resolution(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test relative import path resolution (./ and ../)."""
    # Create a nested test file
    nested_dir = javascript_imports_project / "src" / "components" / "forms"
    nested_dir.mkdir()
    test_file = nested_dir / "Input.js"
    test_file.write_text(
        """
// Same directory relative imports
import Button from './Button';
import Modal from './Modal';

// Parent directory relative imports
import utils from '../utils/helpers';
import constants from '../utils/constants';

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
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_imports_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    import_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "IMPORTS"
    ]

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
        f"{project_name}.lib.config",
        f"{project_name}.shared",
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
        """
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
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_imports_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    import_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "IMPORTS"
    ]

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
        """
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
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_imports_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Dynamic imports might be tracked as function calls to import()
    call_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

    dynamic_calls = [
        call for call in call_relationships if "dynamic_imports" in call.args[0][2]
    ]

    # Should have some calls tracked (at minimum, the import() calls)
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
        """
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
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_imports_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    import_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "IMPORTS"
    ]

    mixed_imports = [
        call for call in import_relationships if "mixed_imports" in call.args[0][2]
    ]

    assert len(mixed_imports) >= 6, (
        f"Expected at least 6 mixed imports, found {len(mixed_imports)}"
    )

    imported_modules = [call.args[2][2] for call in mixed_imports]

    # Should include both ES6 and CommonJS imports
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
        """
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
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_imports_project,
        parsers=parsers,
        queries=queries,
    )

    # Should not raise an exception
    updater.run()

    import_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "IMPORTS"
    ]

    error_file_imports = [
        call for call in import_relationships if "error_imports" in call.args[0][2]
    ]

    # Should still parse valid imports despite errors
    assert len(error_file_imports) >= 3, (
        f"Expected at least 3 valid imports despite errors, found {len(error_file_imports)}"
    )

    imported_modules = [call.args[2][2] for call in error_file_imports]
    expected_valid = ["react", "fs", "path"]

    for expected in expected_valid:
        assert any(expected in module for module in imported_modules), (
            f"Missing valid import after error: {expected}"
        )


def test_import_relationships_comprehensive(
    javascript_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all import types create proper relationships."""
    test_file = javascript_imports_project / "comprehensive_imports.js"
    test_file.write_text(
        """
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
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_imports_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Verify all relationship types exist
    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    import_relationships = [c for c in all_relationships if c.args[1] == "IMPORTS"]
    defines_relationships = [c for c in all_relationships if c.args[1] == "DEFINES"]

    # Should have comprehensive import coverage
    comprehensive_imports = [
        call
        for call in import_relationships
        if "comprehensive_imports" in call.args[0][2]
    ]

    assert len(comprehensive_imports) >= 10, (
        f"Expected at least 10 comprehensive imports, found {len(comprehensive_imports)}"
    )

    # Verify relationship structure
    for relationship in comprehensive_imports:
        assert len(relationship.args) == 3, "Import relationship should have 3 args"
        assert relationship.args[1] == "IMPORTS", "Second arg should be 'IMPORTS'"

        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        # Source should be our test module
        assert "comprehensive_imports" in source_module, (
            f"Source module should contain test file name: {source_module}"
        )

        # Target should be a valid module name
        assert isinstance(target_module, str) and target_module, (
            f"Target module should be non-empty string: {target_module}"
        )

    # Test that import parsing doesn't interfere with other relationships
    assert defines_relationships, "Should still have DEFINES relationships"

    print("✅ JavaScript import relationship validation passed:")
    print(f"   - IMPORTS relationships: {len(import_relationships)}")
    print(f"   - DEFINES relationships: {len(defines_relationships)}")
    print(f"   - Comprehensive test imports: {len(comprehensive_imports)}")
