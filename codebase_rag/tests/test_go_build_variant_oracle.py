"""The build-variant differential oracle, bounded (issue #2483, PR #3360).

Seeded cases of interfaces, types and methods declared per build, checked
against the real go/types run per GOOS (`evals/go_build_variants.py`, whose
module docstring lists the checks). The full run is the eval script:
`python -m evals.go_build_variants --cases 400 --seed 7`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codebase_rag.parsers.go_frontend import go_frontend_available
from evals import go_build_variants as oracle


@pytest.mark.parametrize("seed", [7, 23])
def test_generated_build_variants_match_go_types(tmp_path: Path, seed: int) -> None:
    if shutil.which("go") is None or not go_frontend_available():
        pytest.skip("go toolchain not available")
    report = oracle.run(cases=30, seed=seed, workdir=tmp_path, renames=3)
    for frontend, found in report.findings.items():
        assert found.failures() == 0, (frontend, vars(found))
        assert not found.unsupported_heuristic_calls, frontend
        assert found.pairs_supported
        assert found.calls_checked
    assert len(report.renames) == 3
    assert all(result.applied for result in report.renames)
    assert not any(result.broke for result in report.renames)
