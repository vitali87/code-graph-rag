"""Issue #2637: the tracing guide says how pytest gets the plugin first.

The plugin is a `pytest11` entry point, so pytest loads it only when
`code-graph-rag` is installed in the project's own test environment. The
README installs cgr in isolation (`uv tool install`, `pipx install`), so the
guide's first step, `pytest --cgr-trace`, failed with "unrecognized
arguments" for anyone who followed it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from codebase_rag.trace import pytest_plugin

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs" / "guide" / "dynamic-tracing.md"
OVERLAY = "uv run --with code-graph-rag pytest --cgr-trace"


def _recording_section() -> str:
    text = GUIDE.read_text(encoding="utf-8")
    start = text.index("## Recording a trace")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_the_guide_gives_the_overlay_before_any_bare_run() -> None:
    section = _recording_section()

    assert OVERLAY in section
    bare = [
        i
        for i, line in enumerate(section.splitlines())
        if line.strip() == "pytest --cgr-trace"
    ]
    overlay = section.splitlines().index(OVERLAY)
    assert all(overlay < i for i in bare)


def test_the_guide_names_the_error_an_isolated_install_gives() -> None:
    section = _recording_section()

    assert "unrecognized arguments: --cgr-trace" in section
    assert "uv tool install" in section
    assert "pipx" in section


# Negative: what the guide relies on must still hold.


def test_the_plugin_is_still_a_pytest11_entry_point() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

    entry_points = project["project"]["entry-points"]["pytest11"]
    assert entry_points == {"cgr_trace": pytest_plugin.__name__}


def test_the_plugin_still_adds_the_option(pytestconfig: pytest.Config) -> None:
    # This suite runs with the package installed, so the option is known.
    assert pytestconfig.getoption("--cgr-trace") is False
