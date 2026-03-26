from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


def index_project(ingestor: MemgraphIngestor, project_path: Path) -> None:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run()


def get_imports_relationships(ingestor: MemgraphIngestor) -> list[dict]:
    query = """
    MATCH (from:Module)-[r:IMPORTS]->(to:Module)
    RETURN from.qualified_name AS from_qn, to.qualified_name AS to_qn
    """
    return ingestor.fetch_all(query)


def get_module_qualified_names(ingestor: MemgraphIngestor) -> set[str]:
    query = "MATCH (m:Module) RETURN m.qualified_name AS qn"
    results = ingestor.fetch_all(query)
    return {r["qn"] for r in results}


JAVA_UTILS_CODE = """\
package utils;

public class StringUtils {
    public static String capitalize(String str) {
        return str.substring(0, 1).toUpperCase() + str.substring(1);
    }
}
"""

JAVA_MAIN_CODE = """\
package main;

import utils.StringUtils;
import java.util.List;
import java.util.ArrayList;

public class Main {
    public void run() {
        String name = StringUtils.capitalize("hello");
        List<String> items = new ArrayList<>();
    }
}
"""

PYTHON_UTILS_CODE = """\
def helper():
    return 42
"""

PYTHON_MAIN_CODE = """\
import os
import json
from pathlib import Path
from utils import helper

def main():
    helper()
    path = Path(".")
    os.getcwd()
"""

JS_UTILS_CODE = """\
export function capitalize(str) {
    return str.charAt(0).toUpperCase() + str.slice(1);
}
"""

JS_MAIN_CODE = """\
import { capitalize } from './utils';
import lodash from 'lodash';

export function run() {
    const name = capitalize('hello');
    return lodash.trim(name);
}
"""

TS_UTILS_CODE = """\
export function helper(): number {
    return 42;
}
"""

TS_MAIN_CODE = """\
import { helper } from './utils';
import * as fs from 'fs';

export function main(): void {
    helper();
    fs.readFileSync('test.txt');
}
"""

RUST_LIB_CODE = """\
pub mod utils;

pub fn lib_function() -> i32 {
    42
}
"""

RUST_UTILS_CODE = """\
pub fn helper() -> i32 {
    100
}
"""

RUST_MAIN_CODE = """\
use std::collections::HashMap;
use crate::utils::helper;

fn main() {
    let map: HashMap<String, i32> = HashMap::new();
    helper();
}
"""

GO_UTILS_CODE = """\
package utils

func Helper() int {
    return 42
}
"""

GO_MAIN_CODE = """\
package main

import (
    "fmt"
    "myproject/utils"
)

func main() {
    fmt.Println(utils.Helper())
}
"""

CPP_UTILS_HEADER = """\
#pragma once

int helper();
"""

CPP_UTILS_SOURCE = """\
#include "utils.h"

int helper() {
    return 42;
}
"""

CPP_MAIN_CODE = """\
#include <iostream>
#include <vector>
#include "utils.h"

int main() {
    std::vector<int> nums;
    nums.push_back(helper());
    std::cout << nums[0] << std::endl;
    return 0;
}
"""

LUA_UTILS_CODE = """\
local M = {}

function M.helper()
    return 42
end

return M
"""

LUA_MAIN_CODE = """\
local utils = require("utils")
local json = require("json")

local function main()
    local val = utils.helper()
    print(val)
end

main()
"""


@pytest.fixture
def java_imports_project(tmp_path: Path) -> Path:
    project = tmp_path / "java_imports_project"
    project.mkdir()
    (project / "utils").mkdir()
    (project / "main").mkdir()
    (project / "utils" / "StringUtils.java").write_text(
        JAVA_UTILS_CODE, encoding="utf-8"
    )
    (project / "main" / "Main.java").write_text(JAVA_MAIN_CODE, encoding="utf-8")
    return project


@pytest.fixture
def python_imports_project(tmp_path: Path) -> Path:
    project = tmp_path / "python_imports_project"
    project.mkdir()
    (project / "utils.py").write_text(PYTHON_UTILS_CODE, encoding="utf-8")
    (project / "main.py").write_text(PYTHON_MAIN_CODE, encoding="utf-8")
    return project


@pytest.fixture
def js_imports_project(tmp_path: Path) -> Path:
    project = tmp_path / "js_imports_project"
    project.mkdir()
    (project / "utils.js").write_text(JS_UTILS_CODE, encoding="utf-8")
    (project / "main.js").write_text(JS_MAIN_CODE, encoding="utf-8")
    return project


@pytest.fixture
def ts_imports_project(tmp_path: Path) -> Path:
    project = tmp_path / "ts_imports_project"
    project.mkdir()
    (project / "utils.ts").write_text(TS_UTILS_CODE, encoding="utf-8")
    (project / "main.ts").write_text(TS_MAIN_CODE, encoding="utf-8")
    return project


