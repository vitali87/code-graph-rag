"""A -modfile in GOFLAGS and the go/types helper (issues #3805, #3806).

-modfile names the module file Go reads in place of the indexed module's
go.mod. go/packages asks Go its version with GO111MODULE=off before it loads
anything, and Go refuses -modfile outside module mode, so a helper that
inherited the flag failed every run (#3805). The helper's own build, run in
its source folder, took the flag as meant for the helper's module (#3806).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.parsers.go_frontend import frontend as fe
from codebase_rag.parsers.go_frontend import run_go_frontend
from codebase_rag.tests.test_symlink_policy import (
    _graph_lines,
    _needs_go_type_checker,
    _stateful_updater,
    _write,
)
from evals.cgr_graph import _StatefulIngestor

# A -modfile by its path from the module, which Go reads from the folder the
# loads run in, and by an absolute path.
_MODFILE_GOFLAGS = ["-modfile=alt.mod", "-modfile={repo}/alt.mod"]

_REQUIRE_LIB = "module example.com/app\n\ngo 1.21\n\nrequire example.com/lib v0.0.0\n"

# Only the type checker proves that Loud implements Speaker through the Base
# it embeds, and that `lib.F()` leaves the module, where the name heuristics
# bind it to the local F. Both need `example.com/lib`, which only alt.mod's
# `replace` finds, so a load that reads go.mod instead checks nothing here.
_MAIN = """package main

import "example.com/lib"

type Speaker interface{ Speak() string }

type Base struct{}

func (Base) Speak() string { return "base" }

type Loud struct{ Base }

func F() {}

func main() {
\tlib.F()
\t_ = Loud{}.Speak()
}
"""


def _module_with_alt_mod(root: Path) -> Path:
    """A module whose module file is alt.mod: its go.mod, which Go needs to
    find the module's folder, requires `example.com/lib` but cannot find it."""
    repo = root / "repo"
    _write(root / "lib" / "go.mod", "module example.com/lib\n\ngo 1.21\n")
    _write(root / "lib" / "lib.go", "package lib\n\nfunc F() {}\n")
    _write(repo / "main.go", _MAIN)
    _write(repo / "alt.mod", f"{_REQUIRE_LIB}\nreplace example.com/lib => ../lib\n")
    _write(repo / "go.mod", _REQUIRE_LIB)
    return repo


def _graph(repo: Path) -> list[str]:
    store = _StatefulIngestor()
    _stateful_updater(repo, store).run(force=True)
    return _graph_lines(store)


def _keep_goflags(root: Path, monkeypatch: pytest.MonkeyPatch, goflags: str) -> None:
    """GOFLAGS kept in Go's settings file, named by GOENV, as `go env -w`
    keeps it, and left unset in the environment (PR #3807 review)."""
    monkeypatch.delenv(cs.GO_ENV_GOFLAGS, raising=False)
    settings_file = _write(root / "go-settings" / "env", f"GOFLAGS={goflags}\n")
    monkeypatch.setenv(cs.GO_ENV_GOENV, str(settings_file))


@pytest.fixture
def go_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No GOFLAGS or GOWORK from the host, and no module fetched: a load
    that reads go.mod in alt.mod's place fails at once."""
    monkeypatch.delenv(cs.GO_ENV_GOFLAGS, raising=False)
    monkeypatch.delenv(cs.GO_ENV_GOWORK, raising=False)
    monkeypatch.setenv("GOPROXY", "off")
    return monkeypatch


