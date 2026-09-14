"""Status glyphs that survive the terminal they are printed to.

Rich substitutes ASCII box characters when a stream's encoding cannot
represent them, but it does not touch CELL TEXT: a glyph written into a
table cell reaches the terminal's codec unchanged, and a code page that
lacks it raises ``UnicodeEncodeError`` instead of printing (#1910 on
``cgr doctor``, #1914 on the query result table -- both reported from a
CP950 Windows terminal).

Shared rather than private to one caller, because the two sites must not
drift: a reader who learns that ``OK``/``NO`` means pass/fail in one table
should not meet a different pair in the next.
"""

from __future__ import annotations

from codebase_rag import constants as cs

__all__ = ["status_mark"]


def _can_encode(text: str, encoding: str | None) -> bool:
    """Whether `encoding` can represent `text`.

    ``None`` is the answer a Rich console gives for a stream with no
    encoding of its own (a ``StringIO`` capture, a pytest ``capsys``
    buffer). Those accept any ``str``, so the glyph is safe there and the
    ASCII pair would only make captured output harder to read.
    """
    if encoding is None:
        return True
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        # LookupError: an encoding name Python does not know. Unreadable is
        # not the same as unrepresentable, but both mean this glyph cannot
        # be shown to be safe, and the ASCII pair always is.
        return False
    return True


def status_mark(passed: bool, encoding: str | None) -> str:
    """``✓``/``✗`` when `encoding` can carry them, ``OK``/``NO`` when it
    cannot.

    Both marks are tested, not just the one being returned: a terminal that
    can encode one and not the other would otherwise produce a column that
    mixes the two alphabets, and the pair is meant to be read together.
    """
    glyphs_usable = _can_encode(cs.HEALTH_MARK_PASS, encoding) and _can_encode(
        cs.HEALTH_MARK_FAIL, encoding
    )
    if glyphs_usable:
        return cs.HEALTH_MARK_PASS if passed else cs.HEALTH_MARK_FAIL
    return cs.HEALTH_MARK_PASS_ASCII if passed else cs.HEALTH_MARK_FAIL_ASCII
