# Covers Python L1 node SPAN (end_line) validation: cgr's end_line for each
# Class/Function/Method is graded against the ast oracle (node.end_lineno) via
# score(), joined on (kind, file, start). Exercises a decorated def, a property,
# an async signature, and a nested function so spans are not single line.
from __future__ import annotations

from pathlib import Path

from evals import constants as ec
from evals.ast_oracle import extract_oracle_graph
from evals.cgr_graph import extract_cgr_graph
from evals.score import score

PY_SRC = '''\
import functools


@functools.cache
def decorated(
    a: int,
    b: int,
) -> int:
    return a + b


class Widget:
    """doc."""

    @property
    def size(self) -> int:
        return self._n

    async def fetch(
        self,
        url: str,
    ) -> str:
        return await call(url)


def outer():
    def inner():
        return 1

    return inner
'''


def test_cgr_matches_ast_oracle_on_python_node_spans(tmp_path: Path) -> None:
    project = tmp_path / "py_span"
    project.mkdir()
    (project / "m.py").write_text(PY_SRC, encoding="utf-8")

    cgr = extract_cgr_graph(project, project.name)
    oracle = extract_oracle_graph(project, project.name)

    result = score(cgr, oracle)
    span_rows = {
        row["label"]: row
        for row in result.rows
        if row["category"] == ec.Category.SPAN.value
    }
    # score() must now emit graded span rows for Class/Function/Method.
    assert span_rows, [r["category"] for r in result.rows]
    aggregate = span_rows.get(ec.AGGREGATE_LABEL)
    assert aggregate is not None, span_rows
    assert aggregate["precision"] == 1.0 and aggregate["recall"] == 1.0, (
        aggregate,
        result.diff,
    )
    assert aggregate["tp"] >= 5, aggregate
