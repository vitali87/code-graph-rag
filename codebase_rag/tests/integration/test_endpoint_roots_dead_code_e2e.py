# Real-Memgraph check of issue #2664: the parser marks a public FastAPI
# handler `is_exported`, and that rule rooted it even with endpoint roots off.
# Indexes the issue's two services with the `io` capture and runs the same
# collection `cgr dead-code --no-endpoint-roots` does.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_USER_SERVICE = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/users/{user_id}")
def get_user(user_id: int) -> dict:
    return {"id": user_id}


@app.post("/users")
def create_user(payload: dict) -> dict:
    return payload


@app.delete("/users/{user_id}")
def _delete_user(user_id: int) -> dict:
    return {"deleted": user_id}
"""
_BILLING_SERVICE = """\
import requests


def bill_user(user_id: int) -> dict:
    return requests.get("http://user-service:8000/users/42").json()
"""


def _index(ingestor: MemgraphIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()
    ingestor.flush_all()


def _services(ingestor: MemgraphIngestor, root: Path) -> None:
    for name, source, module in (
        ("user-service", _USER_SERVICE, "main.py"),
        ("billing-service", _BILLING_SERVICE, "bill.py"),
    ):
        app = root / name / "app"
        app.mkdir(parents=True)
        (app / "__init__.py").touch()
        (app / module).write_text(source, encoding="utf-8")
    _index(ingestor, root / "user-service")
    _index(ingestor, root / "billing-service")


def _dead_names(ingestor: MemgraphIngestor, endpoint_roots: bool) -> set[str]:
    config = default_dead_code_config(include_tests=True, include_classes=False)
    rows = collect_dead_code(
        ingestor, "user-service", config._replace(endpoint_roots=endpoint_roots)
    )
    return {str(row[cs.KEY_NAME]) for row in rows}


def test_an_uncalled_public_handler_is_reported_with_endpoint_roots_off(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _services(memgraph_ingestor, tmp_path)

    assert _dead_names(memgraph_ingestor, endpoint_roots=False) == {
        "create_user",
        "_delete_user",
    }


# Negative: the default still roots every handler.


def test_endpoint_roots_on_reports_no_handler(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _services(memgraph_ingestor, tmp_path)

    assert _dead_names(memgraph_ingestor, endpoint_roots=True) == set()
