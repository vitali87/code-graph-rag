# Trie-backed registry of every defined function/method qualified name, with
# the auxiliary indices resolution needs: simple-name lookup, ending-with
# cache, duplicate-QN variants, property/abstract markers, callable params.

import sys
from collections.abc import Callable, ItemsView, KeysView

from . import constants as cs
from .types_defs import (
    FunctionRegistry,
    NodeType,
    QualifiedName,
    SimpleNameLookup,
    TrieNode,
)


class FunctionRegistryTrie:
    __slots__ = (
        "root",
        "_entries",
        "_simple_name_lookup",
        "_ending_with_cache",
        "_duplicates",
        "_properties",
        "_property_names",
        "_abstracts",
        "_callable_params",
    )

    def __init__(self, simple_name_lookup: SimpleNameLookup | None = None) -> None:
        self.root: TrieNode = {}
        self._entries: FunctionRegistry = {}
        self._simple_name_lookup = simple_name_lookup
        self._ending_with_cache: dict[str, list[QualifiedName]] = {}
        self._duplicates: dict[QualifiedName, list[QualifiedName]] = {}
        self._properties: set[QualifiedName] = set()
        self._property_names: set[str] = set()
        self._abstracts: set[QualifiedName] = set()
        self._callable_params: dict[QualifiedName, dict[str, int]] = {}

    def mark_callable_params(
        self, qualified_name: QualifiedName, params: dict[str, int]
    ) -> None:
        if params:
            self._callable_params[qualified_name] = params

    def callable_params(self, qualified_name: QualifiedName) -> dict[str, int] | None:
        return self._callable_params.get(qualified_name)

    def mark_property(self, qualified_name: QualifiedName) -> None:
        self._properties.add(qualified_name)
        self._property_names.add(qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1])

    def is_property(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._properties

    def property_names(self) -> set[str]:
        return self._property_names

    def mark_abstract(self, qualified_name: QualifiedName) -> None:
        self._abstracts.add(qualified_name)

    def is_abstract(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._abstracts

    def register_unique_qn(
        self, natural_qn: QualifiedName, start_line: int
    ) -> QualifiedName:
        if natural_qn not in self._entries:
            return natural_qn
        variant = f"{natural_qn}{cs.DUP_QN_MARKER}{start_line}"
        bucket = self._duplicates.setdefault(natural_qn, [natural_qn])
        if variant not in bucket:
            bucket.append(variant)
        return variant

    def variants(self, qualified_name: QualifiedName) -> list[QualifiedName]:
        return self._duplicates.get(qualified_name, [qualified_name])

    def insert(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        qualified_name = sys.intern(qualified_name)
        self._entries[qualified_name] = func_type

        simple_name = qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        if self._simple_name_lookup is not None:
            self._simple_name_lookup[simple_name].add(qualified_name)
        self._invalidate_ending_with_cache(qualified_name, simple_name)

        parts = qualified_name.split(cs.SEPARATOR_DOT)
        current: TrieNode = self.root

        for part in parts:
            if part not in current:
                current[part] = {}
            child = current[part]
            assert isinstance(child, dict)
            current = child

        current[cs.TRIE_TYPE_KEY] = func_type
        current[cs.TRIE_QN_KEY] = qualified_name

    def get(
        self, qualified_name: QualifiedName, default: NodeType | None = None
    ) -> NodeType | None:
        return self._entries.get(qualified_name, default)

    def __contains__(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._entries

    def __getitem__(self, qualified_name: QualifiedName) -> NodeType:
        return self._entries[qualified_name]

    def __setitem__(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        self.insert(qualified_name, func_type)

    def __delitem__(self, qualified_name: QualifiedName) -> None:
        if qualified_name not in self._entries:
            return

        del self._entries[qualified_name]
        self._duplicates.pop(qualified_name, None)
        for natural, bucket in list(self._duplicates.items()):
            if qualified_name in bucket:
                bucket.remove(qualified_name)
                if len(bucket) <= 1:
                    self._duplicates.pop(natural, None)
        simple_name = qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]

        if qualified_name in self._properties:
            self._properties.discard(qualified_name)
            if not any(
                p.rsplit(cs.SEPARATOR_DOT, 1)[-1] == simple_name
                for p in self._properties
            ):
                self._property_names.discard(simple_name)
        self._abstracts.discard(qualified_name)
        self._callable_params.pop(qualified_name, None)

        self._invalidate_ending_with_cache(qualified_name, simple_name)

        if self._simple_name_lookup is not None:
            if simple_name in self._simple_name_lookup:
                self._simple_name_lookup[simple_name].discard(qualified_name)

        parts = qualified_name.split(cs.SEPARATOR_DOT)
        self._cleanup_trie_path(parts, self.root)

    def _cleanup_trie_path(self, parts: list[str], node: TrieNode) -> bool:
        if not parts:
            node.pop(cs.TRIE_QN_KEY, None)
            node.pop(cs.TRIE_TYPE_KEY, None)
            return not node

        part = parts[0]
        if part not in node:
            return False

        child = node[part]
        assert isinstance(child, dict)
        if self._cleanup_trie_path(parts[1:], child):
            del node[part]

        is_endpoint = cs.TRIE_QN_KEY in node
        has_children = any(not key.startswith(cs.TRIE_INTERNAL_PREFIX) for key in node)
        return not has_children and not is_endpoint

    def _navigate_to_prefix(self, prefix: str) -> TrieNode | None:
        parts = prefix.split(cs.SEPARATOR_DOT) if prefix else []
        current: TrieNode = self.root
        for part in parts:
            if part not in current:
                return None
            child = current[part]
            assert isinstance(child, dict)
            current = child
        return current

    def _collect_from_subtree(
        self,
        node: TrieNode,
        filter_fn: Callable[[QualifiedName], bool] | None = None,
    ) -> list[tuple[QualifiedName, NodeType]]:
        results: list[tuple[QualifiedName, NodeType]] = []

        def dfs(n: TrieNode) -> None:
            if cs.TRIE_QN_KEY in n:
                qn = n[cs.TRIE_QN_KEY]
                func_type = n[cs.TRIE_TYPE_KEY]
                assert isinstance(qn, str) and isinstance(func_type, NodeType)
                if filter_fn is None or filter_fn(qn):
                    results.append((qn, func_type))

            for key, child in n.items():
                if not key.startswith(cs.TRIE_INTERNAL_PREFIX):
                    assert isinstance(child, dict)
                    dfs(child)

        dfs(node)
        return results

    def keys(self) -> KeysView[QualifiedName]:
        return self._entries.keys()

    def items(self) -> ItemsView[QualifiedName, NodeType]:
        return self._entries.items()

    def __len__(self) -> int:
        return len(self._entries)

    def find_with_prefix_and_suffix(
        self, prefix: str, suffix: str
    ) -> list[QualifiedName]:
        node = self._navigate_to_prefix(prefix)
        if node is None:
            return []
        suffix_pattern = f".{suffix}"
        matches = self._collect_from_subtree(
            node, lambda qn: qn.endswith(suffix_pattern)
        )
        return [qn for qn, _ in matches]

    def _invalidate_ending_with_cache(
        self, qualified_name: QualifiedName, simple_name: str
    ) -> None:
        if not self._ending_with_cache:
            return
        self._ending_with_cache.pop(simple_name, None)
        # dotted suffixes are cached too (#513); drop any the qn ends with.
        for key in [
            k
            for k in self._ending_with_cache
            if cs.SEPARATOR_DOT in k and qualified_name.endswith(f".{k}")
        ]:
            del self._ending_with_cache[key]

    def find_ending_with(self, suffix: str) -> list[QualifiedName]:
        cached = self._ending_with_cache.get(suffix)
        if cached is not None:
            return cached
        if self._simple_name_lookup is not None:
            if suffix in self._simple_name_lookup:
                result = sorted(self._simple_name_lookup[suffix])
            elif cs.SEPARATOR_DOT in suffix:
                # #513: the index only holds last segments, so a dotted
                # suffix ("Class.method") always misses it; fall back to
                # the linear scan instead of dropping the match.
                result = sorted(
                    qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")
                )
            else:
                # dot-free miss is authoritative: insert() indexes every
                # entry's last segment, so nothing can end with ".suffix".
                result = []
        else:
            result = sorted(
                qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")
            )
        self._ending_with_cache[suffix] = result
        return result

    def find_with_prefix(self, prefix: str) -> list[tuple[QualifiedName, NodeType]]:
        node = self._navigate_to_prefix(prefix)
        return [] if node is None else self._collect_from_subtree(node)
