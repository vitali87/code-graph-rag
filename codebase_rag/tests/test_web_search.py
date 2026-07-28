from __future__ import annotations

import pytest
from pydantic_ai import Tool

from codebase_rag import tool_errors as te
from codebase_rag.tools.web_search import WebSearcher, create_web_search_tool


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def captured(monkeypatch) -> dict:
    """Intercepts the outbound call: no test in this file touches the network."""
    sent: dict = {}

    def fake_post(url, **kwargs):
        sent.update(url=url, **kwargs)
        return sent.pop("_response", None) or FakeResponse(
            {
                "results": [
                    {
                        "url": "https://docs.example.com/a",
                        "title": None,  # the API sends null when a page has no title
                        "date": "2026-07-01",
                        "content": "the extracted text of the page",
                    },
                    {
                        "url": "https://docs.example.com/b",
                        "title": "Second source",
                        "content": "more extracted text",
                    },
                ]
            }
        )

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.delenv("SERPDIVE_MODEL", raising=False)
    return sent


def test_search_returns_page_content_not_just_links(captured: dict) -> None:
    out = WebSearcher("sd_live_x").search_web("pydantic-ai tool api", max_results=2)

    assert captured["url"] == "https://api.serpdive.com/v1/search"
    assert captured["headers"]["Authorization"] == "Bearer sd_live_x"
    assert captured["json"]["query"] == "pydantic-ai tool api"
    assert captured["json"]["max_results"] == 2
    # the URL is what the agent cites, the content is what it reads
    assert "https://docs.example.com/a" in out
    assert "the extracted text of the page" in out
    assert "Published: 2026-07-01" in out
    # a null title must never surface as "None"
    assert "None" not in out
    assert "Second source" in out


def test_default_model_is_the_free_tier(captured: dict) -> None:
    WebSearcher("k").search_web("q")
    assert captured["json"]["model"] == "krill"


def test_paid_model_is_opt_in_and_a_typo_falls_back_to_free(
    captured: dict, monkeypatch
) -> None:
    monkeypatch.setenv("SERPDIVE_MODEL", "mako")
    WebSearcher("k").search_web("q")
    assert captured["json"]["model"] == "mako"

    monkeypatch.setenv("SERPDIVE_MODEL", "MAKO-turbo")  # unknown value
    WebSearcher("k").search_web("q")
    assert captured["json"]["model"] == "krill"  # never a paid model by accident


def test_max_results_is_clamped(captured: dict) -> None:
    WebSearcher("k").search_web("q", max_results=99)
    assert captured["json"]["max_results"] == 10
    WebSearcher("k").search_web("q", max_results=0)
    assert captured["json"]["max_results"] == 1


def test_empty_query_is_refused_without_a_call(captured: dict) -> None:
    assert WebSearcher("k").search_web("   ") == te.WEB_SEARCH_EMPTY_QUERY
    assert not captured  # nothing left the process


def test_http_error_is_reported_not_raised(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: FakeResponse({}, status_code=401)
    )
    out = WebSearcher("bad-key").search_web("q")
    assert out == te.WEB_SEARCH_FAILED.format(status=401)


def test_network_failure_is_reported_not_raised(monkeypatch) -> None:
    import httpx

    def boom(url, **kw):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx, "post", boom)
    assert WebSearcher("k").search_web("q") == te.WEB_SEARCH_UNREACHABLE


def test_no_results_is_a_message_not_an_error(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, **kw: FakeResponse({"results": []}))
    assert WebSearcher("k").search_web("q") == te.WEB_SEARCH_NO_RESULTS.format(
        query="q"
    )


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "a", "dict"],
        {"results": "not a list"},
        {"results": [{"url": "https://ok"}, "not a dict"]},
        {},
    ],
)
def test_malformed_payload_is_reported_not_raised(monkeypatch, payload) -> None:
    """A 200 does not guarantee the shape; the tool must not raise from inside."""
    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, **kw: FakeResponse(payload))
    assert WebSearcher("k").search_web("q") == te.WEB_SEARCH_BAD_RESPONSE


def test_tool_is_registered_with_the_expected_name() -> None:
    tool = create_web_search_tool(WebSearcher("k"))
    assert isinstance(tool, Tool)
    assert tool.name == "web_search"
