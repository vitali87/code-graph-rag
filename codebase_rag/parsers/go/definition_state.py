"""What the definition pass records for Go beside the shared facts."""

from __future__ import annotations

from dataclasses import dataclass, field

from .builds import Expr, GoBuilds
from .interfaces import GoInterfaceDecl, GoMethodSpec
from .link_state import GoLinkState


@dataclass(frozen=True, slots=True)
class GoDefinitionState:
    """Built by `DefinitionProcessor` and held by reference by the Go
    collaborators that fill it, the go/types joins, rehydration and, through
    `link_state`, call resolution: each container is refilled in place,
    never replaced."""

    # Each Go file's build constraint, for telling a name each build
    # declares again (`i_linux.go`, `i_windows.go`) from a redeclaration
    # (shared with the call resolver and type inference).
    builds: GoBuilds
    # (rel_file, type_start_line, type_start_col) -> (class qn, node label)
    # for every ingested Go type. Go's type_spec start_point IS the name
    # token, so the frontend's pair positions join here directly (no alias).
    type_locations: dict[tuple[str, int, int], tuple[str, str]] = field(
        default_factory=dict
    )
    # Go interfaces as method sets (issue #2483): each Go method's
    # parameter count and package, each interface's embedded types as
    # written, and the interfaces this run parsed, from which implicit
    # satisfaction is computed once every type is registered.
    methods: dict[str, GoMethodSpec] = field(default_factory=dict)
    interfaces: dict[str, GoInterfaceDecl] = field(default_factory=dict)
    interfaces_parsed: dict[str, str] = field(default_factory=dict)
    # The Go pairs in interface_implementers by source, replaced each
    # pass: go/types proofs, and name-and-arity matches (whose edges are
    # labelled heuristic, and which the resolver reads to say so).
    proven_pairs: set[tuple[str, str]] = field(default_factory=set)
    guessed_pairs: set[tuple[str, str]] = field(default_factory=set)
    # Go package (`dir:clause`, or its test files' scope) -> the
    # predeclared type names (`int`, `error`) it declares itself, refilled
    # by each link (shared with the call resolver): there the name is the
    # package's own type.
    builtin_shadows: dict[str, frozenset[str]] = field(default_factory=dict)
    # Go type -> method name -> the declarations its build variants hold
    # in files its build compiles too, refilled by each link (shared with
    # the call resolver).
    variant_methods: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # (Go interface, implementer) -> the builds holding the pair (shared
    # with the call resolver).
    pair_builds: dict[tuple[str, str], Expr] = field(default_factory=dict)

    def link_state(
        self, declaration_variants: dict[str, tuple[str, ...]]
    ) -> GoLinkState:
        """The containers call resolution reads, by reference: the link
        refills them in place."""
        return GoLinkState(
            guessed_implementations=self.guessed_pairs,
            methods=self.methods,
            builtin_shadows=self.builtin_shadows,
            variant_methods=self.variant_methods,
            declaration_variants=declaration_variants,
            pair_builds=self.pair_builds,
            builds=self.builds,
        )
