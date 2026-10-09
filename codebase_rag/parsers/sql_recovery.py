"""Parse a SQL file so one bad statement costs only that statement (#3180).

A `.sql` file is one tree-sitter tree, and tree-sitter-sql 0.3.11 does not
always resynchronize at the next `;` after a statement it cannot handle:

- it accepts any text between two `$` as a dollar-quote tag, so the
  positional parameters in `f($1,$2)` open a quote that runs to the next
  `$1,$`, and `do$llar$s` (a legal identifier) opens one at `$llar$`;
- a plpgsql body or a header it cannot parse leaves an ERROR that can run
  over the next statements.

Every `CREATE FUNCTION` such a region covered vanished from the graph,
with its calls, and nothing said so. When the whole-file tree loses a
function that parses on its own, the file is re-parsed from a copy in
which each statement stands alone. That copy has the same length, with
every newline kept, so lines and columns are unchanged:

- a positional parameter's `$` becomes `_` (`$1` -> `_1`), which no
  grammar rule reads as a quote;
- a `CREATE FUNCTION` that fails on its own is kept with its body blanked
  (its name, parameters and span survive; its body's calls were lost
  anyway), or blanked whole when even its header does not parse;
- any other statement that fails on its own is blanked, but only when the
  functions are still not all found without that.

The statements are split by PostgreSQL's own lexical rules: a dollar-quote
tag is empty or identifier-like and never starts inside an identifier.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from tree_sitter import Node, Parser, Tree

from .. import constants as cs

_IDENT_BYTE = re.compile(rb"[\w$\x80-\xff]")
_DOLLAR_TAG = re.compile(rb"\$(?:[A-Za-z_\x80-\xff][\w\x80-\xff]*)?\$")
# Where a comment, a string, a quoted identifier or a dollar quote can start.
_LEXICAL_START = re.compile(rb"--|/\*|['\"$]")
_SEMICOLON = re.compile(rb";")
_POSITIONAL_PARAM = re.compile(rb"(?<![\w$\x80-\xff])\$(?=\d)")
_CREATE_FUNCTION = re.compile(
    rb"\s*CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b", re.IGNORECASE
)
_PARAM_PLACEHOLDER = b"_"
_BLANK = 0x20
_NEWLINE = 0x0A
_QUOTE = 0x27


class _Statement(NamedTuple):
    start: int
    end: int
    lead: int
    is_function: bool
    # Each dollar-quoted body, delimiters included.
    bodies: tuple[tuple[int, int], ...]


class _Standalone(NamedTuple):
    # How a `CREATE FUNCTION` statement is kept: its text, whether that
    # text defines the function, and, when it does so only with a parse
    # error, a clean hollowed text to fall back on if the error spreads.
    text: bytes
    defines: bool
    clean_fallback: bytes | None


class _Isolation(NamedTuple):
    # Functions kept with a parse error, and their clean hollowed texts; the
    # other statements that fail on their own; how many functions parse.
    noisy: list[tuple[_Statement, bytes]]
    failing_others: list[_Statement]
    recoverable: int


def parse_sql(parser: Parser, source: bytes) -> Tree:
    """The file's tree, re-parsed statement-safe only when that finds more."""
    tree = parser.parse(source)
    if not tree.root_node.has_error:
        return tree
    statements = _statements(source)
    expected = [s.lead for s in statements if s.is_function]
    best, best_found = tree, _found(tree, expected)
    if best_found == len(expected):
        return tree
    isolated = bytearray(_POSITIONAL_PARAM.sub(_PARAM_PLACEHOLDER, source))
    isolation = _isolate(parser, statements, isolated)
    return _quietest(parser, isolated, isolation, expected, best, best_found)


def _isolate(
    parser: Parser, statements: list[_Statement], isolated: bytearray
) -> _Isolation:
    # Stand each `CREATE FUNCTION` alone in `isolated` and note what else
    # fails on its own, for the quieting steps.
    noisy: list[tuple[_Statement, bytes]] = []
    failing_others: list[_Statement] = []
    recoverable = 0
    for statement in statements:
        text = bytes(isolated[statement.start : statement.end])
        if statement.is_function:
            kept = _standalone_function(parser, statement, text)
            isolated[statement.start : statement.end] = kept.text
            recoverable += kept.defines
            if kept.clean_fallback is not None:
                noisy.append((statement, kept.clean_fallback))
        elif parser.parse(text).root_node.has_error:
            failing_others.append(statement)
    return _Isolation(noisy, failing_others, recoverable)


def _quietest(
    parser: Parser,
    isolated: bytearray,
    isolation: _Isolation,
    expected: list[int],
    best: Tree,
    best_found: int,
) -> Tree:
    # Each step quiets more, at a cost: a hollowed body loses its calls, a
    # blanked statement its own content. Stop once every function that
    # parses on its own is found.
    for step in range(3):
        if step == 1:
            for statement, fallback in isolation.noisy:
                isolated[statement.start : statement.end] = fallback
        elif step == 2:
            for statement in isolation.failing_others:
                _blank(isolated, statement.start, statement.end)
        retry = parser.parse(bytes(isolated))
        if (found := _found(retry, expected)) > best_found:
            best, best_found = retry, found
        if best_found >= isolation.recoverable:
            break
    return best


def lost_sql_functions(source: bytes, root: Node) -> int:
    """How many of `source`'s `CREATE FUNCTION` statements `root` lacks."""
    expected = [s.lead for s in _statements(source) if s.is_function]
    return len(expected) - len(set(expected) & _function_starts(root))


