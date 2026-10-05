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


# Binding order and `__all__` (bot review on PR #2989): Python keeps a name's
# last binding, star or named, and a star binds only what its module's
# `__all__` lists when it has one.
KLASS = "class Client:\n    def send(self, request):\n        return request\n"
HIDDEN_KLASS = '__all__ = ["Other"]\n\n\nclass Other:\n    pass\n\n\n' + KLASS
LISTED = '__all__ = ["Client"]\n\n\n' + KLASS

ORDER_APP = """\
import later_star
import star_after_named
import named_after_star
import excluded
import excluded_only
import listed


def via_later_star():
    client = later_star.Client()
    client.send(1)


def via_star_after_named():
    client = star_after_named.Client()
    client.send(1)


def via_named_after_star():
    client = named_after_star.Client()
    client.send(1)


def via_excluded():
    client = excluded.Client()
    client.send(1)


def via_excluded_only():
    client = excluded_only.Client()
    client.send(1)


def via_listed():
    client = listed.Client()
    client.send(1)
"""


@pytest.fixture(scope="module")
def order_graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("starorder") / "starorder"
    for package in ("later_star", "star_after_named", "named_after_star", "excluded"):
        _write(root, f"{package}/_a.py", KLASS)
        _write(root, f"{package}/_b.py", KLASS)
    _write(root, "later_star/__init__.py", "from ._a import *\nfrom ._b import *\n")
    _write(
        root,
        "star_after_named/__init__.py",
        "from ._a import Client\nfrom ._b import *\n",
    )
    _write(
        root,
        "named_after_star/__init__.py",
        "from ._b import *\nfrom ._a import Client\n",
    )
    _write(root, "excluded/_a.py", HIDDEN_KLASS)
    _write(root, "excluded/__init__.py", "from ._b import *\nfrom ._a import *\n")
    _write(root, "excluded_only/_a.py", HIDDEN_KLASS)
    _write(root, "excluded_only/__init__.py", "from ._a import *\n")
    _write(root, "listed/_impl.py", LISTED)
    _write(root, "listed/__init__.py", "from ._impl import *\n")
    _write(root, "app.py", ORDER_APP)
    return _index(root, MagicMock())


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("via_later_star", "later_star._b.Client.send"),
        ("via_star_after_named", "star_after_named._b.Client.send"),
        ("via_named_after_star", "named_after_star._a.Client.send"),
        ("via_excluded", "excluded._b.Client.send"),
        ("via_listed", "listed._impl.Client.send"),
    ],
    ids=[
        "the-later-star-wins",
        "a-star-after-a-named-import-wins",
        "a-named-import-after-a-star-wins",
        "a-star-whose-all-leaves-it-out-binds-nothing",
        "a-star-whose-all-lists-it-binds-it",
    ],
)
def test_the_binding_python_keeps_types_the_receiver(
    order_graph: RecordedGraph, caller: str, callee: str
) -> None:
    callees = _callees(order_graph, caller)
    assert callees == {callee}, callees


def test_a_star_whose_all_leaves_the_name_out_binds_nothing(
    order_graph: RecordedGraph,
) -> None:
    # `excluded_only/__init__.py` stars `_a`, whose `__all__` is ["Other"],
    # so `excluded_only.Client` is unbound and the receiver stays untyped.
    assert "excluded_only._a.Client.send" not in _callees(
        order_graph, "via_excluded_only"
    )


# Negative: what must not change.


def test_a_by_name_re_export_still_binds(graph: RecordedGraph) -> None:
    callees = _callees(graph, "via_explicit")
    assert "explicit._client.Client.send" in callees
    assert "explicit._client.BaseClient.build_request" in callees


def test_a_private_name_is_not_taken_through_a_star(graph: RecordedGraph) -> None:
    # `from ._client import *` binds no `_Hidden`, so `star._Hidden` names
    # nothing and its receiver stays untyped.
    assert "star._client._Hidden.send" not in _callees(graph, "via_private_name")
