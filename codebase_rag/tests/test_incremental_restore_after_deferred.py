"""Restored inbound edges reach targets registered after Pass 2 (issue #3270).

An incremental run captures the edges INTO each re-indexed file, deletes and
re-parses the file, and restores them. The restore ran at the end of Pass 2,
before the deferred stages register a Go receiver method, so every captured
edge into one was dropped as a target the re-index "did not recreate". A
comment edit to `util.go` re-parses `box.go` (Box.Size calls clamp), and the
unchanged `main.go`'s `main -> Box.Size` was gone for good: main.go is not
re-parsed (dependents go one level deep), so only the restore could keep it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_incremental_inbound_deferred_targets import (
    Snapshot,
    _calls,
    _snapshot,
)
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
    "main.go": (
        'package main\n\nimport "fmt"\n\nfunc main() {\n\tb := &Box{n: 1}\n'
        "\tfmt.Println(b.Size())\n}\n"
    ),
}
_MAIN_TO_SIZE = ("proj.main.main", "proj.box.Box.Size")
_SIZE_TO_CLAMP = ("proj.box.Box.Size", "proj.util.clamp")

_PY = {
    "util.py": "def clamp(n):\n    return max(n, 0)\n",
    "box.py": (
        "from util import clamp\n\n\nclass Box:\n    def __init__(self):\n"
        "        self.n = 1\n\n    def size(self):\n        return clamp(self.n)\n"
    ),
    "main.py": "from box import Box\n\n\ndef main():\n    return Box().size()\n",
}
_PY_MAIN_TO_SIZE = ("proj.main.main", "proj.box.Box.size")


def _index(store: _StatefulIngestor, root: Path, force: bool) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=force)
    return updater


def _edit_after_cache(path: Path, root: Path, marker: str) -> None:
    # A comment changes the hash, not the AST. The mtime lands past the hash
    # cache's so a coarse-timestamp filesystem cannot skip the file unhashed.
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(path.read_text(encoding="utf-8") + f"{marker} edited\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


def _fresh_and_incremental(
    temp_repo: Path, files: dict[str, str], edited: str, marker: str
) -> tuple[Snapshot, Snapshot]:
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    clean = _snapshot(store)
    _edit_after_cache(root / edited, root, marker)
    _index(store, root, force=False)
    return clean, _snapshot(store)


def test_an_unchanged_files_call_into_a_reindexed_go_method_survives(
    temp_repo: Path,
) -> None:
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")
    clean, after = _fresh_and_incremental(temp_repo, _GO, "util.go", "//")

    assert _MAIN_TO_SIZE in _calls(clean), "fixture must produce the call"
    assert _SIZE_TO_CLAMP in _calls(clean)
    # box.go was re-parsed as a dependent of util.go; main.go was not.
    assert _MAIN_TO_SIZE in _calls(after), sorted(_calls(after))
    assert after == clean


def test_the_python_layout_still_keeps_its_inbound_call(temp_repo: Path) -> None:
    # Negative control: a Python method is registered during Pass 2, so the
    # restore always reached it; moving the restore must not lose it.
    clean, after = _fresh_and_incremental(temp_repo, _PY, "util.py", "#")

    assert _PY_MAIN_TO_SIZE in _calls(clean), "fixture must produce the call"
    assert _PY_MAIN_TO_SIZE in _calls(after)
    assert after == clean


def test_a_run_stopped_after_pass_2_still_restores_the_inbound_edges(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: the restore moved later, but a stage failing in between must
    # not take the captured edges with it. The next run cannot capture them
    # again, since their targets' subtrees are already deleted.
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in _PY.items():
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    # box.py re-parses as a dependent; main.py does not, so only the restore
    # can bring its edge into Box.size back.
    _edit_after_cache(root / "util.py", root, "#")

    def fail(_self: GraphUpdater) -> None:
        raise RuntimeError("frontend failed")

    monkeypatch.setattr(GraphUpdater, "_run_java_frontend", fail)
    with pytest.raises(RuntimeError, match="frontend failed"):
        _index(store, root, force=False)

    assert _PY_MAIN_TO_SIZE in _calls(_snapshot(store))


def test_a_file_failing_in_pass_2_still_restores_the_inbound_edges(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: a re-parse failure re-raises out of Pass 2 before `run()`
    # reaches the deferred stages, so the restore happens on the way out.
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in _PY.items():
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    _edit_after_cache(root / "util.py", root, "#")
    original = GraphUpdater._process_single_file

    def fail_util(
        self: GraphUpdater,
        filepath: Path,
        file_bytes: bytes | None = None,
        pre_parsed: tuple[Node, dict[str, list] | None] | None = None,
    ) -> None:
        if filepath.name == "util.py":
            raise RuntimeError("util.py failed")
        original(self, filepath, file_bytes=file_bytes, pre_parsed=pre_parsed)

    monkeypatch.setattr(GraphUpdater, "_process_single_file", fail_util)
    with pytest.raises(RuntimeError, match="util.py failed"):
        _index(store, root, force=False)

    assert _PY_MAIN_TO_SIZE in _calls(_snapshot(store))
