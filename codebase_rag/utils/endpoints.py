"""Endpoint helpers that need no provider package.

Kept apart from `providers.base`, which imports the LiteLLM provider and with
it pydantic-ai at module level: `cgr doctor`'s Ollama probe is imported with
the CLI, and start-up must not load an LLM package (issue #2253).
"""

from .. import constants as cs


def strip_v1_suffix(endpoint: str) -> str:
    """`endpoint` without a trailing `/v1` path segment or slash.

    OpenAI-compatible endpoints are configured with `/v1`, but health checks
    live at the server root. `removesuffix`, not `rstrip`: the latter treats
    its argument as a character set and would eat a port or hostname ending
    in `1` or `v` (`http://host:4001/v1` -> `http://host:400`).
    """
    return (
        endpoint.rstrip(cs.SEPARATOR_SLASH)
        .removesuffix(cs.V1_PATH)
        .rstrip(cs.SEPARATOR_SLASH)
    )


def join_endpoint_path(endpoint: str, path: str) -> str:
    """`path` appended below `endpoint`'s own path.

    Not `urljoin`: an absolute `path` replaces the endpoint's path there, so an
    Ollama served behind a proxy at `https://host/ollama` would be probed at
    `https://host/api/tags` and reported as not running (review of PR 2502).
    """
    return (
        f"{endpoint.rstrip(cs.SEPARATOR_SLASH)}"
        f"{cs.SEPARATOR_SLASH}{path.lstrip(cs.SEPARATOR_SLASH)}"
    )
