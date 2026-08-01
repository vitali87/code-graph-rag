# TypeScript wraps first-class function values in node types Python has no
# equivalent for, and the first-class-value machinery only recognised the Python
# shapes. So an inline arrow reached through a TS cast (`(() => {}) as any`), a
# nullish/logical operator (`fn ?? (() => {})`), or a ternary on a
# variable-declaration RHS was never referenced and `cgr dead-code` false-flagged
# it. Found dogfooding zod (`catchValue: (... ? ... : () => ...) as any`,
# `fn ?? (() => true)`, `const f = typeof d === "function" ? d : () => d`).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"

CASES = {
    # arrow reached through a nullish-coalescing operator, as a call argument
    "nullish_arg": """export function reachable(fn: any) {
  return callFoo(fn ?? (() => { used(); }));
}
""",
    # arrow reached through `??`, as an object-property value
    "nullish_obj_value": """class C { constructor(cfg: any) {} }
export function reachable(o: any) {
  return new C({ cb: (o as any) ?? (() => { used(); }) });
}
""",
    # a cast-wrapped inline arrow object value
    "cast_arrow_obj_value": """class C { constructor(cfg: any) {} }
export function reachable() {
  return new C({ cb: ((i: any) => { used(); }) as any });
}
""",
    # a cast-wrapped ternary object value
    "cast_ternary_obj_value": """class C { constructor(cfg: any) {} }
export function reachable(x: any) {
  return new C({ cb: (typeof x === "function" ? x : () => { used(); }) as any });
}
""",
    # a ternary on a variable-declaration RHS
    "var_decl_ternary": """class C { constructor(cfg: any) {} }
export function reachable(x: any) {
  const cb = typeof x === "function" ? x : () => { used(); };
  return new C({ cb });
}
""",
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


def _anonymous_arrow_referenced(tmp_path: Path, src: str) -> bool:
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


class TestTsCastLogicalValueWrappers:
    @pytest.mark.parametrize("case", sorted(CASES))
    def test_wrapped_arrow_referenced(self, tmp_path: Path, case: str) -> None:
        assert _anonymous_arrow_referenced(tmp_path, CASES[case])


# Go also spells `a || b` as `binary_expression`, but its operands are booleans,
# not callables. Expanding them would fabricate a phantom REFERENCES edge to any
# function whose name collides with a bool local/param. The short-circuit
# expansion must be gated to JS/TS.
GO_SRC = """package main

func ready() {}

func check(ready bool) bool { return ready || ready }

func flag() {}

func use(b bool) {}

func run(flag bool) { use(flag || flag) }
"""


def test_go_short_circuit_operands_not_referenced(tmp_path: Path) -> None:
    (tmp_path / "m.go").write_text(GO_SRC)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    phantom = [
        (frm, to)
        for frm, rel, to in cap.rels
        if rel == str(cs.RelationshipType.REFERENCES)
        and (str(to).endswith(".ready") or str(to).endswith(".flag"))
    ]
    assert not phantom, f"Go `||` operand fabricated a phantom reference: {phantom}"
