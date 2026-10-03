"""The notebook reader: code cells to aligned Python source (issue #2480)."""

from __future__ import annotations

import json

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.notebook import (
    NotebookSkipped,
    NotebookSource,
    _Cursor,
    notebook_source_text,
    read_notebook,
)


def _nb(cells: list[dict], **metadata: object) -> bytes:
    document = {"cells": cells, "metadata": metadata, "nbformat": 4}
    return json.dumps(document, indent=1).encode()


def _code(source: list[str] | str, **extra: object) -> dict:
    return {
        "cell_type": "code",
        "metadata": {},
        "outputs": [],
        **extra,
        "source": source,
    }


def _read(data: bytes) -> NotebookSource:
    notebook = read_notebook(data)
    assert isinstance(notebook, NotebookSource), notebook
    return notebook


def _lines(notebook: NotebookSource) -> list[str]:
    return notebook.source.decode().split("\n")


def _file_line(data: bytes, fragment: str) -> int:
    encoded = json.dumps(fragment).encode()
    return next(
        number for number, line in enumerate(data.split(b"\n"), 1) if encoded in line
    )


def test_each_code_line_sits_on_its_notebook_line() -> None:
    data = _nb(
        [
            {"cell_type": "markdown", "metadata": {}, "source": ["# title\n"]},
            _code(["import os\n", "\n", "def f():\n", "    return os.sep\n"]),
            _code(["f()"]),
        ]
    )
    notebook = _read(data)
    lines = _lines(notebook)

    for fragment in ("import os\n", "def f():\n", "    return os.sep\n", "f()"):
        assert lines[_file_line(data, fragment) - 1] == fragment.rstrip("\n")
    assert [cell.index for cell in notebook.cells] == [2, 3]


def test_a_line_maps_back_to_its_cell_and_line_in_the_cell() -> None:
    data = _nb(
        [
            {"cell_type": "markdown", "metadata": {}, "source": ["# title\n"]},
            _code(["import os\n", "def f():\n", "    return os.sep\n"]),
            _code(["f()"]),
        ]
    )
    notebook = _read(data)

    assert notebook.cell_at(_file_line(data, "    return os.sep\n")) == (2, 3)
    assert notebook.cell_at(_file_line(data, "f()")) == (3, 1)
    assert notebook.cell_at(1) is None


def test_quotes_backslashes_and_unicode_in_code_survive() -> None:
    code = 'print("a \\"quoted\\" \\\\ path", "café")'
    notebook = _read(_nb([_code([code])]))

    assert code in _lines(notebook)


def test_outputs_are_skipped_without_being_read() -> None:
    blob = "def in_output():\\n" * 200_000
    output = {"output_type": "stream", "name": "stdout", "text": [blob, '"]}{[']}
    notebook = _read(_nb([_code(["x = 1\n"], outputs=[output]), _code(["y = 2"])]))

    text = notebook.source.decode()
    assert "in_output" not in text
    assert "x = 1" in text
    assert "y = 2" in text


def test_a_continuation_line_is_not_taken_for_a_magic() -> None:
    code = ["total = (\n", "    a\n", "    % b\n", "    != c\n", ")\n"]
    notebook = _read(_nb([_code(code)]))

    assert "    % b" in _lines(notebook)
    assert "    != c" in _lines(notebook)


def test_magic_looking_lines_inside_strings_and_brackets_are_python() -> None:
    code = [
        'doc = """\n',
        "%matplotlib inline\n",
        "!pip install x\n",
        "files = !ls\n",
        '"""\n',
        "text = '''\n",
        "?help\n",
        "'''\n",
        "total = (a\n",
        "    %divisor())\n",
        "rest = a \\\n",
        "    %divisor()\n",
        "s = 'one \\\n",
        "%not a magic'\n",
    ]
    lines = _lines(_read(_nb([_code(code)])))

    for text in code:
        assert text.rstrip("\n") in lines
    assert "pass" not in (line.strip() for line in lines)


