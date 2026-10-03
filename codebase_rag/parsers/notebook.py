"""The Python source a Jupyter notebook's code cells add up to (issue #2480).

A notebook is JSON, so the Python parser cannot read it as it is on disk.
This module turns it into the Python module its code cells make when run top
to bottom, and the rest of the pipeline parses that like any `.py` file.

The source is laid out line for line against the notebook FILE: each code
line sits on the line of the `.ipynb` where its JSON string is written, and
every other line is blank. So the `start_line`, `end_line` and call-site
`line` the parse records are lines of the notebook itself, and `cgr` output
such as `analysis.ipynb:14` opens on that code line inside its cell. Columns
are columns of the code line, not of the JSON text around it. Jupyter and
nbformat write one source line per JSON line; a cell stored as one string,
or a minified notebook, still parses, but each of its lines moves down past
the one before, so those lines drift from the file's.

Only `source` is decoded. Outputs, attachments and metadata other than the
kernel language are skipped token by token, so a notebook carrying large
embedded images costs a scan of its bytes, not a decode of them.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from collections.abc import Iterator
from pathlib import PurePath
from typing import NamedTuple

from .. import constants as cs

_BOM = b"\xef\xbb\xbf"
_WHITESPACE = re.compile(rb"[ \t\r\n]*")
_SCALAR = re.compile(rb"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null")
_STRUCTURAL = re.compile(rb'["\[\]{}]')
_NEWLINE = re.compile(r"\r\n|\r|\n")
_MAGIC_LINE = re.compile(cs.NB_MAGIC_LINE_PATTERN)
_CELL_MAGIC = re.compile(cs.NB_CELL_MAGIC_PATTERN)
_QUOTE, _COLON, _COMMA, _BACKSLASH = ord('"'), ord(":"), ord(","), ord("\\")
_OPEN_OBJECT, _CLOSE_OBJECT = ord("{"), ord("}")
_OPEN_ARRAY, _CLOSE_ARRAY = ord("["), ord("]")
_OPENERS = frozenset({_OPEN_OBJECT, _OPEN_ARRAY})


class NotebookCell(NamedTuple):
    """A code cell's place in the notebook and in its Python source."""

    index: int
    first_line: int
    last_line: int


class NotebookSource(NamedTuple):
    """A Python notebook's code cells as one module, aligned to its file."""

    source: bytes
    cells: tuple[NotebookCell, ...]

    def cell_at(self, line: int) -> tuple[int, int] | None:
        """`(cell index, line within the cell)` of a notebook line, both 1-based."""
        for cell in self.cells:
            if cell.first_line <= line <= cell.last_line:
                return cell.index, line - cell.first_line + 1
        return None


class NotebookSkipped(NamedTuple):
    """A notebook that has no Python source to parse, and why."""

    reason: cs.NotebookSkip
    language: str | None = None


class _Fragment(NamedTuple):
    file_line: int
    text: str


class _CodeCell(NamedTuple):
    index: int
    fragments: list[_Fragment]


def is_notebook_path(path: PurePath) -> bool:
    return path.suffix == cs.EXT_IPYNB


