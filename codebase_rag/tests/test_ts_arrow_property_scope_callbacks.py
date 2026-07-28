# A class factory written as an ARROW PROPERTY (`static create = (s) => {...}`)
# must reference the inline callbacks in its body exactly as a regular method
# does. cgr attributed those callbacks to the enclosing CLASS scope and gave the
# arrow property no caller pass, so they got no reference edge and `cgr
# dead-code` false-flagged them. Found dogfooding zod, whose factories are all
# arrow properties (`create = (...) => {...}`).
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"

# Case A: static arrow property (the zod pattern).  Case B: regular method
# (already works) — a control that must also pass.
ARROW_PROPERTY_SRC = """class Box {
  constructor(cfg: any) {}
  static make = (s: any) => {
    return new Box({ shape: () => { used(); } });
  };
}
"""

METHOD_SRC = """class Box {
  constructor(cfg: any) {}
  static make(s: any) {
    return new Box({ shape: () => { used(); } });
  }
}
"""

# The arrow property is bound THROUGH a cast wrapper (zustand's public-API
# shape). The binding name must still be recovered so the callback attributes to
# the property scope.
CAST_WRAPPED_ARROW_PROPERTY_SRC = """class Box {
  constructor(cfg: any) {}
  static make = ((s: any) => {
    return new Box({ shape: () => { used(); } });
  }) as any;
}
"""


class _Capture:
    def __init__(self) -> None:
        self.rels: list[tuple[PropertyValue, str, PropertyValue]] = []
        self.func_qns: set[str] = set()

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        if label in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
            qn = properties.get(cs.KEY_QUALIFIED_NAME)
            if qn is not None:
                self.func_qns.add(str(qn))

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


# The factory owns a callback that escapes (`shape`) AND one that is constructed
# then dropped (`unused`); the escaping one must be referenced FROM the property
# scope and the dropped one must stay dead, proving precise attribution.
PRECISION_SRC = """class Box {
  constructor(cfg: any) {}
  static make = (s: any) => {
    (() => { neverEscapes(); });
    return new Box({ shape: () => { used(); } });
  };
}
"""


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


_REF_RELS = {
    str(cs.RelationshipType.REFERENCES),
    str(cs.RelationshipType.CALLS),
}


def _is_shape_callback(qn: str) -> bool:
    # The `shape` callback registers either by its object key (`...shape`) or by
    # position (`...anonymous_<row>_<col>`) behind a cast wrapper.
    leaf = qn.rsplit(".", 1)[-1]
    return leaf.startswith("shape") or leaf.startswith(cs.PREFIX_ANONYMOUS)


def _shape_referenced_from(cap: _Capture, expected_scope_suffix: str) -> bool:
    # The escaping `shape` callback must get a REFERENCES edge (the exact edge
    # the fix emits) FROM the recovered property scope (e.g. `...Box.make`), not
    # merely a CALLS edge or reachability from anywhere.
    references = str(cs.RelationshipType.REFERENCES)
    return any(
        rel == references
        and _is_shape_callback(str(to))
        and str(frm).endswith(expected_scope_suffix)
        for frm, rel, to in cap.rels
    )


class TestArrowPropertyScopeCallbacks:
    def test_method_body_callback_referenced(self, tmp_path: Path) -> None:
        # Control: a regular method already references its body callbacks.
        assert _shape_referenced_from(_run(tmp_path, METHOD_SRC), ".Box.make")

    def test_arrow_property_body_callback_referenced(self, tmp_path: Path) -> None:
        assert _shape_referenced_from(_run(tmp_path, ARROW_PROPERTY_SRC), ".Box.make")

    def test_cast_wrapped_arrow_property_body_callback_referenced(
        self, tmp_path: Path
    ) -> None:
        cap = _run(tmp_path, CAST_WRAPPED_ARROW_PROPERTY_SRC)
        assert _shape_referenced_from(cap, ".Box.make")

    def test_dropped_sibling_callback_stays_dead(self, tmp_path: Path) -> None:
        # Precision: `shape` escapes and is referenced from `Box.make`; the
        # bare `(() => {...})` expression is constructed and dropped, so no ref
        # edge may target a Box.make callback that only calls `neverEscapes`.
        cap = _run(tmp_path, PRECISION_SRC)
        assert _shape_referenced_from(cap, ".Box.make")
        dropped = {
            str(to)
            for frm, rel, to in cap.rels
            if rel == str(cs.RelationshipType.DEFINES)
            and str(frm).endswith(".Box.make")
            and cs.PREFIX_ANONYMOUS in str(to).rsplit(".", 1)[-1]
        }
        referenced = {to for _f, rel, to in cap.rels if rel in _REF_RELS}
        assert dropped, "expected a dropped anonymous callback under Box.make"
        assert not (dropped & {str(t) for t in referenced}), (
            f"a constructed-then-dropped callback was referenced: "
            f"{dropped & {str(t) for t in referenced}}"
        )
