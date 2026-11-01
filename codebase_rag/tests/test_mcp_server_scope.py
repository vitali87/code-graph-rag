"""The MCP server serves one project, and only to the client that started it.

Two gaps against the MCP local-server security guide: a server launched
without TARGET_REPO_PATH served whatever directory its client started it in,
a home directory or `/` included, and the HTTP transport without a token let
any web page the user opened reach it through DNS rebinding.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.transport_security import (
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from starlette.requests import Request

from codebase_rag import exceptions as ex
from codebase_rag.mcp import server as srv

PORT = 8765


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "users" / "me"
    (fake_home / "project").mkdir(parents=True)
    monkeypatch.setattr(srv.Path, "home", lambda: fake_home)
    monkeypatch.delenv("TARGET_REPO_PATH", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(srv.settings, "TARGET_REPO_PATH", ".")
    return fake_home


class TestProjectRoot:
    @pytest.mark.parametrize("start", ["home", "above home", "disk root"])
    def test_an_unnamed_broad_root_is_refused(
        self, start: str, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = {
            "home": home,
            "above home": home.parent,
            "disk root": Path(Path.cwd().anchor),
        }[start]
        monkeypatch.chdir(directory)

        with pytest.raises(ex.RepoPathError, match="TARGET_REPO_PATH"):
            srv.get_project_root()

    def test_a_project_under_home_is_served(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(home / "project")

        assert srv.get_project_root() == (home / "project").resolve()

    def test_a_dot_names_no_folder(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(home)
        monkeypatch.setenv("TARGET_REPO_PATH", ".")

        with pytest.raises(ex.RepoPathError):
            srv.get_project_root()

    def test_the_hosts_project_is_served_instead(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host that names its project in CLAUDE_PROJECT_ROOT has chosen
        one, even when it launched the server in the home directory."""
        monkeypatch.chdir(home)
        monkeypatch.setenv("CLAUDE_PROJECT_ROOT", str(home / "project"))

        assert srv.get_project_root() == (home / "project").resolve()

    def test_a_narrower_launch_directory_stays_the_root(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(home / "project")
        monkeypatch.setenv("CLAUDE_PROJECT_ROOT", str(tmp_path))

        assert srv.get_project_root() == (home / "project").resolve()

    def test_a_named_home_is_served(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Naming it is a choice, as `cgr start --yes` is for a sync."""
        monkeypatch.setenv("TARGET_REPO_PATH", str(home))

        assert srv.get_project_root() == home.resolve()


def _request(host: str, origin: str | None = None) -> Request:
    headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "query_string": b"",
            "headers": headers,
        }
    )


def _status(host: str, origin: str | None = None) -> int | None:
    hosts, origins = srv._loopback_only_headers(PORT)
    middleware = TransportSecurityMiddleware(
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )
    )
    response = asyncio.run(
        middleware.validate_request(_request(host, origin), is_post=True)
    )
    return None if response is None else response.status_code


class TestLoopbackHeaders:
    @pytest.mark.parametrize(
        ("host", "origin"),
        [
            (f"127.0.0.1:{PORT}", None),
            (f"localhost:{PORT}", None),
            (f"[::1]:{PORT}", None),
            (f"localhost:{PORT}", f"http://localhost:{PORT}"),
        ],
    )
    def test_a_local_client_is_let_in(self, host: str, origin: str | None) -> None:
        assert _status(host, origin) is None

    @pytest.mark.parametrize(
        ("host", "origin", "status"),
        [
            # A rebound page names its own domain.
            (f"attacker.example:{PORT}", "http://attacker.example", 421),
            # Another local server's page, on another port.
            (f"127.0.0.1:{PORT}", "http://localhost:3000", 403),
            (f"127.0.0.1:{PORT}", "http://attacker.example", 403),
        ],
    )
    def test_a_web_page_is_kept_out(self, host: str, origin: str, status: int) -> None:
        assert _status(host, origin) == status

    def test_port_80_is_named_without_its_port(self) -> None:
        hosts, origins = srv._loopback_only_headers(80)

        assert "localhost" in hosts
        assert "http://localhost" in origins


class TestServeHttp:
    @pytest.fixture
    def manager_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        import uvicorn
        from mcp.server import streamable_http_manager

        captured: dict[str, Any] = {}

        def record(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(
            streamable_http_manager, "StreamableHTTPSessionManager", record
        )
        monkeypatch.setattr(srv, "create_server", lambda _: (MagicMock(), MagicMock()))
        monkeypatch.setattr(
            srv, "_service_lifecycle", lambda _: contextlib.nullcontext()
        )
        monkeypatch.setattr(uvicorn.Server, "serve", AsyncMock())
        return captured

    def test_without_a_token_only_local_headers_pass(
        self, manager_kwargs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(srv.settings, "MCP_HTTP_AUTH_TOKEN", None)

        asyncio.run(srv.serve_http(host="127.0.0.1", port=PORT))

        security = manager_kwargs["security_settings"]
        assert security.enable_dns_rebinding_protection
        assert (security.allowed_hosts, security.allowed_origins) == (
            srv._loopback_only_headers(PORT)
        )

    def test_a_token_leaves_the_headers_to_the_proxy(
        self, manager_kwargs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(srv.settings, "MCP_HTTP_AUTH_TOKEN", "s3cret")

        asyncio.run(srv.serve_http(host="0.0.0.0", port=PORT))  # noqa: S104

        assert manager_kwargs["security_settings"] is None
