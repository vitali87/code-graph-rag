"""Issue #2931: Protocol dispatch keeps a guess a guess and targets conformers.

A call resolved to a `typing.Protocol` stub was replaced by an edge to the
method on every class defining a method of that name, labelled `exact`. A
name-only guess (a dict literal's `.get(...)` landing on the stub) became
exact edges to unrelated classes and test doubles, and `cgr rename` rewrote
those `dict.get` calls without asking.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

FILES = {
    "pkg/__init__.py": "",
    "tests/__init__.py": "",
    "pkg/bccache.py": (
        "from __future__ import annotations\n\n"
        "import typing as t\n\n"
        "if t.TYPE_CHECKING:\n"
        "    import typing_extensions as te\n\n"
        "    class _Client(te.Protocol):\n"
        "        def get(self, key: str) -> bytes: ...\n\n"
        "        def set(self, key: str, value: bytes) -> None: ...\n\n\n"
        "class RemoteCache:\n"
        "    def __init__(self, client: _Client):\n"
        "        self.client = client\n\n"
        "    def load(self, key):\n"
        "        return self.client.get(key)\n"
    ),
    "pkg/utils.py": (
        "class LRUCache:\n"
        "    def get(self, key, default=None):\n"
        "        return default\n"
    ),
    "pkg/memcached.py": (
        "class Memcached:\n"
        "    def get(self, key):\n"
        '        return b""\n\n'
        "    def set(self, key, value):\n"
        "        return None\n"
    ),
    "pkg/lexer.py": (
        "def describe(token_type):\n"
        '    return {"name": "identifier"}.get(token_type, token_type)\n'
    ),
    "tests/test_cache.py": (
        "class MockClient:\n"
        "    def get(self, key):\n"
        '        return b""\n\n'
        "    def set(self, key, value):\n"
        "        return None\n"
    ),
    "pkg/single.py": (
        "from typing import Protocol\n\n\n"
        "class Sink(Protocol):\n"
        "    def emit(self, item) -> None: ...\n\n\n"
        "class FileSink:\n"
        "    def emit(self, item) -> None:\n"
        "        return None\n\n\n"
        "def flush(sink: Sink, item) -> None:\n"
        "    sink.emit(item)\n"
    ),
    "pkg/named.py": (
        "from typing import Protocol\n\n\n"
        "class StoreProtocol(Protocol):\n"
        "    def read(self, key): ...\n\n"
        "    def write(self, key, value): ...\n\n\n"
        "class Store:\n"
        "    def read(self, key):\n"
        "        return key\n\n\n"
        "def fetch(store: StoreProtocol, key):\n"
        "    return store.read(key)\n"
    ),
    "pkg/overloaded.py": (
        "from typing import Protocol, overload\n\n\n"
        "class Reader(Protocol):\n"
        "    @overload\n"
        "    def read(self, n: int) -> bytes: ...\n\n"
        "    @overload\n"
        "    def read(self, n: None = None) -> bytes: ...\n\n\n"
        "class DiskReader:\n"
        "    def read(self, n=None):\n"
        '        return b""\n\n\n'
        "def load(reader: Reader):\n"
        "    return reader.read(3)\n"
    ),
    "pkg/inherited.py": (
        "from typing import Protocol\n\n\n"
        "class Closer(Protocol):\n"
        "    def close(self) -> None: ...\n\n\n"
        "class Stream(Closer, Protocol):\n"
        "    def pull(self) -> bytes: ...\n\n\n"
        "class Pump:\n"
        "    def pull(self) -> bytes:\n"
        '        return b""\n\n\n'
        "class Socket:\n"
        "    def pull(self) -> bytes:\n"
        '        return b""\n\n'
        "    def close(self) -> None:\n"
        "        return None\n\n\n"
        "def drain(stream: Stream):\n"
        "    return stream.pull()\n"
    ),
}


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("proto") / "proto"
    for rel, text in FILES.items():
        _write(root, rel, text)
    return _index(root, MagicMock())


def _calls(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get(cs.KEY_RESOLUTION))
        for src, rel, dst, props in graph.edges
        if rel == cs.RelationshipType.CALLS and src == f"{prefix}{caller}"
    }


def test_a_guessed_stub_is_not_fanned_out_as_exact(graph: RecordedGraph) -> None:
    calls = _calls(graph, "pkg.lexer.describe")
    assert "pkg.utils.LRUCache.get" not in calls
    assert "tests.test_cache.MockClient.get" not in calls
    assert cs.EdgeResolution.EXACT.value not in calls.values()


def test_a_typed_call_dispatches_only_to_classes_that_conform(
    graph: RecordedGraph,
) -> None:
    # LRUCache defines `get` but not `set`, so it does not implement _Client.
    calls = _calls(graph, "pkg.bccache.RemoteCache.load")
    assert "pkg.utils.LRUCache.get" not in calls
    assert "pkg.memcached.Memcached.get" in calls
    assert "tests.test_cache.MockClient.get" in calls


def test_an_overloaded_stub_still_reaches_its_implementer(
    graph: RecordedGraph,
) -> None:
    # The two `@overload` stubs register as `read` and `read@<line>`; the
    # implementer defines `read` once and still conforms.
    assert "pkg.overloaded.DiskReader.read" in _calls(graph, "pkg.overloaded.load")


def test_an_inherited_protocol_method_is_required_too(graph: RecordedGraph) -> None:
    # `Stream` inherits `close` from `Closer`: `Pump` defines only `pull`, so
    # it does not implement `Stream`, and `Socket`, which defines both, does.
    calls = _calls(graph, "pkg.inherited.drain")
    assert "pkg.inherited.Pump.pull" not in calls
    assert "pkg.inherited.Socket.pull" in calls


def test_a_dispatch_to_several_conformers_is_not_exact(graph: RecordedGraph) -> None:
    calls = _calls(graph, "pkg.bccache.RemoteCache.load")
    assert calls["pkg.memcached.Memcached.get"] == cs.EdgeResolution.OVERLOAD.value
    assert calls["tests.test_cache.MockClient.get"] == cs.EdgeResolution.OVERLOAD.value


# Negative: what must not change.


def test_a_single_conformer_still_binds_exactly(graph: RecordedGraph) -> None:
    assert _calls(graph, "pkg.single.flush") == {
        "pkg.single.FileSink.emit": cs.EdgeResolution.EXACT.value
    }


def test_the_named_implementer_is_still_a_target(graph: RecordedGraph) -> None:
    # `Store` is StoreProtocol's implementer by the XxxProtocol -> Xxx naming
    # convention, though it defines `read` alone.
    assert "pkg.named.Store.read" in _calls(graph, "pkg.named.fetch")


def test_the_stub_itself_gets_no_edge(graph: RecordedGraph) -> None:
    calls = _calls(graph, "pkg.bccache.RemoteCache.load")
    assert not [qn for qn in calls if qn.startswith("pkg.bccache._Client")]


def test_the_layout_is_indexed(tmp_path: Path) -> None:
    # The fixture's files all parse: a typo in one would silently drop the
    # module and pass the absence assertions above for the wrong reason.
    root = tmp_path / "proto"
    for rel, text in FILES.items():
        _write(root, rel, text)
    graph = _index(root, MagicMock())
    modules = {
        qn.removeprefix(f"{graph.project}.")
        for qn, props in graph.nodes.items()
        if props[cs.KEY_LABEL] == cs.NodeLabel.MODULE.value
    }
    assert {
        "pkg.bccache",
        "pkg.utils",
        "pkg.memcached",
        "pkg.lexer",
        "tests.test_cache",
        "pkg.single",
        "pkg.named",
        "pkg.overloaded",
        "pkg.inherited",
    } <= modules