@pytest.mark.usefixtures("one_go_helper_per_session")
@pytest.mark.parametrize("kept", [False, True], ids=["environment", "settings-file"])
@pytest.mark.parametrize("goflags", _MODFILE_GOFLAGS)
def test_a_module_whose_modfile_is_named_gets_the_type_checkers_graph(
    tmp_path: Path, go_env: pytest.MonkeyPatch, goflags: str, kept: bool
) -> None:
    # GOFLAGS set in the environment, or kept in Go's settings file by `go
    # env -w`, which Go reads when the environment leaves it unset.
    _needs_go_type_checker()
    go_env.setattr(settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    repo = _module_with_alt_mod(tmp_path)
    if kept:
        _keep_goflags(tmp_path, go_env, goflags.format(repo=repo))
    else:
        go_env.setenv(cs.GO_ENV_GOFLAGS, goflags.format(repo=repo))

    with_modfile = _graph(repo)

    # alt.mod renamed to go.mod, and the go.mod it replaces kept as alt.mod,
    # so both runs list the same files.
    go_env.delenv(cs.GO_ENV_GOFLAGS, raising=False)
    go_env.setenv(cs.GO_ENV_GOENV, cs.GO_ENV_FILE_OFF)
    (repo / "go.mod").rename(repo / "stub.mod")
    (repo / "alt.mod").rename(repo / "go.mod")
    (repo / "stub.mod").rename(repo / "alt.mod")
    renamed = _graph(repo)
    go_env.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    assert with_modfile == renamed != _graph(repo)


# Settings meant for the indexed module, which the helper's build must not
# read: a -modfile by its path from the module or an absolute one, vendoring,
# and the user's go.work, named by GOWORK or found above the helper's cache.
# Each with the GOFLAGS the build gets: -modcacherw alone is kept, so the
# module cache the build fills stays deletable.
_USER_MODULE_SETTINGS = [
    pytest.param({cs.GO_ENV_GOFLAGS: "-modfile=alt.mod"}, " ", id="relative-modfile"),
    pytest.param(
        {cs.GO_ENV_GOFLAGS: "-modfile={repo}/alt.mod"}, " ", id="absolute-modfile"
    ),
    pytest.param({cs.GO_ENV_GOFLAGS: "-mod=vendor"}, " ", id="vendor"),
    pytest.param(
        {cs.GO_ENV_GOFLAGS: "-mod=vendor -modcacherw"},
        "-modcacherw",
        id="vendor-modcacherw",
    ),
    pytest.param({cs.GO_ENV_GOWORK: "{root}/ws/go.work"}, " ", id="named-go-work"),
    pytest.param({}, " ", id="go-work-above-the-cache"),
]


@pytest.mark.parametrize(("user_settings", "build_goflags"), _USER_MODULE_SETTINGS)
def test_a_cold_helper_cache_builds_the_helper_under_user_module_settings(
    tmp_path: Path,
    go_env: pytest.MonkeyPatch,
    user_settings: dict[str, str],
    build_goflags: str,
) -> None:
    # The build runs in the helper's own folder, where a relative -modfile
    # names no file, an absolute one names the indexed module's, there is no
    # vendor folder, and no go.work of the user's uses the helper's module.
    _needs_go_type_checker()
    repo = _module_with_alt_mod(tmp_path)
    _write(tmp_path / "ws" / "go.work", "go 1.21\n\nuse ../repo\n")
    if not user_settings:
        _write(tmp_path / "go.work", "go 1.21\n\nuse ./repo\n")
    cold = tmp_path / "cold-cgr-home"
    go_env.setattr(settings, "CGR_HOME", cold)
    for name, value in user_settings.items():
        go_env.setenv(name, value.format(repo=repo, root=tmp_path))
    real_run = subprocess.run
    builds: list[dict[str, str]] = []

    def _run(
        cmd: list[str], env: dict[str, str], **kwargs: bool | int | str | Path
    ) -> subprocess.CompletedProcess[str]:
        if cmd[1:2] == ["build"]:
            builds.append(env)
        return real_run(cmd, env=env, **kwargs)

    go_env.setattr(fe.subprocess, "run", _run)

    binary = fe._build_tool(str(shutil.which("go")))

    assert binary is not None
    assert binary.is_relative_to(cold)
    assert not list(cold.rglob(fe._BUILD_FAILED_MARKER))
    assert [
        (env.get(cs.GO_ENV_GOFLAGS), env.get(cs.GO_ENV_GOWORK)) for env in builds
    ] == [(build_goflags, cs.GO_WORK_OFF)]


@pytest.mark.usefixtures("one_go_helper_per_session")
@pytest.mark.parametrize(
    "goflags", ["-tags=cgrtag -modfile=alt.mod", "-modfile=alt.mod '-tags=cgrtag'"]
)
def test_the_loads_keep_the_other_goflags_beside_a_modfile(
    tmp_path: Path, go_env: pytest.MonkeyPatch, goflags: str
) -> None:
    _needs_go_type_checker()
    repo = _module_with_alt_mod(tmp_path)
    _write(repo / "tagged.go", "//go:build cgrtag\n\npackage main\n\nfunc T() {}\n")
    go_env.setenv(cs.GO_ENV_GOFLAGS, goflags)

    facts = run_go_frontend(repo)

    assert facts.degraded_modules == ()
    assert {"main.go", "tagged.go"} <= facts.checked_files


# GOFLAGS, then the GOFLAGS and the module file the helper is handed. Every
# other field is kept as written. A GOFLAGS left empty stays a blank, which
# Go splits into no flags, as an empty one would make Go read GOFLAGS from
# its own settings file instead. The last -modfile wins, and an empty one is
# go.mod.
_HANDED = [
    ("-modfile=alt.mod", " ", "alt.mod"),
    ("-mod=mod --modfile=alt.mod", "-mod=mod", "alt.mod"),
    ("-tags=x '-modfile=alt b.mod' -mod=mod", "-tags=x -mod=mod", "alt b.mod"),
    ('-tags=x\t"-modfile=alt b.mod"', "-tags=x", "alt b.mod"),
    ("-modfile=other.mod -modfile=/abs/alt.mod", " ", "/abs/alt.mod"),
    ("-modfile=alt.mod -modfile=", " ", ""),
    ("-mod=mod", "-mod=mod", ""),
]


def _handed(root: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """The environment of each helper run over a module at `root`. What
    the helper is handed, so a host without Go checks it too."""
    monkeypatch.delenv(cs.GO_ENV_GOWORK, raising=False)
    _write(root / "go.mod", "module example.com/app\n\ngo 1.21\n")
    envs: list[dict[str, str]] = []

    def _run(
        cmd: list[str], env: dict[str, str], **_kwargs: bool | int | str
    ) -> subprocess.CompletedProcess[str]:
        envs.append(env)
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(fe.shutil, "which", lambda _name: "go")
    monkeypatch.setattr(fe, "_build_tool", lambda _go: Path("gotypes"))
    monkeypatch.setattr(fe.subprocess, "run", _run)

    run_go_frontend(root)
    return envs


@pytest.mark.parametrize(("goflags", "handed", "modfile"), _HANDED)
def test_the_helper_is_handed_the_modfile_apart_from_its_goflags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    goflags: str,
    handed: str,
    modfile: str,
) -> None:
    monkeypatch.setenv(cs.GO_ENV_GOFLAGS, goflags)

    envs = _handed(tmp_path, monkeypatch)

    assert [(env[cs.GO_ENV_GOFLAGS], env[fe._MODFILE_ENV]) for env in envs] == [
        (handed, modfile)
    ]


# GOFLAGS kept in Go's settings file, and the GOFLAGS and the module file
# the helper is handed when the environment leaves GOFLAGS unset (None) or
# empty. A kept -modfile is cut out as one in the environment is, and the
# rest set, which keeps Go from reading the file's; a kept GOFLAGS without
# one is left as the environment had it, for Go to read itself.
_HANDED_FROM_FILE = [
    ("-tags=x -modfile=alt.mod", "-tags=x", "alt.mod"),
    ("'-modfile=alt b.mod'", " ", "alt b.mod"),
    ("-mod=mod", None, ""),
]


@pytest.mark.parametrize("environment", [None, ""])
@pytest.mark.parametrize(("kept", "handed", "modfile"), _HANDED_FROM_FILE)
def test_the_helper_is_handed_a_kept_modfile_apart_from_its_goflags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str | None,
    kept: str,
    handed: str | None,
    modfile: str,
) -> None:
    # Inherited, a kept -modfile reached go/packages' GO111MODULE=off query,
    # which Go refuses, and every run failed (PR #3807 review).
    _keep_goflags(tmp_path, monkeypatch, kept)
    if environment is not None:
        monkeypatch.setenv(cs.GO_ENV_GOFLAGS, environment)

    envs = _handed(tmp_path / "module", monkeypatch)

    assert [(env.get(cs.GO_ENV_GOFLAGS), env[fe._MODFILE_ENV]) for env in envs] == [
        (environment if handed is None else handed, modfile)
    ]


