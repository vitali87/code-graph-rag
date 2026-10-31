# Dispatch discovery, tested against synthetic graphs: the scan, the base
# resolution and the target computation, without an index (issue #3338).
# These pin the discovery alone, so a wrong target set cannot hide behind
# a re-parse that happens to fix the graph anyway.
from __future__ import annotations

from codebase_rag.dispatch_dependents import (
    ClassChange,
    ScannedFile,
    build_class_changes,
    dispatch_target_prefixes,
    scan_python_classes,
)

PROJECT = "proj"


def _scanned(
    key: str, module_qn: str, source: str, *, package_init: bool = False
) -> ScannedFile:
    classes, imports = scan_python_classes(source.encode())
    return ScannedFile(key, module_qn, package_init, classes, imports)


def test_scan_reads_nested_classes_methods_and_bases() -> None:
    source = """from typing import Generic, Protocol, TypeVar

T = TypeVar("T")


class Outer(Generic[T]):
    def outer_method(self): ...

    class Inner:
        def inner_method(self): ...

    def with_nested(self):
        class Local:
            def local_method(self): ...
"""
    classes, imports = scan_python_classes(source.encode())
    assert ("Outer",) in classes
    assert classes[("Outer",)].methods == {"outer_method", "with_nested"}
    assert classes[("Outer",)].bases == ("Generic",)
    assert classes[("Outer", "Inner")].methods == {"inner_method"}
    # A class defined inside a function body is not a dispatch target.
    assert ("Outer", "with_nested", "Local") not in classes
    assert any(binding.name == "Protocol" for binding in imports)


def test_scan_of_unparsable_bytes_is_empty() -> None:
    classes, imports = scan_python_classes(b"class Broken(:\n")
    assert classes == {} and imports == ()


def test_base_resolution_prefers_same_module_then_import() -> None:
    scanned = _scanned(
        "pkg/mod.py",
        "proj.pkg.mod",
        "from base import Transformer\n\nclass Splitter(Transformer):\n    pass\n",
    )
    old_classes = {"proj.base.Transformer": "base.py"}
    changes = build_class_changes(
        [scanned], old_classes, {}, {}, PROJECT, changed_keys=["pkg/mod.py"]
    )
    assert [change.new_bases for change in changes] == [("proj.base.Transformer",)]

    # A same-module class shadows the import of the same name.
    scanned = _scanned(
        "mod.py",
        "proj.mod",
        "from base import Transformer\n\nclass Transformer:\n    pass\n\nclass Splitter(Transformer):\n    pass\n",
    )
    changes = build_class_changes(
        [scanned],
        {"proj.base.Transformer": "base.py"},
        {},
        {},
        PROJECT,
        changed_keys=["mod.py"],
    )
    splitter = next(c for c in changes if c.qn == "proj.mod.Splitter")
    assert splitter.new_bases == ("proj.mod.Transformer",)


def test_base_resolution_handles_relative_and_aliased_imports() -> None:
    scanned = _scanned(
        "pkg/sub/mod.py",
        "proj.pkg.sub.mod",
        "from ..base import Transformer\nfrom .sib import Helper as H\n\nclass A(Transformer, H):\n    pass\n",
    )
    changes = build_class_changes(
        [scanned],
        {
            "proj.pkg.base.Transformer": "pkg/base.py",
            "proj.pkg.sub.sib.Helper": "pkg/sub/sib.py",
        },
        {},
        {},
        PROJECT,
        changed_keys=["pkg/sub/mod.py"],
    )
    assert changes[0].new_bases == (
        "proj.pkg.base.Transformer",
        "proj.pkg.sub.sib.Helper",
    )


def test_protocol_and_external_bases_produce_no_subclass_change() -> None:
    scanned = _scanned(
        "real.py",
        "proj.real",
        "from typing import Protocol\n\nclass Client(Protocol):\n    def get(self): ...\n\nclass MyDict(dict):\n    def get(self): ...\n",
    )
    changes = build_class_changes(
        [scanned], {}, {}, {}, PROJECT, changed_keys=["real.py"]
    )
    by_qn = {change.qn: change for change in changes}
    assert by_qn["proj.real.Client"].new_bases == ()
    assert by_qn["proj.real.MyDict"].new_bases == ()


