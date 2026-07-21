# L3 finding from the evals/ harness: a method calls self.attr.method(), but the
# type of self.attr is only knowable from the __init__ assignment in the same
# class. cgr scanned only the calling method for self-assignments, so the type
# was unknown and an ambiguous bare name resolved to the wrong global. Instance
# attributes assigned in __init__ must be visible to every method of the class.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

MODULE_SRC = """def run() -> str:
    return "global"


def status() -> str:
    return "globalprop"


class Helper:
    def run(self) -> str:
        return "real"

    @property
    def status(self) -> str:
        return "ok"


class App:
    def __init__(self) -> None:
        self.helper = Helper()

    def go(self) -> str:
        return self.helper.run()

    def check(self) -> str:
        return self.helper.status
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


class TestInstanceAttrTypeInference:
    def test_method_call_resolves_via_init_attribute_type(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.App.go", "proj.m.Helper.run") in calls, calls

    def test_ambiguous_method_does_not_resolve_to_module_function(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.App.go", "proj.m.run") not in calls, calls

    def test_property_access_resolves_via_init_attribute_type(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.App.check", "proj.m.Helper.status") in calls, calls

    def test_property_access_not_resolved_to_module_function(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.App.check", "proj.m.status") not in calls, calls
