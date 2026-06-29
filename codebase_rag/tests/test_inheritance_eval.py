from pathlib import Path

from evals import constants as ec
from evals.inheritance import (
    CgrResult,
    OracleResult,
    cgr_inheritance,
    oracle_inheritance,
    score_inheritance,
)


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "base.py").write_text(
        "class Animal:\n"
        "    def speak(self):\n"
        "        return 1\n"
        "    def move(self):\n"
        "        return 2\n",
        encoding="utf-8",
    )
    (root / "derived.py").write_text(
        "from proj.base import Animal\n\n\n"
        "class Dog(Animal):\n"
        "    def speak(self):\n"
        "        return 3\n"
        "    def fetch(self):\n"
        "        return 4\n",
        encoding="utf-8",
    )


def test_oracle_resolves_inherits_and_overrides(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src)
    oracle = oracle_inheritance(src, "proj")

    assert ("proj.derived.Dog", "proj.base.Animal") in oracle.inherits
    # (H) speak is redefined in Dog and exists in the base -> an override.
    assert ("proj.derived.Dog", "proj.base.Animal", "speak") in oracle.overrides
    # (H) fetch is new (not in base); move is inherited (not redefined). Neither
    # (H) is an override.
    assert ("proj.derived.Dog", "proj.base.Animal", "fetch") not in oracle.overrides
    assert all(m != "move" for (_c, _b, m) in oracle.overrides)
    # (H) Dog is single-base and top-level, so it is eligible for override grading.
    assert "proj.derived.Dog" in oracle.override_scope


def test_cgr_matches_oracle_on_clean_hierarchy(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src)
    result = score_inheritance(
        cgr_inheritance(src, "proj"), oracle_inheritance(src, "proj")
    )
    assert all(row["fp"] == 0 and row["fn"] == 0 for row in result.rows)


def test_score_flags_missing_override() -> None:
    oracle = OracleResult(
        inherits={("proj.derived.Dog", "proj.base.Animal")},
        overrides={("proj.derived.Dog", "proj.base.Animal", "speak")},
        top_classes=frozenset({"proj.derived.Dog", "proj.base.Animal"}),
        override_scope=frozenset({"proj.derived.Dog"}),
    )
    cgr = CgrResult(
        inherits={("proj.derived.Dog", "proj.base.Animal")},
        overrides=set(),
    )
    result = score_inheritance(cgr, oracle)
    overrides = next(r for r in result.rows if r["label"] == ec.OVERRIDES_LABEL)
    assert overrides["fn"] == 1
    assert overrides["recall"] == 0.0
