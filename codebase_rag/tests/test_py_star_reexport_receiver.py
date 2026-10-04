"""Issue #2928: a receiver built from a star-re-exported class is typed.

`client = pkg.Client()` and `with pkg.Client() as client` typed `client`
when `pkg/__init__.py` re-exported `Client` by name (#2558), but not when it
re-exported it with `from ._client import *` (httpx does): the re-export
walk only looked the name up directly, and a star import is one wildcard
entry, so every method call on `client` got no edge.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

CLIENT = """\
__all__ = ["Client"]


class BaseClient:
    def build_request(self, method, url):
        return (method, url)


class Client(BaseClient):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def send(self, request):
        return request


class _Hidden:
    def send(self, request):
        return request
"""

APP = """\
import explicit
import star
import chain


def via_explicit():
    client = explicit.Client()
    client.send(client.build_request("GET", "/"))


def via_star():
    client = star.Client()
    client.send(client.build_request("GET", "/"))


def via_star_with():
    with star.Client() as client:
        client.send(client.build_request("GET", "/"))


def via_star_chain():
    client = chain.Client()
    client.send(client.build_request("GET", "/"))


def via_private_name():
    hidden = star._Hidden()
    hidden.send(1)
"""


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("stardemo") / "stardemo"
    for package in ("explicit", "star"):
        _write(root, f"{package}/_client.py", CLIENT)
    _write(root, "explicit/__init__.py", "from ._client import Client\n")
    _write(root, "star/__init__.py", "from ._client import *\n")
    _write(root, "chain/__init__.py", "from .sub import *\n")
    _write(root, "chain/sub/__init__.py", "from ._impl import *\n")
    _write(root, "chain/sub/_impl.py", CLIENT)
    _write(root, "app.py", APP)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> set[str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix)
        for src, rel, dst, _props in graph.edges
        if rel == cs.RelationshipType.CALLS and src == f"{prefix}app.{caller}"
    }


@pytest.mark.parametrize(
    ("caller", "package"),
    [
        ("via_star", "star._client"),
        ("via_star_with", "star._client"),
        ("via_star_chain", "chain.sub._impl"),
    ],
    ids=["assignment", "with", "two-level-chain"],
)
def test_a_star_re_exported_receiver_binds_its_methods(
    graph: RecordedGraph, caller: str, package: str
) -> None:
    callees = _callees(graph, caller)
    assert f"{package}.Client.send" in callees
    assert f"{package}.BaseClient.build_request" in callees


# Negative: what must not change.


def test_a_by_name_re_export_still_binds(graph: RecordedGraph) -> None:
    callees = _callees(graph, "via_explicit")
    assert "explicit._client.Client.send" in callees
    assert "explicit._client.BaseClient.build_request" in callees


def test_a_private_name_is_not_taken_through_a_star(graph: RecordedGraph) -> None:
    # `from ._client import *` binds no `_Hidden`, so `star._Hidden` names
    # nothing and its receiver stays untyped.
    assert "star._client._Hidden.send" not in _callees(graph, "via_private_name")
