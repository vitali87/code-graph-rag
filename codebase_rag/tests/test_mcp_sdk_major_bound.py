"""The declared ``mcp`` range must stop at the SDK major the server is written for.

Issue #2519: ``pyproject.toml`` declared ``mcp>=1.28.1`` with no ceiling. Every
documented install (``uv tool install``, ``pipx``, ``pip``) ignores ``uv.lock``
and resolved mcp 2.2.0, whose ``mcp.server.Server`` has no ``list_tools()``
decorator any more, so ``cgr mcp-server`` died on start while every lock-synced
CI job stayed green. The declared range, not the lock, is what users get.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCKFILE = REPO_ROOT / "uv.lock"
MCP = "mcp"


def _declared_mcp_range() -> SpecifierSet:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    for raw in project["dependencies"]:
        requirement = Requirement(raw)
        if requirement.name == MCP:
            return requirement.specifier
    raise AssertionError(f"{MCP} is not a runtime dependency in {PYPROJECT}")


def _locked_mcp_version() -> str:
    lock = tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))
    return next(p["version"] for p in lock["package"] if p["name"] == MCP)


class TestMcpSdkMajorBound:
    @pytest.mark.parametrize(
        "version",
        [
            # 2.2.0 is what a fresh install resolved when the issue was filed.
            "2.2.0",
            "2.0.0",
            # Pre-releases too: `pip install --pre` and resolvers that fall back
            # to them must not reach the 2.x API either.
            "2.0.0a1",
            "2.0.0rc1",
            "3.0.0",
        ],
    )
    def test_a_fresh_install_cannot_resolve_the_2x_sdk(self, version: str) -> None:
        declared = _declared_mcp_range()

        assert not declared.contains(version, prereleases=True), (
            f"pyproject.toml declares mcp{declared}, which admits {version}: "
            "codebase_rag/mcp/server.py registers its handlers with the 1.x "
            "decorator API that 2.x removed, so `cgr mcp-server` cannot start"
        )


class TestMcpSdkRangeStillAdmits1x:
    """Negative tests: the ceiling must not narrow anything below 2.0."""

    @pytest.mark.parametrize("version", ["1.28.1", "1.29.0", "1.29.1", "1.30.0"])
    def test_the_supported_1x_releases_stay_installable(self, version: str) -> None:
        # 1.x is still maintained upstream (1.30.0 shipped after 2.0.0), so a
        # fresh install keeps receiving its fixes.
        assert _declared_mcp_range().contains(version)

    def test_the_floor_is_unchanged(self) -> None:
        assert not _declared_mcp_range().contains("1.28.0")

    def test_the_locked_version_is_inside_the_declared_range(self) -> None:
        locked = _locked_mcp_version()

        assert _declared_mcp_range().contains(locked), (
            f"uv.lock pins mcp {locked}, outside the declared range; dev and CI "
            "would then run an SDK that no user can install"
        )
