# C# Phase 4: attribute-driven and IDisposable dead-code reachability roots.
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.types_defs import ResultRow

_METHOD = cs.NodeLabel.METHOD.value
_MODULE = cs.NodeLabel.MODULE.value
_CALLS = cs.RelationshipType.CALLS.value


class FakeIngestor:
    def __init__(self, nodes: list[ResultRow], rels: list[ResultRow]) -> None:
        self._nodes = nodes
        self._rels = rels

    def fetch_all(
        self, query: str, params: dict[str, str] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return self._nodes
        return self._rels


def _method(qn: str, name: str, decorators: list[str] | None = None) -> ResultRow:
    return {
        "label": _METHOD,
        "qualified_name": qn,
        "name": name,
        "path": "proj/Svc.cs",
        "start_line": 1,
        "end_line": 2,
        "decorators": decorators or [],
        "is_exported": False,
        "overrides_external": False,
    }


def _dead(ingestor: FakeIngestor) -> set[str]:
    config = default_dead_code_config(include_tests=True, include_classes=True)
    return {
        row["qualified_name"] for row in collect_dead_code(ingestor, "proj", config)
    }


def test_attribute_and_dispose_methods_are_roots() -> None:
    # None of these has an incoming CALLS edge; only the plain private helper
    # is genuinely dead. [Fact]/[HttpGet] and Dispose are framework/runtime
    # roots on a .cs file, so they must NOT be reported dead.
    nodes = [
        _method("proj.Svc.T1", "T1", ["Fact"]),
        _method("proj.Svc.Get", "Get", ['Route("api")']),
        # A bracketed form must normalize too (robust to the capture shape).
        _method("proj.Svc.Bracketed", "Bracketed", ["[Theory]"]),
        _method("proj.Svc.Dispose", "Dispose"),
        _method("proj.Svc.Helper", "Helper"),
    ]
    dead = _dead(FakeIngestor(nodes, []))

    assert "proj.Svc.Helper" in dead
    assert "proj.Svc.T1" not in dead
    assert "proj.Svc.Get" not in dead
    assert "proj.Svc.Bracketed" not in dead
    assert "proj.Svc.Dispose" not in dead


def test_operator_and_finalizer_are_roots() -> None:
    # An operator overload is invoked by operator syntax (`a + b`) and a
    # finalizer (`~Svc`) by the GC, never a named call the graph sees, so
    # neither may be reported dead despite having no incoming CALLS.
    nodes = [
        _method("proj.Svc.operator_+(int, int)", "operator_+"),
        _method("proj.Svc.~Svc", "~Svc"),
        _method("proj.Svc.Helper", "Helper"),
    ]
    dead = _dead(FakeIngestor(nodes, []))

    assert "proj.Svc.Helper" in dead
    assert "proj.Svc.operator_+(int, int)" not in dead
    assert "proj.Svc.~Svc" not in dead
