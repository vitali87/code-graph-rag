"""Dart's share of the name-based callee resolution: a type name read in the
library it is written in, the extension member a receiver reaches, what a
hop through a Dart member returns, the import prefix a local shadows, and the
member an external generic base hands back through its type arguments.

`CallResolver` owns what every language shares (the order the stages run in,
the resolution cache and the verdict each answer carries, the hop-by-hop
chain typing and the import-prefix fold) and hands Dart's part to the
`DartResolver` it builds."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol
from ..definition_facts import DefinitionFacts
from ..resolver_host import ResolverHost

if TYPE_CHECKING:
    from ..call_site import CallSite
    from ..import_processor import ImportProcessor
    from ..type_inference import TypeInferenceEngine


class DartResolverHost(ResolverHost, Protocol):
    """What Dart resolution reads and calls on the `CallResolver` that builds
    it: the lookups every language shares, the one definition an import
    prefix's libraries hold for a name, and a chained hop's type name to its
    class."""

    def unique_prefixed_definition(
        self, targets: list[str], name: str
    ) -> tuple[str, str, cs.NodeLabel] | None: ...

    def chain_class_qn(
        self, type_name: str, module_qn: str, rust_caller: str | None = None
    ) -> str: ...


class DartResolver:
    """Binds Dart calls by the library scoping and extension rules of Dart.

    Built and owned by `CallResolver`. The registry, the import processor
    (whose `DartImporter` holds the prefixes, exports and shadowing spans
    read here), the type inference engine and the definition facts are the
    resolver's own, held by reference and read as they stand when a call
    resolves."""

    __slots__ = (
        "_host",
        "function_registry",
        "import_processor",
        "type_inference",
        "facts",
    )

    def __init__(
        self,
        host: DartResolverHost,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        facts: DefinitionFacts,
    ) -> None:
        self._host = host
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.facts = facts

    def dart_caller_dependent(self, call: CallSite) -> bool:
        # The cache is keyed by (call_name, module_qn) only, so a caller whose
        # class carries extends-clause type arguments must bypass it: its
        # member resolution is class-context-dependent (#875) and a sibling
        # class's cached answer for the same name would be wrong.
        return (
            call.language == cs.SupportedLanguage.DART
            and call.class_context in self.type_inference.facts.dart_extends_type_args
        )

    def resolve_dart_receiver_shadow(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        # Dart's counterpart of Python's untyped shadow: a local or parameter
        # named like an import prefix makes `p.Box(1)` a call on that value,
        # so the import map must not answer for it (issue #2088). The binding
        # spans are syntactic, so `var p = 1`, which inference leaves untyped,
        # counts. A shadow the type map holds resolves through its type as
        # usual.
        if call.language == cs.SupportedLanguage.DART and self._dart_call_on_shadow(
            call.call_name, call.module_qn, call.local_var_types, call.call_point
        ):
            return True, None
        return False, None

    def resolve_dart_external_base_arg_member(
        self, call: CallSite
    ) -> tuple[str, str] | None:
        # `recv.member(...)` on a receiver undeclared in first-party scope,
        # inside a class extending an EXTERNAL generic base: the only
        # first-party types the base can hand back are its type arguments
        # (`State<GridBtn>.widget` is a GridBtn), so bind when exactly one
        # argument's class defines the member (issue #875). A first-party
        # base, a declared receiver, or a member named like the receiver on
        # the class or ANY registered ancestor (a first-party mixin's getter
        # is inherited, not handed back by the external base) all fall
        # through to the unique-member gate instead.
        call_name, class_context, local_var_types = (
            call.call_name,
            call.class_context,
            call.local_var_types,
        )
        if call.language != cs.SupportedLanguage.DART or not class_context:
            return None
        type_args = self.type_inference.facts.dart_extends_type_args.get(class_context)
        if not type_args:
            return None
        receiver, sep, member = call_name.partition(cs.SEPARATOR_DOT)
        if not sep or cs.SEPARATOR_DOT in member:
            return None
        if local_var_types and receiver in local_var_types:
            return None
        if self._dart_registered_member_named(class_context, receiver):
            return None
        bases = self.facts.class_inheritance.get(class_context)
        # resolve_deferred_inherits rewrote bases in place, so a registered
        # bases[0] means a first-party extends base owns its members.
        if not bases or self.function_registry.get(bases[0]) is not None:
            return None
        matches = [
            qn
            for arg_qn in dict.fromkeys(type_args)
            if (qn := f"{arg_qn}{cs.SEPARATOR_DOT}{member}") in self.function_registry
        ]
        if len(matches) == 1:
            return self.function_registry[matches[0]], matches[0]
        return None

    def _dart_registered_member_named(self, class_qn: str, name: str) -> bool:
        # True when the class or any registered first-party ancestor
        # (extends base or `with` mixin, both in class_inheritance) defines
        # a member with this name. Dart `implements` contributes interface
        # only, never implementation, so it cannot supply a receiver.
        seen: set[str] = set()
        stack = [class_qn]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if f"{current}{cs.SEPARATOR_DOT}{name}" in self.function_registry:
                return True
            stack.extend(self.facts.class_inheritance.get(current, ()))
        return False

    def dart_member_return_type(
        self, class_qn: str, member: str, module_qn: str
    ) -> str | None:
        """The class a Dart hop `.member()` on a `class_qn` receiver returns.

        The member is the class's own, else an inherited one, else an
        extension's (Dart's lookup order). Its return type is recorded under
        the DECLARING qn, so a hop through an inherited or extension member
        ended the chain (`o.move(1).sum()` on a subclass of Point, issue
        #2482). It is also written in the DECLARING library, so it resolves
        there: read in the caller's file instead, a bare `Result` bound to
        whichever `Result` the CALLER sees (PR #2804).
        """
        return_types = self.type_inference.facts.method_return_types
        own_qn = f"{class_qn}{cs.SEPARATOR_DOT}{member}"
        if own_qn in return_types or own_qn in self.function_registry:
            # An own member shadows every inherited and extension one, even
            # when it records no return type.
            member_qn = own_qn
        elif found := self._host.resolve_inherited_method(
            class_qn, member
        ) or self.dart_extension_member(class_qn, member, module_qn):
            member_qn = found[1]
        else:
            return None
        returned = return_types.get(member_qn)
        if not returned:
            return None
        # Dart has no nested types: a member sits in a top-level class, mixin,
        # enum or extension, so its library is two segments up.
        declaring_module = member_qn.rsplit(cs.SEPARATOR_DOT, 2)[0]
        return self.resolve_dart_written_type(returned, declaring_module)

    def resolve_dart_written_type(self, written: str, module_qn: str) -> str:
        """A Dart type as written in `module_qn` -> its qn, else as written.

        Dart scopes a type name to the library it is written in (Greptile,
        PR #2804): `p.Point` names Point through `module_qn`'s OWN `import
        ... as p` (read as a qualified name it matched nothing), and a bare
        `Result` is the library's own declaration, else the ONE its imports
        expose, before any project-wide guess. A name that is already a
        registered qn (an earlier hop's result) is kept.
        """
        if written in self.function_registry:
            return written
        prefix, dot, name = written.partition(cs.SEPARATOR_DOT)
        if dot:
            hit = self._host.unique_prefixed_definition(
                self.dart_prefix_targets(prefix, module_qn), name
            )
            if hit is not None:
                return hit[1]
        else:
            own = f"{module_qn}{cs.SEPARATOR_DOT}{written}"
            if own in self.function_registry:
                return own
            imported = [
                qn
                for library in self._dart_visible_libraries(module_qn) - {module_qn}
                if (qn := f"{library}{cs.SEPARATOR_DOT}{written}")
                in self.function_registry
            ]
            if len(imported) == 1:
                return imported[0]
        return self._host.chain_class_qn(written, module_qn)

    def dart_extension_member(
        self, class_qn: str, member: str, module_qn: str
    ) -> tuple[str, str] | None:
        """The extension member a Dart receiver of `class_qn` reaches, if any.

        Dart consults extensions only after the receiver's own and inherited
        members miss, applies only those the caller's library can see, and
        among them the one on the most specific type wins: one `on` the
        receiver's class beats one `on` an ancestor. Two at the same level are
        ambiguous (a compile error in Dart) and bind nothing.
        """
        on_types = self.type_inference.facts.dart_extension_on_types
        if not on_types:
            return None
        # The `on` type is resolved from the extension's own library, where it
        # was written (`on p.Point` through ITS `as p` import); an extension
        # is always a top-level declaration.
        candidates = [
            (
                member_qn,
                ext_qn,
                self.resolve_dart_written_type(on_types[ext_qn], ext_module),
                ext_module,
            )
            for member_qn in self.function_registry.find_ending_with(member)
            if (ext_qn := member_qn.rpartition(cs.SEPARATOR_DOT)[0]) in on_types
            and (ext_module := ext_qn.rpartition(cs.SEPARATOR_DOT)[0])
        ]
        if not candidates:
            return None
        levels = self._class_and_ancestor_levels(class_qn)
        receiver_types = set().union(*levels)
        # Applicability, then visibility, then specificity. Only an extension
        # `on` the receiver's class or an ancestor competes at all: filtering
        # by visibility first let a visible extension on an UNRELATED type
        # push out the applicable one (PR #2804).
        applicable = [
            candidate for candidate in candidates if candidate[2] in receiver_types
        ]
        if not applicable:
            return None
        # Among those, an unimported one `on Derived` must not beat an
        # imported one `on Base` (PR #2804). A `package:` import is
        # kept verbatim and proves nothing, so when no applicable candidate is
        # provably visible they all stay in play rather than the call being
        # dropped. A provable one also wins a same-level tie against an
        # unprovable one: were both visible, Dart would reject the call as
        # ambiguous, so in code that compiles the unprovable one is not.
        visible_libraries = self._dart_visible_libraries(module_qn)
        pool = [
            candidate for candidate in applicable if candidate[3] in visible_libraries
        ] or applicable
        for level in levels:
            matches = [
                member_qn for member_qn, _ext, on_qn, _module in pool if on_qn in level
            ]
            if len(matches) == 1:
                return self.function_registry[matches[0]], matches[0]
            if matches:
                return None
        return None

    def _class_and_ancestor_levels(self, class_qn: str) -> list[set[str]]:
        # The class, then each breadth-first generation of its ancestors.
        levels: list[set[str]] = [{class_qn}]
        seen = {class_qn}
        frontier = [class_qn]
        while frontier:
            parents = {
                self._host.follow_reexports(parent)
                for child in frontier
                for parent in self.facts.class_inheritance.get(child, [])
            } - seen
            if not parents:
                break
            levels.append(parents)
            seen |= parents
            frontier = list(parents)
        return levels

    def _dart_visible_libraries(self, module_qn: str) -> set[str]:
        """The libraries whose top-level names `module_qn` can see: itself,
        every library it imports (prefixed or not), and whatever those hand
        on through `export` / `part`, transitively. A library a dependency
        merely IMPORTS stays hidden (issue #2482)."""
        processor = self.import_processor
        imported = set(processor.import_mapping.get(module_qn, {}).values())
        for libraries in processor.dart.dart_import_aliases.get(module_qn, {}).values():
            imported.update(libraries)
        visible = {module_qn, *imported}
        frontier = list(imported)
        while frontier:
            for exposed in processor.dart.dart_exposed_libraries.get(
                frontier.pop(), ()
            ):
                if exposed not in visible:
                    visible.add(exposed)
                    frontier.append(exposed)
        return visible

    def _dart_call_on_shadow(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        call_point: int | None,
    ) -> bool:
        """Is `call_name`'s first segment an import prefix a binding at this
        site shadows, with no type to resolve the call through instead?"""
        if cs.SEPARATOR_DOT not in call_name:
            return False
        prefix = call_name.split(cs.SEPARATOR_DOT, 1)[0]
        typed = local_var_types.get(prefix) if local_var_types else None
        if typed and typed != cs.DART_DYNAMIC_TYPE:
            return False
        return bool(
            self.dart_prefix_targets(prefix, module_qn)
        ) and self.dart_prefix_is_shadowed(prefix, module_qn, call_point)

    def dart_prefix_is_shadowed(
        self, prefix: str, module_qn: str, call_point: int | None
    ) -> bool:
        """Does a local or parameter named `prefix` bind it at this site?

        The resolver's `local_var_types` only holds names it could infer a
        TYPE for, so `var p = 1` is absent from it and an inferred-type check
        cannot see the shadow. The import processor records the syntactic
        binding spans instead, which are independent of inference.

        A site with no recorded position cannot be placed against a span, so
        it is treated as unshadowed: flow and taint passes resolve without a
        position, and losing the fold there would drop real edges.
        """
        if call_point is None:
            return False
        spans = self.import_processor.dart.dart_prefix_shadows.get(module_qn, {})
        return any(lo <= call_point < hi for lo, hi in spans.get(prefix, ()))

    def dart_prefix_targets(self, prefix: str, module_qn: str) -> list[str]:
        # An explicit `as` alias wins over the file-derived key: with
        # `import 'helper.dart'; import 'other.dart' as helper;` the name
        # `helper` in the source is other.dart, while import_mapping still
        # holds helper.dart under that key so both IMPORTS edges survive
        # (PR #2040).
        aliases = self.import_processor.dart.dart_import_aliases.get(module_qn, {})
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        return aliases.get(prefix) or (
            [import_map[prefix]] if import_map.get(prefix) else []
        )
