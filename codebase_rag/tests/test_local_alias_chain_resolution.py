# (H) L3 finding from the evals/ harness: CallProcessor._ingest_function_calls does
# (H) `registry = resolver.function_registry` (resolver = self._resolver) then
# (H) `qn in registry`, dispatching to FunctionRegistryTrie.__contains__. Resolving it
# (H) needs local-variable aliasing (local = self.attr) plus cross-class attribute-chain
# (H) typing (local2 = local.attr) so the operand's concrete type is known.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

FILES = {
    "pkg/__init__.py": "",
    "pkg/registry.py": (
        "class Registry:\n    def __contains__(self, key):\n        return True\n"
    ),
    "pkg/resolver.py": (
        "from .registry import Registry\n\n\n"
        "class Resolver:\n"
        "    def __init__(self) -> None:\n"
        "        self.registry = Registry()\n"
    ),
    "pkg/proc.py": (
        "from .resolver import Resolver\n\n\n"
        "class Proc:\n"
        "    def __init__(self) -> None:\n"
        "        self._resolver = Resolver()\n\n"
        "    def run(self, qn):\n"
        "        resolver = self._resolver\n"
        "        registry = resolver.registry\n"
        "        return qn in registry\n"
    ),
}


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
    for rel, content in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
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


class TestLocalAliasChainResolution:
    def test_local_alias_attribute_chain_dispatches_to_dunder(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.proc.Proc.run",
            "proj.pkg.registry.Registry.__contains__",
        ) in calls, calls
