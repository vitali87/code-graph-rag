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

# Rust's OUTER doc markers: the forms that document the NEXT item. `///` and
# `/**` only -- `//!` and `/*!` are the INNER forms that describe the enclosing
# module, and both stay with the module spec. The first version listed `/*!`
# here too (copied from the C-family table, where it is Doxygen's alternative
# block form), so a `/*! ... */` opening a Rust file was stored on the module
# AND on the first item beneath it (Greptile and CodeRabbit on PR #1888).
_OUTER_LINE_MARKERS = ("///",)
_OUTER_BLOCK_MARKERS = ("/**",)

# Node types that legally sit BETWEEN a doc comment and the declaration it
# documents, as SIBLINGS of that declaration. A walk that stopped at the first
# non-comment sibling would miss the declaration entirely.
#
# Rust alone, and that is measured rather than assumed. The first version of
# this table also carried Java, Scala, C# and Dart entries on the reasoning
# that each language allows an annotation there -- they do, but their grammars
# make the annotation a CHILD of the declaration, so the doc comment is
# already the immediately preceding sibling and this walk never runs. Checked
# on the loaded grammars: for `/** DOC */ @Deprecated class C {}` the
# top-level siblings are `[block_comment, class_declaration]` in Java, and the
# same shape in Scala, C# and Dart, against
# `[line_comment, attribute_item, struct_item]` in Rust.
#
# Found by mutation: disabling the walk reddened ONE test rather than the three
# predicted, which is what exposed the other entries as unreachable. An entry
# that cannot execute is not a safety margin -- it is a claim about a grammar
# that nothing checks.
_INTERLEAVED: dict[SupportedLanguage, frozenset[str]] = {
    SupportedLanguage.RUST: frozenset({"attribute_item"}),
}

# Node types that WRAP the declaration the ingester hands us, so that the doc
# comment is a sibling of the wrapper and not of the node. `export class C {}`
# is an `export_statement` around a `class_declaration`; Go's `type S struct{}`
# is a `type_declaration` around the `type_spec` the class ingest passes; a
# Dart method is a `method_signature` around its `function_signature`; a JS
# `const f = () => {}` is `lexical_declaration` > `variable_declarator` >
# `arrow_function`, and a CommonJS `module.exports.f = function () {}` is
# `expression_statement` > `assignment_expression` > `function_expression`.
# Reading `node.parent.children` for any of these looked at the wrapper's
# children and reported every such declaration as undocumented (greptile-local,
# PR for #1809: TS export class/function/interface, Go struct and interface,
# Dart method, JS arrow and CJS function all came back None end-to-end).
_JS_WRAPPERS = frozenset(
    {
        "export_statement",
        "lexical_declaration",
        "variable_declaration",
        "variable_declarator",
        "expression_statement",
        "assignment_expression",
    }
)
_WRAPPERS: dict[SupportedLanguage, frozenset[str]] = {
    SupportedLanguage.JS: _JS_WRAPPERS,
    SupportedLanguage.TS: _JS_WRAPPERS,
    SupportedLanguage.TSX: _JS_WRAPPERS,
    SupportedLanguage.GO: frozenset({"type_declaration"}),
    SupportedLanguage.DART: frozenset({"method_signature"}),
}


def _climb_wrappers(node: ASTNode, language: SupportedLanguage) -> ASTNode:
    """The outermost single-declaration wrapper around `node`, or `node`.

    Climbs only while the parent is a wrapper type AND holds exactly one child
    of the node's type. A grouped Go `type ( A struct{}; B struct{} )` or a
    `const a = () => {}, b = () => {}` is not climbed: the leading comment
    describes the group, and each member's own doc -- if any -- is already its
    sibling inside the group, where the ordinary walk finds it.
    """
    wrappers = _WRAPPERS.get(language)
    if not wrappers:
        return node
    while (parent := node.parent) is not None and parent.type in wrappers:
        same = [child for child in parent.children if child.type == node.type]
        if len(same) != 1:
            break
        node = parent
    return node


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
    # A comment that starts on the row where the previous sibling ENDS is that
    # sibling's trailing comment -- Doxygen's `int a; ///< The a field`, Go's
    # `var x = 1 // note` -- and go/parser, doxygen and javadoc all read it
    # so. Accepting it on adjacency alone wrote the previous line's remark to
    # the NEXT declaration as its docstring (greptile-local, PR for #1809).
    if _is_trailing(siblings, index):
        return None
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
    return _line_block_opener(siblings, index, spec)


