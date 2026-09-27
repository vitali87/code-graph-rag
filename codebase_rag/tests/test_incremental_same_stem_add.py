"""An added same-stem sibling on the batch path matches a clean index (#2022).

`util.c` added beside `util.h`, or `shape.cpp` beside `shape.h`, takes the
bare module qn a clean index gives the first file in walk order, and the
header moves to its suffixed qn. A fresh updater always got this right; one
held across runs (the watcher, the MCP server) kept the header's claim and
its definitions from the previous run, so the added file was suffixed and
its function marked a duplicate.
"""

from __future__ import annotations

import builtins
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.cpp_frontend import cpp_frontend_available
from codebase_rag.parsers.frontends import (
    EMITTING_FRONTENDS,
    FrontendEmitContext,
    FrontendEmitResult,
    FrontendPhase,
)
from codebase_rag.tests.test_graph_updater_incremental_rename import (
    InMemoryGraph,
    _make_updater,
)

_C = {
    "util.h": "int util(void);\n",
    "main.c": '#include "util.h"\nint main(void) { return util(); }\n',
}
_C_ADDED = ("util.c", '#include "util.h"\nint util(void) { return 1; }\n')
_CPP = {
    "shape.h": "namespace geo { class Shape { public: int area(); }; }\n",
    "use.cpp": '#include "shape.h"\nint use() { geo::Shape s; return s.area(); }\n',
}
_CPP_ADDED = ("shape.cpp", '#include "shape.h"\nint geo::Shape::area() { return 1; }\n')


def _write(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text)


@pytest.mark.parametrize(
    ("base", "added"), [(_C, _C_ADDED), (_CPP, _CPP_ADDED)], ids=["c", "cpp"]
)
@pytest.mark.parametrize("reuse", [False, True], ids=["fresh", "reused"])
def test_an_added_same_stem_sibling_matches_a_clean_index(
    tmp_path: Path,
    base: dict[str, str],
    added: tuple[str, str],
    reuse: bool,
) -> None:
    golden_root = tmp_path / "golden" / "proj"
    _write(golden_root, {**base, added[0]: added[1]})
    golden = InMemoryGraph()
    _make_updater(golden_root, golden).run(force=True)

    incr_root = tmp_path / "incr" / "proj"
    _write(incr_root, base)
    incr = InMemoryGraph()
    updater = _make_updater(incr_root, incr)
    updater.run(force=True)
    (incr_root / added[0]).write_text(added[1])
    (updater if reuse else _make_updater(incr_root, incr)).run(force=False)

    (golden_nodes, golden_rels), (nodes, rels) = golden.snapshot(), incr.snapshot()
    assert golden_nodes, "the clean index is empty, so the comparison proves nothing"
    assert nodes == golden_nodes, {
        "extra": sorted(map(str, nodes - golden_nodes)),
        "missing": sorted(map(str, golden_nodes - nodes)),
    }
    assert rels == golden_rels, {
        "extra": sorted(map(str, rels - golden_rels)),
        "missing": sorted(map(str, golden_rels - rels)),
    }


def _compile_commands(root: Path, sources: list[str]) -> None:
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "arguments": ["c++", "-std=c++17", str(root / src)],
                    "file": str(root / src),
                }
                for src in sources
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.skipif(not cpp_frontend_available(), reason="libclang not available")
def test_an_added_same_stem_sibling_keeps_the_libclang_registrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The header was parsed by tree-sitter last run (no compile database), so
    # it holds a module qn; this run the database arrives with `shape.cpp`,
    # the frontend registers the header before the flux-stem cleanup, and the
    # file pass skips the covered header. The cleanup must keep what the
    # frontend just registered: nothing else restores it.
    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.LIBCLANG)
    root = tmp_path / "proj"
    _write(root, _CPP)
    updater = _make_updater(root, InMemoryGraph())
    updater.run(force=True)
    module_map = updater.factory.definition_processor.module_qn_to_file_path
    assert root / "shape.h" in module_map.values(), "the header holds no claim"

    (root / _CPP_ADDED[0]).write_text(_CPP_ADDED[1])
    _compile_commands(root, ["use.cpp", _CPP_ADDED[0]])
    updater.run(force=False)

    owned = updater._frontend_owned_qns.get("shape.h", set())
    assert "shape.h" in updater._cpp_frontend_covered, "the header is not covered"
    assert owned, "the frontend registered nothing for the header"
    registry = updater.factory.function_registry
    assert {qn for qn in owned if qn not in registry} == set(), owned


