# Trie-backed registry of every defined function/method qualified name, with
# the auxiliary indices resolution needs: simple-name lookup, ending-with
# cache, duplicate-QN variants, C++ member-overload signatures,
# property/object-member/abstract markers, callable params.

import sys
from collections.abc import Callable, ItemsView, KeysView

from . import constants as cs
from .types_defs import (
    FunctionRegistry,
    NodeType,
    OverloadSignature,
    QualifiedName,
    SimpleNameLookup,
    TrieNode,
)
from .utils import qn_markers
from .utils.cpp_signatures import overload_signature_from_text, pick_overload


class FunctionRegistryTrie:
    __slots__ = (
        "root",
        "_entries",
        "_simple_name_lookup",
        "_ending_with_cache",
        "_ending_with_tails",
        "_duplicates",
        "_variant_columns",
        "_overloads",
        "_declared_overloads",
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
        self._overloads: dict[QualifiedName, OverloadSignature] = {}
        self._declared_overloads: set[QualifiedName] = set()
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
        self, natural_qn: QualifiedName, start_line: int, start_col: int = 0
    ) -> QualifiedName:
        """A name for this definition that no other definition holds.

        The line alone named two same-line twins identically, so they became
        one node and one of them left the graph (issue #1071). The column
        joins only for a definition at a DIFFERENT column on a line already
        claimed, which keeps every variant that was already unique spelled the
        way it always was, and keeps the call idempotent: two passes
        registering one definition must agree on its name, not mint a second.
        """
        if natural_qn not in self._entries:
            return natural_qn
        variant = f"{natural_qn}{cs.DUP_QN_MARKER}{start_line}"
        claimed_col = self._variant_columns.setdefault(variant, start_col)
        if claimed_col != start_col:
            variant = f"{variant}{cs.DUP_QN_COLUMN_MARKER}{start_col}"
        bucket = self._duplicates.setdefault(natural_qn, [natural_qn])
        if variant not in bucket:
            bucket.append(variant)
        return variant

    def variants(self, qualified_name: QualifiedName) -> list[QualifiedName]:
        return self._duplicates.get(qualified_name, [qualified_name])

    def register_overload_qn(
        self,
        natural_qn: QualifiedName,
        signature: OverloadSignature,
        start_line: int,
        start_col: int = 0,
        declared_in_class: bool = False,
    ) -> QualifiedName:
        """The name of one C++ member overload (issue #2455).

        A C++ member is written twice, declared in its class and defined out
        of it, often in another file, and both must land on one node; but
        `register_unique_qn` would give the second sighting an `@line` of its
        own, which is why C++ members skipped it and every overload merged
        into one node. Keying on the signature keeps both properties: the
        first overload keeps the plain name, as a member that is not
        overloaded always did, and each other overload gets `@<line>` of the
        place it is first seen, which is its in-class declaration whenever
        the class body was parsed.
        """
        held = [qn for qn in self.variants(natural_qn) if qn in self._entries]
        for qn in held:
            if (known := self._overloads.get(qn)) and known.text == signature.text:
                return self._record_overload(qn, signature, declared_in_class)
        # A holder with no signature on record (a member whose parameter list
        # could not be read, or a graph written before overloads were told
        # apart) stands for every overload, as it did. It stays unrecorded so
        # the next overload lands on it too.
        if unrecorded := [qn for qn in held if qn not in self._overloads]:
            return unrecorded[0]
        qualified_name = self._matching_declaration(
            held, signature, declared_in_class
        ) or self.register_unique_qn(natural_qn, start_line, start_col)
        return self._record_overload(qualified_name, signature, declared_in_class)

    def _record_overload(
        self,
        qualified_name: QualifiedName,
        signature: OverloadSignature,
        declared_in_class: bool,
    ) -> QualifiedName:
        self._overloads.setdefault(qualified_name, signature)
        if declared_in_class:
            self._declared_overloads.add(qualified_name)
        return qualified_name

    def _matching_declaration(
        self,
        held: list[QualifiedName],
        signature: OverloadSignature,
        declared_in_class: bool,
    ) -> QualifiedName | None:
        # Two members written in one class body are two overloads.
        if declared_in_class:
            return None
        # An out-of-class definition must define a member its class declares,
        # so one that matches no declaration verbatim is a respelling
        # (`string` for `std::string` under a using-directive, `int` for a
        # `Count` typedef) of one of them, when the spelling settles which.
        return pick_overload(
            signature,
            [
                (qn, self._overloads[qn])
                for qn in held
                if qn in self._declared_overloads
            ],
        )

    def overload_signature(
        self, qualified_name: QualifiedName
    ) -> OverloadSignature | None:
        return self._overloads.get(qualified_name)

    def restore_overload(
        self,
        qualified_name: QualifiedName,
        signature_text: str,
        declared_in_class: bool,
    ) -> None:
        """Re-record an overload read back from the graph on an incremental run.

        Without it, a definition re-parsed beside an unchanged header could
        not tell which of the class's overloads it defines, and a call from
        the re-parsed file would see only the plain-named one.
        """
        # The arity is read off the text: without it two read-back
        # declarations of different arity could not be told apart for a
        # respelled definition, and it would mint a node a clean index has not.
        self._overloads.setdefault(
            qualified_name, overload_signature_from_text(signature_text)
        )
        # Only a member its class body declared is a declaration a definition
        # may pair with by respelling. One known only from a definition is
        # just that definition, and treating it as declared would hand a
        # different overload defined beside it the same node.
        if declared_in_class:
            self._declared_overloads.add(qualified_name)
        natural = qn_markers.natural_qn(qualified_name)
        if natural != qualified_name:
            bucket = self._duplicates.setdefault(natural, [natural])
            if qualified_name not in bucket:
                bucket.append(qualified_name)

    def insert(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        qualified_name = sys.intern(qualified_name)
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
        # The line this variant claimed is free again, so the next definition
        # written there takes the plain `@line` rather than inheriting a column
        # from something the graph no longer holds. Without this the name a
        # definition gets depends on what was indexed before it.
        self._variant_columns.pop(qualified_name, None)
        self._overloads.pop(qualified_name, None)
        self._declared_overloads.discard(qualified_name)
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
