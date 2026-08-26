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

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from .. import constants as cs
from ..utils.path_utils import cached_relative_path, cached_resolve_posix
from .flat_module import emit_flat_module

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

# Link grammar nodes. `inline_link` is ``[text](target)``; the destination sits
# in a `link_destination` child. Reference-style links (``[text][label]``) name
# a definition elsewhere and carry no destination of their own, so they are not
# resolvable to a file here.
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

# A destination that names something other than a file in this repository.
# Scheme-bearing targets (http:, https:, mailto:, ftp:) are external, and a
# bare fragment ("#section") points inside the current document.
_URI_SCHEME_SEPARATOR = "://"
_MAILTO_PREFIX = "mailto:"
_FRAGMENT_PREFIX = "#"
# Windows drive letters ("C:/x") would otherwise read as a scheme.
_MIN_SCHEME_LENGTH = 2

# A heading with no text (`##` alone) has nothing to name a node after.
_UNTITLED = "(untitled)"


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


def _heading_text(node: Node, source: bytes) -> str:
    """The heading's own text, without its marker or underline.

    ATX puts the text in a direct `inline` child. Setext wraps it one level
    deeper, in a `paragraph` holding the `inline`, so a direct-children-only
    reader silently returns nothing for every setext heading.
    """
    for child in node.children:
        if child.type == _INLINE:
            return _decode(child, source)
        if child.type == _PARAGRAPH:
            for grandchild in child.children:
                if grandchild.type == _INLINE:
                    return _decode(grandchild, source)
    return ""


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


def _link_destinations_in(inline_root: Node, text: bytes) -> list[tuple[int, str]]:
    """(offset, destination) for each link in one parsed inline span."""
    found: list[tuple[int, str]] = []
    stack = [inline_root]
    while stack:
        node = stack.pop()
        if node.type == _INLINE_LINK:
            for child in node.children:
                if child.type == _LINK_DESTINATION:
                    found.append((child.start_byte, _decode(child, text)))
                    break
            # A link cannot nest another link; no need to descend.
            continue
        stack.extend(reversed(node.children))
    return found


def _collect_link_destinations(
    root: Node, source: bytes, inline_parser: Parser | None
) -> list[str]:
    """Every inline-link destination in the document, in source order.

    Duplicates are kept: the same target linked twice is two link sites, and
    de-duplication is the caller's business once targets are resolved.

    Returns nothing when the inline grammar is unavailable, so heading
    extraction still works on an install where only the block parser loaded.
    """
    found: list[tuple[int, int, str]] = []

    # Reference definitions are block-level, so they are read whether or not
    # the inline grammar loaded.
    for node in _collect_by_type(root, _LINK_REFERENCE_DEFINITION):
        for child in node.children:
            if child.type == _LINK_DESTINATION:
                found.append((child.start_byte, 0, _decode(child, source)))
                break

    if inline_parser is not None:
        for span in _collect_inline_spans(root):
            text = source[span.start_byte : span.end_byte]
            try:
                inline_root = inline_parser.parse(text).root_node
            except (RuntimeError, ValueError):
                continue
            for offset, destination in _link_destinations_in(inline_root, text):
                found.append((span.start_byte, offset, destination))

    found.sort()
    return [destination for _, _, destination in found]


def _is_external(destination: str) -> bool:
    """True when the destination does not name a file in this repository.

    Absolute URLs, mail links, and bare fragments all resolve somewhere other
    than a repo-relative path, so treating them as file links would invent
    edges to files that do not exist.
    """
    if not destination or destination.startswith(_FRAGMENT_PREFIX):
        return True
    if _URI_SCHEME_SEPARATOR in destination:
        return True
    lowered = destination.lower()
    if lowered.startswith(_MAILTO_PREFIX):
        return True
    # "scheme:rest" with a plausible scheme, excluding "C:/path".
    scheme, separator, _ = destination.partition(":")
    return bool(separator) and len(scheme) > _MIN_SCHEME_LENGTH and scheme.isalpha()


def _strip_target(destination: str) -> str:
    """The path part of a destination, without any anchor or title.

    ``guide.md#install`` points at a section of ``guide.md``; the file is the
    part before the fragment. Angle-bracket forms (``<a b.md>``) wrap targets
    containing spaces.
    """
    target = destination.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    target, _, _ = target.partition(_FRAGMENT_PREFIX)
    return target.strip()


def _sanitize(name: str) -> str:
    """A heading rendered safe for a dot-separated qualified name.

    Dots would create phantom hierarchy levels when a qn is split, so they
    become underscores; whitespace is collapsed so the same heading reflowed
    across lines keeps one identity.
    """
    collapsed = " ".join(name.split())
    return collapsed.replace(cs.SEPARATOR_DOT, "_") or _UNTITLED