def test_unresolved_import_base_invents_no_change() -> None:
    # `from foo import Bar` names no class the graph, this run's scan,
    # or the hierarchy holds, and the persisted edge spells the base
    # `foo.Bar` where the scan guesses `proj.foo.Bar`: the same base
    # under two spellings, so a comment-only edit reports no change.
    scanned = _scanned(
        "ext.py",
        "proj.ext",
        "from foo import Bar\n\nclass Child(Bar):\n    def get(self): ...\n",
    )
    changes = build_class_changes(
        [scanned],
        {"proj.ext.Child": "ext.py"},
        {"proj.ext.Child": {"get"}},
        {"proj.ext.Child": ["foo.Bar"]},
        PROJECT,
        changed_keys=["ext.py"],
    )
    assert changes == []


def test_graph_backed_base_in_unchanged_file_is_kept() -> None:
    # `Root` lives in an unchanged file with no hierarchy edges of its
    # own, so only the graph-backed set names it: gaining it as a base
    # must still be a change.
    scanned = _scanned(
        "child.py",
        "proj.child",
        "from root import Root\n\nclass Child(Root):\n    def m(self): ...\n",
    )
    changes = build_class_changes(
        [scanned],
        {"proj.child.Child": "child.py"},
        {"proj.child.Child": {"m"}},
        {},
        PROJECT,
        changed_keys=["child.py"],
        graph_class_qns={"proj.root.Root"},
    )
    assert [change.new_bases for change in changes] == [("proj.root.Root",)]


def test_base_added_by_another_file_this_run_is_kept() -> None:
    # The base is defined by a file this run adds, so the graph does
    # not hold it yet: the scan's own classes still vouch for it.
    base = _scanned("base.py", "proj.base", "class Base:\n    def m(self): ...\n")
    child = _scanned(
        "child.py",
        "proj.child",
        "from base import Base\n\nclass Child(Base):\n    def m(self): ...\n",
    )
    changes = build_class_changes(
        [base, child],
        {},
        {},
        {},
        PROJECT,
        changed_keys=["base.py", "child.py"],
    )
    child_change = next(c for c in changes if c.qn == "proj.child.Child")
    assert child_change.new_bases == ("proj.base.Base",)


def test_no_change_when_scan_matches_the_graph() -> None:
    scanned = _scanned(
        "split.py",
        "proj.split",
        "from base import Transformer\n\nclass Splitter(Transformer):\n    def do_match(self): ...\n",
    )
    changes = build_class_changes(
        [scanned],
        {"proj.split.Splitter": "split.py"},
        {"proj.split.Splitter": {"do_match"}},
        {"proj.split.Splitter": ["proj.base.Transformer"]},
        PROJECT,
        changed_keys=["split.py"],
    )
    assert changes == []


def test_removed_class_is_a_change_with_old_state_only() -> None:
    changes = build_class_changes(
        [],
        {"proj.split.Splitter": "split.py"},
        {"proj.split.Splitter": {"do_match"}},
        {"proj.split.Splitter": ["proj.base.Transformer"]},
        PROJECT,
        changed_keys=["split.py"],
    )
    assert changes == [
        ClassChange(
            "proj.split.Splitter",
            frozenset({"do_match"}),
            frozenset(),
            ("proj.base.Transformer",),
            (),
            present=False,
        )
    ]


def _change(
    qn: str,
    old_methods: set[str],
    new_methods: set[str],
    old_bases: tuple[str, ...] = (),
    new_bases: tuple[str, ...] = (),
) -> ClassChange:
    return ClassChange(
        qn, frozenset(old_methods), frozenset(new_methods), old_bases, new_bases
    )


def test_subclass_targets_cover_ancestors_and_descendants() -> None:
    # Grandparent: the call in Grandparent resolves to Grandparent.m,
    # and the new override sits two levels down.
    hierarchy = {
        "proj.m.Child": ["proj.m.Grandparent"],
        "proj.m.New": ["proj.m.Child"],
    }
    prefixes, bases = dispatch_target_prefixes(
        [_change("proj.m.New", set(), {"m"}, (), ("proj.m.Child",))],
        hierarchy,
        {"proj.m.Grandparent": {"m"}, "proj.m.Child": set()},
        {},
        PROJECT,
    )
    assert "proj.m.Grandparent.m" in prefixes
    assert "proj.m.Child.m" in prefixes
    assert bases == {"proj.m.Child"}


def test_third_party_base_is_never_a_dispatch_target() -> None:
    hierarchy = {"proj.m.MyDict": ["dict"]}
    prefixes, bases = dispatch_target_prefixes(
        [_change("proj.m.MyDict", set(), {"get"}, (), ("dict",))],
        hierarchy,
        {},
        {},
        PROJECT,
    )
    assert prefixes == set()
    assert bases == set()


