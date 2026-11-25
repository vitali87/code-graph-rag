"""Contract tests for shared coverage-aware test time bounds (issue #3674)."""

import sys

import coverage
import pytest

from codebase_rag.tests.time_bounds import HANDOFF_TIMEOUT_S, cpu_ceiling


def test_handoff_timeout_is_thirty_seconds() -> None:
    assert HANDOFF_TIMEOUT_S == 30


def test_cpu_ceiling_clears_the_observed_coverage_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Observed CPU seconds on the SonarCloud coverage run (PR #3118) for
    # `test_a_chain_of_redeclarations_links_each_link_once[ends]`.
    observed_cpu_s = 796.81348949 - 791.015878591
    monkeypatch.setattr(coverage.Coverage, "current", staticmethod(object))
    assert cpu_ceiling(5) == 15
    assert cpu_ceiling(10) == 30
    assert observed_cpu_s < cpu_ceiling(5)


def test_cpu_ceiling_keeps_nominal_bound_when_coverage_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coverage.Coverage, "current", staticmethod(lambda: None))
    assert cpu_ceiling(5) == 5
    assert cpu_ceiling(10) == 10


def test_cpu_ceiling_keeps_nominal_bound_when_coverage_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "coverage", None)
    assert cpu_ceiling(5) == 5
