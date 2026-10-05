# ruff: noqa: T201
"""Smoke the installed wheel against freshly resolved dependencies (issue #1096).

CI otherwise only ever installs from ``uv.lock``, so a dependency that releases
a breaking major inside our declared floor ships to PyPI unnoticed: #964 was a
`pydantic-ai` 2.0 change that broke every fresh install for days while CI stayed
green. This script runs against whatever versions plain resolution picked and
exercises the third-party surfaces the app actually calls.

Run it from a clean environment that has the wheel installed, not from a
lock-synced dev checkout.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import timedelta
from importlib.metadata import version


class SmokeFailure(Exception):
    """A dependency surface the application depends on has changed shape."""


def check_package_imports() -> None:
    """The installed wheel must be what imports, never the source checkout.

    Running this from the repo root would otherwise let a working source tree
    mask a wheel that cannot import at all.
    """
    import codebase_rag

    module_file = getattr(codebase_rag, "__file__", None)
    if not module_file:
        raise SmokeFailure("codebase_rag imported without a module file")

    print(f"      codebase_rag imported from {module_file}")
    if "site-packages" not in module_file:
        raise SmokeFailure(
            f"codebase_rag was imported from {module_file}, which is not an "
            "installed package; this smoke must run against the built wheel"
        )


def check_cli_entry_point() -> None:
    from codebase_rag.cli import app

    if app is None:
        raise SmokeFailure("codebase_rag.cli.app is not importable")


def check_agent_run_result_usage_is_a_property() -> None:
    """`main.py` reads ``response.usage`` as an attribute, not a call."""
    from pydantic_ai.agent import AgentRunResult

    usage_attr = inspect.getattr_static(AgentRunResult, "usage")
    if not isinstance(usage_attr, property):
        raise SmokeFailure(
            f"AgentRunResult.usage is {type(usage_attr).__name__}, not a property; "
            f"codebase_rag/main.py reads it as an attribute "
            f"(pydantic-ai {version('pydantic-ai')})"
        )


def check_agent_loop_reports_usage() -> None:
    """Run a real agent turn against a stub model and price its usage.

    This is the exact sequence `_run_agent_turn` performs: read ``.usage`` off
    the result, pull token counts off it, and hand it to the pricing helper.
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from codebase_rag.services.usage_cost import price_run

    agent = Agent(TestModel())
    response = asyncio.run(agent.run("smoke"))

    run_usage = response.usage
    for field in ("input_tokens", "output_tokens"):
        if not isinstance(getattr(run_usage, field, None), int):
            raise SmokeFailure(
                f"RunUsage.{field} is missing or not an int; "
                f"codebase_rag/main.py accumulates it per turn "
                f"(pydantic-ai {version('pydantic-ai')})"
            )

    # Unknown models have no public price, so None is the expected answer here;
    # a raised exception is not.
    price_run(run_usage, "test", "test-model")

    if not response.new_messages():
        raise SmokeFailure("agent run produced no messages to extend the history with")


def check_mcp_server_lists_tools() -> None:
    """Build the real MCP server and answer ``initialize`` + ``tools/list``.

    Issue #2519: mcp 2.x removed the ``Server.list_tools()`` decorator that
    ``codebase_rag/mcp/server.py`` registers its handlers with, so
    ``cgr mcp-server`` died on start for every fresh install. Importing the
    module still worked, since the attribute is only read while the server is
    built, so this builds it and performs the handshake a host does, over an
    in-memory transport. Memgraph is only connected when the server is served,
    so the job needs no database.
    """
    from codebase_rag.config import settings
    from codebase_rag.mcp.server import create_server

    # A remote provider with a placeholder key builds its model offline; the
    # default local provider would probe an Ollama endpoint the job lacks.
    offline_model = ("openai", "gpt-4o-mini")
    settings.set_orchestrator(*offline_model, api_key="smoke-offline")
    settings.set_cypher(*offline_model, api_key="smoke-offline")

    try:
        server, _ = create_server()
    except AttributeError as exc:
        raise SmokeFailure(
            f"codebase_rag/mcp/server.py cannot register its handlers on this "
            f"SDK: {exc} (mcp {version('mcp')})"
        ) from exc

    # Imported only once the server is built: an SDK that also moved this
    # client helper must still fail on the server's own break, the one a user
    # hits, rather than on a test utility.
    from mcp.shared.memory import create_connected_server_and_client_session

    async def list_tool_names() -> list[str]:
        async with create_connected_server_and_client_session(
            server, read_timeout_seconds=timedelta(seconds=30)
        ) as client:
            listed = await client.list_tools()
        return [tool.name for tool in listed.tools]

    names = asyncio.run(list_tool_names())
    if not names:
        raise SmokeFailure(
            f"the MCP server answered tools/list with no tools (mcp {version('mcp')})"
        )
    print(f"      mcp {version('mcp')}: initialize + tools/list -> {len(names)} tools")


CHECKS = (
    check_package_imports,
    check_cli_entry_point,
    check_agent_run_result_usage_is_a_property,
    check_agent_loop_reports_usage,
    check_mcp_server_lists_tools,
)


def main() -> int:
    failures: list[str] = []
    for check in CHECKS:
        try:
            check()
        except Exception as exc:
            failures.append(f"{check.__name__}: {exc}")
            print(f"FAIL  {check.__name__}: {exc}", file=sys.stderr)
        else:
            print(f"ok    {check.__name__}")

    if failures:
        print(
            f"\n{len(failures)} smoke check(s) failed against freshly resolved "
            "dependencies. A dependency released a breaking version inside the "
            "range pyproject.toml declares.",
            file=sys.stderr,
        )
        return 1

    print(f"\nAll {len(CHECKS)} smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
