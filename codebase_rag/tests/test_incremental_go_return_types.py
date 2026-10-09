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
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
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
    cache_mtime = (go_root / cs.HASH_CACHE_FILENAME).stat().st_mtime
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
    cache_mtime = (go_root / cs.HASH_CACHE_FILENAME).stat().st_mtime
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
