"""Issue #2747: `flow_verdict` walks the resource flows the indexer writes.

The I/O capture group answers the leak question with a resource-to-resource
edge (`ENV::K -FLOWS_TO {kind: resource}-> STDOUT`), but the verdict loaded
only FLOWS_TO edges with a project-prefixed endpoint, which a `resource::...`
name never has. So `ENV::K -> STDOUT` answered NO_FLOW with no gaps, a
verified absence, while the edge was in the graph; a function that leaked a
value had no way onto a resource; and the cross-service walk only worked
when it started at the NETWORK resource itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.flow_verdict import (
    FLOW_VERDICT_FOUND,
    FLOW_VERDICT_NO_FLOW,
    FLOW_VERDICT_UNKNOWN,
    flow_reachability_verdict,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

SLACK = "resource::ENV::SLACK_TOKEN"
OTHER = "resource::ENV::OTHER_TOKEN"
STDOUT = "resource::STDOUT::<dynamic>"
CLIENT_TOKEN = "resource::ENV::CLIENT_TOKEN"
URL = "http://user-service:8000/items"
NETWORK = f"resource::NETWORK::{URL}"

NOTIFY = """import os


def send(v):
    print(v)


def notify():
    token = os.getenv("SLACK_TOKEN")
    send(token)
    print(token)


def quiet():
    other = os.getenv("OTHER_TOKEN")
    return len(other)


def hello():
    print("hi")
"""

CLIENT = f"""import os

import requests


def push_plain():
    requests.post("{URL}", json=os.getenv("CLIENT_TOKEN"))
"""

# `build` returns the env value, and `caller` posts that return to the
# other service (Greptile review on PR #2762).
CLIENT_RELAY = f"""import os

import requests


def build():
    return os.getenv("CLIENT_TOKEN")


def caller():
    requests.post("{URL}", json=build())
"""

SERVER = """app = object()


@app.post("/items")
def create_item(item):
    print(item)
