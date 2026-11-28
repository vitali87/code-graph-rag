"""A re-parsed Go caller still types locals from unchanged functions (#3272).

On Go's tree-sitter path (every `_test.go`, and any module go/types cannot
load), `b, _ := NewBox(2)` types `b` from `go_function_return_types`, and
`NewBox(2).Size()` chains through `method_return_types`. Both maps were
filled only while `NewBox`'s own file was parsed. An incremental run that
re-parsed the caller alone left them empty for it, so `b.Size()` bound by
name to another type's `Size`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_file
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_incremental_interface_skip import _diff, _snapshot
from evals.cgr_graph import _StatefulIngestor

_GO = {
    "go.mod": "module example.com/ret\n\ngo 1.21\n",
    "types.go": (
        "package main\n\ntype Box struct{ n int }\n\n"
        "func (b *Box) Size() int { return b.n }\n\n"
        "func (b *Box) Self() *Box { return b }\n\n"
        "type Bag struct{}\n\nfunc (g *Bag) Size() int { return 0 }\n\n"
        "func (g *Bag) Self() *Bag { return g }\n"
    ),
    "helpers.go": (
        "package main\n\n"
        "func NewBox(n int) (*Box, error) { return &Box{n: n}, nil }\n\n"
        "func MakeBox(n int) *Box { return &Box{n: n} }\n\n"
        "func Named(n int) (b *Box, err error) { return &Box{n: n}, nil }\n"
    ),
    "main.go": "package main\n\nfunc main() {}\n",
    "box_test.go": (
        'package main\n\nimport "testing"\n\n'
        "func TestPair(t *testing.T) {\n\tb, _ := NewBox(2)\n\t_ = b.Size()\n}\n\n"
        "func TestChain(t *testing.T) {\n\t_ = MakeBox(2).Size()\n}\n\n"
        "func TestNamed(t *testing.T) {\n\tb, _ := Named(2)\n\t_ = b.Size()\n}\n\n"
        "func TestLiteral(t *testing.T) {\n\tb := &Box{n: 2}\n\t_ = b.Size()\n}\n\n"
        # A method's result: Box.Self in the unchanged types.go.
        "func TestMethodChain(t *testing.T) {\n\tb := &Box{n: 2}\n"
        "\t_ = b.Self().Size()\n}\n"
    ),
}
_BOX_SIZE = "proj.types.Box.Size"


def _index(store: _StatefulIngestor, root: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _size_calls(store: _StatefulIngestor) -> dict[str, set[str]]:
    # caller -> the Size methods it calls.
    out: dict[str, set[str]] = {}
    for from_label, from_qn, rel, _to_label, to_qn in store.edges:
        del from_label
        if rel == cs.RelationshipType.CALLS.value and str(to_qn).endswith(".Size"):
            out.setdefault(str(from_qn).rsplit(".", 1)[-1], set()).add(str(to_qn))
    return out


@pytest.fixture
def go_root(temp_repo: Path) -> Path:
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in _GO.items():
        (root / rel).write_text(text, encoding="utf-8")
    return root


def test_a_reparsed_test_file_still_types_locals_from_unchanged_helpers(
    go_root: Path,
) -> None:
    store = _StatefulIngestor()
    _index(store, go_root, force=True)
    fresh = _size_calls(store)
    expected = {
        name: {_BOX_SIZE}
        for name in (
            "TestPair",
            "TestChain",
            "TestNamed",
            "TestLiteral",
            "TestMethodChain",
        )
    }
    assert fresh == expected, fresh

    # A comment changes the hash, not the AST; only box_test.go re-parses.
    test_file = go_root / "box_test.go"
    cache_mtime = state_file(go_root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    test_file.write_text(test_file.read_text(encoding="utf-8") + "// edited\n")
    os.utime(test_file, (cache_mtime + 1, cache_mtime + 1))
    _index(store, go_root, force=False)

    assert _size_calls(store) == expected


def test_a_helper_whose_result_changed_types_its_callers_anew(go_root: Path) -> None:
    # Negative: the stored names are a fallback for files the run does not
    # parse. A helper re-parsed with a new result type is read as written
    # now, never as the graph last stored it.
    store = _StatefulIngestor()
    _index(store, go_root, force=True)
    helpers = go_root / "helpers.go"
    cache_mtime = state_file(go_root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    helpers.write_text(
        helpers.read_text(encoding="utf-8")
        .replace(
            "(*Box, error) { return &Box{n: n}, nil }",
            "(*Bag, error) { return &Bag{}, nil }",
        )
        .replace("*Box { return &Box{n: n} }", "*Bag { return &Bag{} }"),
        encoding="utf-8",
    )
    os.utime(helpers, (cache_mtime + 1, cache_mtime + 1))
    _index(store, go_root, force=False)

    calls = _size_calls(store)
    bag = "proj.types.Bag.Size"
    assert calls["TestPair"] == {bag}, calls
    assert calls["TestChain"] == {bag}, calls
    assert calls["TestNamed"] == {_BOX_SIZE}, calls


_CROSS_PACKAGE = {
    "go.mod": "module example.com/ret\n\ngo 1.21\n",
    "lib/types.go": (
        "package lib\n\ntype Box struct{ n int }\n\n"
        "func (b *Box) Size() int { return b.n }\n\n"
        "func (b *Box) Self() *Box { return b }\n"
    ),
    "lib/helpers.go": (
        "package lib\n\nfunc MakeBox(n int) *Box { return &Box{n: n} }\n"
    ),
    "other/bag.go": "package other\n\ntype Bag struct{}\n\nfunc (g *Bag) Size() int { return 0 }\n",
    "app/app.go": "package app\n",
    "app/app_test.go": (
        'package app\n\nimport (\n\t"testing"\n\n\t"example.com/ret/lib"\n)\n\n'
        "func TestChain(t *testing.T) {\n\t_ = lib.MakeBox(2).Size()\n}\n\n"
        "func TestMethodChain(t *testing.T) {\n\tb := lib.MakeBox(2)\n"
        "\t_ = b.Self().Size()\n}\n"
    ),
}


def test_a_caller_in_another_package_keeps_its_chained_result_types(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An edit re-parses the whole package it is in, so the unchanged
    # helpers that need reading back sit in another one. Pinned to the
    # tree-sitter path, where these maps type the receiver: go/types, when
    # its helper builds, types the calls itself.
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = temp_repo / "proj"
    for rel, text in _CROSS_PACKAGE.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    expected = {
        name: {"proj.lib.types.Box.Size"} for name in ("TestChain", "TestMethodChain")
    }
    assert _size_calls(store) == expected

    test_file = root / "app" / "app_test.go"
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    test_file.write_text(test_file.read_text(encoding="utf-8") + "// edited\n")
    os.utime(test_file, (cache_mtime + 1, cache_mtime + 1))
    _index(store, root, force=False)

    assert _size_calls(store) == expected


_BUILD_VARIANTS = {
    "go.mod": "module example.com/ret\n\ngo 1.21\n",
    "lib/types.go": (
        "package lib\n\ntype Box struct{ n int }\n\n"
        "func (b *Box) Size() int { return b.n }\n\n"
        "type Bag struct{}\n\nfunc (g *Bag) Size() int { return 0 }\n\n"
        "func MakeBox(n int) *Box { return &Box{n: n} }\n"
    ),
    "lib/self_linux.go": (
        "//go:build linux\n\npackage lib\n\nfunc (b *Box) Self() *Box { return b }\n"
    ),
    "lib/self_windows.go": (
        "//go:build windows\n\npackage lib\n\n"
        "func (b *Box) Self() *Bag { return &Bag{} }\n"
    ),
    "app/app.go": "package app\n",
    "app/app_test.go": (
        'package app\n\nimport (\n\t"testing"\n\n\t"example.com/ret/lib"\n)\n\n'
        "func TestVariant(t *testing.T) {\n\t_ = lib.MakeBox(2).Self().Size()\n}\n"
    ),
}


def test_build_variants_of_a_method_type_its_chain_as_a_fresh_index_does(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each build file's `Box.Self` is its own node (`Self`, `Self@5`), but
    # parsing records their results under the one natural qn, each over the
    # last in registration order. Read back node by node, the natural qn
    # kept the first file's result where a clean index has the last's.
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = temp_repo / "proj"
    for rel, text in _BUILD_VARIANTS.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    fresh = _size_calls(store)
    assert fresh.get("TestVariant"), fresh

    test_file = root / "app" / "app_test.go"
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    test_file.write_text(test_file.read_text(encoding="utf-8") + "// edited\n")
    os.utime(test_file, (cache_mtime + 1, cache_mtime + 1))
    _index(store, root, force=False)

    assert _size_calls(store) == fresh


_INTERFACE_CHAIN = {
    "go.mod": "module example.com/ret\n\ngo 1.21\n",
    "ssa/ssa.go": (
        "package ssa\n\ntype CallCommon struct{}\n\n"
        "func (c *CallCommon) StaticCallee() int { return 1 }\n\n"
        "type CallInstruction interface {\n\tCommon() *CallCommon\n}\n"
    ),
    "util/util.go": "package util\n\nfunc Log() {}\n",
    "app/app.go": (
        'package app\n\nimport (\n\t"example.com/ret/ssa"\n\t"example.com/ret/util"\n)\n\n'
        "func run(i ssa.CallInstruction) int {\n\tutil.Log()\n"
        "\treturn i.Common().StaticCallee()\n}\n"
    ),
}


def test_a_chain_through_an_interface_method_resolves_as_a_fresh_index_does(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An interface method's node stores its result for the read-back, but
    # parsing records none for it, so a clean index leaves
    # `i.Common().StaticCallee()` unresolved. Read back for ssa.go, which the
    # sync did not re-parse, it typed the chain, and the re-parsed app.go
    # gained a CALLS edge to `CallCommon.StaticCallee` that a fresh index of
    # the same tree does not have (issue #3781, golang/tools'
    # cmd/deadcode isStaticCall).
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = temp_repo / "proj"
    for rel, text in _INTERFACE_CHAIN.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)

    util_file = root / "util" / "util.go"
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    util_file.write_text(util_file.read_text(encoding="utf-8") + "// edited\n")
    os.utime(util_file, (cache_mtime + 1, cache_mtime + 1))
    _index(store, root, force=False)
    clean = _StatefulIngestor()
    _index(clean, root, force=True)

    synced, fresh = _snapshot(store), _snapshot(clean)
    assert synced == fresh, _diff(synced, fresh)


_IMPORTED_RESULT = {
    "go.mod": "module example.com/ret\n\ngo 1.23\n",
    "inspector/cursor.go": (
        "package inspector\n\ntype Cursor struct{ n int }\n\n"
        "func (c Cursor) Preorder(types ...int) int { return c.n }\n"
    ),
    "parsego/file.go": (
        'package parsego\n\nimport "example.com/ret/inspector"\n\n'
        "type File struct{ cursor inspector.Cursor }\n\n"
        "func (pgf *File) Cursor() inspector.Cursor { return pgf.cursor }\n"
    ),
    "server/helper.go": (
        'package server\n\nimport "example.com/ret/inspector"\n\n'
        "func first() inspector.Cursor { return inspector.Cursor{} }\n"
    ),
    "util/util.go": "package util\n\nfunc Log() {}\n",
    "server/link.go": (
        "package server\n\nimport (\n"
        '\t"example.com/ret/parsego"\n\t"example.com/ret/util"\n)\n\n'
        "func links(pgf *parsego.File) int {\n\tutil.Log()\n"
        "\treturn pgf.Cursor().Preorder(1) + first().Preorder(2)\n}\n"
    ),
}


def test_a_chain_through_an_unchanged_files_imported_result_resolves(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `pgf.Cursor()` returns `inspector.Cursor`, a name read in the import
    # map of parsego/file.go, the file declaring Cursor(); `first()` returns
    # one read in the map of server/helper.go, beside the caller. A sync
    # re-parsing link.go alone restored neither map, since a Go import names
    # a package and the import walk found no file behind it, so both
    # `Preorder` edges were missing where a fresh index has them (issue
    # #3781, golang/tools' gopls/internal/server/link.go).
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = temp_repo / "proj"
    for rel, text in _IMPORTED_RESULT.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)

    util_file = root / "util" / "util.go"
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    util_file.write_text(util_file.read_text(encoding="utf-8") + "// edited\n")
    os.utime(util_file, (cache_mtime + 1, cache_mtime + 1))
    _index(store, root, force=False)
    clean = _StatefulIngestor()
    _index(clean, root, force=True)

    synced, fresh = _snapshot(store), _snapshot(clean)
    assert synced == fresh, _diff(synced, fresh)
