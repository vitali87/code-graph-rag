"""A Python file's declared encoding holds for every reader, not just the indexer.

#2445 taught the indexer to decode a source in the encoding its PEP 263
cookie declares, so `def café():` in a latin-1 file is indexed as `café`.
The readers still assumed UTF-8: a rename in such a module crashed with
UnicodeDecodeError or could not locate the name, `get_code_snippet` failed
and `read_file` called the file binary (issue #2901). A rename must also
write the file back in its declared encoding: re-encoding as UTF-8 would
silently turn `é` into `Ã©` under the latin-1 cookie.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.editing.rename import RenameRefused, RenameReport, rename
from codebase_rag.tests.test_rename_op import RecordedGraph, _index
from codebase_rag.tools.code_retrieval import CodeRetriever
from codebase_rag.tools.file_reader import FileReader

_MENU = """\
# -*- coding: latin-1 -*-
\"\"\"Menu helpers.\"\"\"


def café(price):
    return price * 2


def menu():
    return café(3)
"""
_LATIN1 = "latin-1"


def _write_menu(root: Path) -> Path:
    path = root / "menu.py"
    path.write_bytes(_MENU.encode(_LATIN1))
    return path


@pytest.fixture
def latin_repo(temp_repo: Path, mock_ingestor: MagicMock) -> tuple[Path, RecordedGraph]:
    _write_menu(temp_repo)
    return temp_repo, _index(temp_repo, mock_ingestor)


def _rename(
    root: Path, graph: RecordedGraph, qn: str, new: str, dry_run: bool
) -> RenameReport:
    return rename(root, graph.fetch_all, graph.project, qn, new, dry_run=dry_run)


def _on_disk(root: Path) -> str:
    data = (root / "menu.py").read_bytes()
    # Still latin-1 bytes: the `é` is one 0xE9 byte, not UTF-8's 0xC3 0xA9.
    assert "é".encode() not in data, data
    compile(data, "menu.py", "exec")
    return data.decode(_LATIN1)


def test_renaming_an_ascii_name_in_a_latin1_module(
    latin_repo: tuple[Path, RecordedGraph],
) -> None:
    root, graph = latin_repo
    preview = _rename(root, graph, f"{graph.project}.menu.menu", "main_menu", True)
    assert "+def main_menu():" in preview.diff, preview.diff

    _rename(root, graph, f"{graph.project}.menu.menu", "main_menu", False)
    text = _on_disk(root)
    assert "def main_menu():" in text and "def café(price):" in text, text


def test_renaming_a_non_ascii_name_in_a_latin1_module(
    latin_repo: tuple[Path, RecordedGraph],
) -> None:
    root, graph = latin_repo
    _rename(root, graph, f"{graph.project}.menu.café", "price_twice", False)
    text = _on_disk(root)
    assert "def price_twice(price):" in text, text
    assert "return price_twice(3)" in text, text
    assert "café" not in text, text


def test_a_name_the_declared_encoding_cannot_hold_is_refused(
    latin_repo: tuple[Path, RecordedGraph],
) -> None:
    # Negative: latin-1 has no `ő`; writing it would need another encoding,
    # so nothing is written.
    root, graph = latin_repo
    before = (root / "menu.py").read_bytes()
    with pytest.raises(RenameRefused):
        _rename(root, graph, f"{graph.project}.menu.menu", "menű", False)
    assert (root / "menu.py").read_bytes() == before


@pytest.mark.asyncio
async def test_get_code_snippet_reads_the_declared_encoding(tmp_path: Path) -> None:
    _write_menu(tmp_path)
    ingestor = MagicMock()
    ingestor.fetch_all.return_value = [
        {"path": "menu.py", "start": 5, "end": 6, "name": "café"}
    ]
    snippet = await CodeRetriever(str(tmp_path), ingestor).find_code_snippet(
        "menu.café"
    )
    assert snippet.found, snippet.error_message
    assert snippet.source_code.startswith("def café(price):"), snippet.source_code


@pytest.mark.asyncio
async def test_read_file_reads_the_declared_encoding(tmp_path: Path) -> None:
    _write_menu(tmp_path)
    result = await FileReader(str(tmp_path)).read_file("menu.py")
    assert result.error_message is None, result.error_message
    assert result.content == _MENU


@pytest.mark.asyncio
async def test_undeclared_non_utf8_python_is_still_reported(tmp_path: Path) -> None:
    # Negative: without a cookie the file IS UTF-8 to CPython, so latin-1
    # bytes there are an undecodable file, not a declared one.
    (tmp_path / "raw.py").write_bytes("x = 'café'\n".encode(_LATIN1))
    result = await FileReader(str(tmp_path)).read_file("raw.py")
    assert result.content is None and result.error_message is not None


@pytest.mark.asyncio
async def test_utf8_python_reads_unchanged(tmp_path: Path) -> None:
    # Negative: the common case keeps reading exactly as before.
    (tmp_path / "plain.py").write_text("x = 'café'\n", encoding="utf-8")
    result = await FileReader(str(tmp_path)).read_file("plain.py")
    assert result.content == "x = 'café'\n"
