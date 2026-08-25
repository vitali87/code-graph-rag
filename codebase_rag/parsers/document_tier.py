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

    __slots__ = ("_ingestor", "_repo_path", "_project_name", "_parser")

    def __init__(
        self, ingestor: IngestorProtocol, repo_path: Path, project_name: str
    ) -> None:
        self._ingestor = ingestor
        self._repo_path = repo_path
        self._project_name = project_name
        self._parser = _load_parser()

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

        for heading in _collect_headings(root):
            level = _heading_level(heading)
            if level is None:
                continue
            text = _heading_text(heading, source).strip()
            name = _sanitize(text)

            while open_headings and open_headings[-1][0] >= level:
                open_headings.pop()

            parent_qn = open_headings[-1][1] if open_headings else module_qn
            start_line = heading.start_point[0] + 1
            qualified_name = f"{parent_qn}{cs.SEPARATOR_DOT}{name}"
            if qualified_name in claimed_qns:
                qualified_name = f"{qualified_name}{cs.DUP_QN_MARKER}{start_line}"
            claimed_qns.add(qualified_name)

            self._emit_section(
                qualified_name=qualified_name,
                name=text or _UNTITLED,
                level=level,
                start_line=start_line,
                end_line=heading.end_point[0] + 1,
                parent_qn=parent_qn,
                parent_is_module=not open_headings,
                module_qn=module_qn,
                relative_path=relative_path,
                absolute_path=absolute_path,
            )
            open_headings.append((level, qualified_name))

    def _emit_module(
        self, file_path: Path, structural_elements: dict[Path, str | None]
    ) -> str:
        relative_path = cached_relative_path(file_path, self._repo_path)
        module_qn = cs.SEPARATOR_DOT.join(
            [self._project_name, *relative_path.with_suffix("").parts]
        )
        self._ingestor.ensure_node_batch(
            cs.NodeLabel.MODULE,
            {
                cs.KEY_QUALIFIED_NAME: module_qn,
                cs.KEY_NAME: file_path.name,
                cs.KEY_PATH: relative_path.as_posix(),
                cs.KEY_ABSOLUTE_PATH: cached_resolve_posix(file_path),
            },
        )
        parent_rel_path = relative_path.parent
        parent_container_qn = structural_elements.get(parent_rel_path)
        if parent_container_qn:
            parent = (cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, parent_container_qn)
        elif parent_rel_path != Path("."):
            parent = (
                cs.NodeLabel.FOLDER,
                cs.KEY_ABSOLUTE_PATH,
                cached_resolve_posix(self._repo_path / parent_rel_path),
            )
        else:
            parent = (cs.NodeLabel.PROJECT, cs.KEY_NAME, self._project_name)
        self._ingestor.ensure_relationship_batch(
            parent,
            cs.RelationshipType.CONTAINS_MODULE,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
        )
        return module_qn

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
        module_qn: str,
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
