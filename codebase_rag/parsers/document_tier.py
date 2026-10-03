"""Heading-structure extractor for document files (issue #1426).

A third tier alongside the tree-sitter (`LanguageSpec`) and ast-grep tiers.
Documents have no functions, classes, or calls, so they get their own node
label: a `Section` per heading, nested by heading level, carrying the line
span of the heading and the prose beneath it.

Why not reuse the other tiers:

- `LanguageSpec` is built around ``function_node_types`` / ``class_node_types``
  / ``call_node_types``. Mapping a heading onto one of those would make every
  heading a `Class` in the graph and surface headings in `cgr dead-code` and
  `cgr duplicates` output.
- The ast-grep tier emits a flat Module/Function/Class set with no nesting,
  which would discard the parent/child heading structure that is the point.

Nesting is computed from heading LEVELS, not from the grammar's `section`
nodes. ATX headings (``## x``) nest as `section` nodes, but setext headings
(``x`` over ``----``) are flat siblings inside one `section`, so level
arithmetic is the only rule that handles both forms alike. It also gives
skipped levels (``#`` straight to ``###``) the natural answer: the deeper
heading is a child of whatever heading is currently open above it.
"""

from __future__ import annotations

import html
import posixpath
import re
import unicodedata
from bisect import bisect_right
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import unquote

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..types_defs import PropertyDict
from ..utils.path_utils import cached_relative_path, cached_resolve_posix
from .flat_module import emit_flat_module, flat_module_qn

if TYPE_CHECKING:
    from tree_sitter import Node, Parser

    from ..services import IngestorProtocol

# Grammar node types. ATX is ``### Heading``; setext underlines a line with
# ``===`` (h1) or ``---`` (h2).
_ATX_HEADING = "atx_heading"
_SETEXT_HEADING = "setext_heading"
_HEADING_TYPES = frozenset({_ATX_HEADING, _SETEXT_HEADING})
_INLINE = "inline"
_PARAGRAPH = "paragraph"
_ATX_MARKER_PREFIX = "atx_h"
_ATX_MARKER_SUFFIX = "_marker"
_SETEXT_H1_UNDERLINE = "setext_h1_underline"
_SETEXT_H2_UNDERLINE = "setext_h2_underline"

_MAX_HEADING_LEVEL = 6

DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({".md", ".markdown"})

# YAML front-matter delimiter. The grammar exposes the whole block as a
# `minus_metadata` node, but its contents are opaque -- tree-sitter-markdown
# does not parse YAML -- so the pairs are read here.
_FRONT_MATTER_FENCE = "---"

# `key: |` and `key: >` introduce a block scalar whose value is the indented
# text beneath, which this parser skips. The marker is not the value.
#
# Matched by PATTERN rather than an enumerated set. YAML allows an optional
# chomping indicator (`-`/`+`) and an optional explicit indentation digit, in
# either order: `|`, `|-`, `|2`, `|2-`, `|-2`, `>+2` are all valid headers. An
# earlier version listed six spellings and missed every form carrying a digit,
# which stored the header text as the value (reported on #1488) -- the same
# defect the set was added to fix, in the spellings the set did not name.
_BLOCK_SCALAR_HEADER = re.compile(r"^[|>](?:[-+]?\d*|\d*[-+]?)$")

# `[a, b]` and `{k: v}` are YAML flow collections: structures on one line.
_FLOW_COLLECTION_OPENERS: frozenset[str] = frozenset({"[", "{"})
_FLOW_SEQUENCE_OPEN = "["
_FLOW_SEQUENCE_CLOSE = "]"
_FLOW_ITEM_SEPARATOR = ","
_QUOTES: frozenset[str] = frozenset({'"', "'"})
_INDENT: frozenset[str] = frozenset({" ", "\t"})
# `- item` in a block sequence; a bare `-` is a null item.
_BLOCK_ITEM_MARKER = "-"
_BLOCK_ITEM_PREFIX = "- "
# `key: value` inside an item makes the item a map, which has no flat
# spelling; so does a key with nothing after its colon.
_MAPPING_SEPARATOR = ": "
_MAPPING_SUFFIX = ":"
# A list is stored as ONE `key=a,b` entry, the spelling #2458 proposes: a
# consumer that reads the entries into a dict keeps every item, where an
# entry per item would keep only the last.
_FRONT_MATTER_ASSIGN = "="
_FRONT_MATTER_LIST_JOIN = ","

# A declared value: a scalar, or the items of a list of scalars.
type FrontMatterValue = str | tuple[str, ...]

# Property names a document may NOT declare, because the ingestion layer owns
# them. A front-matter `path:` would otherwise overwrite the node's real path
# and break every consumer that resolves a Module back to a file.
_RESERVED_FRONT_MATTER_KEYS: frozenset[str] = frozenset(
    {
        cs.KEY_QUALIFIED_NAME,
        cs.KEY_NAME,
        cs.KEY_PATH,
        cs.KEY_ABSOLUTE_PATH,
        cs.KEY_START_LINE,
        cs.KEY_END_LINE,
        cs.KEY_HEADING_LEVEL,
    }
)


def _without_trailing_comment(value: str) -> str:
    """`value` with an unquoted trailing YAML comment removed.

    Used only to recognise a block-scalar header that carries one. Requires
    whitespace before the `#`, so `C#` and `a#b` are left alone -- a bare
    `#`-split would corrupt ordinary values, which is worse than the defect
    it fixes.
    """
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1].isspace():
            return value[:index].rstrip()
    return value


def _declared_key(line: str) -> tuple[str, str] | None:
    """A top-level `key:` line's name and the raw text after its colon.

    The guards every declaration shares, whether its value is a scalar on the
    line or a list on the lines below.
    """
    # INDENTED lines belong to a parent key, not the document. Taking them
    # would hoist `child: v` under `parent:` to top level, inventing a
    # declaration the author never made at that level.
    if line[:1] in _INDENT:
        return None
    # A comment declares nothing. `# note: x` would become the key "# note".
    if line.lstrip().startswith("#"):
        return None
    key, separator, value = line.partition(":")
    if not separator:
        return None
    name = key.strip()
    if not name or name in _RESERVED_FRONT_MATTER_KEYS:
        return None
    return name, value


