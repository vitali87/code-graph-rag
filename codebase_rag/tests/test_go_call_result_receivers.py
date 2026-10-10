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
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor


@pytest.fixture(autouse=True)
def _pin_treesitter(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default GO_FRONTEND=auto runs go/types wherever its helper builds
    # with the local Go, and go/types binds what tree-sitter cannot (a method
    # promoted through an embedded field), so these tree-sitter facts are only
    # pinned with the frontend pinned too.
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)


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
    assert calls.get("proj.m.box.Box.With") == EXACT, calls
    assert calls.get("proj.m.box.Box.Bump") == EXACT, calls
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


# A result type spelled with another package's name (`*model.Item`) is read
# through the DECLARING file's imports, the way a qualified literal is.

MODEL_GO = (
    "package model\n\n"
    "type Item struct{}\n\n"
    'func (i *Item) Name() string { return "" }\n'
    "func (i *Item) Next() *Item  { return i }\n"
)
QUALIFIED_BOX_GO = (
    "package box\n\n"
    "import (\n"
    '\t"bytes"\n\n'
    '\t"proj/model"\n'
    '\tm "proj/model"\n'
    ")\n\n"
    "type Box struct{}\n\n"
    "func (b *Box) Item() *model.Item     { return &model.Item{} }\n"
    "func (b *Box) Buf() *bytes.Buffer    { return nil }\n"
    "func (b *Box) Ghost() *model.Ghost   { return nil }\n"
    "func (b *Box) Len() int              { return 0 }\n"
    'func (b *Box) Name() string          { return "" }\n\n'
    "func New() *Box                      { return &Box{} }\n"
    "func NewItem() *model.Item           { return &model.Item{} }\n"
    "func NewAliased() *m.Item            { return &m.Item{} }\n"
    "func NewBuf() *bytes.Buffer          { return nil }\n\n"
    "func sameFile() string { return NewItem().Next().Name() }\n"
)


def _qualified_project(tmp_path: Path, app_body: str) -> _StatefulIngestor:
    # The app imports only box: every result type is spelled in box's file.
    return _project(
        tmp_path,
        {
            "model/item.go": MODEL_GO,
            "box/box.go": QUALIFIED_BOX_GO,
            "app/main.go": (
                'package app\n\nimport "proj/box"\n\n'
                "type Item struct{}\n\n"
                'func (i *Item) Name() string { return "" }\n\n' + app_body
            ),
        },
    )


def test_a_hop_returning_another_packages_struct_binds(tmp_path: Path) -> None:
    store = _qualified_project(
        tmp_path,
        "func viaMethod() string { return box.New().Item().Next().Name() }\n\n"
        "func viaConstructor() string { return box.NewItem().Name() }\n",
    )
    via_method = _calls(store, "proj.app.main.viaMethod")
    assert via_method.get("proj.box.box.Box.Item") == EXACT, via_method
    assert via_method.get("proj.model.item.Item.Next") == EXACT, via_method
    assert via_method.get("proj.model.item.Item.Name") == EXACT, via_method
    # The caller's own Item is not what box's methods return.
    assert "proj.app.main.Item.Name" not in via_method, via_method

    via_constructor = _calls(store, "proj.app.main.viaConstructor")
    assert via_constructor.get("proj.model.item.Item.Name") == EXACT, via_constructor
    assert "proj.app.main.Item.Name" not in via_constructor, via_constructor

    same_file = _calls(store, "proj.box.box.sameFile")
    assert same_file.get("proj.model.item.Item.Next") == EXACT, same_file
    assert same_file.get("proj.model.item.Item.Name") == EXACT, same_file
    assert "proj.box.box.Box.Name" not in same_file, same_file


def test_an_aliased_import_resolves_through_its_alias(tmp_path: Path) -> None:
    # `m "proj/model"` binds `m`, so `*m.Item` is model's Item.
    store = _qualified_project(
        tmp_path, "func viaAlias() string { return box.NewAliased().Name() }\n"
    )
    calls = _calls(store, "proj.app.main.viaAlias")
    assert calls.get("proj.model.item.Item.Name") == EXACT, calls
    assert "proj.app.main.Item.Name" not in calls, calls