class _Cursor:
    """A forward-only reader over JSON bytes that can skip what it does not need.

    Any shape it does not expect raises ValueError, which the caller turns
    into `NotebookSkip.MALFORMED`; every step either advances or raises, so a
    damaged file cannot make it loop.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = len(_BOM) if data.startswith(_BOM) else 0
        self._line = 1
        self._line_pos = 0

    def peek(self) -> int | None:
        match = _WHITESPACE.match(self.data, self.pos)
        self.pos = match.end() if match else self.pos
        return self.data[self.pos] if self.pos < len(self.data) else None

    def expect(self, char: int) -> None:
        if self.peek() != char:
            raise ValueError(self.pos)
        self.pos += 1

    def line(self) -> int:
        # Asked in file order only, so counting from the last answer keeps
        # the whole read linear in the file size.
        self._line += self.data.count(b"\n", self._line_pos, self.pos)
        self._line_pos = self.pos
        return self._line

    def string(self) -> str:
        if self.peek() != _QUOTE:
            raise ValueError(self.pos)
        start = self.pos
        self._skip_string()
        raw = self.data[start : self.pos].decode(cs.ENCODING_UTF8, errors="replace")
        decoded = json.loads(raw)
        if not isinstance(decoded, str):
            raise ValueError(self.pos)
        return decoded

    def skip(self) -> None:
        first = self.peek()
        if first == _QUOTE:
            self.string()
        elif first in _OPENERS:
            self._skip_container()
        elif match := _SCALAR.match(self.data, self.pos):
            self.pos = match.end()
        else:
            raise ValueError(self.pos)

    def _skip_container(self) -> None:
        depth = 0
        while match := _STRUCTURAL.search(self.data, self.pos):
            self.pos = match.start()
            char = self.data[self.pos]
            if char == _QUOTE:
                self._skip_string()
                continue
            self.pos += 1
            depth += 1 if char in _OPENERS else -1
            if depth == 0:
                return
        raise ValueError(self.pos)

    def _skip_string(self) -> None:
        # A search for the closing quote rather than a regex over the
        # string: a base64 image output is tens of megabytes of one string.
        end = self.pos + 1
        while (end := self.data.find(b'"', end)) >= 0:
            escapes = 0
            while self.data[end - 1 - escapes] == _BACKSLASH:
                escapes += 1
            end += 1
            if escapes % 2 == 0:
                self.pos = end
                return
        raise ValueError(self.pos)

    def keys(self) -> Iterator[str]:
        """Each key of the object here; the caller reads or skips its value."""
        self.expect(_OPEN_OBJECT)
        if self.peek() == _CLOSE_OBJECT:
            self.pos += 1
            return
        while True:
            key = self.string()
            self.expect(_COLON)
            yield key
            if self.peek() != _COMMA:
                self.expect(_CLOSE_OBJECT)
                return
            self.pos += 1

    def items(self) -> Iterator[None]:
        """Once per element of the array here; the caller reads or skips it."""
        self.expect(_OPEN_ARRAY)
        if self.peek() == _CLOSE_ARRAY:
            self.pos += 1
            return
        while True:
            yield None
            if self.peek() != _COMMA:
                self.expect(_CLOSE_ARRAY)
                return
            self.pos += 1


def read_notebook(data: bytes) -> NotebookSource | NotebookSkipped:
    """The Python source of a notebook's code cells, or why there is none."""
    try:
        cells, language = _read_cells(_Cursor(data))
    except (ValueError, IndexError):
        return NotebookSkipped(cs.NotebookSkip.MALFORMED)
    if cells is None:
        return NotebookSkipped(cs.NotebookSkip.MALFORMED)
    if language is not None and language.strip().lower() not in (
        cs.NB_PYTHON_LANGUAGES
    ):
        return NotebookSkipped(cs.NotebookSkip.NOT_PYTHON, language)
    return _lay_out(cells)


def notebook_source_text(data: bytes) -> str | None:
    """The aligned Python text of a notebook, for reading snippets back."""
    notebook = read_notebook(data)
    if isinstance(notebook, NotebookSkipped):
        return None
    return notebook.source.decode(cs.ENCODING_UTF8, errors="replace")


def _read_cells(cursor: _Cursor) -> tuple[list[_CodeCell] | None, str | None]:
    cells: list[_CodeCell] | None = None
    language: str | None = None
    for key in cursor.keys():
        match key:
            case cs.NB_KEY_CELLS:
                cells = []
                for index, _ in enumerate(cursor.items(), start=1):
                    if (cell := _read_cell(cursor, index)) is not None:
                        cells.append(cell)
            case cs.NB_KEY_METADATA:
                language = _read_language(cursor)
            case _:
                cursor.skip()
    return cells, language


def _read_language(cursor: _Cursor) -> str | None:
    reported: str | None = None
    declared: str | None = None
    if cursor.peek() != _OPEN_OBJECT:
        cursor.skip()
        return None
    for key in cursor.keys():
        match key:
            case cs.NB_KEY_LANGUAGE_INFO:
                reported = _string_member(cursor, cs.NB_KEY_NAME)
            case cs.NB_KEY_KERNELSPEC:
                declared = _string_member(cursor, cs.NB_KEY_LANGUAGE)
            case _:
                cursor.skip()
    return reported or declared


