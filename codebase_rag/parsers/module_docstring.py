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
    # Node types a doc comment DOCUMENTS rather than merely precedes. When the
    # comment sits directly against one of these, with no blank line, it is
    # that declaration's documentation and not the file's.
    #
    # A deny-list, not an allow-list of legal followers, because the two fail
    # in opposite directions: an unlisted DECLARATION costs a wrong doc, but an
    # unlisted FOLLOWER costs a missing one -- and the followers are open-ended
    # (any statement may legally open a file) while the declarations are a
    # closed set the grammar names. Every entry below was read off the loaded
    # grammar, not assumed; the names differ more than they look like they
    # should (Scala `class_definition` vs Java `class_declaration`, C++
    # `class_specifier`, Dart `function_signature`).
    declaration_types: frozenset[str] = frozenset()


_C_STYLE_BLOCK = frozenset({"comment", "block_comment"})
# `/**` is Javadoc/JSDoc/Doxygen. A bare `/*` is a licence header or a note,
# and `/*!` is Doxygen's alternative form.
_DOC_BLOCK_MARKERS = ("/**", "/*!")
# `///` is the line form of the same convention. Not valid for Rust, where
# `///` documents the NEXT ITEM and `//!` documents the enclosing module, nor
# for JS/TS, where `///` is TypeScript's reference DIRECTIVE and not a doc.
_DOC_LINE_MARKERS = ("///",)

# A shebang precedes the doc comment in an executable script, and each grammar
# names it differently. Without skipping it the first child is not a comment
# and the file's documentation is silently dropped -- and a CLI entry point is
# exactly the kind of file that has both a shebang and a module doc.
_SHEBANGS = frozenset({"hash_bang_line", "shebang", "shebang_directive"})

# C and C++ share a grammar family but not their node names.
_C_DECLS = frozenset(
    {
        "function_definition",
        "struct_specifier",
        "union_specifier",
        "enum_specifier",
        "type_definition",
        "declaration",
        "class_specifier",
        "namespace_definition",
        "template_declaration",
        "alias_declaration",
        "concept_definition",
        "linkage_specification",
    }
)

_JAVA_DECLS = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
        "module_declaration",
    }
)

_SCALA_DECLS = frozenset(
    {
        "class_definition",
        "object_definition",
        "trait_definition",
        "enum_definition",
        "given_definition",
        "type_definition",
        "function_definition",
        "extension_definition",
        "val_definition",
        "var_definition",
    }
)

_CSHARP_DECLS = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "struct_declaration",
        "enum_declaration",
        "record_declaration",
        "delegate_declaration",
        # The BRACED `namespace N { ... }` only. `namespace N;` is
        # deliberately absent: the file-scoped form has no body and scopes the
        # whole file, so its siblings sit at file level and a doc above it
        # describes the file. The braced form is a block that contains the
        # rest of the code, so a doc above it documents that block.
        "namespace_declaration",
    }
)

_PHP_DECLS = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "trait_declaration",
        "enum_declaration",
        "function_definition",
        "const_declaration",
    }
)

# `lexical_declaration` and `variable_declaration` are deliberately absent:
# each is BOTH a declaration a doc attaches to and an ordinary statement that
# may legally open a documented file, so the node type cannot decide and
# adjacency does -- see `_documents_declaration`.
#
# `export_statement` is absent for a different reason: it is a WRAPPER, not a
# declaration. `export class C {}` and `export const x = 1` share the type, but
# the exported declaration is the node's own last named child, so unwrapping
# decides what the type alone cannot. `_unwrap_export` does that before the
# membership test below.
_JS_DECLS = frozenset(
    {
        "class_declaration",
        "function_declaration",
        "generator_function_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "enum_declaration",
        "type_alias_declaration",
        "ambient_declaration",
    }
)

_DART_DECLS = frozenset(
    {
        "class_definition",
        "mixin_declaration",
        "enum_declaration",
        "extension_declaration",
        "extension_type_declaration",
        "function_signature",
        "type_alias",
    }
)

_C_STYLE = ModuleDocSpec(
    line_types=frozenset({"comment", "line_comment"}),
    line_markers=_DOC_LINE_MARKERS,
    block_types=_C_STYLE_BLOCK,
    block_markers=_DOC_BLOCK_MARKERS,
    skip_types=_SHEBANGS,
    declaration_types=_C_DECLS,
)

