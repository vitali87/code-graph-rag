# (H) L3 finding from the evals/ harness: a function bound to a local variable and
# (H) then called through that alias (g = self._method; g()) runs the aliased
# (H) callable at runtime, but cgr saw a bare-name call that resolved to nothing.
# (H) A call through a local alias must produce a CALLS edge to the aliased target.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

MODULE_SRC = """class Engine:
    def run(self) -> str:
        do = self._start
        return do()

    def _start(self) -> str:
        return helper()


def helper() -> str:
    return "x"


def top() -> str:
    fn = helper
    return fn()
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


class TestLocalAliasCalls:
    def test_alias_to_self_method_is_a_call(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.Engine.run", "proj.m.Engine._start") in calls, calls

    def test_alias_to_module_function_is_a_call(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.top", "proj.m.helper") in calls, calls

    def test_direct_call_unaffected(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.Engine._start", "proj.m.helper") in calls, calls
