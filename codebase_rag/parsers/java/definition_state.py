"""What the definition pass records for Java beside the shared facts."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class JavaDefinitionState:
    """Built by `DefinitionProcessor` and held by reference by the Java class
    and function collaborators, which both record into it: the container is
    refilled in place, never replaced."""

    # Java anonymous-class override methods: (anon_method_qn, method_name,
    # base_type_name, module_qn). An anon class `new Base(){ @Override m(){} }`
    # is not modelled as a subclass, so its overrides register under the
    # enclosing class with no OVERRIDES edge and look dead. Recorded at method
    # ingestion, resolved to OVERRIDES edges once every base type is registered.
    anon_overrides: list[tuple[str, str, str, str]] = field(default_factory=list)