# JS/TS/TSX: `/**` (JSDoc) is the only module-doc form. `///` there is
# TypeScript's `/// <reference ... />` directive, which is machine input
# rather than a description of the file.
_JS_STYLE = ModuleDocSpec(
    block_types=_C_STYLE_BLOCK,
    block_markers=_DOC_BLOCK_MARKERS,
    skip_types=_SHEBANGS,
    declaration_types=_JS_DECLS,
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
        # `#![no_std]` and friends legally precede the crate doc. Without
        # skipping them the walk stops on the attribute and the crate's
        # documentation is silently dropped.
        skip_types=_SHEBANGS | {"inner_attribute_item"},
    ),
    # Go has no marker at all: the package comment is an ordinary `//` comment
    # that happens to sit immediately above `package foo`. The blank line is
    # the whole distinction, and it is why a licence header does not count.
    SupportedLanguage.GO: ModuleDocSpec(
        line_types=frozenset({"comment"}),
        line_markers=("//",),
        anchor_types=frozenset({"package_clause"}),
        skip_types=_SHEBANGS,
    ),
    SupportedLanguage.JAVA: _C_STYLE._replace(declaration_types=_JAVA_DECLS),
    SupportedLanguage.SCALA: _C_STYLE._replace(declaration_types=_SCALA_DECLS),
    SupportedLanguage.JS: _JS_STYLE,
    SupportedLanguage.TS: _JS_STYLE,
    SupportedLanguage.TSX: _JS_STYLE,
    SupportedLanguage.C: _C_STYLE,
    SupportedLanguage.CPP: _C_STYLE,
    SupportedLanguage.CSHARP: _C_STYLE._replace(declaration_types=_CSHARP_DECLS),
    SupportedLanguage.DART: ModuleDocSpec(
        line_types=frozenset({"documentation_comment", "comment"}),
        line_markers=_DOC_LINE_MARKERS,
        # Dart's grammar labels a `/** */` doc `documentation_comment` too, so
        # the block set must carry it or the documented form yields nothing.
        block_types=_C_STYLE_BLOCK | {"documentation_comment"},
        block_markers=_DOC_BLOCK_MARKERS,
        skip_types=_SHEBANGS,
        declaration_types=_DART_DECLS,
    ),
    SupportedLanguage.PHP: ModuleDocSpec(
        line_types=frozenset({"comment"}),
        line_markers=_DOC_LINE_MARKERS,
        block_types=_C_STYLE_BLOCK,
        block_markers=_DOC_BLOCK_MARKERS,
        # `<?php` is the first child of every PHP file.
        skip_types=frozenset({"php_tag", "text_interpolation", "text"}) | _SHEBANGS,
        declaration_types=_PHP_DECLS,
    ),
    # LuaDoc/LDoc use `---`. A plain `--` is an ordinary comment.
    SupportedLanguage.LUA: ModuleDocSpec(
        line_types=frozenset({"comment"}),
        line_markers=("---",),
        skip_types=_SHEBANGS,
    ),
    # SQL has no module-documentation convention -- a leading `--` is as
    # likely to be a commented-out statement -- so nothing is extracted.
}

# Leading decoration on each line of a block comment: ` * text` -> `text`.
_BLOCK_LINE_PREFIX = re.compile(r"^\s*\*+/?\s?")
# The delimiters themselves, stripped before the per-line pass.
_BLOCK_OPEN = re.compile(r"^/\*+!?")


def _strip_block_close(body: str) -> str:
    """Drop the trailing `**/` of a block comment.

    Also the bare `/` that `_BLOCK_OPEN` leaves on an empty `/**/`, whose
    asterisks it has already taken: without that, the slash survives as the
    file's documentation.

    A scan rather than a regex. `\\*+/$` backtracks once per asterisk on a
    long rule with no closing slash -- `/****...` with no terminator is a
    separator inside a block comment, not a rarity -- and every regex spelling
    that fixes the backtracking either keeps a quantifier a static analyser
    still reads as super-linear or caps the run at an arbitrary length. The
    scan is linear, has no cap, and says plainly what it removes.
    """
    if not body.endswith("/"):
        return body
    index = len(body) - 1
    while index > 0 and body[index - 1] == "*":
        index -= 1
    return body[:index]


