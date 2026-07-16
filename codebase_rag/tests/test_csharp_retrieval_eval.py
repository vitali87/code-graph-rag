from pathlib import Path

import pytest

from evals import constants as ec
from evals.csharp_retrieval import (
    cgr_csharp_call_edges,
    oracle_csharp_call_edges,
    score_csharp_retrieval,
)
from evals.oracles import csharp_oracle_available

needs_dotnet = pytest.mark.skipif(
    not csharp_oracle_available(), reason="dotnet toolchain not installed"
)


def _make_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Util.cs").write_text(
        "namespace N;\npublic static class Util {\n"
        "    public static int Free() { return 2; }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "T.cs").write_text(
        "namespace N;\npublic class T {\n"
        "    public int Helper() { return 1; }\n"
        "    public int Caller() { return this.Helper(); }\n"
        "    public static T Make() { return new T(); }\n"
        "    public int Orphan() { return 9; }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "Use.cs").write_text(
        "namespace N;\npublic class Use {\n"
        "    public int UseIt() {\n"
        "        T t = T.Make();\n"
        "        return Util.Free() + t.Caller();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )


@needs_dotnet
def test_oracle_captures_first_party_csharp_calls(tmp_path: Path) -> None:
    _make_project(tmp_path)
    edges, declared = oracle_csharp_call_edges(tmp_path)

    # (H) this.Helper(), T.Make(), Util.Free(), t.Caller() are first-party calls.
    assert ("T.cs", "Helper") in edges
    assert ("Use.cs", "Make") in edges
    assert ("Use.cs", "Free") in edges
    assert ("Use.cs", "Caller") in edges
    # (H) Orphan is declared but never called -> never a call edge.
    assert ("T.cs", "Orphan") not in edges
    assert {"Helper", "Caller", "Make", "Free", "Orphan", "UseIt"} <= declared


@needs_dotnet
def test_cgr_matches_oracle_on_clean_csharp_project(tmp_path: Path) -> None:
    _make_project(tmp_path)
    oracle, declared = oracle_csharp_call_edges(tmp_path)
    cgr = cgr_csharp_call_edges(tmp_path, tmp_path.name, declared)
    assert cgr == oracle


def test_score_csharp_retrieval_prf() -> None:
    result = score_csharp_retrieval(
        {("A.cs", "F"), ("A.cs", "G")}, {("A.cs", "F"), ("B.cs", "H")}
    )
    row = next(r for r in result.rows if r["label"] == ec.CSHARP_RETRIEVAL_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