@pytest.fixture
def rust_imports_project(tmp_path: Path) -> Path:
    project = tmp_path / "rust_imports_project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "lib.rs").write_text(RUST_LIB_CODE, encoding="utf-8")
    (project / "src" / "utils.rs").write_text(RUST_UTILS_CODE, encoding="utf-8")
    (project / "src" / "main.rs").write_text(RUST_MAIN_CODE, encoding="utf-8")
    return project


@pytest.fixture
def go_imports_project(tmp_path: Path) -> Path:
    project = tmp_path / "go_imports_project"
    project.mkdir()
    (project / "utils").mkdir()
    (project / "utils" / "utils.go").write_text(GO_UTILS_CODE, encoding="utf-8")
    (project / "main.go").write_text(GO_MAIN_CODE, encoding="utf-8")
    return project


@pytest.fixture
def cpp_imports_project(tmp_path: Path) -> Path:
    project = tmp_path / "cpp_imports_project"
    project.mkdir()
    (project / "utils.h").write_text(CPP_UTILS_HEADER, encoding="utf-8")
    (project / "utils.cpp").write_text(CPP_UTILS_SOURCE, encoding="utf-8")
    (project / "main.cpp").write_text(CPP_MAIN_CODE, encoding="utf-8")
    return project


@pytest.fixture
def lua_imports_project(tmp_path: Path) -> Path:
    project = tmp_path / "lua_imports_project"
    project.mkdir()
    (project / "utils.lua").write_text(LUA_UTILS_CODE, encoding="utf-8")
    (project / "main.lua").write_text(LUA_MAIN_CODE, encoding="utf-8")
    return project


class TestJavaImportsRelationships:
    def test_internal_import_creates_relationship(
        self, memgraph_ingestor: MemgraphIngestor, java_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, java_imports_project)

        imports = get_imports_relationships(memgraph_ingestor)
        modules = get_module_qualified_names(memgraph_ingestor)

        project_name = java_imports_project.name
        main_module = f"{project_name}.main.Main"
        utils_module = f"{project_name}.utils.StringUtils"

        assert main_module in modules, f"Main module not found. Modules: {modules}"
        assert utils_module in modules, f"Utils module not found. Modules: {modules}"

        import_pairs = [(i["from_qn"], i["to_qn"]) for i in imports]
        main_imports_utils = any(
            from_qn == main_module and to_qn == utils_module
            for from_qn, to_qn in import_pairs
        )
        assert main_imports_utils, (
            f"Expected {main_module} -> {utils_module} relationship.\n"
            f"Found relationships: {import_pairs}\n"
            f"Available modules: {modules}"
        )

    def test_external_import_creates_module_node(
        self, memgraph_ingestor: MemgraphIngestor, java_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, java_imports_project)

        modules = get_module_qualified_names(memgraph_ingestor)

        external_module = "java.util"
        assert external_module in modules, (
            f"Expected external module node for '{external_module}'.\n"
            f"Available modules: {modules}"
        )

    def test_external_import_creates_relationship(
        self, memgraph_ingestor: MemgraphIngestor, java_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, java_imports_project)

        imports = get_imports_relationships(memgraph_ingestor)
        project_name = java_imports_project.name
        main_module = f"{project_name}.main.Main"

        import_pairs = [(i["from_qn"], i["to_qn"]) for i in imports]
        main_imports_external = any(
            from_qn == main_module and "java" in to_qn
            for from_qn, to_qn in import_pairs
        )

        assert main_imports_external, (
            f"Expected {main_module} -> java.util.* relationship.\n"
            f"Found relationships: {import_pairs}"
        )


