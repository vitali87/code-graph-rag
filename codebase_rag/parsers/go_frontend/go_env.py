"""Go's own settings, read as a `go` command reads them, without running one.

`go env -w` keeps a setting in Go's settings file, and a `go` command takes
any setting its environment leaves unset or empty from there, so a GOFLAGS
kept that way reaches the helper's loads though cgr's environment lacks it
(PR #3804 review). This follows cmd/go/internal/cfg: `EnvFile` for where
the file is, `readEnvFile` for how a line reads, and `Getenv` for which
value wins. Go's last source, the go.env file of its GOROOT, is not read:
where GOROOT is takes the `go` binary's own answer, and the go.env Go ships
sets only GOPROXY, GOSUMDB and GOTOOLCHAIN.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from ... import constants as cs
from ...utils.path_utils import is_regular_file

_LINE_END = b"\n"
_KEY_END = b"="
# Go skips a line whose first byte is not an ASCII capital: a comment, a
# blank line, or a name no setting has.
_FIRST_KEY_BYTES = (b"A", b"Z")


def go_setting(name: str, folder: str, env: Mapping[str, str] = os.environ) -> str:
    """The value a `go` command run in `folder` with `env` takes for the
    setting `name`: the environment's when it is set and not empty, else
    the settings file's, else empty."""
    if value := env.get(name, ""):
        return value
    path = _settings_file(folder, env)
    return "" if path is None else _file_settings(path).get(name, "")


def _settings_file(folder: str, env: Mapping[str, str]) -> str | None:
    """The settings file of a `go` command run in `folder` with `env`, or
    None when it has none. Go opens a relative GOENV from the folder it
    runs in."""
    goenv = env.get(cs.GO_ENV_GOENV, "")
    if goenv == cs.GO_ENV_FILE_OFF:
        return None
    if goenv:
        return os.path.join(folder, goenv)
    config = _user_config_dir(env)
    if config is None:
        return None
    return os.path.normpath(os.path.join(config, *cs.GO_ENV_FILE_PARTS))


def _user_config_dir(env: Mapping[str, str]) -> str | None:
    """The user's config folder as Go's os.UserConfigDir reads it from
    `env`, or None where Go finds none."""
    if sys.platform == cs.PLATFORM_WINDOWS:
        return env.get(cs.GO_ENV_WINDOWS_CONFIG) or None
    home = env.get(cs.GO_ENV_HOME, "")
    if sys.platform in cs.GO_ENV_APPLE_PLATFORMS:
        return os.path.join(home, *cs.GO_ENV_APPLE_CONFIG_PARTS) if home else None
    if config := env.get(cs.GO_ENV_XDG_CONFIG_HOME, ""):
        return config if os.path.isabs(config) else None
    return os.path.join(home, cs.GO_ENV_UNIX_CONFIG) if home else None


def _file_settings(path: str) -> dict[str, str]:
    """The settings the file at `path` holds, as Go reads it: one per line,
    named by the bytes before the first `=` and valued by every byte after
    it, a later line of a name winning. A file that cannot be read holds
    none, and a pipe is never opened."""
    try:
        data = Path(path).read_bytes() if is_regular_file(path) else b""
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in data.split(_LINE_END):
        name, given, value = line.partition(_KEY_END)
        if given and _FIRST_KEY_BYTES[0] <= line[:1] <= _FIRST_KEY_BYTES[1]:
            found[os.fsdecode(name)] = os.fsdecode(value)
    return found