def _front_matter_pair(line: str) -> tuple[str, FrontMatterValue] | None:
    """One top-level `key: value` declaration, or None if the line has none.

    Extracted from `parse_front_matter` to keep each rule separately readable
    and the caller's complexity down (Sonar S3776). Every `None` below is a
    distinct reason a line is not a declaration, and the comments say which.
    """
    declared = _declared_key(line)
    if declared is None:
        return None
    name, value = declared
    # An EMPTY value opens a nested block or a list (`parent:` / `tags:`)
    # rather than declaring a scalar. Recording it as an empty string would
    # assert the author declared it empty -- a different claim from declaring
    # a structure. A list beneath is read by `_block_list` instead.
    #
    # REDUNDANT with the unquoted check below, verified by mutation: removing
    # this leaves all 23 tests passing, because a value empty here is empty
    # there too. Kept because the two guards answer different questions and
    # only coincide today -- this one is about a key that declares a
    # STRUCTURE, that one about quote-stripping emptying a scalar. If either
    # rule changes they diverge, and a reader tracing "why is `tags:` skipped"
    # should land here rather than on a quote-stripping detail.
    cleaned = value.strip()
    if not cleaned:
        return None
    # A BLOCK SCALAR header (`key: |`, `key: >2-`) says the value is the
    # indented text below, which this parser skips. Storing the header records
    # punctuation as content with the real text dropped. It may carry a
    # trailing comment (`note: | # explanation`), which is still a header --
    # the comment is stripped ONLY for this test, and only when preceded by
    # whitespace, so `title: C# notes` is untouched.
    if _BLOCK_SCALAR_HEADER.match(_without_trailing_comment(cleaned)):
        return None
    # An unquoted TRAILING COMMENT is not part of the value: `status: planned
    # # later` declares "planned". Only for an unquoted value -- quoting is
    # how an author says the `#` is content, and `note: "a # b"` would
    # otherwise be truncated. `_without_trailing_comment` requires whitespace
    # before the `#`, so `C#` and `a#b` are untouched either way.
    #
    # This runs BEFORE the emptiness checks below on purpose: `status: #
    # planned` is a null value carrying a comment, and stripping it makes
    # that the same case as a bare `status:`, which already declares nothing.
    if cleaned[:1] not in _QUOTES:
        # A value that BEGINS with `#` is entirely a comment (`status: #
        # planned` is a null value plus a comment).
        # `_without_trailing_comment` cannot see this one: it requires
        # whitespace before the `#`, and here the `#` is at index 0 of the
        # stripped value.
        if cleaned.startswith("#"):
            return None
        cleaned = _without_trailing_comment(cleaned)
        if not cleaned:
            return None
    # A FLOW SEQUENCE (`[a, b]`) is a list on one line, kept as its items
    # rather than as source text that would read like a string (#2458).
    if cleaned.startswith(_FLOW_SEQUENCE_OPEN):
        items = _flow_sequence_items(cleaned)
        return None if items is None else (name, items)
    # A FLOW MAPPING (`{k: v}`) is a structure with no `key=value` spelling.
    if cleaned[:1] in _FLOW_COLLECTION_OPENERS:
        return None
    # Quote-stripping can empty a value that passed the check above: `k: ""`
    # is non-empty as written and empty once unquoted.
    unquoted = cleaned.strip("\"'")
    if not unquoted:
        return None
    return name, unquoted


def _list_item(raw: str) -> str | None:
    """One list item as a scalar, or None when it is not one.

    An item that is itself a structure -- a nested list, a map such as
    `- name: x`, a null `-` -- has no flat spelling, and the caller refuses
    the whole list rather than store part of it as if it were all of it.
    """
    cleaned = raw.strip()
    if cleaned[:1] not in _QUOTES:
        cleaned = _without_trailing_comment(cleaned)
        if (
            not cleaned
            or cleaned.startswith("#")
            or cleaned[:1] in _FLOW_COLLECTION_OPENERS
            or cleaned == _BLOCK_ITEM_MARKER
            or cleaned.startswith(_BLOCK_ITEM_PREFIX)
            or _MAPPING_SEPARATOR in cleaned
            or cleaned.endswith(_MAPPING_SUFFIX)
        ):
            return None
    return cleaned.strip("\"'") or None


def _flow_sequence_items(text: str) -> tuple[str, ...] | None:
    """The items of a one-line `[a, b]`, or None when it is not a flat list.

    Split on the commas outside quotes, so `["a, b", c]` is two items. An
    empty list declares nothing, as an empty scalar does.
    """
    if not text.endswith(_FLOW_SEQUENCE_CLOSE):
        return None
    raw_items: list[str] = []
    current: list[str] = []
    quote = ""
    for char in text[1:-1]:
        if quote:
            quote = "" if char == quote else quote
        elif char in _QUOTES:
            quote = char
        elif char == _FLOW_ITEM_SEPARATOR:
            raw_items.append("".join(current))
            current = []
            continue
        current.append(char)
    if quote:
        return None
    raw_items.append("".join(current))
    items: list[str] = []
    # A blank entry is `[]` itself or the trailing comma YAML allows.
    for raw in (raw for raw in raw_items if raw.strip()):
        item = _list_item(raw)
        if item is None:
            return None
        items.append(item)
    return tuple(items) or None


def _block_list_key(line: str) -> str | None:
    """The name of a top-level key whose value is the block beneath it."""
    declared = _declared_key(line)
    if declared is None:
        return None
    name, value = declared
    rest = value.strip()
    return name if not rest or rest.startswith("#") else None


def _is_block_item(stripped: str) -> bool:
    """Whether a stripped line is a `- item` entry, or a bare `-`."""
    return stripped == _BLOCK_ITEM_MARKER or stripped.startswith(_BLOCK_ITEM_PREFIX)


def _ends_block(line: str) -> bool:
    """Whether `line` is a top-level line that is neither an item nor a comment."""
    stripped = line.strip()
    if not stripped or line[:1] in _INDENT:
        return False
    return not (_is_block_item(stripped) or stripped.startswith("#"))


def _block_list(lines: list[str], index: int) -> tuple[tuple[str, ...] | None, int]:
    """The `- item` list starting at `index`, and the index after its block.

    The block runs to the next top-level line that is not an item: YAML lets
    a list under a key sit at the key's own indentation. Anything else in it
    -- an indented `child: v` (a map), a continuation line -- is a structure
    this parser does not represent, so the key is refused, not truncated.
    """
    items: list[str] = []
    flat = True
    while index < len(lines) and not _ends_block(lines[index]):
        stripped = lines[index].strip()
        index += 1
        if not stripped or stripped.startswith("#"):
            continue
        item = (
            _list_item(stripped[len(_BLOCK_ITEM_MARKER) :])
            if _is_block_item(stripped)
            else None
        )
        if item is None:
            flat = False
        else:
            items.append(item)
    return (tuple(items) if flat and items else None), index


