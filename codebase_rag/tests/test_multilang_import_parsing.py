#!/usr/bin/env python3
"""
Test script to verify import parsing works for all supported languages.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Add the parent directory to path for imports
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def test_javascript_import_parsing() -> None:
    """Test JavaScript import parsing."""
    test_code = """
import { func1, func2 } from './utils';
import React from 'react';
import * as helpers from './helpers';
const fs = require('fs');

function main() {
    func1();
    React.createElement();
    helpers.doSomething();
    fs.readFile();
}
"""
    with tempfile.TemporaryDirectory() as temp_dir:
        test_file = Path(temp_dir) / "test.js"
        test_file.write_text(test_code)

        parsers, queries = load_parsers()
        assert "javascript" in parsers, "JavaScript parser not available"

        mock_ingestor = MagicMock()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=Path(temp_dir),
            parsers=parsers,
            queries=queries,
        )

        updater.run()

        project_name = Path(temp_dir).name
        test_module = f"{project_name}.test"

        assert test_module in updater.factory.import_processor.import_mapping, (
            f"No import mapping for {test_module}"
        )
        actual_imports = updater.factory.import_processor.import_mapping[test_module]

        expected = {
            "func1": f"{project_name}.utils.func1",
            "func2": f"{project_name}.utils.func2",
            "React": "react.default",
            "helpers": f"{project_name}.helpers",
            "fs": "fs",
        }

        for name, path in expected.items():
            assert name in actual_imports, f"Missing import: {name}"
            assert actual_imports[name] == path, (
                f"Wrong path for {name}: expected {path}, got {actual_imports[name]}"
            )


def test_java_import_parsing() -> None:
    """Test Java import parsing."""
    test_code = """
import java.util.List;
import java.util.*;
import static java.lang.Math.PI;

public class Test {
    public void main() {
        List<String> list = new ArrayList<>();
        System.out.println(PI);
    }
}
"""
    with tempfile.TemporaryDirectory() as temp_dir:
        test_file = Path(temp_dir) / "test.java"
        test_file.write_text(test_code)

        parsers, queries = load_parsers()
        if "java" not in parsers:
            return  # Skip if Java parser not available

        mock_ingestor = MagicMock()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=Path(temp_dir),
            parsers=parsers,
            queries=queries,
        )

        updater.run()

        project_name = Path(temp_dir).name
        test_module = f"{project_name}.test"

        assert test_module in updater.factory.import_processor.import_mapping, (
            f"No import mapping for {test_module}"
        )
        actual_imports = updater.factory.import_processor.import_mapping[test_module]

        expected = {
            "List": "java.util.List",
            "PI": "java.lang.Math.PI",
            "*java.util": "java.util",
        }

        for name, path in expected.items():
            assert name in actual_imports, f"Missing import: {name}"
            assert actual_imports[name] == path, (
                f"Wrong path for {name}: expected {path}, got {actual_imports[name]}"
            )


def test_rust_import_parsing() -> None:
    """Test Rust import parsing."""
    test_code = """
use std::collections::HashMap;
use std::{fs, io};
use crate::utils::*;
use std::collections::HashMap as Map;

fn main() {
    let mut map = HashMap::new();
    let file = fs::File::open("test.txt");
    let mut other_map = Map::new();
}
"""
    with tempfile.TemporaryDirectory() as temp_dir:
        test_file = Path(temp_dir) / "test.rs"
        test_file.write_text(test_code)

        parsers, queries = load_parsers()
        if "rust" not in parsers:
            return  # Skip if Rust parser not available

        mock_ingestor = MagicMock()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=Path(temp_dir),
            parsers=parsers,
            queries=queries,
        )

        updater.run()

        project_name = Path(temp_dir).name
        test_module = f"{project_name}.test"

        assert test_module in updater.factory.import_processor.import_mapping, (
            f"No import mapping for {test_module}"
        )
        actual_imports = updater.factory.import_processor.import_mapping[test_module]

        expected = {
            "HashMap": "std::collections::HashMap",
            "fs": "std::fs",
            "io": "std::io",
            "*crate::utils": "crate::utils",
            "Map": "std::collections::HashMap",
        }

        for name, path in expected.items():
            assert name in actual_imports, f"Missing import: {name}"
            assert actual_imports[name] == path, (
                f"Wrong path for {name}: expected {path}, got {actual_imports[name]}"
            )


def test_go_import_parsing() -> None:
    """Test Go import parsing."""
    test_code = """
package main

import "fmt"
import (
    "os"
    f "fmt"
)

func main() {
    fmt.Println("Hello")
    os.Exit(0)
    f.Printf("Test")
}
"""
    with tempfile.TemporaryDirectory() as temp_dir:
        test_file = Path(temp_dir) / "test.go"
        test_file.write_text(test_code)

        parsers, queries = load_parsers()
        if "go" not in parsers:
            return  # Skip if Go parser not available

        mock_ingestor = MagicMock()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=Path(temp_dir),
            parsers=parsers,
            queries=queries,
        )

        updater.run()

        project_name = Path(temp_dir).name
        test_module = f"{project_name}.test"

        assert test_module in updater.factory.import_processor.import_mapping, (
            f"No import mapping for {test_module}"
        )
        actual_imports = updater.factory.import_processor.import_mapping[test_module]

        expected = {"fmt": "fmt", "os": "os", "f": "fmt"}

        for name, path in expected.items():
            assert name in actual_imports, f"Missing import: {name}"
            assert actual_imports[name] == path, (
                f"Wrong path for {name}: expected {path}, got {actual_imports[name]}"
            )
