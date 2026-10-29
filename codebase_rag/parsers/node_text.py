"""Text and query helpers every parser module reads a tree-sitter node through.

A leaf: it imports nothing from the parser hubs, so `language_spec`,
`parsers.utils` and each language's helpers can import it at module level.
"""

from __future__ import annotations

from functools import lru_cache

from tree_sitter import Language, Query, QueryCursor

from .. import constants as cs
from ..types_defs import ASTNode, TreeSitterNodeProtocol


def decode_node_text(raw: bytes) -> str:
    """Decode node text without letting one bad byte delete a whole file.

    `node.text` is a slice of the raw file bytes, which are never validated
    as UTF-8. A strict decode raises `UnicodeDecodeError`, and the per-file
    handler in `graph_updater` catches it and abandons the file, so a single
    undecodable byte drops EVERY definition in that file from the graph --
    including definitions nowhere near the bad byte (issue #1797).

    Several passes decode node text independently -- the name extractors
    below, the call pass, and `parsers.utils.safe_decode_text` (decorators,
    modifiers, import paths) -- and each one abandons the file on its own.
    Every such site must route through here; fixing one leaves the rest
    losing data on the same input.

    `errors="replace"` keeps the rest of the file indexable and leaves a
    visibly mangled identifier, matching what `document_tier`, `java.lombok`
    and `build_lock` already do with the same class of input.
    """
    return raw.decode(cs.ENCODING_UTF8, errors="replace")


@lru_cache(maxsize=50000)
def _cached_decode_bytes(text_bytes: bytes) -> str:
    # Replacement decode, not strict. `safe_decode_text` feeds decorators,
    # modifiers and import paths, and a strict decode here raised
    # UnicodeDecodeError and abandoned the whole file -- one bad byte in a
    # Python decorator dropped every definition in it (issue #1797).
    return decode_node_text(text_bytes)


def safe_decode_text(node: ASTNode | TreeSitterNodeProtocol | None) -> str | None:
    """A node's text as `str`, or `None` when there is no text to decode.

    "Safe" covers both failure modes: a null node or null `text` yields
    `None`, and undecodable bytes are replaced rather than raised (the decode
    goes through `language_spec.decode_node_text`, `errors="replace"`), so one
    bad byte cannot drop a file's definitions (issue #1811). A `text` that is
    not `bytes` is returned through `str()`.
    """
    if node is None or (text_bytes := node.text) is None:
        return None
    if isinstance(text_bytes, bytes):
        return _cached_decode_bytes(text_bytes)
    return str(text_bytes)


def safe_decode_with_fallback(node: ASTNode | None, fallback: str = "") -> str:
    return result if (result := safe_decode_text(node)) is not None else fallback


_QUERY_CACHE: dict[tuple[Language, str], Query] = {}
_QUERY_LAST: tuple[tuple[Language, str], Query] | None = None


def get_cached_query(language_obj: Language, query_text: str) -> Query:
    # Key by the Language itself, never id(): Language hashes by grammar pointer,
    # so wrappers dedupe, and the dict pins the key so a GC'd wrapper's address
    # can't be reused to serve a wrong-grammar Query.
    global _QUERY_LAST
    key = (language_obj, query_text)
    if _QUERY_LAST is not None and _QUERY_LAST[0] == key:
        return _QUERY_LAST[1]
    if key not in _QUERY_CACHE:
        _QUERY_CACHE[key] = Query(language_obj, query_text)
    result = _QUERY_CACHE[key]
    _QUERY_LAST = (key, result)
    return result


def sorted_captures(cursor: QueryCursor, node: ASTNode) -> dict[str, list[ASTNode]]:
    # tree-sitter v0.25 captures() returns nodes in non-deterministic order;
    # sort by (start_byte, end_byte) for reproducibility. start_byte alone leaves
    # nested same-start captures (the outer `Greeter().greet()` chain and its
    # inner `Greeter()` call) in raw order, flipping between runs.
    raw = cursor.captures(node)
    result: dict[str, list[ASTNode]] = {}
    for name, nodes in raw.items():
        if len(nodes) <= 1:
            result[name] = nodes
        else:
            is_sorted = True
            prev_key = _span_key(nodes[0])
            for i in range(1, len(nodes)):
                cur_key = _span_key(nodes[i])
                if cur_key < prev_key:
                    is_sorted = False
                    break
                prev_key = cur_key
            result[name] = nodes if is_sorted else sorted(nodes, key=_span_key)
    return result


def _span_key(n: ASTNode) -> tuple[int, int]:
    return (n.start_byte, n.end_byte)
