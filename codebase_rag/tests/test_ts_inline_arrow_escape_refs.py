# An inline arrow function that ESCAPES its definition site (passed, assigned,
# returned, placed in a collection) is invoked later and must not be reported as
# dead code. cgr already referenced arrows in most positions, but two leaked and
# were false-flagged by `cgr dead-code` on zod:
#   1. an arrow inside an ARRAY literal   (`x.onattach = [(i) => {...}]`)
#   2. an arrow on the RHS of an AUGMENTED assignment (`x.when ??= (p) => {...}`)
# Each escaping arrow must receive a REFERENCES (or CALLS) edge from its
# enclosing scope so the reachability walk keeps it live. Arrows are identified
# by their source ROW, which the definition pass encodes into the anonymous
# qualified name (`...anonymous_<row>_<col>`).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"

# One escaping arrow per line; the dict maps a label to that arrow's 0-indexed
# source row. The trailing `discarded` arrow does NOT escape and must stay dead.
SRC_LINES = [
    "export function reachable() {",  # row 0
    "  obj.list = [() => { arrLit(); }];",  # row 1  arrLit  (array assigned to member)
    "  obj.when ??= (p) => { augWhen(); };",  # row 2  augWhen (augmented ??=)
    "  obj.or ||= (p) => { augOr(); };",  # row 3  augOr   (augmented ||=)
    "  callFoo([() => { arrArg(); }]);",  # row 4  arrArg  (array as call arg)
    "  callFoo({ items: [() => { nested(); }] });",  # row 5  nested (array in object arg)
    "  const local = { list: [() => { objArr(); }] };",  # row 6  objArr (array in object const)
    "  (() => { discarded(); });",  # row 7  discarded (constructed, dropped)
    "}",
]
SRC = "\n".join(SRC_LINES) + "\n"

ESCAPING_ROWS = {
    "arrLit": 1,
    "augWhen": 2,
    "augOr": 3,
    "arrArg": 4,
    "nested": 5,
    "objArr": 6,
}
DISCARDED_ROW = 7


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


def _referenced_rows(tmp_path: Path) -> set[int]:
    """Source rows of the anonymous arrows that receive a REFERENCES/CALLS edge."""
    (tmp_path / "m.ts").write_text(SRC)
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
    referenced_anon = {
        str(to)
        for frm, rel, to in cap.rels
        if rel in ref_rels and cs.PREFIX_ANONYMOUS in str(to).rsplit(".", 1)[-1]
    }
    rows: set[int] = set()
    for qn in referenced_anon:
        leaf = qn.rsplit(".", 1)[-1]
        # ...anonymous_<row>_<col>
        row_col = leaf[len(cs.PREFIX_ANONYMOUS) :]
        if "_" in row_col and row_col.split("_", 1)[0].isdigit():
            rows.add(int(row_col.split("_", 1)[0]))
    return rows


class TestInlineArrowEscapeRefs:
    @pytest.mark.parametrize("label", sorted(ESCAPING_ROWS))
    def test_escaping_arrow_is_referenced(self, tmp_path: Path, label: str) -> None:
        rows = _referenced_rows(tmp_path)
        assert ESCAPING_ROWS[label] in rows, (
            f"arrow {label!r} (row {ESCAPING_ROWS[label]}) escaped its definition "
            f"site but got no REFERENCES edge; referenced rows={sorted(rows)}"
        )

    def test_discarded_arrow_stays_dead(self, tmp_path: Path) -> None:
        rows = _referenced_rows(tmp_path)
        # An arrow that is constructed and immediately dropped never escapes, so
        # it correctly receives no reference and remains a dead-code candidate.
        assert DISCARDED_ROW not in rows, (
            f"discarded arrow (row {DISCARDED_ROW}) does not escape and must not "
            f"be referenced; referenced rows={sorted(rows)}"
        )
