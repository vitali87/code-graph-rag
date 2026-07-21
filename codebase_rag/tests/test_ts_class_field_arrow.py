from pathlib import Path

from evals.cgr_graph import _capture


def _make(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "util.ts").write_text(
        "export function parsedType(x: unknown): number { return 1; }\n",
        encoding="utf-8",
    )
    (root / "t.ts").write_text(
        'import * as util from "./util.js";\n'
        "export class T {\n"
        "  helper = (x: unknown): number => util.parsedType(x);\n"
        "  regular(x: unknown): number { return util.parsedType(x); }\n"
        "}\n",
        encoding="utf-8",
    )


def test_ts_class_field_arrow_is_modeled_and_calls_resolve(tmp_path: Path) -> None:
    # A class-property arrow (`helper = (x) => ...`) must be modeled as a
    # member node (p.t.T.helper) just like a normal method, and the calls in
    # its body must be attributed to it. Previously the definition pass created
    # no node for it (no name field) and the call pass skipped its body.
    _make(tmp_path)
    ingestor = _capture(tmp_path, "p")
    member_qns = {
        str(uid) for (label, uid) in ingestor.nodes if label in ("Method", "Function")
    }
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }

    assert "p.t.T.helper" in member_qns
    assert ("p.t.T.helper", "p.util.parsedType") in calls
    # regression guard: the normal method still works.
    assert ("p.t.T.regular", "p.util.parsedType") in calls
