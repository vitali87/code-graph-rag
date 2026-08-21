"""The Java oracle compiles Oracle.java into a shared in-repo directory, so
parallel pytest-xdist workers can race: the mtime guard is a check-then-act and
javac writes the class in place, letting one worker launch the JVM against a
class file another is still writing (issue #1366). The Rust and C# oracles
already lock for exactly this reason; these tests pin that Java does too."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from evals.oracles import java_oracle


@pytest.fixture
def staged_oracle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Point the module at a scratch oracle dir so no test ever compiles into
    # the real one (which is what makes the production race reachable).
    source = tmp_path / "Oracle.java"
    source.write_text("class Oracle {}\n", encoding="utf-8")
    monkeypatch.setattr(java_oracle, "_ORACLE_DIR", tmp_path)
    monkeypatch.setattr(java_oracle, "_SOURCE", source)
    monkeypatch.setattr(java_oracle, "_CLASS", tmp_path / "Oracle.class")
    monkeypatch.setattr(java_oracle.shutil, "which", lambda _name: "/usr/bin/javac")
    return tmp_path


def _slow_compiler(calls: list[float], dwell: float):
    # Stands in for javac: takes measurable time and only then publishes the
    # class, which is the window a second worker would otherwise compile into.
    def fake_run(command, **kwargs):
        calls.append(time.monotonic())
        time.sleep(dwell)
        Path(command[1]).with_suffix(".class").write_bytes(b"compiled")
        return subprocess.CompletedProcess(command, 0, "", "")

    return fake_run


def test_concurrent_callers_compile_the_oracle_once(
    staged_oracle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without a lock every worker sees the class missing and runs javac, so the
    # last writer can be mid-write while another worker execs the JVM.
    calls: list[float] = []
    monkeypatch.setattr(java_oracle.subprocess, "run", _slow_compiler(calls, dwell=0.3))
    threads = [threading.Thread(target=java_oracle._ensure_compiled) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(calls) == 1
    assert (staged_oracle / "Oracle.class").read_bytes() == b"compiled"


def test_a_fresh_class_is_not_recompiled(
    staged_oracle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The freshness short-circuit must survive the locking change: a class
    # newer than its source is left alone.
    (staged_oracle / "Oracle.class").write_bytes(b"already-built")
    calls: list[float] = []
    monkeypatch.setattr(java_oracle.subprocess, "run", _slow_compiler(calls, dwell=0.0))
    java_oracle._ensure_compiled()
    assert calls == []


def test_a_stale_class_is_rebuilt(
    staged_oracle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A class OLDER than the source must still be recompiled, or an edited
    # Oracle.java would be silently shadowed by a stale artifact.
    class_file = staged_oracle / "Oracle.class"
    class_file.write_bytes(b"stale")
    source = staged_oracle / "Oracle.java"
    future = time.time() + 60
    import os

    os.utime(source, (future, future))
    calls: list[float] = []
    monkeypatch.setattr(java_oracle.subprocess, "run", _slow_compiler(calls, dwell=0.0))
    java_oracle._ensure_compiled()
    assert len(calls) == 1
    assert class_file.read_bytes() == b"compiled"
