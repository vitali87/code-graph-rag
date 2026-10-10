"""Issue #2395: the go/types helper builds on the Go most machines have.

A Dependabot bump of golang.org/x/tools raised the helper's `go` directive to
1.26.0. The helper builds with GOTOOLCHAIN=local, so every machine on Go 1.25
or older lost the go/types frontend for every repository, logged the build
failure twice (the second time with an empty stderr), and retried the build
on every sync.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
import yaml
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.parsers import build_lock
from codebase_rag.parsers.go_frontend import frontend as fe

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "codebase_rag" / "parsers" / "go_frontend" / "gotypes"
# The oldest Go the helper asks for. It parses the repositories it indexes
# with go/parser, so the floor is the first release carrying the stdlib fixes
# OSV reports as reachable from it: GO-2024-3105 and GO-2024-3107 (1.22.7),
# GO-2025-3750 (1.23.10) and GO-2025-3956 (1.23.12).
SUPPORTED_FLOOR = (1, 23, 12)
# Bounds every wait in the two-worker tests, so a regression fails them
# instead of hanging the run.
_RACE_TIMEOUT_S = 30.0


def _directive(name: str) -> str | None:
    for line in (HELPER / "go.mod").read_text(encoding=cs.ENCODING_UTF8).splitlines():
        if line.startswith(f"{name} "):
            return line.split()[1]
    return None


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text))


class _FakeGo:
    """Answers the two commands the frontend runs before any analysis."""

    def __init__(
        self, version: str, build_ok: bool, time_out_first: bool = False
    ) -> None:
        self.version = version
        self.build_ok = build_ok
        self.time_out_first = time_out_first
        self.builds = 0

    def __call__(
        self, cmd: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if list(cmd[1:]) == ["env", "GOVERSION"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{self.version}\n")
        if len(cmd) > 1 and cmd[1] == "build":
            self.builds += 1
            if self.time_out_first and self.builds == 1:
                raise subprocess.TimeoutExpired(cmd, timeout=1)
            if self.build_ok:
                out = Path(cmd[cmd.index("-o") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("binary", encoding=cs.ENCODING_UTF8)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="go: go.mod requires go >= 9.0"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout='{"calls": []}\n', stderr="")


class _GatedFakeGo(_FakeGo):
    """A `go` whose first build holds the build lock until a second worker is
    queued on that lock, so the two syncs race the same way on every run."""

    def __init__(self, version: str, build_ok: bool) -> None:
        super().__init__(version, build_ok)
        self.building = threading.Event()
        self.waiter_queued = threading.Event()

    def watch_lock(self, try_lock: Callable[[int], bool]) -> Callable[[int], bool]:
        def spy(fd: int) -> bool:
            taken = try_lock(fd)
            if not taken:
                self.waiter_queued.set()
            return taken

        return spy

    def __call__(
        self, cmd: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if len(cmd) > 1 and cmd[1] == "build" and not self.building.is_set():
            self.building.set()
            self.waiter_queued.wait(_RACE_TIMEOUT_S)
        return super().__call__(cmd, **kwargs)


class _Clock:
    """Stands in for the frontend's `time` module, so a test can age a failure
    marker without sleeping."""

    def __init__(self) -> None:
        self.now = time.time()

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(fe, "time", fake)
    return fake


@pytest.fixture
def go_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n")
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n")
    return repo


@pytest.fixture
def cgr_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "cgr_home"
    monkeypatch.setattr(settings, "CGR_HOME", home)
    monkeypatch.setattr(fe.shutil, "which", lambda _name: "/usr/bin/go")
    return home


def _run(
    monkeypatch: pytest.MonkeyPatch, repo: Path, fake: _FakeGo
) -> tuple[fe.GoSemanticFacts, list[str]]:
    monkeypatch.setattr(fe.subprocess, "run", fake)
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        facts = fe.run_go_frontend(repo)
    finally:
        logger.remove(sink)
    return facts, warnings


def _race_two_workers(
    monkeypatch: pytest.MonkeyPatch, fake: _GatedFakeGo
) -> tuple[dict[str, Path | None], list[str]]:
    """Two syncs build the helper at once over the real build lock: the second
    starts while the first is building, and the first's build returns only
    once the second is queued on the lock. The lock is an OS file lock taken
    through a descriptor of its own per acquire (flock / msvcrt.locking), so
    two threads exclude each other just as two processes do."""
    monkeypatch.setattr(fe.subprocess, "run", fake)
    monkeypatch.setattr(build_lock, "_try_lock", fake.watch_lock(build_lock._try_lock))
    monkeypatch.setattr(fe, "_LOCK_POLL_SECONDS", 0.01)
    results: dict[str, Path | None] = {}

    def worker(name: str) -> None:
        results[name] = fe._build_tool("/usr/bin/go")

    first = threading.Thread(target=worker, args=("first",))
    second = threading.Thread(target=worker, args=("second",))
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        first.start()
        fake.building.wait(_RACE_TIMEOUT_S)
        second.start()
        first.join(_RACE_TIMEOUT_S)
        second.join(_RACE_TIMEOUT_S)
    finally:
        logger.remove(sink)
    assert fake.waiter_queued.is_set()
    return results, warnings


def test_parallel_syncs_share_one_failed_build(
    monkeypatch: pytest.MonkeyPatch, cgr_home: Path
) -> None:
    # Both syncs pass the marker check before the first build fails; the one
    # queued on the lock must find the marker that build dated instead of
    # running another failing build (PR #2417 review).
    fake = _GatedFakeGo("go1.26.0", build_ok=False)

    results, warnings = _race_two_workers(monkeypatch, fake)

    assert fake.builds == 1
    assert results == {"first": None, "second": None}
    assert len(warnings) == 2
    assert sum("go.mod requires go" in w for w in warnings) == 1
    assert sum("go1.26.0" in w for w in warnings) == 1


def test_parallel_syncs_retry_an_expired_failure_once(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, clock: _Clock
) -> None:
    # Past the TTL the queued sync still defers to the retry the first one
    # just made and failed, so an expired marker costs one build, not one each.
    _run(monkeypatch, go_repo, _FakeGo("go1.26.0", build_ok=False))
    clock.now += cs.GO_FRONTEND_BUILD_FAILURE_TTL_S + 1
    fake = _GatedFakeGo("go1.26.0", build_ok=False)

    results, _warnings = _race_two_workers(monkeypatch, fake)

    assert fake.builds == 1
    assert results == {"first": None, "second": None}


def test_a_queued_sync_reuses_the_helper_just_built(
    monkeypatch: pytest.MonkeyPatch, cgr_home: Path
) -> None:
    fake = _GatedFakeGo("go1.26.0", build_ok=True)

    results, warnings = _race_two_workers(monkeypatch, fake)

    binary = fe._cache_dir() / "out" / fe._BINARY_NAME
    assert fake.builds == 1
    assert results == {"first": binary, "second": binary}
    assert warnings == []


def test_the_helper_asks_for_no_newer_go_than_the_floor() -> None:
    directive = _directive("go")
    assert directive is not None
    assert _version(directive) <= SUPPORTED_FLOOR


def test_the_helper_asks_for_a_go_with_the_stdlib_fixes() -> None:
    # The helper links the stdlib of whichever Go builds it, so a floor below
    # these fixes lets it build against a go/parser with the known
    # stack-exhaustion bug (review of PR 2417). This test is the only check:
    # since osv-scanner 2.4.0 the CI scan skips the stdlib version in go.mod.
    directive = _directive("go")
    assert directive is not None
    assert _version(directive) >= SUPPORTED_FLOOR


def test_the_helper_pins_no_newer_toolchain() -> None:
    toolchain = _directive("toolchain")
    assert toolchain is None or _version(toolchain) <= SUPPORTED_FLOOR


def test_the_helper_keeps_alias_types_distinct_on_every_supported_go() -> None:
    # Below go 1.23 the default GODEBUG resolves `type A = B` to B's Named
    # type, and the implements pass would then list the alias as a second
    # declaration of B.
    main = (HELPER / "main.go").read_text(encoding=cs.ENCODING_UTF8)
    assert "//go:debug gotypesalias=1" in main


def test_dependabot_holds_x_tools_to_patch_releases() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding=cs.ENCODING_UTF8)
    )
    [gomod] = [u for u in config["updates"] if u["package-ecosystem"] == "gomod"]
    tools = [
        rule
        for rule in gomod.get("ignore") or []
        if rule.get("dependency-name") == "golang.org/x/tools"
    ]
    assert tools
    assert all(
        set(rule.get("update-types") or [])
        >= {"version-update:semver-minor", "version-update:semver-major"}
        for rule in tools
    )


@pytest.mark.parametrize("found", ["1.21.9", "1.22.6", "1.23.11"])
def test_an_old_go_is_named_once_and_never_built(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, found: str
) -> None:
    fake = _FakeGo(f"go{found}", build_ok=True)

    facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 0
    assert not facts.call_sites
    assert len(warnings) == 1
    assert found in warnings[0]
    assert "1.23.12" in warnings[0]


def test_a_failed_build_is_logged_once(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    _facts, warnings = _run(monkeypatch, go_repo, _FakeGo("go1.26.0", build_ok=False))

    assert len(warnings) == 1
    assert "go.mod requires go" in warnings[0]


def test_a_failed_build_is_not_retried_on_the_next_sync(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    fake = _FakeGo("go1.26.0", build_ok=False)
    _run(monkeypatch, go_repo, fake)

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 1
    assert len(warnings) == 1


def test_a_timed_out_build_is_retried_on_the_next_sync(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    # A wedged toolchain or a slow module fetch says nothing about whether the
    # helper builds, so it is not remembered (PR #2417 review).
    fake = _FakeGo("go1.26.0", build_ok=True, time_out_first=True)
    _run(monkeypatch, go_repo, fake)

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 2
    assert warnings == []


def test_a_failure_older_than_the_ttl_is_rebuilt(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, clock: _Clock
) -> None:
    # A failure the environment caused (no network, a module proxy outage)
    # clears without Go or the helper changing, and the marker sits in a cache
    # every repository shares, so it must not keep go/types facts off for long
    # after (PR #2417 review).
    fake = _FakeGo("go1.26.0", build_ok=False)
    _run(monkeypatch, go_repo, fake)
    fake.build_ok = True
    clock.now += cs.GO_FRONTEND_BUILD_FAILURE_TTL_S + 1

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 2
    assert warnings == []


def test_a_failure_within_the_ttl_skips_the_build_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, clock: _Clock
) -> None:
    fake = _FakeGo("go1.26.0", build_ok=False)
    _run(monkeypatch, go_repo, fake)
    clock.now += cs.GO_FRONTEND_BUILD_FAILURE_TTL_S - 1

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 1
    assert len(warnings) == 1
    assert "go1.26.0" in warnings[0]


def test_the_failure_time_comes_from_the_marker_not_its_mtime(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, clock: _Clock
) -> None:
    # Copying or restoring the cache can give the marker a fresh mtime, but the
    # failure is still as old as the marker records.
    fake = _FakeGo("go1.26.0", build_ok=False)
    _run(monkeypatch, go_repo, fake)
    [marker] = cgr_home.rglob(fe._BUILD_FAILED_MARKER)
    clock.now += cs.GO_FRONTEND_BUILD_FAILURE_TTL_S + 1
    os.utime(marker, (clock.now, clock.now))
    fake.build_ok = True

    _run(monkeypatch, go_repo, fake)

    assert fake.builds == 2


def test_a_failure_dated_after_now_is_rebuilt(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, clock: _Clock
) -> None:
    # After the clock moves back, a marker dated ahead of it would otherwise
    # hold until real time caught up, plus the TTL.
    fake = _FakeGo("go1.26.0", build_ok=False)
    _run(monkeypatch, go_repo, fake)
    fake.build_ok = True
    clock.now -= 24 * cs.GO_FRONTEND_BUILD_FAILURE_TTL_S

    _run(monkeypatch, go_repo, fake)

    assert fake.builds == 2


def test_a_new_toolchain_retries_a_remembered_failure(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    _run(monkeypatch, go_repo, _FakeGo("go1.26.0", build_ok=False))
    upgraded = _FakeGo("go1.26.1", build_ok=True)

    _run(monkeypatch, go_repo, upgraded)

    assert upgraded.builds == 1


def test_changed_helper_sources_retry_a_remembered_failure(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    fake = _FakeGo("go1.26.0", build_ok=False)
    _run(monkeypatch, go_repo, fake)
    monkeypatch.setattr(fe, "_newest_source_mtime", lambda: 4102444800.0)

    _run(monkeypatch, go_repo, fake)

    assert fake.builds == 2


def test_a_new_enough_go_builds_the_helper(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    fake = _FakeGo("go1.23.12", build_ok=True)

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 1
    assert warnings == []


def test_an_unreadable_go_version_does_not_block_the_build(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    fake = _FakeGo("devel +abcdef", build_ok=True)

    _run(monkeypatch, go_repo, fake)

    assert fake.builds == 1


_ALIAS_FIXTURE = """package main

type Speaker interface{ Speak() string }

type Dog struct{}

func (d Dog) Speak() string { return "woof" }

type Pup = Dog

func main() { _ = Pup{} }
"""


def test_an_alias_is_not_a_second_implementer(tmp_path: Path) -> None:
    go = fe.shutil.which("go")
    if go is None:
        pytest.skip("go toolchain not available")
    if fe._build_tool(go) is None:
        pytest.skip("gotypes tool could not build in this environment")
    (tmp_path / "go.mod").write_text("module example.com/alias\n\ngo 1.22\n")
    (tmp_path / "main.go").write_text(_ALIAS_FIXTURE, encoding=cs.ENCODING_UTF8)

    facts = fe.run_go_frontend(tmp_path)

    assert [pair.impl_line for pair in facts.implements] == [5]
