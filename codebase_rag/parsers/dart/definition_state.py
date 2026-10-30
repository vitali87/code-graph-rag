"""What the definition pass records for Dart beside the shared facts."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DartDefinitionState:
    """Built by `DefinitionProcessor` and held by reference by the method
    pass that fills it and the Dart class collaborator that judges it: the
    container is refilled in place, never replaced."""

    # {class_qn: [(method_qn, method_name)]} for Dart @override methods;
    # whether they override an EXTERNAL base is only decidable once every
    # class is registered, so resolve_deferred_inherits consumes this.
    annotated_overrides: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
