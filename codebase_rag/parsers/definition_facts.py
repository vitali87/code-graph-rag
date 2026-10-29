"""What the definition pass records for the passes that follow it.

The definition pass fills these maps as it ingests each file; type inference
and the call pass read them while they resolve. Every pass holds this one
object and reaches a map as `facts.<map>`, so a new map is one field here,
and a reset or a prune made through it reaches every reader.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from ..types_defs import CSharpCallShape, FunctionLocations, FunctionSpanKey
from .frontends.protocol import FrontendFacts, new_frontend_facts


@dataclass(slots=True)
class DefinitionFacts:
    # {go module qn: its `package` clause name}; Go package membership is
    # (directory, clause), so receiver binding needs both.
    go_package_names: dict[str, str] = field(default_factory=dict)
    # {SQL file: the bytes its tree was parsed from}, kept only where the
    # tree's text is the statement-isolated copy (issue #3180), which
    # blanks the DDL the call pass reads routine references from (issue
    # #3181). Written at every parse, an evicted tree's reload included,
    # so they always belong to the tree the call pass walks.
    sql_sources: dict[Path, bytes] = field(default_factory=dict)
    # {class qn: the base class qns its declaration names}, written at class
    # ingest for every language.
    class_inheritance: dict[str, list[str]] = field(default_factory=dict)
    # {class_qn: [type_arg_qns]} for a Dart `extends Base<T, ...>` clause;
    # read at call resolution to bind a member call on an undeclared
    # receiver against the type arguments of an EXTERNAL base (#875).
    dart_extends_type_args: dict[str, list[str]] = field(default_factory=dict)
    # {extension_qn: on_type_as_written} for `extension E on T`; resolved
    # lazily against E's module, since T may be parsed after E, so a member
    # call on a T receiver can reach E's members (issue #2482).
    dart_extension_on_types: dict[str, str] = field(default_factory=dict)
    # Dart constructor qns (default, named, const, factory): a named
    # constructor call resolves to its own method, so the call pass
    # needs this set to record the construction (issue #2012).
    dart_constructor_qns: set[str] = field(default_factory=set)
    # {interface_qn: [implementer_class_qns]} from IMPLEMENTS edges, so the
    # resolver can redirect an interface-typed call `I.m` to the concrete
    # `Impl.m` when I has exactly one first-party implementer (unambiguous).
    interface_implementers: dict[str, set[str]] = field(default_factory=dict)
    # The same pairs read back from the graph on an incremental run, which
    # parses only the changed files (issue #2403). Kept apart and rebuilt
    # on every rehydration so a pair the graph no longer holds leaves.
    rehydrated_interface_implementers: dict[str, set[str]] = field(default_factory=dict)
    # {class_qn: {field_name: bare_type_name}} for C++ member fields, so a
    # member call `field_.method()` in a (possibly out-of-line, cross-file)
    # method resolves via the field's declared type. Populated at class
    # ingestion, read by the type-inference engine at call resolution.
    class_field_types: dict[str, dict[str, str]] = field(default_factory=dict)
    # C# partial-class unification. A `partial` type split across files
    # becomes N path-distinct Class nodes; parts sharing a
    # namespace-qualified name are one logical type. Each part qn maps to
    # the SHARED list of all its part qns (grows as parts are ingested),
    # so member/base resolution on any part spans the whole group.
    csharp_partial_groups: dict[str, list[str]] = field(default_factory=dict)
    # C# extension methods indexed for call binding: {method simple name:
    # [(method_qn, receiver_type_simple_name)]}. `s.WordCount()` resolves
    # to a `static WordCount(this string s)` whose receiver type matches
    # the call's receiver, a lookup the instance-hierarchy walk can't make
    # (the method lives on an unrelated static class). Populated at method
    # ingestion, read by the type-inference engine as a fallback.
    csharp_extension_methods: dict[str, list[tuple[str, str, str, int]]] = field(
        default_factory=dict
    )
    # C# local functions: {local_fn_qn: (host span key, parameter count)}.
    # The host (method/ctor/enclosing local fn) is pinned by SPAN because
    # at Function-pass time the host method's signatured identity may not
    # be registered yet; the resolver joins the span to function_locations
    # lazily. Bare-name resolution uses this to honor C# scoping: a local
    # fn is callable only from inside its host, and shadows same-name
    # method overloads there (Polly's PredicateBuilder.HandleInner).
    csharp_local_functions: dict[str, tuple[FunctionSpanKey, int]] = field(
        default_factory=dict
    )
    # qns of C# methods declared WITH type parameters (`M<T>(X)`), so
    # bare-call resolution can prefer the overload whose genericness
    # matches the callee shape (`M<TResult>(x)` vs `M(x)`) when
    # parameter arity alone cannot tell same-name twins apart.
    csharp_generic_methods: set[str] = field(default_factory=set)
    # {C# method qn: arguments a call must pass} -- parameters with no
    # default and no `params` -- so a bare call that omits arguments binds
    # only an overload whose omitted parameters are defaulted. A method
    # with no entry (not parsed this run) is judged from its signature.
    csharp_call_shapes: dict[str, CSharpCallShape] = field(default_factory=dict)
    # {class qn: declared type-parameter count} for C# generic types,
    # so `Builder` vs `Builder<TResult>` (same simple name) can be told
    # apart when a type reference's written arity is known.
    csharp_class_generic_arity: dict[str, int] = field(default_factory=dict)
    # {class qn: module qn of the file that DECLARED it}. Recorded
    # rather than derived: a C# class qn embeds its namespace, so
    # `proj/Core.cs` with `namespace Util` yields
    # `proj.Core.Util.Helper`, which sits under the SIBLING module
    # `proj.Core.Util`. No prefix rule on the qn can recover the
    # declarer, so the prune reads this map (#1769).
    csharp_class_owner_module: dict[str, str] = field(default_factory=dict)
    # {class qn: namespace-qualified name (`N1.Outer.Widget`)} for every
    # C# type, read from the declaration at ingest: the qn leaves out a
    # namespace the module's directory already spells (issue #1629), so
    # the `this`-receiver matcher cannot recover the form from it.
    csharp_class_namespaced: dict[str, str] = field(default_factory=dict)
    # The reverse: {namespace-qualified name: class qns carrying it}, so
    # a written `Zeta.BaseC` in a base list still finds the class whose
    # qn no longer ends with those segments (#1629).
    csharp_namespaced_qns: dict[str, set[str]] = field(default_factory=dict)
    # {method qn: (normalized return type, its written generic arity)}
    # for chained-receiver typing; separate from the cross-language
    # method_return_types because the arity is C#-specific.
    csharp_method_return_types: dict[str, tuple[str, int]] = field(default_factory=dict)
    # What each compiler frontend (Roslyn, go/types, javac, Jedi) supplied
    # this run, by language (issue #1178): exact call targets keyed on the
    # callee NAME token, proofs a call leaves the repo, C# base kinds and
    # flows, go/types implements pairs and the roots and files it checked.
    # Readers index it on use; an off or unavailable frontend reads empty.
    frontend_facts: FrontendFacts = field(default_factory=new_frontend_facts)
    # Go interface -> every declaration of its name that another build makes
    # (itself included), refilled by each link; the call resolver binds a
    # call through it per the caller's builds.
    go_declaration_variants: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # {class_qn: {field_name: inner_type}} for Rust guard-container fields
    # (`state: Mutex<State>` -> {"state": "State"}). The field map above keeps
    # the WRAPPER; this inner is applied only when a receiver chain reaches a
    # lock/read/borrow guard accessor (guards do not deref-coerce).
    class_field_guard_inner: dict[str, dict[str, str]] = field(default_factory=dict)
    # {class_qn: {field_name: element_type}} for Rust sequence fields
    # (`workers: Vec<Worker>` -> {"workers": "Worker"}), applied only when
    # an iterator adaptor's closure parameter binds the element (#1045).
    class_field_element_types: dict[str, dict[str, str]] = field(default_factory=dict)
    # {alias_name: underlying_bare_type} for C++ typedef/using aliases, so a
    # receiver declared with an alias resolves to the aliased class. Collected
    # across all files (an alias in a header is used in a .cc), read by the
    # resolver when mapping a receiver type name to a class.
    type_aliases: dict[str, str] = field(default_factory=dict)
    # {func_or_method_qn: bare_return_type_name} captured at definition
    # ingestion, so a chained call `x.foo().bar()` can resolve `bar` on the
    # type `foo()` returns. Read by the resolver's chained-call path.
    method_return_types: dict[str, str] = field(default_factory=dict)
    # {go_free_fn_qn: first_return_type} for typing `v, err := f()`
    # bindings. Kept OFF method_return_types deliberately: that map
    # feeds chained-call resolution, where a multi-return callee is
    # uncallable and first-type recording would shift edge behavior.
    go_function_return_types: dict[str, str] = field(default_factory=dict)
    # (module_qn, def start_line) -> (method_qn, class_qn) for every
    # out-of-class C++ method the definition pass bound; Pass-3 call
    # attribution reuses these decisions instead of re-resolving.
    cpp_out_of_class_methods: dict[tuple[str, int], tuple[str, str]] = field(
        default_factory=dict
    )
    # (module_qn, def start_line) -> location of EVERY C++ function or
    # method node Pass 2 registered, so Pass-3 caller attribution reuses
    # the registered label/qn instead of re-deriving them structurally
    # (the walks diverge on preprocessor-distorted class bodies).
    function_locations: FunctionLocations = field(default_factory=FunctionLocations)
    # Inline (non-file) module qns, e.g. Rust `mod x {}`; deferred
    # import verification counts them as real internal targets.
    declared_module_qns: set[str] = field(default_factory=set)
    # {Rust function/method qn: the module qn it is contained by}, taken
    # from the AST at ingest because qn space cannot distinguish an inline
    # `mod` from an impl block. Pass-3 resolves `super::`/`self::` against
    # it (issue #1086).
    rust_function_modules: dict[str, str] = field(default_factory=dict)
    # Registered qns that are macro definitions (Rust macro_rules!):
    # macros register as Function nodes but live in a separate namespace,
    # so Pass-3 gates macro-invocation call sites to these targets and
    # fn-namespace call sites away from them.
    macro_qns: set[str] = field(default_factory=set)
    # {qn: file path} for definitions rehydrated from the graph on an
    # incremental run, whose modules are absent from module_qn_to_file_path
    # (only re-parsed files populate it). _is_cpp_defined falls back to
    # this so cross-file resolution into UNCHANGED headers still works.
    rehydrated_definition_paths: dict[str, str] = field(default_factory=dict)

    def drop_return_types(self, qns: Collection[str]) -> None:
        """Forget the return types recorded under `qns` (issues #1738, #1753).

        Both maps, the general one and the C# `(type, arity)` one, are read
        directly, with no derived index to invalidate.
        """
        for qn in qns:
            self.method_return_types.pop(qn, None)
            self.csharp_method_return_types.pop(qn, None)
        # A Dart constructor's qn is written by the same ingest step as its
        # return type, so it leaves with it; a stale one would keep stamping
        # INSTANTIATES on a call that now reaches a static factory (#2012).
        self.dart_constructor_qns.difference_update(qns)

    def drop_csharp_entries(
        self, function_qns: Collection[str], class_qns: Collection[str]
    ) -> None:
        """Forget the C# entries owned by removed definitions (issue #1769).

        A deleted file's generic methods, class arities, local functions and
        extension methods otherwise outlive it on a reused updater and go on
        steering resolution towards a definition that is gone.

        `function_qns` are the method and function qns the caller determined
        the file owned, under the filter the registry sweep uses; `class_qns`
        are class qns, owned by the module recorded for them at ingest
        (`csharp_class_owner_module`): their qn embeds a namespace and so
        cannot be attributed by prefix. `csharp_extension_methods` is keyed
        by simple NAME with a list of owners, so its prune drops the owning
        entries from each list and removes the key only once nothing is left
        under it.
        """
        function_qns = set(function_qns)
        self.csharp_generic_methods -= function_qns
        for qn in function_qns:
            self.csharp_local_functions.pop(qn, None)
            self.csharp_call_shapes.pop(qn, None)
        for qn in class_qns:
            self.csharp_class_generic_arity.pop(qn, None)
            # Dropped together: an owner record for a class whose arity is
            # gone names a file this updater no longer has, and would keep
            # the entry alive across the next deletion of the same qn.
            self.csharp_class_owner_module.pop(qn, None)
        for name in list(self.csharp_extension_methods):
            kept = [
                entry
                for entry in self.csharp_extension_methods[name]
                if entry[0] not in function_qns
            ]
            if kept:
                self.csharp_extension_methods[name] = kept
            else:
                del self.csharp_extension_methods[name]

    def drop_class(self, qn: str) -> None:
        """Forget one removed class's rows in the class-keyed maps."""
        self.class_inheritance.pop(qn, None)
        self.class_field_types.pop(qn, None)
        # A partial part leaves its group too, and IN PLACE: the group list is
        # the very list the definition pass's partial index holds under the
        # syntactic key, so a new list would leave that view on the old one.
        # Without this a re-parse appended the part again and a deleted part
        # kept typing its siblings' members (issue #2016).
        if (group := self.csharp_partial_groups.pop(qn, None)) is not None:
            group[:] = [part for part in group if part != qn]
        # Same ownership, same reason (issue #1629): every C# class has an
        # entry, not only the generic ones `drop_csharp_entries` covers.
        namespaced = self.csharp_class_namespaced.pop(qn, None)
        if namespaced is not None:
            carriers = self.csharp_namespaced_qns.get(namespaced)
            if carriers is not None:
                carriers.discard(qn)
                if not carriers:
                    del self.csharp_namespaced_qns[namespaced]

    def drop_implementers(self, removed: Collection[str]) -> None:
        """Forget the parsed implementer pairs of classes a removed file
        declared.

        A class that stopped implementing an interface kept its pair on a
        reused updater, and a call typed to the interface still reached its
        method as the sole implementer's (#2403). The re-parse records the
        pairs the class still has.
        """
        if not removed:
            return
        emptied = []
        for interface_qn, implementers in self.interface_implementers.items():
            implementers.difference_update(removed)
            if not implementers:
                emptied.append(interface_qn)
        for interface_qn in emptied:
            del self.interface_implementers[interface_qn]
