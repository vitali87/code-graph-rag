"""Issue #2744: `surgical_replace_code` changes the target block and nothing else.

The file was read with universal newlines and written back with `\\n`, so a
one-line edit to a CRLF file rewrote every line ending. A target that occurs
more than once was replaced at its first match while the tool reported
success; the only hint was a server-side log line no client sees.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.mcp.tools import MCPToolsRegistry, _plain_function
from codebase_rag.tools.file_editor import FileEditor, create_file_editor_tool

pytestmark = [pytest.mark.anyio]

CRLF = b"def a():\r\n    return 1\r\n\r\n\r\ndef b():\r\n    return 1\r\n"
DUP = b"def x():\n    total = 0\n    return total\n\n\ndef y():\n    total = 0\n    return total\n"


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _editor(root: Path, files: dict[str, bytes]) -> FileEditor:
    for name, data in files.items():
        (root / name).write_bytes(data)
    return FileEditor(str(root))


def test_a_crlf_file_keeps_its_line_endings(tmp_path: Path) -> None:
    editor = _editor(tmp_path, {"crlf.py": CRLF})

    ok = editor.replace_code_block(
        "crlf.py", "def b():\n    return 1", "def b():\n    return 2"
    )

    assert ok is True
    assert (tmp_path / "crlf.py").read_bytes() == CRLF.replace(
        b"b():\r\n    return 1", b"b():\r\n    return 2"
    )


def test_new_lines_in_a_crlf_file_are_written_with_crlf(tmp_path: Path) -> None:
    editor = _editor(tmp_path, {"crlf.py": CRLF})

    editor.replace_code_block(
        "crlf.py", "def b():\n    return 1", "def b():\n    value = 2\n    return value"
    )

    assert (tmp_path / "crlf.py").read_bytes() == (
        b"def a():\r\n    return 1\r\n\r\n\r\n"
        b"def b():\r\n    value = 2\r\n    return value\r\n"
    )


def test_a_target_copied_with_crlf_matches_a_crlf_file(tmp_path: Path) -> None:
    editor = _editor(tmp_path, {"crlf.py": CRLF})

    ok = editor.replace_code_block(
        "crlf.py", "def b():\r\n    return 1", "def b():\r\n    return 2"
    )

    assert ok is True
    assert (tmp_path / "crlf.py").read_bytes().count(b"\r\n") == CRLF.count(b"\r\n")


def test_lines_outside_the_target_keep_their_own_endings(tmp_path: Path) -> None:
    mixed = b"a = 1\nb = 2\r\nc = 3\n"
    editor = _editor(tmp_path, {"mixed.py": mixed})

    editor.replace_code_block("mixed.py", "a = 1", "a = 10")

    assert (tmp_path / "mixed.py").read_bytes() == b"a = 10\nb = 2\r\nc = 3\n"


def test_an_ambiguous_target_is_refused_and_the_file_is_untouched(
    tmp_path: Path,
) -> None:
    editor = _editor(tmp_path, {"dup.py": DUP})

    ok = editor.replace_code_block("dup.py", "    total = 0", "    total = 10")

    assert ok is False
    assert (tmp_path / "dup.py").read_bytes() == DUP


def test_overlapping_matches_are_ambiguous(tmp_path: Path) -> None:
    repeated = b"x = 1\nx = 1\nx = 1\n"
    editor = _editor(tmp_path, {"rep.py": repeated})

    ok = editor.replace_code_block("rep.py", "x = 1\nx = 1", "x = 2")

    assert ok is False
    assert (tmp_path / "rep.py").read_bytes() == repeated


async def test_the_tool_names_the_match_count_and_lines(tmp_path: Path) -> None:
    tool = _plain_function(create_file_editor_tool(_editor(tmp_path, {"dup.py": DUP})))

    result = await tool(
        file_path="dup.py", target_code="    total = 0", replacement_code="    x = 1"
    )

    assert result != cs.MSG_SURGICAL_SUCCESS.format(path="dup.py")
    assert "2 times" in result
    assert "lines 2, 7" in result


async def test_an_mcp_client_is_told_the_edit_was_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "dup.py").write_bytes(DUP)
    registry = MCPToolsRegistry(
        project_root=str(tmp_path), ingestor=MagicMock(), cypher_gen=MagicMock()
    )

    result = await registry.surgical_replace_code(
        file_path="dup.py", target_code="    total = 0", replacement_code="    x = 1"
    )

    assert not result.startswith(cs.MSG_SURGICAL_SUCCESS.format(path="dup.py"))
    assert "lines 2, 7" in result
    assert (tmp_path / "dup.py").read_bytes() == DUP


# Negative: what must not change.


def test_an_lf_file_keeps_lf_and_everything_but_the_target(tmp_path: Path) -> None:
    editor = _editor(tmp_path, {"dup.py": DUP})

    ok = editor.replace_code_block(
        "dup.py", "def y():\n    total = 0", "def y():\n    total = 5"
    )

    assert ok is True
    assert (tmp_path / "dup.py").read_bytes() == DUP.replace(
        b"y():\n    total = 0", b"y():\n    total = 5"
    )


def test_a_file_without_a_trailing_newline_keeps_none(tmp_path: Path) -> None:
    editor = _editor(tmp_path, {"t.py": b"a = 1\nb = 2"})

    editor.replace_code_block("t.py", "b = 2", "b = 3")

    assert (tmp_path / "t.py").read_bytes() == b"a = 1\nb = 3"


def test_a_replacement_that_contains_the_target_is_applied_once(
    tmp_path: Path,
) -> None:
    editor = _editor(tmp_path, {"t.py": b"a = 1\n"})

    ok = editor.replace_code_block("t.py", "a = 1", "a = 1\nb = 2")

    assert ok is True
    assert (tmp_path / "t.py").read_bytes() == b"a = 1\nb = 2\n"


def test_a_missing_target_is_still_refused(tmp_path: Path) -> None:
    editor = _editor(tmp_path, {"crlf.py": CRLF})

    assert editor.replace_code_block("crlf.py", "def c():", "def d():") is False
    assert (tmp_path / "crlf.py").read_bytes() == CRLF


async def test_a_unique_edit_still_reports_success(tmp_path: Path) -> None:
    tool = _plain_function(create_file_editor_tool(_editor(tmp_path, {"dup.py": DUP})))

    result = await tool(
        file_path="dup.py", target_code="def x():", replacement_code="def z():"
    )

    assert result == cs.MSG_SURGICAL_SUCCESS.format(path="dup.py")