def _string_member(cursor: _Cursor, wanted: str) -> str | None:
    if cursor.peek() != _OPEN_OBJECT:
        cursor.skip()
        return None
    found: str | None = None
    for key in cursor.keys():
        if key == wanted and cursor.peek() == _QUOTE:
            found = cursor.string()
        else:
            cursor.skip()
    return found


def _read_cell(cursor: _Cursor, index: int) -> _CodeCell | None:
    if cursor.peek() != _OPEN_OBJECT:
        cursor.skip()
        return None
    cell_type: str | None = None
    fragments: list[_Fragment] = []
    for key in cursor.keys():
        if key == cs.NB_KEY_CELL_TYPE and cursor.peek() == _QUOTE:
            cell_type = cursor.string()
        # nbformat writes keys sorted, so the type is usually known before
        # the source: a markdown cell's text is skipped, not decoded.
        elif key == cs.NB_KEY_SOURCE and cell_type in (None, cs.NB_CELL_TYPE_CODE):
            fragments = _read_source(cursor)
        else:
            cursor.skip()
    return _CodeCell(index, fragments) if cell_type == cs.NB_CELL_TYPE_CODE else None


def _read_source(cursor: _Cursor) -> list[_Fragment]:
    # nbformat allows a cell's source as one string or as a list of strings;
    # anything else is not a source the reader can place.
    first = cursor.peek()
    if first == _QUOTE:
        line = cursor.line()
        return [_Fragment(line, cursor.string())]
    if first != _OPEN_ARRAY:
        raise ValueError(cursor.pos)
    fragments: list[_Fragment] = []
    for _ in cursor.items():
        cursor.peek()
        line = cursor.line()
        fragments.append(_Fragment(line, cursor.string()))
    return fragments


def _lay_out(cells: list[_CodeCell]) -> NotebookSource:
    lines: list[str] = []
    placed: list[NotebookCell] = []
    for cell in cells:
        code = _python_lines(cell.fragments)
        if not any(line.text.strip() for line in code):
            continue
        first = 0
        for file_line, text in code:
            # Never above the line before: a cell stored as one string puts
            # all of its lines on one file line, and they cannot share it.
            target = max(file_line, len(lines) + 1)
            lines.extend([""] * (target - len(lines) - 1))
            lines.append(text)
            first = first or target
        placed.append(NotebookCell(cell.index, first, len(lines)))
    text = "\n".join(lines) + "\n" if lines else ""
    return NotebookSource(
        text.encode(cs.ENCODING_UTF8, errors="replace"), tuple(placed)
    )


def _python_lines(fragments: list[_Fragment]) -> list[_Fragment]:
    """The cell's lines, each on the file line its text starts on, with
    IPython-only syntax neutralised; empty for a cell that is not Python."""
    text = "".join(fragment.text for fragment in fragments)
    starts: list[int] = []
    offset = 0
    for fragment in fragments:
        starts.append(offset)
        offset += len(fragment.text)
    lines: list[_Fragment] = []
    pos = 0
    for newline in (*_NEWLINE.finditer(text), None):
        end = newline.start() if newline else len(text)
        if newline is None and pos == end:
            break
        file_line = fragments[bisect_right(starts, pos) - 1].file_line
        lines.append(_Fragment(file_line, text[pos:end]))
        if newline is not None:
            pos = newline.end()
    first = next((line.text for line in lines if line.text.strip()), "")
    if (magic := _CELL_MAGIC.match(first)) and (
        magic.group(1) not in cs.NB_PYTHON_BODY_CELL_MAGICS
    ):
        return []
    return [
        _Fragment(line.file_line, _neutralised(line.text))
        if _MAGIC_LINE.match(line.text)
        else line
        for line in lines
    ]


def _neutralised(line: str) -> str:
    indent = line[: len(line) - len(line.lstrip())]
    return f"{indent}{cs.NB_NEUTRAL_STATEMENT}"
