from __future__ import annotations

import pytest
from pydantic_ai import Tool

from codebase_rag import tool_errors as te
from codebase_rag.tools.web_search import (
    DuckDuckGoBackend,
    SerpdiveBackend,
    WebSearcher,
    create_web_search_tool,
    make_web_searcher,
)

DDG_PAGE = """
<div class="result">
  <a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com%2Fa&amp;rut=x">
     First <b>source</b></a>
  <a class="result__snippet" href="#">the extracted <b>snippet</b> text</a>
</div>
<div class="result">
  <a rel="nofollow" class="result__a" href="https://docs.example.com/b">Second source</a>
  <a class="result__snippet" href="#">more snippet text</a>
</div>
"""


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def captured(monkeypatch) -> dict:
    """Intercepts the outbound call: no test in this file touches the network."""
    sent: dict = {}

    def fake_post(url, **kwargs):
        sent.update(url=url, **kwargs)
        response = sent.pop("_response", None)
        if response is not None:
            return response
        if "duckduckgo" in url:
            return FakeResponse(text=DDG_PAGE)
        return FakeResponse(
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
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("SERPDIVE_API_KEY", raising=False)
    return sent


def serpdive() -> WebSearcher:
    return WebSearcher(SerpdiveBackend("sd_live_x"))


def duckduckgo() -> WebSearcher:
    return WebSearcher(DuckDuckGoBackend())


class TestKeylessDefault:
    def test_default_provider_needs_no_key_and_no_configuration(
        self, captured: dict
    ) -> None:
        searcher = make_web_searcher()
        assert isinstance(searcher.backend, DuckDuckGoBackend)

    def test_search_works_without_any_account(self, captured: dict) -> None:
        out = duckduckgo().search_web("pydantic-ai tool api", max_results=2)

        assert captured["url"] == "https://html.duckduckgo.com/html/"
        assert "Authorization" not in captured.get("headers", {})
        # the redirect wrapper is unwrapped to the target the agent cites
        assert "https://docs.example.com/a" in out
        assert "First source" in out
        assert "the extracted snippet text" in out
        assert "Second source" in out
        assert "https://docs.example.com/b" in out

    def test_max_results_caps_parsed_results(self, captured: dict) -> None:
        out = duckduckgo().search_web("q", max_results=1)
        assert "https://docs.example.com/a" in out
        assert "https://docs.example.com/b" not in out


DDG_PAGE_HOSTILE = """
<div class="result">
  <a href="https://docs.example.com/first" rel="nofollow"
     class="result__a result--url-above">First</a>
  <a class="snippet result__snippet" href="#">first snippet</a>
</div>
<div class="result">
  <a rel="nofollow" class="result__a" href="https://docs.example.com/bare">Bare</a>
</div>
<div class="result">
  <a rel="nofollow" class="result__a" href="https://docs.example.com/third">Third</a>
  <a class="result__snippet" href="#">third snippet</a>
</div>
"""


class TestDuckDuckGoParsing:
    def test_href_first_and_multi_class_anchors_are_matched(
        self, captured: dict
    ) -> None:
        """Attribute order and extra class tokens are valid HTML, not a miss."""
        captured["_response"] = FakeResponse(text=DDG_PAGE_HOSTILE)
        out = duckduckgo().search_web("q")
        assert "https://docs.example.com/first" in out
        assert "first snippet" in out

    def test_snippetless_result_does_not_steal_the_next_snippet(
        self, captured: dict
    ) -> None:
        """A missing snippet must yield no content for that result, never a
        neighbour's text attributed to the wrong URL."""
        captured["_response"] = FakeResponse(text=DDG_PAGE_HOSTILE)
        out = duckduckgo().search_web("q")
        blocks = out.split("\n\n")
        bare = next(b for b in blocks if "https://docs.example.com/bare" in b)
        third = next(b for b in blocks if "https://docs.example.com/third" in b)
        assert "snippet" not in bare
        assert "third snippet" in third

    def test_snippet_before_the_title_anchor_stays_with_its_result(
        self, captured: dict
    ) -> None:
        """Reordered markup inside a container must not push the snippet onto
        the preceding result."""
        page = """
        <div class="result web-result">
          <a rel="nofollow" class="result__a" href="https://docs.example.com/one">One</a>
        </div>
        <div class="result web-result">
          <a class="result__snippet" href="#">second snippet first</a>
          <a rel="nofollow" class="result__a" href="https://docs.example.com/two">Two</a>
        </div>
        """
        captured["_response"] = FakeResponse(text=page)
        out = duckduckgo().search_web("q")
        blocks = out.split("\n\n")
        one = next(b for b in blocks if "https://docs.example.com/one" in b)
        two = next(b for b in blocks if "https://docs.example.com/two" in b)
        assert "second snippet first" not in one
        assert "second snippet first" in two


class TestProviderSelection:
    def test_serpdive_is_selectable_with_a_key(self, captured: dict, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "serpdive")
        monkeypatch.setenv("SERPDIVE_API_KEY", "sd_live_x")
        assert isinstance(make_web_searcher().backend, SerpdiveBackend)

    def test_serpdive_without_a_key_falls_back_to_keyless(
        self, captured: dict, monkeypatch
    ) -> None:
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "serpdive")
        assert isinstance(make_web_searcher().backend, DuckDuckGoBackend)

    def test_unknown_provider_falls_back_to_keyless(
        self, captured: dict, monkeypatch
    ) -> None:
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "not-a-provider")
        assert isinstance(make_web_searcher().backend, DuckDuckGoBackend)