def _defines_by_module(graph: InMemoryGraph) -> dict[str, set[tuple[str, str]]]:
    """Module qn -> {(defined qn, path of the defined node)}."""
    paths = {
        uid: props.get(cs.KEY_PATH) for (_label, uid), props in graph.nodes.items()
    }
    out: dict[str, set[tuple[str, str]]] = {}
    for fl, _fk, fv, rel, _tl, _tk, tv in graph.rels:
        if fl == cs.NodeLabel.MODULE and rel == cs.RelationshipType.DEFINES:
            out.setdefault(str(fv), set()).add((str(tv), str(paths.get(tv))))
    return out


def test_an_unreadable_same_stem_survivor_keeps_its_module(
    tmp_path: Path,
) -> None:
    # `util.h` cannot be read when `util.c` is added: it is not re-parsed, so
    # its graph subtree stays, and it must keep its module qn with it. Were
    # its claim dropped, `util.c` would take the bare qn over a Module that
    # still DEFINES the header's functions.
    root = tmp_path / "proj"
    _write(
        root,
        {
            "util.h": "static inline int helper(void) { return 2; }\nint util(void);\n",
            "main.c": '#include "util.h"\nint main(void) { return util() + helper(); }\n',
        },
    )
    graph = InMemoryGraph()
    updater = _make_updater(root, graph)
    updater.run(force=True)
    header_module = next(
        (
            uid
            for (label, uid), props in graph.nodes.items()
            if label == cs.NodeLabel.MODULE and props.get(cs.KEY_PATH) == "util.h"
        ),
        None,
    )
    assert header_module is not None, "the header was never indexed"

    (root / _C_ADDED[0]).write_text(_C_ADDED[1])
    header = root / "util.h"
    os.utime(header, (time.time() + 5, time.time() + 5))
    real_open, real_read_bytes = builtins.open, Path.read_bytes

    def denied(path: object) -> None:
        if Path(str(path)) == header:
            raise PermissionError(13, "Permission denied", str(path))

    def open_denied(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        denied(file)
        return real_open(file, *args, **kwargs)

    def read_bytes_denied(self: Path) -> bytes:
        denied(self)
        return real_read_bytes(self)

    with (
        patch("builtins.open", open_denied),
        patch.object(Path, "read_bytes", read_bytes_denied),
    ):
        updater.run(force=False)

    modules = {
        str(uid): props.get(cs.KEY_PATH)
        for (label, uid), props in graph.nodes.items()
        if label == cs.NodeLabel.MODULE
    }
    assert modules.get(str(header_module)) == "util.h", modules
    for module, defined in _defines_by_module(graph).items():
        foreign = {pair for pair in defined if pair[1] != modules.get(module)}
        assert not foreign, (module, modules.get(module), foreign)


def test_a_survivor_that_opens_but_cannot_be_read_keeps_its_module(
    tmp_path: Path,
) -> None:
    # The race behind the check above: `util.h` passes the open check, which
    # drops its claim, and then fails the read that decides whether it is
    # parsed. Its subtree stays, so the claim has to come back before
    # `util.c` is parsed.
    root = tmp_path / "proj"
    _write(
        root,
        {
            "util.h": "static inline int helper(void) { return 2; }\nint util(void);\n",
            "main.c": '#include "util.h"\nint main(void) { return util() + helper(); }\n',
        },
    )
    graph = InMemoryGraph()
    updater = _make_updater(root, graph)
    updater.run(force=True)
    header_module = next(
        (
            uid
            for (label, uid), props in graph.nodes.items()
            if label == cs.NodeLabel.MODULE and props.get(cs.KEY_PATH) == "util.h"
        ),
        None,
    )
    assert header_module is not None, "the header was never indexed"

    (root / _C_ADDED[0]).write_text(_C_ADDED[1])
    header = root / "util.h"
    os.utime(header, (time.time() + 5, time.time() + 5))
    real_open, real_read_bytes = builtins.open, Path.read_bytes

    def denied(path: object) -> None:
        if Path(str(path)) == header:
            raise PermissionError(13, "Permission denied", str(path))

    def open_denied(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        denied(file)
        return real_open(file, *args, **kwargs)

    def read_bytes_denied(self: Path) -> bytes:
        denied(self)
        return real_read_bytes(self)

    with (
        patch("codebase_rag.graph_updater._opens_for_reading", return_value=True),
        patch("builtins.open", open_denied),
        patch.object(Path, "read_bytes", read_bytes_denied),
    ):
        updater.run(force=False)

    modules = {
        str(uid): props.get(cs.KEY_PATH)
        for (label, uid), props in graph.nodes.items()
        if label == cs.NodeLabel.MODULE
    }
    assert modules.get(str(header_module)) == "util.h", modules
    for module, defined in _defines_by_module(graph).items():
        foreign = {pair for pair in defined if pair[1] != modules.get(module)}
        assert not foreign, (module, modules.get(module), foreign)


@pytest.mark.skipif(not cpp_frontend_available(), reason="libclang not available")
@pytest.mark.parametrize("reuse", [False, True], ids=["fresh", "reused"])
def test_an_added_same_stem_sibling_matches_a_clean_libclang_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reuse: bool
) -> None:
    # With the compile database present on BOTH runs, the frontend emits the
    # header's subtree before `_process_files` deletes the stem-flux
    # survivors' old subtrees by path, and the file pass skips covered files,
    # so nothing restored the header: the incremental graph kept last run's
    # `proj.shape.geo.Shape` and lost `proj.shape.h.*` (#2231).
    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.LIBCLANG)
    sources = ["use.cpp", _CPP_ADDED[0]]

    golden_root = tmp_path / "golden" / "proj"
    _write(golden_root, {**_CPP, _CPP_ADDED[0]: _CPP_ADDED[1]})
    _compile_commands(golden_root, sources)
    golden = InMemoryGraph()
    golden_updater = _make_updater(golden_root, golden)
    golden_updater.run(force=True)

    incr_root = tmp_path / "incr" / "proj"
    _write(incr_root, _CPP)
    _compile_commands(incr_root, ["use.cpp"])
    incr = InMemoryGraph()
    updater = _make_updater(incr_root, incr)
    updater.run(force=True)
    (incr_root / _CPP_ADDED[0]).write_text(_CPP_ADDED[1])
    _compile_commands(incr_root, sources)
    if not reuse:
        updater = _make_updater(incr_root, incr)
    updater.run(force=False)

    # The registry too: it held both `proj.shape.geo.Shape*` and
    # `proj.shape.h.geo.Shape*` after the incremental run.
    assert set(updater.factory.function_registry.keys()) == set(
        golden_updater.factory.function_registry.keys()
    )

    (golden_nodes, golden_rels), (nodes, rels) = golden.snapshot(), incr.snapshot()
    assert golden_nodes, "the clean index is empty, so the comparison proves nothing"
    assert nodes == golden_nodes, {
        "extra": sorted(map(str, nodes - golden_nodes)),
        "missing": sorted(map(str, golden_nodes - nodes)),
    }
    assert rels == golden_rels, {
        "extra": sorted(map(str, rels - golden_rels)),
        "missing": sorted(map(str, golden_rels - rels)),
    }


