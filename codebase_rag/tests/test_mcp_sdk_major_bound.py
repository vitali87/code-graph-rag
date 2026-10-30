"""The declared ``mcp`` range must stay inside the SDK major the server is written for.

Issue #2519: ``pyproject.toml`` declared ``mcp>=1.28.1`` with no ceiling. Every
documented install (``uv tool install``, ``pipx``, ``pip``) ignores ``uv.lock``
and resolved mcp 2.2.0, whose ``mcp.server.Server`` has no ``list_tools()``
decorator any more, so ``cgr mcp-server`` died on start while every lock-synced
CI job stayed green. The declared range, not the lock, is what users get. The
server now registers its handlers through the 2.x ``Server`` constructor, so
the range must keep out 1.x as well as the next major.
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
            "3.0.0",
            # Pre-releases too: `pip install --pre` and resolvers that fall back
            # to them must not reach the next major's API either.
            "3.0.0a1",
            "3.0.0rc1",
        ],
    )
    def test_a_fresh_install_cannot_resolve_the_next_major(self, version: str) -> None:
        declared = _declared_mcp_range()

        assert not declared.contains(version, prereleases=True), (
            f"pyproject.toml declares mcp{declared}, which admits {version}: "
            "codebase_rag/mcp/server.py is written for the 2.x Server API"
        )

    @pytest.mark.parametrize("version", ["1.30.0", "1.31.0", "1.99.0"])
    def test_a_fresh_install_cannot_resolve_the_1x_sdk(self, version: str) -> None:
        declared = _declared_mcp_range()

        assert not declared.contains(version, prereleases=True), (
            f"pyproject.toml declares mcp{declared}, which admits {version}: "
            "its Server takes no on_list_tools/on_call_tool handlers, so "
            "`cgr mcp-server` cannot start"
        )


class TestMcpSdkRangeAdmits2x:
    @pytest.mark.parametrize("version", ["2.3.0", "2.4.0"])
    def test_the_supported_2x_releases_stay_installable(self, version: str) -> None:
        assert _declared_mcp_range().contains(version)

    def test_the_locked_version_is_inside_the_declared_range(self) -> None:
        locked = _locked_mcp_version()

        assert _declared_mcp_range().contains(locked), (
            f"uv.lock pins mcp {locked}, outside the declared range; dev and CI "
            "would then run an SDK that no user can install"
        )


class TestMcpFloorExcludesUnboundedSessions:
    """Negative tests: no install may resolve an SDK that never frees HTTP sessions.

    Before 1.30.0 (CVE-2026-59951) the stateful StreamableHTTP session manager
    that ``serve_http`` mounts kept every session forever: no idle expiry, no
    cap, and a ``DELETE`` left the entry registered, so any client able to
    reach the endpoint could grow server memory without bound.
    """

    @pytest.mark.parametrize("version", ["1.28.0", "1.28.1", "1.29.0", "1.29.1"])
    def test_a_fresh_install_cannot_resolve_a_vulnerable_release(
        self, version: str
    ) -> None:
        declared = _declared_mcp_range()

        assert not declared.contains(version), (
            f"pyproject.toml declares mcp{declared}, which admits {version}: "
            "its StreamableHTTP session manager never reclaims sessions "
            "(CVE-2026-59951, fixed in 1.30.0)"
        )
