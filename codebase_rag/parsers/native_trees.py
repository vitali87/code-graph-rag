"""The Rust extension's walks of the trees the indexer parsed.

The definitions pass hashes every definition it ingests twice over, once
for its anchor (`anchor_hash.py`) and once for its clone-detection skeleton
(`ast_fingerprint.py`), walking each subtree node by node through
py-tree-sitter: every `node.children`, `node.type` and `node.text` crosses
into C and builds a Python object. `TreeBridge` (rust/cgr_graph_rs/src/trees)
walks the same subtree in Rust. It is handed the Python `Node` itself and
reads the C node inside it, so nothing is parsed twice and no grammar is
compiled into the extension; its tree-sitter runtime is the very C code
compiled into the py-tree-sitter release cgr pins.

Each Rust walk returns exactly what its Python twin returns, which stays the
reference and runs whenever the bridge is off: the extension is missing or
predates it, py-tree-sitter is not the release it reads, or the probe below
reads differently through the two runtimes. A node the bridge cannot read
exactly (a test double, a tree whose source is not `bytes`) raises
`NotBorrowed` and that one call runs the Python twin.

The classification rules are Python's own constants, handed over when the
bridge is built, so the twins cannot drift apart on them.
"""

from collections.abc import Sequence
from functools import cache
from typing import Protocol

from loguru import logger
from tree_sitter import Language, Node, Parser

from .. import constants as cs
from .. import exceptions as ex
from .. import logs as ls
from ..embedded_graph.extension import rust_extension
from ..types_defs import DescribedNode, TreeRules

# Whether the indexer may use the bridge; the parity tests and benchmarks
# flip it to compare the two paths.
ENABLED: bool = True


class TreeBridge(Protocol):
    def anchor_hash(self, definition: Node, decorators: Sequence[str]) -> str: ...

    def ast_fingerprint(self, func_node: Node) -> tuple[str, int, list[str]] | None: ...

    def describe(self, node: Node) -> list[DescribedNode]: ...


NotBorrowed: type[Exception] = getattr(
    rust_extension(), cs.EG_RUST_NOT_BORROWED, ex.NoTreeBridge
)


def rules() -> TreeRules:
    """The constants the Python twins classify nodes by, under the bridge's names."""
    return TreeRules(
        anchor_version=cs.ANCHOR_HASH_VERSION,
        comment=cs.AST_FP_COMMENT_SUBSTRING,
        punct=sorted(cs.AST_FP_PUNCT_TYPES),
        id_token=cs.AST_FP_ID_TOKEN,
        lit_token=cs.AST_FP_LIT_TOKEN,
        id_substring=cs.AST_FP_ID_TYPE_SUBSTRING,
        id_extras=sorted(cs.AST_FP_ID_TYPE_EXTRAS),
        lit_substrings=list(cs.AST_FP_LIT_TYPE_SUBSTRINGS),
        lit_extras=sorted(cs.AST_FP_LIT_TYPE_EXTRAS),
        block_substrings=list(cs.AST_FP_BLOCK_PARENT_SUBSTRINGS),
        block_extras=sorted(cs.AST_FP_BLOCK_PARENT_EXTRAS),
        min_branch_nodes=cs.AST_FP_MIN_BRANCH_NODES,
        wrapped_def_substring=cs.AST_FP_WRAPPED_DEF_SUBSTRING,
        expression_body_types=sorted(cs.AST_FP_EXPRESSION_BODY_TYPES),
        self_body_types=sorted(cs.AST_FP_SELF_BODY_TYPES),
        hash_open=cs.AST_FP_HASH_OPEN,
        hash_close=cs.AST_FP_HASH_CLOSE,
        digest_size=cs.AST_FP_DIGEST_SIZE,
        body_field=cs.FIELD_BODY,
        dart_signature_types=sorted(cs.DART_SIGNATURE_TYPES),
        dart_signature_wrappers=sorted(cs.DART_SIGNATURE_WRAPPERS),
        dart_function_body=cs.TS_DART_FUNCTION_BODY,
    )


def tree_bridge() -> TreeBridge | None:
    """The bridge, or None when the Python twins must walk the trees."""
    return _bridge() if ENABLED else None


@cache
def _bridge() -> TreeBridge | None:
    if (bridge_type := getattr(rust_extension(), cs.EG_RUST_TREE_BRIDGE, None)) is None:
        return None
    try:
        bridge: TreeBridge = bridge_type(rules())
        disagreement = _disagreement(bridge)
    except Exception as error:
        logger.debug(ls.NATIVE_TREES_UNAVAILABLE, error=error)
        return None
    if disagreement is not None:
        check, node_type = disagreement
        logger.warning(ls.NATIVE_TREES_DISAGREE, check=check, node_type=node_type)
        return None
    return bridge


def describe(node: Node) -> list[DescribedNode]:
    """What `TreeBridge.describe` reports, as py-tree-sitter reads it."""
    out: list[DescribedNode] = []
    cursor = node.walk()
    while True:
        if (n := cursor.node) is None:
            return out
        out.append(
            (
                n.kind_id,
                n.type,
                n.is_named,
                n.start_byte,
                n.end_byte,
                tuple(n.start_point),
                tuple(n.end_point),
                n.child_count,
                cursor.field_name,
            )
        )
        if cursor.goto_first_child():
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                return out


def _disagreement(bridge: TreeBridge) -> tuple[cs.TreeBridgeCheck, str] | None:
    """What the bridge reads differently from Python on the probe, and at
    which node type, if anything."""
    # Imported here: both modules ask this one for the bridge.
    import tree_sitter_python

    from .anchor_hash import python_anchor_hash
    from .ast_fingerprint import python_ast_fingerprint

    probe = Parser(Language(tree_sitter_python.language())).parse(cs.TREE_BRIDGE_PROBE)
    root = probe.root_node
    if bridge.describe(root) != describe(root):
        return cs.TreeBridgeCheck.LAYOUT, root.type
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if bridge.anchor_hash(node, ()) != python_anchor_hash(node, ()):
            return cs.TreeBridgeCheck.ANCHOR_HASH, node.type
        native = bridge.ast_fingerprint(node)
        python = python_ast_fingerprint(node)
        if (None if native is None else tuple(native)) != (
            None if python is None else tuple(python)
        ):
            return cs.TreeBridgeCheck.FINGERPRINT, node.type
    return None
