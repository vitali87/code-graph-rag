"""What the definition pass records for C# beside the shared facts."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CSharpDefinitionState:
    """Built by `DefinitionProcessor` and held by reference by the C#
    collaborators that fill it, the override walk, the Roslyn joins and
    rehydration: each container is refilled in place, never replaced."""

    # Every C# method qn, and the subset carrying the `override` modifier.
    # C# base-CLASS overrides require `override` (a `new`/implicit-hide
    # member is not an override), so the shared override walk emits a
    # class-parent match only for C# methods in the override subset;
    # interface implementations need no modifier and are unaffected.
    methods: set[str] = field(default_factory=set)
    override_methods: set[str] = field(default_factory=set)
    # C# partial parts grouped by their namespace-qualified key, the
    # index behind `facts.csharp_partial_groups`.
    partial_index: dict[str, list[str]] = field(default_factory=dict)
    # (rel_file, type_start_line) -> class qn for every ingested C# type,
    # the reverse of the Roslyn fact keys, so partial declaration groups
    # join back to the Pass-2 Class nodes.
    type_locations: dict[tuple[str, int], str] = field(default_factory=dict)
