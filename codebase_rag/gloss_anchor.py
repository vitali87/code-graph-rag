"""The text-quote anchor of a Gloss (issue #1808, stage five).

A note records its subject's `anchor_hash` so a same-name move can be
followed by content, but the hash keeps the definition's name (a signature
change is when a note should go stale), so a RENAME reads as no match. This
is the tier below it: a digest of the definition's tokens with its name
masked out, plus digests of the non-blank lines either side of it. A
renamed definition keeps its quote; only the body matters. The prefix and
suffix are tie-breakers between definitions whose bodies are identical
(`return None` is a common body), never a match on their own.

The tokens are tree-sitter leaves, the way `anchor_hash` walks them, so
the quote answers the same edits the hash does: whitespace between tokens
and comments are not part of it, while a string literal's text is kept
exactly as written -- `return "run"` and `return "execute"` are different
bodies, and so are `"a  b"` and `"a b"` (bot review on PR #1966). Only a
token equal to the definition's name is masked, so a name inside a literal
stays what it is. Without a parser for the file there is no quote: a
text-only reading could not tell a literal from code.

Digests rather than the text itself, the SonarQube precedent (issues are
tracked across scans by a line hash "excluding white spaces"): a note on a
long function would otherwise carry a copy of it, and comparing is all this
tier does. Versioned, so a later format is never compared to this one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node, Parser, Tree

from . import constants as cs
from .language_spec import get_language_for_extension


class ParsedSource(NamedTuple):
    """A file's text and its parse; `tree` is None when no grammar covers it."""

    text: str
    tree: Tree | None


# (project name, path relative to that project's root) -> the file, or None
# when the caller cannot read it (another project's checkout, a file gone
# from disk). Scoped by project so a note about one project is never
# matched against a same-named path under another's root.
SourceReader = Callable[[str, str], ParsedSource | None]

_MASK = "\x00"
# A leaf under an ancestor of one of these kinds is literal text: kept
# verbatim and never masked. Substrings, because every grammar spells its
# literals differently (`string`, `string_literal`, `template_string`,
# `interpreted_string_literal`, `encapsed_string`, `char_literal`).
_LITERAL_KINDS = ("string", "char", "template", "heredoc", "encapsed")


class TextAnchor(NamedTuple):
    quote: str
    prefix: str
    suffix: str


def _digest(text: str) -> str:
    return f"{cs.ANCHOR_QUOTE_VERSION}{hashlib.sha256(text.encode(cs.ENCODING_UTF8)).hexdigest()}"


def _context(text: str) -> str:
    return " ".join(text.split())


def _leaves_in_rows(tree: Tree, first_row: int, last_row: int) -> Iterator[Node]:
    # Iterative, deep trees overflow recursion; pruned by row so a large
    # file costs its size once, not per definition.
    stack: list[Node] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.end_point[0] < first_row or node.start_point[0] > last_row:
            continue
        if node.child_count == 0:
            if node.start_point[0] >= first_row:
                yield node
            continue
        stack.extend(reversed(node.children))


def _kind(leaf: Node, root: Node) -> str:
    """ "literal", "comment" or "code" for one leaf, from its ancestors."""
    current: Node | None = leaf
    while current is not None and current != root:
        kind = current.type
        if cs.AST_FP_COMMENT_SUBSTRING in kind:
            return "comment"
        if current != leaf and any(k in kind for k in _LITERAL_KINDS):
            return "literal"
        current = current.parent
    return "code"


def _quote_tokens(
    tree: Tree, name: str | None, first_row: int, last_row: int
) -> list[str]:
    tokens: list[str] = []
    for leaf in _leaves_in_rows(tree, first_row, last_row):
        kind = _kind(leaf, tree.root_node)
        if kind == "comment":
            continue
        text = (leaf.text or b"").decode(cs.ENCODING_UTF8, errors="replace")
        if kind == "code" and name and text == name:
            text = _MASK
        tokens.append(text)
    return tokens


def text_anchor(
    source: ParsedSource, name: str | None, start_line: int, end_line: int
) -> TextAnchor | None:
    """The anchor of the definition on `start_line`..`end_line` (1-based,
    inclusive), or None when the span does not fit the source or the file
    has no parse."""
    lines = source.text.splitlines()
    if source.tree is None or not (1 <= start_line <= end_line <= len(lines)):
        return None
    tokens = _quote_tokens(source.tree, name, start_line - 1, end_line - 1)
    if not tokens:
        return None
    before = [line for line in lines[: start_line - 1] if line.strip()]
    after = [line for line in lines[end_line:] if line.strip()]
    return TextAnchor(
        quote=_digest("\x1f".join(tokens)),
        prefix=_digest(_context("\n".join(before[-cs.ANCHOR_CONTEXT_LINES :]))),
        suffix=_digest(_context("\n".join(after[: cs.ANCHOR_CONTEXT_LINES]))),
    )


def is_comparable_quote(value: object) -> bool:
    return isinstance(value, str) and value.startswith(cs.ANCHOR_QUOTE_VERSION)


def parse_source(
    parsers: Mapping[cs.SupportedLanguage, Parser], path: Path, text: str
) -> Tree | None:
    """`text` parsed with the grammar for `path`'s extension, or None."""
    language = get_language_for_extension(path.suffix)
    parser = parsers.get(language) if language is not None else None
    if parser is None:
        return None
    try:
        return parser.parse(text.encode(cs.ENCODING_UTF8))
    except (ValueError, TypeError):
        return None
