from pathlib import Path

from evals import constants as ec
from evals.instantiation import (
    cgr_instantiations,
    oracle_instantiations,
    score_instantiations,
)


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "w.py").write_text(
        "class Widget:\n    def __init__(self):\n        pass\n\n\nclass Unused:\n    pass\n",
        encoding="utf-8",
    )
    (root / "u.py").write_text(
        "from proj.w import Widget\n\n\ndef build():\n    return Widget()\n",
        encoding="utf-8",
    )
    (root / "n.py").write_text(
        "from proj.w import Widget\n\nALIAS = Widget\n",
        encoding="utf-8",
    )


def test_oracle_captures_constructor_calls(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src)
    deps = oracle_instantiations(src, "proj")

    # (H) build() constructs Widget().
    assert ("u.py", "Widget") in deps
    # (H) n.py only aliases Widget, never calls it -> not an instantiation.
    assert ("n.py", "Widget") not in deps
    # (H) Unused is never constructed anywhere.
    assert all(name != "Unused" for (_f, name) in deps)


def test_cgr_matches_oracle_on_clean_repo(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src)
    assert cgr_instantiations(src, "proj") == oracle_instantiations(src, "proj")


def test_score_computes_prf() -> None:
    oracle = {("u.py", "Widget"), ("x.py", "Thing")}
    cgr = {("u.py", "Widget")}
    result = score_instantiations(cgr, oracle)
    row = next(r for r in result.rows if r["label"] == ec.INSTANTIATES_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 0, 1)