def _strip_line(text: str, markers: tuple[str, ...]) -> str:
    # Longest first, so that adding a marker that is a prefix of another (a
    # `--` beside Lua's `---`) strips the longer one rather than leaving a
    # stray delimiter. No spec needs this yet; every one has a single marker.
    for marker in sorted(markers, key=lambda candidate: -len(candidate)):
        if text.startswith(marker):
            return text[len(marker) :].strip()
    return text.strip()


def _clean_block(text: str) -> str:
    body = _strip_block_close(_BLOCK_OPEN.sub("", text))
    lines = [_BLOCK_LINE_PREFIX.sub("", line) for line in body.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line.rstrip() for line in lines).strip()


def _is_marked(text: str, markers: tuple[str, ...]) -> bool:
    return any(text.startswith(marker) for marker in markers)


# A comment that is only its own delimiter repeated -- `--------`, `////////`
# -- is a visual separator, not prose. `startswith` matches one, and stripping
# the marker leaves the remaining dashes looking like content.
#
# Strictly longer than the marker, never equal: a bare `//` or `//!` is the
# blank line of a doc block, not decoration. Go's standard package comment
# (`// Package m ...` / `//` / `// More.`) and Rust's `//!` paragraph break
# both depend on it, and treating one as a separator drops everything after
# the first paragraph -- or, for Go, the whole comment, because the bare `//`
# then ends the block and the anchor check no longer sees `package`.
def _is_separator(text: str, markers: tuple[str, ...]) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return any(
        marker and set(stripped) <= set(marker) and len(stripped) > len(marker)
        for marker in markers
    )


# Machine-readable directives that sit where a package comment would.
# Go's `//go:generate`, linter pragmas, and the generated-file banner are
# instructions to tooling, not a description of the file.
_DIRECTIVE = re.compile(
    r"^(?://)?\s*(?:go:|lint:|nolint|export\s|cgo\s)|^Code generated .* DO NOT EDIT\.$"
)


def _is_directive(text: str) -> bool:
    return _DIRECTIVE.search(text.strip()) is not None


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
    index = _first_meaningful_child(children, spec)
    if index is None:
        return None

    node = children[index]
    text = safe_decode_with_fallback(node).strip()

    if node.type in spec.block_types and _is_marked(text, spec.block_markers):
        return _block_doc(children, index, node, text, spec)
    if node.type in spec.line_types and _is_marked(text, spec.line_markers):
        return _line_doc(children, index, spec)
    return None


def _first_meaningful_child(children: list[ASTNode], spec: ModuleDocSpec) -> int | None:
    """The first child that is not a shebang, a PHP tag or a Rust attribute.

    Each of these legally precedes the documentation, and each is named
    differently by its grammar. Without skipping them the first child is not a
    comment and the file's documentation is silently dropped.
    """
    for index, child in enumerate(children):
        if child.type not in spec.skip_types:
            return index
    return None


def _describes_the_file(
    children: list[ASTNode], index: int, last: ASTNode, spec: ModuleDocSpec
) -> bool:
    """Whether the comment describes the file rather than what follows it."""
    if spec.anchor_types and not _anchored(children, index, last, spec):
        return False
    return not _documents_declaration(children, index, last, spec)


def _block_doc(
    children: list[ASTNode],
    index: int,
    node: ASTNode,
    text: str,
    spec: ModuleDocSpec,
) -> str | None:
    if not _describes_the_file(children, index, node, spec):
        return None
    cleaned = _clean_block(text)
    if not cleaned or _is_directive(cleaned):
        return None
    return cleaned


def _line_doc(children: list[ASTNode], index: int, spec: ModuleDocSpec) -> str | None:
    lines, last = _collect_line_block(children, index, spec)
    if not _describes_the_file(children, index, last, spec):
        return None
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip() or None


