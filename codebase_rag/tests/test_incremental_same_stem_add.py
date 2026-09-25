"""An added same-stem sibling on the batch path matches a clean index (#2022).

`util.c` added beside `util.h`, or `shape.cpp` beside `shape.h`, takes the
bare module qn a clean index gives the first file in walk order, and the
header moves to its suffixed qn. A fresh updater always got this right; one
held across runs (the watcher, the MCP server) kept the header's claim and
its definitions from the previous run, so the added file was suffixed and
its function marked a duplicate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
