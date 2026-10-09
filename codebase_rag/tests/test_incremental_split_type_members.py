"""Re-parsing a type's file keeps the members other files attach to it (#3271).

A Go method keys under its receiver type's module whatever file declares
it, so `func (b *Box) Grow()` in box_extra.go hangs off box.go's `Box`.
An incremental run that re-parses box.go (as a dependent of an edited
util.go) deleted box.go's whole Module subtree, `Grow` included, and
re-created only what box.go declares: box_extra.go was not re-parsed, so
the method stayed gone until its own file changed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_incremental_inbound_deferred_targets import _snapshot
from evals.cgr_graph import _StatefulIngestor

_GO = {
    "go.mod": "module example.com/split\n\ngo 1.21\n",
    "util.go": (
        "package main\n\nfunc clamp(n int) int {\n\tif n < 0 {\n\t\treturn 0\n\t}\n"
        "\treturn n\n}\n"
    ),
    "box.go": (
        "package main\n\ntype Box struct{ n int }\n\n"
        "func (b *Box) Size() int { return clamp(b.n) }\n"
    ),
    # box_extra.go attaches Grow to box.go's Box, and declares a type of its
    # own whose method lives in a third file: re-parsing box_extra.go takes
    # Tag.Label with it, so tag_methods.go must join in turn.
    "box_extra.go": (
        "package main\n\nfunc (b *Box) Grow() int { return double(b.n) }\n\n"
        "type Tag struct{}\n"
    ),
    "tag_methods.go": ('package main\n\nfunc (t Tag) Label() string { return "t" }\n'),
    "double.go": "package main\n\nfunc double(n int) int { return n * 2 }\n",
    "other.go": (
        "package main\n\ntype Other struct{}\n\nfunc (o Other) Name() string "
        '{ return "o" }\n'
    ),
    "main.go": (
        'package main\n\nimport "fmt"\n\nfunc main() {\n\tb := &Box{n: 1}\n'
        "\tfmt.Println(b.Size(), b.Grow())\n}\n"
    ),
}
_GROW = "proj.box.Box.Grow"
_LABEL = (cs.NodeLabel.METHOD.value, "proj.box_extra.Tag.Label")
_GROW_NODE = (cs.NodeLabel.METHOD.value, _GROW)
_GROW_FACTS = {
    (cs.NodeLabel.CLASS.value, "proj.box.Box", "DEFINES_METHOD", *_GROW_NODE),
    (*_GROW_NODE, "CALLS", cs.NodeLabel.FUNCTION.value, "proj.double.double"),
}

# The C++ shape: a header-declared member defined out of line in its .cpp,
# the header re-parsed as a dependent of an edited header it includes.
_CPP = {
    "util.h": "inline int clamp(int n) { return n < 0 ? 0 : n; }\n",
    "box.h": (
        '#include "util.h"\nclass Box {\n public:\n  int n = 1;\n'
        "  int size() const { return clamp(n); }\n  int grow() const;\n};\n"
    ),
    "box.cpp": '#include "box.h"\nint Box::grow() const { return n + 1; }\n',
}
_CPP_GROW = "proj.box.h.Box.grow"


def _write(root: Path, files: dict[str, str]) -> None:
    root.mkdir()
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")


def _index(store: _StatefulIngestor, root: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _edit_after_cache(path: Path, root: Path, marker: str) -> None:
    # A comment changes the hash, not the AST. The mtime lands past the hash
    # cache's so a coarse-timestamp filesystem cannot skip the file unhashed.
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(path.read_text(encoding="utf-8") + f"{marker} edited\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


def _facts(store: _StatefulIngestor, qn: str) -> set[tuple[str, ...]]:
    # The node and every edge leaving it or its owner's link to it. Edges
    # INTO a re-indexed method from an unchanged file are #3270's restore.
    nodes, edges = _snapshot(store)
    facts: set[tuple[str, ...]] = {n for n in nodes if n[1] == qn}
    facts |= {e for e in edges if e[1] == qn or (e[4] == qn and e[2] != "CALLS")}
    return facts


@pytest.fixture
def go_root(temp_repo: Path) -> Path:
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")
    root = temp_repo / "proj"
    _write(root, _GO)
    return root


def test_a_method_declared_in_another_file_survives_its_types_reparse(
    go_root: Path,
) -> None:
    store = _StatefulIngestor()
    _index(store, go_root, force=True)
    clean = _facts(store, _GROW)
    assert {_GROW_NODE, *_GROW_FACTS} <= clean, "fixture must define Grow"

    # box.go re-parses as a dependent of util.go (Box.Size calls clamp).
    _edit_after_cache(go_root / "util.go", go_root, "//")
    _index(store, go_root, force=False)

    assert _facts(store, _GROW) == clean
    assert _LABEL in _snapshot(store)[0]


def test_only_the_files_attaching_members_join_the_reparse(
    go_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: box_extra.go joins because it attaches Grow to Box; main.go
    # (a caller of box.go) and the unrelated files stay as they are.
    store = _StatefulIngestor()
    _index(store, go_root, force=True)
    parsed: list[str] = []
    original = GraphUpdater._process_single_file

    def record(
        self: GraphUpdater,
        filepath: Path,
        file_bytes: bytes | None = None,
        pre_parsed: tuple[Node, dict[str, list] | None] | None = None,
    ) -> None:
        parsed.append(filepath.name)
        original(self, filepath, file_bytes=file_bytes, pre_parsed=pre_parsed)

    monkeypatch.setattr(GraphUpdater, "_process_single_file", record)
    _edit_after_cache(go_root / "util.go", go_root, "//")
    _index(store, go_root, force=False)

    assert sorted(parsed) == ["box.go", "box_extra.go", "tag_methods.go", "util.go"]


def test_a_type_whose_methods_are_all_its_own_reparses_alone(
    go_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: with no member declared elsewhere, nothing else joins.
    for rel in ("box_extra.go", "tag_methods.go"):
        (go_root / rel).unlink()
    (go_root / "main.go").write_text(
        _GO["main.go"].replace(", b.Grow()", ""), encoding="utf-8"
    )
    store = _StatefulIngestor()
    _index(store, go_root, force=True)
    parsed: list[str] = []
    original = GraphUpdater._process_single_file

    def record(
        self: GraphUpdater,
        filepath: Path,
        file_bytes: bytes | None = None,
        pre_parsed: tuple[Node, dict[str, list] | None] | None = None,
    ) -> None:
        parsed.append(filepath.name)
        original(self, filepath, file_bytes=file_bytes, pre_parsed=pre_parsed)

    monkeypatch.setattr(GraphUpdater, "_process_single_file", record)
    _edit_after_cache(go_root / "util.go", go_root, "//")
    _index(store, go_root, force=False)

    assert sorted(parsed) == ["box.go", "util.go"]


def test_a_cpp_out_of_line_definition_survives_its_headers_reparse(
    temp_repo: Path,
) -> None:
    if cs.SupportedLanguage.CPP not in load_parsers()[0]:
        pytest.skip("cpp parser not available")
    root = temp_repo / "proj"
    _write(root, _CPP)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    clean = store.nodes[(cs.NodeLabel.METHOD.value, _CPP_GROW)]
    assert clean.get(cs.KEY_PATH) == "box.cpp", clean

    # box.h re-parses as a dependent of util.h, which it includes.
    _edit_after_cache(root / "util.h", root, "//")
    _index(store, root, force=False)

    after = store.nodes[(cs.NodeLabel.METHOD.value, _CPP_GROW)]
    assert after.get(cs.KEY_PATH) == "box.cpp", after
    assert after.get(cs.KEY_START_LINE) == clean.get(cs.KEY_START_LINE)
