# External tool versions belong in the parser fingerprint (issue #1465).
#
# The fingerprint exists so a change to what cgr can EXTRACT invalidates the
# incremental graph. It recorded the resolved frontend mode -- which frontend
# ran -- but not the version of the tool that frontend drives, which is what
# it knew. A Go release with better inference, a Roslyn update resolving an
# overload differently, a libclang that expands a macro it previously could
# not: each changes the emitted edges while `GO_FRONTEND=gotypes` is identical.
#
# `LOMBOK=` was already in the list, so the file agreed with the principle for
# one tool and simply did not apply it to the others.
from __future__ import annotations

from unittest.mock import patch

from codebase_rag import parser_fingerprint as pf

_PROBES = "codebase_rag.parser_fingerprint._tool_versions"


def test_a_tool_version_change_changes_the_fingerprint() -> None:
    """Upgrading a compiler must not silently reuse the old graph.

    Asserting the two differ is the whole contract: an incremental run whose
    fingerprint matches skips the work, so an unchanged fingerprint means the
    new compiler's output never reaches the graph.
    """
    with patch(_PROBES, return_value=["GO_VERSION=go1.22.0"]):
        before = pf.compute_parser_fingerprint()
    with patch(_PROBES, return_value=["GO_VERSION=go1.23.0"]):
        after = pf.compute_parser_fingerprint()

    assert before != after


def test_the_fingerprint_is_stable_for_an_unchanged_toolchain() -> None:
    """The control, and the failure mode that is worse than the bug.

    A fingerprint that changes every run invalidates the cache always, which
    costs more than never invalidating it. Asserting only that versions change
    the hash would pass against an implementation that hashed a timestamp.
    """
    with patch(_PROBES, return_value=["GO_VERSION=go1.22.0"]):
        first = pf.compute_parser_fingerprint()
        second = pf.compute_parser_fingerprint()

    assert first == second


def test_an_absent_toolchain_is_distinguishable_from_any_version() -> None:
    """A toolchain that disappears changes extraction as much as one that moves.

    `absent` must not collide with a version string, or removing a compiler
    reuses the graph it built.
    """
    with patch(_PROBES, return_value=["GO_VERSION=absent"]):
        without = pf.compute_parser_fingerprint()
    with patch(_PROBES, return_value=["GO_VERSION=go1.22.0"]):
        with_go = pf.compute_parser_fingerprint()

    assert without != with_go


def test_a_probe_failure_degrades_rather_than_raising() -> None:
    """The fingerprint is computed on every index; a probe must never abort it.

    A tool that cannot be probed cannot be running, so its absence is the
    honest answer -- but it has to be reached without an exception escaping.
    """
    with patch(
        "codebase_rag.parser_fingerprint.subprocess.run",
        side_effect=OSError("no such tool"),
    ):
        versions = pf._tool_versions()

    assert versions, "no version entries produced"
    assert all(entry.endswith("=absent") for entry in versions), versions
