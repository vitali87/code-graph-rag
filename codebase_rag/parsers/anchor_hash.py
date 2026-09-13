"""Content hash of one definition, for telling whether a note about it is stale.

Issue #1808 measured which level of hashing a Gloss should be graded against:

| edit             | raw text | this hash | shape only (`ast_fingerprint`) |
|------------------|----------|-----------|--------------------------------|
| reformatting     | flips    | same      | same                           |
| local rename     | flips    | flips     | same                           |
| logic change     | flips    | flips     | flips                          |

`ast_fingerprint` is the clone-detection skeleton: identifiers and literals
collapse to placeholders and the signature is excluded, so a renamed local or
a changed constant leaves it unchanged, which is exactly the edit a note like
"safe because `validate()` runs first" must notice. This hash keeps identifier
and literal TEXT, keeps the signature (a signature change is when a note
should go stale, Unison's `incorporateType` precedent), and drops what
formatters move: whitespace (never a node), punctuation, and comments. It is
the tree-sitter equivalent of Python's `ast.dump()`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from tree_sitter import Node

from .. import constants as cs
from ..types_defs import PropertyDict

_SEPARATOR = b"\x00"
_OPEN = b"\x01"
_CLOSE = b"\x02"


def anchor_hash(definition: Node, decorators: Sequence[str] = ()) -> str:
    """Hex digest of the definition's named-node tree with leaf text.

    `decorators` are the already-extracted decorator / annotation names. In
    Python and TypeScript they are siblings or parents of the definition
    node rather than children, so the tree walk alone would miss removing
    `@property`, which is a signature change by any reading; folding the
    extracted list in makes every language agree (Java's `@Override` IS a
    child and would be hashed twice, harmlessly).
    """
    digest = hashlib.sha256()
    for decorator in decorators:
        digest.update(decorator.encode(cs.ENCODING_UTF8) + _SEPARATOR)
    # Iterative: deep trees overflow Python recursion, as `ast_fingerprint`
    # found. Open/close markers keep the nesting in the digest so moving a
    # statement into or out of a block changes it.
    stack: list[tuple[Node, bool]] = [(definition, False)]
    while stack:
        node, closing = stack.pop()
        if closing:
            digest.update(_CLOSE)
            continue
        if cs.AST_FP_COMMENT_SUBSTRING in node.type:
            continue
        if not node.is_named:
            # Keywords and operators distinguish `a + b` from `a - b` and
            # `if` from `while`; punctuation is layout and is dropped.
            if node.type not in cs.AST_FP_PUNCT_TYPES:
                digest.update(node.type.encode(cs.ENCODING_UTF8) + _SEPARATOR)
            continue
        digest.update(_OPEN + node.type.encode(cs.ENCODING_UTF8) + _SEPARATOR)
        if node.child_count == 0:
            # A leaf carries its text: an identifier's name, a literal's value.
            digest.update((node.text or b"") + _SEPARATOR)
        stack.append((node, True))
        # Reversed so children are visited in source order.
        stack.extend((child, False) for child in reversed(node.children))
    return digest.hexdigest()


def anchor_hash_props(definition: Node, decorators: Sequence[str] = ()) -> PropertyDict:
    return {cs.KEY_ANCHOR_HASH: anchor_hash(definition, decorators)}
