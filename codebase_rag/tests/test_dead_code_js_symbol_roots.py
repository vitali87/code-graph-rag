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


def test_spaced_and_bracket_notation_variants_are_roots() -> None:
    # `[ Symbol.iterator ]` and `[Symbol["iterator"]]` are the same runtime
    # protocol member spelled differently; formatting must not decide.
    dead = _collect(
        [
            _method(
                "proj.a.Spaced.[ Symbol.iterator ]",
                "[ Symbol.iterator ]",
                "a/s.ts",
            ),
            _method(
                'proj.a.Bracketed.[Symbol["iterator"]]',
                '[Symbol["iterator"]]',
                "a/b.ts",
            ),
        ]
    )
    assert not dead


def test_tab_and_newline_formatting_variants_are_roots() -> None:
    # Formatters can put any whitespace inside the computed brackets; tabs and
    # line terminators must normalise away exactly like spaces.
    dead = _collect(
        [
            _method(
                "proj.a.Tabbed.[\tSymbol.iterator\t]",
                "[\tSymbol.iterator\t]",
                "a/t.ts",
            ),
            _method(
                "proj.a.Wrapped.[\n  Symbol.asyncIterator\n]",
                "[\n  Symbol.asyncIterator\n]",
                "a/w.js",
            ),
        ]
    )
    assert not dead


def test_symbol_registry_members_are_roots() -> None:
    # `Symbol.for(...)` registry symbols are invoked by runtime consumers the
    # graph cannot see (Node's util.inspect invokes
    # `Symbol.for('nodejs.util.inspect.custom')` members). The predicate is
    # deliberately any-Symbol-access, not an allowlist of the well-known set.
    dead = _collect(
        [
            _method(
                "proj.a.C.[Symbol.for('nodejs.util.inspect.custom')]",
                "[Symbol.for('nodejs.util.inspect.custom')]",
                "a/c.js",
            )
        ]
    )
    assert not dead


def test_near_miss_names_still_report() -> None:
    # A STRING key spelling `'Symbol.fake'` and a user symbol variable are
    # ordinary members: no runtime protocol invokes them.
    dead = _collect(
        [
            _method("proj.a.C.['Symbol.fake']", "['Symbol.fake']", "a/c.ts"),
            _method("proj.a.C.[mySym]", "[mySym]", "a/c.ts"),
        ]
    )
    assert "proj.a.C.['Symbol.fake']" in dead
    assert "proj.a.C.[mySym]" in dead
