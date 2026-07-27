# A TS/JS `new X()` runs X's `constructor` method, but the constructor-call
# redirect only looked for Python's `__init__`, so an explicitly-declared
# TypeScript constructor got INSTANTIATES -> class and no CALLS edge, and the
# dead-code walk (which never traverses INSTANTIATES by default) reported every
# such constructor as unreachable. Found dogfooding `cgr dead-code` on zod.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

MODULE_SRC = """class Widget {
  x: number;
  constructor() {
    this.x = 1;
  }
}

class Plain {}

function build(): Widget {
  return new Widget();
}

function buildPlain(): Plain {
  return new Plain();
}
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
    (tmp_path / "m.ts").write_text(MODULE_SRC)
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


class TestTsConstructorCallResolution:
    def test_new_calls_constructor(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.build", "proj.m.Widget.constructor") in calls, calls

    def test_new_without_constructor_not_dropped_to_class(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        # Plain declares no constructor; cgr must not emit a CALLS edge to the
        # class node (INSTANTIATES carries the construction instead).
        assert ("proj.m.buildPlain", "proj.m.Plain") not in calls, calls
