# A `this.method()` self-dispatch inside a NESTED arrow must anchor `this` to the
# enclosing class, so a method called only from such a position is not dead. The
# JS/TS `this.M` resolver climbed only one scope level (landing on the class for
# a direct method but on the method for a nested arrow), then fell back to a
# bare-name lookup that, under cross-class same-name ambiguity, drops the edge.
# Found dogfooding NestJS `ListenersController.getContextId` (issue #971): it has
# 5 same-named siblings across classes, and the real self-call sits inside a
# nested arrow, so it was reported dead.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"

# Two classes each declare `getContextId` (the cross-class ambiguity), and A's
# self-call sits inside a nested arrow bound to a local const.
AMBIGUOUS_NESTED_ARROW_SRC = """export class Other {
  getContextId(x: unknown): number { return 2; }
}

export class A {
  private getContextId(x: unknown): number { return 1; }

  public make(): (...args: unknown[]) => Promise<number> {
    const handler = async (...args: unknown[]) => {
      return this.getContextId(args);
    };
    return handler;
  }
}
"""

# A `this.M()` inside a PLAIN nested function (not an arrow) must NOT anchor to
# the enclosing class: JS rebinds `this` per plain-function call, so it does not
# refer to the instance. Anchoring here would fabricate a false CALLS edge.
PLAIN_NESTED_FUNCTION_SRC = """export class Other {
  getContextId(x: unknown): number { return 2; }
}

export class A {
  getContextId(x: unknown): number { return 1; }

  public make(): number {
    function inner() {
      return this.getContextId(1);
    }
    return inner();
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


def _calls_to(cap: _Capture, target_qn: str) -> bool:
    calls = str(cs.RelationshipType.CALLS)
    return any(rel == calls and str(to) == target_qn for _f, rel, to in cap.rels)


def test_this_call_in_nested_arrow_resolves_to_enclosing_class(
    tmp_path: Path,
) -> None:
    cap = _run(tmp_path, AMBIGUOUS_NESTED_ARROW_SRC)
    # The self-dispatch must land on A.getContextId (the enclosing class), not be
    # dropped as ambiguous with Other.getContextId.
    assert _calls_to(cap, f"{PROJECT}.m.A.getContextId")
    # And must NOT wrongly resolve to the sibling class's method.
    assert not _calls_to(cap, f"{PROJECT}.m.Other.getContextId")


def test_this_call_in_plain_nested_function_is_not_anchored(
    tmp_path: Path,
) -> None:
    # A plain nested `function(){}` rebinds `this` dynamically, so `this.M()`
    # inside it does NOT refer to the enclosing class instance; anchoring it to
    # the class would fabricate a false CALLS edge.
    cap = _run(tmp_path, PLAIN_NESTED_FUNCTION_SRC)
    assert not _calls_to(cap, f"{PROJECT}.m.A.getContextId")
