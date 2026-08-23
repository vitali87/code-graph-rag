from __future__ import annotations

import io
import json
from typing import Any

import pytest

from codebase_rag.exceptions import ArcadeHttpError
from codebase_rag.services.graph.arcade_http import ArcadeHttpClient


class _FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_sql_posts_to_the_command_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.get_header("Authorization")
        return _FakeResponse(200, {"result": [{"ok": 1}]})

    monkeypatch.setattr(
        "codebase_rag.services.graph.arcade_http.urllib.request.urlopen", fake_urlopen
    )
    client = ArcadeHttpClient(
        host="localhost", port=2480, database="cg", username="root", password="pw"
    )
    rows = client.sql("CREATE VERTEX TYPE Function IF NOT EXISTS")

    assert captured["url"] == "http://localhost:2480/api/v1/command/cg"
    assert captured["body"] == {
        "language": "sql",
        "command": "CREATE VERTEX TYPE Function IF NOT EXISTS",
    }
    assert captured["auth"].startswith("Basic ")
    assert rows == [{"ok": 1}]


def test_sql_raises_on_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        raise urllib.error.HTTPError(
            req.full_url,
            500,
            "Server Error",
            {},
            None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        "codebase_rag.services.graph.arcade_http.urllib.request.urlopen", fake_urlopen
    )
    client = ArcadeHttpClient(
        host="localhost", port=2480, database="cg", username="root", password="pw"
    )
    with pytest.raises(ArcadeHttpError, match="500"):
        client.sql("CREATE VERTEX TYPE Bad")


def test_sql_returns_empty_list_when_result_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        return _FakeResponse(200, {})

    monkeypatch.setattr(
        "codebase_rag.services.graph.arcade_http.urllib.request.urlopen", fake_urlopen
    )
    client = ArcadeHttpClient(
        host="localhost", port=2480, database="cg", username="root", password="pw"
    )
    assert client.sql("CREATE VERTEX TYPE X IF NOT EXISTS") == []


def test_sql_error_includes_the_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    # ArcadeDB puts the actual cause of a DDL failure (e.g. which of an
    # 85-statement schema bootstrap failed and why) in the response body;
    # e.reason alone is just the generic HTTP phrase.
    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        raise urllib.error.HTTPError(
            req.full_url,
            500,
            "Server Error",
            {},
            io.BytesIO(b"schema error: property Foo.bar already has a different type"),
        )

    monkeypatch.setattr(
        "codebase_rag.services.graph.arcade_http.urllib.request.urlopen", fake_urlopen
    )
    client = ArcadeHttpClient(
        host="localhost", port=2480, database="cg", username="root", password="pw"
    )
    with pytest.raises(ArcadeHttpError, match="already has a different type"):
        client.sql("CREATE PROPERTY Foo.bar IF NOT EXISTS STRING")


def test_sql_error_falls_back_to_reason_when_body_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    # fp=None (as in test_sql_raises_on_error_status above) means e.read()
    # cannot succeed; the message must still degrade to e.reason rather
    # than raise out of the error-handling path itself.
    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        raise urllib.error.HTTPError(
            req.full_url,
            500,
            "Server Error",
            {},
            None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        "codebase_rag.services.graph.arcade_http.urllib.request.urlopen", fake_urlopen
    )
    client = ArcadeHttpClient(
        host="localhost", port=2480, database="cg", username="root", password="pw"
    )
    with pytest.raises(ArcadeHttpError, match="Server Error"):
        client.sql("CREATE VERTEX TYPE Bad")


class TestPlaintextCredentialsRefused:
    """ArcadeHttpClient sends Basic auth on every request; over plaintext
    http that must never leave the loopback interface (CodeRabbit review:
    ARCADEDB_HOST accepted any host while the scheme was hardcoded http,
    so credentials could go to a remote host in the clear)."""

    def test_loopback_and_http_is_allowed(self) -> None:
        ArcadeHttpClient(
            host="localhost",
            port=2480,
            database="cg",
            username="root",
            password="pw",
            scheme="http",
        )

    def test_loopback_ip_and_http_is_allowed(self) -> None:
        ArcadeHttpClient(
            host="127.0.0.1",
            port=2480,
            database="cg",
            username="root",
            password="pw",
            scheme="http",
        )

    def test_non_loopback_and_http_raises(self) -> None:
        with pytest.raises(ValueError, match="plaintext"):
            ArcadeHttpClient(
                host="db.example.com",
                port=2480,
                database="cg",
                username="root",
                password="pw",
                scheme="http",
            )

    def test_non_loopback_and_https_is_allowed(self) -> None:
        ArcadeHttpClient(
            host="db.example.com",
            port=2480,
            database="cg",
            username="root",
            password="pw",
            scheme="https",
        )

    def test_loopback_and_https_is_allowed(self) -> None:
        ArcadeHttpClient(
            host="localhost",
            port=2480,
            database="cg",
            username="root",
            password="pw",
            scheme="https",
        )
