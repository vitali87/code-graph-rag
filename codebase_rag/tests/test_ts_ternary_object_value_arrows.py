# An inline arrow that is a BRANCH of a ternary used as an object-property value
# (`{ catchValue: cond ? x : () => fallback }`) is stored and invoked later, so
# it must be referenced, not reported dead. cgr already handled a bare or
# parenthesised arrow object value; only the ternary branch leaked. Found
# dogfooding `cgr dead-code` on zod (`new ZodCatch({ catchValue: typeof c ===
# "function" ? c : () => c })`).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"

# Object literal passed to a `new` of a first-party class.
NEW_EXPR_SRC = """class C { constructor(cfg: any) {} }
export function reachable(x: any) {
  return new C({ cb: (typeof x === "function" ? x : () => { fallback(); }) });
}
"""

# Object literal passed as a plain call argument.
CALL_ARG_SRC = """export function reachable(x: any) {
  return callFoo({ cb: (typeof x === "function" ? x : () => { fallback(); }) });
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


def _anonymous_arrow_referenced(tmp_path: Path, src: str) -> bool:
    # Each source declares exactly ONE inline arrow (the ternary's alternate
    # branch); assert it receives a reference edge.
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
    ref_rels = {
        str(cs.RelationshipType.REFERENCES),
        str(cs.RelationshipType.CALLS),
    }
    return any(
        rel in ref_rels and cs.PREFIX_ANONYMOUS in str(to).rsplit(".", 1)[-1]
        for _frm, rel, to in cap.rels
    )


class TestTernaryObjectValueArrows:
    @pytest.mark.parametrize(
        "src", [NEW_EXPR_SRC, CALL_ARG_SRC], ids=["new_expr", "call_arg"]
    )
    def test_ternary_branch_arrow_referenced(self, tmp_path: Path, src: str) -> None:
        assert _anonymous_arrow_referenced(tmp_path, src)
