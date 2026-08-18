# The Java availability probe must run the toolchain, not just find it: macOS
# ships /usr/bin/javac shims that exist with no JDK and exit non-zero, and a
# wedged binary must never stall pytest collection (PR #1308).

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from evals.oracles import java_available
from evals.oracles.java_oracle import _toolchain_runs


def test_probe_reports_unavailable_when_binary_is_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert _toolchain_runs("javac") is False


def test_probe_reports_unavailable_for_a_broken_shim(monkeypatch):
    # The macOS no-JDK shim: present on PATH, exits non-zero when invoked.
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/javac")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    assert _toolchain_runs("javac") is False


def test_probe_reports_unavailable_when_the_binary_hangs(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/javac")

    def hang(*_a, **kwargs):
        assert kwargs.get("timeout") is not None
        raise subprocess.TimeoutExpired(cmd="javac", timeout=kwargs["timeout"])

    monkeypatch.setattr("subprocess.run", hang)
    assert _toolchain_runs("javac") is False


def test_available_requires_both_toolchain_binaries(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **k: SimpleNamespace(
            returncode=0 if cmd[0].endswith("javac") else 1, stdout="", stderr=""
        ),
    )
    assert java_available() is False
