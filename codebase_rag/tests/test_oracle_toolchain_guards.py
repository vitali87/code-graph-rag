"""The oracle skip guards must answer "can this toolchain do the job" (#1639).

Every one of these guards used to be `shutil.which(...)`, which answers "is
there a file on PATH". A toolchain that is present but too old sails past that
and the test HARD-FAILS instead of skipping: on a machine with Node 18 and
.NET SDK 8 that was 24 failures and 4 errors on a clean main, in tests named as
though they were real regressions.

These tests pin the guards against stub toolchains, because the failure they
exist to prevent cannot be reproduced on a machine whose toolchains work. The
stubs are the point: a green run on a well-provisioned host says nothing about
the case the guards were written for.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import typer
from loguru import logger

from evals import constants as ec
from evals.oracles import _common
from evals.oracles._common import (
    _REQUIRE_OK,
    NodeOracleUnavailable,
    _oracle_dependency,
    node_oracle_available,
    node_oracle_skip_reason,
)
from evals.oracles.csharp_oracle import (
    _sdk_major,
    csharp_oracle_available,
    csharp_oracle_skip_reason,
)

_ORACLE_CSPROJ = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "oracles"
    / ec.CSHARP_ORACLE_DIRNAME
    / "Oracle.csproj"
)


def _stub(
    directory: Path,
    name: str,
    *,
    stdout: str = "",
    stderr: str = "",
    code: int = 0,
    fail_when_arg_contains: str | None = None,
) -> None:
    """Put a fake `name` on PATH that behaves the same on POSIX and Windows.

    A `#!/bin/sh` script with the executable bit is invisible to Windows:
    `shutil.which` resolves through PATHEXT, so an extensionless shell script
    is not found and the guards reported "node is not on PATH" instead of the
    stubbed verdict -- 8 failures on both Windows jobs, in tests that pass
    everywhere else.

    The stub is therefore a Python script plus, on Windows, a `.cmd` wrapper
    that PATHEXT does find. Python rather than batch because the bodies here
    branch on an argument, and translating that to `.cmd` would be a second
    implementation to keep in step with the POSIX one.

    `fail_when_arg_contains` reproduces Node 18: the `require()` probe is
    refused while anything else succeeds. It must discriminate on the
    ARGUMENT, or a test cannot tell a probe that exercises the breaking call
    from one that does not.
    """
    logic = f"""import sys
argv = sys.argv[1:]
needle = {fail_when_arg_contains!r}
if needle is not None and not any(needle in a for a in argv):
    sys.exit(0)
if {stdout!r}:
    sys.stdout.write({stdout!r} + "\\n")
if {stderr!r}:
    sys.stderr.write({stderr!r} + "\\n")
sys.exit({code!r})
"""
    worker = directory / f"{name}_stub.py"
    worker.write_text(logic, encoding="utf-8")
    if sys.platform == "win32":
        # PATHEXT includes .CMD, so this is what `shutil.which` finds.
        (directory / f"{name}.cmd").write_text(
            f'@echo off\r\n"{sys.executable}" "{worker}" %*\r\n', encoding="utf-8"
        )
    else:
        launcher = directory / name
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{worker}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)


@pytest.fixture(autouse=True)
def _clear_guard_caches() -> Iterator[None]:
    """Clear both guard caches BEFORE and AFTER each test.

    Teardown matters as much as setup: `csharp_oracle_skip_reason` is cached
    with no arguments, so a verdict cached under a test's temporary PATH would
    answer for the next test, and for production code in the same process,
    long after `monkeypatch` restored the environment.

    `_REQUIRE_OK` holds only POSITIVE verdicts (rule 1 in
    `node_oracle_available`), so it is a set rather than an lru_cache.
    """
    _REQUIRE_OK.clear()
    csharp_oracle_skip_reason.cache_clear()
    yield
    _REQUIRE_OK.clear()
    csharp_oracle_skip_reason.cache_clear()


class TestSdkMajor:
    """The version parse must fail CLOSED: unreadable means unusable."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("10.0.400 [/usr/share/dotnet/sdk]", 10),
            ("8.0.130 [/usr/share/dotnet/sdk]", 8),
            # A preview SDK is still that major version.
            ("10.0.100-preview.5.24307.3 [/x]", 10),
            ("10.0.400", 10),
            ("", 0),
            ("   ", 0),
            ("Nothing here", 0),
        ],
    )
    def test_major_version_is_read_or_refused(self, line: str, expected: int) -> None:
        assert _sdk_major(line) == expected


