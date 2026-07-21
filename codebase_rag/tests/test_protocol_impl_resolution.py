# L3 finding from the evals/ harness: a call on a parameter typed as a
# Protocol (function_registry.get() where function_registry is a
# FunctionRegistryTrieProtocol) is traced to the concrete implementer
# (FunctionRegistryTrie), not the Protocol stub. cgr infers the Protocol
# type but stops at the stub; the XxxProtocol -> Xxx naming convention picks
# the real implementer and disambiguates it from other structural conformers
# such as a test mock.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

MODULE_SRC = """from typing import Protocol


class StoreProtocol(Protocol):
    def fetch(self, key: str) -> int: ...


class Store:
    def fetch(self, key: str) -> int:
        return 1


class MockStore:
    def fetch(self, key: str) -> int:
        return 2


def use(store: StoreProtocol) -> int:
    return store.fetch("x")
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


class TestProtocolImplResolution:
    def test_protocol_typed_call_resolves_to_concrete_implementer(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.use", "proj.m.Store.fetch") in calls, calls

    def test_does_not_resolve_to_protocol_stub(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.use", "proj.m.StoreProtocol.fetch") not in calls, calls

    def test_naming_convention_disambiguates_from_other_conformer(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.use", "proj.m.MockStore.fetch") not in calls, calls
