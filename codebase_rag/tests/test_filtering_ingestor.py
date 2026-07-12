from __future__ import annotations

from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.services import FilteringIngestor

RT = cs.RelationshipType
NL = cs.NodeLabel
QN = cs.KEY_QUALIFIED_NAME


def _wrap(tokens: list[str]) -> tuple[FilteringIngestor, MagicMock]:
    inner = MagicMock()
    return FilteringIngestor(inner, resolve_capture(tokens)), inner


def test_enabled_rel_passes_through() -> None:
    ing, inner = _wrap([])
    ing.ensure_relationship_batch(
        (NL.FUNCTION, QN, "m.a"), RT.CALLS, (NL.FUNCTION, QN, "m.b")
    )
    inner.ensure_relationship_batch.assert_called_once()


def test_call_shape_preserved_without_properties() -> None:
    # (H) Callers that omit properties must not gain a spurious properties=None;
    # (H) downstream tests compare the exact call() and read call.kwargs.
    ing, inner = _wrap([])
    ing.ensure_relationship_batch(
        (NL.CLASS, QN, "m.C"), RT.DEFINES_METHOD, (NL.METHOD, QN, "m.C.f")
    )
    c = inner.ensure_relationship_batch.call_args
    assert c.kwargs == {}
    assert len(c.args) == 3


def test_call_shape_forwards_properties_as_kwarg() -> None:
    ing, inner = _wrap([])
    props = {"version_spec": ">=2.0.0"}
    ing.ensure_relationship_batch(
        (NL.PROJECT, cs.KEY_NAME, "p"),
        RT.DEPENDS_ON_EXTERNAL,
        (NL.EXTERNAL_PACKAGE, cs.KEY_NAME, "flask"),
        properties=props,
    )
    c = inner.ensure_relationship_batch.call_args
    assert c.kwargs.get("properties") == props


def test_call_shape_forwards_positional_properties() -> None:
    # (H) INHERITS passes base_index props positionally; the wrapper must keep it
    # (H) positional (tests assert len(call.args) == 4).
    ing, inner = _wrap([])
    ing.ensure_relationship_batch(
        (NL.CLASS, QN, "m.D"),
        RT.INHERITS,
        (NL.CLASS, QN, "m.B"),
        {"base_index": 0},
    )
    c = inner.ensure_relationship_batch.call_args
    assert len(c.args) == 4
    assert c.args[3] == {"base_index": 0}
    assert c.kwargs == {}


def test_disabled_rel_dropped() -> None:
    ing, inner = _wrap([])  # (H) io off by default
    ing.ensure_relationship_batch(
        (NL.FUNCTION, QN, "m.a"), RT.WRITES_TO, (NL.RESOURCE, QN, "resource::FILE::x")
    )
    inner.ensure_relationship_batch.assert_not_called()


def test_disabled_node_dropped_enabled_kept() -> None:
    ing, inner = _wrap([])  # (H) io off → Resource off
    ing.ensure_node_batch(NL.RESOURCE, {QN: "resource::FILE::x"})
    ing.ensure_node_batch(NL.FUNCTION, {QN: "m.a"})
    labels = [c.args[0] for c in inner.ensure_node_batch.call_args_list]
    assert NL.FUNCTION in labels
    assert NL.RESOURCE not in labels


def test_io_enabled_lets_resource_through() -> None:
    ing, inner = _wrap(["io"])
    ing.ensure_node_batch(NL.RESOURCE, {QN: "resource::FILE::x"})
    ing.ensure_relationship_batch(
        (NL.FUNCTION, QN, "m.a"), RT.WRITES_TO, (NL.RESOURCE, QN, "resource::FILE::x")
    )
    inner.ensure_node_batch.assert_called_once()
    inner.ensure_relationship_batch.assert_called_once()


class _QueryableInner:
    # (H) A concrete class (not MagicMock) so isinstance(_, QueryProtocol) holds,
    # (H) matching how the real MemgraphIngestor is detected at runtime.
    def __init__(self) -> None:
        self.ensure_node_batch = MagicMock()
        self.ensure_relationship_batch = MagicMock()
        self.flush_all = MagicMock()
        self.fetch_all = MagicMock(return_value=[])
        self.execute_write = MagicMock()


def test_passthrough_methods_forwarded() -> None:
    inner = _QueryableInner()
    ing = FilteringIngestor(inner, resolve_capture([]))
    ing.flush_all()
    inner.flush_all.assert_called_once()
    ing.fetch_all("MATCH (n) RETURN n")
    inner.fetch_all.assert_called_once()
    ing.execute_write("CREATE (n)")
    inner.execute_write.assert_called_once()
