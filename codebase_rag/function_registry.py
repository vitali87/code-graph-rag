# Trie-backed registry of every defined function/method qualified name, with
# the auxiliary indices resolution needs: simple-name lookup, ending-with
# cache, duplicate-QN variants, C++ member-overload signatures,
# property/object-member/abstract markers, callable params.

import sys
from collections.abc import Callable, ItemsView, Iterable, KeysView

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

# A column no source line reaches: a same-position duplicate in another file
# takes the next multiple of it past its own column (`register_unique_qn`).
_ORIGIN_SLOT = 10000


def _variant_position(qualified_name: QualifiedName) -> tuple[int, int]:
    # Every bucket entry past the head carries a marker; the fallback only
    # keeps the sort key total.
    return qn_markers.marker_position(qualified_name) or (0, -1)


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
        "_variant_origins",
        "_overloads",
        "_declared_overloads",
        "_type_twins",
        "_properties",
        "_property_names",
        "_member_aliases",
        "_aliases_by_target",
        "_alias_holders",
        "_non_method_members",
        "_non_method_holders",
        "_object_members",
        "_abstracts",
        "_module_private",
        "_body_scoped_names",
        "_callable_params",
        "_reserved",
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
        # The file of each variant registered with one (`origin`).
        self._variant_origins: dict[QualifiedName, str] = {}
        self._overloads: dict[QualifiedName, OverloadSignature] = {}
        self._declared_overloads: set[QualifiedName] = set()
        # The type-space declaration sharing its name with the value that
        # holds `_entries` (issue #2520). Kept out of `_duplicates`, so no
        # call fans out onto it, and out of `_entries`, so a call binds the
        # value; only a type position asks for it, through `type_kind`.
        self._type_twins: dict[QualifiedName, NodeType] = {}
        self._properties: set[QualifiedName] = set()
        self._property_names: set[str] = set()
        # `Class.run` -> the methods a class body bound `run` to (issue
        # #2620); the alias is no node, so it never enters the trie.
        self._member_aliases: dict[QualifiedName, list[QualifiedName]] = {}
        self._aliases_by_target: dict[QualifiedName, set[QualifiedName]] = {}
        # Member name -> the classes whose body aliases it, so only a lookup
        # of an aliased name pays for the exact (C3) MRO walk.
        self._alias_holders: dict[str, set[QualifiedName]] = {}
        # Class -> the member names its body binds to something other than a
        # method (`run = None`); attribute lookup stops there instead of
        # reaching a base's `run`.
        self._non_method_members: dict[QualifiedName, frozenset[str]] = {}
        # Member name -> the classes binding it that way, so a lookup only
        # pays for an exact MRO walk when some class hides the name.
        self._non_method_holders: dict[str, set[QualifiedName]] = {}
        self._object_members: set[QualifiedName] = set()
        self._abstracts: set[QualifiedName] = set()
        # JS/TS declarations their module does not export. `export * from`
        # passes on only exported names, so a private one in a star source is
        # not a candidate for the barrel's member (follow_reexports).
        self._module_private: set[QualifiedName] = set()
        self._body_scoped_names: set[QualifiedName] = set()
        self._callable_params: dict[QualifiedName, dict[str, int]] = {}
        self._reserved: dict[QualifiedName, tuple[int, int]] = {}

    def mark_callable_params(
        self, qualified_name: QualifiedName, params: dict[str, int]
    ) -> None:
        if params:
            self._callable_params[qualified_name] = params

    def callable_params(self, qualified_name: QualifiedName) -> dict[str, int] | None:
        return self._callable_params.get(qualified_name)

    def reserve_qns(self, reservations: dict[QualifiedName, tuple[int, int]]) -> None:
        """Keep each plain name for the definition at its (line, col).

        Passes register one kind at a time, not in document order: Python's
        functions go before its classes, so a `def Tool` shim below `class
        Tool` took the plain name and the class, written first, took `@line`
        (issue #2621). A reserved name goes to that position when it
        registers; a definition above it still takes the plain name first,
        and one below takes the `@line` variant. Each call replaces the last
        file's reservations, which only ever name that file's definitions.
        """
        self._reserved = reservations

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

    def mark_module_private(self, qualified_name: QualifiedName) -> None:
        self._module_private.add(qualified_name)

    def is_module_private(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._module_private

    def mark_body_scoped_name(self, qualified_name: QualifiedName) -> None:
        self._body_scoped_names.add(qualified_name)

    def is_body_scoped_name(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._body_scoped_names

    def register_unique_qn(
        self,
        natural_qn: QualifiedName,
        start_line: int,
        start_col: int = 0,
        kind: NodeType | None = None,
        origin: str | None = None,
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

        `origin`, the declaring file, is passed where one natural name spans
        files (a Go method `t_linux.go` and `t_windows.go` each declare at
        line 3): there the same line and column in another file is another
        definition, and takes the next free column slot past the real ones.
        """
        # A definition written above this one but registered by a later pass
        # is still owed the plain name (issue #2621, see `reserve_qns`).
        held = self._reserved.get(natural_qn, (start_line, start_col))
        if natural_qn not in self._entries and held >= (start_line, start_col):
            return natural_qn
        if kind is not None and self.claim_other_space(natural_qn, kind):
            return natural_qn
        variant = f"{natural_qn}{cs.DUP_QN_MARKER}{start_line}"
        claimed_col = self._variant_columns.setdefault(variant, start_col)
        if claimed_col != start_col:
            variant = f"{variant}{cs.DUP_QN_COLUMN_MARKER}{start_col}"
        if origin is not None:
            variant = self._variant_from(
                variant, natural_qn, start_line, start_col, origin
            )
        bucket = self._duplicates.setdefault(natural_qn, [natural_qn])
        if variant not in bucket:
            bucket.append(variant)
        return variant

    def _variant_from(
        self,
        variant: QualifiedName,
        natural_qn: QualifiedName,
        start_line: int,
        start_col: int,
        origin: str,
    ) -> QualifiedName:
        # `variant`, unless another file holds it: then the first column slot
        # past any real column (`@3_10000`) that this file holds or none does.
        slot = 0
        while self._variant_origins.setdefault(variant, origin) != origin:
            slot += 1
            variant = (
                f"{natural_qn}{cs.DUP_QN_MARKER}{start_line}"
                f"{cs.DUP_QN_COLUMN_MARKER}{start_col + _ORIGIN_SLOT * slot}"
            )
        return variant

    def restore_variant(self, qualified_name: QualifiedName) -> None:
        """Rejoin a `@line` variant read back from the graph to its natural qn.

        Parsing records the bucket as `register_unique_qn` mints the name, but
        an incremental run reads the definitions of files it did not re-parse
        back from the graph instead. Without the bucket `variants` answered
        the natural qn alone there, so a re-parsed caller bound one target
        where a clean index fans out to every same-named definition (issue
        #2403). A name without a trailing marker is not a variant.
        """
        natural = qn_markers.natural_qn(qualified_name)
        if natural == qualified_name:
            return
        bucket = self._duplicates.setdefault(natural, [natural])
        if qualified_name in bucket:
            return
        bucket.append(qualified_name)
        # Graph rows arrive in no fixed order. Parsing mints variants in
        # document order and readers take the head as the natural qn, so keep
        # it first and the rest by position: the list then reads the same
        # whichever files this run happened to re-parse.
        bucket[1:] = sorted(bucket[1:], key=_variant_position)

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
        # The `@line` overload rejoins its natural qn as any read-back variant
        # does (#2403): once, and in line order whatever order rows arrive in.
        self.restore_variant(qualified_name)

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

    def index_name(self, qualified_name: QualifiedName, name: str) -> None:
        """Make `qualified_name` findable by `name` too.

        `insert` indexes a qn's last segment, which for a signature-suffixed
        qn (`ILog.Info(string)`) or a duplicate's variant (`sep@7`) is not
        the definition's name. Parsing indexes the name as well; a definition
        read back from the graph comes through here (issues #3261, #2403). An
        empty name indexes nothing.
        """
        if not name:
            return
        if self._simple_name_lookup is not None:
            self._simple_name_lookup[name].add(qualified_name)
        self._invalidate_ending_with_cache(name)

    def invalidate_name(self, name: str) -> None:
        """Drop a `find_ending_with` answer cached for `name`.

        For a caller that removes qns from the simple-name index itself:
        `__delitem__` invalidates only a qn's last segment, never the name
        `index_name` or a parser added it under (`Info` for
        `ILog.Info(string)`).
        """
        self._invalidate_ending_with_cache(name)

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
        self._variant_origins.pop(qualified_name, None)
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
        self._module_private.discard(qualified_name)
        self._body_scoped_names.discard(qualified_name)
        self._callable_params.pop(qualified_name, None)

        self._invalidate_ending_with_cache(simple_name)
        self._drop_member_alias_target(qualified_name)
        self.set_non_method_members(qualified_name, ())

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

    def has_prefix(self, prefix: str) -> bool:
        """Whether anything is registered at or under `prefix`.

        `bool(find_with_prefix(...))` without the listing: it stops at the
        first entry, so asking about a 2,000-definition module costs the
        walk to its first one, not a copy of all of them (issue #2919).
        """
        node = self._navigate_to_prefix(prefix)
        return node is not None and self._subtree_has_entry(node)

    def _subtree_has_entry(self, node: TrieNode) -> bool:
        # Descends into a child before looking at its siblings: a module's
        # first child leads to an entry, so a wide module costs the walk down
        # to one, not a pass over all of its children.
        if cs.TRIE_QN_KEY in node:
            return True
        for key, child in node.items():
            if not key.startswith(cs.TRIE_INTERNAL_PREFIX):
                assert isinstance(child, dict)
                if self._subtree_has_entry(child):
                    return True
        return False

    def add_member_alias(
        self, alias_qn: QualifiedName, target_qn: QualifiedName
    ) -> None:
        targets = self._member_aliases.setdefault(alias_qn, [])
        if target_qn not in targets:
            targets.append(target_qn)
        self._aliases_by_target.setdefault(target_qn, set()).add(alias_qn)
        class_qn, _, member = alias_qn.rpartition(cs.SEPARATOR_DOT)
        self._alias_holders.setdefault(member, set()).add(class_qn)

    def member_alias_targets(
        self, alias_qn: QualifiedName
    ) -> tuple[QualifiedName, ...]:
        return tuple(self._member_aliases.get(alias_qn, ()))

    def set_non_method_members(
        self, class_qn: QualifiedName, names: Iterable[str]
    ) -> None:
        for name in self._non_method_members.pop(class_qn, ()):
            holders = self._non_method_holders.get(name)
            if holders is not None:
                holders.discard(class_qn)
                if not holders:
                    del self._non_method_holders[name]
        if members := frozenset(names):
            self._non_method_members[class_qn] = members
            for name in members:
                self._non_method_holders.setdefault(name, set()).add(class_qn)

    def binds_non_method(self, class_qn: QualifiedName, member: str) -> bool:
        return member in self._non_method_members.get(class_qn, ())

    def non_method_holders(self, member: str) -> frozenset[QualifiedName]:
        return frozenset(self._non_method_holders.get(member, ()))

    def alias_holders(self, member: str) -> frozenset[QualifiedName]:
        return frozenset(self._alias_holders.get(member, ()))

    def _drop_member_alias_target(self, target_qn: QualifiedName) -> None:
        # A re-parsed file registers its aliases again with its methods, so a
        # removed method takes its alias entries with it.
        for alias_qn in self._aliases_by_target.pop(target_qn, ()):
            targets = self._member_aliases.get(alias_qn, [])
            if target_qn in targets:
                targets.remove(target_qn)
            if not targets:
                self._member_aliases.pop(alias_qn, None)
                self._drop_alias_holder(alias_qn)

    def _drop_alias_holder(self, alias_qn: QualifiedName) -> None:
        class_qn, _, member = alias_qn.rpartition(cs.SEPARATOR_DOT)
        if (holders := self._alias_holders.get(member)) is not None:
            holders.discard(class_qn)
            if not holders:
                del self._alias_holders[member]