class TestCsharpGuard:
    def test_an_sdk_older_than_the_csproj_is_not_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported failure: SDK 8 present, csproj targets net10.0.

        `which` was satisfied here and the build then died with NETSDK1045.
        """
        _stub(tmp_path, "dotnet", stdout="8.0.130 [/usr/share/dotnet/sdk]")
        monkeypatch.setenv("PATH", str(tmp_path))
        assert csharp_oracle_available() is False

    def test_an_sdk_at_least_the_csproj_is_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(tmp_path, "dotnet", stdout="10.0.400 [/usr/share/dotnet/sdk]")
        monkeypatch.setenv("PATH", str(tmp_path))
        assert csharp_oracle_available() is True

    def test_a_newer_sdk_is_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An exact-TFM check would wrongly skip here: SDK 11 builds net10.0.
        _stub(tmp_path, "dotnet", stdout="11.0.100 [/usr/share/dotnet/sdk]")
        monkeypatch.setenv("PATH", str(tmp_path))
        assert csharp_oracle_available() is True

    def test_a_runtime_only_install_is_not_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `dotnet` exists and runs, but lists no SDKs: the launcher is present
        # for a runtime-only install, which cannot build anything.
        _stub(tmp_path, "dotnet")
        monkeypatch.setenv("PATH", str(tmp_path))
        assert csharp_oracle_available() is False

    def test_no_dotnet_at_all_is_not_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        assert csharp_oracle_available() is False


class TestCsprojDrift:
    def test_the_minimum_sdk_matches_the_csproj_target(self) -> None:
        """The forcing function a comment cannot provide.

        `CSHARP_ORACLE_MIN_SDK_MAJOR` duplicates the csproj's TargetFramework.
        Nothing else in the repo reads that file, so a bump to net11.0 would
        leave the guard silently under-requiring and the NETSDK1045 failures
        would come back exactly as reported.
        """
        target = re.search(
            r"<TargetFramework>net(\d+)\.", _ORACLE_CSPROJ.read_text(encoding="utf-8")
        )
        assert target is not None, f"no TargetFramework in {_ORACLE_CSPROJ}"
        assert int(target.group(1)) == ec.CSHARP_ORACLE_MIN_SDK_MAJOR


class TestNodeGuard:
    """The probe must be the call that BREAKS, not one that merely runs."""

    @staticmethod
    def _oracle(tmp_path: Path, package: str) -> Path:
        oracle = tmp_path / "oracle"
        (oracle / ec.NODE_MODULES_DIRNAME).mkdir(parents=True)
        # The MARKER, not just the directory: npm creates node_modules before
        # populating it, so the guard treats a marker-less tree as "installing"
        # and declines to probe it (rule 2).
        (oracle / ec.NODE_DEPS_MARKER).write_text("ok", encoding="utf-8")
        (oracle / "oracle_ast.js").write_text(
            f'const fs = require("fs");\nconst p = require("{package}");\n',
            encoding="utf-8",
        )
        return oracle

    def test_a_node_that_cannot_require_the_dependency_is_not_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Node 18's shape: `require()` of an ESM-only package is refused.

        This is the case a dynamic-`import()` probe could not see, because
        dynamic import has worked since Node 12: such a probe returned True on
        the very version that hard-fails the oracle.
        """
        binaries = tmp_path / "bin"
        binaries.mkdir()
        # The stub answers like Node 18: a `require()` of an ES module is
        # refused, while anything else (a dynamic `import()`, say) succeeds.
        # It must discriminate on the ARGUMENT, or the test cannot tell a
        # probe that exercises the breaking call from one that does not --
        # and a dynamic-import probe passing here is precisely the bug.
        _stub(
            binaries,
            "node",
            stderr="Error [ERR_REQUIRE_ESM]",
            code=1,
            fail_when_arg_contains="require",
        )
        _stub(binaries, "npm")
        monkeypatch.setenv("PATH", str(binaries))
        assert node_oracle_available(self._oracle(tmp_path, "@ruby/prism")) is False

    def test_a_node_that_can_require_the_dependency_is_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binaries = tmp_path / "bin"
        binaries.mkdir()
        _stub(binaries, "node")
        _stub(binaries, "npm")
        monkeypatch.setenv("PATH", str(binaries))
        assert node_oracle_available(self._oracle(tmp_path, "@ruby/prism")) is True

    def test_uninstalled_deps_are_not_reported_as_a_broken_toolchain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Not fetched yet" must not read as "node is broken".

        `ensure_node_deps` installs them later; refusing here would skip a run
        that was about to become perfectly capable.
        """
        binaries = tmp_path / "bin"
        binaries.mkdir()
        _stub(binaries, "node", code=1)
        _stub(binaries, "npm")
        monkeypatch.setenv("PATH", str(binaries))
        bare = tmp_path / "oracle"
        bare.mkdir()
        assert node_oracle_available(bare) is True

    def test_no_node_at_all_is_not_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        assert node_oracle_available(self._oracle(tmp_path, "luaparse")) is False


class TestOracleDependency:
    @staticmethod
    def _with_script(tmp_path: Path, source: str) -> Path:
        oracle = tmp_path / "oracle"
        (oracle / ec.NODE_MODULES_DIRNAME).mkdir(parents=True)
        (oracle / "oracle_ast.js").write_text(source, encoding="utf-8")
        return oracle

    def test_the_package_is_the_one_the_script_requires(self, tmp_path: Path) -> None:
        """Read from the oracle's own `require()`, never guessed.

        The four oracles do not share a dependency, and only `@ruby/prism` is
        ESM-only, so the probe must ask about the package THIS oracle loads.
        """
        oracle = self._with_script(tmp_path, 'const p = require("luaparse");\n')
        assert _oracle_dependency(oracle) == "luaparse"

    def test_an_extra_dependency_does_not_displace_the_real_one(
        self, tmp_path: Path
    ) -> None:
        """The alphabetical-pick defect.

        Reading `package.json` and taking the first key sorts `aaa-helper`
        ahead of `luaparse`, so the probe would validate a package the oracle
        never loads and report a broken toolchain as usable. It picked the
        right entry today only because each manifest happens to hold exactly
        one dependency -- a latent bug, not a theoretical one.
        """
        oracle = self._with_script(tmp_path, 'const p = require("luaparse");\n')
        (oracle / ec.NODE_PACKAGE_MANIFEST).write_text(
            '{"dependencies": {"aaa-helper": "1.0.0", "luaparse": "0.3.1"}}',
            encoding="utf-8",
        )
        assert _oracle_dependency(oracle) == "luaparse"

    def test_builtins_and_relative_requires_are_skipped(self, tmp_path: Path) -> None:
        """A builtin loads on every Node, so probing one proves nothing."""
        oracle = self._with_script(
            tmp_path,
            'const fs = require("fs");\nconst u = require("./util");\n'
            'const p = require("php-parser");\n',
        )
        assert _oracle_dependency(oracle) == "php-parser"

    def test_uninstalled_or_scriptless_oracles_yield_no_package(
        self, tmp_path: Path
    ) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()
        assert _oracle_dependency(bare) is None
        installed = tmp_path / "installed"
        (installed / ec.NODE_MODULES_DIRNAME).mkdir(parents=True)
        assert _oracle_dependency(installed) is None


class TestGuardCacheLifecycle:
    def test_a_failed_probe_is_never_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rule 1: only a POSITIVE verdict is remembered.

        Same node, same directory, same package, but the require becomes
        loadable between two calls -- which is exactly what an install does.
        Caching the first answer makes the second wrong for the rest of the
        process. No other test here probes twice across a state change, so
        without this the rule is unpinned: verified by mutation, re-enabling
        negative caching left the whole suite green.
        """
        binaries = tmp_path / "bin"
        binaries.mkdir()
        # Rewritten mid-test below, so it goes through `_stub` both times and
        # stays portable: a raw `#!/bin/sh` here would be unfindable on
        # Windows and this test would assert against "node is not on PATH".
        _stub(binaries, "node", code=1)
        _stub(binaries, "npm")
        monkeypatch.setenv("PATH", str(binaries))

        oracle = tmp_path / "oracle"
        (oracle / ec.NODE_MODULES_DIRNAME).mkdir(parents=True)
        (oracle / ec.NODE_DEPS_MARKER).write_text("ok", encoding="utf-8")
        (oracle / "oracle_ast.js").write_text(
            'const p = require("pkg");\n', encoding="utf-8"
        )

        assert node_oracle_available(oracle) is False
        # The failure must not be remembered under the probe key. Caching it
        # makes the NEXT call read the cache as a hit and report "available",
        # which is worse than the original defect: an unusable toolchain now
        # reports usable. Asserting the second call still says False (with the
        # node still refusing) is what catches that -- asserting it flips to
        # True instead would ACCEPT the poisoned cache, since a cache hit
        # yields exactly that.
        assert node_oracle_available(oracle) is False, (
            "a failed probe was cached, so the second call read the cache as "
            "a success and reported an unusable toolchain as available"
        )
        assert not _REQUIRE_OK, f"a failure was remembered: {_REQUIRE_OK}"

        # And a real success IS remembered, or rule 1 would be satisfied by
        # caching nothing at all.
        _stub(binaries, "node")
        assert node_oracle_available(oracle) is True
        assert _REQUIRE_OK, "a successful probe was not cached"

    def test_a_pre_install_answer_is_not_cached(self, tmp_path: Path) -> None:
        """Issue #1639: the provisional "ask again" must not become a verdict.

        On a clean checkout the guard runs before `ensure_node_deps`, so it
        cannot probe and answers True to avoid mistaking "not fetched" for
        "toolchain broken". Caching that froze it: the real probe never ran,
        and an incompatible Node reached the oracle anyway. Only genuine
        verdicts are cached.
        """
        oracle = tmp_path / "oracle"
        oracle.mkdir()
        (oracle / "oracle_ast.js").write_text(
            'const p = require("definitely-not-installed");\n', encoding="utf-8"
        )
        assert node_oracle_available(oracle) is True

        # ensure_node_deps installs them AND writes the completion marker;
        # only then does the guard probe (rule 2), and it must now actually do
        # so rather than reuse the pre-install answer (rule 1).
        (oracle / ec.NODE_MODULES_DIRNAME).mkdir()
        (oracle / ec.NODE_DEPS_MARKER).write_text("ok", encoding="utf-8")
        assert node_oracle_available(oracle) is False

        # A marker-less tree is "installing", never "unavailable": probing it
        # measures a half-written directory, and caching that False is the
        # third variant of this defect.
        half = tmp_path / "half"
        (half / ec.NODE_MODULES_DIRNAME).mkdir(parents=True)
        (half / "oracle_ast.js").write_text(
            'const p = require("definitely-not-installed");\n', encoding="utf-8"
        )
        assert node_oracle_available(half) is True


