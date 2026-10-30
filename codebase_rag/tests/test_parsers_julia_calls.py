"""`JuliaCallResolution`: the name a Julia definition's body calls are
attributed under (its node has no `name` field), the calls an anonymous
arrow leaves to its enclosing scope, and a definition head that is no call."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.julia.calls import JuliaCallResolution
from codebase_rag.parsers.language_calls import CallName
from codebase_rag.tests.collaborator_support import edges, index, nodes_of, parse
from evals.recorded_graph import RecordingIngestor

_JULIA = cs.SupportedLanguage.JULIA
_MAIN = (
    "ys = map(x -> helper(x), [1])\n"
    "helper(x) = x\n"
    "function outer(x)\n    inner(y) = helper(y)\n    inner(x)\nend\n"
    "struct P\n  v\n  P() = new(helper(1))\nend\n"
)


@pytest.fixture
def julia_calls(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> JuliaCallResolution:
    updater = index(temp_repo / "jl", mock_ingestor, {"main.jl": _MAIN})
    strategy = updater.factory.call_processor.calls_for(_JULIA)
    assert isinstance(strategy, JuliaCallResolution)
    return strategy


def test_an_anonymous_arrows_calls_belong_to_the_enclosing_scope(
    julia_calls: JuliaCallResolution, mock_ingestor: RecordingIngestor
) -> None:
    assert ("jl.main", "jl.main.helper") in edges(
        mock_ingestor, cs.RelationshipType.CALLS
    )


def test_a_nested_definition_keeps_its_own_calls(
    julia_calls: JuliaCallResolution, mock_ingestor: RecordingIngestor
) -> None:
    calls = edges(mock_ingestor, cs.RelationshipType.CALLS)
    assert ("jl.main.outer", "jl.main.inner") in calls
    assert ("jl.main.inner", "jl.main.helper") in calls
    assert ("jl.main.outer", "jl.main.helper") not in calls


def test_an_inner_constructors_body_is_its_own_caller(
    julia_calls: JuliaCallResolution, mock_ingestor: RecordingIngestor
) -> None:
    assert ("jl.main.P.P", "jl.main.helper") in edges(
        mock_ingestor, cs.RelationshipType.CALLS
    )


def test_a_definition_head_is_no_call(julia_calls: JuliaCallResolution) -> None:
    root = parse(_JULIA, "helper(x) = run(x)\n")
    head, body_call = nodes_of(root, "call_expression")
    assert julia_calls.call_name(head) == CallName(True, None)
    assert julia_calls.call_name(body_call) == CallName(True, "run")


def test_callers_are_named_from_the_head_or_the_arrows_binding(
    julia_calls: JuliaCallResolution,
) -> None:
    root = parse(_JULIA, "helper(x) = x\ng = y -> y\nmap(z -> z, [1])\n")
    short = nodes_of(root, "assignment")[0]
    arrow, anonymous = nodes_of(root, "arrow_function_expression")
    assert julia_calls.caller_name(short, "jl.main") == "helper"
    assert julia_calls.caller_name(arrow, "jl.main") == "g"
    # An anonymous arrow gets no caller pass of its own.
    assert julia_calls.caller_name(anonymous, "jl.main") is None
    assert julia_calls.attributable([short, arrow, anonymous], "jl.main") == [
        short,
        arrow,
    ]
