"""What the Go interface link shares with call resolution (issue #2483)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .builds import Expr, GoBuilds
from .interfaces import GoMethodSpec


@dataclass
class GoLinkState:
    """A view of the definition pass's `GoDefinitionState`, built by its
    `link_state`: each container is that state's own, held by reference, and
    the link refills them in place before any call is resolved.

    `guessed_implementations` are the (interface, implementer) pairs matched
    by name, `methods` each method's signature, `builtin_shadows` the
    predeclared names each package declares, `variant_methods` the
    declarations a type's build variants hold for it, `declaration_variants`
    each interface's build variants, `pair_builds` the builds holding each
    pair, and `builds` each file's build constraint."""

    guessed_implementations: set[tuple[str, str]] = field(default_factory=set)
    methods: dict[str, GoMethodSpec] = field(default_factory=dict)
    builtin_shadows: dict[str, frozenset[str]] = field(default_factory=dict)
    variant_methods: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    declaration_variants: dict[str, tuple[str, ...]] = field(default_factory=dict)
    pair_builds: dict[tuple[str, str], Expr] = field(default_factory=dict)
    builds: GoBuilds | None = None