class TestSkipReasons:
    """The reason must name the obstacle, not restate the guard (#1639).

    "dotnet toolchain not installed" was printed on a machine where dotnet IS
    installed, which is the whole complaint: the skip line told a developer
    nothing and sent them looking for a missing package that was present.
    """

    def test_an_old_sdk_reason_names_the_versions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(tmp_path, "dotnet", stdout="8.0.130 [/usr/share/dotnet/sdk]")
        monkeypatch.setenv("PATH", str(tmp_path))
        reason = csharp_oracle_skip_reason()
        assert reason is not None
        # Both halves: what was found and what is needed.
        assert "8.0.130" in reason
        assert str(ec.CSHARP_ORACLE_MIN_SDK_MAJOR) in reason
        # And it must not repeat the FALSE claim this replaces. Asserting only
        # that the values appear is satisfied by "dotnet toolchain not
        # installed" with the numbers concatenated onto it -- verified by
        # mutation: that string passed the two assertions above.
        assert "not installed" not in reason
        assert "not available" not in reason

    def test_a_missing_binary_reason_names_the_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        reason = csharp_oracle_skip_reason()
        assert reason is not None
        assert ec.DOTNET_BIN in reason

    def test_a_node_reason_carries_the_require_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binaries = tmp_path / "bin"
        binaries.mkdir()
        _stub(
            binaries,
            "node",
            stderr="Error [ERR_REQUIRE_ESM]: nope",
            code=1,
            fail_when_arg_contains="require",
        )
        _stub(binaries, "npm")
        monkeypatch.setenv("PATH", str(binaries))
        oracle = tmp_path / "oracle"
        (oracle / ec.NODE_MODULES_DIRNAME).mkdir(parents=True)
        (oracle / ec.NODE_DEPS_MARKER).write_text("ok", encoding="utf-8")
        (oracle / "oracle_ast.js").write_text(
            'const p = require("@ruby/prism");\n', encoding="utf-8"
        )
        reason = node_oracle_skip_reason(oracle)
        assert reason is not None
        assert "@ruby/prism" in reason
        assert "ERR_REQUIRE_ESM" in reason
        # node IS on PATH here, so a reason saying otherwise is the bug.
        assert "not available" not in reason
        assert "not installed" not in reason

    def test_a_working_toolchain_has_no_reason(self, tmp_path: Path) -> None:
        """None is the signal the call sites branch on, so it must be exact."""
        assert node_oracle_skip_reason(None) is None


