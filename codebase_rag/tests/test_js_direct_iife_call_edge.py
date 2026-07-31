# An unparenthesised direct IIFE (`const a = function () {...}()`) mints an
# `iife_direct_R_C` node on the definition side; the call side must produce
# the same name and resolve it, or every such node is an orphan reported
# dead. Applies equally to generator IIFEs (issue #994 follow-on).
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


def _assert_single_direct_iife_called(cap: _Capture) -> None:
    iife_nodes = [n for n in cap.nodes if cs.PREFIX_IIFE_DIRECT in n]
    assert len(iife_nodes) == 1
    assert any(
        rel == str(cs.RelationshipType.CALLS) and str(to) == iife_nodes[0]
        for _frm, rel, to in cap.rels
    )


def test_direct_function_iife_gets_module_call_edge(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x }
const b = function () { return helper(2) }()
module.exports = { b }
"""
    _assert_single_direct_iife_called(_run(tmp_path, src))


def test_direct_generator_iife_gets_module_call_edge(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x }
const a = function* () { yield helper(1) }()
module.exports = { a }
"""
    _assert_single_direct_iife_called(_run(tmp_path, src))