def test_the_helpers_run_keeps_the_users_go_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unlike the build, the run loads the user's module, in their workspace.
    workfile = str(tmp_path / "go.work")
    monkeypatch.setenv(cs.GO_ENV_GOWORK, workfile)
    _write(tmp_path / "go.mod", "module example.com/app\n\ngo 1.21\n")
    envs: list[dict[str, str]] = []

    def _run(
        cmd: list[str], env: dict[str, str], **_kwargs: bool | int | str
    ) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "gotypes":
            envs.append(env)
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(fe.shutil, "which", lambda _name: "go")
    monkeypatch.setattr(fe, "_build_tool", lambda _go: Path("gotypes"))
    monkeypatch.setattr(fe.subprocess, "run", _run)

    run_go_frontend(tmp_path)

    assert [env[cs.GO_ENV_GOWORK] for env in envs] == [workfile]


# GOFLAGS, and the GOFLAGS the helper is built with: -modcacherw alone, as
# written, or a blank, which also keeps Go from reading GOFLAGS from its own
# settings file. None is GOFLAGS unset.
_BUILD_GOFLAGS = [
    (None, " "),
    ("", " "),
    ("-modfile=alt.mod", " "),
    ("-mod=vendor -tags=x", " "),
    ("-mod=vendor -modcacherw", "-modcacherw"),
    ("--modcacherw '-modfile=alt b.mod' -mod=mod", "--modcacherw"),
    ("'-modcacherw=false'\t-modfile=alt.mod", "'-modcacherw=false'"),
]


