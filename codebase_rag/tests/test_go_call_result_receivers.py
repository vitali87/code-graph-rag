"""Go method calls on a call result or a composite literal bind to the type (#2467).

`b := NewBox(); b.Bump()` already typed `b` from NewBox's declared result and
bound `Bump` exactly, but the same call written as a chain, `NewBox().Bump()`,
got no CALLS edge, and neither did a fluent chain (`NewBox().With(1).Bump()`)
or a parenthesised literal receiver (`(&Box{}).Bump()`). These pin the
receiver typing on the default tree-sitter path (no go/types frontend), plus
the shapes that must keep binding nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

BOX_GO = (
    "package m\n\n"
    "type Box struct{}\n\n"
    "func (b Box) Bump() int        { return 1 }\n"
    "func (b *Box) With(n int) *Box { return b }\n\n"
    "func NewBox() *Box { return &Box{} }\n\n"
    "type Other struct{}\n\n"
    "func (o *Other) Bump() int { return 2 }\n"
    "func (o *Other) Lock()     {}\n"
)


def _index(root: Path) -> _StatefulIngestor:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.GO not in parsers:
        pytest.skip("go parser not available")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run()
    store.flush_all()
    return store


def _project(tmp_path: Path, files: dict[str, str]) -> _StatefulIngestor:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return _index(root)


def _calls(store: _StatefulIngestor, caller: str) -> dict[str, str | None]:
    # callee qn -> the edge's resolution stamp, for the caller's CALLS edges.
    out: dict[str, str | None] = {}
    for edge, props in store.edge_props.items():
        _sl, src, rel, _tl, dst, _site = edge
        if rel == cs.RelationshipType.CALLS.value and str(src) == caller:
            stamp = props.get(cs.KEY_RESOLUTION)
            out[str(dst)] = None if stamp is None else str(stamp)
    for _sl, src, rel, _tl, dst in store.edges:
        if rel == cs.RelationshipType.CALLS.value and str(src) == caller:
            out.setdefault(str(dst), None)
    return out


def _instantiates(store: _StatefulIngestor, caller: str) -> set[str]:
    return {
        str(dst)
        for _sl, src, rel, _tl, dst in store.edges
        if rel == cs.RelationshipType.INSTANTIATES.value and str(src) == caller
    }


EXACT = cs.EdgeResolution.EXACT.value


def test_method_on_a_constructor_result_binds_exactly(tmp_path: Path) -> None:
    store = _project(
        tmp_path,
        {"m/box.go": BOX_GO + "\nfunc chained() int { return NewBox().Bump() }\n"},
    )
    calls = _calls(store, "proj.m.box.chained")
    assert calls == {
        "proj.m.box.NewBox": EXACT,
        "proj.m.box.Box.Bump": EXACT,
    }, calls


def test_every_hop_of_a_fluent_chain_binds(tmp_path: Path) -> None:
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO
            + "\nfunc fluent() int { return NewBox().With(1).With(2).Bump() }\n",
        },
    )
    calls = _calls(store, "proj.m.box.fluent")
    assert calls.get("proj.m.box.Box.With") == EXACT, calls
    assert calls.get("proj.m.box.Box.Bump") == EXACT, calls
    assert "proj.m.box.Other.Bump" not in calls, calls


def test_a_chain_split_across_lines_binds(tmp_path: Path) -> None:
    # gofmt puts each hop of a long builder chain on its own line; the line
    # breaks are part of the callee's source text, never part of a name.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO + "\nfunc multiLine() int {\n"
            "\treturn NewBox().\n"
            "\t\tWith(1).\n"
            "\t\tBump()\n"
            "}\n\n"
            "func (b *Box) Again() int {\n"
            "\treturn b.With(1).\n"
            "\t\tBump()\n"
            "}\n",
        },
    )
    calls = _calls(store, "proj.m.box.multiLine")
    assert calls.get("proj.m.box.Box.With") == EXACT, calls
    assert calls.get("proj.m.box.Box.Bump") == EXACT, calls
    # Rooted in a method on a typed receiver, which the name-based path
    # typed on one line already.
    method_rooted = _calls(store, "proj.m.box.Box.Again")
    assert method_rooted.get("proj.m.box.Box.Bump") == EXACT, method_rooted


@pytest.mark.parametrize("receiver", ["(&Box{})", "(Box{})", "Box{}"], ids=lambda r: r)
def test_a_composite_literal_receiver_binds_to_its_type(
    tmp_path: Path, receiver: str
) -> None:
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO,
            "m/use.go": f"package m\n\nfunc literal() {{ _ = {receiver}.Bump() }}\n",
        },
    )
    calls = _calls(store, "proj.m.use.literal")
    assert calls == {"proj.m.box.Box.Bump": EXACT}, calls
    assert _instantiates(store, "proj.m.use.literal") == {"proj.m.box.Box"}


def test_a_literal_rooted_chain_follows_each_result(tmp_path: Path) -> None:
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO
            + "\nfunc literalChain() int { return (&Box{}).With(1).Bump() }\n",
        },
    )
    calls = _calls(store, "proj.m.box.literalChain")
    assert calls == {
        "proj.m.box.Box.With": EXACT,
        "proj.m.box.Box.Bump": EXACT,
    }, calls


def test_a_constructor_in_a_sibling_file_types_the_chain(tmp_path: Path) -> None:
    # A package spans its directory: the constructor sits in another file of
    # the same package, as in `b := NewBox()` from that file.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO,
            "m/use.go": (
                "package m\n\nfunc sibling() int { return NewBox().With(1).Bump() }\n"
            ),
        },
    )
    calls = _calls(store, "proj.m.use.sibling")
    assert "proj.m.box.Box.With" in calls, calls
    assert "proj.m.box.Box.Bump" in calls, calls
    assert "proj.m.box.Other.Bump" not in calls, calls


def test_an_imported_constructor_types_its_result_in_its_own_package(
    tmp_path: Path,
) -> None:
    # `box.New()` returns `*Box` as written in package box. The caller's own
    # package declares a `Box` too; that one is not what New returns.
    store = _project(
        tmp_path,
        {
            "box/box.go": (
                "package box\n\n"
                "type Box struct{}\n\n"
                "func (b *Box) Bump() int       { return 1 }\n"
                "func (b *Box) With(n int) *Box { return b }\n\n"
                "func New() *Box { return &Box{} }\n"
            ),
            "app/main.go": (
                "package app\n\n"
                'import "proj/box"\n\n'
                "type Box struct{}\n\n"
                "func (b *Box) Bump() int { return 3 }\n\n"
                "func viaImport() int { return box.New().With(1).Bump() }\n\n"
                "func viaLiteral() int { return (&box.Box{}).Bump() }\n"
            ),
        },
    )
    calls = _calls(store, "proj.app.main.viaImport")
    assert "proj.box.box.New" in calls, calls
    assert calls.get("proj.box.box.Box.With") == EXACT, calls
    assert calls.get("proj.box.box.Box.Bump") == EXACT, calls
    assert "proj.app.main.Box.Bump" not in calls, calls

    literal_calls = _calls(store, "proj.app.main.viaLiteral")
    assert literal_calls == {"proj.box.box.Box.Bump": EXACT}, literal_calls


# Shapes that must keep binding nothing.


def test_a_method_the_result_struct_lacks_binds_no_namesake(tmp_path: Path) -> None:
    # Crate declares no Bump (Go would promote Box's through the embedded
    # field, which the tree-sitter path does not model). Other's Bump, and
    # another package's same-named Crate, merely share the names.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO
            + "\ntype Crate struct{ Box }\n\nfunc NewCrate() *Crate { return &Crate{} }\n",
            "m/use.go": "package m\n\nfunc promoted() int { return NewCrate().Bump() }\n",
            "z/crate.go": (
                "package z\n\ntype Crate struct{}\n\nfunc (c *Crate) Bump() int { return 7 }\n"
            ),
        },
    )
    calls = _calls(store, "proj.m.use.promoted")
    assert set(calls) == {"proj.m.box.NewCrate"}, calls


def test_a_multi_result_callee_does_not_type_a_chain(tmp_path: Path) -> None:
    # `Pair().Bump()` does not compile: a two-result call has no single value
    # to call a method on. Its first result must not stand in for one.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO
            + "\nfunc Pair() (*Box, error) { return &Box{}, nil }\n\n"
            "func pairChain() int { return Pair().Bump() }\n",
        },
    )
    calls = _calls(store, "proj.m.box.pairChain")
    assert "proj.m.box.Box.Bump" not in calls, calls
    assert "proj.m.box.Other.Bump" not in calls, calls


def test_a_container_result_does_not_type_a_chain(tmp_path: Path) -> None:
    # `Boxes()` returns a slice; a method on it is the slice's, never Box's.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO + "\nfunc Boxes() []Box { return nil }\n\n"
            "func sliceChain() int { return Boxes().Bump() }\n",
        },
    )
    calls = _calls(store, "proj.m.box.sliceChain")
    assert "proj.m.box.Box.Bump" not in calls, calls
    assert "proj.m.box.Other.Bump" not in calls, calls


def test_an_interface_result_does_not_bind_a_same_named_struct(
    tmp_path: Path,
) -> None:
    # NewBumper returns the interface `Bumper`; another package's struct of
    # that name is not it, though a project-wide name search would find it.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO + "\ntype Bumper interface{ Bump() int }\n\n"
            "func NewBumper() Bumper { return &Box{} }\n\n"
            "func ifaceChain() int { return NewBumper().Bump() }\n",
            "z/bumper.go": (
                "package z\n\ntype Bumper struct{}\n\nfunc (b *Bumper) Bump() int { return 9 }\n"
            ),
        },
    )
    calls = _calls(store, "proj.m.box.ifaceChain")
    assert "proj.z.bumper.Bumper.Bump" not in calls, calls
    assert "proj.m.box.Other.Bump" not in calls, calls


def test_a_literal_receiver_lacking_the_method_binds_nothing(tmp_path: Path) -> None:
    # Box has no Lock; Other's Lock merely shares the name.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO,
            "m/use.go": "package m\n\nfunc lacks() { (&Box{}).Lock() }\n",
        },
    )
    assert _calls(store, "proj.m.use.lacks") == {}


@pytest.mark.parametrize(
    ("package", "call"),
    [("bytes", "(&bytes.Buffer{}).Len()"), ("time", "time.Time{}.Unix()")],
    ids=["pointer", "value"],
)
def test_an_external_literal_receiver_binds_no_first_party_method(
    tmp_path: Path, package: str, call: str
) -> None:
    # The receiver is a standard-library struct; Box's Len and Unix merely
    # share the method names.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO + "\nfunc (b *Box) Len() int   { return 0 }\n"
            "func (b *Box) Unix() int64 { return 0 }\n",
            "m/use.go": (
                f'package m\n\nimport "{package}"\n\nfunc ext() {{ _ = {call} }}\n'
            ),
        },
    )
    assert _calls(store, "proj.m.use.ext") == {}


def test_an_untyped_receiver_still_types_no_chain(tmp_path: Path) -> None:
    # Neither `r` (an external type) nor `x` (bound from an external call) has
    # a first-party type. Getter.Get only shares the method name, so it says
    # nothing about what `Get()` returns and Bump stays unbound.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO + "\ntype Getter struct{}\n\n"
            "func (g *Getter) Get() *Box { return &Box{} }\n",
            "m/use.go": (
                'package m\n\nimport (\n\t"strings"\n\n\t"example.com/ext"\n)\n\n'
                "func typedExternal(r *strings.Reader) int { return r.Get().Bump() }\n\n"
                "func untyped() int {\n\tx := ext.Make()\n\treturn x.Get().Bump()\n}\n"
            ),
        },
    )
    for caller in ("proj.m.use.typedExternal", "proj.m.use.untyped"):
        calls = _calls(store, caller)
        assert "proj.m.box.Box.Bump" not in calls, (caller, calls)
        assert "proj.m.box.Other.Bump" not in calls, (caller, calls)


def test_a_method_rooted_chain_still_binds(tmp_path: Path) -> None:
    # `c.Root().Run()` typed through the receiver before this change; it
    # must keep doing so, and a slice-returning hop must keep stopping it.
    store = _project(
        tmp_path,
        {
            "m/cmd.go": (
                "package m\n\n"
                "type Command struct{}\n\n"
                "func (c *Command) Root() *Command   { return c }\n"
                "func (c *Command) Kids() []Command { return nil }\n"
                "func (c *Command) Run() int        { return 1 }\n\n"
                "func (c *Command) Use() int  { return c.Root().Run() }\n"
                "func (c *Command) Fan() int  { return c.Kids().Run() }\n"
            ),
        },
    )
    assert (
        _calls(store, "proj.m.cmd.Command.Use").get("proj.m.cmd.Command.Run") == EXACT
    )
    assert "proj.m.cmd.Command.Run" not in _calls(store, "proj.m.cmd.Command.Fan")
