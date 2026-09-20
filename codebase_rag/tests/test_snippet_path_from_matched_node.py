"""The snippet lookup projects the path of the node it matched (issue #1934).

`CYPHER_FIND_BY_QUALIFIED_NAME` binds a related Module with
`OPTIONAL MATCH (m:Module)-[*]-(n)` and returned that Module's path. Two
failures follow from projecting the neighbour instead of the match:

* a Module with no path to another Module binds nothing, so `path` came back
  null and `find_code_snippet` reported `CODE_MISSING_LOCATION` for a node
  whose own `path` is set;
* `[*]` is unbounded, so `m` is whichever Module the traversal reaches, not
  necessarily the file `n` lives in. Where the matched node records its own
  path the neighbour's is not a fallback, it is a different file.

These tests evaluate the query's own `AS path` expression against a row's
bindings rather than grepping the query text. `assert "coalesce" in query`
holds for `coalesce(m.path, n.path)` as well, which leaves both failures in
place.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.cypher_queries import CYPHER_FIND_BY_QUALIFIED_NAME
from codebase_rag.schemas import CodeSnippet
from codebase_rag.tools.code_retrieval import CodeRetriever

_PATH_ALIAS = re.compile(r"\bAS\s+path\b")
_PATH_PROPERTY = re.compile(r"\b\w+\.path\b")
_ORDER_BY = re.compile(r"\bORDER\s+BY\b")
# What ends an ORDER BY. Uppercase-only, like the compatibility scanner's own
# clause split, so a lowercase property named `limit` cannot close the clause.
_CLAUSE_AFTER_ORDER_BY = re.compile(
    r"\b(SKIP|LIMIT|RETURN|WITH|MATCH|OPTIONAL|UNWIND|MERGE|DETACH|DELETE"
    r"|SET|REMOVE|CREATE|CALL|UNION)\b"
)
_COALESCE = "coalesce("


def _split_top_level(text: str) -> list[str]:
    """Split on the commas a function call does not own.

    Runs of whitespace collapse to one space so a term that wraps across lines
    compares equal to the same expression written on one.
    """
    terms: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(text):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            terms.append(text[start:index])
            start = index + 1
    terms.append(text[start:])
    return [" ".join(term.split()) for term in terms if term.strip()]


def _path_projection(query: str) -> str:
    """The expression the query returns as `path`."""
    alias = _PATH_ALIAS.search(query)
    assert alias is not None, f"no `AS path` projection in:\n{query}"
    returns_at = query.rfind("RETURN", 0, alias.start())
    assert returns_at != -1, f"`AS path` outside a RETURN in:\n{query}"
    return _split_top_level(query[returns_at + len("RETURN") : alias.start()])[-1]


def _project_path(query: str, bindings: dict[str, dict[str, str] | None]) -> str | None:
    """Evaluate the query's `AS path` expression over one row's bindings.

    Only the forms this projection can take are understood -- one
    `<variable>.<property>`, or a `coalesce` over them. An unrecognised
    expression fails here instead of evaluating to None, which the caller
    would read as a legitimately absent path and the test as the old defect.
    """
    expression = _path_projection(query)
    if expression.startswith(_COALESCE) and expression.endswith(")"):
        terms = _split_top_level(expression[len(_COALESCE) : -1])
    else:
        terms = [expression]
    for term in terms:
        variable, _, prop = term.partition(".")
        assert prop, f"unsupported term in the path projection: {term!r}"
        bound = bindings[variable]
        if bound is not None and bound.get(prop) is not None:
            return bound[prop]
    return None


def _path_order_terms(query: str) -> list[str] | None:
    """The ORDER BY terms that sort on a path property, or None if it does not order.

    The clause runs to the next Cypher clause, not to the end of its first line.
    A three-term ORDER BY wraps inside a long string literal, and a reader that
    stopped at the newline would find no path term in a query that sorts on one
    -- reporting an unguarded query as guarded.
    """
    order_by = _ORDER_BY.search(query)
    if order_by is None:
        return None
    tail = query[order_by.end() :]
    boundary = _CLAUSE_AFTER_ORDER_BY.search(tail)
    clause = tail[: boundary.start()] if boundary else tail
    return [term for term in _split_top_level(clause) if _PATH_PROPERTY.search(term)]


async def _retrieve_projected_path_snippet(
    tmp_path: Path, projected_path: str | None
) -> CodeSnippet:
    ingestor = MagicMock()
    ingestor.fetch_all.return_value = [
        {
            "name": "widget",
            "start": 1,
            "end": 3,
            "path": projected_path,
            "absolute_path": None,
            "docstring": None,
        }
    ]
    retriever = CodeRetriever(str(tmp_path), ingestor)
    return await retriever.find_code_snippet("proj.widget")


@pytest.mark.asyncio
async def test_an_isolated_module_resolves_to_its_own_path(tmp_path: Path) -> None:
    """A Module with no path to another Module leaves `m` unbound. Its own
    `path` is set, so the lookup has a usable location and must return it."""
    source = tmp_path / "widget.py"
    source.write_text("one\ntwo\nthree\n")

    path = _project_path(
        CYPHER_FIND_BY_QUALIFIED_NAME, {"n": {"path": source.name}, "m": None}
    )
    result = await _retrieve_projected_path_snippet(tmp_path, path)

    assert result.found, result.error_message
    assert result.file_path == source.name
    assert result.source_code == "one\ntwo\nthree\n"


@pytest.mark.asyncio
async def test_the_matched_node_outranks_a_related_module(tmp_path: Path) -> None:
    """Which Module `[*]` reaches is not a property of `n`'s location, so a
    node that records its own path must not be read out of a neighbour's file.

    This also pins the argument order: the reversed `coalesce(m.path, n.path)`
    satisfies the isolated-Module case above and reinstates this one.
    """
    own = tmp_path / "own.py"
    own.write_text("one\ntwo\nthree\n")
    neighbour = tmp_path / "neighbour.py"
    neighbour.write_text("four\nfive\nsix\n")

    path = _project_path(
        CYPHER_FIND_BY_QUALIFIED_NAME,
        {"n": {"path": own.name}, "m": {"path": neighbour.name}},
    )
    result = await _retrieve_projected_path_snippet(tmp_path, path)

    assert result.file_path == own.name
    assert result.source_code == "one\ntwo\nthree\n"


@pytest.mark.asyncio
async def test_a_node_without_a_path_still_uses_the_related_module(
    tmp_path: Path,
) -> None:
    """Ingestion writes `path` onto a Function or Method only when the parser
    knows both the file and the repo root, so the related Module stays the
    only location such a node has and must keep resolving."""
    source = tmp_path / "module.py"
    source.write_text("one\ntwo\nthree\n")

    path = _project_path(
        CYPHER_FIND_BY_QUALIFIED_NAME, {"n": {}, "m": {"path": source.name}}
    )
    result = await _retrieve_projected_path_snippet(tmp_path, path)

    assert result.found, result.error_message
    assert result.file_path == source.name


def test_the_order_key_agrees_with_the_projection() -> None:
    """`LIMIT 1` returns whichever row the ORDER BY put first. A sort key that
    is a different expression from the projection picks the winning row on a
    path the caller never sees; #1930 adds the ORDER BY this guards."""
    projection = _path_projection(CYPHER_FIND_BY_QUALIFIED_NAME)
    terms = _path_order_terms(CYPHER_FIND_BY_QUALIFIED_NAME)

    if terms is None:
        # The only legitimate way to have no path sort key. Checked against the
        # raw text rather than the detector that just returned None, so a clause
        # the detector cannot see is a failure instead of a silent pass.
        assert "ORDER" not in CYPHER_FIND_BY_QUALIFIED_NAME.upper(), (
            f"the query orders but the detector found no clause:\n{CYPHER_FIND_BY_QUALIFIED_NAME}"
        )
        return

    assert terms, (
        "the query orders but sorts on no path at all, so the tiebreak among "
        f"colliding rows ignores the projected path:\n{CYPHER_FIND_BY_QUALIFIED_NAME}"
    )
    assert terms == [projection], (terms, projection)


