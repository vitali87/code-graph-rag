# (H) Issue #808: the HTTP MCP endpoint mounts the StreamableHTTP session
# (H) manager with no auth middleware, so the DEFAULT bind must be loopback --
# (H) a 0.0.0.0 default exposed the unauthenticated transport to the network
# (H) and made CVE-2026-52869 (session requests served without verifying the
# (H) authenticated principal) reachable from outside the host. Reaching a
# (H) non-local bind must be an explicit operator decision (MCP_HTTP_HOST).
from __future__ import annotations

from importlib.metadata import version

from codebase_rag.config import AppConfig


def test_mcp_http_default_bind_is_loopback() -> None:
    config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
    assert config.MCP_HTTP_HOST == "127.0.0.1"


def test_mcp_dependency_is_patched() -> None:
    # (H) 1.27.2 fixed CVE-2026-52869/52870; 1.28.1 fixed CVE-2026-59950
    major, minor, patch = (int(p) for p in version("mcp").split(".")[:3])
    assert (major, minor, patch) >= (1, 28, 1), version("mcp")
