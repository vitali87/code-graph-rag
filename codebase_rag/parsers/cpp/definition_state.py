"""What the definition pass records for C/C++ beside the shared facts."""

from __future__ import annotations

from dataclasses import dataclass, field

from ...types_defs import CppDefinitionSpan


@dataclass(frozen=True, slots=True)
class CppDefinitionState:
    """Built by `DefinitionProcessor` and held by reference by the C/C++
    collaborators that fill it, the hybrid frontend and rehydration: each
    container is refilled in place, never replaced."""

    # {rel path: [full line spans]} of every C/C++ function/method the
    # tree-sitter pass ingested; the hybrid C++ frontend's macro-use
    # CALLS resolve against these after Pass 2 (see CppDefinitionSpan).
    definition_spans: dict[str, list[CppDefinitionSpan]] = field(default_factory=dict)
    # C++20 module interfaces declared this run (export module X).
    module_interfaces: set[str] = field(default_factory=set)
    # The subset of the above that THIS run's parsing registered, as
    # opposed to entries read back from the graph. Rehydration rebuilds
    # the graph-derived part and must not drop these: a just-parsed
    # interface's node write is still in the ingestor's buffer, so it is
    # absent from the rows rehydration fetches.
    interfaces_parsed_this_run: set[str] = field(default_factory=set)
