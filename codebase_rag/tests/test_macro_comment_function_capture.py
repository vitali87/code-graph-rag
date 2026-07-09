from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_node_names, run_updater

BISON_STYLE_MACRO = (
    "#define FAIL(loc, msg)   \\\n"
    "  do {                   \\\n"
    "    handle(loc, msg);    \\\n"
    "    /*YYERROR*/;         \\\n"
    "  } while (0)\n"
    "\n"
    "void yyerror(int* loc, const char* msg) { }\n"
)


@pytest.fixture
def c_macro_comment_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "c_macro_comment_project"
    project_path.mkdir()
    (project_path / "Makefile").write_text("all:\n\tgcc -o parser parser.c\n")
    (project_path / "parser.c").write_text(BISON_STYLE_MACRO)
    return project_path


@pytest.fixture
def cpp_macro_comment_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "cpp_macro_comment_project"
    project_path.mkdir()
    (project_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(parser)\n"
    )
    (project_path / "parser.cpp").write_text(BISON_STYLE_MACRO)
    return project_path


def test_c_function_after_comment_bearing_macro_is_ingested(
    c_macro_comment_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    run_updater(c_macro_comment_project, mock_ingestor, skip_if_missing="c")
    func_names = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
    assert any("yyerror" in name for name in func_names)


def test_cpp_function_after_comment_bearing_macro_is_ingested(
    cpp_macro_comment_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    run_updater(cpp_macro_comment_project, mock_ingestor, skip_if_missing="cpp")
    func_names = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
    assert any("yyerror" in name for name in func_names)
