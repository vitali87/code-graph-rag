from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag.stack import constants as stack_cs
from codebase_rag.stack.manager import StackError, StackManager


def _fake_subprocess_result(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _make_compose_source(tmp_path: Path) -> Path:
    src = tmp_path / "src_compose.yaml"
    src.write_text("services: {}\n", encoding="utf-8")
    return src


@pytest.fixture
def stack_home(tmp_path: Path) -> Path:
    home = tmp_path / "cgr-home"
    home.mkdir()
    return home


def test_ensure_compose_file_copies_when_missing(
    stack_home: Path, tmp_path: Path
) -> None:
    src = _make_compose_source(tmp_path)
    mgr = StackManager(home=stack_home, package_compose=src)
    target = mgr.ensure_compose_file()
    assert target == stack_home / stack_cs.COMPOSE_FILENAME
    assert target.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_ensure_compose_file_preserves_existing(
    stack_home: Path, tmp_path: Path
) -> None:
    src = _make_compose_source(tmp_path)
    target = stack_home / stack_cs.COMPOSE_FILENAME
    target.write_text("custom: yes\n", encoding="utf-8")
    mgr = StackManager(home=stack_home, package_compose=src)
    result = mgr.ensure_compose_file()
    assert result.read_text(encoding="utf-8") == "custom: yes\n"


def test_ensure_compose_file_raises_when_source_missing(
    stack_home: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "nope.yaml"
    mgr = StackManager(home=stack_home, package_compose=missing)
    with pytest.raises(StackError):
        mgr.ensure_compose_file()


def test_check_docker_raises_when_docker_not_on_path(stack_home: Path) -> None:
    mgr = StackManager(home=stack_home, package_compose=Path("/dev/null"))
    with patch("codebase_rag.stack.manager.shutil.which", return_value=None):
        with pytest.raises(StackError) as exc:
            mgr.check_docker()
    assert "docker not found" in str(exc.value).lower()


def test_check_docker_raises_when_daemon_down(stack_home: Path) -> None:
    mgr = StackManager(home=stack_home, package_compose=Path("/dev/null"))
    with (
        patch(
            "codebase_rag.stack.manager.shutil.which", return_value="/usr/bin/docker"
        ),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            return_value=_fake_subprocess_result(returncode=1, stderr="daemon down"),
        ),
    ):
        with pytest.raises(StackError) as exc:
            mgr.check_docker()
    assert "daemon" in str(exc.value).lower()


def test_check_docker_raises_when_compose_missing(stack_home: Path) -> None:
    mgr = StackManager(home=stack_home, package_compose=Path("/dev/null"))

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["docker", "info"]:
            return _fake_subprocess_result(returncode=0)
        return _fake_subprocess_result(returncode=1)

    with (
        patch(
            "codebase_rag.stack.manager.shutil.which", return_value="/usr/bin/docker"
        ),
        patch("codebase_rag.stack.manager.subprocess.run", side_effect=fake_run),
    ):
        with pytest.raises(StackError) as exc:
            mgr.check_docker()
    assert "compose" in str(exc.value).lower()


def test_status_returns_stopped_when_nothing_reachable(stack_home: Path) -> None:
    mgr = StackManager(home=stack_home, package_compose=Path("/dev/null"))
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=False),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=False),
    ):
        status = mgr.status()
    assert status.state == stack_cs.StackState.STOPPED


def test_status_returns_running_when_both_reachable(stack_home: Path) -> None:
    mgr = StackManager(home=stack_home, package_compose=Path("/dev/null"))
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
    ):
        status = mgr.status()
    assert status.state == stack_cs.StackState.RUNNING
    assert status.memgraph_reachable
    assert status.qdrant_reachable


def test_status_returns_partial_when_only_memgraph_reachable(stack_home: Path) -> None:
    mgr = StackManager(home=stack_home, package_compose=Path("/dev/null"))
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=False),
    ):
        status = mgr.status()
    assert status.state == stack_cs.StackState.PARTIAL


def test_compose_cmd_uses_project_and_file(stack_home: Path, tmp_path: Path) -> None:
    src = _make_compose_source(tmp_path)
    mgr = StackManager(home=stack_home, package_compose=src, project_name="cgr-test")
    cmd = mgr._compose_cmd("up", "-d")
    assert cmd[0] == "docker"
    assert cmd[1] == "compose"
    assert "-p" in cmd and "cgr-test" in cmd
    assert "-f" in cmd
    assert str(mgr.compose_file) in cmd
    assert cmd[-2:] == ["up", "-d"]


def test_ensure_running_skips_docker_when_already_up(
    stack_home: Path, tmp_path: Path
) -> None:
    src = _make_compose_source(tmp_path)
    mgr = StackManager(home=stack_home, package_compose=src)
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
        patch.object(mgr, "up") as mock_up,
        patch.object(mgr, "wait_healthy") as mock_wait,
    ):
        status = mgr.ensure_running()
    assert status.state == stack_cs.StackState.RUNNING
    mock_up.assert_not_called()
    mock_wait.assert_not_called()


def test_ensure_running_starts_when_stopped(stack_home: Path, tmp_path: Path) -> None:
    src = _make_compose_source(tmp_path)
    mgr = StackManager(home=stack_home, package_compose=src)
    reachable_state = {"memgraph": False, "qdrant": False}

    def memgraph_check(*_: object, **__: object) -> bool:
        return reachable_state["memgraph"]

    def qdrant_check(*_: object, **__: object) -> bool:
        return reachable_state["qdrant"]

    def fake_up(timeout: float = 0.0) -> None:
        reachable_state["memgraph"] = True
        reachable_state["qdrant"] = True

    with (
        patch(
            "codebase_rag.stack.manager.wait_for_memgraph", side_effect=memgraph_check
        ),
        patch("codebase_rag.stack.manager.wait_for_qdrant", side_effect=qdrant_check),
        patch.object(mgr, "up", side_effect=fake_up) as mock_up,
        patch.object(mgr, "wait_healthy") as mock_wait,
    ):
        status = mgr.ensure_running()
    assert status.state == stack_cs.StackState.RUNNING
    mock_up.assert_called_once()
    mock_wait.assert_called_once()


def test_up_propagates_failure(stack_home: Path, tmp_path: Path) -> None:
    src = _make_compose_source(tmp_path)
    mgr = StackManager(home=stack_home, package_compose=src)
    with (
        patch.object(mgr, "check_docker"),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            return_value=_fake_subprocess_result(returncode=1, stderr="boom"),
        ),
    ):
        with pytest.raises(StackError) as exc:
            mgr.up()
    assert "boom" in str(exc.value) or "Failed" in str(exc.value)
