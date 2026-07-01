from pathlib import Path

import pytest

from evals import constants as ec
from evals.oracles import scala_available
from evals.scala_retrieval import (
    cgr_scala_call_edges,
    oracle_scala_call_edges,
    score_scala_retrieval,
)

needs_scala = pytest.mark.skipif(
    not scala_available(), reason="scala-cli toolchain not installed"
)


def _make_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Util.scala").write_text(
        "object Util {\n  def free(): Int = 2\n}\n",
        encoding="utf-8",
    )
    (root / "T.scala").write_text(
        "class T {\n"
        "  def helper(): Int = 1\n"
        "  def caller(): Int = this.helper()\n"
        "  def orphan(): Int = 9\n"
        "  def ~>(o: T): T = o\n"
        "  def done: Boolean = true\n"
        "}\n"
        "object T {\n"
        "  def make(): T = new T()\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "Use.scala").write_text(
        "object Use {\n"
        "  def useIt(): Int = {\n"
        "    val t = T.make()\n"
        "    val u = t ~> T.make()\n"
        "    val d = u.done\n"
        "    Util.free() + t.caller()\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )


@needs_scala
def test_oracle_captures_first_party_scala_calls(tmp_path: Path) -> None:
    _make_project(tmp_path)
    edges, declared, covered = oracle_scala_call_edges(tmp_path)

    # (H) this.helper(), T.make(), Util.free(), t.caller() are first-party calls.
    assert ("T.scala", "helper") in edges
    assert ("Use.scala", "make") in edges
    assert ("Use.scala", "free") in edges
    assert ("Use.scala", "caller") in edges
    # (H) An infix operator call (t ~> ...) and a nullary field-expression call
    # (H) (u.done, no parens) are first-party calls too.
    assert ("Use.scala", "~>") in edges
    assert ("Use.scala", "done") in edges
    # (H) orphan is declared but never called -> never a call edge.
    assert ("T.scala", "orphan") not in edges
    assert {
        "helper",
        "caller",
        "make",
        "free",
        "orphan",
        "useIt",
        "~>",
        "done",
    } <= declared
    assert {"Util.scala", "T.scala", "Use.scala"} <= covered


@needs_scala
def test_cgr_matches_oracle_on_clean_scala_project(tmp_path: Path) -> None:
    _make_project(tmp_path)
    oracle, declared, covered = oracle_scala_call_edges(tmp_path)
    cgr = cgr_scala_call_edges(tmp_path, tmp_path.name, declared, covered)
    assert cgr == oracle


def test_score_scala_retrieval_prf() -> None:
    result = score_scala_retrieval(
        {("A.scala", "f"), ("A.scala", "g")}, {("A.scala", "f"), ("B.scala", "h")}
    )
    row = next(r for r in result.rows if r["label"] == ec.SCALA_RETRIEVAL_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