def _collect_line_block(
    children: list[ASTNode], index: int, spec: ModuleDocSpec
) -> tuple[list[str], ASTNode]:
    """The consecutive marked line comments starting at `index`, as content.

    `//!` in Rust and `///` in Dart are written a line at a time, and taking
    only the first would truncate every multi-line module doc to its opening
    sentence. Returns the content lines and the last comment node consumed,
    which the anchor and declaration checks measure from.
    """
    lines: list[str] = []
    last = children[index]
    for candidate in children[index:]:
        text = safe_decode_with_fallback(candidate).strip()
        if _ends_the_block(candidate, text, lines, last, spec):
            break
        # A rule of repeated delimiters is decoration, and a directive is an
        # instruction to tooling. Neither is prose, but both may sit above a
        # real doc comment, so each is skipped rather than ending the block.
        # `_ends_the_block` has already stopped a separator that follows
        # content, which closes a doc rather than opening one.
        if not _is_separator(text, spec.line_markers):
            content = _strip_line(text, spec.line_markers)
            if not _is_directive(content):
                lines.append(content)
        last = candidate
    return lines, last


def _ends_the_block(
    candidate: ASTNode,
    text: str,
    lines: list[str],
    last: ASTNode,
    spec: ModuleDocSpec,
) -> bool:
    """Whether `candidate` stops the run of comments forming one doc block."""
    if candidate.type not in spec.line_types:
        return True
    if not _is_marked(text, spec.line_markers):
        return True
    # A blank line between comments ends the block.
    if lines and candidate.start_point[0] > _content_end_row(last) + 1:
        return True
    # A separator rule closes a doc that has begun; one that opens the block
    # is decoration above the doc and is skipped instead.
    return bool(lines) and _is_separator(text, spec.line_markers)


def _documents_declaration(
    children: list[ASTNode], index: int, last: ASTNode, spec: ModuleDocSpec
) -> bool:
    """Whether the comment documents the declaration beneath it.

    `/** Class docs */` directly above `class C {}` is Javadoc for the class,
    not documentation of the file; recording it on the Module node attributes
    a class's own description to its file. The distinction is the blank line,
    the same convention Go uses for its package comment and the same one every
    doc generator in these languages follows: a doc comment touching a
    declaration belongs to it, a detached one describes the file.

    Adjacency also settles the node types that are ambiguous on their own. A
    JavaScript `export const x = 1` is `export_statement` whether it is the
    documented subject or merely the first statement of a documented file, so
    `_JS_DECLS` omits it and the blank line decides instead.
    """
    if not spec.declaration_types:
        return False
    end_row = _content_end_row(last)
    end_column = last.end_point[1]
    for candidate in children[index:]:
        # Skip the doc block itself and anything before it. On the doc's last
        # row that means anything starting at or left of where it ends; a node
        # starting to the RIGHT is the declaration sharing the line with its
        # own doc comment, which is adjacent to it.
        if candidate.start_point[0] < end_row:
            continue
        if (
            candidate.start_point[0] == end_row
            and candidate.start_point[1] <= end_column
        ):
            continue
        if candidate.start_point[0] > end_row + 1:
            return False
        return _unwrap_export(candidate).type in spec.declaration_types
    return False


def _unwrap_export(node: ASTNode) -> ASTNode:
    """The declaration inside an `export`, or the node itself.

    `export class C {}` is an `export_statement` wrapping a
    `class_declaration`, so the wrapper's type says nothing about whether the
    doc belongs to the file. The exported declaration is its last named child;
    `export const x = 1` and `export {}` unwrap to nodes that are not
    declaration types, so both still read as file documentation.
    """
    while node.type == "export_statement":
        named = [child for child in node.children if child.is_named]
        if not named:
            return node
        node = named[-1]
    return node


def _content_end_row(node: ASTNode) -> int:
    """The last row carrying text, which is not always `end_point`.

    Rust's `line_comment` includes its trailing newline, so `end_point` is the
    row AFTER the comment, and a gap measured from it counts one line short --
    a blank line between two `//!` paragraphs would read as adjacent. Every
    other grammar here ends the node on the text. Measuring from the text
    itself is right for both.
    """
    text = safe_decode_with_fallback(node)
    trailing = text[len(text.rstrip("\r\n")) :]
    # Only a NEWLINE moves `end_point` onto the following row. Dart's CRLF
    # `documentation_comment` keeps the `\r` and ends on its own row, so
    # testing for any trailing whitespace would subtract a row that was never
    # added and put the doc an impossible row above itself.
    return node.end_point[0] - (1 if "\n" in trailing else 0)


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
