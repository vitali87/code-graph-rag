# Finding #3 from the evals/ harness: methods of a class defined inside a
# function body (function-local class) were dropped. They are now captured by
# default (CAPTURE_FUNCTION_LOCAL_DEFINITIONS=True); explicitly disabling the
# flag restores the historical behaviour of skipping them.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "localproj"

MODULE_SRC = """class Holder:
    def make(self) -> object:
        class Local:
            def helper(self) -> str:
                return "x"

        return Local()
"""

_RelTuple = tuple[str, PropertyValue, str, str, PropertyValue]


class _Capture:
    def __init__(self) -> None:
        self.nodes: dict[tuple[str, PropertyValue], PropertyDict] = {}
        self.rels: list[_RelTuple] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        uid = properties[cs.NODE_UNIQUE_CONSTRAINTS[label]]
        self.nodes[(str(label), uid)] = dict(properties)

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append(
            (
                str(from_spec[0]),
                from_spec[2],
                str(rel_type),
                str(to_spec[0]),
                to_spec[2],
            )
        )

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _build(tmp_path: Path) -> _Capture:
    (tmp_path / "m.py").write_text(MODULE_SRC)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return cap


def _local_method_lines(cap: _Capture) -> list[int]:
    return sorted(
        int(props[cs.KEY_START_LINE])
        for (label, _uid), props in cap.nodes.items()
        if label == cs.NodeLabel.METHOD
        and props.get(cs.KEY_NAME) == "helper"
        and props.get(cs.KEY_START_LINE) is not None
    )


class TestFunctionLocalDefinitions:
    def test_default_captures_local_class_methods(self, tmp_path: Path) -> None:
        cap = _build(tmp_path)
        assert _local_method_lines(cap) == [4]

        defines_method_to_helper = [
            target
            for (_fl, _fv, rel_type, _tl, target) in cap.rels
            if rel_type == cs.RelationshipType.DEFINES_METHOD
            and str(target).endswith(".Local.helper")
        ]
        assert len(defines_method_to_helper) == 1, defines_method_to_helper

    def test_flag_off_skips_local_class_methods(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CAPTURE_FUNCTION_LOCAL_DEFINITIONS", False)
        cap = _build(tmp_path)
        assert _local_method_lines(cap) == []
