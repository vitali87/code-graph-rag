"""The construction a call bound to a class runs.

A call whose callee resolves to a class constructs it: the call takes an
INSTANTIATES edge to each class variant, and a CALLS edge to the
constructors the language runs (Python's `__init__`, JS/TS's `constructor`,
every declared constructor of a Java, C#, C++ or Dart class, a PHP class's
own or inherited `__construct`). Julia dispatches the call in the function
namespace instead, where a same-named function shadows the type. The
`CallConstruction` the call pass builds writes those edges and hands back
the constructor the call's own CALLS edge goes to, where there is one; it
calls back through `ConstructionHost` for the edge site and the verdict of
the call node being scanned."""

from typing import Protocol

from .. import constants as cs
from .call_host import VerdictHost
from .call_resolver import CallResolver
from .definition_facts import DefinitionFacts
from .edge_site import EdgeSiteHost
from .language_calls import CallScan


class ConstructionHost(EdgeSiteHost, VerdictHost, Protocol):
    """What the construction path reads on the call pass that builds it: the
    verdict, which a construction with one target carries. Beside the edge
    site, whose resolution a construction with several targets marks
    overloaded."""


class CallConstruction:
    """Writes the edges a call bound to a class makes.

    Built and owned by `CallProcessor`, whose resolver and definition facts
    it reads."""

    __slots__ = ("_host", "_resolver", "_facts")

    def __init__(
        self, host: ConstructionHost, resolver: CallResolver, facts: DefinitionFacts
    ) -> None:
        self._host = host
        self._resolver = resolver
        self._facts = facts

    def redirect_construction(
        self,
        ctx: CallScan,
        callee_qn: str,
        ctor_rel: cs.RelationshipType = cs.RelationshipType.CALLS,
    ) -> tuple[str, str] | None:
        """Write a construction's INSTANTIATES and constructor edges; the
        constructor the call's own edge goes to, or None when they are
        written."""
        registry = self._resolver.function_registry
        host = self._host
        class_variants = registry.variants(callee_qn)
        # Julia call syntax dispatches in the FUNCTION namespace:
        # a same-named free function wins over the type (no
        # INSTANTIATES) and a macro is not a call target.
        julia_fn_variants: list[tuple[cs.NodeLabel, str]] = []
        if ctx.calls.functions_shadow_types:
            julia_fn_variants = [
                (node_type, variant)
                for variant in class_variants
                if (node_type := registry.get(variant))
                in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
                and variant not in self._facts.macro_qns
            ]
        host._resolution = (
            cs.EdgeResolution.OVERLOAD if len(class_variants) > 1 else host.verdict
        )
        for class_variant in class_variants:
            # A duplicate-suffixed variant may be a DIFFERENT kind
            # of node (a merged TS namespace registers as a class,
            # a colliding function as a Function); only class-typed
            # variants are instantiable, and a mismatched label is
            # a phantom the database drops (issue #652).
            variant_type = registry.get(class_variant)
            if variant_type is not None and variant_type != cs.NodeLabel.CLASS:
                ctx.calls.emit_binding_twin(
                    ctx.caller_spec, class_variant, variant_type, ctx.ensure_rel
                )
                continue
            if julia_fn_variants:
                # A shadowing function owns the call: no instantiation.
                continue
            ctx.ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.INSTANTIATES,
                (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_variant),
            )
        if ctx.calls.functions_shadow_types:
            self._emit_julia_construction_calls(ctx, callee_qn, julia_fn_variants)
            return None
        if ctx.calls.runs_declared_constructors:
            # A Java/C#/C++/Dart constructor is a method named like its
            # class (`Foo.Foo`), not `__init__`; `new Foo(...)` / `Foo(...)`
            # runs one, so redirect a CALLS edge to every declared
            # constructor (overload selection unneeded for reachability);
            # a Java `Foo::new` runs none yet and passes REFERENCES.
            # C#, C++, and Dart default constructors use the same
            # class-simple-name convention, so java_constructor_targets
            # selects them too (a Dart NAMED constructor is invoked by its
            # own name and resolves as an ordinary method). C++ additionally
            # redirects to the destructor: the object's `~X` runs at end of
            # lifetime with no call node of its own. sorted(): the target
            # label is a hash-randomized StrEnum, so sort for determinism.
            self._emit_declared_ctor_calls(ctx, callee_qn, class_variants, ctor_rel)
            return None
        if ctx.calls.inherits_constructors:
            # A PHP `new X(...)` runs `__construct`, X's own or, when X
            # declares none, the one it inherits (issue #2466).
            return self._resolver.php.php_constructor(callee_qn)
        # Redirect the CALLS edge to the constructor member the language
        # names (Python's `__init__`, JS/TS's `constructor`) so an
        # explicitly-declared constructor is reachable.
        init_qn = f"{callee_qn}{cs.SEPARATOR_DOT}{ctx.calls.constructor_member}"
        if init_qn not in registry:
            return None
        return (cs.NodeLabel.METHOD, init_qn)

    def _emit_julia_construction_calls(
        self,
        ctx: CallScan,
        callee_qn: str,
        fn_variants: list[tuple[cs.NodeLabel, str]],
    ) -> None:
        host = self._host
        registry = self._resolver.function_registry
        # A shadowing free function wins, else the inner same-name
        # constructor (like Java/C#). The resolution describes the
        # FILTERED target set (#1882).
        if fn_variants:
            host._resolution = (
                cs.EdgeResolution.OVERLOAD if len(fn_variants) > 1 else host.verdict
            )
            for node_type, variant in sorted(fn_variants):
                ctx.ensure_rel(
                    ctx.caller_spec,
                    cs.RelationshipType.CALLS,
                    (node_type, cs.KEY_QUALIFIED_NAME, variant),
                )
            return
        ctor_edges = [
            (ctor_type, variant)
            for ctor_type, ctor_qn in sorted(
                self._resolver.java.java_constructor_targets(callee_qn)
            )
            for variant in registry.variants(ctor_qn)
        ]
        host._resolution = (
            cs.EdgeResolution.OVERLOAD if len(ctor_edges) > 1 else host.verdict
        )
        for ctor_type, variant in ctor_edges:
            ctx.ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.CALLS,
                (ctor_type, cs.KEY_QUALIFIED_NAME, variant),
            )

    def _emit_declared_ctor_calls(
        self,
        ctx: CallScan,
        callee_qn: str,
        class_variants: list[str],
        ctor_rel: cs.RelationshipType = cs.RelationshipType.CALLS,
    ) -> None:
        if ctx.calls.emit_constructor_calls(ctx, callee_qn, class_variants):
            return
        registry = self._resolver.function_registry
        # Every class variant INSTANTIATES above, so every one
        # takes its constructors too: `Box@8` for `class Box<T>`
        # declares `Box@8.Box(T)`, which is not a variant of the
        # natural twin's `Box.Box` (issue #2007).
        # The same class-typed gate INSTANTIATES applies above: a
        # variant of another kind (a colliding function, a merged
        # namespace) has no constructor to redirect to.
        ctor_edges = [
            (ctor_type, variant)
            for class_variant in class_variants
            if registry.get(class_variant) in (None, cs.NodeLabel.CLASS)
            for ctor_type, ctor_qn in sorted(
                self._resolver.java.java_constructor_targets(class_variant)
            )
            for variant in registry.variants(ctor_qn)
        ]
        if len(ctor_edges) > 1:
            # Every declared constructor takes an edge because
            # argument-type selection is not attempted: one call,
            # several candidates, which is what `overload` means
            # (issue #1526).
            self._host._resolution = cs.EdgeResolution.OVERLOAD
        for ctor_type, variant in ctor_edges:
            ctx.ensure_rel(
                ctx.caller_spec,
                ctor_rel,
                (ctor_type, cs.KEY_QUALIFIED_NAME, variant),
            )
