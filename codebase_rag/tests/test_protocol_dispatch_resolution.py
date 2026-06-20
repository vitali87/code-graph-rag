# (H) L3 finding from the evals/ harness: DefinitionProcessor._extract_decorators calls
# (H) self._handler.extract_decorators(node), where _handler is annotated as the Protocol
# (H) LanguageHandler (class-level annotation) and assigned dynamically via
# (H) get_handler(language). The runtime type is one of several conformers, so the sound
# (H) call graph emits an edge to extract_decorators on every conformer (capturing the
# (H) traced PythonHandler edge) and never to the Protocol stub, which never runs.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

FILES = {
    "pkg/__init__.py": "",
    "pkg/proto.py": (
        "from typing import Protocol\n\n\n"
        "class HandlerLike(Protocol):\n"
        "    def extract(self, node): ...\n"
    ),
    "pkg/base.py": (
        "class BaseHandler:\n    def extract(self, node):\n        return []\n"
    ),
    "pkg/python_h.py": (
        "from .base import BaseHandler\n\n\n"
        "class PyHandler(BaseHandler):\n"
        "    def extract(self, node):\n"
        "        return ['py']\n"
    ),
    "pkg/js_h.py": (
        "from .base import BaseHandler\n\n\n"
        "class JsHandler(BaseHandler):\n"
        "    def extract(self, node):\n"
        "        return ['js']\n"
    ),
    "pkg/proc.py": (
        "from .proto import HandlerLike\n\n\n"
        "class Proc:\n"
        "    _handler: HandlerLike\n\n"
        "    def __init__(self, handler) -> None:\n"
        "        self._handler = handler\n\n"
        "    def go(self, node):\n"
        "        return self._handler.extract(node)\n"
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


class TestProtocolDispatchResolution:
    def test_dispatches_to_concrete_conformer(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.proc.Proc.go",
            "proj.pkg.python_h.PyHandler.extract",
        ) in calls, calls

    def test_dispatches_to_all_conformers(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.proc.Proc.go",
            "proj.pkg.js_h.JsHandler.extract",
        ) in calls, calls
        assert (
            "proj.pkg.proc.Proc.go",
            "proj.pkg.base.BaseHandler.extract",
        ) in calls, calls

    def test_does_not_emit_protocol_stub_edge(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.proc.Proc.go",
            "proj.pkg.proto.HandlerLike.extract",
        ) not in calls, calls
