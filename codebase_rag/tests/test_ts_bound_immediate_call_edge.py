# A locally-defined function invoked immediately through `fn.bind(this)()`
# must record a CALLS edge to `fn`, exactly like the plain `fn()` form; without
# it the function reports as dead code. Found dogfooding TypeORM
# JoinAttribute.relation, whose local `getValue` is called as
# `getValue.bind(this)()` (issue: TS bound-then-invoked call loses its edge).
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"

SRC = """export class C {
  get direct() {
    const getValue = () => { return 1; };
    return getValue();
  }
  get bound() {
    const getValue = () => { return 2; };
    return getValue.bind(this)();
  }
  get chained() {
    const getValue = () => { return 3; };
    return getValue.bind(this).bind(this)();
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


def _calls_leaf_under(cap: _Capture, scope: str, leaf: str) -> bool:
    calls = str(cs.RelationshipType.CALLS)
    return any(
        rel == calls
        and str(frm).rsplit(".", 1)[-1] == scope
        and str(to).rsplit(".", 1)[-1] == leaf
        for frm, rel, to in cap.rels
    )


def _run(tmp_path: Path) -> _Capture:
    (tmp_path / "c.ts").write_text(SRC)
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


def test_direct_local_call_has_edge(tmp_path: Path) -> None:
    # Guards the already-working plain `getValue()` form against regression.
    assert _calls_leaf_under(_run(tmp_path), "direct", "getValue")


def test_bound_immediate_local_call_has_edge(tmp_path: Path) -> None:
    # `getValue.bind(this)()` invokes getValue and must record the CALLS edge.
    assert _calls_leaf_under(_run(tmp_path), "bound", "getValue")


def test_chained_bound_local_call_has_edge(tmp_path: Path) -> None:
    # `getValue.bind(this).bind(this)()` still invokes getValue; the bound
    # callee must be peeled to a fixpoint, not a single pass.
    assert _calls_leaf_under(_run(tmp_path), "chained", "getValue")


# A plain call-of-a-call `getFactory()()` invokes getFactory (inner call) and
# then its RETURN value; the outer callee is not nameable, so it must NOT be
# treated as a bound call. getFactory still links from the inner call node.
CALL_OF_CALL_SRC = """export class D {
  run() {
    const getFactory = () => () => 1;
    return getFactory()();
  }
}
"""


def test_call_of_call_still_links_inner_only(tmp_path: Path) -> None:
    (tmp_path / "c.ts").write_text(CALL_OF_CALL_SRC)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    # The inner `getFactory()` records the call; the outer `()` names nothing.
    assert _calls_leaf_under(cap, "run", "getFactory")
