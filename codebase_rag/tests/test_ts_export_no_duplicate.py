from pathlib import Path

from evals.cgr_graph import _capture


def _make(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "util.ts").write_text(
        "export function parsedType(x: number): number { return x; }\n"
        "export const dbl = (n: number): number => n * 2;\n",
        encoding="utf-8",
    )
    (root / "use.ts").write_text(
        'import { parsedType, dbl } from "./util.js";\n'
        "export function go(): number { return parsedType(1) + dbl(2); }\n",
        encoding="utf-8",
    )


def test_ts_exported_function_not_duplicated(tmp_path: Path) -> None:
    # An exported function / const-arrow is already ingested by the definition
    # pass at its natural qn (p.util.parsedType). The ES6-export pass must not
    # re-register it -- doing so makes a spurious `qn@line` duplicate node and
    # splits CALLS edges onto that duplicate, mangling the callee qn.
    _make(tmp_path)
    ingestor = _capture(tmp_path, "p")
    fn_qns = {str(uid) for (label, uid) in ingestor.nodes if label == "Function"}

    assert "p.util.parsedType" in fn_qns
    assert "p.util.dbl" in fn_qns
    assert not any("@" in q for q in fn_qns), f"duplicate fn nodes: {fn_qns}"

    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    assert ("p.use.go", "p.util.parsedType") in calls
    assert ("p.use.go", "p.util.dbl") in calls
    assert not any("@" in to_val for _f, to_val in calls), f"calls to dup: {calls}"