def _standalone_function(
    parser: Parser, statement: _Statement, text: bytes
) -> _Standalone:
    # The statement as it parses on its own: as written; else with each
    # dollar-quoted body turned into an equally long string literal, which
    # the grammar takes as a body (`AS '...'`) with no PL/pgSQL to trip on;
    # else blanked whole.
    raw = parser.parse(text).root_node
    hollow = bytearray(text)
    for lo, hi in statement.bodies:
        _hollow(hollow, lo - statement.start, hi - statement.start)
    hollow_root = parser.parse(bytes(hollow)).root_node
    clean_hollow = (
        bytes(hollow)
        if _function_starts(hollow_root) and not hollow_root.has_error
        else None
    )
    if _function_starts(raw):
        return _Standalone(text, True, None if not raw.has_error else clean_hollow)
    if _function_starts(hollow_root):
        return _Standalone(bytes(hollow), True, None)
    _blank(hollow, 0, len(hollow))
    return _Standalone(bytes(hollow), False, None)


def _found(tree: Tree, expected: list[int]) -> int:
    starts = _function_starts(tree.root_node)
    return sum(lead in starts for lead in expected)


def _function_starts(root: Node) -> set[int]:
    # Start offsets of every `create_function`, ERROR regions included.
    starts: set[int] = set()
    cursor = root.walk()
    while True:
        node = cursor.node
        if node is not None and node.type == cs.TS_SQL_CREATE_FUNCTION:
            starts.add(node.start_byte)
        if cursor.goto_first_child():
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                return starts


def _hollow(buffer: bytearray, lo: int, hi: int) -> None:
    # A dollar-quoted body becomes a string literal of the same length.
    _blank(buffer, lo, hi)
    buffer[lo] = buffer[hi - 1] = _QUOTE


def _blank(buffer: bytearray, lo: int, hi: int) -> None:
    for i in range(lo, hi):
        if buffer[i] != _NEWLINE:
            buffer[i] = _BLANK


def _statements(source: bytes) -> list[_Statement]:
    # Top-level statements split on `;` outside comments, strings, quoted
    # identifiers and dollar quotes; each with the content spans of its
    # dollar-quoted bodies.
    masked, bodies = _lex(source)
    n = len(source)
    statements: list[_Statement] = []
    start = 0
    body_index = 0
    for end in (*(m.end() for m in _SEMICOLON.finditer(masked)), n):
        if start >= end:
            continue
        lead = start + len(masked[start:end]) - len(masked[start:end].lstrip())
        if lead < end:
            own: list[tuple[int, int]] = []
            while body_index < len(bodies) and bodies[body_index][0] < end:
                own.append(bodies[body_index])
                body_index += 1
            statements.append(
                _Statement(
                    start,
                    end,
                    lead,
                    bool(_CREATE_FUNCTION.match(masked, start, end)),
                    tuple(own),
                )
            )
        start = end
    return statements


def _lex(source: bytes) -> tuple[bytearray, list[tuple[int, int]]]:
    # `source` with comments, strings and dollar quotes blanked, and the
    # span of each dollar-quoted body, delimiters included.
    masked = bytearray(source)
    bodies: list[tuple[int, int]] = []
    i = 0
    while (lexical := _LEXICAL_START.search(source, i)) is not None:
        start = lexical.start()
        i, blanked, is_body = _lexical_end(source, start)
        if is_body:
            bodies.append((start, i))
        if blanked:
            _blank(masked, start, i)
    return masked, bodies


def _lexical_end(source: bytes, i: int) -> tuple[int, bool, bool]:
    # Where the comment, string, quoted identifier or dollar quote starting
    # at `i` ends, whether it is blanked and whether it is a dollar-quoted
    # body; (i + 1, False, False) when the byte starts none of them.
    if source.startswith(b"--", i):
        end = source.find(b"\n", i)
        return (len(source) if end < 0 else end), True, False
    if source.startswith(b"/*", i):
        return _block_comment_end(source, i), True, False
    if source[i] == 0x27:  # '
        escapes = _is_escape_string(source, i)
        return _skip_quoted(source, i, 0x27, escapes=escapes), True, False
    if source[i] == 0x22:  # "
        return _skip_quoted(source, i, 0x22, escapes=False), False, False
    if (i == 0 or not _IDENT_BYTE.match(source, i - 1)) and (
        tag := _DOLLAR_TAG.match(source, i)
    ):
        close = source.find(tag.group(), tag.end())
        return (len(source) if close < 0 else close + len(tag.group())), True, True
    return i + 1, False, False


def _block_comment_end(source: bytes, i: int) -> int:
    # Block comments nest.
    n = len(source)
    depth, j = 1, i + 2
    while j < n and depth:
        if source.startswith(b"/*", j):
            depth, j = depth + 1, j + 2
        elif source.startswith(b"*/", j):
            depth, j = depth - 1, j + 2
        else:
            j += 1
    return j


def _is_escape_string(source: bytes, quote: int) -> bool:
    # `E'..\'..'`: a backslash escapes the next byte.
    return (
        quote > 0
        and source[quote - 1] in b"Ee"
        and not (quote > 1 and _IDENT_BYTE.match(source, quote - 2))
    )


def _skip_quoted(source: bytes, i: int, quote: int, *, escapes: bool) -> int:
    # The offset just past the quoted run opening at `i`; a doubled quote
    # embeds one.
    n = len(source)
    j = i + 1
    while j < n:
        if escapes and source[j] == 0x5C:  # backslash
            j += 2
        elif source[j] == quote:
            if j + 1 < n and source[j + 1] == quote:
                j += 2
            else:
                return j + 1
        else:
            j += 1
    return n
