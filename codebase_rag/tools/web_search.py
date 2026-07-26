from __future__ import annotations

import os

from loguru import logger
from pydantic_ai import Tool

from .. import logs as ls
from .. import tool_errors as te
from . import tool_descriptions as td

# Keys are created at https://serpdive.com/dashboard/keys (free, no card).
SERPDIVE_URL = "https://api.serpdive.com/v1/search"
_TIMEOUT = 30.0
_MAX_RESULTS = 10

# Retrieval depth. `krill` is the free tier and stays the default: adding this
# tool must not start spending on anyone's behalf, and it does not run out at
# the end of the month either. `mako` and `moby` are billed and opt-in through
# SERPDIVE_MODEL.
_MODELS = ("krill", "mako", "moby")
_DEFAULT_MODEL = "krill"


class WebSearcher:
    """Web search that returns the extracted text of each page, not just links.

    One call gives the agent ranked results and the readable content of each
    one, so answering a question about a library, an API or a recent release
    does not need a second round trip to fetch the pages.
    """

    __slots__ = ("api_key", "model")

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self.api_key = api_key
        chosen = (model or os.environ.get("SERPDIVE_MODEL") or _DEFAULT_MODEL).strip().lower()
        # An unknown value falls back to the free tier rather than failing: a
        # typo in an env var must not cost money and must not break search.
        self.model = chosen if chosen in _MODELS else _DEFAULT_MODEL

    def search_web(self, query: str, max_results: int = 5) -> str:
        if not (query := query.strip()):
            return te.WEB_SEARCH_EMPTY_QUERY

        import httpx

        capped = max(1, min(int(max_results), _MAX_RESULTS))
        logger.info(ls.WEB_SEARCH_QUERY.format(query=query, model=self.model))

        try:
            response = httpx.post(
                SERPDIVE_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"query": query, "model": self.model, "max_results": capped},
                timeout=_TIMEOUT,
            )
        except Exception as e:
            logger.error(ls.WEB_SEARCH_ERROR.format(query=query, error=e))
            return te.WEB_SEARCH_UNREACHABLE

        if response.status_code != 200:
            logger.error(
                ls.WEB_SEARCH_HTTP_ERROR.format(status=response.status_code, query=query)
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
        if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
            logger.error(ls.WEB_SEARCH_BAD_SHAPE.format(query=query))
            return te.WEB_SEARCH_BAD_RESPONSE

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


def create_web_search_tool(web_searcher: WebSearcher) -> Tool:
    return Tool(
        function=web_searcher.search_web,
        name=td.AgenticToolName.WEB_SEARCH,
        description=td.WEB_SEARCH,
    )
