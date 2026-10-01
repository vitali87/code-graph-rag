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
"""

CLIENT = f"""import os

import requests


def push_plain():
    requests.post("{URL}", json=os.getenv("CLIENT_TOKEN"))
"""

SERVER = """app = object()


@app.post("/items")
def create_item(item):
    print(item)
"""


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


@pytest.fixture
def notify(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> MemgraphIngestor:
    _index(memgraph_ingestor, _project(tmp_path, "notify", "app.py", NOTIFY), "notify")
    return memgraph_ingestor


@pytest.fixture
def services(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> MemgraphIngestor:
    _index(memgraph_ingestor, _project(tmp_path, "server", "api.py", SERVER), "server")
    _index(
        memgraph_ingestor, _project(tmp_path, "client", "client.py", CLIENT), "client"
    )
    return memgraph_ingestor


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


def test_a_client_function_reaches_the_handler_of_another_service(
    services: MemgraphIngestor,
) -> None:
    result = flow_reachability_verdict(
        services.fetch_all,
        "client",
        "client.client.push_plain",
        "server.api.create_item",
    )

    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (
        "client.client.push_plain",
        NETWORK,
        "server.api.create_item",
    )
    assert result.remote_hops == ((NETWORK, "server.api.create_item"),)


def test_a_client_env_value_reaches_the_handler_of_another_service(
    services: MemgraphIngestor,
) -> None:
    result = flow_reachability_verdict(
        services.fetch_all, "client", CLIENT_TOKEN, "server.api.create_item"
    )

    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (CLIENT_TOKEN, NETWORK, "server.api.create_item")


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
