from pathlib import Path

import pytest

from evals import constants as ec
from evals.java_retrieval import (
    cgr_java_call_edges,
    oracle_java_call_edges,
    score_java_retrieval,
)
from evals.oracles import java_available

needs_java = pytest.mark.skipif(
    not java_available(), reason="java toolchain not installed"
)


def _make_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Util.java").write_text(
        "class Util {\n    static int free() { return 2; }\n}\n",
        encoding="utf-8",
    )
    (root / "T.java").write_text(
        "class T {\n"
        "    int helper() { return 1; }\n"
        "    int caller() { return this.helper(); }\n"
        "    static T make() { return new T(); }\n"
        "    int orphan() { return 9; }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "Use.java").write_text(
        "class Use {\n"
        "    int useIt() {\n"
        "        T t = T.make();\n"
        "        return Util.free() + t.caller();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )


@needs_java
def test_oracle_captures_first_party_java_calls(tmp_path: Path) -> None:
    _make_project(tmp_path)
    edges, declared = oracle_java_call_edges(tmp_path)

    # (H) this.helper(), T.make(), Util.free(), t.caller() are first-party calls.
    assert ("T.java", "helper") in edges
    assert ("Use.java", "make") in edges
    assert ("Use.java", "free") in edges
    assert ("Use.java", "caller") in edges
    # (H) orphan is declared but never called -> never a call edge.
    assert ("T.java", "orphan") not in edges
    assert {"helper", "caller", "make", "free", "orphan", "useIt"} <= declared


@needs_java
def test_cgr_matches_oracle_on_clean_java_project(tmp_path: Path) -> None:
    _make_project(tmp_path)
    oracle, declared = oracle_java_call_edges(tmp_path)
    cgr = cgr_java_call_edges(tmp_path, tmp_path.name, declared)
    assert cgr == oracle


def test_score_java_retrieval_prf() -> None:
    result = score_java_retrieval(
        {("A.java", "f"), ("A.java", "g")}, {("A.java", "f"), ("B.java", "h")}
    )
    row = next(r for r in result.rows if r["label"] == ec.JAVA_RETRIEVAL_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
