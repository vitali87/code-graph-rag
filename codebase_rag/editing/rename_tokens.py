"""Where a rename's name token sits in source (issue #1532): the
definition's own name, the callee of a call site (never its arguments), the
links of a fluent chain, the closing tag of a JSX element and the overload
declarations folded into an implementation. Split from `rename.py`.
"""

import re
from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..parsers.js_ts.overloads import overload_signatures
from ..parsers.python.overloads import overload_stub_names
from .patcher import Patcher, byte_to_line_col, line_col_to_byte
from .rename_spec import IDENTIFIER_RE, TS_LANGUAGES, RenameSite
from .sites import call_node_at, calls_starting_at

# A call site whose recorded position no longer holds a call: the source
# changed after indexing, so the site must be refused, not guessed at.
STALE_CALL = (-1, -1)
_MEMBER_NAME_TYPES = frozenset(
    {cs.TS_IDENTIFIER, cs.TS_PROPERTY_IDENTIFIER, "field_identifier"}
)


def name_token(
    source: bytes,
    language: cs.SupportedLanguage | None,
    start_line: int,
    end_line: int,
    name: str,
) -> tuple[int, int] | None:
    """(line, col) of the definition's own name identifier inside its span."""
    parser = None
    if language is not None:
        parsers, _queries = load_parsers()
        parser = parsers.get(language)
    if parser is not None:
        root = parser.parse(source).root_node
        stack: list[Node] = [root]
        while stack:
            node = stack.pop()
            if node.end_point[0] + 1 < start_line or node.start_point[0] + 1 > end_line:
                continue
            named = node.child_by_field_name(cs.FIELD_NAME)
            if (
                named is not None
                and named.text is not None
                and named.text.decode(cs.ENCODING_UTF8, errors="replace") == name
                and named.start_point[0] + 1 >= start_line
            ):
                return named.start_point[0] + 1, named.start_point[1]
            stack.extend(node.children)
        return None
    # No grammar: the first whole-word occurrence inside the span.
    text = source.decode(cs.ENCODING_UTF8, errors="replace")
    lines = text.split("\n")
    for number in range(start_line, min(end_line, len(lines)) + 1):
        match = re.search(IDENTIFIER_RE % re.escape(name), lines[number - 1])
        if match:
            return number, len(
                lines[number - 1][: match.start()].encode(cs.ENCODING_UTF8)
            )
    return None


def _callee_span(
    source: bytes,
    language: cs.SupportedLanguage | None,
    line: int,
    col: int,
    end_line: int | None = None,
    end_col: int | None = None,
) -> tuple[int, int] | None:
    """Byte span of the callee expression of the site's call at (line, col).

    `helper(helper(1))` and `helper(2).upper()` both start at the same point
    as an inner call, and so do both links of `obj.helper(1).helper(2)`: the
    site's call is the one ending where the graph recorded the site's end,
    or, without a recorded end, the outermost one.
    """
    if language is None:
        return None
    parsers, _queries = load_parsers()
    parser = parsers.get(language)
    if parser is None:
        return None
    root = parser.parse(source).root_node
    recorded_end = (
        (end_line - 1, end_col)
        if end_line is not None and end_col is not None
        else None
    )
    best = call_node_at(root, line, col, recorded_end)
    # `call_node_at` picks among calls that have a callee, so there is one
    # whenever there is a call.
    func = best.child_by_field_name(cs.FIELD_FUNCTION) if best is not None else None
    if func is None:
        return None
    return func.start_byte, func.end_byte


def _calls_start_at(
    source: bytes, language: cs.SupportedLanguage | None, line: int, col: int
) -> bool:
    if language is None:
        return False
    parsers, _queries = load_parsers()
    parser = parsers.get(language)
    if parser is None:
        return False
    return bool(calls_starting_at(parser.parse(source).root_node, line - 1, col))


def chain_links(
    source: bytes,
    language: cs.SupportedLanguage | None,
    line: int,
    col: int,
    name: str,
) -> list[tuple[int, int]]:
    """(line, col) of `name` as the member called by EVERY call starting at
    (line, col): the links of a fluent chain `obj.name(1).name(2)`.

    The index keeps one call row per start position, so the chain's later
    links have no row of their own; the caller decides what to do with them.
    """
    if language is None:
        return []
    parsers, _queries = load_parsers()
    parser = parsers.get(language)
    if parser is None:
        return []
    root = parser.parse(source).root_node
    # Each link's own site starts at its name (issue #3166); the links of
    # its chain are the calls that start where the site's call starts.
    starts = {call.start_point for call in calls_starting_at(root, line - 1, col)}
    starts = starts or {(line - 1, col)}
    found: list[tuple[int, int]] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if (
            node.start_point in starts
            and (member := _called_member(node, name)) is not None
        ):
            found.append((member.start_point[0] + 1, member.start_point[1]))
        if any(node.start_point[0] <= row <= node.end_point[0] for row, _ in starts):
            stack.extend(node.children)
    return sorted(found)


def _called_member(call: Node, name: str) -> Node | None:
    # The member name a call invokes (`name` in `obj.name(...)`), if it is
    # `name`; None for a bare call or another member.
    func = call.child_by_field_name(cs.FIELD_FUNCTION)
    member = func.children[-1] if func is not None and func.children else None
    if (
        member is not None
        and member.type in _MEMBER_NAME_TYPES
        and member.text is not None
        and member.text.decode(cs.ENCODING_UTF8, errors="replace") == name
    ):
        return member
    return None