"""

# The same endpoint, with a handler that ignores what it was sent.
SERVER_OK = SERVER.replace("print(item)", 'print("ok")')
HANDLER = "server.api.create_item"


def _index(ingestor: MemgraphIngestor, root: Path, project: str) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run(force=True)
    ingestor.flush_all()


def _project(tmp_path: Path, name: str, rel: str, text: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / rel).write_text(text, encoding="utf-8")
    return root


# The embedded engine is the default backend, and the verdict's reads must
# run there too.
@pytest.fixture(params=["memgraph_ingestor", "sqlite_ingestor"])
def ingestor(request: pytest.FixtureRequest) -> MemgraphIngestor:
    return request.getfixturevalue(request.param)


@pytest.fixture
def notify(ingestor: MemgraphIngestor, tmp_path: Path) -> MemgraphIngestor:
    _index(ingestor, _project(tmp_path, "notify", "app.py", NOTIFY), "notify")
    return ingestor


def _services(
    ingestor: MemgraphIngestor, tmp_path: Path, server: str, client: str = CLIENT
) -> MemgraphIngestor:
    _index(ingestor, _project(tmp_path, "server", "api.py", server), "server")
    _index(ingestor, _project(tmp_path, "client", "client.py", client), "client")
    return ingestor


@pytest.fixture
def services(ingestor: MemgraphIngestor, tmp_path: Path) -> MemgraphIngestor:
    return _services(ingestor, tmp_path, SERVER)


def test_an_env_value_printed_is_a_flow_to_stdout(notify: MemgraphIngestor) -> None:
    result = flow_reachability_verdict(notify.fetch_all, "notify", SLACK, STDOUT)

    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (SLACK, STDOUT)


def test_a_function_that_prints_a_value_flows_to_stdout(
    notify: MemgraphIngestor,
) -> None:
    result = flow_reachability_verdict(
        notify.fetch_all, "notify", "notify.app.notify", STDOUT
    )

    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == ("notify.app.notify", STDOUT)


@pytest.mark.parametrize(
    "function",
    ["notify.app.send", "notify.app.hello"],
    ids=["a-parameter-it-prints", "a-literal-it-prints"],
)
def test_a_function_reaches_the_sink_its_own_body_writes(
    notify: MemgraphIngestor, function: str
) -> None:
    # `send` prints the value `notify` read, so the resource flow is
    # `notify`'s; `hello` prints a literal, which makes no flow at all (bot
    # review on PR #2762).
    result = flow_reachability_verdict(notify.fetch_all, "notify", function, STDOUT)

    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (function, STDOUT)


@pytest.mark.parametrize("source", [CLIENT_TOKEN, "client.client.push_plain"])
@pytest.mark.parametrize(
    "server",
    [SERVER, SERVER_OK],
    ids=["the-handler-prints-its-request", "the-handler-prints-a-constant"],
)
def test_what_a_handler_prints_is_unknown_for_the_value_sent_to_it(
    ingestor: MemgraphIngestor, tmp_path: Path, source: str, server: str
) -> None:
    # The graph records that `create_item` writes STDOUT, not what it
    # writes: printing "ok" and printing the request leave the same edge.
    # So neither is FOUND, and neither is a verified absence (CodeRabbit
    # review on PR #2762).
    services = _services(ingestor, tmp_path, server)

    result = flow_reachability_verdict(services.fetch_all, "client", source, STDOUT)

    assert result.verdict == FLOW_VERDICT_UNKNOWN
    assert result.path == ()
    assert result.gaps == (HANDLER,)


def test_a_client_function_reaches_the_handler_of_another_service(
    services: MemgraphIngestor,
) -> None:
    result = flow_reachability_verdict(
        services.fetch_all,
        "client",
        "client.client.push_plain",
        HANDLER,
    )

    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (
        "client.client.push_plain",
        NETWORK,
        HANDLER,
    )
    assert result.remote_hops == ((NETWORK, HANDLER),)


def test_a_function_reaches_a_handler_through_the_function_that_posts_it(
    ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A question between two functions goes on from every function the walk
    # reaches: from `caller`, which `build` returns into, through the
    # NETWORK resource it writes. Taking writes only from the source stopped
    # at `caller` and answered NO_FLOW with full coverage, a false absence
    # (Greptile review on PR #2762).
    services = _services(ingestor, tmp_path, SERVER, CLIENT_RELAY)

    result = flow_reachability_verdict(
        services.fetch_all, "client", "client.client.build", HANDLER
    )

    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (
        "client.client.build",
        "client.client.caller",
        NETWORK,
        HANDLER,
    )
    assert result.remote_hops == ((NETWORK, HANDLER),)


def test_a_client_env_value_reaches_the_handler_of_another_service(
    services: MemgraphIngestor,
) -> None:
    result = flow_reachability_verdict(
        services.fetch_all, "client", CLIENT_TOKEN, HANDLER
    )

    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (CLIENT_TOKEN, NETWORK, HANDLER)


def test_an_env_value_asked_about_the_function_reading_it_is_unknown(
    notify: MemgraphIngestor,
) -> None:
    # `notify` reads SLACK_TOKEN, but no edge links a resource to the code
    # that reads it: NO_FLOW would be a verified absence that is not one
    # (CodeRabbit review on PR #2762).
    result = flow_reachability_verdict(
        notify.fetch_all, "notify", SLACK, "notify.app.notify"
    )

    assert result.verdict == FLOW_VERDICT_UNKNOWN
    assert result.gaps == ("notify.app.notify",)


# Negative: what must not change.


def test_a_function_to_function_flow_is_still_found(notify: MemgraphIngestor) -> None:
    result = flow_reachability_verdict(
        notify.fetch_all, "notify", "notify.app.notify", "notify.app.send"
    )

    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == ("notify.app.notify", "notify.app.send")


def test_an_env_value_that_is_never_written_has_no_flow(
    notify: MemgraphIngestor,
) -> None:
    result = flow_reachability_verdict(notify.fetch_all, "notify", OTHER, STDOUT)

    assert result.verdict == FLOW_VERDICT_NO_FLOW


def test_a_function_that_writes_nothing_has_no_flow_to_stdout(
    notify: MemgraphIngestor,
) -> None:
    result = flow_reachability_verdict(
        notify.fetch_all, "notify", "notify.app.quiet", STDOUT
    )

    assert result.verdict == FLOW_VERDICT_NO_FLOW


def test_another_projects_leak_is_not_this_projects_flow(
    notify: MemgraphIngestor, tmp_path: Path
) -> None:
    _index(notify, _project(tmp_path, "solo", "solo.py", "x = 1\n"), "solo")

    result = flow_reachability_verdict(notify.fetch_all, "solo", SLACK, STDOUT)

    assert result.verdict == FLOW_VERDICT_NO_FLOW
