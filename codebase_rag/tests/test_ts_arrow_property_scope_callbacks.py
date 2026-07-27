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

# The `used` callback sits at row 3 in each source, inside the factory body.
CALLBACK_ROW = 3

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


def _callback_is_referenced(tmp_path: Path, src: str) -> bool:
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
    # Any Function/Method node whose body starts on the callback row.
    referenced = {str(to) for frm, rel, to in cap.rels if rel in ref_rels}
    # The `shape` callback registers either by key (`...shape`) or by position
    # (`...anonymous_<row>_<col>`); accept both, but it must be referenced.
    for qn in referenced:
        leaf = qn.rsplit(".", 1)[-1]
        if leaf.startswith("shape") or leaf.startswith(
            f"{cs.PREFIX_ANONYMOUS}{CALLBACK_ROW}_"
        ):
            return True
    return False


class TestArrowPropertyScopeCallbacks:
    def test_method_body_callback_referenced(self, tmp_path: Path) -> None:
        # Control: a regular method already references its body callbacks.
        assert _callback_is_referenced(tmp_path, METHOD_SRC)

    def test_arrow_property_body_callback_referenced(self, tmp_path: Path) -> None:
        assert _callback_is_referenced(tmp_path, ARROW_PROPERTY_SRC)

    def test_cast_wrapped_arrow_property_body_callback_referenced(
        self, tmp_path: Path
    ) -> None:
        assert _callback_is_referenced(tmp_path, CAST_WRAPPED_ARROW_PROPERTY_SRC)
