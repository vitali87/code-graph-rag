"""Issue #2684: the tracer agents ship with the package and can be found.

The dynamic-tracing guide builds the C/C++ shim, the Lua agent, the Dart
collector and the JVM agent from files under `codebase_rag/trace/`, but
`package-data` listed none of them, so an installed cgr had nothing to
compile, load or run, and the guide's paths only existed in a source
checkout. `cgr trace agent <language>` now prints where each one is.
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.tests.test_packaged_data import _BUILD_WHEEL, _copy_project
from codebase_rag.trace.cli import cli as trace_cli

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "guide" / "dynamic-tracing.md"

AGENT_FILES = [
    "codebase_rag/trace/c_agent/cgr_trace_shim.c",
    "codebase_rag/trace/lua_agent/cgr_trace.lua",
    "codebase_rag/trace/dart_collector/pubspec.yaml",
    "codebase_rag/trace/dart_collector/pubspec.lock",
    "codebase_rag/trace/dart_collector/bin/cgr_trace_collect.dart",
    "codebase_rag/trace/jvm_agent/MANIFEST.MF",
    "codebase_rag/trace/jvm_agent/src/cgr/trace/CgrTraceAgent.java",
    "codebase_rag/trace/jvm_agent/src/cgr/trace/Json.java",
    "codebase_rag/trace/jvm_agent/src/cgr/trace/MethodEntryTransformer.java",
    "codebase_rag/trace/jvm_agent/src/cgr/trace/TraceRecorder.java",
]

AGENTS = {
    "c": "codebase_rag/trace/c_agent/cgr_trace_shim.c",
    "lua": "codebase_rag/trace/lua_agent/cgr_trace.lua",
    "dart": "codebase_rag/trace/dart_collector",
    "jvm": "codebase_rag/trace/jvm_agent",
}

_PROBE = (
    "import sys\n"
    "from click.testing import CliRunner\n"
    "from codebase_rag.trace.cli import cli\n"
    "result = CliRunner().invoke(cli, ['agent', sys.argv[1]])\n"
    "print(result.output.strip())\n"
)


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("setuptools")
    project = tmp_path_factory.mktemp("project")
    dist = tmp_path_factory.mktemp("dist")
    _copy_project(project)
    subprocess.run(
        [sys.executable, "-c", _BUILD_WHEEL, str(dist)],
        cwd=project,
        check=True,
        capture_output=True,
        timeout=300,
    )
    (wheel,) = dist.glob("*.whl")
    return wheel


@pytest.fixture(scope="module")
def site(built_wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("site")
    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(target)
    return target


@pytest.mark.parametrize("relative", AGENT_FILES)
def test_every_agent_file_ships_in_the_wheel(built_wheel: Path, relative: str) -> None:
    with zipfile.ZipFile(built_wheel) as archive:
        assert relative in archive.namelist()


@pytest.mark.parametrize(("agent", "relative"), sorted(AGENTS.items()))
def test_an_installed_cgr_points_at_its_own_agent(
    site: Path, tmp_path: Path, agent: str, relative: str
) -> None:
    # PYTHONPATH replaces the checkout, so only the unpacked wheel answers.
    probe = subprocess.run(
        [sys.executable, "-c", _PROBE, agent],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(site)},
        check=True,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        timeout=300,
    )
    printed = Path(probe.stdout.strip().splitlines()[-1])

    assert printed == site / relative
    assert printed.exists()


@pytest.mark.parametrize(("agent", "relative"), sorted(AGENTS.items()))
def test_trace_agent_prints_the_path(agent: str, relative: str) -> None:
    result = CliRunner().invoke(trace_cli, ["agent", agent])

    assert result.exit_code == 0, result.output
    assert Path(result.output.strip()) == ROOT / relative


def test_the_guide_locates_every_agent_through_the_command() -> None:
    # A demo's alt text describes its recording (the JVM one was made with
    # `make jvm-agent`); only the guide's prose and commands are checked.
    text = "\n".join(
        line
        for line in GUIDE.read_text(encoding="utf-8").splitlines()
        if not line.startswith("![")
    )

    for agent in AGENTS:
        assert f"cgr trace agent {agent}" in text
    assert "make jvm-agent" not in text
    assert " codebase_rag/trace/c_agent/cgr_trace_shim.c" not in text
    assert "cd codebase_rag/trace/dart_collector" not in text


# Negative: what must not change.


def test_an_unknown_agent_is_refused() -> None:
    result = CliRunner().invoke(trace_cli, ["agent", "cobol"])

    assert result.exit_code == 2
    assert "cobol" in result.output


def test_the_dart_collector_gitignore_does_not_ship(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as archive:
        assert not [n for n in archive.namelist() if n.endswith(".gitignore")]