def test_the_order_key_detector_flags_a_disagreement() -> None:
    """The assertion above holds vacuously while the shipped query does not
    order, so the detector is proven on the shape it exists to catch."""
    disagreeing = (
        "MATCH (n) WHERE n.qualified_name = $qn\n"
        "OPTIONAL MATCH (m:Module)-[*]-(n)\n"
        "RETURN n.name AS name, coalesce(n.path, m.path) AS path\n"
        "ORDER BY labels(n)[0], m.path, n.start_line\n"
        "LIMIT 1\n"
    )

    assert _path_projection(disagreeing) == "coalesce(n.path, m.path)"
    assert _path_order_terms(disagreeing) == ["m.path"]


def test_the_order_key_detector_reads_past_the_first_line() -> None:
    """Three sort terms wrap in a long string literal. Stopping at the newline
    after `ORDER BY` finds no path term and reports a query that sorts on the
    wrong one as agreeing, so the whole clause has to be read (Greptile on
    #1943)."""
    wrapped = (
        "MATCH (n) WHERE n.qualified_name = $qn\n"
        "OPTIONAL MATCH (m:Module)-[*]-(n)\n"
        "RETURN n.name AS name, coalesce(n.path, m.path) AS path\n"
        "ORDER BY labels(n)[0],\n"
        "         m.path,\n"
        "         n.start_line\n"
        "LIMIT 1\n"
    )

    assert _path_order_terms(wrapped) == ["m.path"]


def test_a_wrapped_projection_matches_its_wrapped_order_key() -> None:
    """Agreement survives the line break on both sides: the same expression
    written across lines in the RETURN and in the ORDER BY must compare equal,
    or the guard reddens on formatting alone."""
    wrapped = (
        "MATCH (n) WHERE n.qualified_name = $qn\n"
        "OPTIONAL MATCH (m:Module)-[*]-(n)\n"
        "RETURN n.name AS name,\n"
        "       coalesce(n.path,\n"
        "                m.path) AS path\n"
        "ORDER BY labels(n)[0],\n"
        "         coalesce(n.path,\n"
        "                  m.path),\n"
        "         n.start_line\n"
        "LIMIT 1\n"
    )

    assert _path_order_terms(wrapped) == [_path_projection(wrapped)]


def test_the_detector_reports_an_order_by_that_sorts_on_no_path() -> None:
    """`None` means "does not order"; an empty list means "orders, but on no
    path". The agreement test fails loudly on the second and must not read it
    as the first."""
    no_path_key = (
        "MATCH (n) WHERE n.qualified_name = $qn\n"
        "RETURN n.name AS name, coalesce(n.path, m.path) AS path\n"
        "ORDER BY labels(n)[0], n.start_line\n"
        "LIMIT 1\n"
    )

    assert _path_order_terms(no_path_key) == []
    assert _path_order_terms("MATCH (n) RETURN n.path AS path\nLIMIT 1\n") is None
