"""A boolean cell must survive the terminal it is printed to.

Issue #1914, the same class as #1910 on a different path. Rich substitutes
ASCII box characters when a stream's encoding cannot represent them, but it
does not touch CELL TEXT: a glyph written into a cell reaches the terminal's
codec unchanged, so the first query returning a boolean column raised
``UnicodeEncodeError`` on a CP950 Windows terminal instead of printing.

The negative control is the point of the rendering test: it renders the same
table with the raw glyph and requires it to RAISE, so a green result means
the fallback carried it rather than the harness never meeting the codec.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from codebase_rag import constants as cs
from codebase_rag.console_marks import status_mark

_UNSUPPORTING = "cp950"


def _console(encoding: str) -> Console:
    return Console(file=io.TextIOWrapper(io.BytesIO(), encoding=encoding), width=40)


def test_a_stream_that_can_carry_the_glyphs_gets_them() -> None:
    assert status_mark(True, "utf-8") == cs.HEALTH_MARK_PASS
    assert status_mark(False, "utf-8") == cs.HEALTH_MARK_FAIL


@pytest.mark.parametrize("encoding", [_UNSUPPORTING, "ascii", "latin-1"])
def test_a_stream_that_cannot_falls_back_to_the_ascii_pair(encoding: str) -> None:
    assert status_mark(True, encoding) == cs.HEALTH_MARK_PASS_ASCII
    assert status_mark(False, encoding) == cs.HEALTH_MARK_FAIL_ASCII


def test_an_encoding_python_does_not_know_falls_back_rather_than_raising() -> None:
    """`LookupError`, not `UnicodeEncodeError`. Unreadable is not the same as
    unrepresentable, but neither shows the glyph to be safe."""
    assert status_mark(True, "not-a-real-codec") == cs.HEALTH_MARK_PASS_ASCII


def test_a_stream_with_no_encoding_of_its_own_keeps_the_glyphs() -> None:
    """A capture buffer accepts any `str`, so the ASCII pair would only make
    captured output harder to read."""
    assert status_mark(True, None) == cs.HEALTH_MARK_PASS


# Shift_JIS-2004 and its relatives carry U+2713 and NOT U+2717 -- a real
# codec that splits the pair, which is what makes the "check both" rule
# falsifiable rather than merely prudent.
_SPLITS_THE_PAIR = "shift_jis_2004"


def test_the_split_codec_really_splits_the_pair() -> None:
    """The control for the test below: if this codec ever gains U+2717, that
    test would pass for the wrong reason."""
    cs.HEALTH_MARK_PASS.encode(_SPLITS_THE_PAIR)
    with pytest.raises(UnicodeEncodeError):
        cs.HEALTH_MARK_FAIL.encode(_SPLITS_THE_PAIR)


def test_both_marks_are_checked_not_only_the_one_returned() -> None:
    """A codec that carries one glyph and not the other must not produce a
    column mixing the two alphabets: `OK` beside `✗` reads as two different
    conventions in one table."""
    assert status_mark(True, _SPLITS_THE_PAIR) == cs.HEALTH_MARK_PASS_ASCII
    assert status_mark(False, _SPLITS_THE_PAIR) == cs.HEALTH_MARK_FAIL_ASCII


def _render(cell_for: object) -> str:
    """The query result table's shape, rendered to a CP950 stream."""
    console = _console(_UNSUPPORTING)
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("qualified_name")
    table.add_column("is_public")
    for name, flag in (("proj.a", True), ("proj.b", False)):
        table.add_row(name, cell_for(flag, console.encoding))  # type: ignore[operator]
    console.print(Panel(table, title=cs.QUERY_RESULTS_PANEL_TITLE, expand=False))
    console.file.flush()
    return console.file.buffer.getvalue().decode(_UNSUPPORTING)  # type: ignore[attr-defined]


def test_the_negative_control_really_cannot_encode_the_glyph() -> None:
    """Without this the test below would pass on a stream that never met the
    codec at all."""
    with pytest.raises(UnicodeEncodeError):
        _render(lambda flag, _enc: cs.HEALTH_MARK_PASS if flag else cs.HEALTH_MARK_FAIL)


def test_the_result_table_prints_on_a_terminal_that_lacks_the_glyphs() -> None:
    rendered = _render(status_mark)
    assert cs.HEALTH_MARK_PASS_ASCII in rendered
    assert cs.HEALTH_MARK_FAIL_ASCII in rendered
