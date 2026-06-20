# (H) L3 finding from the evals/ harness: CallProcessor._ingest_function_calls binds a
# (H) local to a conditionally-selected bound method (resolve_builtin =
# (H) resolver.resolve_builtin_call if is_js_ts else None) then calls it. The alias must
# (H) be resolved through the non-None branch of the conditional to its real method.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

FILES = {
    "pkg/__init__.py": "",
    "pkg/helper.py": (
        "class Helper:\n    def do(self, value):\n        return value\n"
    ),
    "pkg/worker.py": (
        "from .helper import Helper\n\n\n"
        "class Worker:\n"
        "    def __init__(self) -> None:\n"
        "        self._helper = Helper()\n\n"
        "    def run(self, value, flag):\n"
        "        helper = self._helper\n"
        "        fn = helper.do if flag else None\n"
        "        return fn(value)\n"
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


class TestConditionalAliasCall:
    def test_conditional_bound_method_alias_resolves(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.worker.Worker.run",
            "proj.pkg.helper.Helper.do",
        ) in calls, calls
