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
    all_active = dict.fromkeys((key for key, _exe, _arg in pf._VERSIONED_TOOLS), True)
    with (
        patch.object(pf, "_active_tools", return_value=all_active),
        patch(
            "codebase_rag.parser_fingerprint.subprocess.run",
            side_effect=OSError("no such tool"),
        ),
    ):
        versions = pf._tool_versions()

    assert versions, "no version entries produced"
    # Every tool forced active, so each one is genuinely probed and each probe
    # raises: `absent` here is the degradation path, not the inactive marker.
    assert all(entry.endswith("=absent") for entry in versions), versions


def test_an_inactive_tool_version_does_not_change_the_fingerprint() -> None:
    """A tool no frontend uses must not invalidate the graph.

    Greptile proved this by execution: with Java resolved to `heuristic` and
    C# to `treesitter`, neither executable participates in extraction, yet
    changing their versions changed the fingerprint and tripped the
    stale-parser warning.

    That is FALSE staleness -- the failure mode the stability control in this
    file already warns about, arriving through a different door. A fingerprint
    that changes when nothing relevant changed invalidates the cache for no
    reason, which costs more than the drift it was added to catch.
    """
    # Every tool inactive, so NOTHING is probed and the varied version can only
    # reach the fingerprint if an inactive tool is being hashed. Leaving one
    # tool active would change its value too and prove nothing.
    none_active = dict.fromkeys((key for key, _exe, _arg in pf._VERSIONED_TOOLS), False)
    with (
        patch.object(pf, "_active_tools", return_value=none_active),
        patch.object(pf, "_probe_version", return_value="17.0.1"),
    ):
        first = pf.compute_parser_fingerprint()
    with (
        patch.object(pf, "_active_tools", return_value=none_active),
        patch.object(pf, "_probe_version", return_value="21.0.9"),
    ):
        second = pf.compute_parser_fingerprint()

    assert first == second, "an inactive tool's version changed the fingerprint"

    # The paired control: with the SAME tools active, the same version change
    # must change the fingerprint. Without it this test passes against an
    # implementation that stopped hashing tool versions entirely.
    all_active = dict.fromkeys((key for key, _e, _a in pf._VERSIONED_TOOLS), True)
    with (
        patch.object(pf, "_active_tools", return_value=all_active),
        patch.object(pf, "_probe_version", return_value="17.0.1"),
    ):
        active_first = pf.compute_parser_fingerprint()
    with (
        patch.object(pf, "_active_tools", return_value=all_active),
        patch.object(pf, "_probe_version", return_value="21.0.9"),
    ):
        active_second = pf.compute_parser_fingerprint()

    assert active_first != active_second, "an ACTIVE tool's version was ignored"
