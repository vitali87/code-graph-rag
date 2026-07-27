# A TS/JS `new X()` runs X's `constructor` method, but the constructor-call
# redirect only looked for Python's `__init__`, so an explicitly-declared
# TypeScript constructor got INSTANTIATES -> class and no CALLS edge, and the
# dead-code walk (which never traverses INSTANTIATES by default) reported every
# such constructor as unreachable. Found dogfooding `cgr dead-code` on zod.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

# Type-annotation-free so the one source is valid JavaScript, TypeScript and TSX.
JS_FAMILY_SRC = """class Widget {
  constructor() {
    this.x = 1;
  }
}

class Plain {}

function build() {
  return new Widget();
}

function buildPlain() {
  return new Plain();
}
"""

# Python regression: the language switch must keep routing `X()` to `__init__`.
PY_SRC = """class Widget:
    def __init__(self) -> None:
        self.x = 1


class Plain:
    pass


def build() -> Widget:
    return Widget()


def build_plain() -> Plain:
    return Plain()
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


def _calls(
    tmp_path: Path, filename: str, src: str
) -> set[tuple[PropertyValue, PropertyValue]]:
    (tmp_path / filename).write_text(src)
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


class TestJsFamilyConstructorCallResolution:
    @pytest.mark.parametrize("filename", ["m.js", "m.ts", "m.tsx"])
    def test_new_calls_constructor(self, tmp_path: Path, filename: str) -> None:
        calls = _calls(tmp_path, filename, JS_FAMILY_SRC)
        assert ("proj.m.build", "proj.m.Widget.constructor") in calls, calls

    @pytest.mark.parametrize("filename", ["m.js", "m.ts", "m.tsx"])
    def test_new_without_constructor_emits_no_phantom_edge(
        self, tmp_path: Path, filename: str
    ) -> None:
        calls = _calls(tmp_path, filename, JS_FAMILY_SRC)
        # Plain declares no constructor: neither a CALLS edge to the class node
        # nor a phantom CALLS edge to a non-existent `Plain.constructor` method.
        assert ("proj.m.buildPlain", "proj.m.Plain") not in calls, calls
        assert ("proj.m.buildPlain", "proj.m.Plain.constructor") not in calls, calls


class TestPythonConstructorStillRoutesToInit:
    def test_new_calls_init(self, tmp_path: Path) -> None:
        # The `constructor`-vs-`__init__` language switch must not regress Python.
        calls = _calls(tmp_path, "m.py", PY_SRC)
        assert ("proj.m.build", "proj.m.Widget.__init__") in calls, calls
        # Plain has no __init__: neither a CALLS edge to the class node nor a
        # phantom edge to a non-existent Plain.__init__ method.
        assert ("proj.m.build_plain", "proj.m.Plain") not in calls, calls
        assert ("proj.m.build_plain", "proj.m.Plain.__init__") not in calls, calls
