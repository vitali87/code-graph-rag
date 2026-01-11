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

        external_modules = ["java.util.List", "java.util.ArrayList", "java.util"]
        found_external = any(ext in modules for ext in external_modules)

        assert found_external, (
            f"Expected external module node for java.util imports.\n"
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

        stdlib_modules = ["os", "json", "pathlib"]
        found_stdlib = [m for m in stdlib_modules if m in modules]

        assert found_stdlib, (
            f"Expected stdlib module nodes for {stdlib_modules}.\n"
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
