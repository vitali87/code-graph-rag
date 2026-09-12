"""`assert_fixture_covers` makes a fixture's adequacy a checked precondition.

The defect it exists for (#1859): a test asserting that a filter returned
NOTHING passes vacuously when the fixture holds none of the values that
filter inspects. The assertion cannot tell "the filter correctly found
nothing" from "the filter was handed nothing to look at", and only the first
is evidence.

Measured instance, on `REAL_ROLLUP_ALL_CONCLUDED` in `test_pr_gate_check.py`:
the fixture held `CodeQL` and `Analyze (actions)`, neither a member of
`AGGREGATED_JOBS`, so the "every dependency concluded" branch was reached
because the rollup contained no dependency. Flipping every conclusion to
unfinished left all 105 tests green.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

Covers = Callable[..., None]


def test_a_complete_fixture_passes(assert_fixture_covers: Covers) -> None:
    assert_fixture_covers({"a", "b"}, {"a", "b"}, what="the rollup")


def test_a_superset_passes(assert_fixture_covers: Covers) -> None:
    """Extra values are not a defect.

    A fixture may legitimately hold non-inputs alongside the real ones --
    indeed it should, so it exercises the filter's DISCRIMINATION rather
    than only its positive path.
    """
    assert_fixture_covers({"a", "b", "unrelated"}, {"a", "b"}, what="the rollup")


def test_a_missing_input_fails_and_names_it(assert_fixture_covers: Covers) -> None:
    """The helper must be able to fail, and say which value is absent.

    A guard that cannot produce a failure is the same defect one level up,
    so this is the test that makes the others mean anything.
    """
    with pytest.raises(AssertionError) as excinfo:
        assert_fixture_covers({"a"}, {"a", "b"}, what="the rollup")

    message = str(excinfo.value)
    assert "'b'" in message, "the failure must name the missing value"
    assert "the rollup" in message, "the failure must name the fixture"


def test_an_empty_fixture_fails(assert_fixture_covers: Covers) -> None:
    """The exact measured shape: a fixture supplying none of the inputs."""
    with pytest.raises(AssertionError):
        assert_fixture_covers(set(), {"a", "b"}, what="the all-concluded rollup")


def test_requiring_nothing_passes(assert_fixture_covers: Covers) -> None:
    """A predicate that reads no named inputs cannot be under-covered.

    Pinned so the helper is not mistaken for a general vacuity detector: it
    checks one specific precondition and says nothing about tests that do
    not have named inputs.
    """
    assert_fixture_covers(set(), set(), what="the rollup")


def test_the_measured_instance_would_have_been_caught(
    assert_fixture_covers: Covers,
) -> None:
    """The helper against the real defect that motivated it (#1859).

    `REAL_ROLLUP_ALL_CONCLUDED` held `CodeQL` and `Analyze (actions)`. Neither
    is an `AGGREGATED_JOBS` member, so `absent_context_reason` reached its
    "every dependency concluded" branch with no dependency present, and four
    tests named for the concluded state passed with every conclusion blanked.

    Reproduced here against the real `AGGREGATED_JOBS` rather than invented
    strings, so this goes red if that tuple ever changes in a way that makes
    the example stop demonstrating the point.
    """
    from scripts.check_pr_gated import AGGREGATED_JOBS, aggregated_job_for

    defective = ["CodeQL", "Analyze (actions)"]
    covered = {
        job for name in defective if (job := aggregated_job_for(name)) is not None
    }

    assert covered == set(), (
        "the historical fixture is expected to cover NO aggregated job; if "
        "it now covers one, this example no longer shows the defect"
    )
    with pytest.raises(AssertionError, match="#1859"):
        assert_fixture_covers(
            covered, set(AGGREGATED_JOBS), what="the all-concluded rollup"
        )


def test_a_corrected_fixture_passes_the_same_check(
    assert_fixture_covers: Covers,
) -> None:
    """The other direction, so the test above is not satisfied by a helper
    that simply always raises."""
    from scripts.check_pr_gated import AGGREGATED_JOBS, aggregated_job_for

    corrected = [
        "CodeQL",
        "Analyze (actions)",
        "Lint & Format",
        "Type Check",
        "Unit Tests (ubuntu-latest, py3.13)",
        "Integration Tests (ubuntu-latest)",
        "Binary Smoke Test",
    ]
    covered = {
        job for name in corrected if (job := aggregated_job_for(name)) is not None
    }

    assert_fixture_covers(
        covered, set(AGGREGATED_JOBS), what="the all-concluded rollup"
    )