class _CoveringFrontend:
    """A BEFORE_DEFINITIONS emitting frontend that owns one file."""

    language = cs.SupportedLanguage.RUST
    phase = FrontendPhase.BEFORE_DEFINITIONS

    def __init__(self, covered: str) -> None:
        self.covered = covered
        self.emits = 0

    def available(self) -> bool:
        return True

    def applies(self, repo_path: Path) -> bool:
        return True

    def emit(self, ctx: FrontendEmitContext) -> FrontendEmitResult:
        self.emits += 1
        return FrontendEmitResult(covered_files=frozenset({self.covered}))


def test_the_rerun_keeps_every_emitting_frontends_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The LIBCLANG re-run clears the covered set, which every emitting
    # frontend adds to, so the frontends that filled it run again with it;
    # re-running only the C++ one left another frontend's file uncovered and
    # its output unregenerated (CodeRabbit, PR #2247).
    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.LIBCLANG)
    frontend = _CoveringFrontend("lib.rs")
    registry = dict(EMITTING_FRONTENDS)
    registry[frontend.language] = frontend
    monkeypatch.setattr("codebase_rag.graph_updater.EMITTING_FRONTENDS", registry)
    root = tmp_path / "proj"
    _write(root, {"lib.rs": "pub fn f() -> i32 { 1 }\n"})
    updater = _make_updater(root, InMemoryGraph())
    updater.run(force=True)

    lib = root / "lib.rs"
    lib.write_text("pub fn f() -> i32 { 2 }\n")
    os.utime(lib, (time.time() + 5, time.time() + 5))
    emits_before = frontend.emits
    updater.run(force=False)

    assert "lib.rs" in updater._cpp_frontend_covered
    assert frontend.emits == emits_before + 2, "the re-run skipped the frontend"