class TestPythonImportsRelationships:
    def test_internal_import_creates_relationship(
        self, memgraph_ingestor: MemgraphIngestor, python_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, python_imports_project)

        imports = get_imports_relationships(memgraph_ingestor)
        modules = get_module_qualified_names(memgraph_ingestor)

        project_name = python_imports_project.name
        main_module = f"{project_name}.main"
        utils_module = f"{project_name}.utils"

        assert main_module in modules, f"Main module not found. Modules: {modules}"
        assert utils_module in modules, f"Utils module not found. Modules: {modules}"

        import_pairs = [(i["from_qn"], i["to_qn"]) for i in imports]
        main_imports_utils = any(
            from_qn == main_module and to_qn == utils_module
            for from_qn, to_qn in import_pairs
        )

        assert main_imports_utils, (
            f"Expected {main_module} -> {utils_module} relationship.\n"
            f"Found relationships: {import_pairs}\n"
            f"Available modules: {modules}"
        )

    def test_stdlib_import_creates_module_node(
        self, memgraph_ingestor: MemgraphIngestor, python_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, python_imports_project)

        modules = get_module_qualified_names(memgraph_ingestor)

        stdlib_modules = {"os", "json", "pathlib"}
        found_modules = {m for m in stdlib_modules if m in modules}

        assert found_modules == stdlib_modules, (
            f"Expected stdlib module nodes for {stdlib_modules}.\n"
            f"Found: {found_modules}\n"
            f"Missing: {stdlib_modules - found_modules}\n"
            f"Available modules: {modules}"
        )

    def test_stdlib_import_creates_relationship(
        self, memgraph_ingestor: MemgraphIngestor, python_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, python_imports_project)

        imports = get_imports_relationships(memgraph_ingestor)
        project_name = python_imports_project.name
        main_module = f"{project_name}.main"

        import_pairs = [(i["from_qn"], i["to_qn"]) for i in imports]
        main_imports_os = any(
            from_qn == main_module and to_qn == "os" for from_qn, to_qn in import_pairs
        )

        assert main_imports_os, (
            f"Expected {main_module} -> os relationship.\n"
            f"Found relationships: {import_pairs}"
        )


class TestJsImportsRelationships:
    def test_internal_import_creates_relationship(
        self, memgraph_ingestor: MemgraphIngestor, js_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, js_imports_project)

        imports = get_imports_relationships(memgraph_ingestor)
        modules = get_module_qualified_names(memgraph_ingestor)

        project_name = js_imports_project.name
        main_module = f"{project_name}.main"
        utils_module = f"{project_name}.utils"

        assert main_module in modules, f"Main module not found. Modules: {modules}"
        assert utils_module in modules, f"Utils module not found. Modules: {modules}"

        import_pairs = [(i["from_qn"], i["to_qn"]) for i in imports]
        main_imports_utils = any(
            from_qn == main_module and to_qn == utils_module
            for from_qn, to_qn in import_pairs
        )

        assert main_imports_utils, (
            f"Expected {main_module} -> {utils_module} relationship.\n"
            f"Found relationships: {import_pairs}\n"
            f"Available modules: {modules}"
        )

    def test_external_import_creates_module_node(
        self, memgraph_ingestor: MemgraphIngestor, js_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, js_imports_project)

        modules = get_module_qualified_names(memgraph_ingestor)

        external_modules = ["lodash", "lodash.default"]
        found_external = any(ext in modules for ext in external_modules)

        assert found_external, (
            f"Expected external module node for lodash.\nAvailable modules: {modules}"
        )


class TestTsImportsRelationships:
    def test_internal_import_creates_relationship(
        self, memgraph_ingestor: MemgraphIngestor, ts_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, ts_imports_project)

        imports = get_imports_relationships(memgraph_ingestor)
        modules = get_module_qualified_names(memgraph_ingestor)

        project_name = ts_imports_project.name
        main_module = f"{project_name}.main"
        utils_module = f"{project_name}.utils"

        assert main_module in modules, f"Main module not found. Modules: {modules}"
        assert utils_module in modules, f"Utils module not found. Modules: {modules}"

        import_pairs = [(i["from_qn"], i["to_qn"]) for i in imports]
        main_imports_utils = any(
            from_qn == main_module and to_qn == utils_module
            for from_qn, to_qn in import_pairs
        )

        assert main_imports_utils, (
            f"Expected {main_module} -> {utils_module} relationship.\n"
            f"Found relationships: {import_pairs}\n"
            f"Available modules: {modules}"
        )

    def test_external_import_creates_module_node(
        self, memgraph_ingestor: MemgraphIngestor, ts_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, ts_imports_project)

        modules = get_module_qualified_names(memgraph_ingestor)

        external_modules = ["fs"]
        found_external = any(ext in modules for ext in external_modules)

        assert found_external, (
            f"Expected external module node for fs.\nAvailable modules: {modules}"
        )


class TestRustImportsRelationships:
    def test_internal_import_creates_relationship(
        self, memgraph_ingestor: MemgraphIngestor, rust_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, rust_imports_project)

        imports = get_imports_relationships(memgraph_ingestor)
        modules = get_module_qualified_names(memgraph_ingestor)

        project_name = rust_imports_project.name
        main_module = f"{project_name}.src.main"
        utils_module = f"{project_name}.src.utils"

        assert main_module in modules, f"Main module not found. Modules: {modules}"
        assert utils_module in modules, f"Utils module not found. Modules: {modules}"

        import_pairs = [(i["from_qn"], i["to_qn"]) for i in imports]
        main_imports_utils = any(
            from_qn == main_module and to_qn == utils_module
            for from_qn, to_qn in import_pairs
        )

        assert main_imports_utils, (
            f"Expected {main_module} -> {utils_module} relationship.\n"
            f"Found relationships: {import_pairs}\n"
            f"Available modules: {modules}"
        )

    def test_external_import_creates_module_node(
        self, memgraph_ingestor: MemgraphIngestor, rust_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, rust_imports_project)

        modules = get_module_qualified_names(memgraph_ingestor)

        external_modules = ["std", "std::collections", "std::collections::HashMap"]
        found_external = any(ext in modules for ext in external_modules)

        assert found_external, (
            f"Expected external module node for std.\nAvailable modules: {modules}"
        )


