"""A qualified-name segment spelled like a trie node's own key.

Each trie node keeps its qn and label under `__qn__` and `__type__` beside
its segment children, so a method named `__type__` collides with its class's
label. Registering one used to fail an assert and stop the index. Any other
segment, a dunder like `__init__` included, is a segment like the rest.
"""

import pytest

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie


@pytest.mark.parametrize("segment", [cs.TRIE_TYPE_KEY, cs.TRIE_QN_KEY])
@pytest.mark.parametrize("class_first", [True, False])
def test_a_segment_named_like_a_node_key_registers(
    segment: str, class_first: bool
) -> None:
    trie = FunctionRegistryTrie()
    method_qn = f"pkg.mod.Foo.{segment}"
    inserts = [
        ("pkg.mod.Foo", cs.NodeLabel.CLASS),
        (method_qn, cs.NodeLabel.METHOD),
        ("pkg.mod.Foo.bar", cs.NodeLabel.METHOD),
    ]
    for qn, label in inserts if class_first else reversed(inserts):
        trie.insert(qn, label)

    assert trie.get(method_qn) == cs.NodeLabel.METHOD
    assert trie.find_ending_with(segment) == [method_qn]
    # The class keeps its own qn and label in the trie.
    assert sorted(trie.find_with_prefix("pkg.mod")) == [
        ("pkg.mod.Foo", cs.NodeLabel.CLASS),
        ("pkg.mod.Foo.bar", cs.NodeLabel.METHOD),
    ]
    assert trie.find_with_prefix(method_qn) == []


@pytest.mark.parametrize("segment", [cs.TRIE_TYPE_KEY, cs.TRIE_QN_KEY])
def test_a_segment_named_like_a_node_key_removes(segment: str) -> None:
    trie = FunctionRegistryTrie()
    method_qn = f"pkg.mod.Foo.{segment}"
    trie.insert("pkg.mod.Foo", cs.NodeLabel.CLASS)
    trie.insert(method_qn, cs.NodeLabel.METHOD)

    del trie[method_qn]

    assert method_qn not in trie
    assert trie.find_with_prefix("pkg.mod") == [("pkg.mod.Foo", cs.NodeLabel.CLASS)]


def test_a_dunder_segment_is_found_by_prefix() -> None:
    trie = FunctionRegistryTrie()
    for qn, label in [
        ("m.Bar", cs.NodeLabel.CLASS),
        ("m.Bar.__init__", cs.NodeLabel.METHOD),
        ("m.Bar.__eq__", cs.NodeLabel.METHOD),
        ("m.Bar.run", cs.NodeLabel.METHOD),
        ("m.__init__.helper", cs.NodeLabel.FUNCTION),
    ]:
        trie.insert(qn, label)

    assert sorted(trie.find_with_prefix("m.Bar")) == [
        ("m.Bar", cs.NodeLabel.CLASS),
        ("m.Bar.__eq__", cs.NodeLabel.METHOD),
        ("m.Bar.__init__", cs.NodeLabel.METHOD),
        ("m.Bar.run", cs.NodeLabel.METHOD),
    ]
    assert trie.find_with_prefix_and_suffix("m", "__init__") == ["m.Bar.__init__"]
    assert trie.find_with_prefix("m.__init__") == [
        ("m.__init__.helper", cs.NodeLabel.FUNCTION)
    ]


def test_removing_a_sibling_keeps_a_dunder_subtree() -> None:
    # `m.pkg` is no endpoint, so once `m.pkg.x` goes its only children are
    # under `__init__`; they are still children, so the node stays.
    trie = FunctionRegistryTrie()
    trie.insert("m.pkg.x", cs.NodeLabel.FUNCTION)
    trie.insert("m.pkg.__init__.f", cs.NodeLabel.FUNCTION)

    del trie["m.pkg.x"]

    assert trie.find_with_prefix("m") == [("m.pkg.__init__.f", cs.NodeLabel.FUNCTION)]
