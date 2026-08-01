# L3 finding from the evals/ harness: instantiating a class (X()) is a call to
# X.__init__ at runtime, but cgr resolved the call to the class and dropped it.
# A constructor call must produce a CALLS edge to the class's __init__ method.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

MODULE_SRC = """class Widget:
    def __init__(self) -> None:
        self.x = 1


class Plain:
    pass


def build() -> Widget:
    return Widget()


def build_plain() -> Plain:
    return Plain()
"""


class _Capture:
    def __init__(self) -> None:
        self.rels: list[tuple[PropertyValue, str, PropertyValue]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append((from_spec[2], str(rel_type), to_spec[2]))

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _calls(tmp_path: Path) -> set[tuple[PropertyValue, PropertyValue]]:
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
    return {
        (frm, to) for (frm, rel, to) in cap.rels if rel == cs.RelationshipType.CALLS
    }


class TestConstructorCallResolution:
    def test_instantiation_calls_init(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.build", "proj.m.Widget.__init__") in calls, calls

    def test_instantiation_without_init_is_not_dropped_to_class(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        # Plain has no __init__; cgr must not emit a CALLS edge to the class node.
        assert ("proj.m.build_plain", "proj.m.Plain") not in calls, calls
