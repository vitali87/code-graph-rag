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
from collections.abc import Iterator
from pathlib import Path

import pytest

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


def _stub(directory: Path, name: str, body: str) -> None:
    script = directory / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)


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
        _stub(tmp_path, "dotnet", 'echo "8.0.130 [/usr/share/dotnet/sdk]"')
        monkeypatch.setenv("PATH", str(tmp_path))
        assert csharp_oracle_available() is False

    def test_an_sdk_at_least_the_csproj_is_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(tmp_path, "dotnet", 'echo "10.0.400 [/usr/share/dotnet/sdk]"')
        monkeypatch.setenv("PATH", str(tmp_path))
        assert csharp_oracle_available() is True

    def test_a_newer_sdk_is_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An exact-TFM check would wrongly skip here: SDK 11 builds net10.0.
        _stub(tmp_path, "dotnet", 'echo "11.0.100 [/usr/share/dotnet/sdk]"')
        monkeypatch.setenv("PATH", str(tmp_path))
        assert csharp_oracle_available() is True

    def test_a_runtime_only_install_is_not_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `dotnet` exists and runs, but lists no SDKs: the launcher is present
        # for a runtime-only install, which cannot build anything.
        _stub(tmp_path, "dotnet", "exit 0")
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
            'case "$2" in\n'
            '  *require*) echo "Error [ERR_REQUIRE_ESM]" >&2; exit 1 ;;\n'
            "  *) exit 0 ;;\n"
            "esac",
        )
        _stub(binaries, "npm", "exit 0")
        monkeypatch.setenv("PATH", str(binaries))
        assert node_oracle_available(self._oracle(tmp_path, "@ruby/prism")) is False

    def test_a_node_that_can_require_the_dependency_is_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binaries = tmp_path / "bin"
        binaries.mkdir()
        _stub(binaries, "node", "exit 0")
        _stub(binaries, "npm", "exit 0")
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
        _stub(binaries, "node", "exit 1")
        _stub(binaries, "npm", "exit 0")
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
        node = binaries / "node"
        node.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        node.chmod(0o755)
        _stub(binaries, "npm", "exit 0")
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
        node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        node.chmod(0o755)
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
        _stub(tmp_path, "dotnet", 'echo "8.0.130 [/usr/share/dotnet/sdk]"')
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
            'case "$2" in\n'
            '  *require*) echo "Error [ERR_REQUIRE_ESM]: nope" >&2; exit 1 ;;\n'
            "  *) exit 0 ;;\n"
            "esac",
        )
        _stub(binaries, "npm", "exit 0")
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
        _stub(
            binaries,
            "node",
            'case "$2" in\n  *require*) exit 1 ;;\n  *) exit 0 ;;\nesac',
        )
        _stub(binaries, "npm", "exit 0")
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