class TestGoImportsRelationships:
    def test_internal_import_creates_relationship(
        self, memgraph_ingestor: MemgraphIngestor, go_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, go_imports_project)

        imports = get_imports_relationships(memgraph_ingestor)
        modules = get_module_qualified_names(memgraph_ingestor)

        project_name = go_imports_project.name
        main_module = f"{project_name}.main"
        utils_module = f"{project_name}.utils.utils"

        assert main_module in modules, f"Main module not found. Modules: {modules}"
        assert utils_module in modules, f"Utils module not found. Modules: {modules}"

        import_pairs = [(i["from_qn"], i["to_qn"]) for i in imports]
        main_imports_utils = any(
            from_qn == main_module and to_qn in {utils_module, "myproject/utils"}
            for from_qn, to_qn in import_pairs
        )

        assert main_imports_utils, (
            f"Expected {main_module} -> {utils_module} relationship.\n"
            f"Found relationships: {import_pairs}\n"
            f"Available modules: {modules}"
        )

    def test_external_import_creates_module_node(
        self, memgraph_ingestor: MemgraphIngestor, go_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, go_imports_project)

        modules = get_module_qualified_names(memgraph_ingestor)

        external_modules = ["fmt"]
        found_external = any(ext in modules for ext in external_modules)

        assert found_external, (
            f"Expected external module node for fmt.\nAvailable modules: {modules}"
        )


class TestCppImportsRelationships:
    def test_internal_include_creates_relationship(
        self, memgraph_ingestor: MemgraphIngestor, cpp_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, cpp_imports_project)

        imports = get_imports_relationships(memgraph_ingestor)
        modules = get_module_qualified_names(memgraph_ingestor)

        project_name = cpp_imports_project.name
        main_module = f"{project_name}.main"
        utils_header = f"{project_name}.utils"

        assert main_module in modules, f"Main module not found. Modules: {modules}"
        assert utils_header in modules, f"Utils header not found. Modules: {modules}"

        import_pairs = [(i["from_qn"], i["to_qn"]) for i in imports]
        main_includes_utils = any(
            from_qn == main_module and to_qn == utils_header
            for from_qn, to_qn in import_pairs
        )

        assert main_includes_utils, (
            f"Expected {main_module} -> {utils_header} relationship.\n"
            f"Found relationships: {import_pairs}\n"
            f"Available modules: {modules}"
        )

    def test_external_include_creates_module_node(
        self, memgraph_ingestor: MemgraphIngestor, cpp_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, cpp_imports_project)

        modules = get_module_qualified_names(memgraph_ingestor)

        external_modules = {"std.iostream", "std.vector"}
        found_modules = {m for m in external_modules if m in modules}

        assert found_modules == external_modules, (
            f"Expected external module nodes for {external_modules}.\n"
            f"Found: {found_modules}\n"
            f"Missing: {external_modules - found_modules}\n"
            f"Available modules: {modules}"
        )


class TestLuaImportsRelationships:
    def test_internal_require_creates_relationship(
        self, memgraph_ingestor: MemgraphIngestor, lua_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, lua_imports_project)

        imports = get_imports_relationships(memgraph_ingestor)
        modules = get_module_qualified_names(memgraph_ingestor)

        project_name = lua_imports_project.name
        main_module = f"{project_name}.main"
        utils_module = f"{project_name}.utils"

        assert main_module in modules, f"Main module not found. Modules: {modules}"
        assert utils_module in modules, f"Utils module not found. Modules: {modules}"

        import_pairs = [(i["from_qn"], i["to_qn"]) for i in imports]
        main_requires_utils = any(
            from_qn == main_module and to_qn == utils_module
            for from_qn, to_qn in import_pairs
        )

        assert main_requires_utils, (
            f"Expected {main_module} -> {utils_module} relationship.\n"
            f"Found relationships: {import_pairs}\n"
            f"Available modules: {modules}"
        )

    def test_external_require_creates_module_node(
        self, memgraph_ingestor: MemgraphIngestor, lua_imports_project: Path
    ) -> None:
        index_project(memgraph_ingestor, lua_imports_project)

        modules = get_module_qualified_names(memgraph_ingestor)

        external_module = "json"
        assert external_module in modules, (
            f"Expected external module node for '{external_module}'.\n"
            f"Available modules: {modules}"
        )