def _line_block_opener(siblings: list[ASTNode], index: int, spec: ModuleDocSpec) -> int:
    """Walk up from the marked line comment at `index` to where its block opens.

    A line-comment doc is written a line at a time. The block extends upward
    while the sibling above is a marked line comment on the adjacent row that
    is not itself trailing some earlier line -- the same rule going up that
    `_doc_block_start` applies going down. Split out for Sonar's cognitive
    complexity limit, not for reuse.
    """
    while index - 1 >= 0 and _continues_block(siblings, index, spec):
        index -= 1
    return index


def _continues_block(siblings: list[ASTNode], index: int, spec: ModuleDocSpec) -> bool:
    above = siblings[index - 1]
    if above.type not in spec.line_types:
        return False
    if not _is_marked(safe_decode_with_fallback(above).strip(), spec.line_markers):
        return False
    if siblings[index].start_point[0] - _last_row(above) > 1:
        return False
    # A trailing comment on the line above the block belongs to that line.
    return not _is_trailing(siblings, index - 1)


def _is_trailing(siblings: list[ASTNode], index: int) -> bool:
    """Whether `siblings[index]` starts on the row its previous sibling ends on.

    `_last_row` rather than `end_point`, because a line comment's extent
    includes its newline: two consecutive `///` lines would otherwise read as
    the second trailing the first, and every multi-line doc would be refused.
    """
    if index <= 0:
        return False
    return _last_row(siblings[index - 1]) == siblings[index].start_point[0]


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
    node = _climb_wrappers(node, language)
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

    NOT by `==` either, which is value equality on type and extent, so the
    first match would win if two siblings ever shared both. The key below is
    (start, end, type); every match is collected and exactly one is required,
    for the reason in the comment on the return.
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


# Doxygen forms libclang reports through `Cursor.raw_comment`: the comment text
# exactly as written, block or consecutive lines, or None when the cursor has
# none. libclang has already decided ownership (it attaches a comment to the
# declaration it documents), so this only cleans.
_LIBCLANG_LINE_MARKERS = ("///", "//!")
_LIBCLANG_BLOCK_MARKERS = ("/**", "/*!")


def libclang_docstring(raw_comment: object) -> str | None:
    """The docstring for a libclang cursor's `raw_comment`, or None.

    The pure-libclang C++ frontend emits definitions without going through the
    tree-sitter walk above, so documented classes and functions came out with
    `docstring=None` whenever `CPP_FRONTEND=libclang` (Greptile on PR #1888).
    `raw_comment` is typed loosely because a test double's attribute is not a
    string; anything that is not one yields None rather than a repr.
    """
    if not isinstance(raw_comment, str):
        return None
    text = raw_comment.strip()
    if not text:
        return None
    if _is_marked(text, _LIBCLANG_BLOCK_MARKERS):
        cleaned = _clean_block(text)
        return cleaned or None
    lines = [
        _strip_line(line.strip(), _LIBCLANG_LINE_MARKERS)
        for line in text.splitlines()
        if _is_marked(line.strip(), _LIBCLANG_LINE_MARKERS)
        and not _is_separator(line.strip(), _LIBCLANG_LINE_MARKERS)
    ]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip() or None


__all__ = ["extract_definition_docstring", "libclang_docstring"]
