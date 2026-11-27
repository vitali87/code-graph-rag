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


@pytest.fixture
def go_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No GOFLAGS or GOWORK from the host, and no module fetched: a load
    that reads go.mod in alt.mod's place fails at once."""
    monkeypatch.delenv(cs.GO_ENV_GOFLAGS, raising=False)
    monkeypatch.delenv(cs.GO_ENV_GOWORK, raising=False)
    monkeypatch.setenv("GOPROXY", "off")
    return monkeypatch


@pytest.mark.usefixtures("one_go_helper_per_session")
@pytest.mark.parametrize("goflags", _MODFILE_GOFLAGS)
def test_a_module_whose_modfile_is_named_gets_the_type_checkers_graph(
    tmp_path: Path, go_env: pytest.MonkeyPatch, goflags: str
) -> None:
    _needs_go_type_checker()
    go_env.setattr(settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    repo = _module_with_alt_mod(tmp_path)
    go_env.setenv(cs.GO_ENV_GOFLAGS, goflags.format(repo=repo))

    with_modfile = _graph(repo)

    # alt.mod renamed to go.mod, and the go.mod it replaces kept as alt.mod,
    # so both runs list the same files.
    go_env.delenv(cs.GO_ENV_GOFLAGS)
    (repo / "go.mod").rename(repo / "stub.mod")
    (repo / "alt.mod").rename(repo / "go.mod")
    (repo / "stub.mod").rename(repo / "alt.mod")
    renamed = _graph(repo)
    go_env.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    assert with_modfile == renamed != _graph(repo)


@pytest.mark.parametrize("goflags", _MODFILE_GOFLAGS)
def test_a_cold_helper_cache_builds_the_helper_under_a_modfile(
    tmp_path: Path, go_env: pytest.MonkeyPatch, goflags: str
) -> None:
    # The build runs in the helper's own folder, where a relative -modfile
    # names no file, and an absolute one names the indexed module's.
    _needs_go_type_checker()
    repo = _module_with_alt_mod(tmp_path)
    cold = tmp_path / "cold-cgr-home"
    go_env.setattr(settings, "CGR_HOME", cold)
    go_env.setenv(cs.GO_ENV_GOFLAGS, goflags.format(repo=repo))

    binary = fe._build_tool(str(shutil.which("go")))

    assert binary is not None
    assert binary.is_relative_to(cold)
    assert not list(cold.rglob(fe._BUILD_FAILED_MARKER))


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


@pytest.mark.parametrize(("goflags", "handed", "modfile"), _HANDED)
def test_the_helper_is_handed_the_modfile_apart_from_its_goflags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    goflags: str,
    handed: str,
    modfile: str,
) -> None:
    # What the helper is handed, so a host without Go checks it too.
    monkeypatch.delenv(cs.GO_ENV_GOWORK, raising=False)
    monkeypatch.setenv(cs.GO_ENV_GOFLAGS, goflags)
    _write(tmp_path / "go.mod", "module example.com/app\n\ngo 1.21\n")
    envs: list[dict[str, str]] = []

    def _run(
        cmd: list[str], env: dict[str, str], **_kwargs: bool | int | str
    ) -> subprocess.CompletedProcess[str]:
        envs.append(env)
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(fe.shutil, "which", lambda _name: "go")
    monkeypatch.setattr(fe, "_build_tool", lambda _go: Path("gotypes"))
    monkeypatch.setattr(fe.subprocess, "run", _run)

    run_go_frontend(tmp_path)

    assert [(env[cs.GO_ENV_GOFLAGS], env[fe._MODFILE_ENV]) for env in envs] == [
        (handed, modfile)
    ]


@pytest.mark.parametrize(
    ("goflags", "handed"), [(goflags, handed) for goflags, handed, _ in _HANDED]
)
def test_the_helper_is_built_without_the_modfile(
    monkeypatch: pytest.MonkeyPatch, goflags: str, handed: str
) -> None:
    # The -modfile names the indexed module's file, never the helper's.
    monkeypatch.setenv(cs.GO_ENV_GOFLAGS, goflags)

    assert fe._host_build_env()[cs.GO_ENV_GOFLAGS] == handed


def test_an_unset_goflags_stays_unset_for_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A blank here would hide the GOFLAGS Go keeps in its settings file.
    monkeypatch.delenv(cs.GO_ENV_GOFLAGS, raising=False)

    assert cs.GO_ENV_GOFLAGS not in fe._host_build_env()
