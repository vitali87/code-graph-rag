"""A module-level class alias is a class receiver for nested construction.

`Alias = Outer` at module level binds the class, so `Alias.Inner(3)` constructs
`Outer.Inner` exactly as `Outer.Inner(3)` does. The receiver guard added for
#1641 asks whether a dotted call's receiver names a type or a module, and an
alias satisfied none of its branches (not `self`, not a typed local, not in
the class lookup, not in the import map because the binding is an assignment),
so the INSTANTIATES edge was dropped (issue #1672). A receiver bound to a
VALUE (`inst = make()`) is still not a constructor, which is the #1641 rule.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

MOD_A = (
    "class Outer:\n"
    "    class Inner:\n"
    "        def __init__(self, v):\n"
    "            self.v = v\n\n\n"
    "def make():\n"
    "    return Outer()\n\n\n"
    "def make_pair():\n"
    "    return Outer(), 1\n"
)
MOD_B = (
    "from mod_a import Outer, make\n\n"
    "Alias = Outer\n"
    "inst = make()\n\n\n"
    "def via_alias():\n"
    "    return Alias.Inner(3)\n\n\n"
    "def via_class():\n"
    "    return Outer.Inner(3)\n\n\n"
    "def via_value():\n"
    "    return inst.Inner(3)\n"
)


def _instantiates(root: Path) -> dict[str, set[str]]:
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run()
    store.flush_all()
    out: dict[str, set[str]] = {}
    for _sl, src, rel, _tl, dst in store.edges:
        if rel == cs.RelationshipType.INSTANTIATES.value:
            out.setdefault(str(src), set()).add(str(dst))
    return out


def test_a_module_level_alias_constructs_the_nested_class_like_the_class_does(
    tmp_path: Path,
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod_a.py").write_text(MOD_A, encoding="utf-8")
    (root / "mod_b.py").write_text(MOD_B, encoding="utf-8")

    by_caller = _instantiates(root)
    inner = "proj.mod_a.Outer.Inner"
    assert by_caller.get("proj.mod_b.via_class") == {inner}, (
        f"fixture guard: the direct spelling lost its edge: {by_caller}"
    )
    assert by_caller.get("proj.mod_b.via_alias") == {inner}, (
        f"the aliased spelling lost its INSTANTIATES edge: {by_caller}"
    )
    # The #1641 rule stands: a receiver bound to a value never constructs.
    assert "proj.mod_b.via_value" not in by_caller, by_caller


def test_an_alias_imported_from_its_defining_module_still_constructs(
    tmp_path: Path,
) -> None:
    # `from mod_b import Alias` lands in the import map, which the guard
    # consulted before the alias fallback, and `proj.mod_b.Alias` is neither
    # a class nor a namespace, so the edge was dropped exactly as before the
    # fix (#1672 local review). The alias is read in the module it came from.
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod_a.py").write_text(MOD_A, encoding="utf-8")
    (root / "mod_b.py").write_text(
        "from mod_a import Outer\n\nAlias = Outer\n", encoding="utf-8"
    )
    (root / "mod_c.py").write_text(
        "from mod_b import Alias\n\n\ndef via_alias():\n    return Alias.Inner(3)\n",
        encoding="utf-8",
    )

    by_caller = _instantiates(root)
    assert by_caller.get("proj.mod_c.via_alias") == {"proj.mod_a.Outer.Inner"}, (
        by_caller
    )


def test_an_alias_rebound_to_a_value_no_longer_constructs(tmp_path: Path) -> None:
    # The LAST module-level binding decides: after `Alias = make()` the name
    # holds a value, and a value never constructs (#1641; #1672 local review).
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod_a.py").write_text(MOD_A, encoding="utf-8")
    (root / "mod_b.py").write_text(
        "from mod_a import Outer, make\n\nAlias = Outer\nAlias = make()\n\n\n"
        "def via_alias():\n    return Alias.Inner(3)\n",
        encoding="utf-8",
    )

    by_caller = _instantiates(root)
    assert "proj.mod_b.via_alias" not in by_caller, by_caller


def test_an_alias_rebound_by_unpacking_no_longer_constructs(tmp_path: Path) -> None:
    # `Alias, other = make_pair()` rebinds `Alias` to a runtime value just as
    # a plain assignment does (#1759 review).
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod_a.py").write_text(MOD_A, encoding="utf-8")
    (root / "mod_b.py").write_text(
        "from mod_a import Outer, make_pair\n\nAlias = Outer\n"
        "Alias, other = make_pair()\n\n\n"
        "def via_alias():\n    return Alias.Inner(3)\n",
        encoding="utf-8",
    )

    by_caller = _instantiates(root)
    assert "proj.mod_b.via_alias" not in by_caller, by_caller


def test_an_attribute_target_in_an_unpacking_does_not_rebind_the_alias(
    tmp_path: Path,
) -> None:
    # `holder.Alias, other = make_pair()` assigns an attribute, not the
    # module-level name, so the alias keeps constructing (#1759 review).
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod_a.py").write_text(MOD_A, encoding="utf-8")
    (root / "mod_b.py").write_text(
        "from mod_a import Outer, make_pair\n\n"
        "class Holder:\n    pass\n\n\nholder = Holder()\n"
        "Alias = Outer\nholder.Alias, other = make_pair()\n\n\n"
        "def via_alias():\n    return Alias.Inner(3)\n",
        encoding="utf-8",
    )

    by_caller = _instantiates(root)
    assert by_caller.get("proj.mod_b.via_alias") == {"proj.mod_a.Outer.Inner"}, (
        by_caller
    )


def test_an_alias_never_constructs_a_rival_nested_class(tmp_path: Path) -> None:
    # `Alias.Inner()` resolves its member by bare name, so with a same-named
    # `Inner` nested in another class the lookup may land on the rival. A
    # class receiver may only construct a class nested in itself (CodeRabbit,
    # #1759): the edge, if any, is to `Outer.Inner`, never `Other.Inner`.
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod_a.py").write_text(
        "class Other:\n    class Inner:\n        pass\n\n\n" + MOD_A,
        encoding="utf-8",
    )
    (root / "mod_b.py").write_text(
        "from mod_a import Outer\n\nAlias = Outer\n\n\n"
        "def via_alias():\n    return Alias.Inner(3)\n\n\n"
        "def via_class():\n    return Outer.Inner(3)\n",
        encoding="utf-8",
    )

    by_caller = _instantiates(root)
    for caller in ("proj.mod_b.via_alias", "proj.mod_b.via_class"):
        # Exactly the receiver's own nested class: not the rival, and not
        # nothing (#1759 review, second round).
        assert by_caller.get(caller) == {"proj.mod_a.Outer.Inner"}, (caller, by_caller)


def test_a_chained_assignment_binds_every_alias(tmp_path: Path) -> None:
    # `Alias = Other = Outer` nests the second assignment on the first one's
    # right-hand side; both names bind the terminal value (CodeRabbit, #1759).
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod_a.py").write_text(MOD_A, encoding="utf-8")
    (root / "mod_b.py").write_text(
        "from mod_a import Outer\n\nAlias = Second = Outer\n\n\n"
        "def via_alias():\n    return Alias.Inner(3)\n\n\n"
        "def via_second():\n    return Second.Inner(3)\n",
        encoding="utf-8",
    )

    by_caller = _instantiates(root)
    for caller in ("proj.mod_b.via_alias", "proj.mod_b.via_second"):
        assert by_caller.get(caller) == {"proj.mod_a.Outer.Inner"}, (caller, by_caller)
