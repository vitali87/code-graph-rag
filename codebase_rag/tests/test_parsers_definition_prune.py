"""What a removed or re-parsed file takes with it from the definition maps:
the ownership each map is pruned by, and the entries another file still owns,
which stay."""

from __future__ import annotations

from codebase_rag.parsers.definition_facts import DefinitionFacts
from codebase_rag.parsers.definition_prune import (
    RemovedFile,
    forget_file,
    pop_owned,
    stale_go_return_types,
    under,
)


def _removed(
    *prefixes: str,
    path: str = "pkg/a.py",
    qns_to_remove: frozenset[str] = frozenset(),
    owned: frozenset[str] = frozenset(),
    foreign: frozenset[str] = frozenset(),
) -> RemovedFile:
    return RemovedFile(
        module_qn_prefixes=set(prefixes),
        relative_posix=path,
        qns_to_remove=set(qns_to_remove),
        owned_qns=set(owned),
        foreign_qns=set(foreign),
    )


def _forget(facts: DefinitionFacts, removed: RemovedFile) -> dict[str, str]:
    class_owner_module: dict[str, str] = {}
    forget_file(facts, class_owner_module, {}, removed)
    return class_owner_module


def test_under_matches_the_prefix_and_what_sits_beneath_it() -> None:
    assert under("proj.a", {"proj.a"})
    assert under("proj.a.f", {"proj.a"})
    # A sibling sharing the prefix's characters is not beneath it.
    assert not under("proj.ab.f", {"proj.a"})
    assert not under("proj.f", set())


def test_pop_owned_removes_only_the_owned_entries() -> None:
    owners = {"proj.a.T": "proj.a", "proj.b.U": "proj.b"}
    assert pop_owned(owners, {"proj.a"}) == {"proj.a.T"}
    assert owners == {"proj.b.U": "proj.b"}


def test_stale_go_return_types_keeps_what_another_file_owns() -> None:
    facts = DefinitionFacts()
    facts.go_function_return_types.update(
        {
            "proj.pkg.Swept": "int",
            "proj.pkg.Mine": "string",
            "proj.pkg.Sibling": "bool",
            "proj.other.F": "error",
        }
    )
    removed = _removed(
        "proj.pkg",
        qns_to_remove=frozenset({"proj.pkg.Swept"}),
        foreign=frozenset({"proj.pkg.Sibling"}),
    )
    assert sorted(stale_go_return_types(facts, removed)) == [
        "proj.pkg.Mine",
        "proj.pkg.Swept",
    ]


def test_a_method_return_type_is_owned_through_its_natural_qn() -> None:
    # The span record names the `@line` duplicate; the map keys the natural
    # qn, under a receiver module the file does not own.
    facts = DefinitionFacts()
    facts.method_return_types.update(
        {"proj.recv.A.Clone": "A", "proj.recv.A.Other": "int"}
    )
    _forget(facts, _removed("proj.decl", owned=frozenset({"proj.recv.A.Clone@3"})))
    assert facts.method_return_types == {"proj.recv.A.Other": "int"}


def test_a_return_type_under_the_prefix_survives_when_another_file_owns_it() -> None:
    facts = DefinitionFacts()
    facts.method_return_types.update({"proj.a.T.m": "int", "proj.a.T.n": "str"})
    facts.csharp_method_return_types["proj.a.T.k"] = ("int", 0)
    _forget(facts, _removed("proj.a", foreign=frozenset({"proj.a.T.n"})))
    assert facts.method_return_types == {"proj.a.T.n": "str"}
    assert facts.csharp_method_return_types == {}


def test_a_class_row_goes_with_the_file_its_owner_record_names() -> None:
    facts = DefinitionFacts()
    facts.class_inheritance.update(
        {"proj.a.Child": ["proj.base.Base"], "proj.b.Kept": ["proj.base.Base"]}
    )
    facts.class_field_types["proj.a.Child"] = {"x": "int"}
    facts.interface_implementers["proj.base.Iface"] = {"proj.a.Child", "proj.b.Kept"}
    class_owner_module = {"proj.a.Child": "proj.a", "proj.b.Kept": "proj.b"}

    forget_file(facts, class_owner_module, {}, _removed("proj.a"))

    assert facts.class_inheritance == {"proj.b.Kept": ["proj.base.Base"]}
    assert facts.class_field_types == {}
    assert facts.interface_implementers == {"proj.base.Iface": {"proj.b.Kept"}}
    assert class_owner_module == {"proj.b.Kept": "proj.b"}


def test_a_rehydrated_class_is_owned_by_its_recorded_path() -> None:
    facts = DefinitionFacts()
    facts.class_inheritance["proj.a.Old"] = ["proj.base.Base"]
    facts.rehydrated_definition_paths["proj.a.Old"] = "pkg/a.py"
    _forget(facts, _removed("proj.unrelated", path="pkg/a.py"))
    assert facts.class_inheritance == {}


def test_a_rust_impl_target_leaves_only_its_implementer_pairs() -> None:
    facts = DefinitionFacts()
    facts.interface_implementers["proj.t.Trait"] = {"proj.a.Impl"}
    rust_owners = {"proj.a.Impl": "proj.a"}
    forget_file(facts, {}, rust_owners, _removed("proj.a"))
    assert facts.interface_implementers == {}
    assert rust_owners == {}


def test_csharp_class_arity_follows_the_owner_record_not_the_qn() -> None:
    # `proj/Core.cs` declares `namespace Util`: its class sits under the
    # sibling module `proj.Core.Util`, which must not take it.
    facts = DefinitionFacts()
    facts.csharp_class_generic_arity["proj.Core.Util.Helper"] = 1
    facts.csharp_class_owner_module["proj.Core.Util.Helper"] = "proj.Core"

    _forget(facts, _removed("proj.Core.Util", path="proj/Core/Util.cs"))
    assert facts.csharp_class_generic_arity == {"proj.Core.Util.Helper": 1}

    _forget(facts, _removed("proj.Core", path="proj/Core.cs"))
    assert facts.csharp_class_generic_arity == {}
    assert facts.csharp_class_owner_module == {}


def test_csharp_function_entries_go_with_the_file_that_owned_them() -> None:
    facts = DefinitionFacts()
    facts.csharp_generic_methods.update({"proj.a.T.Map", "proj.b.U.Map"})
    facts.csharp_extension_methods["Ext"] = [
        ("proj.a.X.Ext", "string", "proj.a.X", 1),
        ("proj.b.Y.Ext", "string", "proj.b.Y", 1),
    ]
    _forget(facts, _removed("proj.a"))
    assert facts.csharp_generic_methods == {"proj.b.U.Map"}
    assert facts.csharp_extension_methods == {
        "Ext": [("proj.b.Y.Ext", "string", "proj.b.Y", 1)]
    }
