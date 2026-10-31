"""The call-edge reduction every payload oracle shares.

Pinned on a synthetic payload, so the Scala `covered` path and the C# class
callables are exercised on hosts with neither scala-cli nor dotnet.
"""

from __future__ import annotations

from codebase_rag import constants as cs
from evals.oracles._common import FUNCTIONS_AND_METHODS, payload_call_edges
from evals.types_defs import OraclePayload

_FUNCTION = cs.NodeLabel.FUNCTION.value
_CLASS = cs.NodeLabel.CLASS.value


def _payload(covered: list[str] | None = None) -> OraclePayload:
    payload: OraclePayload = {
        "nodes": [
            {"kind": _FUNCTION, "file": "a.src", "line": 1, "name": "helper"},
            {"kind": _CLASS, "file": "a.src", "line": 5, "name": "Widget"},
        ],
        "edges": [],
        "name_edges": [],
        "calls": [
            {"file": "a.src", "name": "helper"},
            {"file": "a.src", "name": "Widget"},
            {"file": "a.src", "name": "print"},
            {"file": "b.src", "name": "helper"},
            {"file": "node_modules/dep.src", "name": "helper"},
        ],
    }
    if covered is not None:
        payload["covered"] = covered
    return payload


def test_only_first_party_callees_outside_ignored_dirs_count() -> None:
    oracle = payload_call_edges(_payload())

    assert oracle.declared == frozenset({"helper"})
    assert oracle.edges == {("a.src", "helper"), ("b.src", "helper")}
    assert oracle.covered is None


def test_a_language_can_widen_the_callable_kinds() -> None:
    # C# admits class names: `new T()` with no explicit ctor names the type.
    oracle = payload_call_edges(_payload(), FUNCTIONS_AND_METHODS | {_CLASS})

    assert oracle.declared == frozenset({"helper", "Widget"})
    assert ("a.src", "Widget") in oracle.edges


def test_covered_holds_grading_to_cleanly_parsed_files() -> None:
    oracle = payload_call_edges(
        _payload(covered=["a.src", "node_modules/dep.src"]), use_covered=True
    )

    assert oracle.covered == frozenset({"a.src"})
    assert oracle.edges == {("a.src", "helper")}
