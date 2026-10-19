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
from codebase_rag.checkout_state import state_file
from codebase_rag.graph_updater import GraphUpdater, ReingestAborted
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_incremental_inbound_deferred_targets import _snapshot
from codebase_rag.types_defs import PropertyDict, ResultRow
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


class _MemberLookupOutage(_StatefulIngestor):
    """Fails the foreign-member lookup once `down` is set, answers the rest."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if self.down and query == cs.CYPHER_FOREIGN_MEMBER_PATHS:
            raise RuntimeError("foreign-member lookup outage")
        return super().fetch_all(query, params)


def _write(root: Path, files: dict[str, str]) -> None:
    root.mkdir()
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")


def _updater(store: _StatefulIngestor, root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def _index(store: _StatefulIngestor, root: Path, force: bool) -> None:
    _updater(store, root).run(force=force)


def _edit_after_cache(path: Path, root: Path, marker: str) -> None:
    # A comment changes the hash, not the AST. The mtime lands past the hash
    # cache's so a coarse-timestamp filesystem cannot skip the file unhashed.
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
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


def _parsed_on_sync(
    root: Path, edited: str, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    # Indexes `root` clean, edits `edited`, and returns the file names the
    # incremental run re-parses.
    store = _StatefulIngestor()
    _index(store, root, force=True)
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
    _edit_after_cache(root / edited, root, "//")
    _index(store, root, force=False)
    return sorted(parsed)


# The negative bounds are pinned on C++: a Go package's files re-parse
# together on a sync (issue #2483), so there the member lookup never decides
# which files join.
@pytest.fixture
def cpp_root(temp_repo: Path) -> Path:
    if cs.SupportedLanguage.CPP not in load_parsers()[0]:
        pytest.skip("cpp parser not available")
    return temp_repo / "proj"


def test_only_the_files_attaching_members_join_the_reparse(
    cpp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: box.cpp joins because it attaches grow to Box; main.cpp (a
    # caller of box.h) and the unrelated other.cpp stay as they are.
    _write(
        cpp_root,
        {
            **_CPP,
            "main.cpp": (
                '#include "box.h"\nint main() { Box b; return b.size() + b.grow(); }\n'
            ),
            "other.cpp": "int other() { return 2; }\n",
        },
    )

    assert _parsed_on_sync(cpp_root, "util.h", monkeypatch) == [
        "box.cpp",
        "box.h",
        "util.h",
    ]


def test_a_type_whose_methods_are_all_its_own_reparses_alone(
    cpp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: with no member defined elsewhere, nothing else joins.
    _write(
        cpp_root,
        {
            "util.h": _CPP["util.h"],
            "box.h": (
                '#include "util.h"\nclass Box {\n public:\n  int n = 1;\n'
                "  int size() const { return clamp(n); }\n};\n"
            ),
            "main.cpp": '#include "box.h"\nint main() { Box b; return b.size(); }\n',
        },
    )

    assert _parsed_on_sync(cpp_root, "util.h", monkeypatch) == ["box.h", "util.h"]


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


def test_a_scoped_reingest_keeps_an_out_of_line_definition(cpp_root: Path) -> None:
    # The watcher and the MCP tool re-ingest through `reingest`, whose
    # dependent walk follows edges only: box.h joined as util.h's dependent
    # and its delete took box.cpp's grow, which nothing re-created.
    _write(cpp_root, _CPP)
    store = _StatefulIngestor()
    _index(store, cpp_root, force=True)
    clean = store.nodes[(cs.NodeLabel.METHOD.value, _CPP_GROW)]

    _edit_after_cache(cpp_root / "util.h", cpp_root, "//")
    report = _updater(store, cpp_root).reingest(["util.h"])

    assert "box.cpp" in report.affected, report
    after = store.nodes.get((cs.NodeLabel.METHOD.value, _CPP_GROW))
    assert after is not None, "the re-ingest deleted Box::grow"
    assert after.get(cs.KEY_PATH) == "box.cpp", after
    assert after.get(cs.KEY_START_LINE) == clean.get(cs.KEY_START_LINE)


def test_a_single_file_run_keeps_an_out_of_line_definition(cpp_root: Path) -> None:
    # A single-file run walks only its target, so the lookup named box.cpp
    # but found no walked file to queue: box.h's delete took grow, and
    # box.cpp's unchanged hash kept every later sync from re-creating it.
    _write(cpp_root, _CPP)
    store = _StatefulIngestor()
    _index(store, cpp_root, force=True)
    clean = _snapshot(store)

    _edit_after_cache(cpp_root / "box.h", cpp_root, "//")
    _index(store, cpp_root / "box.h", force=False)

    assert _snapshot(store) == clean


def test_a_single_file_run_keeps_a_method_declared_in_another_file(
    go_root: Path,
) -> None:
    # box_extra.go joins for Grow, then tag_methods.go for the Tag.Label
    # that box_extra.go's own module holds.
    store = _StatefulIngestor()
    _index(store, go_root, force=True)
    clean = _facts(store, _GROW)

    _edit_after_cache(go_root / "box.go", go_root, "//")
    _index(store, go_root / "box.go", force=False)

    assert _facts(store, _GROW) == clean
    assert _LABEL in _snapshot(store)[0]


def test_an_incremental_sync_aborts_when_the_member_lookup_fails(
    cpp_root: Path,
) -> None:
    # The lookup decides which files must re-parse before the delete runs;
    # read as "none" it let box.h's delete take grow with no file left to
    # re-create it, and the hash commit then marked the run complete.
    _write(cpp_root, _CPP)
    store = _MemberLookupOutage()
    _index(store, cpp_root, force=True)
    _edit_after_cache(cpp_root / "util.h", cpp_root, "//")
    store.down = True

    with pytest.raises(RuntimeError, match="foreign-member lookup outage"):
        _index(store, cpp_root, force=False)
    assert (cs.NodeLabel.METHOD.value, _CPP_GROW) in store.nodes


def test_a_scoped_reingest_aborts_when_the_member_lookup_fails(
    cpp_root: Path,
) -> None:
    _write(cpp_root, _CPP)
    store = _MemberLookupOutage()
    _index(store, cpp_root, force=True)
    _edit_after_cache(cpp_root / "util.h", cpp_root, "//")
    store.down = True
    before = _snapshot(store)
    updater = _updater(store, cpp_root)

    with pytest.raises(ReingestAborted, match="foreign-member lookup outage"):
        updater.reingest(["util.h"])
    assert _snapshot(store) == before


def test_a_full_build_goes_on_when_the_member_lookup_fails(cpp_root: Path) -> None:
    # A full build re-parses every file, so it needs no lookup to keep grow.
    _write(cpp_root, _CPP)
    store = _MemberLookupOutage()
    store.down = True
    _index(store, cpp_root, force=True)

    node = store.nodes[(cs.NodeLabel.METHOD.value, _CPP_GROW)]
    assert node.get(cs.KEY_PATH) == "box.cpp", node
