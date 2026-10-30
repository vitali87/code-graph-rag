# Follow-up to #808/#809: the loopback default made the unauthenticated
# StreamableHTTP endpoint safe by default, but intentional remote exposure
# still had NO auth at all. serve_http now refuses a non-loopback bind
# without a configured token, and a configured token puts bearer-auth
# middleware (constant-time compare) in front of the mount.
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codebase_rag.mcp.server import _require_bearer_auth, _validate_http_exposure


async def _inner_app(scope: dict, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"inner"})


def _drive(app: Any, headers: list[tuple[bytes, bytes]]) -> tuple[int, bytes]:
    scope = {"type": "http", "method": "POST", "headers": headers, "path": "/mcp"}
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return status, body


def test_missing_bearer_is_rejected() -> None:
    app = _require_bearer_auth(_inner_app, "sekrit")
    status, _ = _drive(app, [])
    assert status == 401


def test_wrong_bearer_is_rejected() -> None:
    app = _require_bearer_auth(_inner_app, "sekrit")
    status, _ = _drive(app, [(b"authorization", b"Bearer wrong")])
    assert status == 401


def test_correct_bearer_passes_through() -> None:
    app = _require_bearer_auth(_inner_app, "sekrit")
    status, body = _drive(app, [(b"authorization", b"Bearer sekrit")])
    assert status == 200
    assert body == b"inner"


def test_scheme_is_case_insensitive() -> None:
    # RFC 7235: the auth SCHEME compares case-insensitively; the token
    # itself stays case-sensitive
    app = _require_bearer_auth(_inner_app, "sekrit")
    status, _ = _drive(app, [(b"authorization", b"bearer sekrit")])
    assert status == 200
    status, _ = _drive(app, [(b"authorization", b"BEARER sekrit")])
    assert status == 200
    status, _ = _drive(app, [(b"authorization", b"Bearer SEKRIT")])
    assert status == 401


def test_rejection_carries_www_authenticate() -> None:
    app = _require_bearer_auth(_inner_app, "sekrit")
    scope = {"type": "http", "method": "POST", "headers": [], "path": "/mcp"}
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert (b"www-authenticate", b"Bearer") in [
        (k.lower(), v) for k, v in start["headers"]
    ]


def _drive_websocket(app: Any, headers: list[tuple[bytes, bytes]]) -> list[dict]:
    scope = {"type": "websocket", "headers": headers, "path": "/mcp"}
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


async def _inner_websocket(scope: dict, receive: Any, send: Any) -> None:
    await send({"type": "websocket.accept"})


def test_a_websocket_without_the_token_is_refused() -> None:
    # Every non-HTTP scope used to pass the check untouched; only the SDK's
    # own refusal of websocket scopes stood behind it.
    app = _require_bearer_auth(_inner_websocket, "sekrit")
    assert _drive_websocket(app, []) == [{"type": "websocket.close", "code": 1008}]


def test_a_websocket_with_the_token_passes_through() -> None:
    app = _require_bearer_auth(_inner_websocket, "sekrit")
    sent = _drive_websocket(app, [(b"authorization", b"Bearer sekrit")])
    assert sent == [{"type": "websocket.accept"}]


def test_the_lifespan_protocol_needs_no_token() -> None:
    reached: list[str] = []

    async def inner(scope: dict, receive: Any, send: Any) -> None:
        reached.append(scope["type"])

    asyncio.run(_require_bearer_auth(inner, "sekrit")({"type": "lifespan"}, None, None))
    assert reached == ["lifespan"]


@pytest.mark.parametrize(
    ("raw", "expected"), [("   ", None), ("", None), ("sekrit\n", "sekrit")]
)
def test_the_token_setting_is_trimmed_and_blank_is_unset(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str | None
) -> None:
    # A blank token passed the exposure guard and became the bearer secret;
    # a secrets file's trailing newline locked every right client out.
    from codebase_rag.config import AppConfig

    monkeypatch.setenv("MCP_HTTP_AUTH_TOKEN", raw)
    token = AppConfig(_env_file=None).MCP_HTTP_AUTH_TOKEN
    assert token == expected
    if expected is None:
        with pytest.raises(ValueError):
            _validate_http_exposure("0.0.0.0", token)


def test_non_loopback_bind_without_token_refuses() -> None:
    with pytest.raises(ValueError):
        _validate_http_exposure("0.0.0.0", None)


def test_loopback_bind_without_token_is_fine() -> None:
    _validate_http_exposure("127.0.0.1", None)
    _validate_http_exposure("localhost", None)
    _validate_http_exposure("::1", None)


def test_non_loopback_bind_with_token_is_fine() -> None:
    _validate_http_exposure("0.0.0.0", "sekrit")


def _validated_status(host: str, headers: dict[str, str]) -> int | None:
    from mcp.server.transport_security import TransportSecurityMiddleware
    from starlette.requests import Request

    from codebase_rag.mcp.server import _transport_security

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [
            (key.encode(), value.encode())
            for key, value in {"content-type": "application/json", **headers}.items()
        ],
    }
    middleware = TransportSecurityMiddleware(_transport_security(host))
    response = asyncio.run(middleware.validate_request(Request(scope), is_post=True))
    return None if response is None else response.status_code


def test_a_rebound_hostname_is_refused_on_a_loopback_bind() -> None:
    # Tokenless loopback trusts the bind address; a page that rebinds its
    # own hostname to 127.0.0.1 still sends that hostname as Host.
    assert _validated_status("127.0.0.1", {"host": "evil.example:8080"}) == 421


def test_a_foreign_origin_is_refused_on_a_loopback_bind() -> None:
    status = _validated_status(
        "127.0.0.1", {"host": "127.0.0.1:8080", "origin": "http://evil.example"}
    )
    assert status == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8080", "localhost:8080", "[::1]:8080"])
def test_a_local_client_passes_on_a_loopback_bind(host: str) -> None:
    assert _validated_status("127.0.0.1", {"host": host}) is None


def test_a_tokened_remote_bind_leaves_host_checks_to_the_token() -> None:
    assert _validated_status("0.0.0.0", {"host": "mcp.internal:8080"}) is None


def test_serve_http_passes_the_rebinding_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from mcp.server import streamable_http_manager

    from codebase_rag.mcp import server

    captured: dict[str, Any] = {}

    class _Stop(Exception):
        pass

    def fake_manager(**kwargs: Any) -> None:
        captured.update(kwargs)
        raise _Stop

    monkeypatch.setattr(
        streamable_http_manager, "StreamableHTTPSessionManager", fake_manager
    )
    monkeypatch.setattr(server, "create_server", lambda _ws: (MagicMock(), None))
    with pytest.raises(_Stop):
        asyncio.run(server.serve_http(host="127.0.0.1", port=8080))
    settings = captured["security_settings"]
    assert settings is not None
    assert settings.enable_dns_rebinding_protection