class TestSerpdiveBackend:
    def test_search_returns_page_content_not_just_links(self, captured: dict) -> None:
        out = serpdive().search_web("pydantic-ai tool api", max_results=2)

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

    def test_outbound_model_is_always_the_free_tier(
        self, captured: dict, monkeypatch
    ) -> None:
        """The open tree carries free capability only: no environment variable,
        argument or typo can put a billed tier in the request body."""
        monkeypatch.setenv("SERPDIVE_MODEL", "mako")
        serpdive().search_web("q")
        assert captured["json"]["model"] == "krill"

    def test_max_results_is_clamped(self, captured: dict) -> None:
        serpdive().search_web("q", max_results=99)
        assert captured["json"]["max_results"] == 10
        serpdive().search_web("q", max_results=0)
        assert captured["json"]["max_results"] == 1

    def test_no_results_is_a_message_not_an_error(self, captured: dict) -> None:
        captured["_response"] = FakeResponse({"results": []})
        assert serpdive().search_web("q") == te.WEB_SEARCH_NO_RESULTS.format(query="q")

    def test_overdelivering_provider_is_sliced_to_the_request(
        self, captured: dict
    ) -> None:
        """A provider returning more than max_results must not widen the output."""
        captured["_response"] = FakeResponse(
            {
                "results": [
                    {"url": "https://docs.example.com/a", "title": "A"},
                    {"url": "https://docs.example.com/b", "title": "B"},
                ]
            }
        )
        out = serpdive().search_web("q", max_results=1)
        assert "https://docs.example.com/a" in out
        assert "https://docs.example.com/b" not in out

    @pytest.mark.parametrize(
        "payload",
        [
            ["not", "a", "dict"],
            {"results": "not a list"},
            {"results": [{"url": "https://ok"}, "not a dict"]},
            {},
            {"results": [{"url": None, "title": "no url"}]},
            {"results": [{"url": "https://ok", "content": ["not", "a", "string"]}]},
            {"results": [{"url": "https://ok", "date": 20260701}]},
        ],
    )
    def test_malformed_payload_is_reported_not_raised(
        self, captured: dict, payload
    ) -> None:
        """A 200 does not guarantee the shape; the tool must not raise."""
        captured["_response"] = FakeResponse(payload)
        assert serpdive().search_web("q") == te.WEB_SEARCH_BAD_RESPONSE


@pytest.mark.parametrize("make", [serpdive, duckduckgo])
class TestSharedGuards:
    def test_empty_query_is_refused_without_a_call(self, captured: dict, make) -> None:
        assert make().search_web("   ") == te.WEB_SEARCH_EMPTY_QUERY
        assert not captured  # nothing left the process

    def test_http_error_is_reported_not_raised(self, captured: dict, make) -> None:
        captured["_response"] = FakeResponse({}, status_code=401)
        assert make().search_web("q") == te.WEB_SEARCH_FAILED.format(status=401)

    def test_network_failure_is_reported_not_raised(self, monkeypatch, make) -> None:
        import httpx

        def boom(url, **kw):
            raise httpx.ConnectError("network down")

        monkeypatch.setattr(httpx, "post", boom)
        assert make().search_web("q") == te.WEB_SEARCH_UNREACHABLE


def test_tool_is_registered_with_the_expected_name() -> None:
    tool = create_web_search_tool(WebSearcher(DuckDuckGoBackend()))
    assert isinstance(tool, Tool)
    assert tool.name == "web_search"
