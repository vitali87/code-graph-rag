"""The endpoint helpers the providers and `cgr doctor` share: the server root
a `/v1` endpoint's health check targets, and a path joined below an
endpoint's own path."""

from __future__ import annotations

import pytest

from codebase_rag.utils.provider_urls import join_endpoint_path, strip_v1_suffix


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://host:11434/v1", "http://host:11434"),
        ("http://host:11434/v1/", "http://host:11434"),
        ("https://host/ollama/v1", "https://host/ollama"),
        # Only a whole `/v1` segment goes: a port or host ending in the
        # characters of "/v1" is kept, which a character-set strip would eat.
        ("http://host:4001", "http://host:4001"),
        ("http://hostv1", "http://hostv1"),
        ("http://host/v1/v1", "http://host/v1"),
        ("", ""),
    ],
)
def test_strip_v1_suffix_removes_one_trailing_v1_segment(
    endpoint: str, expected: str
) -> None:
    assert strip_v1_suffix(endpoint) == expected


@pytest.mark.parametrize(
    ("endpoint", "path", "expected"),
    [
        ("http://host:11434", "api/tags", "http://host:11434/api/tags"),
        ("http://host:11434/", "/api/tags", "http://host:11434/api/tags"),
        # An absolute path stays below a proxied endpoint's own path, where
        # urljoin would replace it.
        ("https://host/ollama", "/api/tags", "https://host/ollama/api/tags"),
        ("https://host/ollama///", "///api/tags", "https://host/ollama/api/tags"),
    ],
)
def test_join_endpoint_path_keeps_the_endpoints_own_path(
    endpoint: str, path: str, expected: str
) -> None:
    assert join_endpoint_path(endpoint, path) == expected


def test_a_stripped_endpoint_joins_the_health_path_at_the_server_root() -> None:
    assert (
        join_endpoint_path(strip_v1_suffix("https://host/ollama/v1/"), "/api/tags")
        == "https://host/ollama/api/tags"
    )
