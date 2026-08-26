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

import subprocess
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


def test_a_tool_is_inactive_when_the_repo_cannot_use_it(tmp_path) -> None:
    """A Python-only repo must not be invalidated by a Go upgrade.

    Gating on the resolved frontend MODE is not enough: Go resolves to
    `gotypes` whenever a go toolchain exists, regardless of whether the
    indexed repository contains a single `go.mod`. Upgrading Go then
    invalidates the graph of a repo it can extract nothing from.

    Same false-staleness failure as the inactive-mode case, one level down --
    the mode says the frontend COULD run, `applies()` says whether it can run
    HERE.
    """
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    active = pf._active_tools(tmp_path)

    assert not active["GO_VERSION"], "Go counted for a repo with no go.mod"
    assert not active["DOTNET_VERSION"], "dotnet counted for a repo with no project"


def test_a_tool_is_active_when_the_repo_does_use_it(tmp_path) -> None:
    """The paired control: a repo that DOES contain the language still counts.

    Without it, an implementation that reported every tool inactive would pass
    the test above and silently stop detecting real toolchain upgrades.
    """
    (tmp_path / "go.mod").write_text(
        "module example.com/x\n\ngo 1.23\n", encoding="utf-8"
    )

    with patch(
        "codebase_rag.parsers.go_frontend.resolve_go_frontend",
        return_value=__import__(
            "codebase_rag.constants", fromlist=["GoFrontend"]
        ).GoFrontend.GOTYPES,
    ):
        active = pf._active_tools(tmp_path)

    assert active["GO_VERSION"], "Go ignored for a repo that has a go.mod"


def test_a_repo_less_call_keeps_mode_gating_only(tmp_path) -> None:
    """`compute_parser_fingerprint()` is called without a repo in places.

    Those calls must stay stable rather than reporting everything inactive,
    which would make a repo-less fingerprint disagree with a repo-scoped one
    for the same toolchain.
    """
    assert set(pf._active_tools(None)) == {
        key for key, _exe, _arg in pf._VERSIONED_TOOLS
    }


def test_the_fingerprint_uses_the_frontends_own_applicability_predicate(
    tmp_path,
) -> None:
    """The fingerprint and `applies()` must agree, and stay agreed.

    A fingerprint describes what produced the graph, so it has to gate on the
    same question the graph builder asks before running a frontend. The two
    predicates being identical is deliberate, and a comment saying so is not
    checkable -- this is.

    Tightening one side alone looks safe and is the inverse defect: a `go.mod`
    with no `.go` files would report the tool inactive while the frontend still
    runs, so a toolchain upgrade before the first `.go` file appears goes
    unrecorded. False staleness costs a re-index; missed staleness costs a
    silently wrong graph.
    """
    from codebase_rag import constants as cs
    from codebase_rag.parsers.frontends import FRONTENDS

    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.23\n")

    go_frontend = FRONTENDS[cs.SupportedLanguage.GO]
    with patch(
        "codebase_rag.parsers.go_frontend.resolve_go_frontend",
        return_value=cs.GoFrontend.GOTYPES,
    ):
        fingerprint_says = pf._active_tools(tmp_path)["GO_VERSION"]

    assert fingerprint_says is go_frontend.applies(tmp_path), (
        "the fingerprint's activity predicate diverged from GoFrontend.applies(); "
        "they must answer the same question or the fingerprint describes a "
        "different run than the one that happened"
    )


def test_version_probes_run_from_the_indexed_repository(tmp_path) -> None:
    """Go and .NET select a toolchain from the working directory.

    `go.work` (and .NET's `global.json`) can pin a different toolchain, so the
    same `go version` command answers differently depending on cwd. Verified
    directly: a clean directory reports the installed 1.26.6, while one with a
    `go.work` pinning 1.99.0 tries to download that instead.

    Probing from the caller's cwd therefore records a version the indexed
    repository would never use, and the fingerprint then describes a toolchain
    that did not produce the graph -- missed staleness, the expensive
    direction.
    """
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.23\n")

    seen: list[object] = []

    def _capture(cmd, **kwargs):
        seen.append(kwargs.get("cwd"))
        return subprocess.CompletedProcess(
            cmd, 0, stdout="go version go1.23", stderr=""
        )

    with (
        patch.object(pf, "_active_tools", return_value={"GO_VERSION": True}),
        patch.object(pf.subprocess, "run", _capture),
    ):
        pf._tool_versions(tmp_path)

    assert seen, "no probe ran"
    assert all(cwd == str(tmp_path) for cwd in seen), seen


def test_a_repo_less_probe_does_not_force_a_working_directory(tmp_path) -> None:
    """The control: with no repository, the probe must not invent a cwd.

    Passing a bogus directory would be worse than inheriting the caller's, and
    asserting only the positive case above would pass against an
    implementation that hardcoded some path.
    """
    seen: list[object] = []

    def _capture(cmd, **kwargs):
        seen.append(kwargs.get("cwd"))
        return subprocess.CompletedProcess(
            cmd, 0, stdout="go version go1.23", stderr=""
        )

    with (
        patch.object(pf, "_active_tools", return_value={"GO_VERSION": True}),
        patch.object(pf.subprocess, "run", _capture),
    ):
        pf._tool_versions(None)

    assert seen, "no probe ran"
    assert all(cwd is None for cwd in seen), seen
