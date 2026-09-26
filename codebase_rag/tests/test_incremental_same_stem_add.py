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
from codebase_rag.parsers.cpp_frontend import cpp_frontend_available
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