def jsx_closing_token(
    source: bytes,
    language: cs.SupportedLanguage | None,
    token: tuple[int, int],
) -> tuple[int, int] | None:
    """(line, col) of the same name in the closing tag of the JSX element
    whose opening tag holds `token`, or None.

    The graph records one use per element, at its opening tag, so renaming
    `Frame` left `<Box>{children}</Frame>`, which no compiler accepts
    (issue #2811). The closing tag repeats the opening tag's name verbatim
    (`</Ns.Card>` for `<Ns.Card>`), so the token sits at the same offset in
    it. A self-closing element has no closing tag.
    """
    if language not in cs.JS_TS_LANGUAGES:
        return None
    parsers, _queries = load_parsers()
    parser = parsers.get(language)
    if parser is None:
        return None
    tags = _jsx_tag_names(parser.parse(source).root_node, token)
    if tags is None:
        return None
    opening, closing = tags
    offset = line_col_to_byte(source, *token)
    if (
        closing.text != opening.text
        or not opening.start_byte <= offset < opening.end_byte
    ):
        return None
    return byte_to_line_col(source, closing.start_byte + offset - opening.start_byte)


def _jsx_tag_names(root: Node, token: tuple[int, int]) -> tuple[Node, Node] | None:
    """The name nodes of the opening and the closing tag of the JSX element
    whose opening tag holds `token`; None outside one, and for a
    self-closing element, which has no closing tag."""
    point = (token[0] - 1, token[1])
    node = root.descendant_for_point_range(point, point)
    while node is not None and node.type != cs.TS_JSX_OPENING_ELEMENT:
        node = node.parent
    if node is None or node.parent is None or node.parent.type != cs.TS_JSX_ELEMENT:
        return None
    opening = node.child_by_field_name(cs.FIELD_NAME)
    closing_tag = node.parent.child_by_field_name(cs.FIELD_JSX_CLOSE_TAG)
    closing = (
        closing_tag.child_by_field_name(cs.FIELD_NAME)
        if closing_tag is not None
        else None
    )
    if opening is None or closing is None:
        return None
    return opening, closing


def last_identifier(
    source: bytes,
    line: int,
    col: int,
    end_line: int | None,
    end_col: int | None,
    name: str,
    language: cs.SupportedLanguage | None = None,
    is_call: bool = False,
) -> tuple[int, int] | None:
    """(line, col) of the token to rename inside a site span.

    For a call site the token is the rightmost `name` inside the OUTERMOST
    call's callee expression (`pkg.helper`, `Circle().area`), never inside
    its arguments; for any other site it is the rightmost `name` in the span.
    """
    start = line_col_to_byte(source, line, col)
    # A site with no recorded end gets a span that covers the name, for the
    # text fallback only: handed to the call lookup it would read as a
    # recorded end that no call matches, and mark a live call stale.
    end = line_col_to_byte(
        source,
        end_line if end_line is not None else line,
        end_col if end_col is not None else col + len(name),
    )
    callee = _callee_span(source, language, line, col, end_line, end_col)
    if callee is None and is_call and _calls_start_at(source, language, line, col):
        # Calls DO start at the recorded position but none ends where the
        # site recorded its end: the index is stale here. The no-grammar
        # fallback below would cut at the last `(` and pick the INNER callee
        # of `helper(helper(1))`. A position no call starts at
        # (a grammar whose call node has no `function` field, such as Java's
        # method_invocation) keeps the fallback.
        return STALE_CALL
    if callee is not None and callee[0] <= start < callee[1]:
        # The callee ends the token search, whether the site starts with it
        # or at its member name (issue #3166): never inside the arguments.
        end = callee[1]
    text = source[start:end].decode(cs.ENCODING_UTF8, errors="replace")
    if callee is None:
        # No grammar: cut at the last opening parenthesis so the arguments
        # of a plain call are excluded (`helper(helper=2)`).
        paren = text.rfind("(")
        if paren >= 0:
            text = text[:paren]
    matches = list(re.finditer(IDENTIFIER_RE % re.escape(name), text))
    if not matches:
        return None
    offset = start + len(text[: matches[-1].start()].encode(cs.ENCODING_UTF8))
    return byte_to_line_col(source, offset)


def overload_stub_sites(definition: RenameSite, patcher: Patcher) -> list[RenameSite]:
    """The name tokens of the overload declarations a definition owns.

    The graph folds Python's `@overload` stubs (issue #2590) and TypeScript's
    overload signatures (issue #2457) into their implementation, so no node
    names them; renaming only the implementation would strand them under the
    old name, which for TypeScript is a type error (TS2389).
    """
    language = get_language_for_extension(Path(definition.path).suffix)
    if language != cs.SupportedLanguage.PYTHON and language not in TS_LANGUAGES:
        return []
    parsers, _queries = load_parsers()
    parser = parsers.get(language)
    if parser is None:
        return []
    root = parser.parse(patcher.source(definition.path)).root_node
    point = (definition.line - 1, definition.col)
    token = root.descendant_for_point_range(point, point)
    function = token.parent if token is not None else None
    if function is None:
        return []
    if language in TS_LANGUAGES:
        names = [
            name
            for signature in overload_signatures(function)
            if (name := signature.child_by_field_name(cs.FIELD_NAME)) is not None
        ]
    elif function.type == cs.TS_PY_FUNCTION_DEFINITION:
        names = overload_stub_names(function, root)
    else:
        return []
    return [
        definition._replace(line=name.start_point[0] + 1, col=name.start_point[1])
        for name in names
    ]
