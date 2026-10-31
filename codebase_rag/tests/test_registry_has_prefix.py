"""Whether anything is registered under a qn is answered without listing it.

`_import_target_is_a_namespace` asks one yes/no question per call through an
imported module (`models.CharField(...)`), and answered it by materialising
every definition under the module and taking `bool()` of the list: call sites
times module size, 28% of django's Pass 3 (issue #2919).
"""

from __future__ import annotations

import pytest

from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.parsers.call_resolver import CallResolver
from codebase_rag.types_defs import NodeType


def _registry() -> FunctionRegistryTrie:
    trie = FunctionRegistryTrie()
    for i in range(50):
        trie[f"proj.db.models.Field{i}"] = NodeType.CLASS
        trie[f"proj.db.models.Field{i}.clean"] = NodeType.METHOD
    trie["proj.db.models.helper"] = NodeType.FUNCTION
    trie["proj.util.run"] = NodeType.FUNCTION
    return trie


def _is_namespace(trie: FunctionRegistryTrie, target: str) -> bool:
    # The check reads only `function_registry` off the resolver.
    resolver = CallResolver.__new__(CallResolver)
    resolver.function_registry = trie
    return resolver._import_target_is_a_namespace(target)


@pytest.fixture
def no_subtree_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: str, **_kwargs: str) -> list[tuple[str, NodeType]]:
        raise AssertionError("listed a whole subtree to answer a yes/no question")

    monkeypatch.setattr(FunctionRegistryTrie, "_collect_from_subtree", refuse)


def test_a_module_is_a_namespace_without_listing_its_members(
    no_subtree_listing: None,
) -> None:
    assert _is_namespace(_registry(), "proj.db.models") is True


def test_an_unknown_name_is_not_a_namespace_without_listing(
    no_subtree_listing: None,
) -> None:
    assert _is_namespace(_registry(), "proj.db.nothing") is False


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        # A registered class is something you construct through.
        ("proj.db.models.Field3", True),
        # A registered function is a value, whatever is under it.
        ("proj.db.models.helper", False),
        ("proj.util.run", False),
        # Unregistered with members: a module.
        ("proj.db", True),
        # Unregistered and empty: an imported instance.
        ("proj.db.models.instance", False),
    ],
)
def test_the_answer_is_unchanged(target: str, expected: bool) -> None:
    # Negative: the same verdicts the subtree listing gave.
    assert _is_namespace(_registry(), target) is expected


def test_has_prefix_matches_the_listing_it_replaces() -> None:
    trie = _registry()
    for prefix in ("proj", "proj.db.models", "proj.db.models.Field7", "proj.nope", ""):
        assert trie.has_prefix(prefix) is bool(trie.find_with_prefix(prefix)), prefix


def test_has_prefix_stops_at_the_first_entry_of_a_wide_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trie = FunctionRegistryTrie()
    for i in range(500):
        trie[f"proj.wide.f{i}"] = NodeType.FUNCTION
    visited: list[object] = []
    descend = FunctionRegistryTrie._subtree_has_entry

    def counting(self: FunctionRegistryTrie, node: dict) -> bool:
        visited.append(node)
        return descend(self, node)

    monkeypatch.setattr(FunctionRegistryTrie, "_subtree_has_entry", counting)
    assert trie.has_prefix("proj.wide")
    # The module node and its first child, not all 500 children.
    assert len(visited) == 2


def test_has_prefix_matches_the_listing_under_an_internal_looking_segment() -> None:
    # Keys starting with the trie's internal prefix are skipped by the
    # listing, so a subtree holding only such a name lists nothing; the
    # yes/no answer must not claim otherwise from the bare child above it.
    trie = FunctionRegistryTrie()
    trie["proj.pkg.__init__"] = NodeType.FUNCTION
    for prefix in ("proj", "proj.pkg"):
        assert trie.has_prefix(prefix) is bool(trie.find_with_prefix(prefix)), prefix


def test_has_prefix_forgets_a_deleted_subtree() -> None:
    trie = FunctionRegistryTrie()
    trie["proj.lone.mod.f"] = NodeType.FUNCTION
    assert trie.has_prefix("proj.lone.mod")
    del trie["proj.lone.mod.f"]
    assert not trie.has_prefix("proj.lone.mod")
    assert not trie.has_prefix("proj.lone")


def test_the_registry_protocol_declares_it() -> None:
    from codebase_rag.types_defs import FunctionRegistryTrieProtocol

    assert hasattr(FunctionRegistryTrieProtocol, "has_prefix")
