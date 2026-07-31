# Class members keyed by well-known symbols (`get [Symbol.toStringTag] ()`,
# `[Symbol.iterator] ()`) are invoked implicitly by the JavaScript runtime;
# no source-level call site can exist, so they are reachability roots, not
# dead code. Found dogfooding fastify (`ContentType.[Symbol.toStringTag]`).
# Issue #993.
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.types_defs import ResultRow

_METHOD = cs.NodeLabel.METHOD.value


class FakeIngestor:
    def __init__(self, nodes: list[ResultRow], rels: list[ResultRow]) -> None:
        self._nodes = nodes
        self._rels = rels

    def fetch_all(
        self, query: str, params: dict[str, str] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return self._nodes
        return self._rels


def _method(qn: str, name: str, path: str) -> ResultRow:
    return {
        "label": _METHOD,
        "qualified_name": qn,
        "name": name,
        "path": path,
        "start_line": 1,
        "end_line": 2,
        "decorators": [],
        "is_exported": False,
        "overrides_external": False,
    }


def _collect(nodes: list[ResultRow]) -> set[str]:
    rows = collect_dead_code(
        FakeIngestor(nodes, []),
        "proj",
        default_dead_code_config(include_tests=True, include_classes=False),
    )
    return {row["qualified_name"] for row in rows}


def test_well_known_symbol_members_are_roots() -> None:
    dead = _collect(
        [
            _method(
                "proj.ct.ContentType.[Symbol.toStringTag]",
                "[Symbol.toStringTag]",
                "lib/content-type.js",
            ),
            _method(
                "proj.ct.Box.[Symbol.iterator]",
                "[Symbol.iterator]",
                "lib/box.ts",
            ),
        ]
    )
    assert "proj.ct.ContentType.[Symbol.toStringTag]" not in dead
    assert "proj.ct.Box.[Symbol.iterator]" not in dead


def test_ordinary_uncalled_method_still_reports() -> None:
    dead = _collect(
        [_method("proj.ct.ContentType.parse", "parse", "lib/content-type.js")]
    )
    assert "proj.ct.ContentType.parse" in dead


def test_symbol_name_on_non_js_path_still_reports() -> None:
    # The bracket pattern is JS/TS syntax; a same-named symbol elsewhere is
    # ordinary code.
    dead = _collect(
        [_method("proj.x.C.[Symbol.toStringTag]", "[Symbol.toStringTag]", "x/c.py")]
    )
    assert "proj.x.C.[Symbol.toStringTag]" in dead
