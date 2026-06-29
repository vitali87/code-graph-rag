from pathlib import Path

from evals import constants as ec
from evals.import_resolution import (
    cgr_import_deps,
    oracle_import_deps,
    score_import_deps,
)


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "helper.py").write_text("def thing():\n    return 1\n", encoding="utf-8")
    (root / "sibling.py").write_text("x = 1\n", encoding="utf-8")
    (root / "m.py").write_text(
        "import os\n"
        "import numpy.linalg\n"
        "from collections import OrderedDict\n"
        "from proj.helper import thing\n"
        "from . import sibling\n",
        encoding="utf-8",
    )


def test_oracle_classifies_internal_and_external(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src)
    deps = oracle_import_deps(src, "proj")

    # (H) stdlib and third-party are external, keyed by top-level package.
    assert ("m.py", "os", True) in deps
    assert ("m.py", "numpy", True) in deps
    assert ("m.py", "collections", True) in deps
    # (H) absolute and relative first-party imports are internal (top == project).
    assert ("m.py", "proj", False) in deps
    # (H) a first-party import is never marked external.
    assert ("m.py", "proj", True) not in deps


def test_oracle_excludes_future_pseudo_import(tmp_path: Path) -> None:
    # (H) `from __future__ import ...` is a compiler directive, not a dependency;
    # (H) cgr rightly ignores it, so the oracle must too or it reports false misses.
    src = tmp_path / "proj"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "f.py").write_text("from __future__ import annotations\n", encoding="utf-8")
    deps = oracle_import_deps(src, "proj")
    assert all(top != "__future__" for (_f, top, _e) in deps)


def test_cgr_matches_oracle_on_clean_repo(tmp_path: Path) -> None:
    # (H) On an unambiguous repo cgr's import classification should equal the
    # (H) oracle: every stdlib/third-party import external, every project import
    # (H) internal.
    src = tmp_path / "proj"
    _make_repo(src)
    assert cgr_import_deps(src, "proj") == oracle_import_deps(src, "proj")


def test_score_flags_misclassified_internal_as_external() -> None:
    oracle = {("m.py", "proj", False), ("m.py", "os", True)}
    # (H) cgr wrongly marks the first-party import external (issue #498 shape).
    cgr = {("m.py", "proj", True), ("m.py", "os", True)}
    result = score_import_deps(cgr, oracle)
    internal = next(r for r in result.rows if r["label"] == ec.IMPORTS_INTERNAL_LABEL)
    assert internal["fn"] == 1
    assert internal["recall"] == 0.0
