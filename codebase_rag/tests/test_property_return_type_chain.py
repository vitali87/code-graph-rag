# (H) L3 finding from the evals/ harness: a method calls self.prop.method(), where
# (H) self.prop is an @property whose declared return type names the class owning
# (H) the real method. The property's return type must seed self.prop's type so the
# (H) chained call resolves to the correct class instead of an ambiguous same-class
# (H) method of the same name.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

MODULE_SRC = """class Worker:
    def build(self) -> str:
        return "real"


class Engine:
    @property
    def inner(self) -> Worker:
        return Worker()

    def build(self) -> str:
        return self.inner.build()
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


class TestPropertyReturnTypeChain:
    def test_chained_call_through_property_resolves_to_return_type_class(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.Engine.build", "proj.m.Worker.build") in calls, calls

    def test_does_not_resolve_to_same_class_method_of_same_name(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.Engine.build", "proj.m.Engine.build") not in calls, calls
