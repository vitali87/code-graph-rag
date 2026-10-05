"""Issue #2407: `cgr daemon up` reports a failure once, briefly, and only when
the stack is actually unusable.

With anything on 127.0.0.1:3000 (Memgraph Lab's port) the command printed the
whole `docker compose up` progress dump twice (a loguru ERROR record and a
red echo), with the real cause as its last line, and exited 1 although
Memgraph and Qdrant had started: `cgr daemon status` said "running" straight
after. Lab is an optional UI.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from loguru import logger

from codebase_rag.stack import cli as stack_cli
from codebase_rag.stack import constants as stack_cs
from codebase_rag.stack.manager import StackError, StackManager

PROGRESS = """ Network cgr_default Creating
 Network cgr_default Created
 Container cgr-memgraph-1 Creating
 Container cgr-lab-1 Creating
 Container cgr-qdrant-1 Creating
 Container cgr-lab-1 Created
 Container cgr-memgraph-1 Created
 Container cgr-qdrant-1 Created
 Container cgr-lab-1 Starting
 Container cgr-qdrant-1 Starting
 Container cgr-memgraph-1 Starting
 Container cgr-qdrant-1 Started
 Container cgr-memgraph-1 Started
"""


def _bind_failure(service: str, port: int, project: str = "cgr") -> str:
    return PROGRESS.replace("cgr", project) + (
        "Error response from daemon: failed to set up container networking: "
        "driver failed programming external connectivity on endpoint "
        f"{project}-{service}-1 (fcf7977130513683d013447a8b854c4ead8edb6997b7): "
        f"failed to bind host port 127.0.0.1:{port}/tcp: address already in use\n"
    )


PULL_FAILURE = (
    " memgraph Pulling\n a1b2c3 Pulling fs layer\n"
    + " a1b2c3 Extracting 1B\n" * 20
    + "Error response from daemon: toomanyrequests: You have reached your pull "
    "rate limit.\n"
)


def _run(up_output: str, running: set[str]) -> object:
    def fake(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        args = list(cmd)
        if "up" in args:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=up_output)
        if "config" in args:
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"services": {}}', stderr=""
            )
        if "ps" in args:
            service = args[-1]
            ids = "c0ffee" if service in running else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=ids, stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    return fake


@pytest.fixture
def mgr(tmp_path: Path) -> StackManager:
    src = tmp_path / "compose.yaml"
    src.write_text("services: {}\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return StackManager(home=home, package_compose=src)


def _up(mgr: StackManager, output: str, running: set[str]) -> list[str]:
    records: list[str] = []
    sink = logger.add(records.append, level="DEBUG", format="{level}|{message}")
    try:
        with (
            patch.object(mgr, "check_docker"),
            patch("codebase_rag.stack.manager.subprocess.run", _run(output, running)),
        ):
            mgr.up()
    finally:
        logger.remove(sink)
    return records


def test_a_lab_that_cannot_start_does_not_fail_a_usable_stack(
    mgr: StackManager,
) -> None:
    records = _up(
        mgr,
        _bind_failure(stack_cs.SERVICE_LAB, 3000),
        {stack_cs.SERVICE_MEMGRAPH, stack_cs.SERVICE_QDRANT},
    )

    warnings = [r for r in records if r.startswith("WARNING|")]
    assert any("127.0.0.1:3000" in w and "LAB_PORT" in w for w in warnings), records


@pytest.mark.parametrize("project", ["demo", "my.stack"])
def test_a_lab_failure_under_another_project_name_is_still_optional(
    tmp_path: Path, project: str
) -> None:
    # Review of PR 2493: Compose names containers after the manager's project
    # (`demo-lab-1`), and a pattern fixed to `cgr` missed them, so a Lab
    # failure failed a usable stack again.
    src = tmp_path / "compose.yaml"
    src.write_text("services: {}\n", encoding="utf-8")
    mgr = StackManager(home=tmp_path, package_compose=src, project_name=project)

    records = _up(
        mgr,
        _bind_failure(stack_cs.SERVICE_LAB, 3000, project),
        {stack_cs.SERVICE_MEMGRAPH, stack_cs.SERVICE_QDRANT},
    )

    warnings = [r for r in records if r.startswith("WARNING|")]
    assert any("127.0.0.1:3000" in w and "LAB_PORT" in w for w in warnings), records


def test_another_projects_container_is_not_this_stacks_lab(tmp_path: Path) -> None:
    # Negative: under project `my.stack`, a name like `myxstack-lab-1` (the dot
    # read as a regex wildcard) or plain `cgr-lab-1` is not this stack's Lab.
    src = tmp_path / "compose.yaml"
    src.write_text("services: {}\n", encoding="utf-8")
    mgr = StackManager(home=tmp_path, package_compose=src, project_name="my.stack")

    for other in ("myxstack", "cgr"):
        output = _bind_failure(stack_cs.SERVICE_LAB, 3000, other)
        with pytest.raises(StackError):
            _up(mgr, output, {stack_cs.SERVICE_MEMGRAPH, stack_cs.SERVICE_QDRANT})


def test_a_lab_failure_with_memgraph_down_still_fails(mgr: StackManager) -> None:
    # Negative: Lab is optional, Memgraph is not.
    output = _bind_failure(stack_cs.SERVICE_LAB, 3000)
    with pytest.raises(StackError) as exc:
        _up(mgr, output, {stack_cs.SERVICE_QDRANT})

    assert "127.0.0.1:3000" in str(exc.value)


def test_the_error_names_the_cause_not_the_progress(mgr: StackManager) -> None:
    output = _bind_failure(stack_cs.SERVICE_MEMGRAPH, 7687)
    with pytest.raises(StackError) as exc:
        _up(mgr, output, set())

    message = str(exc.value)
    assert "127.0.0.1:7687" in message
    assert "MEMGRAPH_PORT" in message
    assert "Creating" not in message
    assert "Started" not in message


def test_a_pull_failure_keeps_its_error_line_and_drops_the_progress(
    mgr: StackManager,
) -> None:
    with pytest.raises(StackError) as exc:
        _up(mgr, PULL_FAILURE, set())

    message = str(exc.value)
    assert "toomanyrequests" in message
    assert "Extracting" not in message


def test_the_raw_compose_output_is_kept_at_debug(mgr: StackManager) -> None:
    records: list[str] = []
    sink = logger.add(records.append, level="DEBUG", format="{level}|{message}")
    try:
        with pytest.raises(StackError):
            _up(mgr, PULL_FAILURE, set())
    finally:
        logger.remove(sink)

    assert any(r.startswith("DEBUG|") and "Extracting 1B" in r for r in records)


def test_an_unrecognised_failure_is_still_reported(mgr: StackManager) -> None:
    # Negative: output with no recognisable error line is not swallowed.
    with pytest.raises(StackError) as exc:
        _up(mgr, "something odd happened\n", set())

    assert "something odd happened" in str(exc.value)


def test_up_prints_its_failure_once() -> None:
    records: list[str] = []
    sink = logger.add(records.append, level="DEBUG", format="{message}")
    try:
        with patch.object(
            StackManager, "ensure_running", side_effect=StackError("port clash")
        ):
            result = CliRunner().invoke(stack_cli.cli, ["up"])
    finally:
        logger.remove(sink)

    assert result.exit_code == 1
    shown = result.output.count("port clash") + sum(
        r.count("port clash") for r in records
    )
    assert shown == 1
