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


# A short-circuit in a JSX DATA prop selects data, not a handler; its operands
# must not be peeled, or a same-named module function is false-revived.
DATA_PROP_SHORT_CIRCUIT_SRC = """import React from "react";
function profile() { return 1; }
export class W extends React.Component {
  render() {
    return <img alt={displayName || profile} title={ready && profile} />;
  }
}
"""


def test_short_circuit_data_prop_does_not_reference_module_function(
    tmp_path: Path,
) -> None:
    # `alt={displayName || profile}` / `title={ready && profile}` select DATA;
    # a JSX short-circuit must NOT be peeled, or `profile` (a same-named module
    # function) is falsely revived (adversarial review of #980).
    (tmp_path / "w.tsx").write_text(DATA_PROP_SHORT_CIRCUIT_SRC)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    assert not _refs_leaf(cap, "profile")