def test_protocol_targets_need_full_conformance() -> None:
    hierarchy = {"proj.lib.Client": ["typing.Protocol"]}
    methods = {
        "proj.lib.Client": {"get", "set"},
        "proj.tests.Mock": {"get", "set"},
        "proj.real.Real": {"get"},
    }
    prefixes, _bases = dispatch_target_prefixes(
        [_change("proj.real.Real", set(), {"get"})],
        hierarchy,
        methods,
        {"proj.lib.Client": {"get", "set"}},
        PROJECT,
    )
    # The partial conformer names no target at all: no caller is queued.
    assert prefixes == set()

    prefixes, _bases = dispatch_target_prefixes(
        [_change("proj.real.Real", {"get"}, {"get", "set"})],
        hierarchy,
        methods,
        {"proj.lib.Client": {"get", "set"}},
        PROJECT,
    )
    assert "proj.lib.Client.get" in prefixes
    assert "proj.real.Real.get" in prefixes
    assert "proj.tests.Mock.get" in prefixes
    assert "proj.real.Real.set" in prefixes


def test_protocol_named_implementer_counts_without_full_set() -> None:
    hierarchy = {"proj.lib.ClientProtocol": ["typing.Protocol"]}
    methods = {
        "proj.lib.ClientProtocol": {"get", "set"},
        "proj.real.Client": {"get"},
    }
    prefixes, _bases = dispatch_target_prefixes(
        [_change("proj.real.Client", set(), {"get"})],
        hierarchy,
        methods,
        {"proj.lib.ClientProtocol": {"get", "set"}},
        PROJECT,
    )
    assert "proj.real.Client.get" in prefixes
    assert "proj.real.Client.set" not in prefixes


def test_removal_names_the_old_targets_too() -> None:
    # The conformer lost its method: the union of old and new state
    # names the old target, so its callers re-parse and the edge goes.
    hierarchy = {"proj.lib.Client": ["typing.Protocol"]}
    methods = {
        "proj.lib.Client": {"get"},
        "proj.real.Real": {"get"},
    }
    prefixes, _bases = dispatch_target_prefixes(
        [_change("proj.real.Real", {"get"}, set())],
        hierarchy,
        methods,
        {"proj.lib.Client": {"get"}},
        PROJECT,
    )
    assert "proj.real.Real.get" in prefixes
    assert "proj.lib.Client.get" in prefixes


def test_base_resolution_handles_dotted_bases_from_imports() -> None:
    # `from pkg import base; class Child(base.Transformer)` must resolve
    # to exactly one project prefix, not a doubled one.
    scanned = _scanned(
        "child.py",
        "proj.child",
        "from pkg import base\n\nclass Child(base.Transformer):\n    def do_match(self): ...\n",
    )
    changes = build_class_changes(
        [scanned],
        {"proj.pkg.base.Transformer": "pkg/base.py"},
        {},
        {},
        PROJECT,
        changed_keys=["child.py"],
    )
    assert [change.new_bases for change in changes] == [("proj.pkg.base.Transformer",)]

    scanned = _scanned(
        "child.py",
        "proj.child",
        "import pkg.mod as m\n\nclass Child(m.Transformer):\n    def do_match(self): ...\n",
    )
    changes = build_class_changes(
        [scanned],
        {"proj.pkg.mod.Transformer": "pkg/mod.py"},
        {},
        {},
        PROJECT,
        changed_keys=["child.py"],
    )
    assert [change.new_bases for change in changes] == [("proj.pkg.mod.Transformer",)]

    scanned = _scanned(
        "child.py",
        "proj.child",
        "import base\n\nclass Child(base.Transformer):\n    def do_match(self): ...\n",
    )
    changes = build_class_changes(
        [scanned],
        {"proj.base.Transformer": "base.py"},
        {},
        {},
        PROJECT,
        changed_keys=["child.py"],
    )
    assert [change.new_bases for change in changes] == [("proj.base.Transformer",)]


