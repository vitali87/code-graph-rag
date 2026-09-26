from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.graph_updater import _load_dir_mtimes
from codebase_rag.stack.health import _http_reachable


def test_load_dir_mtimes_reads_a_valid_cache(tmp_path: Path) -> None:
    cache = tmp_path / "dir_mtimes.json"
    cache.write_text(json.dumps({"src": 1.5, "lib": 2, "bad": "x"}), encoding="utf-8")

    assert _load_dir_mtimes(cache) == {"src": 1.5, "lib": 2.0}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"{not json", id="json-decode-error"),
        pytest.param(b"\xff\xfe\x00garbage", id="unicode-decode-error"),
    ],
)
def test_load_dir_mtimes_treats_an_unreadable_cache_as_empty(
    tmp_path: Path, payload: bytes
) -> None:
    # Both errors reach the handler only as ValueError subclasses.
    cache = tmp_path / "dir_mtimes.json"
    cache.write_bytes(payload)

    assert _load_dir_mtimes(cache) == {}


def test_http_reachable_true_for_a_live_endpoint() -> None:
    resp = MagicMock(status=200)
    resp.__enter__.return_value = resp
    with patch("urllib.request.urlopen", return_value=resp):
        assert _http_reachable("http://localhost:6333") is True


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(urllib.error.URLError("refused"), id="url-error"),
        pytest.param(TimeoutError("timed out"), id="timeout-error"),
        pytest.param(ConnectionRefusedError(), id="connection-refused"),
    ],
)
def test_http_reachable_false_when_the_request_fails(error: Exception) -> None:
    # URLError and TimeoutError reach the handler only as OSError subclasses.
    with patch("urllib.request.urlopen", side_effect=error):
        assert _http_reachable("http://localhost:6333") is False
