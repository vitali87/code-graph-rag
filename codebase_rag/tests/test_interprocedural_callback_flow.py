# L3 finding from the evals/ harness: extract_java_interface_names invokes a
# resolve_to_qn callback threaded through extract_implemented_interfaces from a
# caller that passes self._resolve_to_qn. The callable is bound at the outer
# call site and flows through pass-through parameters to where it is invoked, so
# resolving the edge needs inter-procedural callback propagation.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

FILES = {
    "pkg/__init__.py": "",
    # extract_names invokes the callback; extract_interfaces only passes it through.
    "pkg/extract.py": (
        "def extract_names(node, out, scope, resolve_to_qn):\n"
        '        out.append(resolve_to_qn("x", scope))\n\n\n'
        "def extract_interfaces(node, scope, resolve_to_qn):\n"
        "        out = []\n"
        "        extract_names(node, out, scope, resolve_to_qn)\n"
        "        return out\n"
    ),
    "pkg/driver.py": (
        "from .extract import extract_interfaces\n\n\n"
        "class Driver:\n"
        "    def resolve(self, name, scope):\n"
        "        return name\n\n"
        "    def run(self, node):\n"
        '        return extract_interfaces(node, "s", self.resolve)\n'
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


class TestInterproceduralCallbackFlow:
    def test_callback_propagates_through_passthrough_param(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.extract.extract_names",
            "proj.pkg.driver.Driver.resolve",
        ) in calls, calls
