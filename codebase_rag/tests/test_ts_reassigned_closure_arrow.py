# An arrow assigned to an outer closure variable inside a nested object-literal
# method is invoked indirectly through that variable elsewhere (a `render` that
# calls `getText()`), so it is live. The reassignment references the arrow, but
# the arrow's registered qn nests one level deeper than the object method's flat
# qn (`p.c.make.update.anonymous_R_C` vs caller `p.c.update`), so the
# caller-scope-prefixed position lookup missed it and the arrow reported dead.
# Found dogfooding TypeORM (`packages/codemod/src/lib/spinner.ts`). Issue #985.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"
_REFERENCES = str(cs.RelationshipType.REFERENCES)


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
    (tmp_path / "c.ts").write_text(src)
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


def _reassigned_arrow_referenced(cap: _Capture, method: str) -> bool:
    # The reassigned arrow is anonymous and registered under the object method's
    # scope; a REFERENCES edge into `...<method>...anonymous_R_C` keeps it live.
    return any(
        rel == _REFERENCES
        and f"{cs.SEPARATOR_DOT}{method}{cs.SEPARATOR_DOT}" in str(to)
        and cs.PREFIX_ANONYMOUS in str(to)
        for _frm, rel, to in cap.rels
    )


def test_reassigned_closure_arrow_in_object_method_is_referenced(
    tmp_path: Path,
) -> None:
    src = """export const createSpinner = (text: string | (() => string)) => {
  let getText = typeof text === "function" ? text : () => text
  const render = () => { process.stderr.write(`${getText()}`) }
  render()
  return {
    update(newText: string | (() => string)) {
      getText = typeof newText === "function" ? newText : () => newText
    },
  }
}
"""
    assert _reassigned_arrow_referenced(_run(tmp_path, src), "update")


def test_plain_reassigned_arrow_in_object_method_is_referenced(
    tmp_path: Path,
) -> None:
    # The same defect without the ternary: a bare arrow reassigned to the outer
    # closure variable inside the nested method must still be referenced.
    src = """export const make = () => {
  let g = () => 1
  const run = () => g()
  run()
  return { update() { g = () => 2 } }
}
"""
    assert _reassigned_arrow_referenced(_run(tmp_path, src), "update")


def _edges_to_leaf(cap: _Capture, leaf: str) -> set[tuple[str, str, str]]:
    return {
        (str(frm), rel, str(to))
        for frm, rel, to in cap.rels
        if str(to).rsplit(cs.SEPARATOR_DOT, 1)[-1] == leaf
    }


def test_nested_named_function_expression_in_array_links_to_itself(
    tmp_path: Path,
) -> None:
    # A NESTED named function-expression whose name collides with a module-level
    # function must reference ITSELF (its lexical scope), never the unrelated
    # top-level function. Resolving by name alone would emit `p.c.update
    # REFERENCES p.c.handler` (the wrong, top-level node) and falsely revive it.
    src = """export function handler() { return 1 }
export const make = () => {
  return { update() { const arr = [function handler() { return 2 }]; return arr } }
}
"""
    edges = _edges_to_leaf(_run(tmp_path, src), "handler")
    refs = {(f, t) for f, rel, t in edges if rel == _REFERENCES}
    # The correct nested node is referenced; the top-level namesake is not.
    assert any(t.endswith(".update.handler") for _f, t in refs)
    assert not any(t == "p.c.handler" for _f, t in refs)


def test_nested_named_function_expression_call_arg_links_to_itself(
    tmp_path: Path,
) -> None:
    # Same collision on the direct call-argument path, which also emits a CALLS
    # edge: neither the CALLS nor REFERENCES edge may target the top-level
    # `handler`.
    src = """export function handler() { return 1 }
export const make = () => {
  return {
    update() { window.addEventListener("click", function handler() { return 2 }) }
  }
}
"""
    edges = _edges_to_leaf(_run(tmp_path, src), "handler")
    # DEFINES `p.c handler -> p.c.handler` is legitimate module containment; only
    # a CALLS/REFERENCES edge onto the top-level namesake would falsely revive it.
    call_ref = {
        (f, rel, t) for f, rel, t in edges if rel != str(cs.RelationshipType.DEFINES)
    }
    assert not any(t == "p.c.handler" for _f, _rel, t in call_ref)
    assert any(t.endswith(".update.handler") for _f, _rel, t in call_ref)
