# (H) Follow-up to #808/#809: the loopback default made the unauthenticated
# (H) StreamableHTTP endpoint safe by default, but intentional remote exposure
# (H) still had NO auth at all. serve_http now refuses a non-loopback bind
# (H) without a configured token, and a configured token puts bearer-auth
# (H) middleware (constant-time compare) in front of the mount.
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


def test_non_loopback_bind_without_token_refuses() -> None:
    with pytest.raises(ValueError):
        _validate_http_exposure("0.0.0.0", None)


def test_loopback_bind_without_token_is_fine() -> None:
    _validate_http_exposure("127.0.0.1", None)
    _validate_http_exposure("localhost", None)
    _validate_http_exposure("::1", None)


def test_non_loopback_bind_with_token_is_fine() -> None:
    _validate_http_exposure("0.0.0.0", "sekrit")
