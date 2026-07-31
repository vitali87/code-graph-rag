# Generator function EXPRESSIONS must behave exactly like function
# expressions everywhere: registration (issue #994), reference emission for
# inline values, and the object-literal / prototype naming passes. A node
# minted without those paths is an orphan that reports dead.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"


class _Capture:
    def __init__(self) -> None:
        self.rels: list[tuple[PropertyValue, str, PropertyValue]] = []
        self.nodes: list[str] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        if qn := properties.get(cs.KEY_QUALIFIED_NAME):
            self.nodes.append(str(qn))

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


def _run(tmp_path: Path, src: str) -> _Capture:
    (tmp_path / "a.js").write_text(src)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return cap


def _has_inbound(cap: _Capture, target_contains: str) -> bool:
    return any(
        target_contains in str(to) and rel != str(cs.RelationshipType.DEFINES)
        for _frm, rel, to in cap.rels
    )


def test_generator_call_argument_is_referenced(tmp_path: Path) -> None:
    src = """'use strict'
function stream (g) { return g }
function main () { stream(function* () { yield 1 }) }
module.exports = { main }
"""
    cap = _run(tmp_path, src)
    gen_nodes = [n for n in cap.nodes if cs.PREFIX_ANONYMOUS in n]
    assert gen_nodes
    assert any(_has_inbound(cap, n) for n in gen_nodes)


def test_object_literal_generator_value_names_and_references(tmp_path: Path) -> None:
    src = """'use strict'
const api = {
  gen: function* () { yield 1 },
}
module.exports = { api }
"""
    cap = _run(tmp_path, src)
    assert any(str(n).endswith(".gen") for n in cap.nodes)
    assert _has_inbound(cap, ".gen")


def test_prototype_generator_assignment_names_the_method(tmp_path: Path) -> None:
    src = """'use strict'
function C () {}
C.prototype.gen = function* () { yield 1 }
function main (c) { return c.gen() }
module.exports = { C, main }
"""
    cap = _run(tmp_path, src)
    assert any(str(n).endswith("C.gen") for n in cap.nodes)
