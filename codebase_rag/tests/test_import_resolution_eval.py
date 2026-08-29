import ast
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

    # stdlib and third-party are external, keyed by top-level package.
    assert ("m.py", "os", True) in deps
    assert ("m.py", "numpy", True) in deps
    assert ("m.py", "collections", True) in deps
    # absolute and relative first-party imports are internal (top == project).
    assert ("m.py", "proj", False) in deps
    # a first-party import is never marked external.
    assert ("m.py", "proj", True) not in deps


def test_every_internal_import_from_one_file_collapses_to_one_dep(
    tmp_path: Path,
) -> None:
    """The granularity this eval grades internal imports at, pinned.

    An internal import's top-level package IS the project name, by the same
    expression that decides externality (`top != project`). So every
    first-party import from one file reduces to a single dep and the
    `imports/internal` row measures which files import something internal,
    not which module they import. `m.py` imports two distinct first-party
    modules and contributes one internal dep.

    Deliberate: the eval isolates internal/external misclassification
    (issue #498), and the structural L1 grades internal targets by resolved
    file. Pinned rather than left emergent because the reduction reads as
    informative from the external side, where top-level names distinguish
    `numpy` from `os`, and a reader can carry that over.

    The fixture precondition is asserted, not assumed. Checking only that
    one internal dep comes out would pass just as well on a fixture with a
    single first-party import, which exercises no collapse at all: the
    claim is about MANY reducing to one, so the many has to be established.
    It is counted from the source rather than from `deps`, so the assertion
    does not depend on the reduction it is measuring.
    """
    src = tmp_path / "proj"
    _make_repo(src)
    module = ast.parse((src / "m.py").read_text(encoding="utf-8"))
    first_party = {
        node.module or ""
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom)
        and (node.level > 0 or (node.module or "").startswith("proj"))
    }

    deps = oracle_import_deps(src, "proj")
    internal = {d for d in deps if not d[2]}

    assert len(first_party) > 1, first_party
    assert internal == {("m.py", "proj", False)}
    assert len({d for d in deps if d[2]}) == 3


def test_oracle_excludes_future_pseudo_import(tmp_path: Path) -> None:
    # `from __future__ import ...` is a compiler directive, not a dependency;
    # cgr rightly ignores it, so the oracle must too or it reports false misses.
    src = tmp_path / "proj"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "f.py").write_text("from __future__ import annotations\n", encoding="utf-8")
    deps = oracle_import_deps(src, "proj")
    assert all(top != "__future__" for (_f, top, _e) in deps)


def test_cgr_matches_oracle_on_clean_repo(tmp_path: Path) -> None:
    # On an unambiguous repo cgr's import classification should equal the
    # oracle: every stdlib/third-party import external, every project import
    # internal.
    src = tmp_path / "proj"
    _make_repo(src)
    assert cgr_import_deps(src, "proj") == oracle_import_deps(src, "proj")


def test_score_flags_misclassified_internal_as_external() -> None:
    oracle = {("m.py", "proj", False), ("m.py", "os", True)}
    # cgr wrongly marks the first-party import external (issue #498 shape).
    cgr = {("m.py", "proj", True), ("m.py", "os", True)}
    result = score_import_deps(cgr, oracle)
    internal = next(r for r in result.rows if r["label"] == ec.IMPORTS_INTERNAL_LABEL)
    assert internal["fn"] == 1
    assert internal["recall"] == 0.0
