# A Python class inheriting a SUBSCRIPTED generic base (click's numeric
# `IntRange(_NumberRangeBase[int, int], IntParamType)`) got NO INHERITS edge:
# the superclass walk only accepted identifier and attribute children, skipping
# `subscript` nodes. Without the edge the subclass has no OVERRIDES, so
# `self._clamp(...)` dispatch never reaches the override and it reports dead.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _rels(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {(c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel)}


def test_subscripted_generic_base_gets_inherits_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "pygen"
    root.mkdir(parents=True)
    (root / "types.py").write_text(
        "import typing as t\n"
        "_V = t.TypeVar('_V')\n"
        "class _NumberRangeBase(t.Generic[_V]):\n"
        "    def convert(self, value):\n"
        "        return self._clamp(value, 1, True)\n"
        "    def _clamp(self, bound, direction, open):\n"
        "        raise NotImplementedError\n"
        "class IntRange(_NumberRangeBase[int]):\n"
        "    def _clamp(self, bound, direction, open):\n"
        "        return bound\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing=None)
    inherits = _rels(mock_ingestor, "INHERITS")
    assert any(
        f.endswith(".IntRange") and t.endswith("._NumberRangeBase") for f, t in inherits
    ), sorted(inherits)
    overrides = _rels(mock_ingestor, "OVERRIDES")
    assert any(
        f.endswith(".IntRange._clamp") and t.endswith("._NumberRangeBase._clamp")
        for f, t in overrides
    ), sorted(overrides)
