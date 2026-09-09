"""Deleting a file leaves its dependents as a clean index would.

A CHARACTERISATION test, not a reproduction. Issue #1584 predicted that a
dependent reparsed by the affected-caller pass, while the deleted file's
module is still in the graph, would resolve against the doomed module and
miss the ExternalModule fallback. Measured on `97dc6a31`, it does not:
node sets and relationship sets are identical to a clean index of the
remaining tree, in Python here and in Java by probe.

Pinned rather than dropped, because the ordering it depends on is
UNSTATED in `_process_files` and `test_graph_updater_phase_order.py`
cannot pin the pair (both call sites live outside `run` and outside
`_DELEGATES`). This is the only thing that would notice if the passes were
reordered.

Written as golden-versus-incremental because the acceptance criterion is
that the two graphs are EQUAL. That comparison is satisfied by mutual
emptiness, so the first test below asserts the fallback is present on the
golden side; without it the comparison would pass on a fixture that
produced nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_graph_updater_incremental_rename import (
    InMemoryGraph,
    _make_updater,
)

_BASE = "class Base:\n    def m(self):\n        return 1\n"
_SUB = (
    "from .base import Base\n\n\nclass Sub(Base):\n    def m(self):\n        return 2\n"
)


def _write_tree(root: Path, *, with_base: bool) -> None:
    (root / "__init__.py").touch()
    (root / "sub.py").write_text(_SUB, encoding="utf-8")
    if with_base:
        (root / "base.py").write_text(_BASE, encoding="utf-8")


def _inherits(graph: InMemoryGraph) -> set[tuple[str, str, str]]:
    """`(subclass_qn, target_label, target_qn)` for every INHERITS edge.

    The rel tuple is `(from_label, from_key, from_val, rel_type, to_label,
    to_key, to_val)`; the target LABEL is kept because the fallback a clean
    index takes is specifically an `ExternalModule`, and an assertion that
    only checked the edge existed could not tell the fallback from a real
    resolution.
    """
    _nodes, rels = graph.snapshot()
    return {
        (str(from_val), str(to_label), str(to_val))
        for (_fl, _fk, from_val, rel, to_label, _tk, to_val) in rels
        if rel == cs.RelationshipType.INHERITS.value
    }


class TestIncrementalDeleteMatchesCleanIndex:
    def test_the_clean_index_creates_the_external_fallback_node(
        self, tmp_path: Path
    ) -> None:
        """The control, and it corrects the issue's description for Python.

        #1584 describes the fallback as an `INHERITS -> ExternalModule`
        EDGE. Measured on a Python tree, a clean index of `sub.py` whose
        `from .base import Base` resolves to nothing creates the
        `ExternalModule` NODE and an `IMPORTS` edge, and no `INHERITS` edge
        at all -- the unresolved base is simply not recorded. So the
        fallback to compare on here is the node.

        Without this control the comparison below would pass vacuously:
        two graphs that both lack an edge are equal, and asserting the
        absent thing is what a wrong reproduction looks like from inside.
        """
        golden_root = tmp_path / "golden"
        golden_root.mkdir()
        _write_tree(golden_root, with_base=False)
        golden = InMemoryGraph()
        _make_updater(golden_root, golden).run(force=True)

        nodes, _rels = golden.snapshot()
        external = {
            str(uid) for (label, uid) in nodes if label == cs.NodeLabel.EXTERNAL_MODULE
        }

        assert external, "the clean index created no ExternalModule at all"
        assert _inherits(golden) == set(), (
            "a Python clean index records no INHERITS for an unresolved base; "
            "if that changed, this test's premise needs revisiting"
        )

    def test_incremental_delete_matches_a_clean_index(self, tmp_path: Path) -> None:
        golden_root = tmp_path / "golden"
        golden_root.mkdir()
        _write_tree(golden_root, with_base=False)
        golden = InMemoryGraph()
        _make_updater(golden_root, golden).run(force=True)

        incr_root = tmp_path / "incr"
        incr_root.mkdir()
        _write_tree(incr_root, with_base=True)
        incr = InMemoryGraph()
        _make_updater(incr_root, incr).run(force=True)

        (incr_root / "base.py").unlink()
        _make_updater(incr_root, incr).run(force=False)

        incr_nodes, incr_rels = incr.snapshot()
        golden_nodes, golden_rels = golden.snapshot()

        assert incr_nodes == golden_nodes, {
            "extra_nodes": sorted(map(str, incr_nodes - golden_nodes)),
            "missing_nodes": sorted(map(str, golden_nodes - incr_nodes)),
        }
        assert incr_rels == golden_rels, {
            "extra_rels": sorted(map(str, incr_rels - golden_rels)),
            "missing_rels": sorted(map(str, golden_rels - incr_rels)),
        }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