@pytest.mark.skipif(not cpp_frontend_available(), reason="libclang not available")
@pytest.mark.parametrize("reuse", [False, True], ids=["fresh", "reused"])
def test_an_added_covered_file_beside_an_uncovered_survivor_matches_a_clean_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reuse: bool
) -> None:
    # The only covered file in flux is the ADDED `shape.cpp`; its same-stem
    # survivor `shape.rs` is tree-sitter's. The frontend's first pass MERGEs
    # `shape.cpp` onto the Module `shape.rs` held under the bare qn, so the
    # re-run must fire for an added covered file too (CodeRabbit, PR #2247).
    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.LIBCLANG)
    base = {
        "shape.rs": "pub fn area() -> i32 { 1 }\n",
        "main.cpp": "int main() { return 0; }\n",
    }
    added = ("shape.cpp", "int perimeter() { return 2; }\n")

    golden_root = tmp_path / "golden" / "proj"
    _write(golden_root, {**base, added[0]: added[1]})
    _compile_commands(golden_root, ["main.cpp", added[0]])
    golden = InMemoryGraph()
    _make_updater(golden_root, golden).run(force=True)

    incr_root = tmp_path / "incr" / "proj"
    _write(incr_root, base)
    _compile_commands(incr_root, ["main.cpp"])
    incr = InMemoryGraph()
    updater = _make_updater(incr_root, incr)
    updater.run(force=True)
    (incr_root / added[0]).write_text(added[1])
    _compile_commands(incr_root, ["main.cpp", added[0]])
    (updater if reuse else _make_updater(incr_root, incr)).run(force=False)

    (golden_nodes, golden_rels), (nodes, rels) = golden.snapshot(), incr.snapshot()
    assert golden_nodes, "the clean index is empty, so the comparison proves nothing"
    assert nodes == golden_nodes, {
        "extra": sorted(map(str, nodes - golden_nodes)),
        "missing": sorted(map(str, golden_nodes - nodes)),
    }
    assert rels == golden_rels, {
        "extra": sorted(map(str, rels - golden_rels)),
        "missing": sorted(map(str, golden_rels - rels)),
    }


