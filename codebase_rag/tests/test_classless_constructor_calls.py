from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater


def _edges(mock_ingestor: MagicMock, rel: str) -> list[tuple[str, str, str]]:
    # edges of a given type as (caller_qn, callee_label, callee_qn).
    out: list[tuple[str, str, str]] = []
    for c in mock_ingestor.ensure_relationship_batch.call_args_list:
        if c.args[1] == rel:
            out.append((c.args[0][2], c.args[2][0], c.args[2][2]))
    return out


class TestConstructionEdges:
    def test_dataclass_construction_emits_instantiates_not_calls(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # a class with no explicit __init__ is represented by INSTANTIATES to
        # the class node; CALLS stays function/method-only (never a class).
        (temp_repo / "app.py").write_text(
            "from dataclasses import dataclass\n"
            "\n"
            "\n"
            "@dataclass\n"
            "class Config:\n"
            "    n: int\n"
            "\n"
            "\n"
            "def use():\n"
            "    return Config(1)\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        instantiates = _edges(mock_ingestor, cs.RelationshipType.INSTANTIATES)
        calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)

        assert any(
            caller.endswith(".use")
            and to_label == cs.NodeLabel.CLASS
            and to_qn.endswith(".Config")
            for caller, to_label, to_qn in instantiates
        ), f"no INSTANTIATES->Config edge; instantiates={sorted(instantiates)}"
        assert not any(
            to_label == cs.NodeLabel.CLASS for _caller, to_label, _to_qn in calls
        ), f"CALLS must never target a class; calls={sorted(calls)}"

    def test_class_with_init_emits_both_instantiates_and_init_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # a class WITH __init__ records INSTANTIATES -> class AND CALLS -> the
        # __init__ method (the constructor runs); still no CALLS -> class.
        (temp_repo / "app.py").write_text(
            "class Widget:\n"
            "    def __init__(self, n):\n"
            "        self.n = n\n"
            "\n"
            "\n"
            "def use():\n"
            "    return Widget(1)\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        instantiates = _edges(mock_ingestor, cs.RelationshipType.INSTANTIATES)
        calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)

        assert any(
            caller.endswith(".use")
            and to_label == cs.NodeLabel.CLASS
            and to_qn.endswith(".Widget")
            for caller, to_label, to_qn in instantiates
        )
        assert any(
            caller.endswith(".use")
            and to_label == cs.NodeLabel.METHOD
            and to_qn.endswith(".Widget.__init__")
            for caller, to_label, to_qn in calls
        )
        assert not any(
            to_label == cs.NodeLabel.CLASS for _caller, to_label, _to_qn in calls
        )