def test_ambiguous_same_named_base_falls_back_to_all_known() -> None:
    # No import evidence and two classes named Transformer: both count,
    # so a re-parse is over-queued rather than a dispatch target missed.
    scanned = _scanned(
        "split.py",
        "proj.split",
        "class Splitter(Transformer):\n    def do_match(self): ...\n",
    )
    changes = build_class_changes(
        [scanned],
        {
            "proj.a.Transformer": "a.py",
            "proj.b.Transformer": "b.py",
        },
        {},
        {},
        PROJECT,
        changed_keys=["split.py"],
    )
    assert len(changes) == 1
    assert set(changes[0].new_bases) == {
        "proj.a.Transformer",
        "proj.b.Transformer",
    }


def test_protocol_targets_name_the_inherited_method_owner() -> None:
    # `B` conforms only through `A.get`, inherited: the CALLS edge a
    # caller holds names `A.get`, the class that defines the method,
    # never `B.get`. Adding a full conformer must queue that caller,
    # so the prefixes name the owner, not the conformer.
    hierarchy = {
        "proj.lib.Client": ["typing.Protocol"],
        "proj.lib.B": ["proj.lib.A"],
    }
    methods = {
        "proj.lib.Client": {"get", "set"},
        "proj.lib.A": {"get"},
        "proj.lib.B": {"set"},
        "proj.real.Real": {"get", "set"},
    }
    prefixes, _bases = dispatch_target_prefixes(
        [_change("proj.real.Real", set(), {"get", "set"})],
        hierarchy,
        methods,
        {"proj.lib.Client": {"get", "set"}},
        PROJECT,
    )
    assert "proj.lib.A.get" in prefixes
    assert "proj.lib.B.set" in prefixes
    assert "proj.real.Real.get" in prefixes


def test_protocol_named_uniqueness_is_per_state() -> None:
    # Two partial `Client`s, one old and one newly added: the merged
    # class set makes the name non-unique, but the OLD state had
    # exactly one, so its old target must still be named and its
    # callers re-parsed to prune it. The new partial names nothing.
    hierarchy = {"proj.lib.ClientProtocol": ["typing.Protocol"]}
    # The old graph holds only the old partial: the new one arrives
    # through the change, as the merged state overlays it.
    methods = {
        "proj.lib.ClientProtocol": {"get", "set"},
        "proj.a.Client": {"get"},
    }
    prefixes, _bases = dispatch_target_prefixes(
        [_change("proj.b.Client", set(), {"get"})],
        hierarchy,
        methods,
        {"proj.lib.ClientProtocol": {"get", "set"}},
        PROJECT,
    )
    assert "proj.a.Client.get" in prefixes
    assert "proj.b.Client.get" not in prefixes


def test_method_union_covers_additions_removals_and_unchanged() -> None:
    # A class present in both states, gaining `c` and losing `b` with
    # bases unchanged: the target set names both directions, and the
    # unchanged method `a` too, so no caller of any of them is missed.
    hierarchy = {"proj.m.Child": ["proj.m.Base"]}
    methods = {"proj.m.Base": {"a"}, "proj.m.Child": {"a", "b"}}
    prefixes, _bases = dispatch_target_prefixes(
        [
            _change(
                "proj.m.Child",
                {"a", "b"},
                {"a", "c"},
                ("proj.m.Base",),
                ("proj.m.Base",),
            )
        ],
        hierarchy,
        methods,
        {},
        PROJECT,
    )
    for target in (
        "proj.m.Base.a",
        "proj.m.Child.a",
        "proj.m.Base.b",
        "proj.m.Child.b",
        "proj.m.Base.c",
        "proj.m.Child.c",
    ):
        assert target in prefixes


def test_protocol_conformance_completed_through_an_inherited_method() -> None:
    # The parent gains `set`; the child inherits it and only then
    # conforms. Neither the parent nor the child conforms on its own,
    # so the conformer set moving is what must queue the callers.
    hierarchy = {
        "proj.lib.Client": ["typing.Protocol"],
        "proj.lib.B": ["proj.lib.A"],
    }
    methods = {
        "proj.lib.Client": {"get", "set", "put"},
        "proj.lib.A": {"get"},
        "proj.lib.B": {"put"},
    }
    prefixes, _bases = dispatch_target_prefixes(
        [_change("proj.lib.A", {"get"}, {"get", "set"})],
        hierarchy,
        methods,
        {"proj.lib.Client": {"get", "set", "put"}},
        PROJECT,
    )
    assert "proj.lib.Client.put" in prefixes
    assert "proj.lib.B.put" in prefixes
    # `B` inherits `get` from `A`: the target is `A.get`, the class
    # that defines it, not `B.get`.
    assert "proj.lib.A.get" in prefixes
