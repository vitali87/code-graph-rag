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

import pytest

from codebase_rag.tests.conftest import assert_fixture_covers


def test_a_complete_fixture_passes() -> None:
    assert_fixture_covers({"a", "b"}, {"a", "b"}, what="the rollup")


def test_a_superset_passes() -> None:
    """Extra values are not a defect.

    A fixture may legitimately hold non-inputs alongside the real ones --
    indeed it should, so it exercises the filter's DISCRIMINATION rather
    than only its positive path.
    """
    assert_fixture_covers({"a", "b", "unrelated"}, {"a", "b"}, what="the rollup")


def test_a_missing_input_fails_and_names_it() -> None:
    """The helper must be able to fail, and say which value is absent.

    A guard that cannot produce a failure is the same defect one level up,
    so this is the test that makes the others mean anything.
    """
    with pytest.raises(AssertionError) as excinfo:
        assert_fixture_covers({"a"}, {"a", "b"}, what="the rollup")

    message = str(excinfo.value)
    assert "'b'" in message, "the failure must name the missing value"
    assert "the rollup" in message, "the failure must name the fixture"


def test_an_empty_fixture_fails() -> None:
    """The exact measured shape: a fixture supplying none of the inputs."""
    with pytest.raises(AssertionError):
        assert_fixture_covers(set(), {"a", "b"}, what="the all-concluded rollup")


def test_partial_and_total_gaps_are_diagnosed_differently() -> None:
    """They are different bugs and need different messages (#1862 review).

    A fixture supplying NONE of the inputs makes the empty result vacuous
    outright. One supplying some makes it merely unreliable -- the filter may
    be examining the present values correctly and simply never seeing the
    absent ones. An earlier version said "none of its inputs" for both, which
    is a false diagnosis on the partial case and sends the reader looking for
    the wrong thing.
    """
    with pytest.raises(AssertionError) as total:
        assert_fixture_covers(set(), {"a", "b"}, what="the rollup")
    with pytest.raises(AssertionError) as partial:
        assert_fixture_covers({"a"}, {"a", "b"}, what="the rollup")

    assert "supplies none of its inputs" in str(total.value)
    assert "supplies none of its inputs" not in str(partial.value), (
        "a fixture covering SOME inputs was reported as covering none"
    )
    assert "does not supply all of them" in str(partial.value)


def test_requiring_nothing_passes() -> None:
    """A predicate that reads no named inputs cannot be under-covered.

    Pinned so the helper is not mistaken for a general vacuity detector: it
    checks one specific precondition and says nothing about tests that do
    not have named inputs.
    """
    assert_fixture_covers(set(), set(), what="the rollup")


def test_the_measured_instance_would_have_been_caught() -> None:
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
    with pytest.raises(AssertionError, match="does not cover") as excinfo:
        assert_fixture_covers(
            covered, set(AGGREGATED_JOBS), what="the all-concluded rollup"
        )

    # Matched on the behavioural phrase rather than the issue number: an
    # issue reference in a message is documentation, so pinning it reddens
    # this test on a pure rewording that changes nothing.
    for job in AGGREGATED_JOBS:
        assert job in str(excinfo.value), (
            f"the failure must name every uncovered dependency; {job!r} is "
            "missing, so a reader cannot fix the fixture from the message"
        )


def test_a_corrected_fixture_passes_the_same_check() -> None:
    """The other direction, so the test above is not satisfied by a helper
    that simply always raises.

    The corrected rollup is DERIVED from `AGGREGATED_JOBS` rather than typed
    out. A hardcoded list is a second fixture with the same defect as the
    first: it silently stops covering the predicate the moment the tuple
    grows, and this one grows -- it went from five entries to eight while
    this PR was open, because `all-checks-pass` declares more dependencies in
    `needs:` than were listed.

    The matrix suffix is appended to one entry so the derived rollup also
    exercises `aggregated_job_for`'s `name (` matching rather than only its
    exact-match arm.
    """
    from scripts.check_pr_gated import AGGREGATED_JOBS, aggregated_job_for

    corrected = [
        # Non-dependencies, kept so the fixture exercises the filter's
        # discrimination rather than only its positive path.
        "CodeQL",
        "Analyze (actions)",
        *(
            f"{job} (ubuntu-latest)" if job == "Integration Tests" else job
            for job in AGGREGATED_JOBS
        ),
    ]
    covered = {
        job for name in corrected if (job := aggregated_job_for(name)) is not None
    }

    assert covered == set(AGGREGATED_JOBS), (
        "fixture guard: the derived rollup must cover every aggregated job, "
        f"or this test asserts nothing; missing {set(AGGREGATED_JOBS) - covered}"
    )
    assert_fixture_covers(
        covered, set(AGGREGATED_JOBS), what="the all-concluded rollup"
    )
