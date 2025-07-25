#!/usr/bin/env python3
"""
Research script to understand tree-sitter AST structures for import statements
across different programming languages.
"""
# type: ignore

import os
import sys
from collections.abc import Callable
from typing import Any

from tree_sitter import Language, Node, Parser

# Add the parent directory to path for imports if needed
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Import language parsers
try:
    from tree_sitter_go import language as go_lang
    from tree_sitter_java import language as java_lang
    from tree_sitter_javascript import language as js_lang
    from tree_sitter_rust import language as rust_lang
    from tree_sitter_typescript import language_typescript as ts_lang
except ImportError as e:
    print(f"Some languages not available: {e}")


def analyze_imports(
    language_name: str,
    language_lib: Callable[[], Any],
    code_samples: list[tuple[str, str]],
) -> None:
    """Analyze import AST structures for a given language."""
    print(f"\n{'=' * 50}")
    print(f"ANALYZING {language_name.upper()} IMPORTS")
    print(f"{'=' * 50}")

    try:
        language = Language(language_lib())
        parser = Parser(language)

        for i, (description, code) in enumerate(code_samples):
            print(f"\n--- Sample {i + 1}: {description} ---")
            print(f"Code: {code}")

            tree = parser.parse(bytes(code, "utf8"))

            def print_tree(node: Node, depth: int = 0) -> None:
                indent = "  " * depth
                node_text = node.text.decode("utf8").replace("\n", "\\n")
                print(f"{indent}{node.type}: '{node_text}'")
                for child in node.children:
                    print_tree(child, depth + 1)

            print("AST:")
            print_tree(tree.root_node)

    except Exception as e:
        print(f"Error analyzing {language_name}: {e}")


# JavaScript/TypeScript samples
js_samples = [
    ("Named import", "import { func1, func2 } from './module';"),
    ("Default import", "import React from 'react';"),
    ("Namespace import", "import * as utils from './utils';"),
    ("Mixed import", "import React, { useState } from 'react';"),
    ("Require (CommonJS)", "const fs = require('fs');"),
]

# Java samples
java_samples = [
    ("Simple import", "import java.util.List;"),
    ("Wildcard import", "import java.util.*;"),
    ("Static import", "import static java.lang.Math.PI;"),
]

# Rust samples
rust_samples = [
    ("Simple use", "use std::collections::HashMap;"),
    ("Multiple use", "use std::{fs, io};"),
    ("Glob use", "use crate::utils::*;"),
    ("Aliased use", "use std::collections::HashMap as Map;"),
]

# Go samples
go_samples = [
    ("Simple import", 'import "fmt"'),
    ("Multiple imports", 'import (\n    "fmt"\n    "os"\n)'),
    ("Aliased import", 'import f "fmt"'),
]

if __name__ == "__main__":
    # Analyze each language
    try:
        analyze_imports("JavaScript", js_lang, js_samples)
    except Exception:
        print("JavaScript not available")

    try:
        analyze_imports("TypeScript", ts_lang, js_samples)  # TS uses same syntax
    except Exception:
        print("TypeScript not available")

    try:
        analyze_imports("Java", java_lang, java_samples)
    except Exception:
        print("Java not available")

    try:
        analyze_imports("Rust", rust_lang, rust_samples)
    except Exception:
        print("Rust not available")

    try:
        analyze_imports("Go", go_lang, go_samples)
    except Exception:
        print("Go not available")