def _front_matter_entry(key: str, value: FrontMatterValue) -> str:
    """`key=value`, a list's items comma-joined: `tags=a,b`."""
    text = value if isinstance(value, str) else _FRONT_MATTER_LIST_JOIN.join(value)
    return f"{key}{_FRONT_MATTER_ASSIGN}{text}"


def parse_front_matter(text: str) -> dict[str, FrontMatterValue]:
    """Read declared YAML front-matter into flat properties.

    Deliberately NOT a YAML parser. Only top-level keys are read, holding a
    scalar or a list of scalars (`tags: [a, b]`, or `- a` lines beneath
    `tags:`, issue #2458). Nested maps, lists of structures and multi-line
    values are skipped rather than flattened, because a graph node property
    is flat and inventing a representation for a map would be a schema
    decision this parser does not make (#1448).

    Declared metadata, not inferred: #1448 lists five other bullets that need
    design decisions about inference and unprompted edits, and this one is
    separable precisely because it reads what the author wrote.

    Refuses rather than guesses in three cases, each of which would otherwise
    put non-metadata into node properties:

    - no opening fence on the FIRST line (a `---` elsewhere is a horizontal
      rule or a setext underline, not front-matter)
    - no closing fence (an unterminated block would swallow the document)
    - a reserved key that the ingestion layer owns

    A single malformed line is skipped rather than discarding the block:
    front-matter is hand-written, so a stray line is likelier than a wholly
    invalid block, and the valid pairs around it still carry meaning.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_FENCE:
        return {}
    closing = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == _FRONT_MATTER_FENCE),
        None,
    )
    if closing is None:
        return {}
    body = lines[1:closing]
    found: dict[str, FrontMatterValue] = {}
    index = 0
    while index < len(body):
        line = body[index]
        index += 1
        if (key := _block_list_key(line)) is not None:
            items, index = _block_list(body, index)
            if items is not None:
                found[key] = items
            continue
        pair = _front_matter_pair(line)
        if pair is not None:
            found[pair[0]] = pair[1]
    return found


# Link grammar nodes. `inline_link` is ``[text](target)``; the destination sits
# in a `link_destination` child. Reference-style links (``[text][label]``) name
# a definition elsewhere and carry no destination of their own: theirs is read
# from the definition their label names.
#
# These come from the INLINE grammar, not the block one. tree-sitter-markdown
# ships a split grammar: the block parser leaves every span of inline content
# as one opaque `inline` node whose children are bare punctuation tokens, so a
# link is simply not present in the block tree. Each `inline` node's text has
# to be re-parsed with `inline_language()` for links to appear at all.
_INLINE_LINK = "inline_link"
_LINK_DESTINATION = "link_destination"

# ``[label]: path`` — the target of a reference-style link (``[text][label]``).
# The use site carries no destination, so without reading definitions a
# document that links its files that way contributes no edges at all. Unlike
# inline links these sit in the BLOCK tree, needing no inline re-parse.
_LINK_REFERENCE_DEFINITION = "link_reference_definition"
_LINK_LABEL = "link_label"
_LINK_TEXT = "link_text"

# The three reference-link forms. `[text][label]` names its label explicitly;
# `[label][]` and `[label]` both use their own text as the label. Each use is
# a link of its own, located where it is written rather than at the
# definition; a definition no link names states no relationship.
_FULL_REFERENCE_LINK = "full_reference_link"
_COLLAPSED_REFERENCE_LINK = "collapsed_reference_link"
_SHORTCUT_LINK = "shortcut_link"
_TEXT_LABELLED_LINKS = frozenset({_COLLAPSED_REFERENCE_LINK, _SHORTCUT_LINK})
_LINK_NODE_TYPES = frozenset(
    {_INLINE_LINK, _FULL_REFERENCE_LINK, *_TEXT_LABELLED_LINKS}
)

# Inline markup as GitHub renders it in a heading, which is the text its
# anchor is slugged from: an image renders as its alt text, an autolink as
# its address, an escape or a character reference as the character itself,
# and delimiters and HTML tags as nothing.
_IMAGE = "image"
_IMAGE_DESCRIPTION = "image_description"
_AUTOLINKS = frozenset({"uri_autolink", "email_autolink"})
_BACKSLASH_ESCAPE = "backslash_escape"
_CHARACTER_REFERENCES = frozenset({"entity_reference", "numeric_character_reference"})
_UNRENDERED_INLINE = frozenset(
    {"emphasis_delimiter", "code_span_delimiter", "html_tag"}
)

# A destination that names something other than a file in this repository.
# Scheme-bearing targets (http:, https:, mailto:, ftp:) are external. A bare
# fragment ("#section") is not: it names a heading of the current document.
_URI_SCHEME_SEPARATOR = "://"
_MAILTO_PREFIX = "mailto:"
_FRAGMENT_PREFIX = "#"
# A network-path reference ("//host/path"): scheme-relative, so external.
_NETWORK_PATH_PREFIX = "//"
# Windows drive letters ("C:/x") would otherwise read as a scheme.
_MIN_SCHEME_LENGTH = 2

# A heading with no text (`##` alone) has nothing to name a node after.
_UNTITLED = "(untitled)"

# A leading "/" in a link means the repository root, not the filesystem's.
_ROOT_RELATIVE_PREFIX = "/"
# ``<a b.md>`` wraps a destination that contains spaces.
_ANGLE_OPEN = "<"
_ANGLE_CLOSE = ">"

# GitHub's heading anchors (the github-slugger rules): lower-cased, every
# character dropped except letters, digits, marks, `-`, `_` and spaces, and
# each space turned into `-`. A repeated slug gets `-1`, `-2`, ... in order.
_SLUG_KEPT: frozenset[str] = frozenset({"-", "_", " "})
_SLUG_SPACE = " "
_SLUG_DASH = "-"
_UNICODE_MARK_PREFIX = "M"
_LINE_BREAK = b"\n"


class _Heading(NamedTuple):
    """One heading, as its Section node and as an anchor target."""

    qualified_name: str
    name: str
    level: int
    start_line: int
    end_line: int
    parent_qn: str
    parent_is_module: bool
    slug: str


class _LinkSite(NamedTuple):
    """One link as written: its byte span, destination and words."""

    start_byte: int
    end_byte: int
    destination: str
    text: str


class _LocatedLink(NamedTuple):
    """A link whose target is a file of this repository."""

    site: _LinkSite
    target: str
    fragment: str


class _PendingLink(NamedTuple):
    """A LINKS_TO edge held until every node it may end at is buffered."""

    source: tuple[cs.NodeLabel, str, str]
    target: str
    fragment: str
    properties: PropertyDict


def _heading_level(node: Node) -> int | None:
    """1-6 for a heading node, or None when the node is not a heading."""
    for child in node.children:
        kind = child.type
        if kind.startswith(_ATX_MARKER_PREFIX) and kind.endswith(_ATX_MARKER_SUFFIX):
            # atx_h3_marker -> 3
            digits = kind[len(_ATX_MARKER_PREFIX) : -len(_ATX_MARKER_SUFFIX)]
            if digits.isdigit():
                level = int(digits)
                if 1 <= level <= _MAX_HEADING_LEVEL:
                    return level
        elif kind == _SETEXT_H1_UNDERLINE:
            return 1
        elif kind == _SETEXT_H2_UNDERLINE:
            return 2
    return None


def _heading_inline(node: Node) -> Node | None:
    """The `inline` node holding a heading's own text, or None.

    ATX puts the text in a direct `inline` child. Setext wraps it one level
    deeper, in a `paragraph` holding the `inline`, so a direct-children-only
    reader silently returns nothing for every setext heading.
    """
    for child in node.children:
        if child.type == _INLINE:
            return child
        if child.type == _PARAGRAPH:
            for grandchild in child.children:
                if grandchild.type == _INLINE:
                    return grandchild
    return None


def _heading_text(node: Node, source: bytes) -> str:
    """The heading's own text as written, without its marker or underline."""
    inline = _heading_inline(node)
    return "" if inline is None else _decode(inline, source)