@pytest.mark.parametrize(("goflags", "built_with"), _BUILD_GOFLAGS)
def test_the_helper_is_built_outside_the_users_go_work_and_goflags(
    monkeypatch: pytest.MonkeyPatch, goflags: str | None, built_with: str
) -> None:
    # Both name settings of the indexed module, never the helper's.
    monkeypatch.setenv(cs.GO_ENV_GOWORK, "/elsewhere/go.work")
    if goflags is None:
        monkeypatch.delenv(cs.GO_ENV_GOFLAGS, raising=False)
    else:
        monkeypatch.setenv(cs.GO_ENV_GOFLAGS, goflags)

    env = fe._host_build_env()

    assert (env[cs.GO_ENV_GOFLAGS], env[cs.GO_ENV_GOWORK]) == (
        built_with,
        cs.GO_WORK_OFF,
    )


@pytest.mark.parametrize(
    ("kept", "built_with"), [case for case in _BUILD_GOFLAGS if case[0]]
)
def test_the_helper_is_built_with_a_kept_modcacherw_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kept: str, built_with: str
) -> None:
    # `go env -w GOFLAGS=-modcacherw` is the usual way to keep it, and Go
    # reads it from the settings file when GOFLAGS is unset.
    _keep_goflags(tmp_path, monkeypatch, kept)

    assert fe._host_build_env()[cs.GO_ENV_GOFLAGS] == built_with
