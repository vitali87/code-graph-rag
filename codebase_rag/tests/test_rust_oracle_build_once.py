# The syn Rust oracle must build its binary ONCE (serialized across parallel
# pytest-xdist workers by a file lock) and then exec that binary directly.
# The old code shelled out to `cargo run` on every call, which re-links
# target/release/rs_oracle each time; concurrent workers raced the link step
# (one exec'd the binary while another rewrote it -> ETXTBSY, cargo exit 101),
# an intermittent CI flake on the macos-py3.12 runner. This pins the fix.
from __future__ import annotations

import types
from pathlib import Path

import pytest

from evals import constants as ec
from evals.oracles import rust_oracle as ro


def test_oracle_builds_once_then_execs_binary_directly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / ec.RS_ORACLE_BIN
    # Bypass `cargo metadata` resolution and the mtime guard so the test pins
    # only the build-once/exec-directly contract, deterministically.
    monkeypatch.setattr(ro, "_binary_path", lambda: binary)
    monkeypatch.setattr(ro, "_BUILD_LOCK", tmp_path / ec.RS_ORACLE_BUILD_LOCK)
    monkeypatch.setattr(ro, "_sources_newer_than", lambda _binary: False)

    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> types.SimpleNamespace:
        commands.append(cmd)
        if cmd[:2] == [ec.CARGO_BIN, ec.CARGO_BUILD]:
            binary.write_text("built", encoding="utf-8")  # simulate built binary
            return types.SimpleNamespace(stdout="", returncode=0)
        assert cmd[0] == str(binary), f"must exec the binary directly, got {cmd}"
        return types.SimpleNamespace(stdout="{}", returncode=0)

    monkeypatch.setattr(ro.subprocess, "run", fake_run)

    ro._run_rust_oracle_payload(tmp_path / "proj_a")
    ro._run_rust_oracle_payload(tmp_path / "proj_b")

    build_cmds = [c for c in commands if c[:2] == [ec.CARGO_BIN, ec.CARGO_BUILD]]
    run_cmds = [c for c in commands if len(c) > 1 and c[1] == ec.CARGO_RUN]
    exec_cmds = [c for c in commands if c[0] == str(binary)]

    assert len(build_cmds) == 1, f"binary must be built exactly once: {commands}"
    assert not run_cmds, f"must never use `cargo run` (the racy path): {commands}"
    assert len(exec_cmds) == 2, f"each payload call execs the binary: {commands}"


@pytest.mark.parametrize(
    ("os_name", "expected_suffix"),
    [("nt", ec.EXE_SUFFIX_WINDOWS), ("posix", "")],
)
def test_binary_path_uses_platform_exe_suffix(
    os_name: str, expected_suffix: str
) -> None:
    # Cargo emits rs_oracle.exe on Windows; direct exec must target the real
    # filename or Windows workers build then fail to exec a nonexistent path.
    assert ro._exe_suffix(os_name) == expected_suffix