class DocumentTier:
    """Extracts a nested Section graph from heading-structured documents."""

    __slots__ = (
        "_ingestor",
        "_repo_path",
        "_project_name",
        "_parser",
        "_inline_parser",
    )

    def __init__(
        self, ingestor: IngestorProtocol, repo_path: Path, project_name: str
    ) -> None:
        self._ingestor = ingestor
        self._repo_path = repo_path
        self._project_name = project_name
        self._parser = _load_parser()
        self._inline_parser = _load_inline_parser()

    def handles(self, suffix: str) -> bool:
        """True when this tier can parse the extension.

        False when the markdown grammar is not installed, so the caller falls
        through to the generic File node rather than losing the file.
        """
        return self._parser is not None and suffix.lower() in DOCUMENT_EXTENSIONS

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

        module_qn = self._emit_module(file_path, structural_elements)
        relative_path = cached_relative_path(file_path, self._repo_path).as_posix()
        absolute_path = cached_resolve_posix(file_path)

        # (level, qualified_name) for the headings currently open above the
        # one being emitted; the nearest shallower entry is its parent.
        open_headings: list[tuple[int, str]] = []
        # Sibling headings can repeat a name ("## Notes" twice under one
        # parent). The qn is the node's identity, so a repeat would merge two
        # distinct sections into one node; the shared "<qn>@<start_line>"
        # convention keeps them apart, and consumers that split on the marker
        # still recover the heading name.
        claimed_qns: set[str] = set()

        # (heading node, level) for every heading, so each section's end can
        # be read off the NEXT heading that closes it.
        levelled: list[tuple[Node, int]] = []
        for heading in _collect_headings(root):
            level = _heading_level(heading)
            if level is not None:
                levelled.append((heading, level))
        last_line = _last_line(source)

        for index, (heading, level) in enumerate(levelled):
            text = _heading_text(heading, source).strip()
            name = _sanitize(text)

            while open_headings and open_headings[-1][0] >= level:
                open_headings.pop()

            parent_qn = open_headings[-1][1] if open_headings else module_qn
            start_line = heading.start_point[0] + 1
            qualified_name = f"{parent_qn}{cs.SEPARATOR_DOT}{name}"
            # A literal heading can already carry the marker ("## Notes@5"),
            # so one suffix is not guaranteed to be free; keep suffixing
            # until the name is unclaimed.
            while qualified_name in claimed_qns:
                qualified_name = f"{qualified_name}{cs.DUP_QN_MARKER}{start_line}"
            claimed_qns.add(qualified_name)

            self._emit_section(
                qualified_name=qualified_name,
                name=text or _UNTITLED,
                level=level,
                start_line=start_line,
                end_line=_section_end_line(levelled, index, level, last_line),
                parent_qn=parent_qn,
                parent_is_module=not open_headings,
                relative_path=relative_path,
                absolute_path=absolute_path,
            )
            open_headings.append((level, qualified_name))

        self._emit_links(root, source, file_path, module_qn)

    def _emit_links(
        self, root: Node, source: bytes, file_path: Path, module_qn: str
    ) -> None:
        """A LINKS_TO edge per relative link that resolves to a repo file.

        Only links whose target exists on disk become edges. An unresolvable
        target — a typo, a file deleted since the link was written, a path
        outside the repository — would otherwise create an edge to a node
        nobody emits, which reads in the graph as a file that does not exist.
        Dropping them keeps a broken link out of the graph rather than
        inventing a phantom.
        """
        emitted: set[str] = set()
        for destination in _collect_link_destinations(
            root, source, self._inline_parser
        ):
            if _is_external(destination):
                continue
            target = _strip_target(destination)
            if not target:
                continue
            resolved = self._resolve_link(file_path, target)
            if resolved is None or resolved in emitted:
                continue
            emitted.add(resolved)
            self._ingestor.ensure_relationship_batch(
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                cs.RelationshipType.LINKS_TO,
                (cs.NodeLabel.FILE, cs.KEY_ABSOLUTE_PATH, resolved),
            )

    def _resolve_link(self, file_path: Path, target: str) -> str | None:
        """The absolute path a link target names, or None when it is not a file.

        Targets are relative to the linking document's directory, except a
        leading "/" which the common convention treats as repo-root-relative
        rather than filesystem-absolute. Anything landing outside the
        repository is rejected, so a "../../.." traversal cannot attach an
        edge to a file the project does not contain.
        """
        try:
            if target.startswith("/"):
                candidate = self._repo_path / target.lstrip("/")
            else:
                candidate = file_path.parent / target
            resolved = candidate.resolve()
            if not resolved.is_file():
                return None
            resolved.relative_to(self._repo_path.resolve())
        except (OSError, ValueError):
            return None
        return resolved.as_posix()

    def _emit_module(
        self, file_path: Path, structural_elements: dict[Path, str | None]
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
        )

    def _emit_section(
        self,
        *,
        qualified_name: str,
        name: str,
        level: int,
        start_line: int,
        end_line: int,
        parent_qn: str,
        parent_is_module: bool,
        relative_path: str,
        absolute_path: str,
    ) -> None:
        self._ingestor.ensure_node_batch(
            cs.NodeLabel.SECTION,
            {
                cs.KEY_QUALIFIED_NAME: qualified_name,
                cs.KEY_NAME: name,
                cs.KEY_HEADING_LEVEL: level,
                cs.KEY_START_LINE: start_line,
                cs.KEY_END_LINE: end_line,
                cs.KEY_PATH: relative_path,
                cs.KEY_ABSOLUTE_PATH: absolute_path,
            },
        )
        parent_label = cs.NodeLabel.MODULE if parent_is_module else cs.NodeLabel.SECTION
        self._ingestor.ensure_relationship_batch(
            (parent_label, cs.KEY_QUALIFIED_NAME, parent_qn),
            cs.RelationshipType.CONTAINS_SECTION,
            (cs.NodeLabel.SECTION, cs.KEY_QUALIFIED_NAME, qualified_name),
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
