from pathlib import Path

from evals.cgr_graph import _capture


def _make(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "util.ts").write_text(
        "export function parsedType(x: unknown): number { return 1; }\n",
        encoding="utf-8",
    )
    (root / "he.ts").write_text(
        'import * as util from "./util.js";\n'
        "export const fmt = (x: unknown): number => util.parsedType(x);\n",
        encoding="utf-8",
    )


def test_ts_arrow_const_caller_body_calls_resolve(tmp_path: Path) -> None:
    # (H) A call inside a named arrow / const-arrow function body must be attributed
    # (H) to that function (p.he.fmt). The call pass skipped arrows because they
    # (H) have no `name` field, so _get_node_name returned None and the whole arrow
    # (H) body -- and its calls -- went unprocessed.
    _make(tmp_path)
    ingestor = _capture(tmp_path, "p")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    assert ("p.he.fmt", "p.util.parsedType") in calls
