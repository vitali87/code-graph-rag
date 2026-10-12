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

pytestmark = pytest.mark.usefixtures("one_go_helper_per_session")


# One seed: the eval script runs 400 cases (see the module docstring), and
# each seed here costs a dozen runs of the real helper and as many `go vet`.
@pytest.mark.parametrize("seed", [7])
def test_generated_build_variants_match_go_types(tmp_path: Path, seed: int) -> None:
    if shutil.which("go") is None or not go_frontend_available():
        pytest.skip("go toolchain not available")
    report = oracle.run(
        cases=30, seed=seed, workdir=tmp_path, renames=3, wide=0.4, deep=0.3
    )
    for frontend, found in report.findings.items():
        assert found.failures() == 0, (frontend, vars(found))
        assert not found.unsupported_heuristic_calls, frontend
        assert found.pairs_supported
        assert found.calls_checked
    assert len(report.renames) == 3
    assert all(result.applied or result.refused for result in report.renames)
    assert not any(result.changed_on_refusal for result in report.renames)
    assert not any(result.broke for result in report.renames)
    assert not any(result.touched for result in report.renames)


def test_interfaces_no_type_implements_rename_whole(tmp_path: Path) -> None:
    # Interfaces alone (`--only`): a method one declares again over an embed,
    # or two embeds bring together, is one method. Each rename of it takes
    # every declaration or refuses (PR #3360 review); the graph joins them.
    if shutil.which("go") is None or not go_frontend_available():
        pytest.skip("go toolchain not available")
    report = oracle.run(
        cases=4,
        seed=7,
        workdir=tmp_path,
        frontends=("treesitter",),
        renames=3,
        only=1.0,
    )
    found = report.findings["treesitter"]
    assert found.failures() == 0, vars(found)
    assert found.interface_pairs_supported
    assert sum("OM_" in result.target for result in report.renames) >= 2
    assert all(result.applied or result.refused for result in report.renames)
    assert not any(result.changed_on_refusal for result in report.renames)
    assert not any(result.broke for result in report.renames)
    assert not any(result.touched for result in report.renames)


def test_the_oracle_counts_an_interface_past_the_cap_apart() -> None:
    # Wide7 has 2**7 method sets: what a build proves through it is unknown
    # to cgr by design. Wide6, at 64, is checked like any other.
    past = "('oracle.c001.p.tw.TW', 'oracle.c001.p.wide.Wide7')"
    at = "linux: oracle.c001.p.callwide.CallWide6 -> oracle.c001.p.tw.TW.WN_1"
    found = oracle.Findings()
    oracle._add_missing(found, found.missing_implements, [past, at])
    assert found.unknown_past_cap == [past]
    assert found.missing_implements == [at]


def test_every_neighbour_mirrors_a_declaration_without_satisfying_it() -> None:
    # Python and JavaScript twins beside each Go file declaring methods, and
    # the types and methods again, under the same constraint, in `p_test`
    # and a sibling package, each method taking an `int`.
    files = {
        "c000/p/t1_linux.go": (
            "package p\n\ntype T1 struct{}\n\nfunc (T1) A_0() int { return 0 }\n"
        ),
        "c000/p/i_a.go": (
            "//go:build linux\n\npackage p\n\ntype I interface {\n\tA_0() int\n}\n"
        ),
    }
    beside = oracle.neighbours(files)
    assert sorted(beside) == [
        "c000/p/i_a_alt.js",
        "c000/p/i_a_alt.py",
        "c000/p/t1_linux_alt.js",
        "c000/p/t1_linux_alt.py",
        "c000/p/t1_linux_test.go",
        "c000/psib/t1_linux.go",
    ]
    mirrored = "type T1 struct{}\n\nfunc (T1) A_0(int) {}\n"
    assert beside["c000/p/t1_linux_test.go"] == f"package p_test\n\n{mirrored}"
    assert beside["c000/psib/t1_linux.go"] == f"package psib\n\n{mirrored}"
    assert "class I:\n    def A_0(self):" in beside["c000/p/i_a_alt.py"]