def test_an_external_or_unknown_qualified_result_binds_nothing(tmp_path: Path) -> None:
    # bytes.Buffer is the standard library's and model has no Ghost: Box's
    # own Len and Name merely share the method names.
    store = _qualified_project(
        tmp_path,
        "func extCtor() int { return box.NewBuf().Len() }\n\n"
        "func extHop() int { return box.New().Buf().Len() }\n\n"
        "func ghostHop() string { return box.New().Ghost().Name() }\n",
    )
    assert set(_calls(store, "proj.app.main.extCtor")) == {"proj.box.box.NewBuf"}
    assert set(_calls(store, "proj.app.main.extHop")) == {
        "proj.box.box.New",
        "proj.box.box.Box.Buf",
    }
    assert set(_calls(store, "proj.app.main.ghostHop")) == {
        "proj.box.box.New",
        "proj.box.box.Box.Ghost",
    }


def test_a_local_bound_from_a_qualified_result_keeps_its_edge(tmp_path: Path) -> None:
    # `y := b.Item()` stays untyped, as before: a qualified type read in the
    # caller's file names nothing the resolver can look up, and typing `y`
    # with it would drop the edge `y.Name()` already gets by name.
    store = _project(
        tmp_path,
        {
            "model/item.go": MODEL_GO,
            "box/box.go": (
                'package box\n\nimport "proj/model"\n\ntype Box struct{}\n\n'
                "func (b *Box) Item() *model.Item { return &model.Item{} }\n"
            ),
            "app/main.go": (
                'package app\n\nimport "proj/box"\n\n'
                "func viaLocal(b *box.Box) string { y := b.Item(); return y.Name() }\n"
            ),
        },
    )
    calls = _calls(store, "proj.app.main.viaLocal")
    assert "proj.model.item.Item.Name" in calls, calls


# A result type that is a type parameter names the call's type argument, not a
# declared type; a call through an import names that package's function only.


def test_a_type_parameter_result_binds_no_same_named_struct(tmp_path: Path) -> None:
    # `Identity[T any](v T) T` returns whatever it is given; the package's
    # struct `T` only shares the parameter's name. Same for a method of a
    # generic receiver (`Holder[T].Get() T`).
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO
            + "\ntype T struct{}\n\nfunc (t T) Bump() int { return 3 }\n\n"
            "type Holder[T any] struct{ v T }\n\n"
            "func (h *Holder[T]) Get() T { return h.v }\n\n"
            "func Identity[T any](v T) T { return v }\n\n"
            "func Wrap[T any](v T) *Box { return &Box{} }\n",
            "m/use.go": (
                "package m\n\n"
                "func viaFunction() int { return Identity(Other{}).Bump() }\n\n"
                "func viaMethod() int { return (&Holder[Other]{}).Get().Bump() }\n\n"
                "func viaConcrete() int { return Wrap(Other{}).Bump() }\n"
            ),
        },
    )
    for caller in ("proj.m.use.viaFunction", "proj.m.use.viaMethod"):
        calls = _calls(store, caller)
        assert "proj.m.box.T.Bump" not in calls, (caller, calls)
    # A generic function whose result names a declared type still types it.
    concrete = _calls(store, "proj.m.use.viaConcrete")
    assert concrete.get("proj.m.box.Box.Bump") == EXACT, concrete


def test_an_instantiated_generic_result_binds_its_base_type(tmp_path: Path) -> None:
    store = _project(
        tmp_path,
        {
            "m/gen.go": (
                "package m\n\n"
                "type Gen[X any] struct{}\n\n"
                "func (g *Gen[X]) Bump() int { return 1 }\n\n"
                "func NewGen() *Gen[int] { return &Gen[int]{} }\n\n"
                "func generic() int { return NewGen().Bump() }\n"
            ),
        },
    )
    calls = _calls(store, "proj.m.gen.generic")
    assert calls.get("proj.m.gen.Gen.Bump") == EXACT, calls


