# Trie-backed registry of every defined function/method qualified name, with
# the auxiliary indices resolution needs: simple-name lookup, ending-with
# cache, duplicate-QN variants, property/object-member/abstract markers,
# callable params.

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

# The kinds that live only in TypeScript's type declaration space. A class or
# an enum declares a type too, but it is also a value, so it is a value here.
TYPE_SPACE_KINDS = frozenset({NodeType.TYPE, NodeType.INTERFACE})


class FunctionRegistryTrie:
    __slots__ = (
        "root",
        "_entries",
        "_simple_name_lookup",
        "_ending_with_cache",
        "_ending_with_tails",
        "_duplicates",
        "_variant_columns",
        "_type_twins",
        "_properties",
        "_property_names",
        "_object_members",
        "_abstracts",
        "_callable_params",
    )

    def __init__(self, simple_name_lookup: SimpleNameLookup | None = None) -> None:
        self.root: TrieNode = {}
        self._entries: FunctionRegistry = {}
        self._simple_name_lookup = simple_name_lookup
        self._ending_with_cache: dict[str, list[QualifiedName]] = {}
        # Dotted cache keys grouped by their last segment, so an insert or
        # delete invalidates only the keys its simple name can end with
        # instead of scanning the whole cache (issue #1524: a scoped
        # re-ingest of a hub file re-registers thousands of definitions).
        self._ending_with_tails: dict[str, set[str]] = {}
        self._duplicates: dict[QualifiedName, list[QualifiedName]] = {}
        self._variant_columns: dict[QualifiedName, int] = {}
        # The type-space declaration sharing its name with the value that
        # holds `_entries` (issue #2520). Kept out of `_duplicates`, so no
        # call fans out onto it, and out of `_entries`, so a call binds the
        # value; only a type position asks for it, through `type_kind`.
        self._type_twins: dict[QualifiedName, NodeType] = {}
        self._properties: set[QualifiedName] = set()
        self._property_names: set[str] = set()
        self._object_members: set[QualifiedName] = set()
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

    def mark_object_member(self, qualified_name: QualifiedName) -> None:
        self._object_members.add(qualified_name)

    def is_object_member(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._object_members

    def mark_abstract(self, qualified_name: QualifiedName) -> None:
        self._abstracts.add(qualified_name)

    def is_abstract(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._abstracts

    def register_unique_qn(
        self,
        natural_qn: QualifiedName,
        start_line: int,
        start_col: int = 0,
        kind: NodeType | None = None,
    ) -> QualifiedName:
        """A name for this definition that no other definition holds.

        The line alone named two same-line twins identically, so they became
        one node and one of them left the graph (issue #1071). The column
        joins only for a definition at a DIFFERENT column on a line already
        claimed, which keeps every variant that was already unique spelled the
        way it always was, and keeps the call idempotent: two passes
        registering one definition must agree on its name, not mint a second.

        `kind` is passed only by languages with separate type and value
        declaration spaces (TypeScript): there a definition from the other
        space keeps the natural name under its own label (see
        `claim_other_space`). Without it every collision is a duplicate.
        """
        if natural_qn not in self._entries:
            return natural_qn
        if kind is not None and self.claim_other_space(natural_qn, kind):
            return natural_qn
        variant = f"{natural_qn}{cs.DUP_QN_MARKER}{start_line}"
        claimed_col = self._variant_columns.setdefault(variant, start_col)
        if claimed_col != start_col:
            variant = f"{variant}{cs.DUP_QN_COLUMN_MARKER}{start_col}"
        bucket = self._duplicates.setdefault(natural_qn, [natural_qn])
        if variant not in bucket:
            bucket.append(variant)
        return variant

    def claim_other_space(self, natural_qn: QualifiedName, kind: NodeType) -> bool:
        """Let `kind` share `natural_qn` if its holder is in the other space.

        TypeScript lets `type input<T>` and `function input` coexist, and Zod
        v4 relies on it. They are two entities, not two definitions of one:
        suffixing the second `@line` made every call fan out as `overload`
        over a Type, and the fan-outs that do not check kinds sent CALLS rows
        at it (issue #2520). The graph's uniqueness is per label, so both keep
        the natural name. The value keeps the entry, since calls and
        instantiations resolve through it; the type is held beside it. A
        value arriving second takes the entry over, which is what lets
        `class Box` keep its name after an earlier `interface Box`.

        False, changing nothing, for a free name, a name both spaces already
        hold, or a holder in the same space (a genuine duplicate).
        """
        held = self._entries.get(natural_qn)
        if held is None or natural_qn in self._type_twins:
            return False
        incoming_is_type = kind in TYPE_SPACE_KINDS
        if incoming_is_type == (held in TYPE_SPACE_KINDS):
            return False
        self._type_twins[natural_qn] = kind if incoming_is_type else held
        return True

    def type_kind(self, qualified_name: QualifiedName) -> NodeType | None:
        """The kind a TYPE position binds `qualified_name` to.

        The type-space twin when a value shares the name (issue #2520),
        otherwise the registered kind, as `get` gives it.
        """
        twin = self._type_twins.get(qualified_name)
        return twin if twin is not None else self._entries.get(qualified_name)

    def variants(self, qualified_name: QualifiedName) -> list[QualifiedName]:
        return self._duplicates.get(qualified_name, [qualified_name])

    def insert(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        qualified_name = sys.intern(qualified_name)
        held = self._entries.get(qualified_name)
        if (
            held is not None
            and held != func_type
            and self._type_twins.get(qualified_name) == func_type
        ):
            # The type-space twin registering under the value's name: the
            # value keeps the entry, and the trie already holds the name.
            return
        self._entries[qualified_name] = func_type

        simple_name = qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        if self._simple_name_lookup is not None:
            self._simple_name_lookup[simple_name].add(qualified_name)
        self._invalidate_ending_with_cache(simple_name)

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
        self._type_twins.pop(qualified_name, None)
        # The line this variant claimed is free again, so the next definition
        # written there takes the plain `@line` rather than inheriting a column
        # from something the graph no longer holds. Without this the name a
        # definition gets depends on what was indexed before it.
        self._variant_columns.pop(qualified_name, None)
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
        self._object_members.discard(qualified_name)
        self._abstracts.discard(qualified_name)
        self._callable_params.pop(qualified_name, None)

        self._invalidate_ending_with_cache(simple_name)

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

    def _invalidate_ending_with_cache(self, simple_name: str) -> None:
        if not self._ending_with_cache:
            return
        self._ending_with_cache.pop(simple_name, None)
        # dotted suffixes are cached too (#513); a qn can only end with a
        # dotted key whose last segment is its own simple name, so only that
        # bucket is consulted. Dropping the whole bucket over-invalidates
        # (keys the qn does not end with are recomputed on the next lookup),
        # which is cheap and never wrong.
        for key in self._ending_with_tails.pop(simple_name, ()):
            self._ending_with_cache.pop(key, None)

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
        if cs.SEPARATOR_DOT in suffix:
            tail = suffix.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            self._ending_with_tails.setdefault(tail, set()).add(suffix)
        return result

    def find_with_prefix(self, prefix: str) -> list[tuple[QualifiedName, NodeType]]:
        node = self._navigate_to_prefix(prefix)
        return [] if node is None else self._collect_from_subtree(node)
