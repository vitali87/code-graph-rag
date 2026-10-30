from typing import NamedTuple

from . import constants as tc

# One directed call edge, both endpoints named the way TraceEval names them.
TraceEdge = tuple[str, str]


class Program(NamedTuple):
    language: tc.Language
    program_id: str
    root: str


class CgrDefinition(NamedTuple):
    label: str
    qualified_name: str
    path: str
    start_line: int | None


class CgrCall(NamedTuple):
    caller: str
    callee: str
    resolution: str


class ProgramResult(NamedTuple):
    language: tc.Language
    program_id: str
    truth: frozenset[TraceEdge]
    predicted: frozenset[TraceEdge]
    # The subset of `predicted` that cgr bound `exact` (scope, import, type
    # or signature), so precision can be read per resolution tier.
    predicted_exact: frozenset[TraceEdge]
    # Both sides again under the static protocol (protocol.py).
    truth_static: frozenset[TraceEdge]
    predicted_static: frozenset[TraceEdge]
    error: str
