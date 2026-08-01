# A function referenced from a CLASS-FIELD initializer (`log = noop`) or an
# OBJECT SHORTHAND property (`return { retryStrategy }`) must get a REFERENCES
# edge, or `cgr dead-code` false-flags it. Found dogfooding NestJS
# (`SilentLogger` assigns a module `noop` to seven fields; `getClientOptions`
# returns `{ retryStrategy }`).
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"

CLASS_FIELD_SRC = """const noop = () => {};

export class Logger {
  log = noop;
  error = noop;
}
"""

SHORTHAND_SRC = """export function getOpts() {
  const retryStrategy = (t: number) => t * 2;
  return { a: 1, retryStrategy };
}
"""

# A class field holding an object literal with a shorthand (the collection scan
# must also descend into class bodies).
CLASS_FIELD_OBJECT_SRC = """function retryStrategy() {}

export class Client {
  opts = { retryStrategy };
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


def _referenced(tmp_path: Path, src: str, leaf: str) -> bool:
    (tmp_path / "m.ts").write_text(src)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    references = str(cs.RelationshipType.REFERENCES)
    return any(
        rel == references and str(to).rsplit(".", 1)[-1] == leaf
        for _frm, rel, to in cap.rels
    )


class TestFieldAndShorthandRefs:
    def test_class_field_initializer_references_function(self, tmp_path: Path) -> None:
        assert _referenced(tmp_path, CLASS_FIELD_SRC, "noop")

    def test_object_shorthand_references_function(self, tmp_path: Path) -> None:
        assert _referenced(tmp_path, SHORTHAND_SRC, "retryStrategy")

    def test_class_field_object_shorthand_references_function(
        self, tmp_path: Path
    ) -> None:
        assert _referenced(tmp_path, CLASS_FIELD_OBJECT_SRC, "retryStrategy")
