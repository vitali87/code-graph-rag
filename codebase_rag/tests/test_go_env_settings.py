"""Go's settings file as cgr reads it (PR #3804 review).

`go env -w` keeps a setting in Go's settings file, and a `go` command takes
any setting its environment leaves unset or empty from there. cgr reads the
file as cmd/go/internal/cfg does, without running `go`: where it is, how a
line reads, and which value wins. Where Go is installed, each case is also
checked against what `go env` itself prints.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.go_frontend import go_env

_SETTING = "GOFLAGS"

# Every variable that decides where the file is, cleared for each case.
_LOCATION_VARIABLES = ["GOENV", "HOME", "XDG_CONFIG_HOME", "APPDATA"]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in [_SETTING, *_LOCATION_VARIABLES]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    return path


def _go_prints(folder: Path) -> str:
    """What `go env GOFLAGS`, run in `folder` with this environment, prints."""
    go = shutil.which("go")
    if go is None:
        pytest.skip("go toolchain not available")
    proc = subprocess.run(
        [go, "env", _SETTING],
        cwd=folder,
        capture_output=True,
        check=True,
        env={**os.environ, "GOTOOLCHAIN": "local"},
    )
    return os.fsdecode(proc.stdout).removesuffix("\n")


# What the file holds, and the GOFLAGS Go reads from it: a line names the
# setting by the bytes before its first `=` and gives it every byte after;
# one that does not start with a capital, or has no `=`, is skipped; a later
# line wins.
_FILE_LINES = [
    ("GOFLAGS=-mod=mod\n", "-mod=mod"),
    ("GOFLAGS=-tags=a  '-modfile=alt b.mod' ", "-tags=a  '-modfile=alt b.mod' "),
    ("GOFLAGS=-mod=mod\nGOFLAGS=-modfile=alt.mod\n", "-modfile=alt.mod"),
    ("GOFLAGS=-mod=mod\nGOFLAGS=\n", ""),
    ("# GOFLAGS=-mod=mod\n goflags=x\n GOFLAGS=y\nGOFLAGS\n\n", ""),
    ("GOPROXY=off\nGOFLAGS=-mod=vendor\r\nGOSUMDB=off", "-mod=vendor\r"),
    ("GOFLAGS==x=y", "=x=y"),
]


@pytest.mark.parametrize(("text", "goflags"), _FILE_LINES)
def test_a_line_of_the_file_reads_as_go_reads_it(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, text: str, goflags: str
) -> None:
    clean_env.setenv("GOENV", str(_write(tmp_path / "env", text)))

    assert go_env.go_setting(_SETTING, str(tmp_path)) == goflags


@pytest.mark.parametrize(("text", "goflags"), _FILE_LINES)
def test_go_reads_each_line_so_too(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, text: str, goflags: str
) -> None:
    clean_env.setenv("GOENV", str(_write(tmp_path / "env", text)))

    assert _go_prints(tmp_path) == goflags


# GOFLAGS in the environment, or None for unset, and what Go takes: an empty
# one falls back to the file, which holds -mod=mod.
_ENVIRONMENT = [(None, "-mod=mod"), ("", "-mod=mod"), ("-tags=x", "-tags=x")]


@pytest.mark.parametrize(("environment", "goflags"), _ENVIRONMENT)
def test_the_environment_wins_unless_it_leaves_the_setting_empty(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
    environment: str | None,
    goflags: str,
) -> None:
    clean_env.setenv("GOENV", str(_write(tmp_path / "env", "GOFLAGS=-mod=mod\n")))
    if environment is not None:
        clean_env.setenv(_SETTING, environment)

    assert go_env.go_setting(_SETTING, str(tmp_path)) == goflags
    assert _go_prints(tmp_path) == goflags


# Where the file is on Linux and the other Unix systems, given the variables
# below (`{root}` is the test's folder) and the file's path from `{root}`, or
# None where Go reads none: GOENV first, `off` for none and a relative one
# from the folder Go runs in, `run`; then $XDG_CONFIG_HOME when it is an
# absolute path, and $HOME/.config only when it is unset or empty.
_UNIX_LOCATIONS = [
    ({"GOENV": "{root}/named"}, "named"),
    ({"GOENV": "named", "HOME": "{root}/home"}, "run/named"),
    ({"GOENV": "off", "HOME": "{root}/home"}, None),
    ({"GOENV": "", "HOME": "{root}/home"}, "home/.config/go/env"),
    ({"XDG_CONFIG_HOME": "{root}/xdg", "HOME": "{root}/home"}, "xdg/go/env"),
    ({"XDG_CONFIG_HOME": "xdg", "HOME": "{root}/home"}, None),
    ({"XDG_CONFIG_HOME": "", "HOME": "{root}/home"}, "home/.config/go/env"),
    ({"HOME": "{root}/home/link/.."}, "home/.config/go/env"),
    ({}, None),
]


def _place_every_candidate(root: Path) -> None:
    """A settings file at every place a case can name, each holding GOFLAGS
    set to its own path from `root`. `home/link` leads to `other/inner`,
    so `home/link/..` is `home` as Go cleans a path and `other` as the
    system resolves it."""
    for rel in [
        "named",
        "run/named",
        "home/.config/go/env",
        "xdg/go/env",
        "other/.config/go/env",
    ]:
        _write(root / rel, f"GOFLAGS={rel}\n")
    (root / "run").mkdir(exist_ok=True)
    (root / "other" / "inner").mkdir()
    (root / "home" / "link").symlink_to(root / "other" / "inner")


@pytest.mark.skipif(sys.platform == "win32", reason="the Unix rules")
@pytest.mark.parametrize(("variables", "found"), _UNIX_LOCATIONS)
def test_the_file_is_where_go_looks_for_it(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
    variables: dict[str, str],
    found: str | None,
) -> None:
    _place_every_candidate(tmp_path)
    for name, value in variables.items():
        clean_env.setenv(name, value.format(root=tmp_path))
    run = tmp_path / "run"
    with pytest.MonkeyPatch.context() as on_linux:
        on_linux.setattr(go_env.sys, "platform", "linux")
        read = go_env.go_setting(_SETTING, str(run))

    assert read == (found or "")
    if sys.platform not in cs.GO_ENV_APPLE_PLATFORMS:
        assert _go_prints(run) == (found or "")


# The same on macOS and iOS, which look in $HOME/Library/Application Support
# and never in $XDG_CONFIG_HOME, and on Windows, which looks in %AppData%.
_OTHER_LOCATIONS = [
    ("darwin", {"HOME": "{root}/home"}, "home/Library/Application Support/go/env"),
    (
        "ios",
        {"HOME": "{root}/home", "XDG_CONFIG_HOME": "{root}/xdg"},
        "home/Library/Application Support/go/env",
    ),
    ("darwin", {"XDG_CONFIG_HOME": "{root}/xdg"}, None),
    ("win32", {"APPDATA": "{root}/appdata", "HOME": "{root}/home"}, "appdata/go/env"),
    ("win32", {"HOME": "{root}/home", "XDG_CONFIG_HOME": "{root}/xdg"}, None),
]


@pytest.mark.parametrize(("platform", "variables", "found"), _OTHER_LOCATIONS)
def test_the_file_is_where_go_looks_for_it_elsewhere(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
    platform: str,
    variables: dict[str, str],
    found: str | None,
) -> None:
    for rel in [
        "home/Library/Application Support/go/env",
        "appdata/go/env",
        "home/.config/go/env",
        "xdg/go/env",
    ]:
        _write(tmp_path / rel, f"GOFLAGS={rel}\n")
    for name, value in variables.items():
        clean_env.setenv(name, value.format(root=tmp_path))
    clean_env.setattr(go_env.sys, "platform", platform)

    assert go_env.go_setting(_SETTING, str(tmp_path)) == (found or "")


@pytest.mark.skipif(sys.platform == "win32", reason="named pipes are POSIX")
def test_a_pipe_for_a_settings_file_is_never_opened(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    # Opening a pipe blocks until something writes to it.
    os.mkfifo(tmp_path / "env")
    clean_env.setenv("GOENV", str(tmp_path / "env"))

    assert go_env.go_setting(_SETTING, str(tmp_path)) == ""
