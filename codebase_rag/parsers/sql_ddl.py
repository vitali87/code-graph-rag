"""Routines a SQL file's DDL names without calling them (issue #3181).

A trigger runs `EXECUTE FUNCTION touch()`, an aggregate runs its
`SFUNC = sum_sfunc`, a cast runs `WITH FUNCTION f`, a type's I/O runs
`INPUT = f`, a foreign-data wrapper runs its `HANDLER f`. None of these is
an `invocation`, so the call pass saw no reference and the routine looked
dead. tree-sitter-sql 0.3.11 cannot parse most of these statements at all
(CREATE AGGREGATE, CAST, OPERATOR, EVENT TRIGGER and FOREIGN DATA WRAPPER
come back as ERROR nodes), so the clauses are read from the source text.

Comments, string literals and dollar-quoted bodies are blanked first, so a
clause inside one (a commented-out trigger, dynamic SQL in a PL/pgSQL body)
is not DDL the file runs. Each statement is matched by its leading
keywords, and only the routine slots that statement kind defines are read:
a table's `handler` column is no FDW handler.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterator
from typing import NamedTuple

from .. import constants as cs
from ..sql_names import normalize_sql_reference
from ..types_defs import PropertyDict

# One (possibly quoted, possibly schema-qualified) routine name.
_IDENT = rb'(?:"(?:[^"]|"")+"|[A-Za-z_\x80-\xff][\w$\x80-\xff]*)'
_NAME_GROUP = "name"
_NAME = rb"(?P<name>" + _IDENT + rb"(?:\." + _IDENT + rb")*)"

_DOLLAR_TAG = re.compile(rb"\$(?:[A-Za-z_\x80-\xff][\w\x80-\xff]*)?\$")
_IDENT_BYTE = re.compile(rb"[\w$\x80-\xff]")
# Where a comment, a string, a quoted identifier or a dollar quote can start.
_LEXICAL_START = re.compile(rb"--|/\*|['\"$]")
_NEWLINE = re.compile(rb"\n")
_SEMICOLON = re.compile(rb";")

# Each statement kind, by its leading keywords, with the clause that names
# a routine in it. A bare-keyword clause (`EXECUTE FUNCTION f`, `SUPPORT f`)
# counts only outside parentheses, where no parameter or column of that
# name can be (`CREATE FUNCTION f(support my_type)`); an `option = f` clause
# lives inside the statement's option list.
_TOP_LEVEL = True
_OPTION_LIST = False
_STATEMENT_SLOTS: tuple[tuple[re.Pattern[bytes], re.Pattern[bytes], bool], ...] = tuple(
    (re.compile(head, re.IGNORECASE), re.compile(slot, re.IGNORECASE), where)
    for head, slot, where in (
        (
            rb"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+|EVENT\s+)?TRIGGER\b",
            rb"\bEXECUTE\s+(?:FUNCTION|PROCEDURE)\s+" + _NAME,
            _TOP_LEVEL,
        ),
        (
            rb"\s*CREATE\s+(?:OR\s+REPLACE\s+)?AGGREGATE\b",
            rb"\b(?:SFUNC|FINALFUNC|COMBINEFUNC|SERIALFUNC|DESERIALFUNC|MSFUNC"
            rb"|MINVFUNC|MFINALFUNC)\s*=\s*" + _NAME,
            _OPTION_LIST,
        ),
        (
            rb"\s*CREATE\s+CAST\b",
            rb"\bWITH\s+FUNCTION\s+" + _NAME,
            _TOP_LEVEL,
        ),
        (
            rb"\s*CREATE\s+OPERATOR\s+(?!CLASS\b|FAMILY\b)",
            rb"\b(?:FUNCTION|PROCEDURE|RESTRICT|JOIN)\s*=\s*" + _NAME,
            _OPTION_LIST,
        ),
        (
            rb"\s*CREATE\s+TYPE\b",
            rb"\b(?:INPUT|OUTPUT|RECEIVE|SEND|TYPMOD_IN|TYPMOD_OUT|ANALYZE"
            rb"|SUBSCRIPT|CANONICAL|SUBTYPE_DIFF)\s*=\s*" + _NAME,
            _OPTION_LIST,
        ),
        (
            rb"\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:TRUSTED\s+)?(?:PROCEDURAL\s+)?"
            rb"LANGUAGE|(?:CREATE|ALTER)\s+FOREIGN\s+DATA\s+WRAPPER"
            rb"|CREATE\s+ACCESS\s+METHOD)\b",
            rb"\b(?:HANDLER|VALIDATOR|INLINE)\s+" + _NAME,
            _TOP_LEVEL,
        ),
        (
            rb"\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?|ALTER\s+)(?:FUNCTION|PROCEDURE)\b",
            rb"\bSUPPORT\s+" + _NAME,
            _TOP_LEVEL,
        ),
    )
)

# Words that follow a slot keyword without naming a routine
# (`NO HANDLER VALIDATOR v` reads `VALIDATOR` after `HANDLER`).
_NOT_ROUTINES = frozenset({"no", "handler", "validator", "inline", "options"})


class DdlRoutineReference(NamedTuple):
    name: str
    site: PropertyDict


def ddl_routine_references(
    text: bytes, start_row: int = 0, start_col: int = 0
) -> list[DdlRoutineReference]:
    """The routines `text`'s DDL names, normalized, each with its name's span.

    `start_row`/`start_col` place `text` in its file (a tree-sitter root
    node starts at its first token, not at byte 0), so the span is the
    file's 1-based line and 0-based byte column, as on a call site.
    """
    masked = _mask(text)
    line_starts = [0, *(m.end() for m in _NEWLINE.finditer(masked))]
    found: list[DdlRoutineReference] = []
    start = 0
    for end in (*(m.start() for m in _SEMICOLON.finditer(masked)), len(masked)):
        if (kind := _statement_slot(masked, start, end)) is not None:
            for lo, hi in _slot_names(masked, *kind, start, end):
                name = normalize_sql_reference(
                    text[lo:hi].decode(cs.ENCODING_UTF8, "replace")
                )
                if name and name not in _NOT_ROUTINES:
                    line, col = _position(lo, line_starts, start_row, start_col)
                    end_line, end_col = _position(hi, line_starts, start_row, start_col)
                    site: PropertyDict = {
                        cs.KEY_LINE: line,
                        cs.KEY_COL: col,
                        cs.KEY_END_LINE: end_line,
                        cs.KEY_END_COL: end_col,
                    }
                    found.append(DdlRoutineReference(name, site))
        start = end + 1
    return found


def _statement_slot(
    masked: bytes, start: int, end: int
) -> tuple[re.Pattern[bytes], bool] | None:
    # The routine slot of the statement in `masked[start:end]`, by its
    # leading keywords; the first kind that matches wins.
    for head, slot, top_level in _STATEMENT_SLOTS:
        if head.match(masked, start, end):
            return slot, top_level
    return None


def _slot_names(
    masked: bytes, slot: re.Pattern[bytes], top_level: bool, start: int, end: int
) -> Iterator[tuple[int, int]]:
    # Spans of the names the statement's slot clauses carry. A top-level
    # clause inside parentheses is a parameter or column, not the clause.
    for match in slot.finditer(masked, start, end):
        lo, hi = match.span(_NAME_GROUP)
        if top_level and masked.count(b"(", start, lo) != masked.count(b")", start, lo):
            continue
        yield lo, hi


def _position(
    offset: int, line_starts: list[int], start_row: int, start_col: int
) -> tuple[int, int]:
    # 1-based file line and 0-based byte column of `offset` in the text.
    row = bisect_right(line_starts, offset) - 1
    col = offset - line_starts[row] + (start_col if row == 0 else 0)
    return start_row + row + 1, col


def _mask(text: bytes) -> bytes:
    # Comments, string literals and dollar-quoted bodies become spaces
    # (newlines kept, so offsets and lines still match `text`). Quoted
    # identifiers stay: `"MyFinal"` is a routine name, and a `;` inside one
    # does not end the statement.
    out = bytearray(text)
    i = 0
    while (lexical := _LEXICAL_START.search(text, i)) is not None:
        start = lexical.start()
        i, blanked = _lexical_end(text, start)
        if blanked:
            for k in range(start, i):
                if out[k] != 0x0A:
                    out[k] = 0x20
    return bytes(out)


def _lexical_end(text: bytes, i: int) -> tuple[int, bool]:
    # Where the comment, literal, quoted identifier or dollar quote starting
    # at `text[i]` ends, and whether it is blanked; (i + 1, False) when the
    # byte starts none of them.
    if text.startswith(b"--", i):
        end = text.find(b"\n", i)
        return (len(text) if end < 0 else end), True
    if text.startswith(b"/*", i):
        return _block_comment_end(text, i), True
    ch = text[i]
    if ch == 0x27:  # '
        return _string_end(text, i), True
    if ch == 0x22:  # "
        return _quoted_identifier_end(text, i), False
    if (
        ch == 0x24
        and not (i > 0 and _IDENT_BYTE.match(text, i - 1))
        and (tag := _DOLLAR_TAG.match(text, i))
    ):
        close = text.find(tag.group(), tag.end())
        return (len(text) if close < 0 else close + len(tag.group())), True
    return i + 1, False


def _block_comment_end(text: bytes, i: int) -> int:
    # Block comments nest.
    n = len(text)
    depth, j = 1, i + 2
    while j < n and depth:
        if text.startswith(b"/*", j):
            depth, j = depth + 1, j + 2
        elif text.startswith(b"*/", j):
            depth, j = depth - 1, j + 2
        else:
            j += 1
    return j


def _string_end(text: bytes, i: int) -> int:
    # `''` is a quote in any string; an `E'...'` string also takes backslash
    # escapes (an `E` that ends a longer word is not that prefix).
    n = len(text)
    escapes = (
        i > 0
        and text[i - 1] in b"Ee"
        and not (i > 1 and _IDENT_BYTE.match(text, i - 2))
    )
    j = i + 1
    while j < n:
        if escapes and text[j] == 0x5C:  # backslash
            j += 2
        elif text[j] == 0x27:
            if j + 1 < n and text[j + 1] == 0x27:
                j += 2
            else:
                break
        else:
            j += 1
    return min(j + 1, n)


def _quoted_identifier_end(text: bytes, i: int) -> int:
    n = len(text)
    j = i + 1
    while j < n and not (text[j] == 0x22 and text[j + 1 : j + 2] != b'"'):
        j += 2 if text[j] == 0x22 else 1
    return j + 1
