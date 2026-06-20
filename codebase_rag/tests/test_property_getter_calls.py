# (H) L3 finding from the evals/ harness: accessing an @property getter runs the
# (H) getter method at runtime, but cgr saw a plain attribute access and emitted no
# (H) CALLS edge. A property access must produce a CALLS edge to the getter method,
# (H) while a normal attribute / method reference must not.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

MODULE_SRC = """class Engine:
    def __init__(self) -> None:
        self._n = 0

    @property
    def status(self) -> str:
        return self._compute()

    def _compute(self) -> str:
        return "ok"

    def check(self) -> str:
        return self.status


def use(e: Engine) -> str:
    return e.status


def plain(e: Engine) -> str:
    return e._compute()
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


class TestPropertyGetterCalls:
    def test_property_access_via_self_is_a_call(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.Engine.check", "proj.m.Engine.status") in calls, calls

    def test_property_access_via_typed_param_is_a_call(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.use", "proj.m.Engine.status") in calls, calls

    def test_property_access_only_emits_the_getter_edge(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        # (H) `use` only reads e.status; no spurious edge to the unrelated _compute.
        from_use = {to for (frm, to) in calls if frm == "proj.m.use"}
        assert from_use == {"proj.m.Engine.status"}, from_use

    def test_regular_method_call_is_unaffected(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        # (H) plain() calls a normal method, resolved by the existing call path.
        assert ("proj.m.plain", "proj.m.Engine._compute") in calls, calls