def _rendered_heading_text(
    node: Node, source: bytes, inline_parser: Parser | None
) -> str:
    """The heading's text as GitHub renders it, which is what it slugs.

    The raw Markdown carries a link's destination, emphasis markers and HTML
    tags that the rendered heading does not, and a slug built from them
    misses every anchor written against the page (PR #2830 review). Without
    the inline grammar the raw text is the best answer left.
    """
    inline = _heading_inline(node)
    if inline is None:
        return ""
    raw = source[inline.start_byte : inline.end_byte]
    if inline_parser is None:
        return raw.decode(cs.ENCODING_UTF8, errors="replace")
    try:
        root = inline_parser.parse(raw).root_node
    except (RuntimeError, ValueError):
        return raw.decode(cs.ENCODING_UTF8, errors="replace")
    return _rendered_text(root, raw)


def _rendered_text(node: Node, text: bytes) -> str:
    """The text an inline node renders to, markup removed.

    A link or image renders as its words, an autolink as its address, an
    escape or entity as the character it stands for; delimiters and HTML
    tags render as nothing, while the text between two tags stays. Plain
    text is not a node in this grammar but the gaps between child nodes,
    so it is copied through as written.
    """
    kind = node.type
    if kind in _UNRENDERED_INLINE:
        return ""
    if kind in _LINK_NODE_TYPES or kind == _IMAGE:
        words = _first_child(node, _IMAGE_DESCRIPTION if kind == _IMAGE else _LINK_TEXT)
        return "" if words is None else _rendered_text(words, text)
    raw = _decode(node, text)
    if kind in _AUTOLINKS:
        return raw[1:-1]
    if kind == _BACKSLASH_ESCAPE:
        return raw[1:]
    if kind in _CHARACTER_REFERENCES:
        return html.unescape(raw)
    parts: list[str] = []
    cursor = node.start_byte
    for child in node.children:
        parts.append(_decode_span(text, cursor, child.start_byte))
        parts.append(_rendered_text(child, text))
        cursor = child.end_byte
    parts.append(_decode_span(text, cursor, node.end_byte))
    return "".join(parts)


def _decode_span(text: bytes, start: int, end: int) -> str:
    return text[start:end].decode(cs.ENCODING_UTF8, errors="replace")


