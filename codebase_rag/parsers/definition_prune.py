"""Forget what a removed or re-parsed file recorded in the definition maps.

The registry sweep decides which definitions the file owned; these functions
take that verdict and drop the file's entries from every map the definition
pass fills, so a map added there is pruned here, beside the others.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import NamedTuple

from ..utils import qn_markers
from .definition_facts import DefinitionFacts


class RemovedFile(NamedTuple):
    """What a removed or re-parsed file owned, as the registry sweep found it.

    `module_qn_prefixes` are the module qns it recorded, `relative_posix` its
    repo-relative path, `qns_to_remove` the definitions the sweep dropped,
    `owned_qns` the qns its span records name, and `foreign_qns` the qns
    another file still owns.
    """

    module_qn_prefixes: set[str]
    relative_posix: str
    qns_to_remove: set[str]
    owned_qns: set[str]
    foreign_qns: set[str]


def under(qn: str, module_qn_prefixes: Collection[str]) -> bool:
    """True when `qn` is one of the prefixes or sits beneath one."""
    return any(
        qn.startswith(f"{prefix}.") or qn == prefix for prefix in module_qn_prefixes
    )


def pop_owned(owner_module: dict[str, str], module_qns: Collection[str]) -> set[str]:
    """Remove and return the qns `owner_module` attributes to `module_qns`."""
    owned = {qn for qn, module_qn in owner_module.items() if module_qn in module_qns}
    for qn in owned:
        del owner_module[qn]
    return owned


def stale_go_return_types(facts: DefinitionFacts, removed: RemovedFile) -> list[str]:
    """The removed file's Go function return types."""
    # Keyed by the same function qns as the registry (issue #1668): a deleted
    # Go file's entry left behind wins the `setdefault`-filled package index
    # over a same-name sibling parsed later. Same ownership filter as the registry sweep, so a qn another
    # file still owns survives; the map's own keys are checked as well as
    # `qns_to_remove` because an entry can outlive its registry row.
    go_return_types = facts.go_function_return_types
    stale_go_qns = [
        qn
        for qn in go_return_types
        if qn in removed.qns_to_remove
        or (
            any(
                qn.startswith(f"{prefix}.") or qn == prefix
                for prefix in removed.module_qn_prefixes
            )
            and qn not in removed.foreign_qns
        )
    ]
    return stale_go_qns


def forget_file(
    facts: DefinitionFacts,
    class_owner_module: dict[str, str],
    rust_impl_owner_module: dict[str, str],
    removed: RemovedFile,
) -> None:
    """Drop the removed file's return types, C# entries and class rows."""
    owned_natural = _drop_method_return_types(
        facts,
        removed.module_qn_prefixes,
        removed.qns_to_remove,
        removed.owned_qns,
        removed.foreign_qns,
    )
    _drop_csharp_entries(
        facts,
        removed.qns_to_remove,
        owned_natural,
        removed.foreign_qns,
        removed.module_qn_prefixes,
    )
    _drop_class_keyed_entries(
        facts,
        class_owner_module,
        rust_impl_owner_module,
        removed.module_qn_prefixes,
        removed.relative_posix,
    )


def _drop_method_return_types(
    facts: DefinitionFacts,
    module_qn_prefixes: set[str],
    qns_to_remove: set[str],
    owned_qns: set[str],
    foreign_qns: set[str],
) -> set[str]:
    """Drop the removed file's method return types; its owned natural qns."""
    # C++, Rust and Dart free functions, every language's methods and Go
    # receiver methods record their return type under the definition's qn
    # (issue #1738). A Go method is
    # keyed by its RECEIVER's module, so its qn can sit under a prefix this
    # file does not own. Ownership therefore comes from the SPAN records
    # (`owned_qns`), not from the registry sweep alone: a receiver method
    # whose registry row is already gone never enters `qns_to_remove`,
    # and no prefix of the declaring file matches it, so its return type
    # outlived both (#1752). The prefix arm covers an entry filed
    # under this file's own module; `foreign_qns` protects a qn another
    # file still owns, as it does for the registry, EXCEPT where this
    # file owns the qn too: the map holds one value per qn, the deleted
    # definition may be the one that wrote it, and the survivor is not
    # re-parsed by this event, so the entry goes and the next parse of
    # the survivor re-records it. A missing type is a degraded answer; a
    # stale one is a wrong answer (#1752).
    # Ownership is compared on the NATURAL qn on both sides: the map is
    # usually keyed by it even when the registry filed the definition as
    # a `@line` duplicate (the second declaration of one receiver method
    # writes `...A.Clone`, its span record says `...A.Clone@3`), and a
    # few writers (deferred Rust functions, duplicate C++ out-of-class
    # methods) key the map by the `@line` name itself (#1752).
    # The C# map (`csharp_method_return_types`, keyed the same way with a
    # (type, arity) value, issue #1753) takes the same filter.
    method_return_types = (
        facts.method_return_types.keys() | facts.csharp_method_return_types.keys()
    )
    owned_natural = {qn_markers.natural_qn(qn) for qn in owned_qns}
    stale_method_qns = [
        qn
        for qn in method_return_types
        if qn in qns_to_remove
        or qn_markers.natural_qn(qn) in owned_natural
        or (
            qn not in foreign_qns
            and any(
                qn.startswith(f"{prefix}.") or qn == prefix
                for prefix in module_qn_prefixes
            )
        )
    ]
    facts.drop_return_types(stale_method_qns)
    return owned_natural


