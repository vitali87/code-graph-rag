"""`FileTools`, the MCP handlers over this server's files, driven on the
mixin itself with the real file, directory and structural tools. Pinned
directly: the paginated read (its header, an empty file, an offset past the
end, bounds checked before the disk is touched, a path outside the root), a
refusal staying a `ToolFailure`, and the structural delta a write appends
only when it wrote and the project is indexed (issue #1525)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import tool_errors as te
from codebase_rag.mcp import constants as mcs
from codebase_rag.mcp.file_tools import FileTools, _paginate, _read_file_slice
from codebase_rag.mcp.run_marker import RunMarker
from codebase_rag.tests.conftest import marker_store
from codebase_rag.tools.ast_grep_service import AstGrepService
from codebase_rag.tools.directory_lister import (
    DirectoryLister,
    create_directory_lister_tool,
)
from codebase_rag.tools.file_editor import FileEditor, create_file_editor_tool
from codebase_rag.tools.file_reader import FileReader, create_file_reader_tool
from codebase_rag.tools.file_writer import FileWriter, create_file_writer_tool
from codebase_rag.tools.structural_editor import create_structural_editor_tool
from codebase_rag.types_defs import ReingestReport, StructuralReplaceChange
from codebase_rag.utils.path_utils import derive_project_name

pytestmark = [pytest.mark.anyio]

TEN_LINES = "".join(f"line {n}\n" for n in range(1, 11))


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class _Updater:
    """A warm scoped updater: what a write re-ingests through."""

    def __init__(self, project_name: str) -> None:
        self.project_name = project_name
        self.reingest_mutated = False
        self.reingested: list[str] = []

    def reingest(
        self,
        paths: list[str],
        deleted: list[str] | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> ReingestReport:
        if before_write is not None:
            before_write()
        self.reingested.extend(paths)
        return ReingestReport(tuple(paths), (), (), (), 0.0)


def _tools(root: Path, store: MagicMock | None = None) -> FileTools:
    tools = FileTools()
    tools.project_root = str(root)
    tools.ingestor = store if store is not None else marker_store()
    if store is None:
        tools.ingestor.list_projects.return_value = []
    tools.workspace = None
    tools._ingestor_lock = asyncio.Lock()
    tools._live_updater = None
    tools.marker = RunMarker(lambda: tools.ingestor)
    editor = FileEditor(project_root=str(root))
    tools._file_editor_tool = create_file_editor_tool(file_editor=editor)
    tools._file_reader_tool = create_file_reader_tool(
        file_reader=FileReader(project_root=str(root))
    )
    tools._file_writer_tool = create_file_writer_tool(
        file_writer=FileWriter(project_root=str(root), write_lock=editor.write_lock)
    )
    tools._directory_lister_tool = create_directory_lister_tool(
        directory_lister=DirectoryLister(project_root=str(root))
    )
    tools._structural_written = []
    return tools


def _indexed(tools: FileTools, root: Path) -> _Updater:
    # A project with a warm updater: a write's delta is measured through it.
    updater = _Updater(derive_project_name(root))
    tools._live_updater = updater
    return updater


# --- the paginated read -------------------------------------------------------


def test_a_slice_carries_a_one_based_header(tmp_path: Path) -> None:
    path = tmp_path / "ten.txt"
    path.write_text(TEN_LINES, encoding="utf-8")

    assert _paginate(path, 2, 2, "ten.txt") == (
        mcs.MCP_PAGINATION_HEADER.format(start=3, end=4, total=10) + "line 3\nline 4\n"
    )
    assert _paginate(path, 8, None, "ten.txt") == (
        mcs.MCP_PAGINATION_HEADER.format(start=9, end=10, total=10)
        + "line 9\nline 10\n"
    )


def test_the_start_of_an_empty_file_is_a_page(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")

    assert _paginate(path, 0, 5, "empty.txt") == mcs.MCP_PAGINATION_HEADER.format(
        start=1, end=0, total=0
    )


@pytest.mark.parametrize("offset", [10, sys.maxsize + 1])
def test_an_offset_past_the_end_is_refused(tmp_path: Path, offset: int) -> None:
    # `islice` refuses a count past sys.maxsize; such an offset is still
    # only past the end.
    path = tmp_path / "ten.txt"
    path.write_text(TEN_LINES, encoding="utf-8")

    result = _paginate(path, offset, 1, "ten.txt")

    assert isinstance(result, te.ToolFailure)
    assert result == te.failure(
        mcs.MCP_READ_PAST_END.format(offset=offset, path="ten.txt", total=10)
    )


def test_a_slice_of_what_a_read_refuses_is_refused_the_same_way(
    tmp_path: Path,
) -> None:
    undecodable = tmp_path / "latin.txt"
    undecodable.write_bytes(b"caf\xe9\n")

    assert _read_file_slice(tmp_path / "gone.txt", 0, 1, "gone.txt") == te.failure(
        te.FILE_NOT_FOUND
    )
    assert _read_file_slice(undecodable, 0, 1, "latin.txt") == te.failure(
        te.UNICODE_DECODE.format(path=undecodable)
    )


@pytest.mark.parametrize(
    ("offset", "limit", "message"),
    [
        (-1, None, mcs.MCP_READ_OFFSET_NEGATIVE.format(offset=-1)),
        (None, 0, mcs.MCP_READ_LIMIT_NOT_POSITIVE.format(limit=0)),
    ],
)
async def test_read_bounds_are_checked_before_the_file(
    tmp_path: Path, offset: int | None, limit: int | None, message: str
) -> None:
    # The file does not exist: the bound is what is refused.
    result = await _tools(tmp_path).read_file("absent.txt", offset, limit)

    assert result == te.failure(message)


async def test_a_paginated_read_outside_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("key\n", encoding="utf-8")

    result = await _tools(root).read_file("../secret.txt", offset=0, limit=1)

    assert result == te.failure(
        te.PATH_OUTSIDE_ROOT.format(path="../secret.txt", root=root.resolve())
    )


async def test_read_file_pages_or_reads_whole(tmp_path: Path) -> None:
    (tmp_path / "ten.txt").write_text(TEN_LINES, encoding="utf-8")
    tools = _tools(tmp_path)

    assert await tools.read_file("ten.txt") == TEN_LINES
    assert await tools.read_file("ten.txt", limit=1) == (
        mcs.MCP_PAGINATION_HEADER.format(start=1, end=1, total=10) + "line 1\n"
    )


async def test_a_whole_read_refusal_stays_a_tool_failure(tmp_path: Path) -> None:
    # The server reports a ToolFailure with `isError` (issue #2650); a plain
    # string with the same text would read as a successful answer.
    result = await _tools(tmp_path).read_file("absent.txt")

    assert isinstance(result, te.ToolFailure)


# --- writes and their structural delta ---------------------------------------


async def test_a_write_to_an_unindexed_project_reports_no_delta(
    tmp_path: Path,
) -> None:
    result = await _tools(tmp_path).write_file("new.py", "x = 1\n")

    assert result == mcs.MCP_WRITE_SUCCESS.format(path="new.py")
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "x = 1\n"


async def test_a_write_appends_the_delta_of_what_it_reingested(
    tmp_path: Path,
) -> None:
    tools = _tools(tmp_path)
    updater = _indexed(tools, tmp_path)

    result = await tools.write_file("new.py", "x = 1\n")

    assert result.startswith(
        mcs.MCP_WRITE_SUCCESS.format(path="new.py")
        + "\n\n"
        + mcs.MCP_DELTA_HEADER
        + "\n"
    )
    assert updater.reingested == ["new.py"]


async def test_a_refused_write_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    tools = _tools(root)
    updater = _indexed(tools, root)

    result = await tools.write_file("../escape.py", "x = 1\n")

    assert isinstance(result, te.ToolFailure)
    assert not (tmp_path / "escape.py").exists()
    assert updater.reingested == []


async def test_a_surgical_replace_rewrites_only_its_target(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text("def f():\n    return 1\n\n\ndef g():\n    return 1\n")
    tools = _tools(tmp_path)
    updater = _indexed(tools, tmp_path)

    result = await tools.surgical_replace_code(
        "mod.py", "def g():\n    return 1\n", "def g():\n    return 2\n"
    )

    assert not isinstance(result, te.ToolFailure)
    assert mcs.MCP_DELTA_HEADER in result
    assert path.read_text(encoding="utf-8") == (
        "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
    )
    assert updater.reingested == ["mod.py"]


async def test_a_surgical_replace_of_absent_code_changes_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mod.py"
    path.write_text("def f():\n    return 1\n", encoding="utf-8")
    tools = _tools(tmp_path)
    updater = _indexed(tools, tmp_path)

    result = await tools.surgical_replace_code("mod.py", "def h():", "def k():")

    assert isinstance(result, te.ToolFailure)
    assert path.read_text(encoding="utf-8") == "def f():\n    return 1\n"
    assert updater.reingested == []


async def test_list_directory_names_the_entries(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "b.txt").write_text("", encoding="utf-8")

    listing = await _tools(tmp_path).list_directory("pkg")

    assert "a.py" in listing
    assert "b.txt" in listing


# --- structural replace --------------------------------------------------------


@pytest.fixture
def structural(tmp_path: Path) -> FileTools:
    pytest.importorskip("ast_grep_py")
    (tmp_path / "mod.py").write_text("print(1)\nprint(2)\n", encoding="utf-8")
    tools = _tools(tmp_path)
    tools._structural_editor_tool = create_structural_editor_tool(
        service=AstGrepService(project_root=str(tmp_path)),
        on_changes=tools._note_structural_changes,
    )
    return tools


def test_only_applied_changes_count_as_written(tmp_path: Path) -> None:
    tools = _tools(tmp_path)

    tools._note_structural_changes(
        [
            StructuralReplaceChange(file="a.py", matches=1, diff="", applied=True),
            StructuralReplaceChange(file="b.py", matches=1, diff="", applied=False),
        ]
    )

    assert tools._structural_written == ["a.py"]


async def test_a_structural_dry_run_reports_no_delta(
    structural: FileTools, tmp_path: Path
) -> None:
    updater = _indexed(structural, tmp_path)

    result = await structural.structural_replace(
        "print($A)", "log($A)", language="python", dry_run=True
    )

    assert mcs.MCP_DELTA_HEADER not in result
    assert "log(1)" in result
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "print(1)\nprint(2)\n"
    assert updater.reingested == []


async def test_a_structural_write_appends_the_delta_of_its_files(
    structural: FileTools, tmp_path: Path
) -> None:
    updater = _indexed(structural, tmp_path)

    result = await structural.structural_replace(
        "print($A)", "log($A)", language="python", dry_run=False
    )

    assert mcs.MCP_DELTA_HEADER in result
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "log(1)\nlog(2)\n"
    assert updater.reingested == ["mod.py"]


async def test_a_structural_write_matching_nothing_reingests_nothing(
    structural: FileTools, tmp_path: Path
) -> None:
    updater = _indexed(structural, tmp_path)
    structural._structural_written = ["stale.py"]

    result = await structural.structural_replace(
        "nothing_matches($A)", "x($A)", language="python", dry_run=False
    )

    assert mcs.MCP_DELTA_HEADER not in result
    assert updater.reingested == []
    assert structural._structural_written == []
