"""Documentation comments attached to a definition, for every language.

The file-level counterpart lives in `module_docstring`. The two answer
opposite halves of ONE question: a doc comment at the top of a file either
describes the file or describes the first declaration in it, and
`module_docstring._documents_declaration` is the predicate that decides.
That module returns None when the comment belongs to the declaration; this
one is what collects it.

Python is not handled here. A Python docstring is not a comment -- it parses
as `expression_statement -> string` INSIDE the definition's body -- so
`DefinitionProcessor._get_docstring` keeps that path and this module covers
the comment-based languages.
"""

from __future__ import annotations

from ..constants import SupportedLanguage
from ..types_defs import ASTNode
from .module_docstring import (
    MODULE_DOC_SPECS,
    ModuleDocSpec,
    _clean_block,
    _is_directive,
    _is_marked,
    _is_separator,
    _strip_line,
)
from .utils import safe_decode_with_fallback

# Outer doc markers: the forms that document the NEXT item rather than the
# enclosing one. For most languages these are the same strings the module
# table uses, because `/**` documents whatever follows it wherever it sits.
# Rust is the exception and the reason this table exists at all.
_OUTER_LINE_MARKERS = ("///",)
_OUTER_BLOCK_MARKERS = ("/**", "/*!")

# Node types that legally sit BETWEEN a doc comment and the declaration it
# documents, per the grammars. C# allows attributes, Dart metadata, Rust
# outer attributes, Java/Scala annotations. A walk that stops at the first
# non-comment sibling misses every annotated declaration.
_INTERLEAVED: dict[SupportedLanguage, frozenset[str]] = {
    SupportedLanguage.RUST: frozenset({"attribute_item"}),
    SupportedLanguage.JAVA: frozenset({"annotation", "marker_annotation", "modifiers"}),
    SupportedLanguage.SCALA: frozenset({"annotation"}),
    SupportedLanguage.CSHARP: frozenset({"attribute_list"}),
    SupportedLanguage.DART: frozenset({"annotation", "metadata"}),
}


def _definition_spec(language: SupportedLanguage) -> ModuleDocSpec | None:
    """The module spec with its markers switched to the outer forms.

    Derived from `MODULE_DOC_SPECS` rather than written out again so a
    language's node types and skip rules cannot drift between the two
    levels. Only the markers differ, and only for Rust do they differ in a
    way that changes behaviour.
    """
    spec = MODULE_DOC_SPECS.get(language)
    if spec is None:
        return None
    if language == SupportedLanguage.RUST:
        # `//!` documents the enclosing module; `///` documents the next
        # item. Reusing the module markers here would look for `//!` above a
        # `struct` and find nothing, silently.
        return spec._replace(
            line_markers=_OUTER_LINE_MARKERS,
            block_markers=_OUTER_BLOCK_MARKERS,
        )
    # Go needs nothing: its doc comment is an ordinary `//` at both levels and
    # the module spec's `anchor_types` is consulted only by the module walk.
    # An earlier version cleared it here "or every definition doc is
    # rejected" -- untrue, this module never reads the field, and the
    # self-audit that found it is the same one that found four unreachable
    # `_INTERLEAVED` entries: a rule nothing executes is a claim, not a guard.
    return spec


def _doc_block_start(
    siblings: list[ASTNode],
    target_index: int,
    spec: ModuleDocSpec,
    interleaved: frozenset[str],
) -> int | None:
    """Index of the first comment of the block documenting `siblings[target_index]`.

    Walks BACKWARD, which is the whole difference from the module case: a
    file's documentation is found by scanning forward from the top, a
    definition's by scanning up from the definition.

    Returns None when the declaration has no doc comment above it, including
    when what sits above is an ordinary comment, a separator rule, a
    directive, or a doc comment separated by a blank line -- a detached
    comment describes the file or nothing, which is the judgement
    `module_docstring` already makes from the other side.
    """
    index = target_index - 1
    # Attributes, annotations and metadata legally sit between the doc and
    # its declaration. C# `[Obsolete]`, Dart `@override`, Rust
    # `#[derive(Debug)]`, a Java annotation: a walk that stopped here would
    # report every annotated declaration as undocumented.
    while index >= 0 and siblings[index].type in interleaved:
        index -= 1
    if index < 0:
        return None

    last = siblings[index]
    text = safe_decode_with_fallback(last).strip()
    is_line = last.type in spec.line_types and _is_marked(text, spec.line_markers)
    is_block = last.type in spec.block_types and _is_marked(text, spec.block_markers)
    if not (is_line or is_block):
        return None
    # Adjacency decides ownership, exactly as it does for the file-level
    # question: a doc comment touching a declaration belongs to it, a
    # detached one does not. Measured against the declaration the comment
    # precedes, skipping whatever interleaved nodes sit between them.
    if siblings[index + 1].start_point[0] - _last_row(last) > 1:
        return None
    if is_block:
        return index
    # A line-comment doc is written a line at a time, so walk up through the
    # consecutive marked comments to find where the block opens.
    while index - 1 >= 0:
        above = siblings[index - 1]
        above_text = safe_decode_with_fallback(above).strip()
        if above.type not in spec.line_types:
            break
        if not _is_marked(above_text, spec.line_markers):
            break
        if siblings[index].start_point[0] - _last_row(above) > 1:
            break
        index -= 1
    return index


