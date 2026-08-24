"""Structural (AST) fingerprints for clone detection.

A function body is reduced to its *skeleton*: identifier-like leaves collapse
to one placeholder token, literal-like nodes to another (without descending
into them), comments and pure punctuation vanish, and every other node
contributes its tree-sitter type — so operators and keywords still
distinguish `a + b` from `a - b`. The skeleton is Merkle-hashed bottom-up:
equal fingerprints mean exact or renamed copies (Type-1/Type-2 clones), and
the per-branch digests of statement-level subtrees feed the near-duplicate
overlap scoring in `duplicates.py` (Type-3).

This module lives under `parsers/` deliberately: the parser fingerprint
hashes every source file here, so any change to this algorithm (or to the
classification sets in `constants/duplicates.py`) flags existing graphs as
stale instead of mixing fingerprint generations.
"""

from __future__ import annotations

import hashlib

from tree_sitter import Node

from .. import constants as cs
from ..types_defs import AstFingerprintResult, PropertyDict
from .dart.utils import dart_body_node


def fingerprint_props(func_node: Node) -> PropertyDict:
    """Fingerprint properties for a function node, empty when it has no body."""
    result = compute_ast_fingerprint(func_node)
    if result is None:
        return {}
    return {
        cs.KEY_AST_FINGERPRINT: result.fingerprint,
        cs.KEY_AST_FINGERPRINT_NODES: result.node_count,
        cs.KEY_AST_BRANCH_FINGERPRINTS: result.branch_fingerprints,
    }


def compute_ast_fingerprint(func_node: Node) -> AstFingerprintResult | None:
    body = _resolve_body(func_node)
    if body is None:
        return None
    return _fingerprint_subtree(body)


def _resolve_body(func_node: Node) -> Node | None:
    """The subtree holding a definition's logic, or None for bodiless nodes."""
    # A token-tree definition (Rust macro_rules!) has no body field; its
    # arms hang directly off the definition node, which the name identifier
    # cannot distinguish since identifiers collapse to one skeleton token.
    if func_node.type in cs.AST_FP_SELF_BODY_TYPES:
        return func_node
    # Every LanguageSpec keeps the default body field; Dart alone splits a
    # definition into a signature node and a sibling function_body.
    body = func_node.child_by_field_name(cs.FIELD_BODY)
    if body is not None:
        return body
    dart_body = dart_body_node(func_node)
    if dart_body is not None:
        return dart_body
    # A wrapper definition (C++ template_declaration) has no body field of
    # its own: the body lives on the inner function_definition. Recursing
    # also covers nested wrappers (member templates of class templates).
    for child in func_node.named_children:
        if cs.AST_FP_WRAPPED_DEF_SUBSTRING in child.type:
            inner = _resolve_body(child)
            if inner is not None:
                return inner
    # An expression-bodied definition (C# `=> expr` property/member) keeps
    # its logic in a plain named child rather than a body field.
    for child in func_node.named_children:
        if child.type in cs.AST_FP_EXPRESSION_BODY_TYPES:
            return child
    return None


def _token_for(node: Node) -> str | None:
    """The skeleton token a node contributes, or None to drop the node.

    A classified node is a skeleton *leaf*: literals are never descended into
    (their internals are renaming-adjacent noise), identifiers have no
    structure to keep.
    """
    node_type = node.type
    if cs.AST_FP_COMMENT_SUBSTRING in node_type:
        return None
    if not node.is_named:
        return None if node_type in cs.AST_FP_PUNCT_TYPES else node_type
    if cs.AST_FP_ID_TYPE_SUBSTRING in node_type or node_type in (
        cs.AST_FP_ID_TYPE_EXTRAS
    ):
        return cs.AST_FP_ID_TOKEN
    if node_type in cs.AST_FP_LIT_TYPE_EXTRAS or any(
        marker in node_type for marker in cs.AST_FP_LIT_TYPE_SUBSTRINGS
    ):
        return cs.AST_FP_LIT_TOKEN
    return node_type


def _is_leaf_token(token: str) -> bool:
    return token in (cs.AST_FP_ID_TOKEN, cs.AST_FP_LIT_TOKEN)


def _is_block_parent(node_type: str) -> bool:
    return node_type in cs.AST_FP_BLOCK_PARENT_EXTRAS or any(
        marker in node_type for marker in cs.AST_FP_BLOCK_PARENT_SUBSTRINGS
    )


def _fingerprint_subtree(body: Node) -> AstFingerprintResult | None:
    # Iterative two-phase walk (deep ASTs overflow Python recursion): a
    # pre-order pass records each kept node's token, children, and whether
    # its parent is block-ish; the reversed order is post-order, so digests
    # and node counts fold bottom-up in one array scan.
    tokens, child_indexes, is_branch_root = _collect_skeleton(body)
    if not tokens:
        return None
    return _fold_digests(tokens, child_indexes, is_branch_root)


def _collect_skeleton(
    body: Node,
) -> tuple[list[str], list[list[int]], list[bool]]:
    tokens: list[str] = []
    child_indexes: list[list[int]] = []
    is_branch_root: list[bool] = []
    stack: list[tuple[Node, int]] = [(body, -1)]
    while stack:
        node, parent_index = stack.pop()
        token = _token_for(node)
        if token is None:
            continue
        index = len(tokens)
        tokens.append(token)
        child_indexes.append([])
        is_branch_root.append(
            parent_index >= 0 and _is_block_parent(tokens[parent_index])
        )
        if parent_index >= 0:
            child_indexes[parent_index].append(index)
        if not _is_leaf_token(token):
            # Reversed push keeps children in source order after popping.
            for child in reversed(node.children):
                stack.append((child, index))
    return tokens, child_indexes, is_branch_root


def _fold_digests(
    tokens: list[str], child_indexes: list[list[int]], is_branch_root: list[bool]
) -> AstFingerprintResult:
    digests: list[bytes] = [b""] * len(tokens)
    counts: list[int] = [0] * len(tokens)
    branch_digests: set[bytes] = set()
    for index in range(len(tokens) - 1, -1, -1):
        hasher = hashlib.blake2b(digest_size=cs.AST_FP_DIGEST_SIZE)
        hasher.update(tokens[index].encode())
        hasher.update(cs.AST_FP_HASH_OPEN)
        count = 1
        for child_index in child_indexes[index]:
            hasher.update(digests[child_index])
            count += counts[child_index]
        hasher.update(cs.AST_FP_HASH_CLOSE)
        digests[index] = hasher.digest()
        counts[index] = count
        if is_branch_root[index] and count >= cs.AST_FP_MIN_BRANCH_NODES:
            branch_digests.add(digests[index])

    return AstFingerprintResult(
        fingerprint=digests[0].hex(),
        node_count=counts[0],
        branch_fingerprints=sorted(digest.hex() for digest in branch_digests),
    )