@pytest.mark.parametrize(
    "magic",
    ["%matplotlib inline", "!pip install x", "files = !ls", "out = %sx ls", "?load"],
)
def test_a_magic_that_starts_a_statement_still_becomes_pass(magic: str) -> None:
    # It follows a string, a bracket and a backslash continuation that have
    # all closed, and a comment holding a quote and a bracket.
    code = [
        'doc = """\n',
        '!not a magic """\n',
        "total = (a\n",
        "    % b)\n",
        "rest = a \\\n",
        "    + b\n",
        "x = 'a # (' # ( '\n",
        f"{magic}\n",
    ]
    lines = _lines(_read(_nb([_code(code)])))

    assert [line for line in lines if line.strip()][-1] == "pass"
    assert '!not a magic """' in lines


def test_magic_lines_become_pass_at_their_own_indent() -> None:
    notebook = _read(_nb([_code(["for f in files:\n", "    !cp {f} out/\n"])]))

    assert "    pass" in _lines(notebook)
    assert "!cp" not in notebook.source.decode()


@pytest.mark.parametrize("language", ["Python", "python3", " ipython "])
def test_python_kernel_spellings_are_python(language: str) -> None:
    data = _nb([_code(["x = 1"])], language_info={"name": language})

    assert isinstance(read_notebook(data), NotebookSource)


def test_a_notebook_declaring_no_language_is_read_as_python() -> None:
    assert isinstance(read_notebook(_nb([_code(["x = 1"])])), NotebookSource)


def test_another_kernel_language_is_reported() -> None:
    data = _nb([_code(["x <- 1"])], kernelspec={"language": "R", "name": "ir"})

    assert read_notebook(data) == NotebookSkipped(cs.NotebookSkip.NOT_PYTHON, "R")
    assert notebook_source_text(data) is None


def test_a_notebook_without_code_cells_is_an_empty_module() -> None:
    data = _nb([{"cell_type": "markdown", "metadata": {}, "source": "# only prose"}])

    assert _read(data) == NotebookSource(b"", ())


@pytest.mark.parametrize(
    "data",
    [
        b'{"cells": [], "metadata": {}}',
        b'{"cells": [], "metadata": {}, "nbformat": 3}',
        b'{"cells": [], "metadata": {}, "nbformat": "4"}',
        b'{"cells": [], "metadata": {}, "nbformat": 4} trailing',
        b'{"cells": [], "metadata": {}, "nbformat": 4}{"cells": []}',
    ],
    ids=["no-nbformat", "nbformat-3", "nbformat-string", "garbage", "two-objects"],
)
def test_only_one_nbformat_4_document_is_a_notebook(data: bytes) -> None:
    assert read_notebook(data) == NotebookSkipped(cs.NotebookSkip.MALFORMED)


def test_whitespace_after_the_document_is_allowed() -> None:
    data = b'{"cells": [], "metadata": {}, "nbformat": 4}\r\n \t\n'

    assert read_notebook(data) == NotebookSource(b"", ())


@pytest.mark.parametrize(
    ("data", "token", "end"),
    [
        (b"0", b"0", 1),
        (b"-0", b"-0", 2),
        (b"12", b"12", 2),
        (b"1.5", b"1.5", 3),
        (b"1e5", b"1e5", 3),
        (b"-2.5E-3", b"-2.5E-3", 7),
        (b"0.5e+10,", b"0.5e+10", 7),
        (b"true", b"true", 4),
        (b"false", b"false", 5),
        (b"null", b"null", 4),
        (b" \n4}", b"4", 3),
        # The grammar stops where JSON's does; what follows is the caller's.
        (b"01", b"0", 1),
        (b"-01", b"-0", 2),
        (b"1.", b"1", 1),
        (b"1e", b"1", 1),
        (b"1e+", b"1", 1),
        (b"nullx", b"null", 4),
    ],
)
def test_a_json_scalar_is_read_where_its_grammar_ends(
    data: bytes, token: bytes, end: int
) -> None:
    cursor = _Cursor(data)

    assert cursor.scalar() == token
    assert cursor.pos == end


@pytest.mark.parametrize("data", [b"nul", b".5", b"+1", b"-", b"True", b"", b'"4"'])
def test_text_that_starts_no_json_scalar_is_refused(data: bytes) -> None:
    with pytest.raises(ValueError):
        _Cursor(data).scalar()
