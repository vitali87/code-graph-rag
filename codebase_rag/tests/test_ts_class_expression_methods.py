# A method of an anonymous CLASS EXPRESSION bound to a field or variable
# (`private static readonly Proxy = class { exclude() {...} }`) must get a
# caller pass so calls and inline callbacks inside its body emit edges. Without
# the binding-name recovery the class expression is skipped in the caller pass
# (no `@name`), its whole body goes unscanned, and `cgr dead-code` false-flags
# every callback inside it. Found dogfooding NestJS `MiddlewareBuilder.ConfigProxy`
# (issue #970: `exclude` / `getRoutesFlatList` / `removeOverlappedRoutes`
# callbacks + `isOverlapped`).
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"

# A field-bound class expression (the NestJS shape).
FIELD_CLASS_SRC = """function useX(v: number): number { return v; }

export class Outer {
  private static readonly Proxy = class {
    public exclude(routes: number[]): number {
      return routes.reduce((a: number, r: number) => useX(a + r), 0);
    }
  };
}
"""

# A variable-bound class expression (the plain shape).
VAR_CLASS_SRC = """function useX(v: number): number { return v; }

const Proxy = class {
  run(xs: number[]): number {
    return xs.reduce((a: number, r: number) => useX(a + r), 0);
  }
};
"""

# A block-body callback inside a field-bound class-expression method: the exact
# NestJS `MiddlewareBuilder.ConfigProxy` shape whose callbacks reported dead.
BLOCK_CALLBACK_SRC = """function isOverlapped(a: number, b: number): boolean {
  return a === b;
}

export class Outer {
  private static readonly Proxy = class {
    public exclude(routes: number[]): number[] {
      return routes.filter((r: number) => {
        return isOverlapped(r, r);
      });
    }
  };
}
"""

# A class-expression method whose body holds ONLY a callback and NO call
# expression: the whole file has no call nodes, so the caller pass must still run
# for the callback to get a REFERENCES edge (else it is dead-flagged).
NO_CALL_CALLBACK_SRC = """const Proxy = class {
  run(): () => number {
    return () => 1;
  }
};
"""


class _Capture:
    def __init__(self) -> None:
        self.rels: list[tuple[PropertyValue, str, PropertyValue]] = []
        self.nodes: list[tuple[str, PropertyValue]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        self.nodes.append((label, properties.get(cs.KEY_QUALIFIED_NAME)))

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
    return cap


def _calls_leaf(cap: _Capture, leaf: str) -> bool:
    calls = str(cs.RelationshipType.CALLS)
    return any(
        rel == calls and str(to).rsplit(".", 1)[-1] == leaf
        for _frm, rel, to in cap.rels
    )


def _qns(cap: _Capture) -> set[str]:
    return {str(q) for _label, q in cap.nodes if q is not None}


def _has_rel(cap: _Capture, frm: str, rel_type: str, to: str) -> bool:
    rel = str(rel_type)
    return any(str(f) == frm and r == rel and str(t) == to for f, r, t in cap.rels)


class TestClassExpressionMethods:
    def test_field_bound_class_expression_body_is_walked(self, tmp_path: Path) -> None:
        # The `useX` call inside the reduce callback inside `exclude` must emit
        # a CALLS edge FROM the binding-qualified method scope; today the class
        # expression is skipped and it does not.
        cap = _run(tmp_path, FIELD_CLASS_SRC)
        assert _calls_leaf(cap, "useX")
        assert _has_rel(
            cap,
            f"{PROJECT}.m.Outer.Proxy.exclude",
            cs.RelationshipType.CALLS,
            f"{PROJECT}.m.useX",
        )

    def test_var_bound_class_expression_body_is_walked(self, tmp_path: Path) -> None:
        cap = _run(tmp_path, VAR_CLASS_SRC)
        assert _calls_leaf(cap, "useX")
        assert _has_rel(
            cap,
            f"{PROJECT}.m.Proxy.run",
            cs.RelationshipType.CALLS,
            f"{PROJECT}.m.useX",
        )

    def test_callback_referenced_when_file_has_no_call_nodes(
        self, tmp_path: Path
    ) -> None:
        # A file with no call expressions must still run the caller pass so a
        # class-expression method's inline callback gets a REFERENCES edge and is
        # not dead-flagged.
        cap = _run(tmp_path, NO_CALL_CALLBACK_SRC)
        method = f"{PROJECT}.m.Proxy.run"
        callbacks = {q for q in _qns(cap) if q.startswith(f"{method}.anonymous")}
        assert callbacks, f"no callback node; qns={_qns(cap)}"
        callback = callbacks.pop()
        assert _has_rel(cap, method, cs.RelationshipType.REFERENCES, callback)

    def test_block_callback_referenced_from_proxy_method(self, tmp_path: Path) -> None:
        # The block-body callback must register under the Proxy-qualified method
        # scope AND get a REFERENCES edge from it (so it is reachable, not dead).
        cap = _run(tmp_path, BLOCK_CALLBACK_SRC)
        method = f"{PROJECT}.m.Outer.Proxy.exclude"
        callbacks = {q for q in _qns(cap) if q.startswith(f"{method}.anonymous")}
        assert callbacks, f"no Proxy-scoped callback node; qns={_qns(cap)}"
        callback = callbacks.pop()
        assert _has_rel(cap, method, cs.RelationshipType.REFERENCES, callback)
        assert _calls_leaf(cap, "isOverlapped")
