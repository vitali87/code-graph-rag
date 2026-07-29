# A `this.method` reference in a JSX attribute value is referenced when written
# directly, and must ALSO be referenced when it sits inside a ternary
# (`onDrop={cond ? this.handleDrop : undefined}`), or the handler reports dead.
# Found dogfooding excalidraw App.handleAppOnDrop (issue #980).
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"

SRC = """import React from "react";
export class Widget extends React.Component {
  handleClick = () => {};
  handleDrop = () => {};
  render() {
    return (
      <div
        onClick={this.handleClick}
        onDrop={cond ? this.handleDrop : undefined}
      />
    );
  }
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


def _refs_leaf(cap: _Capture, leaf: str) -> bool:
    ref = str(cs.RelationshipType.REFERENCES)
    calls = str(cs.RelationshipType.CALLS)
    return any(
        rel in (ref, calls) and str(to).rsplit(".", 1)[-1] == leaf
        for _frm, rel, to in cap.rels
    )


def _run(tmp_path: Path) -> _Capture:
    (tmp_path / "w.tsx").write_text(SRC)
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


def test_direct_jsx_handler_is_referenced(tmp_path: Path) -> None:
    # The already-working direct case (guards against regression).
    assert _refs_leaf(_run(tmp_path), "handleClick")


def test_ternary_jsx_handler_is_referenced(tmp_path: Path) -> None:
    # `cond ? this.handleDrop : undefined` must reference handleDrop.
    assert _refs_leaf(_run(tmp_path), "handleDrop")


SHORT_CIRCUIT_SRC = """import React from "react";
function guard() { return true; }
export class W extends React.Component {
  handleClick = () => {};
  render() {
    return <div onClick={guard && this.handleClick}>x</div>;
  }
}
"""


def test_short_circuit_and_left_operand_is_not_referenced(tmp_path: Path) -> None:
    # In `guard && this.handleClick` the LEFT operand `guard` is only
    # truthiness-tested, never the bound value, so it must NOT be referenced
    # (a same-named module function would otherwise be falsely revived); the
    # RIGHT operand `this.handleClick` IS the value and must be referenced.
    (tmp_path / "w.tsx").write_text(SHORT_CIRCUIT_SRC)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    assert _refs_leaf(cap, "handleClick")  # right operand = the value
    assert not _refs_leaf(cap, "guard")  # left operand = truthiness only
