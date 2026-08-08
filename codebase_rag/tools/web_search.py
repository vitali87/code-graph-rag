from __future__ import annotations

import html
import os
import re
import urllib.parse

from loguru import logger
from pydantic_ai import Tool

from .. import logs as ls
from .. import tool_errors as te
from . import tool_descriptions as td

# Backend selection follows the provider convention the LLM and embedding
# layers use: a provider name from configuration plus {PROVIDER}_API_KEY for
# the ones that need a key. The default provider needs no account at all.
WEB_SEARCH_PROVIDER_ENV = "WEB_SEARCH_PROVIDER"
SERPDIVE_API_KEY_ENV = "SERPDIVE_API_KEY"

_TIMEOUT = 30.0
_MAX_RESULTS = 10

# Keyless default. DuckDuckGo's HTML endpoint needs no account, so the tool
# works out of the box; richer backends are opt-in through WEB_SEARCH_PROVIDER.
DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
_DDG_ANCHOR = re.compile(r"<a\s(?P<attrs>[^>]*)>(?P<body>.*?)</a>", re.DOTALL)
_DDG_HREF = re.compile(r'href="(?P<href>[^"]*)"')
_DDG_CLASS = re.compile(r'class="(?P<classes>[^"]*)"')
_DDG_RESULT_CLASS = "result__a"
_DDG_SNIPPET_CLASS = "result__snippet"
_TAGS = re.compile(r"<[^>]+>")

# Keys are created at https://serpdive.com/dashboard/keys (free, no card).
# `krill` is SERPdive's free tier and the only tier this tool will ever
# request: the open tree carries free capability only, so the request body
# pins it unconditionally and nothing here can select a billed plan.
SERPDIVE_URL = "https://api.serpdive.com/v1/search"
SERPDIVE_FREE_TIER = "krill"


class DuckDuckGoBackend:
    """Keyless ranked results with snippets from DuckDuckGo's HTML endpoint."""

    __slots__ = ()
    name = "duckduckgo"

    def fetch(self, query: str, max_results: int) -> list[dict] | str:
        import httpx

        try:
            response = httpx.post(
                DUCKDUCKGO_URL,
                data={"q": query},
                headers={"User-Agent": "code-graph-rag"},
                timeout=_TIMEOUT,
                follow_redirects=True,
            )
        except Exception as e:
            logger.error(ls.WEB_SEARCH_ERROR.format(query=query, error=e))
            return te.WEB_SEARCH_UNREACHABLE
        if response.status_code != 200:
            logger.error(
                ls.WEB_SEARCH_HTTP_ERROR.format(
                    status=response.status_code, query=query
                )
            )
            return te.WEB_SEARCH_FAILED.format(status=response.status_code)

        return _parse_ddg_page(response.text, max_results)


class SerpdiveBackend:
    """SERPdive free tier: ranked results plus the extracted text of each page."""

    __slots__ = ("api_key",)
    name = "serpdive"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def fetch(self, query: str, max_results: int) -> list[dict] | str:
        import httpx

        try:
            response = httpx.post(
                SERPDIVE_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "query": query,
                    "model": SERPDIVE_FREE_TIER,
                    "max_results": max_results,
                },
                timeout=_TIMEOUT,
            )
        except Exception as e:
            logger.error(ls.WEB_SEARCH_ERROR.format(query=query, error=e))
            return te.WEB_SEARCH_UNREACHABLE
        if response.status_code != 200:
            logger.error(
                ls.WEB_SEARCH_HTTP_ERROR.format(
                    status=response.status_code, query=query
                )
            )
            return te.WEB_SEARCH_FAILED.format(status=response.status_code)

        try:
            payload = response.json()
        except Exception as e:
            logger.error(ls.WEB_SEARCH_ERROR.format(query=query, error=e))
            return te.WEB_SEARCH_BAD_RESPONSE
        # A 200 does not guarantee the shape. Validate before formatting rather
        # than letting a malformed payload raise from inside the tool.
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or any(
            not isinstance(r, dict) for r in results
        ):
            logger.error(ls.WEB_SEARCH_BAD_SHAPE.format(query=query))
            return te.WEB_SEARCH_BAD_RESPONSE
        return results


