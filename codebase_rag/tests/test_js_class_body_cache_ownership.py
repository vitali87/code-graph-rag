# The class-body lookup cache must never serve entries across trees. It was
# keyed by id(root), a recycled heap address: after a tree was freed, a new
# parse allocated at the same address inherited the dead tree's Node values
# on one xdist worker distribution (issue #1042). Ownership is now a strong
# reference compared by NODE EQUALITY — the held reference pins the owner's
# tree so no live tree can alias it, a fresh wrapper over the same tree
# compares equal and keeps the cache, and a root from another tree compares
# unequal and clears it.
from __future__ import annotations

import tree_sitter_javascript as tsj
from tree_sitter import Language, Parser

from codebase_rag.parsers.js_ts import utils as js_utils


def _parse(src: str):
    parser = Parser(Language(tsj.language()))
    return parser.parse(src.encode())


def test_owner_is_held_by_reference_not_recycled_id() -> None:
    tree_a = _parse("class Box { open () { return 1 } }")
    root_a = tree_a.root_node
    found = js_utils.find_method_in_ast(root_a, "Box", "open")
    assert found is not None

    # The cache must HOLD the owning root: that reference pins the tree, so
    # no live tree can alias its address — which is what makes the equality
    # comparison sound.
    assert js_utils._CLASS_BODY_CACHE_OWNER == root_a


def test_fresh_wrapper_over_the_same_tree_keeps_the_cache(tmp_path=None) -> None:
    # Each `tree.root_node` access mints a NEW wrapper object; a same-tree
    # lookup through a fresh wrapper must hit the cache, not reset it.
    tree = _parse("class Box { open () { return 1 } }")
    first = tree.root_node
    assert js_utils.find_method_in_ast(first, "Box", "open") is not None
    marker = js_utils._CLASS_BODY_CACHE.get("Box")
    fresh = tree.root_node
    assert fresh is not first
    assert js_utils.find_method_in_ast(fresh, "Box", "open") is not None
    assert js_utils._CLASS_BODY_CACHE.get("Box") is marker


def test_fresh_root_never_inherits_the_previous_trees_entries() -> None:
    tree_a = _parse("class Box { open () { return 1 } }")
    root_a = tree_a.root_node
    assert js_utils.find_method_in_ast(root_a, "Box", "open") is not None

    # Same class name, different tree and different member set: the entry
    # cached for tree A must not answer for tree B.
    tree_b = _parse("class Box { close () { return 2 } }")
    root_b = tree_b.root_node
    assert js_utils.find_method_in_ast(root_b, "Box", "open") is None
    found = js_utils.find_method_in_ast(root_b, "Box", "close")
    assert found is not None
    assert js_utils._CLASS_BODY_CACHE_OWNER is root_b


def test_stale_poisoned_entry_cannot_be_served() -> None:
    # Model the recycled-address hazard directly: an entry for the same class
    # name planted by ANOTHER tree sits in the cache dict. A root from a
    # different tree compares UNEQUAL to the held owner, so the cache clears
    # rather than reading through it.
    tree_a = _parse("class Box { open () { return 1 } }")
    root_a = tree_a.root_node
    wrong_body = js_utils.find_method_in_ast(root_a, "Box", "open")
    assert wrong_body is not None

    tree_b = _parse("class Box { close () { return 2 } }")
    root_b = tree_b.root_node
    assert js_utils.find_method_in_ast(root_b, "Box", "open") is None
