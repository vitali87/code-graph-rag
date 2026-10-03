"""Issue #2469: a C# partial type split across files is one type.

Each file declaring `partial class Order` holds its own Class node, and the
parts are joined at parse time for member and base lookups. Two places did
not join them:

- a type reference (`Plain(Order o)`, `Doubled(this Order o)`, a return
  type, a generic argument) matched every part by name, read them as equally
  near candidates and dropped the edge, so no ACCEPTS or RETURNS named the
  type at all;
- `rename` renamed the one part it was given. The other part kept declaring
  `partial class Order`, now a second type holding half the members, and the
  build broke while the report listed nothing ambiguous or unlocatable.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import RenameRefused, rename
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.type_facts import TypeReferenceResolver
from codebase_rag.tests.conftest import get_relationships, run_updater
from codebase_rag.types_defs import NodeType, PropertyDict, ResultRow
from evals.cgr_graph import _StatefulIngestor

SKIP = "c_sharp"
PROJECT = "cspart"

ORDER = (
    "namespace Shop {\n"
    "  public partial class Order {\n"
    "    public int Total() { return Tax() + 1; }\n"
    "  }\n"
    "}\n"
)
ORDER_TAX = (
    "namespace Shop {\n"
    "  public partial class Order {\n"
    "    private int Tax() { return 2; }\n"
    "    public int Count { get; set; }\n"
    "  }\n"
    "}\n"
)
# The issue's reproduction: two parameters typed as the partial class, and a
# non-partial class in the same shapes as the control.
EXT = (
    "namespace Shop {\n"
    "  public static class OrderExt {\n"
    "    public static int Doubled(this Order o) { return o.Total() * 2; }\n"
    "    public static int Plain(Order o) { return 1; }\n"
    "    public static int Twice(this Solo s) { return 2; }\n"
    "  }\n"
    "  public class Solo { }\n"
    "  public class Use {\n"
    "    public int Run() { var o = new Order(); o.Count = 3;"
    " return o.Doubled() + o.Count; }\n"
    "  }\n"
    "}\n"
)
# Only constructed and called: nothing names the type without a site.
USE = (
    "namespace Shop {\n"
    "  public class Use {\n"
    "    public int Run() { var o = new Order(); o.Count = 3;"
    " return o.Total() + o.Count; }\n"
    "  }\n"
    "}\n"
)

FIRST_PART = f"{PROJECT}.Order.Shop.Order"
SECOND_PART = f"{PROJECT}.Order.Tax.Shop.Order"


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _indexed(
    root: Path, files: dict[str, str]
) -> tuple[_StatefulIngestor, GraphUpdater]:
    _write(root, files)
    parsers, queries = load_parsers()
    if SKIP not in parsers:
        pytest.skip(f"{SKIP} parser not available")
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


def _cs_sources(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.cs"))
    }


def _edges(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {(c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel)}


def _targets_of(edges: set[tuple[str, str]], owner_marker: str) -> set[str]:
    return {target for source, target in edges if owner_marker in source}


# --- type references -------------------------------------------------------


def test_parameters_typed_as_a_partial_class_accept_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / PROJECT
    _write(root, {"Order.cs": ORDER, "Order.Tax.cs": ORDER_TAX, "Ext.cs": EXT})
    run_updater(root, mock_ingestor, skip_if_missing=SKIP)

    accepts = _edges(mock_ingestor, cs.RelationshipType.ACCEPTS)
    # Both parameters name the one type, on the part every other reference
    # to it binds (the constructor call below lands there too).
    assert _targets_of(accepts, ".OrderExt.Doubled(") == {FIRST_PART}, accepts
    assert _targets_of(accepts, ".OrderExt.Plain(") == {FIRST_PART}, accepts
    instantiates = _edges(mock_ingestor, cs.RelationshipType.INSTANTIATES)
    assert _targets_of(instantiates, ".Use.Run") == {FIRST_PART}, instantiates


def test_return_and_generic_argument_types_name_the_partial_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / PROJECT
    tax = ORDER_TAX.replace(
        "public int Count { get; set; }",
        "public int Count { get; set; }\n    public Order Clone() { return this; }",
    )
    factory = (
        "using System.Collections.Generic;\n"
        "namespace Shop {\n"
        "  public class Factory {\n"
        "    public Order Make() { return new Order(); }\n"
        "    public List<Order> Many() { return null; }\n"
        "  }\n"
        "}\n"
    )
    _write(root, {"Order.cs": ORDER, "Order.Tax.cs": tax, "Factory.cs": factory})
    run_updater(root, mock_ingestor, skip_if_missing=SKIP)

    returns = _edges(mock_ingestor, cs.RelationshipType.RETURNS)
    assert _targets_of(returns, ".Factory.Make") == {FIRST_PART}, returns
    assert _targets_of(returns, ".Factory.Many") == {FIRST_PART}, returns
    # Inside the second part's own file the name still means the one type,
    # not that file's part of it.
    assert _targets_of(returns, ".Order.Clone") == {FIRST_PART}, returns


def test_an_incremental_sync_keeps_the_edge_to_unchanged_parts(
    temp_repo: Path,
) -> None:
    # A sync that re-parses only the referencing file rebuilds the partial
    # groups of the unchanged parts from the graph; the reference must
    # still read as one type, not two.
    root = temp_repo / PROJECT
    store, updater = _indexed(
        root, {"Order.cs": ORDER, "Order.Tax.cs": ORDER_TAX, "Ext.cs": EXT}
    )
    (root / "Ext.cs").write_text(
        EXT.replace("return 1;", "return 7;"), encoding="utf-8"
    )
    updater.run()

    accepts = {
        (edge[1], edge[4])
        for edge in store.keyed_edges
        if edge[2] == cs.RelationshipType.ACCEPTS
    }
    assert _targets_of(accepts, ".OrderExt.Plain(") == {FIRST_PART}, accepts
    assert _targets_of(accepts, ".OrderExt.Doubled(") == {FIRST_PART}, accepts


def test_a_non_partial_class_still_gets_its_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / PROJECT
    _write(root, {"Order.cs": ORDER, "Order.Tax.cs": ORDER_TAX, "Ext.cs": EXT})
    run_updater(root, mock_ingestor, skip_if_missing=SKIP)

    accepts = _edges(mock_ingestor, cs.RelationshipType.ACCEPTS)
    assert _targets_of(accepts, ".OrderExt.Twice(") == {f"{PROJECT}.Ext.Shop.Solo"}, (
        accepts
    )


def test_two_unrelated_same_named_classes_stay_unresolved(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Not one type split in two but two types, equally near the reference:
    # guessing either would bind half the references to the wrong class.
    root = temp_repo / PROJECT
    _write(
        root,
        {
            "A/Order.cs": "namespace A { public class Order { } }\n",
            "B/Order.cs": "namespace B { public class Order { } }\n",
            "C/Use.cs": (
                "namespace C { public class Use {"
                " public static int Plain(Order o) { return 1; } } }\n"
            ),
        },
    )
    run_updater(root, mock_ingestor, skip_if_missing=SKIP)

    accepts = _edges(mock_ingestor, cs.RelationshipType.ACCEPTS)
    assert _targets_of(accepts, ".Use.Plain(") == set(), accepts


def test_partial_classes_of_two_directories_stay_two_types(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Each directory's parts are one type; the two directories' are two (the
    # partial-group rule: separate projects must not merge). From a third
    # directory both are equally near and the reference stays unresolved;
    # from inside one directory its own type is the nearer.
    root = temp_repo / PROJECT
    use = "namespace Shop { public class Use { public static int Plain(Order o) { return 1; } } }\n"
    _write(
        root,
        {
            "A/Order.cs": ORDER,
            "A/Order.Tax.cs": ORDER_TAX,
            "A/Use.cs": use,
            "B/Order.cs": ORDER,
            "B/Order.Tax.cs": ORDER_TAX,
            "C/Use.cs": use.replace("class Use", "class Far"),
        },
    )
    run_updater(root, mock_ingestor, skip_if_missing=SKIP)

    accepts = _edges(mock_ingestor, cs.RelationshipType.ACCEPTS)
    assert _targets_of(accepts, ".Use.Plain(") == {f"{PROJECT}.A.Order.Shop.Order"}, (
        accepts
    )
    assert _targets_of(accepts, ".Far.Plain(") == set(), accepts


PARTS = ["p.Order.Tax.Shop.Order", "p.Order.Shop.Order"]


def _resolver(*others: str, joined: bool) -> TypeReferenceResolver:
    registry = FunctionRegistryTrie()
    for qn in (*PARTS, *others):
        registry[qn] = NodeType.CLASS
    resolver = TypeReferenceResolver(registry, {}, "p")
    if joined:
        resolver.join_partial_groups({qn: PARTS for qn in PARTS})
    return resolver


def test_the_resolver_counts_the_parts_of_a_partial_type_once() -> None:
    # The issue's shape: from another file the two parts tie and the
    # reference was dropped; joined, they are one type, named by its
    # lowest part.
    assert _resolver(joined=False).resolve("Order", "p.Ext") is None
    assert _resolver(joined=True).resolve("Order", "p.Ext") == "p.Order.Shop.Order"


def test_the_parts_are_as_near_as_their_nearest_part() -> None:
    # From the second part's file the group is nearer than the unrelated
    # `p.Order.Other.Order` through that part, though the part naming the
    # group is no nearer than it.
    resolver = _resolver("p.Order.Other.Order", joined=True)
    assert resolver.resolve("Order", "p.Order.Tax") == "p.Order.Shop.Order"


def test_a_joined_type_still_ties_with_an_equally_near_other_type() -> None:
    resolver = _resolver("p.B.Order.Shop.Order", joined=True)
    assert resolver.resolve("Order", "p.Ext") is None


# --- rename ------------------------------------------------------------------


@pytest.mark.parametrize("part", [FIRST_PART, SECOND_PART], ids=["first", "second"])
def test_renaming_either_part_renames_every_part(temp_repo: Path, part: str) -> None:
    root = temp_repo / PROJECT
    store, updater = _indexed(
        root, {"Order.cs": ORDER, "Order.Tax.cs": ORDER_TAX, "Use.cs": USE}
    )

    report = rename(
        root, store.fetch_all, PROJECT, part, "Purchase", reingest=updater.reingest
    )

    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok, report.verdict
    assert set(report.hierarchy) == {FIRST_PART, SECOND_PART}
    sources = _cs_sources(root)
    assert "public partial class Purchase {" in sources["Order.cs"]
    assert "public partial class Purchase {" in sources["Order.Tax.cs"]
    assert "new Purchase()" in sources["Use.cs"]
    leftover = {rel for rel, text in sources.items() if re.search(r"\bOrder\b", text)}
    assert leftover == set(), sources


@pytest.mark.parametrize("part", [FIRST_PART, SECOND_PART], ids=["first", "second"])
def test_a_partial_class_named_by_a_parameter_refuses_and_writes_nothing(
    temp_repo: Path, part: str
) -> None:
    # The issue's files: the parameters are graph-known references with no
    # site to rewrite, so the rename refuses whichever part it is given,
    # instead of renaming one part and leaving the parameters behind.
    root = temp_repo / PROJECT
    store, updater = _indexed(
        root, {"Order.cs": ORDER, "Order.Tax.cs": ORDER_TAX, "Ext.cs": EXT}
    )
    before = _cs_sources(root)

    with pytest.raises(RenameRefused) as refused:
        rename(
            root, store.fetch_all, PROJECT, part, "Purchase", reingest=updater.reingest
        )

    owners = {site.owner for site in refused.value.ambiguous}
    assert any(".OrderExt.Doubled(" in owner for owner in owners), owners
    assert any(".OrderExt.Plain(" in owner for owner in owners), owners
    assert _cs_sources(root) == before


def test_a_same_named_partial_class_of_another_directory_is_left_alone(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    other = "namespace Shop {\n  public partial class Order { public int Other() { return 3; } }\n}\n"
    store, updater = _indexed(
        root,
        {
            "A/Order.cs": ORDER,
            "A/Order.Tax.cs": ORDER_TAX,
            "B/Order.cs": other,
        },
    )

    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.A.Order.Tax.Shop.Order",
        "Purchase",
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert set(report.hierarchy) == {
        f"{PROJECT}.A.Order.Shop.Order",
        f"{PROJECT}.A.Order.Tax.Shop.Order",
    }
    assert (root / "B" / "Order.cs").read_text(encoding="utf-8") == other


def test_a_non_partial_same_named_type_is_left_alone(temp_repo: Path) -> None:
    # `Box` and `Box<T>` share a name, a namespace and a directory, but
    # neither is partial: they are two types, and renaming one must not
    # touch the other.
    root = temp_repo / PROJECT
    generic = "namespace Shop {\n  public class Box<T> { }\n}\n"
    store, updater = _indexed(
        root,
        {
            "Box.cs": "namespace Shop {\n  public class Box { }\n}\n",
            "Box.Generic.cs": generic,
        },
    )

    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.Box.Shop.Box",
        "Crate",
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert report.hierarchy == (f"{PROJECT}.Box.Shop.Box",)
    assert (root / "Box.Generic.cs").read_text(encoding="utf-8") == generic


def test_renaming_a_python_class_never_asks_for_partial_parts(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    store, updater = _indexed(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "class Helper:\n    pass\n",
            "pkg/app.py": "from pkg.util import Helper\n\n\ndef run():\n    return Helper()\n",
        },
    )
    asked: list[str] = []

    def fetch_all(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        asked.append(query)
        return store.fetch_all(query, params)

    report = rename(
        root,
        fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.Helper",
        "Assist",
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert cs.CYPHER_SAME_NAMED_CSHARP_TYPES not in asked
