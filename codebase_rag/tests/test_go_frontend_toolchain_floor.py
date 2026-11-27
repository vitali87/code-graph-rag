"""The go/types helper's Go floor, and how an older Go is handled (issue #2395).

The helper builds with GOTOOLCHAIN=local, so its `go` directive is the oldest
Go that gets go/types facts. An older Go is named in one warning and falls
back to Tree-sitter, a failed build is logged once, and it is not retried on
every sync.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
import yaml
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.config import settings
from codebase_rag.parsers import build_lock
from codebase_rag.parsers.go_frontend import frontend as fe

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "codebase_rag" / "parsers" / "go_frontend" / "gotypes"
# The oldest Go the helper asks for: the first release with no open advisory
# against the stdlib it links or the cmd/go that go/packages drives to load
# the indexed repository. GO-2026-6179 and GO-2026-6180 (sumdb bypasses in
# cmd/go and in golang.org/x/mod, which x/mod v0.40.0 fixes and which needs
# go 1.25.0) and GO-2026-6601 and GO-2026-6602 (checksum bypasses in cmd/go)
# are fixed on the newest line only in 1.27.2; 1.27.0 and 1.27.1 lack them,
# so no lower floor admits only fixed toolchains.
SUPPORTED_FLOOR = (1, 27, 2)
# Bounds every wait in the two-worker tests, so a regression fails them
# instead of hanging the run.
_RACE_TIMEOUT_S = 30.0
# Kept before a test replaces `subprocess.run`, so a fake `go` can still
# start a real process and have `subprocess` decode its output exactly as the
# frontend asks it to, on every platform.
_REAL_RUN = subprocess.run


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
        self.runs = 0

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
        self.runs += 1
        return subprocess.CompletedProcess(cmd, 0, stdout='{"calls": []}\n', stderr="")


class _PrintingFakeGo(_FakeGo):
    """A `go` whose build fails printing `stderr`, which need not be UTF-8,
    from a real process."""

    def __init__(self, stderr: bytes) -> None:
        super().__init__("go1.27.2", build_ok=False)
        self.stderr = stderr

    def __call__(
        self, cmd: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if len(cmd) > 1 and cmd[1] == "build":
            self.builds += 1
            script = (
                f"import sys; sys.stderr.buffer.write({self.stderr!r}); sys.exit(1)"
            )
            return _REAL_RUN([sys.executable, "-c", script], **kwargs)
        return super().__call__(cmd, **kwargs)


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
    fake = _GatedFakeGo("go1.27.2", build_ok=False)

    results, warnings = _race_two_workers(monkeypatch, fake)

    assert fake.builds == 1
    assert results == {"first": None, "second": None}
    assert len(warnings) == 2
    assert sum("go.mod requires go" in w for w in warnings) == 1
    assert sum("go1.27.2" in w for w in warnings) == 1


def test_parallel_syncs_retry_an_expired_failure_once(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, clock: _Clock
) -> None:
    # Past the TTL the queued sync still defers to the retry the first one
    # just made and failed, so an expired marker costs one build, not one each.
    _run(monkeypatch, go_repo, _FakeGo("go1.27.2", build_ok=False))
    clock.now += cs.GO_FRONTEND_BUILD_FAILURE_TTL_S + 1
    fake = _GatedFakeGo("go1.27.2", build_ok=False)

    results, _warnings = _race_two_workers(monkeypatch, fake)

    assert fake.builds == 1
    assert results == {"first": None, "second": None}


def test_a_queued_sync_reuses_the_helper_just_built(
    monkeypatch: pytest.MonkeyPatch, cgr_home: Path
) -> None:
    fake = _GatedFakeGo("go1.27.2", build_ok=True)

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
    # The helper links the stdlib of whichever Go builds it and runs that Go's
    # cmd/go, so a floor below these fixes lets it build and load packages
    # with known-vulnerable code. This test is the only check: since
    # osv-scanner 2.4.0 the CI scan skips the stdlib version in go.mod.
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


@pytest.mark.parametrize("found", ["1.23.12", "1.26.9", "1.27.1"])
def test_an_old_go_is_named_once_and_never_built(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, found: str
) -> None:
    fake = _FakeGo(f"go{found}", build_ok=True)

    facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 0
    assert not facts.call_sites
    assert len(warnings) == 1
    assert found in warnings[0]
    assert "1.27.2" in warnings[0]


def test_a_failed_build_is_logged_once(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    _facts, warnings = _run(monkeypatch, go_repo, _FakeGo("go1.27.2", build_ok=False))

    assert len(warnings) == 1
    assert "go.mod requires go" in warnings[0]


def test_a_failed_build_is_not_retried_on_the_next_sync(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    fake = _FakeGo("go1.27.2", build_ok=False)
    _run(monkeypatch, go_repo, fake)

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 1
    assert len(warnings) == 1


def test_a_timed_out_build_is_retried_on_the_next_sync(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    # A wedged toolchain or a slow module fetch says nothing about whether the
    # helper builds, so it is not remembered (PR #2417 review).
    fake = _FakeGo("go1.27.2", build_ok=True, time_out_first=True)
    _run(monkeypatch, go_repo, fake)

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 2
    assert warnings == []


def test_a_failed_build_that_prints_bytes_that_are_not_utf8_is_a_failed_build(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    # The strict decode of the build's output raised out of the build step
    # and ended the index (#3792). The output is only ever shown, so it is
    # shown with the byte escaped, and the build is remembered as failed.
    fake = _PrintingFakeGo(b"go: cannot write /srv/caf\xe9/out: read-only\n")

    facts, warnings = _run(monkeypatch, go_repo, fake)
    _again, later = _run(monkeypatch, go_repo, fake)

    assert not facts.call_sites
    assert len(warnings) == 1
    assert "/srv/caf\\xe9/out: read-only" in warnings[0]
    assert fake.builds == 1
    assert len(later) == 1
    assert "go1.27.2" in later[0]


def test_a_cgr_home_that_cannot_be_written_falls_back_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    # A file where the cache folder must go: the helper's cache cannot be
    # made, and the OSError ended the index (#3792).
    cgr_home.write_text("", encoding=cs.ENCODING_UTF8)
    fake = _FakeGo("go1.27.2", build_ok=True)

    facts, warnings = _run(monkeypatch, go_repo, fake)

    assert not facts.call_sites
    assert fake.builds == 0
    assert fake.runs == 0
    assert len(warnings) == 1
    assert cgr_home.name in warnings[0]


def test_a_go_that_cannot_start_falls_back_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, tmp_path: Path
) -> None:
    # A `go` on PATH that is no program, as a broken shim is: the build's
    # OSError ended the index (#3792). It says nothing about whether the
    # helper builds and costs nothing to retry, so it is not remembered.
    go = tmp_path / "bin" / "go"
    go.parent.mkdir()
    go.write_bytes(b"\x00\x01 not a program\n")
    go.chmod(go.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(fe.shutil, "which", lambda _name: str(go))
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        first = fe.run_go_frontend(go_repo)
        second = fe.run_go_frontend(go_repo)
    finally:
        logger.remove(sink)

    assert not first.call_sites
    assert not second.call_sites
    # Windows names no file in the error it gives for a program it cannot
    # start, so only the warning's own text is checked.
    failed = ls.GO_FRONTEND_BUILD_FAILED.partition("{")[0]
    assert len(warnings) == 2
    assert all(w.startswith(failed) for w in warnings)
    assert not list(cgr_home.rglob(fe._BUILD_FAILED_MARKER))


def test_a_failure_older_than_the_ttl_is_rebuilt(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, clock: _Clock
) -> None:
    # A failure the environment caused (no network, a module proxy outage)
    # clears without Go or the helper changing, and the marker sits in a cache
    # every repository shares, so it must not keep go/types facts off for long
    # after (PR #2417 review).
    fake = _FakeGo("go1.27.2", build_ok=False)
    _run(monkeypatch, go_repo, fake)
    fake.build_ok = True
    clock.now += cs.GO_FRONTEND_BUILD_FAILURE_TTL_S + 1

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 2
    assert warnings == []


def test_a_failure_within_the_ttl_skips_the_build_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, clock: _Clock
) -> None:
    fake = _FakeGo("go1.27.2", build_ok=False)
    _run(monkeypatch, go_repo, fake)
    clock.now += cs.GO_FRONTEND_BUILD_FAILURE_TTL_S - 1

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 1
    assert len(warnings) == 1
    assert "go1.27.2" in warnings[0]


def test_the_failure_time_comes_from_the_marker_not_its_mtime(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path, clock: _Clock
) -> None:
    # Copying or restoring the cache can give the marker a fresh mtime, but the
    # failure is still as old as the marker records.
    fake = _FakeGo("go1.27.2", build_ok=False)
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
    fake = _FakeGo("go1.27.2", build_ok=False)
    _run(monkeypatch, go_repo, fake)
    fake.build_ok = True
    clock.now -= 24 * cs.GO_FRONTEND_BUILD_FAILURE_TTL_S

    _run(monkeypatch, go_repo, fake)

    assert fake.builds == 2


def test_a_new_toolchain_retries_a_remembered_failure(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    _run(monkeypatch, go_repo, _FakeGo("go1.27.2", build_ok=False))
    upgraded = _FakeGo("go1.27.3", build_ok=True)

    _run(monkeypatch, go_repo, upgraded)

    assert upgraded.builds == 1


def test_changed_helper_sources_retry_a_remembered_failure(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    fake = _FakeGo("go1.27.2", build_ok=False)
    _run(monkeypatch, go_repo, fake)
    monkeypatch.setattr(fe, "_newest_source_mtime", lambda: 4102444800.0)

    _run(monkeypatch, go_repo, fake)

    assert fake.builds == 2


@pytest.mark.parametrize(
    ("name", "failed_under", "now"),
    [
        ("GOPROXY", "off", "https://proxy.golang.org"),
        ("GOSUMDB", "sum.example.com", None),
        ("GOMODCACHE", None, "/warm/cache"),
        ("GOFLAGS", "-mod=vendor", "-mod=vendor -modcacherw"),
    ],
)
def test_a_changed_go_setting_retries_a_remembered_failure(
    monkeypatch: pytest.MonkeyPatch,
    go_repo: Path,
    cgr_home: Path,
    name: str,
    failed_under: str | None,
    now: str | None,
) -> None:
    # A failure the environment caused, such as no module proxy with an
    # empty module cache, clears when that setting does, while Go and the
    # helper stay as they were: holding it an hour longer kept go/types
    # facts off for no reason (issue #3806).
    _set_env(monkeypatch, name, failed_under)
    fake = _FakeGo("go1.27.2", build_ok=False)
    _run(monkeypatch, go_repo, fake)
    _set_env(monkeypatch, name, now)
    fake.build_ok = True

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 2
    assert warnings == []


@pytest.mark.parametrize(
    ("name", "failed_under", "now"),
    [
        ("GOFLAGS", "-modfile=alt.mod", "-modfile=other.mod"),
        ("GOFLAGS", "-mod=vendor", "-mod=mod"),
        ("GOFLAGS", None, "-tags=x"),
        ("GOWORK", "/elsewhere/go.work", None),
        ("GOOS", "linux", "windows"),
        ("CGR_UNRELATED", None, "1"),
    ],
)
def test_a_change_the_build_never_sees_keeps_a_remembered_failure(
    monkeypatch: pytest.MonkeyPatch,
    go_repo: Path,
    cgr_home: Path,
    name: str,
    failed_under: str | None,
    now: str | None,
) -> None:
    # The build is handed no GOFLAGS flag but -modcacherw, GOWORK=off, the
    # host's GOOS and GOARCH, and reads no variable outside Go's own, so a
    # change to those alone repeats the same build.
    _set_env(monkeypatch, name, failed_under)
    fake = _FakeGo("go1.27.2", build_ok=False)
    _run(monkeypatch, go_repo, fake)
    _set_env(monkeypatch, name, now)

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 1
    assert len(warnings) == 1


def test_the_marker_holds_no_go_setting_in_the_clear(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    # The marker sits in a cache every repository shares, and a GOPROXY can
    # carry a token.
    secret = "https://user:s3cr3t-token@proxy.example.com"
    monkeypatch.setenv("GOPROXY", secret)
    _run(monkeypatch, go_repo, _FakeGo("go1.27.2", build_ok=False))

    [marker] = cgr_home.rglob(fe._BUILD_FAILED_MARKER)
    assert "s3cr3t-token" not in marker.read_text(encoding=cs.ENCODING_UTF8)


def _set_env(monkeypatch: pytest.MonkeyPatch, name: str, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


def test_a_new_enough_go_builds_the_helper(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    fake = _FakeGo("go1.27.2", build_ok=True)

    _facts, warnings = _run(monkeypatch, go_repo, fake)

    assert fake.builds == 1
    assert warnings == []


def test_a_helper_built_under_a_lower_floor_is_not_reused(
    monkeypatch: pytest.MonkeyPatch, cgr_home: Path
) -> None:
    # A helper an older Go built while the floor was lower stays timestamp
    # fresh, so the cache must not hand it back once the floor is raised: the
    # old Go is named and nothing runs (PR #2417 review).
    current_floor = fe._required_go_version
    old_go = _FakeGo("go1.22.6", build_ok=True)
    monkeypatch.setattr(fe.subprocess, "run", old_go)
    monkeypatch.setattr(fe, "_required_go_version", lambda: (1, 22))
    assert fe._build_tool("/usr/bin/go") is not None
    monkeypatch.setattr(fe, "_required_go_version", current_floor)
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        binary = fe._build_tool("/usr/bin/go")
    finally:
        logger.remove(sink)

    assert binary is None
    assert old_go.builds == 1
    assert len(warnings) == 1
    assert "1.27.2" in warnings[0]


def test_a_helper_built_under_the_current_floor_is_reused(
    monkeypatch: pytest.MonkeyPatch, cgr_home: Path
) -> None:
    fake = _FakeGo("go1.27.2", build_ok=True)
    monkeypatch.setattr(fe.subprocess, "run", fake)

    first = fe._build_tool("/usr/bin/go")
    second = fe._build_tool("/usr/bin/go")

    assert first is not None
    assert second == first
    assert fake.builds == 1


def test_a_cached_helper_is_not_handed_to_an_older_go(
    monkeypatch: pytest.MonkeyPatch, go_repo: Path, cgr_home: Path
) -> None:
    # The helper loads packages with the `go` on PATH, and a version manager
    # can put one below the floor there after a newer Go built the helper; the
    # fresh cached binary must not let it run (PR #3410 review).
    _run(monkeypatch, go_repo, _FakeGo("go1.27.2", build_ok=True))
    older = _FakeGo("go1.27.1", build_ok=True)

    facts, warnings = _run(monkeypatch, go_repo, older)

    assert older.builds == 0
    assert older.runs == 0
    assert not facts.call_sites
    assert len(warnings) == 1
    assert "1.27.1" in warnings[0]
    assert "1.27.2" in warnings[0]


def test_an_unreadable_floor_gets_a_cache_of_its_own(
    monkeypatch: pytest.MonkeyPatch, cgr_home: Path
) -> None:
    monkeypatch.setattr(fe, "_required_go_version", lambda: None)
    unknown = fe._cache_dir()
    monkeypatch.setattr(fe, "_required_go_version", lambda: SUPPORTED_FLOOR)

    assert unknown != fe._cache_dir()
    assert unknown.parent == fe._cache_dir().parent


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