def test_a_call_through_an_import_never_binds_a_local_namesake(
    tmp_path: Path,
) -> None:
    # `bytes.NewBuffer` is the standard library's and `box.Missing` does not
    # exist; the caller's own NewBuffer and Missing only share the names, so
    # their results type nothing.
    store = _project(
        tmp_path,
        {
            "box/box.go": "package box\n\ntype Box struct{}\n",
            "m/box.go": (
                "package m\n\n"
                'import (\n\t"bytes"\n\n\t"proj/box"\n)\n\n'
                "type Local struct{}\n\n"
                "func (l *Local) Len() int { return 0 }\n\n"
                "func NewBuffer(b []byte) *Local { return &Local{} }\n"
                "func Missing() *Local           { return &Local{} }\n\n"
                "var _ = box.Box{}\n\n"
                "func viaStdlib() int  { return bytes.NewBuffer(nil).Len() }\n"
                "func viaMissing() int { return box.Missing().Len() }\n"
                "func viaLocal() int   { return NewBuffer(nil).Len() }\n"
            ),
        },
    )
    for caller in ("proj.m.box.viaStdlib", "proj.m.box.viaMissing"):
        calls = _calls(store, caller)
        assert "proj.m.box.Local.Len" not in calls, (caller, calls)
    # The caller's own NewBuffer, called by its bare name, is the local one.
    local = _calls(store, "proj.m.box.viaLocal")
    assert local.get("proj.m.box.Local.Len") == EXACT, local


# A `_test.go` file is compiled only under `go test`, and a `package m_test`
# file is another package: neither declares anything a production file sees.

TEST_SIBLING_GO = (
    "package m\n\n"
    "type Other2 struct{}\n\n"
    "func (o *Other2) Bump() int { return 4 }\n\n"
    "func NewBox() *Other2 { return &Other2{} }\n"
)


def test_a_test_file_namesake_does_not_hide_the_production_function(
    tmp_path: Path,
) -> None:
    # Production `NewBox() *Box` beside a `_test.go` `NewBox() *Other2`: the
    # production caller sees only the first, so its chain still binds.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO,
            "m/box_test.go": TEST_SIBLING_GO,
            "m/use.go": "package m\n\nfunc prod() int { return NewBox().Bump() }\n",
        },
    )
    calls = _calls(store, "proj.m.use.prod")
    assert calls.get("proj.m.box.Box.Bump") == EXACT, calls
    assert "proj.m.box_test.Other2.Bump" not in calls, calls


def test_a_test_file_caller_still_binds_its_constructor(tmp_path: Path) -> None:
    # An internal test (`package m`) sees the production NewBox; an external
    # test package (`package m_test`) declaring its own NewBox sees only that.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO,
            "m/box_test.go": (
                "package m\n\nfunc TestChain() int { return NewBox().Bump() }\n"
            ),
            "m/ext_test.go": (
                "package m_test\n\n"
                "type Ext struct{}\n\n"
                "func (e *Ext) Bump() int { return 5 }\n\n"
                "func NewBox() *Ext { return &Ext{} }\n\n"
                "func TestExt() int { return NewBox().Bump() }\n"
            ),
        },
    )
    internal = _calls(store, "proj.m.box_test.TestChain")
    assert internal.get("proj.m.box.Box.Bump") == EXACT, internal
    external = _calls(store, "proj.m.ext_test.TestExt")
    assert external.get("proj.m.ext_test.Ext.Bump") == EXACT, external
    assert "proj.m.box.Box.Bump" not in external, external