def _drop_csharp_entries(
    facts: DefinitionFacts,
    qns_to_remove: set[str],
    owned_natural: set[str],
    foreign_qns: set[str],
    module_qn_prefixes: set[str],
) -> None:
    """Drop a removed file's entries from `csharp_generic_methods`,
    `csharp_class_generic_arity`, `csharp_local_functions` and
    `csharp_extension_methods`.

    Left behind, a deleted file's entries survive on a reused updater and
    go on steering resolution at a definition that is gone (issue #1769).

    Three of the four key by the DEFINITION's qn, so they take exactly
    the filter the registry sweep and the return-type maps use: already
    swept, or owned by this file's span records on the natural qn, or
    under one of its module prefixes with `foreign_qns` excluded.
    `csharp_class_generic_arity` keys by a CLASS qn instead. Classes
    record no function span, so `owned_natural` can never name one and
    `foreign_qns` is vacuous for them; ownership comes from the module
    index instead. A class qn does sit under the module qn of its
    declaring file, but it can equally sit under a SHORTER module qn
    belonging to a different file: `proj/A.cs` records `proj.A` and
    `proj/A/B.cs` records `proj.A.B`, so deleting `A.cs` on the prefix
    alone would take `B.cs`'s `proj.A.B.N.Nested` with it. A qn under a
    surviving module is therefore kept, whichever prefix also matches.
    A partial type's other halves are separate qns under their own
    modules and each is pruned by its own deletion.
    """

    def owned(qn: str) -> bool:
        return (
            qn in qns_to_remove
            or qn_markers.natural_qn(qn) in owned_natural
            or (qn not in foreign_qns and under(qn, module_qn_prefixes))
        )

    extension_owners = {
        owner
        for entries in facts.csharp_extension_methods.values()
        for owner, *_rest in entries
    }
    function_qns = {
        qn
        for qn in (
            facts.csharp_generic_methods
            | facts.csharp_local_functions.keys()
            | facts.csharp_call_shapes.keys()
            | extension_owners
        )
        if owned(qn)
    }
    # `foreign_qns` is built from function span records, and a class
    # registers none, so for a class qn it is vacuously false and cannot
    # protect anything. Ownership comes from a record written at INGEST
    # instead of being inferred from the qn, because the qn cannot yield
    # it: a C# class qn embeds its namespace, so `proj/Core.cs` declaring
    # `namespace Util` produces `proj.Core.Util.Helper`, which sits under
    # the SIBLING module `proj.Core.Util` (`proj/Core/Util.cs`). Both a
    # prefix rule and a longest-prefix rule therefore attribute that
    # class to the wrong file and err in both directions -- deleting the
    # sibling drops a live class, and deleting its real declarer keeps a
    # dead one (#1769). A class with no recorded owner (ingested
    # before this map existed) is left alone rather than guessed at.
    owner_module = facts.csharp_class_owner_module
    class_qns = {
        qn
        for qn in facts.csharp_class_generic_arity
        if owner_module.get(qn) in module_qn_prefixes
    }
    if function_qns or class_qns:
        facts.drop_csharp_entries(function_qns, class_qns)


def _drop_class_keyed_entries(
    facts: DefinitionFacts,
    class_owner_module: dict[str, str],
    rust_impl_owner_module: dict[str, str],
    module_qn_prefixes: set[str],
    relative_posix: str,
) -> None:
    """Drop a removed file's entries from `class_inheritance`,
    `class_field_types`, the C# partial groups and the parsed implementer
    pairs.

    All three are keyed by a CLASS qn. Left behind, a deleted file's rows
    outlive it on a reused updater (issues #1772, #2016), and that is a wrong
    answer rather than a missing one: `class_field_types` types a receiver reached through a
    field, and `class_inheritance` is walked to reach base-class members
    and drives the OVERRIDES arbitration -- both at a class that is gone.

    Unlike #1769's four maps these are written by EVERY language, so the
    owner index consulted here is the cross-language `class_owner_module`
    rather than the C#-only one. Ownership is read from that record and
    never inferred from the qn (#1769): a C# class qn embeds its namespace, so `proj/Core.cs` declaring
    `namespace Util` yields `proj.Core.Util.Helper`, which sits under the
    SIBLING module `proj.Core.Util`; a prefix rule errs in both directions
    there.

    Ownership comes from two records, because two different paths write
    `class_inheritance`. Classes this updater PARSED are keyed in
    `class_owner_module` at ingest. Classes it only REHYDRATED from the
    graph never reach that ingest, so an incremental run -- where most
    classes are rehydrated rather than re-parsed -- would otherwise leave
    every one of their entries behind, which is the very defect this
    closes. Their declaring file is already known: the rehydrate records
    `rehydrated_definition_paths[qn] = path`, relative and posix-formed, so
    it is compared as such. The caller drops those rows only after this
    runs: dropped first, they could not name the rehydrated classes.
    """
    stale = {
        qn
        for qn, module_qn in class_owner_module.items()
        if module_qn in module_qn_prefixes
    }
    stale |= {
        qn
        for qn, path in facts.rehydrated_definition_paths.items()
        if path == relative_posix
    }
    # A Rust impl target with no class node is attributed by its own
    # record; it keys no class map, so it only leaves the pairs.
    impl_targets = pop_owned(rust_impl_owner_module, module_qn_prefixes)
    facts.drop_implementers(stale | impl_targets)
    # The owner record goes with each class: it names a file this updater
    # no longer has, and keeping it would re-sweep the same qn on the
    # next deletion of a file that happens to reuse the module qn.
    for qn in stale:
        facts.drop_class(qn)
        class_owner_module.pop(qn, None)
