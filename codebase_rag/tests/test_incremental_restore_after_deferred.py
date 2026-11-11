"""Restored inbound edges reach targets registered after Pass 2 (issue #3270).

An incremental run captures the edges INTO each re-indexed file, deletes and
re-parses the file, and restores them. The restore ran at the end of Pass 2,
before the deferred stages register a Go receiver method, so every captured
edge into one was dropped as a target the re-index "did not recreate". A
comment edit to `util.go` re-parses `box.go` (Box.Size calls clamp), and the
unchanged `cmd/app/main.go`'s `main -> Box.Size` was gone for good: main.go is
not re-parsed (dependents go one level deep, and a sync re-parses only the
edited package's Go files, issue #2483), so only the restore could keep it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_file
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import function_ingest
from codebase_rag.parsers.frontends import (
    EMITTING_FRONTENDS,
    FrontendEmitContext,
    FrontendEmitResult,
    FrontendPhase,
)
from codebase_rag.tests.test_incremental_inbound_deferred_targets import (
    Snapshot,
    _calls,
    _snapshot,
)
from evals.cgr_graph import _StatefulIngestor

# The caller sits in another package: a sync re-parses every Go file of an
# edited package (issue #2483), which would rebuild a same-package caller's
# edge without the restore.
_GO = {
    "go.mod": "module example.com/split\n\ngo 1.21\n",
    "util.go": (
        "package split\n\nfunc clamp(n int) int {\n\tif n < 0 {\n\t\treturn 0\n\t}\n"
        "\treturn n\n}\n"
    ),
    "box.go": (
        "package split\n\ntype Box struct{ N int }\n\n"
        "func (b *Box) Size() int { return clamp(b.N) }\n"
    ),
    "cmd/app/main.go": (
        'package main\n\nimport (\n\t"fmt"\n\n\t"example.com/split"\n)\n\n'
        "func main() {\n\tb := &split.Box{N: 1}\n\tfmt.Println(b.Size())\n}\n"
    ),
}
_MAIN_TO_SIZE = ("proj.cmd.app.main.main", "proj.box.Box.Size")
# box.go also holds a plain function, registered during Pass 2, so its
# inbound edge never depended on the deferred Go stages.
_GO_WITH_CONSTRUCTOR = {
    **_GO,
    "box.go": (
        "package split\n\ntype Box struct{ N int }\n\n"
        "func NewBox() *Box { return &Box{N: clamp(1)} }\n\n"
        "func (b *Box) Size() int { return clamp(b.N) }\n"
    ),
    "cmd/app/main.go": (
        'package main\n\nimport (\n\t"fmt"\n\n\t"example.com/split"\n)\n\n'
        "func main() {\n\tb := split.NewBox()\n\tfmt.Println(b.Size())\n}\n"
    ),
}
_MAIN_TO_NEW_BOX = ("proj.cmd.app.main.main", "proj.box.NewBox")
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
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(path.read_text(encoding="utf-8") + f"{marker} edited\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


def _fresh_and_incremental(
    temp_repo: Path, files: dict[str, str], edited: str, marker: str
) -> tuple[Snapshot, Snapshot]:
    root = temp_repo / "proj"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
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
    # box.go was re-parsed as a dependent of util.go; cmd/app/main.go was not.
    assert _MAIN_TO_SIZE in _calls(after), sorted(_calls(after))
    assert after == clean


def test_the_python_layout_still_keeps_its_inbound_call(temp_repo: Path) -> None:
    # Negative control: a Python method is registered during Pass 2, so the
    # restore always reached it; moving the restore must not lose it.
    clean, after = _fresh_and_incremental(temp_repo, _PY, "box.py", "#")

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
    # box.py re-parses; main.py does not (box.py's interface is unchanged), so
    # only the restore can bring its edge into Box.size back.
    _edit_after_cache(root / "box.py", root, "#")

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
    # box.py re-parses cleanly and needs main.py's edge restored; util.py,
    # edited too, is the one that fails.
    _edit_after_cache(root / "box.py", root, "#")
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


def test_a_run_failing_after_pass_2_bookkeeping_still_restores_the_inbound_edges(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: the cache stash that closes Pass 2 runs after the captured
    # edges are held for the deferred restore. A failure there (the parser
    # fingerprint reads files) must not take them with it either.
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in _PY.items():
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    _edit_after_cache(root / "box.py", root, "#")

    def fail(_self: GraphUpdater, *_args: object) -> None:
        raise OSError("fingerprint input unreadable")

    monkeypatch.setattr(GraphUpdater, "_stash_parser_fingerprint", fail)
    with pytest.raises(OSError, match="fingerprint input unreadable"):
        _index(store, root, force=False)

    assert _PY_MAIN_TO_SIZE in _calls(_snapshot(store))


class _BrokenEmitter:
    """An after-definitions emitting frontend whose emit raises."""

    language = cs.SupportedLanguage.RUST
    phase = FrontendPhase.AFTER_DEFINITIONS

    def available(self) -> bool:
        return True

    def applies(self, repo_path: Path) -> bool:
        return True

    def emit(self, ctx: FrontendEmitContext) -> FrontendEmitResult:
        raise RuntimeError("stage failed")


def _break_an_after_definitions_emitter(patch: pytest.MonkeyPatch) -> None:
    # Inside the deferred stages, ahead of the Go receiver methods.
    patched = dict(EMITTING_FRONTENDS)
    patched[_BrokenEmitter.language] = _BrokenEmitter()
    patch.setattr("codebase_rag.graph_updater.EMITTING_FRONTENDS", patched)


def _break_the_java_frontend(patch: pytest.MonkeyPatch) -> None:
    # Between Pass 2 and the deferred stages.
    def fail(_self: GraphUpdater) -> None:
        raise RuntimeError("stage failed")

    patch.setattr(GraphUpdater, "_run_java_frontend", fail)


@pytest.mark.parametrize(
    "break_stage",
    [_break_an_after_definitions_emitter, _break_the_java_frontend],
    ids=["after-definitions-emitter", "stage-before-the-deferred-ones"],
)
def test_a_run_stopped_before_the_go_methods_register_keeps_the_edges_into_them(
    temp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    break_stage: Callable[[pytest.MonkeyPatch], None],
) -> None:
    # Negative: a Go receiver method joins the registry only in the deferred
    # stages, so a run stopped before them dropped every held edge into one,
    # and for good: the next run re-parses box.go again, not cmd/app/main.go,
    # and the edge is no longer in the graph to capture.
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")
    root = temp_repo / "proj"
    for rel, text in _GO.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    clean = _snapshot(store)
    _edit_after_cache(root / "util.go", root, "//")
    with monkeypatch.context() as patch:
        break_stage(patch)
        with pytest.raises(RuntimeError, match="stage failed"):
            _index(store, root, force=False)

    assert _MAIN_TO_SIZE in _calls(_snapshot(store))
    # The next run completes what the stopped one left (box.go's own calls)
    # and keeps the edge, with nothing extra.
    _index(store, root, force=False)
    assert _snapshot(store) == clean


def test_a_failing_go_method_registration_still_restores_the_other_edges(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: the restore registers the pending Go receiver methods first.
    # When that raises (it fails in the deferred stage, and again on the way
    # out), the held edges into everything else must still come back, as
    # they did when the restore ran at the end of Pass 2: the next run
    # cannot capture them again, and cmd/app/main.go is not re-parsed.
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")
    root = temp_repo / "proj"
    for rel, text in _GO_WITH_CONSTRUCTOR.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    assert _MAIN_TO_NEW_BOX in _calls(_snapshot(store)), "fixture must produce it"
    _edit_after_cache(root / "util.go", root, "//")
    original = function_ingest.ingest_method

    def fail_go(*args: object, **kwargs: object) -> str | None:
        if kwargs.get("language") == cs.SupportedLanguage.GO:
            raise RuntimeError("go method ingestion failed")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(function_ingest, "ingest_method", fail_go)
        with pytest.raises(RuntimeError, match="go method ingestion failed"):
            _index(store, root, force=False)

    assert _MAIN_TO_NEW_BOX in _calls(_snapshot(store))
    _index(store, root, force=False)
    assert _MAIN_TO_NEW_BOX in _calls(_snapshot(store))