def test_build_variants_returning_one_struct_still_bind(tmp_path: Path) -> None:
    # `//go:build` variants each declare NewBox; they agree on the result.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO.replace("func NewBox() *Box { return &Box{} }\n\n", ""),
            "m/new_linux.go": (
                "//go:build linux\n\npackage m\n\n"
                "func NewBox() *Box { return &Box{} }\n"
            ),
            "m/new_other.go": (
                "//go:build !linux\n\npackage m\n\n"
                "func NewBox() *Box { return new(Box) }\n"
            ),
            "m/use.go": "package m\n\nfunc variants() int { return NewBox().Bump() }\n",
        },
    )
    calls = _calls(store, "proj.m.use.variants")
    assert calls.get("proj.m.box.Box.Bump") == EXACT, calls


def test_an_external_test_package_sees_the_package_it_imports(
    tmp_path: Path,
) -> None:
    # `package m_test` imports its own directory's `package m`, by name or
    # with a dot. Through the import it sees m's production files, whose
    # package clause is `m`, not its own `m_test`.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO,
            "m/ext_test.go": (
                'package m_test\n\nimport "proj/m"\n\n'
                "func TestExt() int { return m.NewBox().Bump() }\n"
            ),
            "m/dot_test.go": (
                'package m_test\n\nimport . "proj/m"\n\n'
                "func TestDot() int { return NewBox().With(1).Bump() }\n"
            ),
        },
    )
    named = _calls(store, "proj.m.ext_test.TestExt")
    assert named.get("proj.m.box.Box.Bump") == EXACT, named
    dotted = _calls(store, "proj.m.dot_test.TestDot")
    assert dotted.get("proj.m.box.Box.With") == EXACT, dotted
    assert dotted.get("proj.m.box.Box.Bump") == EXACT, dotted


def test_a_locally_bound_root_types_nothing_from_the_package_function(
    tmp_path: Path,
) -> None:
    # A local closure or a parameter named NewBox shadows the package's
    # NewBox, so `NewBox()` is not the package function and its `*Box` result
    # types nothing. A binding in a block that does not enclose the call
    # shadows nothing.
    store = _project(
        tmp_path,
        {
            "m/box.go": BOX_GO,
            "m/use.go": (
                "package m\n\n"
                "func viaClosure() int {\n"
                "\tNewBox := func() *Other { return &Other{} }\n"
                "\treturn NewBox().Bump()\n"
                "}\n\n"
                "func viaParam(NewBox func() *Other) int { return NewBox().Bump() }\n\n"
                "func outOfScope(ok bool) int {\n"
                "\tif ok {\n"
                "\t\tNewBox := func() *Other { return &Other{} }\n"
                "\t\t_ = NewBox\n"
                "\t}\n"
                "\treturn NewBox().Bump()\n"
                "}\n"
            ),
        },
    )
    for caller in ("proj.m.use.viaClosure", "proj.m.use.viaParam"):
        calls = _calls(store, caller)
        assert "proj.m.box.Box.Bump" not in calls, (caller, calls)
    scoped = _calls(store, "proj.m.use.outOfScope")
    assert scoped.get("proj.m.box.Box.Bump") == EXACT, scoped


def test_a_local_named_like_an_import_is_the_local(tmp_path: Path) -> None:
    # The parameter `box` shadows the import `box`: `box.New()` is Maker's
    # New, whose `*Other` result is what Bump is called on.
    store = _project(
        tmp_path,
        {
            "box/box.go": (
                "package box\n\ntype Box struct{}\n\n"
                "func (b *Box) Bump() int { return 1 }\n\n"
                "func New() *Box { return &Box{} }\n"
            ),
            "m/box.go": BOX_GO,
            "m/use.go": (
                'package m\n\nimport "proj/box"\n\n'
                "type Maker struct{}\n\n"
                "func (k *Maker) New() *Other { return &Other{} }\n\n"
                "var _ = box.New\n\n"
                "func viaLocal(box *Maker) int { return box.New().Bump() }\n"
            ),
        },
    )
    calls = _calls(store, "proj.m.use.viaLocal")
    assert "proj.box.box.Box.Bump" not in calls, calls
    assert calls.get("proj.m.box.Other.Bump") == EXACT, calls