class TestPostInstallRecheck:
    def test_a_toolchain_that_turns_out_unusable_skips_rather_than_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The clean-checkout gap (#1639).

        The guard runs before `ensure_node_deps`, so on a fresh clone it
        cannot probe and answers "available" to avoid mistaking "not fetched"
        for "toolchain broken". Nothing revisited that once the deps landed,
        so an incompatible runtime reached the oracle and died with
        ERR_REQUIRE_ESM -- an evaluation FAILURE standing in for an
        unavailable toolchain, which is the whole defect.

        Asserting on the specific exception: an empty payload would grade
        every node as missing, and an unhandled error is the misdiagnosis this
        issue is about. `conftest.pytest_runtest_call` turns this into a skip
        for the ~24 real call sites; a standalone eval script sees an ordinary
        exception it can report, which `pytest.skip` in library code could not
        give it.
        """
        binaries = tmp_path / "bin"
        binaries.mkdir()
        _stub(binaries, "node", code=1, fail_when_arg_contains="require")
        _stub(binaries, "npm")
        monkeypatch.setenv("PATH", str(binaries))

        oracle = tmp_path / "oracle"
        oracle.mkdir()
        script = oracle / "oracle_ast.js"
        script.write_text('const p = require("@ruby/prism");\n', encoding="utf-8")

        # Start WITHOUT the deps and let the patched installer create them, so
        # this distinguishes pre-install from post-install probing. With
        # node_modules already present, a regression that probed too early
        # would still see a complete tree and pass.
        def _install(_dir: Path) -> None:
            (oracle / ec.NODE_MODULES_DIRNAME).mkdir(exist_ok=True)
            (oracle / ec.NODE_DEPS_MARKER).write_text("ok", encoding="utf-8")

        monkeypatch.setattr(_common, "ensure_node_deps", _install)
        # Rule 2: before the marker exists the guard says "cannot tell yet",
        # never "unavailable". Caching a False here is what poisoned the
        # post-install check.
        assert node_oracle_skip_reason(oracle) is None

        with pytest.raises(NodeOracleUnavailable) as raised:
            _common.run_node_oracle_payload(oracle, script, ())
        # The reason travels with it, so the skip line names the real
        # obstacle rather than restating the guard.
        assert "@ruby/prism" in str(raised.value)


class TestStandaloneL1Runner:
    """A standalone eval must REPORT an unavailable toolchain, not crash.

    The four L1 scripts (`evals/lua_l1.py`, `ts_l1.py`, `php_l1.py`) call the
    oracle outside pytest, so an exception escaping the shared runner reaches
    the operator as a traceback rather than as the command's result.
    """

    @staticmethod
    def _run(**overrides: object) -> int:
        from evals import l1_eval
        from evals.types_defs import GraphData

        kwargs: dict[str, object] = {
            "available": lambda: True,
            "oracle_missing": "fixed message for {binary}",
            "extract_cgr": lambda _t, _p: GraphData(nodes=[], edges=[], name_edges=[]),
            "run_oracle": lambda _t: GraphData(nodes=[], edges=[], name_edges=[]),
            "oracle_binary": "node",
            "scored_node_kinds": frozenset(),
            "extracting_cgr": "x {target} {project}",
            "cgr_done": "y {count}",
            "extracting_oracle": "z {binary} {target}",
            "oracle_done": "w {count}",
            "scores_filename": "s.csv",
            "diff_filename": "d.json",
            "title": "t",
        }
        kwargs.update(overrides)
        with pytest.raises(typer.Exit) as exit_info:
            l1_eval.run_l1_eval(Path("."), "", Path("."), **kwargs)  # type: ignore[arg-type]
        return int(exit_info.value.exit_code)

    def test_a_post_install_failure_exits_rather_than_raising(self) -> None:
        """The path `available()` cannot cover.

        On a clean checkout the guard runs before the deps exist, so it
        honestly says "cannot tell yet"; the verdict only arrives inside
        `run_oracle`, once `ensure_node_deps` has fetched them.
        """

        def _unavailable(_target: Path) -> object:
            raise NodeOracleUnavailable("this node cannot require(luaparse): boom")

        # Caught HERE rather than left to propagate. `conftest`'s
        # `pytest_runtest_call` hook turns a leaked NodeOracleUnavailable into
        # a SKIP, so a runner that stopped catching it would make this test
        # vanish rather than fail -- verified by mutation: removing the
        # try/except gave "30 passed, 1 skipped". A skipped test is not a
        # passing one, but it is not a failing one either, and this must fail.
        try:
            assert self._run(run_oracle=_unavailable) == 1
        except NodeOracleUnavailable as leaked:  # pragma: no cover - the defect
            raise AssertionError(
                f"the L1 runner let the exception escape: {leaked}"
            ) from leaked

    def test_the_reason_replaces_the_false_fixed_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ "not found on PATH" is false when the binary is present.

        Asserting the fixed string is ABSENT as well as the reason present:
        checking only for the reason would pass for a runner that logged both.
        """
        messages: list[str] = []
        sink = logger.add(messages.append, level="ERROR")
        try:
            assert (
                self._run(
                    available=lambda: False,
                    skip_reason=lambda: "this node cannot require(luaparse): boom",
                )
                == 1
            )
        finally:
            logger.remove(sink)
        joined = "\n".join(messages)
        assert "cannot require(luaparse)" in joined
        assert "fixed message" not in joined, joined

    def test_an_oracle_without_a_reason_probe_keeps_the_fixed_message(self) -> None:
        """The fallback must survive: not every oracle has a reason probe."""
        messages: list[str] = []
        sink = logger.add(messages.append, level="ERROR")
        try:
            assert self._run(available=lambda: False) == 1
        finally:
            logger.remove(sink)
        assert "fixed message for node" in "\n".join(messages)
