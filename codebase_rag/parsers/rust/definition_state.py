"""What the definition pass records for Rust beside the shared facts."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RustDefinitionState:
    """Built by `DefinitionProcessor` and held by reference by the Rust class
    collaborator that fills it, the override walk and the file prune: each
    container is refilled in place, never replaced."""

    # {Rust impl target qn: module of the file whose `impl Trait for T`
    # recorded its pair}. A primitive or foreign target (`u64`) has no
    # class node, so `class_owner_module` never names that file, and the
    # pair outlived the impl on a reused updater (#2403).
    impl_owner_module: dict[str, str] = field(default_factory=dict)
    # method qn -> the first-party trait qn its own impl block names. The
    # override pass cannot re-derive this: two traits declaring one method
    # name leave the method qns indistinguishable (issue #1076).
    impl_method_traits: dict[str, str] = field(default_factory=dict)
    # Methods of an inherent `impl Type` block. Rust has no
    # inheritance, so these override nothing and must never reach the
    # ancestry walk (issue #1078).
    inherent_impl_methods: set[str] = field(default_factory=set)
