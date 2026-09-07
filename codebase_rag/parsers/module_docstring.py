"""Module-level documentation, per language (issue #1789).

Every language has a place for "what this file is about", but no two agree on
what it looks like, and the grammars do not distinguish it from an ordinary
comment: tree-sitter reports Rust's `//!` crate doc and a throwaway `// note`
both as `line_comment`, and Go's package comment and a `// TODO` both as
`comment`. Node type therefore cannot decide, and the marker prefix must.

So each language declares a `ModuleDocSpec`: which node types can carry a doc,
which prefixes mark one, and whether consecutive line comments join into a
single block. Languages with no module-doc convention declare nothing and are
skipped rather than guessing -- reporting a licence header as a module
docstring is worse than reporting none.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from ..constants import SupportedLanguage
from ..types_defs import ASTNode
from .utils import safe_decode_with_fallback


class ModuleDocSpec(NamedTuple):
    """How one language marks the documentation of a whole file.

    `line_types`/`block_types` are node types, `line_markers`/`block_markers`
    the prefixes that make such a node a doc rather than an ordinary comment.
    A node type with no marker listed is never treated as documentation.
    """

    line_types: frozenset[str] = frozenset()
    line_markers: tuple[str, ...] = ()
    block_types: frozenset[str] = frozenset()
    block_markers: tuple[str, ...] = ()
    # Go only: the comment must sit immediately above `package foo`, with no
    # blank line. A detached comment is a licence header by convention.
    anchor_types: frozenset[str] = frozenset()
    # PHP opens with `<?php`; the doc comment follows it.
    skip_types: frozenset[str] = frozenset()


_C_STYLE_BLOCK = frozenset({"comment", "block_comment"})
# `/**` is Javadoc/JSDoc/Doxygen. A bare `/*` is a licence header or a note,
# and `/*!` is Doxygen's alternative form.
_DOC_BLOCK_MARKERS = ("/**", "/*!")
# `///` is the line form of the same convention. Not valid for Rust, where
# `///` documents the NEXT ITEM and `//!` documents the enclosing module.
_DOC_LINE_MARKERS = ("///",)

_C_STYLE = ModuleDocSpec(
    line_types=frozenset({"comment", "line_comment"}),
    line_markers=_DOC_LINE_MARKERS,
    block_types=_C_STYLE_BLOCK,
    block_markers=_DOC_BLOCK_MARKERS,
)

MODULE_DOC_SPECS: dict[SupportedLanguage, ModuleDocSpec] = {
    # Rust draws the distinction the others do not: `//!` and `/*!` are INNER
    # docs describing the enclosing module, `///` describes the next item.
    # Treating `///` as a module doc would attribute a function's own
    # documentation to its file.
    SupportedLanguage.RUST: ModuleDocSpec(
        line_types=frozenset({"line_comment"}),
        line_markers=("//!",),
        block_types=frozenset({"block_comment"}),
        block_markers=("/*!",),
    ),
    # Go has no marker at all: the package comment is an ordinary `//` comment
    # that happens to sit immediately above `package foo`. The blank line is
    # the whole distinction, and it is why a licence header does not count.
    SupportedLanguage.GO: ModuleDocSpec(
        line_types=frozenset({"comment"}),
        line_markers=("//",),
        anchor_types=frozenset({"package_clause"}),
    ),
    SupportedLanguage.JAVA: _C_STYLE,
    SupportedLanguage.SCALA: _C_STYLE,
    SupportedLanguage.JS: _C_STYLE,
    SupportedLanguage.TS: _C_STYLE,
    SupportedLanguage.TSX: _C_STYLE,
    SupportedLanguage.C: _C_STYLE,
    SupportedLanguage.CPP: _C_STYLE,
    SupportedLanguage.CSHARP: _C_STYLE,
    SupportedLanguage.DART: ModuleDocSpec(
        line_types=frozenset({"documentation_comment", "comment"}),
        line_markers=_DOC_LINE_MARKERS,
        block_types=_C_STYLE_BLOCK,
        block_markers=_DOC_BLOCK_MARKERS,
    ),
    SupportedLanguage.PHP: ModuleDocSpec(
        line_types=frozenset({"comment"}),
        line_markers=_DOC_LINE_MARKERS,
        block_types=_C_STYLE_BLOCK,
        block_markers=_DOC_BLOCK_MARKERS,
        # `<?php` is the first child of every PHP file.
        skip_types=frozenset({"php_tag", "text_interpolation"}),
    ),
    # LuaDoc/LDoc use `---`. A plain `--` is an ordinary comment.
    SupportedLanguage.LUA: ModuleDocSpec(
        line_types=frozenset({"comment"}),
        line_markers=("---",),
    ),
    # SQL has no module-documentation convention -- a leading `--` is as
    # likely to be a commented-out statement -- so nothing is extracted.
}

# Leading decoration on each line of a block comment: ` * text` -> `text`.
_BLOCK_LINE_PREFIX = re.compile(r"^\s*\*+/?\s?")
# The delimiters themselves, stripped before the per-line pass.
_BLOCK_OPEN = re.compile(r"^/\*+!?")
_BLOCK_CLOSE = re.compile(r"\*+/$")


def _strip_line(text: str, markers: tuple[str, ...]) -> str:
    # Longest first, so that adding a marker that is a prefix of another (a
    # `--` beside Lua's `---`) strips the longer one rather than leaving a
    # stray delimiter. No spec needs this yet; every one has a single marker.
    for marker in sorted(markers, key=lambda candidate: -len(candidate)):
        if text.startswith(marker):
            return text[len(marker) :].strip()
    return text.strip()


def _clean_block(text: str) -> str:
    body = _BLOCK_CLOSE.sub("", _BLOCK_OPEN.sub("", text))
    lines = [_BLOCK_LINE_PREFIX.sub("", line) for line in body.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line.rstrip() for line in lines).strip()


def _is_marked(text: str, markers: tuple[str, ...]) -> bool:
    return any(text.startswith(marker) for marker in markers)


def extract_module_docstring(root: ASTNode, language: SupportedLanguage) -> str | None:
    """The documentation for the file as a whole, or None.

    None rather than a guess: a licence header, a `// TODO`, or a
    commented-out statement are all common in the same position, and
    recording one as the file's documentation is a wrong answer that reads
    like a right one.
    """
    spec = MODULE_DOC_SPECS.get(language)
    if spec is None:
        return None

    children = list(root.children)
    index = 0
    while index < len(children) and children[index].type in spec.skip_types:
        index += 1
    if index >= len(children):
        return None

    node = children[index]
    text = safe_decode_with_fallback(node).strip()

    if node.type in spec.block_types and _is_marked(text, spec.block_markers):
        if spec.anchor_types and not _anchored(children, index, node, spec):
            return None
        return _clean_block(text) or None

    if node.type not in spec.line_types or not _is_marked(text, spec.line_markers):
        return None

    # Consecutive line comments form one doc block: `//!` in Rust and `///` in
    # Dart are written a line at a time, and taking only the first would
    # truncate every multi-line module doc to its opening sentence.
    lines: list[str] = []
    last = node
    for candidate in children[index:]:
        if candidate.type not in spec.line_types:
            break
        candidate_text = safe_decode_with_fallback(candidate).strip()
        if not _is_marked(candidate_text, spec.line_markers):
            break
        # A blank line between comments ends the block.
        if lines and candidate.start_point[0] > _content_end_row(last) + 1:
            break
        lines.append(_strip_line(candidate_text, spec.line_markers))
        last = candidate

    if spec.anchor_types and not _anchored(children, index, last, spec):
        return None

    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip() or None


def _content_end_row(node: ASTNode) -> int:
    """The last row carrying text, which is not always `end_point`.

    Rust's `line_comment` includes its trailing newline, so `end_point` is the
    row AFTER the comment, and a gap measured from it counts one line short --
    a blank line between two `//!` paragraphs would read as adjacent. Every
    other grammar here ends the node on the text. Measuring from the text
    itself is right for both.
    """
    text = safe_decode_with_fallback(node)
    trailing = len(text) - len(text.rstrip("\r\n"))
    return node.end_point[0] - (1 if trailing else 0)


def _anchored(
    children: list[ASTNode], index: int, last: ASTNode, spec: ModuleDocSpec
) -> bool:
    """Whether the comment sits immediately above the anchor (Go).

    A blank line between the comment and `package foo` means the comment is a
    licence header, which Go tooling does not treat as package documentation
    either.
    """
    end_row = _content_end_row(last)
    for candidate in children[index:]:
        if candidate.start_point[0] <= end_row:
            continue
        if candidate.type not in spec.anchor_types:
            return False
        return candidate.start_point[0] == end_row + 1
    return False


__all__ = ["MODULE_DOC_SPECS", "ModuleDocSpec", "extract_module_docstring"]
