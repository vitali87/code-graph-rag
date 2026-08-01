# L3 finding from the evals/ harness: TypeInferenceEngine.build_local_variable_type_map
# calls self.python_type_inference.build_local_variable_type_map(...), where the
# python_type_inference property returns PythonTypeInferenceEngine imported via a
# package re-export (from .py import PythonTypeInferenceEngine). The caller's import
# map points the name at the re-export module, not the class's real definition, so
# the chained method must follow the re-export hop to resolve to the concrete class
# rather than collapsing to an ambiguous same-named method (the caller itself).
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

# PythonEngine lives in pkg/py/engine.py and is re-exported from pkg/py/__init__.py.
# A sibling JsEngine.build_map exists so the bare name is ambiguous in the trie.
FILES = {
    "pkg/__init__.py": "",
    "pkg/py/__init__.py": "from .engine import PythonEngine\n\n__all__ = ['PythonEngine']\n",
    "pkg/py/engine.py": (
        "class PythonEngine:\n    def build_map(self, node):\n        return {}\n"
    ),
    "pkg/js_engine.py": (
        "class JsEngine:\n    def build_map(self, node):\n        return {}\n"
    ),
    "pkg/dispatch.py": (
        "from .py import PythonEngine\n\n\n"
        "class Dispatch:\n"
        "    def __init__(self) -> None:\n"
        "        self._python_engine = None\n\n"
        "    @property\n"
        "    def python_engine(self) -> PythonEngine:\n"
        "        if self._python_engine is None:\n"
        "            self._python_engine = PythonEngine()\n"
        "        return self._python_engine\n\n"
        "    def build_map(self, node):\n"
        "        return self.python_engine.build_map(node)\n"
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


class TestReexportChainResolution:
    def test_property_typed_by_reexport_resolves_to_real_class(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.dispatch.Dispatch.build_map",
            "proj.pkg.py.engine.PythonEngine.build_map",
        ) in calls, calls

    def test_does_not_collapse_to_caller_same_named_method(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.dispatch.Dispatch.build_map",
            "proj.pkg.dispatch.Dispatch.build_map",
        ) not in calls, calls
