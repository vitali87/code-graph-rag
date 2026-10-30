"""`rename_hierarchy`: what a rename takes along beyond the symbol asked for
(issue #1532), read from graphs indexed from real snippets. Pinned directly:
the override hierarchy and which of its members only a guess reaches, the
other parts of a C# partial type (#2469, #2579), each other build's
declaration of a Go name (#3360), and the Go interface of unknown
implementers that refuses the rename."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.editing.rename_hierarchy import (
    go_implementers_unknown,
    rename_hierarchy,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, QueryFn, ResultRow
from evals.cgr_graph import StatefulIngestor

pytestmark = pytest.mark.usefixtures("fingerprint_inputs_once_per_session")

PROJECT = "proj"
GO_MOD = "module proj\n\ngo 1.22\n"


def _graph(root: Path, files: dict[str, str]) -> QueryFn:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)

    def fetch_all(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        return store.fetch_all(query, None if params is None else dict(params))

    return fetch_all


@pytest.fixture
def go_treesitter(monkeypatch: pytest.MonkeyPatch) -> None:
    # The go/types frontend would build and run its helper; the edges these
    # tests read are the tree-sitter pass's.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)


SHAPES = {
    "pkg/__init__.py": "",
    "pkg/shapes.py": (
        "def helper():\n    return 1\n\n\n"
        "class Base:\n    def area(self):\n        return 0\n\n\n"
        "class Circle(Base):\n    def area(self):\n        return 3\n"
    ),
}


def test_a_method_takes_the_method_it_overrides_from_either_end(
    temp_repo: Path,
) -> None:
    query = _graph(temp_repo, SHAPES)

    child = rename_hierarchy(query, PROJECT, f"{PROJECT}.pkg.shapes.Circle.area")
    parent = rename_hierarchy(query, PROJECT, f"{PROJECT}.pkg.shapes.Base.area")

    # The symbol asked for is always first: the plan names the rename by it.
    assert child.members == [
        f"{PROJECT}.pkg.shapes.Circle.area",
        f"{PROJECT}.pkg.shapes.Base.area",
    ]
    assert parent.members == [
        f"{PROJECT}.pkg.shapes.Base.area",
        f"{PROJECT}.pkg.shapes.Circle.area",
    ]
    assert child.guessed == parent.guessed == frozenset()


def test_a_symbol_on_no_hierarchy_is_its_only_member(temp_repo: Path) -> None:
    query = _graph(temp_repo, SHAPES)

    found = rename_hierarchy(query, PROJECT, f"{PROJECT}.pkg.shapes.helper", temp_repo)

    assert found.members == [f"{PROJECT}.pkg.shapes.helper"]
    assert found.guessed == frozenset()


@pytest.mark.usefixtures("go_treesitter")
def test_a_go_method_matched_to_an_interface_by_name_is_a_guessed_member(
    temp_repo: Path,
) -> None:
    # Go's interfaces are satisfied structurally: the OVERRIDES edge is a
    # guess, so the other end joins the rename only on the caller's opt-in.
    query = _graph(
        temp_repo,
        {
            "go.mod": GO_MOD,
            "p/i.go": "package p\n\ntype Speaker interface {\n\tSpeak() string\n}\n",
            "p/t.go": (
                "package p\n\ntype Dog struct{}\n\n"
                'func (Dog) Speak() string { return "woof" }\n'
            ),
        },
    )
    method, declared = f"{PROJECT}.p.t.Dog.Speak", f"{PROJECT}.p.i.Speaker.Speak"

    found = rename_hierarchy(query, PROJECT, method, temp_repo)

    assert found.members == [method, declared]
    assert found.guessed == frozenset({declared})


GO_BUILDS = {
    "go.mod": GO_MOD,
    "p/t_linux.go": "package p\n\ntype T struct{}\n\nfunc (T) M() {}\n\nfunc Helper() {}\n",
    "p/t_windows.go": (
        "package p\n\ntype T struct{}\n\nfunc (T) M() {}\n\nfunc Helper() {}\n"
    ),
    # Compiled with both files above: another receiver's M, not a variant.
    "p/u.go": "package p\n\ntype U struct{}\n\nfunc (U) M() {}\n",
    # Another package's T.M, in a file named for a build as well.
    "r/t_windows.go": "package r\n\ntype T struct{}\n\nfunc (T) M() {}\n",
}


@pytest.mark.usefixtures("go_treesitter")
@pytest.mark.parametrize(
    ("asked", "variant"),
    [("p.t_linux.T.M", "p.t_windows.T.M"), ("p.t_windows.Helper", "p.t_linux.Helper")],
)
def test_each_builds_declaration_of_a_go_name_is_taken_along(
    temp_repo: Path, asked: str, variant: str
) -> None:
    query = _graph(temp_repo, GO_BUILDS)

    found = rename_hierarchy(query, PROJECT, f"{PROJECT}.{asked}", temp_repo)

    assert found.members == [f"{PROJECT}.{asked}", f"{PROJECT}.{variant}"]
    assert found.guessed == frozenset()


@pytest.mark.usefixtures("go_treesitter")
def test_a_go_name_without_build_twins_stays_alone(temp_repo: Path) -> None:
    query = _graph(temp_repo, GO_BUILDS)

    found = rename_hierarchy(query, PROJECT, f"{PROJECT}.p.u.U.M", temp_repo)

    assert found.members == [f"{PROJECT}.p.u.U.M"]


@pytest.mark.usefixtures("go_treesitter")
def test_a_twin_some_build_compiles_beside_it_is_no_variant(temp_repo: Path) -> None:
    # `b.go` builds everywhere, so linux compiles both `Twin`s: a
    # redeclaration, not one name declared once per build.
    query = _graph(
        temp_repo,
        {
            "go.mod": GO_MOD,
            "p/a_linux.go": "package p\n\nfunc Twin() {}\n",
            "p/b.go": "package p\n\nfunc Twin() {}\n",
        },
    )
    asked = f"{PROJECT}.p.a_linux.Twin"

    assert rename_hierarchy(query, PROJECT, asked, temp_repo).members == [asked]


@pytest.mark.usefixtures("go_treesitter")
def test_build_variants_need_the_tree_they_are_read_from(temp_repo: Path) -> None:
    # A variant is decided from each file's `package` clause and build
    # constraint, read from disk: without a root there is none, and a twin
    # whose file is gone is not a declaration any build still compiles.
    query = _graph(temp_repo, GO_BUILDS)
    asked = f"{PROJECT}.p.t_linux.T.M"

    assert rename_hierarchy(query, PROJECT, asked).members == [asked]
    (temp_repo / "p" / "t_windows.go").unlink()
    assert rename_hierarchy(query, PROJECT, asked, temp_repo).members == [asked]


CS_ORDER = (
    "namespace Shop {\n  public partial class Order {\n"
    "    public int Total() { return Tax() + 1; }\n  }\n}\n"
)
CS_ORDER_TAX = (
    "namespace Shop {\n  public partial class Order {\n"
    "    private int Tax() { return 2; }\n  }\n}\n"
)
CS_FILES = {
    "A/Order.cs": CS_ORDER,
    "A/Order.Tax.cs": CS_ORDER_TAX,
    # Partial too, but of another arity: `Order<T>` is another type (#2579).
    "A/Order.Generic.cs": "namespace Shop {\n  public partial class Order<T> { }\n}\n",
    "A/Box.cs": "namespace Shop {\n  public class Box { }\n}\n",
    # The same partial declaration in another directory is another type.
    "B/Order.cs": (
        "namespace Shop {\n  public partial class Order"
        " { public int Other() { return 3; } }\n}\n"
    ),
}


@pytest.mark.parametrize(
    ("asked", "other"),
    [
        ("A.Order.Shop.Order", "A.Order.Tax.Shop.Order"),
        ("A.Order.Tax.Shop.Order", "A.Order.Shop.Order"),
    ],
)
def test_each_part_of_a_csharp_partial_type_takes_the_others(
    temp_repo: Path, asked: str, other: str
) -> None:
    query = _graph(temp_repo, CS_FILES)

    found = rename_hierarchy(query, PROJECT, f"{PROJECT}.{asked}")

    assert found.members == [f"{PROJECT}.{asked}", f"{PROJECT}.{other}"]
    assert found.guessed == frozenset()


@pytest.mark.parametrize(
    "asked", ["A.Order.Generic.Shop.Order", "A.Box.Shop.Box", "B.Order.Shop.Order"]
)
def test_a_csharp_type_with_no_parts_of_its_group_stays_alone(
    temp_repo: Path, asked: str
) -> None:
    query = _graph(temp_repo, CS_FILES)

    found = rename_hierarchy(query, PROJECT, f"{PROJECT}.{asked}")

    assert found.members == [f"{PROJECT}.{asked}"]


# K embeds io.Closer, which cgr does not read: K's method set is unknown, so
# no type gets an edge to it and the hierarchy cannot show K's methods.
UNRESOLVED_EMBED = {
    "go.mod": GO_MOD,
    "q/k.go": (
        'package q\n\nimport "io"\n\ntype K interface {\n\tio.Closer\n\tn()\n\tN()\n}\n'
    ),
    "q/v.go": (
        "package q\n\ntype V struct{}\n\nfunc (V) n() {}\n\nfunc (V) N() {}\n\n"
        "func (V) Close() error { return nil }\n\nvar _ K = V{}\n"
    ),
    "r/w.go": (
        "package r\n\ntype W struct{}\n\nfunc (W) n() {}\n\nfunc (W) N() {}\n\n"
        "func use() { W{}.n() }\n"
    ),
}


@pytest.mark.usefixtures("go_treesitter")
@pytest.mark.parametrize(
    ("member", "name"),
    [
        ("q.v.V.n", "n"),
        ("q.k.K.n", "n"),
        # An exported name may satisfy K from any package.
        ("r.w.W.N", "N"),
    ],
)
def test_a_go_name_an_interface_of_unknown_implementers_declares_is_found(
    temp_repo: Path, member: str, name: str
) -> None:
    query = _graph(temp_repo, UNRESOLVED_EMBED)

    found = go_implementers_unknown(query, PROJECT, [f"{PROJECT}.{member}"])

    assert found == (f"{PROJECT}.q.k.K", "embeds", name)


@pytest.mark.usefixtures("go_treesitter")
@pytest.mark.parametrize(
    "member",
    [
        # An unexported name satisfies K only from K's own package.
        "r.w.W.n",
        # A name K does not declare.
        "q.v.V.Close",
    ],
)
def test_a_go_name_no_such_interface_could_need_is_not_found(
    temp_repo: Path, member: str
) -> None:
    query = _graph(temp_repo, UNRESOLVED_EMBED)

    assert go_implementers_unknown(query, PROJECT, [f"{PROJECT}.{member}"]) is None


def test_members_with_no_go_method_never_ask_for_such_an_interface(
    temp_repo: Path,
) -> None:
    graph = _graph(temp_repo, SHAPES)
    asked: list[str] = []

    def query(q: str, params: PropertyDict | None = None) -> list[ResultRow]:
        asked.append(q)
        return graph(q, params)

    members = [f"{PROJECT}.pkg.shapes.Circle.area", f"{PROJECT}.pkg.shapes.helper"]

    assert go_implementers_unknown(query, PROJECT, members) is None
    assert asked
    assert all("implementers_unknown" not in q for q in asked)