def _last_row(node: ASTNode) -> int:
    """The node's last row, ignoring a trailing newline in its extent.

    A block comment's `end_point` sits on the closing `*/`; a line comment's
    may land on the following row when the grammar includes the newline.
    Measuring adjacency from an overshot row makes a touching comment look
    detached.
    """
    row, column = node.end_point
    return row - 1 if column == 0 and row > node.start_point[0] else row


def extract_definition_docstring(
    node: ASTNode, language: SupportedLanguage
) -> str | None:
    """The documentation comment attached to this definition, or None.

    None rather than a guess, for the same reason the module extractor
    refuses one: an ordinary comment, a separator rule, a commented-out
    statement and a licence header all sit in the same position, and
    recording one as a definition's documentation is a wrong answer that
    reads like a right one.

    Python is not handled here -- see the module docstring.
    """
    spec = _definition_spec(language)
    if spec is None:
        return None
    parent = node.parent
    if parent is None:
        return None

    siblings = list(parent.children)
    target = _target_index(siblings, node)
    if target is None:
        return None

    interleaved = _INTERLEAVED.get(language, frozenset())
    start = _doc_block_start(siblings, target, spec, interleaved)
    if start is None:
        return None

    opener = siblings[start]
    text = safe_decode_with_fallback(opener).strip()
    if opener.type in spec.block_types and _is_marked(text, spec.block_markers):
        cleaned = _clean_block(text)
        return None if not cleaned or _is_directive(cleaned) else cleaned
    return _line_doc_for(siblings, start, target, spec)


def _target_index(siblings: list[ASTNode], node: ASTNode) -> int | None:
    """Where `node` sits among its siblings, by source position.

    NOT by `is`: `parent.children` builds fresh wrapper objects on every
    access, so the node handed to us is never identical to the one in the
    list we just fetched -- measured, `sibs[2] is node` is False while
    `sibs[2] == node` is True. An identity search silently found nothing and
    every definition read as undocumented.

    NOT by `==` either, which is value equality on type and extent: two
    siblings can share both (an empty `declaration` repeated, Dart's split
    signature/body) and the first match would win. The start point plus the
    type is unique among one parent's children, because two siblings cannot
    begin at the same byte.
    """
    key = (node.start_point, node.end_point, node.type)
    matches = [
        index
        for index, sibling in enumerate(siblings)
        if (sibling.start_point, sibling.end_point, sibling.type) == key
    ]
    # Every match, then require exactly one, rather than taking the first.
    # A first-match-wins lookup over a set that can hold two is the defect
    # shape a peer hit on #1835: the concrete case happened to work because
    # the wanted row came back first, and nothing guaranteed it would. Here a
    # second match would mean the key is not unique among one parent's
    # children after all -- measured as unique across the annotated and
    # attributed forms in Dart, Java, C and PHP, which is evidence and not a
    # proof -- so return None and extract nothing rather than silently
    # documenting whichever sibling came back first.
    return matches[0] if len(matches) == 1 else None


def _line_doc_for(
    siblings: list[ASTNode], start: int, target: int, spec: ModuleDocSpec
) -> str | None:
    """The content of the line-comment block from `start` up to `target`."""
    lines: list[str] = []
    for candidate in siblings[start:target]:
        if candidate.type not in spec.line_types:
            continue
        text = safe_decode_with_fallback(candidate).strip()
        if not _is_marked(text, spec.line_markers):
            continue
        # Decoration and machine directives sit in the same place as prose and
        # are neither; skipped rather than ending the block, so a rule above a
        # real doc comment does not truncate it.
        if _is_separator(text, spec.line_markers) or _is_directive(text):
            continue
        lines.append(_strip_line(text, spec.line_markers))
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip() or None


__all__ = ["extract_definition_docstring"]
