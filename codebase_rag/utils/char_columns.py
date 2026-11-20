"""Character columns for the byte columns the graph stores (issue #3162).

tree-sitter counts a column in bytes of the UTF-8 text it parsed, and the
graph keeps that unit: `rename` and the other edits slice those bytes. An
editor, an LSP client or an agent indexing a `str` counts characters, which is
the same number only while the line before the position is ASCII. `cgr graph`
and its MCP tools therefore answer a character column beside each byte column,
derived when the answer is read from the bytes the indexer parsed, so the graph
itself does not change.
"""

from __future__ import annotations

from pathlib import Path

from .. import constants as cs
from ..language_spec import get_language_for_extension
from .source_encoding import grammar_bytes


class CharColumns:
    """Character columns of positions in the files under one root.

    One instance serves one answer, so a file is read at most once however
    many rows sit in it. No root, a file that cannot be read from under it, a
    line past its end, or a byte offset past the line or inside a character
    answers None: the caller leaves the field out rather than guess.
    """

    def __init__(self, root: Path | None) -> None:
        self._root = root
        self._lines: dict[str, list[bytes] | None] = {}

    def at(self, path: str | None, line: int | None, col: int | None) -> int | None:
        """The character column of `col`, a 0-based UTF-8 byte offset into
        1-based `line` of `path` (relative to the root)."""
        if self._root is None or path is None or line is None or col is None:
            return None
        if path not in self._lines:
            self._lines[path] = _parsed_lines(self._root, path)
        lines = self._lines[path]
        if lines is None or not 0 < line <= len(lines):
            return None
        text = lines[line - 1]
        if not 0 <= col <= len(text):
            return None
        try:
            return len(text[:col].decode(cs.ENCODING_UTF8))
        except UnicodeDecodeError:
            return None


def _parsed_lines(root: Path, path: str) -> list[bytes] | None:
    """The lines of the bytes the indexer parsed for `path`, or None when it
    is not a readable file under `root`.

    A source declaring another encoding, a UTF-16 one and a notebook were
    parsed as UTF-8 text made from the file, and the byte columns count that
    text, not the file (issues #2445, #3153, #2480). Lines split on `\\n`
    alone, as tree-sitter counts rows. None too for a Java source the
    indexer may have parsed from its delombok expansion (issue #1140).
    """
    file = root / path
    try:
        if not file.resolve().is_relative_to(root.resolve()) or not file.is_file():
            return None
        if file.suffix == cs.EXT_JAVA:
            # Imported here, as the notebook reader below is. The expansion
            # is only had by running delombok, so such a file answers nothing.
            from ..parsers.java_lombok import may_parse_delomboked

            if may_parse_delomboked(root, path):
                return None
        source = file.read_bytes()
    except (OSError, RuntimeError, ValueError):
        return None
    language = get_language_for_extension(file.suffix)
    if file.suffix == cs.EXT_IPYNB:
        # Imported here: the parsers package loads every processor, which
        # `cgr graph` does not otherwise import.
        from ..parsers.notebook import NotebookSkipped, read_notebook

        notebook = read_notebook(source)
        if isinstance(notebook, NotebookSkipped):
            return None
        source, language = notebook.source, cs.SupportedLanguage.PYTHON
    return grammar_bytes(source, language, file).split(b"\n")