def _ddg_anchors(page: str, class_token: str) -> list[tuple[int, str, str]]:
    # Anchor attributes appear in any order and class carries multiple tokens,
    # so match the tag first and test class membership rather than relying on
    # one attribute layout.
    anchors: list[tuple[int, str, str]] = []
    for m in _DDG_ANCHOR.finditer(page):
        classes = _DDG_CLASS.search(m.group("attrs"))
        if classes is None or class_token not in classes.group("classes").split():
            continue
        href = _DDG_HREF.search(m.group("attrs"))
        anchors.append((m.start(), href.group("href") if href else "", m.group("body")))
    return anchors


def _parse_ddg_page(page: str, max_results: int) -> list[dict]:
    hits = _ddg_anchors(page, _DDG_RESULT_CLASS)
    snippets = _ddg_anchors(page, _DDG_SNIPPET_CLASS)
    results: list[dict] = []
    for i, (position, href, title) in enumerate(hits[:max_results]):
        # A snippet belongs to the result whose block it sits in: after this
        # result's anchor and before the next one. A result without a snippet
        # therefore yields no content instead of stealing its neighbour's.
        block_end = hits[i + 1][0] if i + 1 < len(hits) else len(page)
        content = next(
            (body for pos, _, body in snippets if position < pos < block_end),
            None,
        )
        results.append(
            {
                "url": _decode_ddg_href(href),
                "title": _strip_markup(title),
                "content": _strip_markup(content) if content is not None else None,
            }
        )
    return results


def _decode_ddg_href(href: str) -> str:
    # DuckDuckGo wraps result links in a redirect carrying the target as uddg=.
    parsed = urllib.parse.urlparse(html.unescape(href))
    target = urllib.parse.parse_qs(parsed.query).get("uddg")
    return target[0] if target else html.unescape(href)


def _strip_markup(fragment: str) -> str:
    return html.unescape(_TAGS.sub("", fragment)).strip()


class WebSearcher:
    """Web search behind a configured backend, keyless by default.

    One call gives the agent ranked results and, when the backend extracts
    page text, the readable content of each one.
    """

    __slots__ = ("backend",)

    def __init__(self, backend: DuckDuckGoBackend | SerpdiveBackend) -> None:
        self.backend = backend

    def search_web(self, query: str, max_results: int = 5) -> str:
        if not (query := query.strip()):
            return te.WEB_SEARCH_EMPTY_QUERY
        capped = max(1, min(int(max_results), _MAX_RESULTS))
        logger.info(ls.WEB_SEARCH_QUERY.format(provider=self.backend.name, query=query))
        results = self.backend.fetch(query, capped)
        if isinstance(results, str):
            return results
        if not results:
            return te.WEB_SEARCH_NO_RESULTS.format(query=query)
        return "\n\n".join(self._format(i, r) for i, r in enumerate(results, 1))

    @staticmethod
    def _format(index: int, result: dict) -> str:
        # `title` is null when a page has none; the URL is what the agent cites.
        title = result.get("title") or result.get("url", "")
        block = [f"[{index}] {title}", result.get("url", "")]
        if date := result.get("date"):
            block.append(f"Published: {date}")
        if content := result.get("content"):
            block.append(content)
        return "\n".join(block)


def make_web_searcher() -> WebSearcher:
    provider = (
        os.environ.get(WEB_SEARCH_PROVIDER_ENV, DuckDuckGoBackend.name).strip().lower()
    )
    if provider == SerpdiveBackend.name:
        if api_key := os.environ.get(SERPDIVE_API_KEY_ENV):
            return WebSearcher(SerpdiveBackend(api_key))
        logger.warning(ls.WEB_SEARCH_KEYLESS_FALLBACK.format(provider=provider))
    elif provider != DuckDuckGoBackend.name:
        logger.warning(ls.WEB_SEARCH_UNKNOWN_PROVIDER.format(provider=provider))
    return WebSearcher(DuckDuckGoBackend())


def create_web_search_tool(web_searcher: WebSearcher) -> Tool:
    return Tool(
        function=web_searcher.search_web,
        name=td.AgenticToolName.WEB_SEARCH,
        description=td.WEB_SEARCH,
    )
