from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    get_node_names,
    get_relationships,
    run_updater,
)


@pytest.fixture
def cpp_single_file(temp_repo: Path) -> Path:
    test_file = temp_repo / "cmGlobalFastbuildGenerator.cxx"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <map>
#include <set>
#include <string>

static std::map<std::string, std::string> const compilerIdToFastbuildFamily = {
    {"GNU", "gcc"},
    {"Clang", "clang"},
};

static std::set<std::string> const supportedLanguages = {
    "C",
    "CXX",
};

template <class T>
T generateAlias(std::string const& name) { return T(); }

static void helperFunc() {}

class FastbuildTarget {
public:
    void GenerateAliases();
};

void FastbuildTarget::GenerateAliases() {
    auto alias = generateAlias("test");
}

void freeFunction() {
    helperFunc();
}
""",
    )
    return test_file


@pytest.fixture
def ran_single_file_updater(cpp_single_file: Path, mock_ingestor: MagicMock) -> None:
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_single_file,
        parsers=parsers,
        queries=queries,
    )
    updater.run()


def test_single_file_repo_path_produces_graph(
    ran_single_file_updater: None,
    mock_ingestor: MagicMock,
) -> None:
    functions = get_node_names(mock_ingestor, "Function")
    methods = get_node_names(mock_ingestor, "Method")
    classes = get_node_names(mock_ingestor, "Class")

    assert any("generateAlias" in qn for qn in functions)
    assert any("helperFunc" in qn for qn in functions)
    assert any("freeFunction" in qn for qn in functions)

    assert any("GenerateAliases" in qn for qn in methods)
    assert any("FastbuildTarget" in qn for qn in classes)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")
    assert len(defines_rels) >= 3

    calls_rels = get_relationships(mock_ingestor, "CALLS")
    assert len(calls_rels) >= 1


def test_single_file_repo_path_static_functions(
    ran_single_file_updater: None,
    mock_ingestor: MagicMock,
) -> None:
    functions = get_node_names(mock_ingestor, "Function")

    assert any("helperFunc" in qn for qn in functions), (
        f"Static function helperFunc not found. Functions: {functions}"
    )

    assert any("generateAlias" in qn for qn in functions), (
        f"Template function generateAlias not found. Functions: {functions}"
    )


def test_single_file_repo_path_out_of_class_methods(
    ran_single_file_updater: None,
    mock_ingestor: MagicMock,
) -> None:
    methods = get_node_names(mock_ingestor, "Method")
    defines_method_rels = get_relationships(mock_ingestor, "DEFINES_METHOD")

    assert any("GenerateAliases" in qn for qn in methods), (
        f"Out-of-class method GenerateAliases not found. Methods: {methods}"
    )
    assert len(defines_method_rels) >= 1


def test_directory_repo_path_still_works(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    project = temp_repo / "normal_project"
    project.mkdir()
    (project / "main.cpp").write_text(
        encoding="utf-8",
        data="""
void doStuff() {}
int main() { doStuff(); return 0; }
""",
    )

    run_updater(project, mock_ingestor)

    functions = get_node_names(mock_ingestor, "Function")
    assert any("doStuff" in qn for qn in functions)
    assert any("main" in qn for qn in functions)
