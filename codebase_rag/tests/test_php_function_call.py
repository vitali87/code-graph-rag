from pathlib import Path

from evals.cgr_graph import _capture


def _make(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "util.php").write_text(
        "<?php\nfunction helper(): int { return 2; }\n", encoding="utf-8"
    )
    (root / "use.php").write_text(
        "<?php\nfunction useIt(): int { return helper(); }\n", encoding="utf-8"
    )


def test_php_plain_function_call_resolves(tmp_path: Path) -> None:
    # A bare PHP function call (`helper()`) is a function_call_expression whose
    # callee is a `name` node under the `function` field. _get_call_target_name
    # did not handle the `name` type, so no callee name was extracted and the
    # CALLS edge was dropped -- only method/static calls (which expose a `name`
    # field directly) resolved.
    _make(tmp_path)
    ingestor = _capture(tmp_path, "p")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    assert ("p.use.useIt", "p.util.helper") in calls