class _OrderRecordingGraph(InMemoryGraph):
    """Records flushes and module deletes in the order they reach the graph."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def flush_all(self) -> None:
        self.events.append("flush")
        super().flush_all()

    def execute_write(self, query: str, params: dict | None = None) -> None:
        if query == cs.CYPHER_DELETE_MODULE:
            self.events.append("delete")
        super().execute_write(query, params)


@pytest.mark.skipif(not cpp_frontend_available(), reason="libclang not available")
def test_the_frontends_emission_is_flushed_before_the_stale_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real ingestor buffers the frontend's writes while the deletes go to
    # the graph at once, so an unflushed emission made the outcome depend on
    # when the buffer filled (CodeRabbit, PR #2247).
    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.LIBCLANG)
    root = tmp_path / "proj"
    _write(root, _CPP)
    _compile_commands(root, ["use.cpp"])
    graph = _OrderRecordingGraph()
    updater = _make_updater(root, graph)
    updater.run(force=True)
    (root / _CPP_ADDED[0]).write_text(_CPP_ADDED[1])
    _compile_commands(root, ["use.cpp", _CPP_ADDED[0]])
    graph.events.clear()
    updater.run(force=False)

    assert "delete" in graph.events, graph.events
    assert graph.events.index("flush") < graph.events.index("delete"), graph.events


@pytest.mark.skipif(not cpp_frontend_available(), reason="libclang not available")
def test_a_failed_rerun_still_rebuilds_the_deleted_files_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The re-run comes after the stale subtrees are deleted. Raising out of
    # it ended the run there and left those files out of the graph until a
    # later run succeeded; like a failed file, it must let the file pass
    # rebuild them and raise afterwards (CodeRabbit, PR #2247).
    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.LIBCLANG)
    root = tmp_path / "proj"
    _write(root, _CPP)
    _compile_commands(root, ["use.cpp"])
    graph = InMemoryGraph()
    updater = _make_updater(root, graph)
    updater.run(force=True)
    (root / _CPP_ADDED[0]).write_text(_CPP_ADDED[1])
    _compile_commands(root, ["use.cpp", _CPP_ADDED[0]])

    real_run = GraphUpdater._run_cpp_frontend
    calls: list[int] = []

    def fail_on_rerun(self: GraphUpdater) -> None:
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("frontend re-run failed")
        real_run(self)

    monkeypatch.setattr(GraphUpdater, "_run_cpp_frontend", fail_on_rerun)
    with pytest.raises(RuntimeError, match="frontend re-run failed"):
        updater.run(force=False)

    paths = {
        props.get(cs.KEY_PATH)
        for (label, _uid), props in graph.nodes.items()
        if label == cs.NodeLabel.MODULE
    }
    assert {"shape.h", "use.cpp"} <= paths, paths


@pytest.mark.skipif(not cpp_frontend_available(), reason="libclang not available")
def test_a_later_emitter_failure_keeps_the_cpp_rerun_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The C++ re-run succeeded and emitted its files; a later emitter's
    # failure must not uncover them, or the file pass rebuilds them with
    # tree-sitter on top of that emission (CodeRabbit, PR #2247).
    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.LIBCLANG)
    root = tmp_path / "proj"
    _write(root, _CPP)
    _compile_commands(root, ["use.cpp"])
    updater = _make_updater(root, InMemoryGraph())
    updater.run(force=True)
    (root / _CPP_ADDED[0]).write_text(_CPP_ADDED[1])
    _compile_commands(root, ["use.cpp", _CPP_ADDED[0]])

    real_emit = GraphUpdater._run_emitting_frontends
    calls: list[int] = []

    def fail_on_rerun(self: GraphUpdater, phase: FrontendPhase) -> None:
        if phase == FrontendPhase.BEFORE_DEFINITIONS:
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("emitter re-run failed")
        real_emit(self, phase)

    monkeypatch.setattr(GraphUpdater, "_run_emitting_frontends", fail_on_rerun)
    with pytest.raises(RuntimeError, match="emitter re-run failed"):
        updater.run(force=False)

    assert "shape.h" in updater._cpp_frontend_covered