def _decode(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode(
        cs.ENCODING_UTF8, errors="replace"
    )


def _collect_headings(root: Node) -> list[Node]:
    """Every heading in the document, in source order.

    Walks the whole tree rather than the top-level `section` children: ATX
    headings sit inside nested `section` nodes, setext headings sit flat
    inside their parent section, and both must come back in one ordered list
    for the level stack to rebuild the hierarchy.
    """
    found: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _HEADING_TYPES:
            found.append(node)
            # A heading never contains another heading; no need to descend.
            continue
        stack.extend(reversed(node.children))
    found.sort(key=lambda n: n.start_byte)
    return found


def _last_line(source: bytes) -> int:
    """The 1-based last line of the document.

    A trailing newline ends the final line rather than starting an empty one,
    so it must not add a line the file does not have.
    """
    if not source:
        return 1
    trimmed = source[:-1] if source.endswith(b"\n") else source
    return trimmed.count(b"\n") + 1


def _section_end_line(
    levelled: list[tuple[Node, int]], index: int, level: int, last_line: int
) -> int:
    """The last line a section owns: its heading plus the prose beneath it.

    A section runs until the next heading at the same or a shallower level
    (a deeper heading is its child and stays inside its span), or to the end
    of the file when nothing closes it. Using the heading node's own end
    instead would report a one- or two-line span for every section.
    """
    # Indexed rather than sliced: `levelled[index + 1:]` would copy the tail
    # for every heading, which is quadratic across a heading-dense document
    # even though the loop below usually stops at the very first entry.
    heading_end = levelled[index][0].end_point[0] + 1
    for next_index in range(index + 1, len(levelled)):
        next_heading, next_level = levelled[next_index]
        if next_level <= level:
            # The line before the closing heading; a heading immediately
            # after another leaves the parent owning only its own line.
            return max(next_heading.start_point[0], heading_end)
    return max(last_line, heading_end)


def _collect_by_type(root: Node, node_type: str) -> list[Node]:
    """Every node of one type in the tree, in source order.

    Does not descend into a match: neither an `inline` span nor a link
    reference definition nests another of its own kind.
    """
    found: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == node_type:
            found.append(node)
            continue
        stack.extend(reversed(node.children))
    found.sort(key=lambda n: n.start_byte)
    return found


def _collect_inline_spans(root: Node) -> list[Node]:
    """Every `inline` node in the block tree, in source order."""
    return _collect_by_type(root, _INLINE)


def _normalise_label(label: str) -> str:
    """A reference label reduced to its match key.

    CommonMark compares labels case-insensitively and treats runs of internal
    whitespace as a single space, so ``[API Guide]`` and ``[api  guide]`` name
    the same definition.
    """
    return " ".join(label.split()).casefold()


def _first_child(node: Node, child_type: str) -> Node | None:
    """The node's first direct child of a type, or None.

    Every link node carries the part that matters (a destination, a label, the
    link text) as one direct child, so this is the shared shape.
    """
    for child in node.children:
        if child.type == child_type:
            return child
    return None


def _label_used_by(node: Node, text: bytes) -> str | None:
    """The definition label a reference link names, or None for other nodes.

    ``[text][label]`` names its label explicitly, in a node that includes the
    surrounding brackets. ``[label][]`` and ``[label]`` carry no label node and
    use their own link text instead.
    """
    if node.type == _FULL_REFERENCE_LINK:
        label = _first_child(node, _LINK_LABEL)
        return (
            None
            if label is None
            else _normalise_label(_decode(label, text).strip("[]"))
        )
    if node.type in _TEXT_LABELLED_LINKS:
        label = _first_child(node, _LINK_TEXT)
        return None if label is None else _normalise_label(_decode(label, text))
    return None


def _link_site_destination(
    node: Node, text: bytes, definitions: dict[str, str]
) -> str | None:
    """The destination a link node states, or None when it is not a link.

    A reference link takes its definition's destination. One whose label no
    definition names is not a link at all: `[x]` alone is bracketed text.
    """
    if node.type == _INLINE_LINK:
        destination = _first_child(node, _LINK_DESTINATION)
        return None if destination is None else _decode(destination, text)
    label = _label_used_by(node, text)
    return None if label is None else definitions.get(label)


def _link_sites_in(
    inline_root: Node, text: bytes, offset: int, definitions: dict[str, str]
) -> list[_LinkSite]:
    """Every link in one re-parsed inline span, at its byte span in the file."""
    found: list[_LinkSite] = []
    stack = [inline_root]
    while stack:
        node = stack.pop()
        if node.type in _LINK_NODE_TYPES:
            destination = _link_site_destination(node, text, definitions)
            if destination is not None:
                words = _first_child(node, _LINK_TEXT)
                found.append(
                    _LinkSite(
                        offset + node.start_byte,
                        offset + node.end_byte,
                        destination,
                        # Reflowed link text keeps one spelling.
                        "" if words is None else " ".join(_decode(words, text).split()),
                    )
                )
            # A link cannot nest another link; no need to descend.
            continue
        stack.extend(reversed(node.children))
    return found


def _reference_definitions(root: Node, source: bytes) -> dict[str, str]:
    """Each reference label's destination, by its normalised label.

    Definitions are block-level, so they parse whether or not the inline
    grammar loaded. CommonMark resolves a duplicated label to its FIRST
    definition, so a document that redefines `[api]` links to one file
    rather than to both; `_collect_by_type` returns definitions in source
    order, which is what makes "first seen wins" the same as "first in the
    document".
    """
    definitions: dict[str, str] = {}
    for node in _collect_by_type(root, _LINK_REFERENCE_DEFINITION):
        label_node = _first_child(node, _LINK_LABEL)
        destination = _first_child(node, _LINK_DESTINATION)
        if label_node is None or destination is None:
            continue
        label = _normalise_label(_decode(label_node, source).strip("[]"))
        definitions.setdefault(label, _decode(destination, source))
    return definitions


def _collect_link_sites(
    root: Node, source: bytes, inline_parser: Parser | None
) -> list[_LinkSite]:
    """Every link in the document, in source order, one entry per use.

    The same target linked twice is two link sites. Each `inline` span is
    re-parsed with the inline grammar, since the block tree leaves inline
    content opaque; a span that fails to parse is skipped rather than
    aborting the document. Returns nothing when the inline grammar is
    unavailable, so heading extraction still works on an install where only
    the block parser loaded.
    """
    if inline_parser is None:
        return []
    definitions = _reference_definitions(root, source)
    sites: list[_LinkSite] = []
    for span in _collect_inline_spans(root):
        text = source[span.start_byte : span.end_byte]
        try:
            inline_root = inline_parser.parse(text).root_node
        except (RuntimeError, ValueError):
            continue
        sites.extend(_link_sites_in(inline_root, text, span.start_byte, definitions))
    sites.sort()
    return sites


def _is_external(destination: str) -> bool:
    """True when the destination does not name a file in this repository.

    Absolute URLs and mail links resolve somewhere other than a repo-relative
    path, so treating them as file links would invent edges to files that do
    not exist. A bare fragment is not external: it names the document itself.
    """
    if not destination:
        return True
    # "//host/path" inherits the page's scheme; it names a host, not a file.
    # It begins with a slash, so root-relative resolution would otherwise map
    # it onto a repository path and invent an edge whenever one happens to
    # exist there.
    if destination.startswith(_NETWORK_PATH_PREFIX):
        return True
    if _URI_SCHEME_SEPARATOR in destination:
        return True
    lowered = destination.lower()
    if lowered.startswith(_MAILTO_PREFIX):
        return True
    # "scheme:rest" with a plausible scheme, excluding "C:/path".
    scheme, separator, _ = destination.partition(":")
    return bool(separator) and len(scheme) > _MIN_SCHEME_LENGTH and scheme.isalpha()


def _percent_decode(target: str) -> str:
    """A link destination with its percent escapes resolved.

    Markdown writers escape spaces and other characters in destinations, so
    ``My%20Guide.md`` names the file ``My Guide.md``; resolving the raw text
    looks for a filename nobody has and drops the edge silently.

    ``unquote`` leaves an invalid escape exactly as written, so a file really
    named ``100%.md`` survives — ``%.m`` is not a valid escape sequence.
    """
    if "%" not in target:
        return target
    return unquote(target)


def _split_destination(destination: str) -> tuple[str, str]:
    """The path a destination names, as written, and its fragment.

    ``guide.md#install`` points at a section of ``guide.md``; the file is the
    part before the fragment. Angle-bracket forms (``<a b.md>``) wrap targets
    containing spaces. The path keeps its percent escapes: it is what a
    broken link is listed as, and `_percent_decode` turns it into a name.
    """
    target = destination.strip()
    if target.startswith(_ANGLE_OPEN) and target.endswith(_ANGLE_CLOSE):
        target = target[1:-1]
    path, _, fragment = target.partition(_FRAGMENT_PREFIX)
    return path.strip(), fragment.strip()


def broken_link_keys(document_key: str, written: Iterable[str]) -> set[str]:
    """The repository paths a document's broken links name.

    `written` is what the document's `broken_links` holds, relative to the
    document (or to the repository root, with a leading "/"). A path that
    climbs out of the repository names nothing a sync could create.
    """
    base = PurePosixPath(document_key).parent
    keys: set[str] = set()
    for entry in written:
        path, _ = _split_destination(entry)
        target = _percent_decode(path)
        joined = (
            target.lstrip(_ROOT_RELATIVE_PREFIX)
            if target.startswith(_ROOT_RELATIVE_PREFIX)
            else (base / target).as_posix()
        )
        key = posixpath.normpath(joined)
        if key != posixpath.pardir and not key.startswith(
            posixpath.pardir + posixpath.sep
        ):
            keys.add(key)
    return keys


def _github_slug(text: str) -> str:
    """The anchor GitHub renders for a heading's text."""
    kept = "".join(
        char
        for char in text.strip().lower()
        if char.isalnum()
        or char in _SLUG_KEPT
        or unicodedata.category(char).startswith(_UNICODE_MARK_PREFIX)
    )
    return kept.replace(_SLUG_SPACE, _SLUG_DASH)


def _unique_slug(base: str, seen: dict[str, int]) -> str:
    """`base`, or `base-N` for its Nth repeat in the document."""
    slug = base
    while slug in seen:
        seen[base] += 1
        slug = f"{base}{_SLUG_DASH}{seen[base]}"
    seen[slug] = 0
    return slug


def _anchor_key(fragment: str) -> str:
    """A link fragment reduced to the slug it should match."""
    return _percent_decode(fragment).strip().lower()


def _anchor_map(outline: list[_Heading]) -> dict[str, str]:
    """Each heading's anchor and the Section qn it names."""
    return {heading.slug: heading.qualified_name for heading in outline if heading.slug}


def _line_starts(source: bytes) -> list[int]:
    """The byte offset each line starts at, for offset -> (line, col)."""
    starts = [0]
    index = source.find(_LINE_BREAK)
    while index != -1:
        starts.append(index + 1)
        index = source.find(_LINE_BREAK, index + 1)
    return starts


def _position(line_starts: list[int], offset: int) -> tuple[int, int]:
    """A byte offset as a 1-based line and a 0-based byte column."""
    line = bisect_right(line_starts, offset)
    return line, offset - line_starts[line - 1]


def _sanitize(name: str) -> str:
    """A heading rendered safe for a dot-separated qualified name.

    Dots would create phantom hierarchy levels when a qn is split, so they
    become underscores; whitespace is collapsed so the same heading reflowed
    across lines keeps one identity.
    """
    collapsed = " ".join(name.split())
    return collapsed.replace(cs.SEPARATOR_DOT, "_") or _UNTITLED


def _outline(
    root: Node, source: bytes, module_qn: str, inline_parser: Parser | None
) -> list[_Heading]:
    """Every heading of a document as a Section, in source order.

    Shared by the document's own parse and by an anchor resolved from
    another document's link, so a link names exactly the qn the linked
    heading's Section is emitted under. The qn and name keep the heading as
    written; only the slug is taken from the rendered text.
    """
    # (heading node, level) for every heading, so each section's end can be
    # read off the NEXT heading that closes it.
    levelled: list[tuple[Node, int]] = []
    for heading in _collect_headings(root):
        level = _heading_level(heading)
        if level is not None:
            levelled.append((heading, level))
    last_line = _last_line(source)

    # (level, qualified_name) for the headings currently open above the one
    # being named; the nearest shallower entry is its parent.
    open_headings: list[tuple[int, str]] = []
    # Sibling headings can repeat a name ("## Notes" twice under one parent).
    # The qn is the node's identity, so a repeat would merge two distinct
    # sections into one node; the shared "<qn>@<start_line>" convention keeps
    # them apart, and consumers that split on the marker still recover the
    # heading name.
    claimed_qns: set[str] = set()
    slugs: dict[str, int] = {}
    outline: list[_Heading] = []
    for index, (heading, level) in enumerate(levelled):
        text = _heading_text(heading, source).strip()
        while open_headings and open_headings[-1][0] >= level:
            open_headings.pop()
        parent_qn = open_headings[-1][1] if open_headings else module_qn
        start_line = heading.start_point[0] + 1
        qualified_name = f"{parent_qn}{cs.SEPARATOR_DOT}{_sanitize(text)}"
        # A literal heading can already carry the marker ("## Notes@5"), so
        # one suffix is not guaranteed to be free; keep suffixing until the
        # name is unclaimed.
        while qualified_name in claimed_qns:
            qualified_name = f"{qualified_name}{cs.DUP_QN_MARKER}{start_line}"
        claimed_qns.add(qualified_name)
        outline.append(
            _Heading(
                qualified_name=qualified_name,
                name=text or _UNTITLED,
                level=level,
                start_line=start_line,
                end_line=_section_end_line(levelled, index, level, last_line),
                parent_qn=parent_qn,
                parent_is_module=not open_headings,
                # Every heading takes its slug, an empty one included:
                # GitHub numbers repeats across the whole document.
                slug=_unique_slug(
                    _github_slug(
                        _rendered_heading_text(heading, source, inline_parser)
                    ),
                    slugs,
                ),
            )
        )
        open_headings.append((level, qualified_name))
    return outline


class DocumentTier:
    """Extracts a nested Section graph from heading-structured documents."""

    __slots__ = (
        "_ingestor",
        "_repo_path",
        "_repo_root",
        "_project_name",
        "_parser",
        "_inline_parser",
        "_pending_links",
        "_anchors",
        "_sections_by_document",
        "_live_sections",
        "_broken_links",
        "_broken_documents",
    )

    def __init__(
        self, ingestor: IngestorProtocol, repo_path: Path, project_name: str
    ) -> None:
        self._ingestor = ingestor
        self._repo_path = repo_path
        self._repo_root = repo_path.resolve()
        self._project_name = project_name
        self._parser = _load_parser()
        self._inline_parser = _load_inline_parser()
        # One entry per link, held until the pass has buffered every File
        # and Section node a link can end at: see `emit_pending_links`.
        self._pending_links: list[_PendingLink] = []
        # Anchor -> Section qn per document (by absolute path), filled by
        # this pass's parses and, for a document this pass did not parse, on
        # first use. Cleared with the pending links, so a reused updater
        # never resolves against a document as it was on an earlier pass.
        self._anchors: dict[str, dict[str, str]] = {}
        # The Section qns each document's latest parse emitted, so a link
        # into one captured before an incremental re-parse is restored only
        # when the heading is still there (`emitted_section`).
        self._sections_by_document: dict[str, frozenset[str]] = {}
        self._live_sections: set[str] = set()
        # This pass's broken links and the documents holding them, for the
        # one summary line `emit_pending_links` writes.
        self._broken_links = 0
        self._broken_documents = 0

    def handles(self, suffix: str) -> bool:
        """True when this tier can parse the extension.

        False when the markdown grammar is not installed, so the caller falls
        through to the generic File node rather than losing the file.
        """
        return self._parser is not None and suffix.lower() in DOCUMENT_EXTENSIONS

    def emitted_section(self, qualified_name: str) -> bool:
        """Whether the latest parse of its document emitted this Section."""
        return qualified_name in self._live_sections

    def process_file(
        self, file_path: Path, structural_elements: dict[Path, str | None]
    ) -> None:
        parser = self._parser
        if parser is None:
            return
        try:
            source = file_path.read_bytes()
        except OSError:
            return
        try:
            root = parser.parse(source).root_node
        except (RuntimeError, ValueError) as exc:  # noqa: BLE001
            logger.warning("markdown parse failed for {}: {}", file_path, exc)
            return

        absolute_path = cached_resolve_posix(file_path)
        located, broken = self._locate_links(root, source, file_path, absolute_path)

        # Declared front-matter becomes Module properties (issue #1448).
        # Decoded leniently: a document with an invalid byte should still be
        # indexed, and its metadata is a bonus rather than a precondition.
        declared = parse_front_matter(source.decode("utf-8", errors="replace"))
        # Emitted as ONE declared `front_matter` property holding "key=value"
        # entries, not as a property per key. The graph's node schema is a
        # fixed property list audited on every ingest, so arbitrary keys would
        # be undocumented properties -- the audit catches exactly that, and it
        # is right to: a document could otherwise define any node property it
        # liked, including ones a future schema wants for something else.
        # ALWAYS emitted, empty list included. The ingestor upserts with
        # `SET n += row.props` (cypher_queries.py:317), which MERGES: a key
        # omitted on re-ingest keeps its previous value. So a document that
        # drops its front-matter would keep the old metadata bound to its node
        # forever, and the graph would assert a declaration the file no longer
        # makes (reported on #1488).
        #
        # An empty list overwrites; omission cannot. That makes "no
        # front-matter" a value the re-ingest can actually store, rather than
        # the absence of one. `broken_links` is emitted the same way, so a
        # link fixed since the last parse stops being listed (issue #2458).
        module_props: PropertyDict = {
            cs.KEY_FRONT_MATTER: [
                _front_matter_entry(k, v) for k, v in sorted(declared.items())
            ],
            cs.KEY_BROKEN_LINKS: broken,
        }
        module_qn = self._emit_module(file_path, structural_elements, module_props)
        relative_path = cached_relative_path(file_path, self._repo_path).as_posix()

        outline = _outline(root, source, module_qn, self._inline_parser)
        for heading in outline:
            self._emit_section(heading, relative_path, absolute_path)
        self._anchors[absolute_path] = _anchor_map(outline)
        self._record_sections(absolute_path, outline)
        self._queue_links(located, outline, module_qn, source)
        if broken:
            self._broken_links += len(broken)
            self._broken_documents += 1

    def _locate_links(
        self, root: Node, source: bytes, file_path: Path, absolute_path: str
    ) -> tuple[list[_LocatedLink], list[str]]:
        """The links that name a repository file, and the broken ones.

        A link becomes an edge only when its target exists on disk. A target
        missing from the repository -- a typo, a file deleted since the link
        was written -- would otherwise create an edge to a node nobody emits,
        so it is listed as broken instead (issue #2458): the path as written,
        once, in source order. A link outside the repository, to a directory,
        or to an external URL names no file here and is neither.
        """
        located: list[_LocatedLink] = []
        broken: list[str] = []
        for site in _collect_link_sites(root, source, self._inline_parser):
            if _is_external(site.destination):
                continue
            path, fragment = _split_destination(site.destination)
            if not path:
                # `#usage` names a heading of this very document; `#` alone
                # (the top of the page) names nothing worth an edge.
                if fragment:
                    located.append(_LocatedLink(site, absolute_path, fragment))
                continue
            target, missing = self._resolve_link(file_path, _percent_decode(path))
            if target is not None:
                located.append(_LocatedLink(site, target, fragment))
            elif missing and path not in broken:
                broken.append(path)
        return located, broken

    def _queue_links(
        self,
        located: list[_LocatedLink],
        outline: list[_Heading],
        module_qn: str,
        source: bytes,
    ) -> None:
        """One pending edge per link, from the innermost section holding it.

        A section runs from its heading to the next heading at its level or
        above, and a deeper heading's span sits inside it, so the innermost
        section around a line is simply the last heading at or before it.
        A link above the first heading belongs to the document itself.
        """
        line_starts = _line_starts(source)
        heading_lines = [heading.start_line for heading in outline]
        for link in located:
            line, col = _position(line_starts, link.site.start_byte)
            end_line, end_col = _position(line_starts, link.site.end_byte)
            enclosing = bisect_right(heading_lines, line)
            origin: tuple[cs.NodeLabel, str, str] = (
                (
                    cs.NodeLabel.SECTION,
                    cs.KEY_QUALIFIED_NAME,
                    outline[enclosing - 1].qualified_name,
                )
                if enclosing
                else (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
            )
            properties: PropertyDict = {
                cs.KEY_LINE: line,
                cs.KEY_COL: col,
                cs.KEY_END_LINE: end_line,
                cs.KEY_END_COL: end_col,
                cs.KEY_LINK_TEXT: link.site.text,
            }
            if link.fragment:
                properties[cs.KEY_ANCHOR] = link.fragment
            self._pending_links.append(
                _PendingLink(origin, link.target, link.fragment, properties)
            )

    def _record_sections(self, absolute_path: str, outline: list[_Heading]) -> None:
        current = frozenset(heading.qualified_name for heading in outline)
        previous = self._sections_by_document.get(absolute_path, frozenset())
        self._live_sections.difference_update(previous - current)
        self._live_sections.update(current)
        self._sections_by_document[absolute_path] = current

    def emit_pending_links(self) -> int:
        """Buffer the LINKS_TO edges held since the last call; how many.

        A relationship is written by MATCHing both endpoints, and a link's
        target File node is only buffered when the pass reaches that file.
        Buffered as soon as the document was parsed, a link to a file parsed
        later was dropped by any flush in between (a full batch, the periodic
        file-interval flush), so the links a fresh index kept depended on
        `--batch-size` and on parse order (issue #2400). The updater calls
        this once every File node of the pass is buffered; every flush writes
        nodes before relationships, so the targets then exist.

        Anchors resolve here for the same reason: a link into a document
        parsed later in the pass needs that document's headings (#2458).
        """
        pending, self._pending_links = self._pending_links, []
        for link in pending:
            section = (
                self._anchors_of(link.target).get(_anchor_key(link.fragment))
                if link.fragment
                else None
            )
            target: tuple[cs.NodeLabel, str, str] = (
                (cs.NodeLabel.SECTION, cs.KEY_QUALIFIED_NAME, section)
                if section is not None
                else (cs.NodeLabel.FILE, cs.KEY_ABSOLUTE_PATH, link.target)
            )
            self._ingestor.ensure_relationship_batch(
                link.source, cs.RelationshipType.LINKS_TO, target, link.properties
            )
        if self._broken_links:
            logger.info(
                ls.MARKDOWN_BROKEN_LINKS,
                count=self._broken_links,
                documents=self._broken_documents,
            )
        self._broken_links = 0
        self._broken_documents = 0
        self._anchors.clear()
        return len(pending)

    def _anchors_of(self, target: str) -> dict[str, str]:
        """Anchor -> Section qn for the document at `target`, or {}.

        A document this pass did not parse (an incremental sync re-parsing
        only the one linking to it) is read from disk here, and named the
        way `emit_flat_module` names it, so the qn matches the Section its
        own last parse emitted.
        """
        cached = self._anchors.get(target)
        if cached is not None:
            return cached
        anchors: dict[str, str] = {}
        path = Path(target)
        if self._parser is not None and path.suffix.lower() in DOCUMENT_EXTENSIONS:
            try:
                source = path.read_bytes()
                root = self._parser.parse(source).root_node
                relative = path.relative_to(self._repo_root)
            except (OSError, RuntimeError, ValueError):
                pass
            else:
                module_qn = flat_module_qn(
                    self._project_name, relative, distinguish_suffix=True
                )
                anchors = _anchor_map(
                    _outline(root, source, module_qn, self._inline_parser)
                )
        self._anchors[target] = anchors
        return anchors

    def _resolve_link(self, file_path: Path, target: str) -> tuple[str | None, bool]:
        """The file a link target names, and whether the link is broken.

        Returns (absolute path, False) for a repository file, (None, True)
        for a repository path that does not exist, and (None, False) for
        anything else: a directory, a path outside the repository, an
        unreadable one. Targets are relative to the linking document's
        directory, except a leading "/" which the common convention treats as
        repo-root-relative rather than filesystem-absolute. Anything landing
        outside the repository is rejected, so a "../../.." traversal cannot
        attach an edge to a file the project does not contain.
        """
        try:
            if target.startswith(_ROOT_RELATIVE_PREFIX):
                candidate = self._repo_path / target.lstrip(_ROOT_RELATIVE_PREFIX)
            else:
                candidate = file_path.parent / target
            resolved = candidate.resolve()
            resolved.relative_to(self._repo_root)
            if resolved.is_file():
                return resolved.as_posix(), False
            return None, not resolved.exists()
        except (OSError, ValueError):
            return None, False

    def _emit_module(
        self,
        file_path: Path,
        structural_elements: dict[Path, str | None],
        properties: PropertyDict | None = None,
    ) -> str:
        return emit_flat_module(
            self._ingestor,
            self._repo_path,
            self._project_name,
            file_path,
            structural_elements,
            # This tier accepts .md AND .markdown, so dropping the suffix
            # would merge "guide.md" and "guide.markdown" onto one Module
            # node and merge their same-named sections with it.
            distinguish_suffix=True,
            extra_properties=properties,
        )

    def _emit_section(
        self, heading: _Heading, relative_path: str, absolute_path: str
    ) -> None:
        self._ingestor.ensure_node_batch(
            cs.NodeLabel.SECTION,
            {
                cs.KEY_QUALIFIED_NAME: heading.qualified_name,
                cs.KEY_NAME: heading.name,
                cs.KEY_HEADING_LEVEL: heading.level,
                cs.KEY_START_LINE: heading.start_line,
                cs.KEY_END_LINE: heading.end_line,
                cs.KEY_PATH: relative_path,
                cs.KEY_ABSOLUTE_PATH: absolute_path,
            },
        )
        parent_label = (
            cs.NodeLabel.MODULE if heading.parent_is_module else cs.NodeLabel.SECTION
        )
        self._ingestor.ensure_relationship_batch(
            (parent_label, cs.KEY_QUALIFIED_NAME, heading.parent_qn),
            cs.RelationshipType.CONTAINS_SECTION,
            (cs.NodeLabel.SECTION, cs.KEY_QUALIFIED_NAME, heading.qualified_name),
        )


def _load_parser() -> Parser | None:
    """A markdown Parser, or None when the optional grammar is absent.

    The grammar ships in the `treesitter-full` extra; a base install must keep
    indexing documents as plain File nodes rather than failing.
    """
    try:
        import tree_sitter_markdown
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    try:
        return Parser(Language(tree_sitter_markdown.language()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("markdown grammar unavailable, document tier disabled: {}", exc)
        return None


def _load_inline_parser() -> Parser | None:
    """A markdown *inline* Parser, or None when it is unavailable.

    Separate from `_load_parser` because tree-sitter-markdown splits block and
    inline grammars: links live only in the inline tree. Its absence disables
    link edges alone — heading extraction runs off the block parser and must
    keep working, so this never disables the tier.
    """
    try:
        import tree_sitter_markdown
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    try:
        return Parser(Language(tree_sitter_markdown.inline_language()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("markdown inline grammar unavailable, no link edges: {}", exc)
        return None
