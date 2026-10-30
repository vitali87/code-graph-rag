"""C#'s share of the class pass: a type's declared namespace, generic
arity and partial group, the members and method facts a C# type records,
the location index the Roslyn facts join back through, and the base a
deferred INHERITS/IMPLEMENTS binds by scope, by written arity or as an arity
sibling.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships, and the deferred
INHERITS/IMPLEMENTS resolution) and hands C#'s part to the
`CSharpClassIngest` it builds."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import module_directory_qn
from ...node_text import safe_decode_text
from ...types_defs import DeferredInherit, FunctionLocation, PropertyDict
from ...utils import qn_markers
from ...utils.path_utils import cached_relative_path
from ..class_ingest import base_targets as bt
from ..class_ingest.utils import csharp_has_override_modifier
from ..parameter_nodes import csharp_call_shape
from ..utils import function_span_key, module_qn_for_entity
from . import utils as csharp_utils
from .arity import CSharpArityIndex

if TYPE_CHECKING:
    from ...types_defs import FunctionRegistryTrieProtocol
    from ..definition_facts import DefinitionFacts
    from ..import_processor import ImportProcessor


def _record_parameter_types(member_node: Node, field_types: dict[str, str]) -> None:
    # A record's positional parameters ARE public properties of
    # the record type; record them as members so receiver typing
    # and the delegate-invoke gate see them (`Callback();` on a
    # record param is Action.Invoke, not a first-party call).
    for pl in member_node.children:
        if pl.type != cs.TS_CSHARP_PARAMETER_LIST:
            continue
        for prm in pl.children:
            if prm.type != cs.TS_CSHARP_PARAMETER:
                continue
            pname = safe_decode_text(prm.child_by_field_name(cs.FIELD_NAME))
            ptype = safe_decode_text(prm.child_by_field_name(cs.FIELD_TYPE))
            if pname and ptype:
                field_types.setdefault(pname, csharp_utils.annotate_type_ref(ptype))


def signatured_method_qn(method_node: Node, class_qn: str) -> str | None:
    # Give C# methods/constructors a parameter signature so
    # overloads and overloaded constructors stay distinct nodes
    # (without it two `Widget(...)` ctors collide and the second
    # gets an `@line` suffix). Zero-arg members stay bare so their
    # qn is stable and matches an unsignatured call site.
    cs_name, cs_params = csharp_utils.extract_method_signature(method_node)
    if cs_name and cs_params:
        param_sig = cs.SEPARATOR_COMMA_SPACE.join(cs_params)
        return f"{class_qn}.{cs_name}({param_sig})"
    return None


class CSharpClassIngest:
    """Records what a C# type and its methods declare beside the shared class
    pass, and binds a deferred C# base the way C# scoping and arity do.

    Built and owned by `ClassIngest`. The registries, the module paths, the
    definition facts and the C# indexes are the pass's own, held by
    reference: the override walk reads the method sets, and the graph
    updater's rehydration refills the partial index and the type locations."""

    __slots__ = (
        "repo_path",
        "function_registry",
        "module_qn_to_file_path",
        "import_processor",
        "facts",
        "methods",
        "override_methods",
        "_partial_index",
        "type_locations",
    )

    def __init__(
        self,
        *,
        repo_path: Path,
        function_registry: FunctionRegistryTrieProtocol,
        module_qn_to_file_path: dict[str, Path],
        import_processor: ImportProcessor,
        facts: DefinitionFacts,
        methods: set[str],
        override_methods: set[str],
        partial_index: dict[str, list[str]],
        type_locations: dict[tuple[str, int], str],
    ) -> None:
        self.repo_path = repo_path
        self.function_registry = function_registry
        self.module_qn_to_file_path = module_qn_to_file_path
        self.import_processor = import_processor
        self.facts = facts
        self.methods = methods
        self.override_methods = override_methods
        self._partial_index = partial_index
        self.type_locations = type_locations

    def record_type_props(
        self,
        class_node: Node,
        class_qn: str,
        class_name: str,
        class_props: PropertyDict,
    ) -> None:
        self._record_namespace(class_node, class_qn, class_props)
        self._record_arity(class_node, class_name, class_props)

    def _record_namespace(
        self, class_node: Node, class_qn: str, class_props: PropertyDict
    ) -> None:
        # The declared namespace is the type's own property, whether or
        # not the qn repeats it (issue #1629).
        if namespace := csharp_utils.declared_namespace(class_node):
            class_props[cs.KEY_NAMESPACE] = namespace
        if namespaced := csharp_utils.namespace_qualified_name(class_node):
            self.facts.csharp_class_namespaced[class_qn] = namespaced
            self.facts.csharp_namespaced_qns.setdefault(namespaced, set()).add(class_qn)

    def _record_arity(
        self, class_node: Node, class_name: str, class_props: PropertyDict
    ) -> None:
        # A generic twin's qn spells its CLR arity (`PB`1`, issue #2579), but
        # its name is still the one written. The simple-name index keeps the
        # qn's spelling, as the registry does for a type read back from the
        # graph, so a plain `PB` meets the non-generic twin only and a
        # written arity reaches the other. The arity is stored as well: an
        # incremental run knows a type in an unchanged file only from its
        # node, and `new PB<int>()` must find the same twin either way.
        class_props[cs.KEY_NAME] = qn_markers.strip_arity_marker(class_name)
        if arity := csharp_utils.type_parameter_count(class_node):
            class_props[cs.KEY_GENERIC_ARITY] = arity

    def record_type_location(
        self, class_qn: str, file_path: Path, class_start_line: int
    ) -> dict[str, str] | None:
        rel_path = cached_relative_path(file_path, self.repo_path).as_posix()
        # Reverse index for the Roslyn frontend's location-keyed facts:
        # partial declaration groups join back to these Class qns after
        # Pass 2.
        self.type_locations[(rel_path, class_start_line)] = class_qn
        return self.facts.frontend_facts[cs.SupportedLanguage.CSHARP].base_kinds.get(
            (rel_path, class_start_line)
        )

    def record_class_members(
        self,
        member_node: Node,
        class_qn: str,
        module_qn: str,
        modifiers: list[str],
        file_path: Path | None,
    ) -> None:
        # Record C# field/property types so a field-typed receiver
        # (`_w.M()`, `this._w.M()`) resolves, including a field inherited
        # from a base class in another file (the resolver walks
        # class_inheritance over these per-class maps).
        field_types = csharp_utils.build_field_type_map(member_node) or {}
        if member_node.type == cs.TS_CSHARP_RECORD_DECLARATION:
            _record_parameter_types(member_node, field_types)
        if field_types:
            self.facts.class_field_types[class_qn] = field_types
        # Declared type-parameter count: `Builder<TResult>` -> 1;
        # unrecorded means 0, so only generics are stored.
        arity = csharp_utils.type_parameter_count(member_node)
        if arity:
            self.facts.csharp_class_generic_arity[class_qn] = arity
            # The declaring module, recorded because the class qn
            # cannot yield it: a namespace pushes the qn under a
            # sibling module's qn (#1769).
            self.facts.csharp_class_owner_module[class_qn] = module_qn
        # A `partial` type is split across files into N path-distinct
        # nodes; group the parts into one shared list so a typed receiver
        # resolves members and bases from any part. The key is the
        # declaring DIRECTORY (module_qn minus the file stem) plus the
        # namespace-qualified name, NOT the bare namespace name: two
        # independent projects that both declare `N.Widget` live in
        # different directories and must not be merged across assembly
        # boundaries. Parts in different directories of one project fall
        # back to generic resolution (safe under-merge) rather than risk a
        # cross-project wrong edge.
        # The directory comes from the file's own segment by name: a part
        # with a dotted stem (`Widget.Designer.cs`) split from its sibling
        # under a last-dot rule, and once base resolution refused any
        # ambiguity that is not one partial group, the split lost the
        # `: N.Widget` edge (#1999).
        if cs.TS_CSHARP_MODIFIER_PARTIAL in modifiers:
            self._group_partial(class_qn, module_qn, file_path, arity)

    def _group_partial(
        self, class_qn: str, module_qn: str, file_path: Path | None, arity: int
    ) -> None:
        directory = module_directory_qn(module_qn, file_path) or module_qn
        # A second same-name part in ONE file registers under a
        # duplicate-suffixed qn (`Bench@24`); the marker is a
        # registration artefact, not part of the declared name, so
        # strip it or the two parts never share a group (issue #2014).
        # The tail below the module, so only its END can carry the
        # marker a registration appended; a verbatim identifier
        # (`@event`) opens with the same character and is kept.
        # The arity is always spelled: `Box<T>` beside a `Box` is `Box`1`
        # while its part in another file is plain `Box`, and each must join
        # the other, never the non-generic twin (issue #2579).
        declared = qn_markers.with_leaf_arity(class_qn[len(module_qn) + 1 :], arity)
        key = f"{directory}{cs.SEPARATOR_DOT}{declared}"
        group = self._partial_index.setdefault(key, [])
        group.append(class_qn)
        self.facts.csharp_partial_groups[class_qn] = group

    def record_method(
        self,
        class_qn: str,
        module_qn: str | None,
        method_node: Node,
        ingested_qn: str,
    ) -> None:
        self.facts.csharp_call_shapes[ingested_qn] = csharp_call_shape(method_node)
        if (
            method_node.child_by_field_name(cs.TS_CSHARP_FIELD_TYPE_PARAMETERS)
            is not None
        ):
            self.facts.csharp_generic_methods.add(ingested_qn)
        # Track C# methods (and the `override`-modified subset) so the
        # override walk gates class-parent matches: an implicit hide or a
        # `new` shadow is not an override, unlike an interface impl.
        self._track_method(class_qn, module_qn, method_node, ingested_qn)
        self._record_method_return_type(method_node, ingested_qn)

    def _track_method(
        self,
        class_qn: str,
        module_qn: str | None,
        method_node: Node,
        ingested_qn: str,
    ) -> None:
        if module_qn is not None:
            # Record where this member landed so the Roslyn frontend's
            # declaration-location facts (call targets, query callers)
            # resolve to the exact registered qn and label.
            self.facts.function_locations[function_span_key(module_qn, method_node)] = (
                FunctionLocation(
                    label=cs.NodeLabel.METHOD.value,
                    qualified_name=ingested_qn,
                    container_qn=class_qn,
                )
            )
        self.methods.add(ingested_qn)
        if csharp_has_override_modifier(method_node):
            self.override_methods.add(ingested_qn)
        # Index extension methods by simple name + receiver type so a
        # `recv.Ext()` call binds to the static method even though it
        # lives on an unrelated static class (not in recv's hierarchy).
        csharp_utils.index_extension_method(
            self.facts.csharp_extension_methods, ingested_qn, method_node
        )

    def _record_method_return_type(self, method_node: Node, ingested_qn: str) -> None:
        if (
            rt_node := method_node.child_by_field_name(cs.TS_CSHARP_FIELD_RETURNS)
            # property_declaration exposes its type via `type`,
            # not `returns`; recording it lets chained typing and
            # the external-member gate see through properties.
            or method_node.child_by_field_name(cs.FIELD_TYPE)
        ) is None:
            return
        # Record a C# method's return type so a chained call
        # (`Policy.Handle<T>().CircuitBreaker(...)`, Polly's whole
        # fluent surface) can type the receiver for the next hop.
        if rt_text := csharp_utils.normalize_csharp_type_name(rt_node):
            raw = safe_decode_text(rt_node) or rt_text
            self.facts.csharp_method_return_types[ingested_qn] = (
                rt_text,
                csharp_utils.generic_arity_of_type_text(raw),
            )

    def scoped_base(self, entry: DeferredInherit) -> str | None:
        if entry.written_ref is None:
            return None
        return bt.resolve_csharp_scoped_base(
            entry.written_ref,
            entry.child_qn,
            self.facts.csharp_class_namespaced.get(entry.child_qn),
            self.import_processor.import_mapping.get(entry.module_qn, {}),
            bt.CSharpTypeIndex(
                self.function_registry,
                self.facts.csharp_namespaced_qns,
                self.facts.csharp_class_generic_arity,
                self.facts.csharp_partial_groups,
            ),
        )

    def _arity_index(self) -> CSharpArityIndex:
        return CSharpArityIndex(
            self.function_registry,
            self.facts.csharp_class_generic_arity,
            self.facts.csharp_class_namespaced,
            self.facts.csharp_namespaced_qns,
            self.facts.csharp_partial_groups,
        )

    def base_by_arity(
        self, entry: DeferredInherit, resolved: tuple[str, bool] | None
    ) -> tuple[str, bool] | None:
        # The name lookup finds `PB` or `PB<T>`, whichever it meets first; the
        # base's written arity picks the one C# binds (issue #2579). Only a C#
        # base carries a written ref.
        if resolved is None or resolved[1] or entry.written_ref is None:
            return resolved
        arity = csharp_utils.split_type_ref(entry.written_ref)[1]
        twin = self._arity_index().twin(resolved[0], arity)
        # The child is never its own base, whichever twin the name found.
        return (twin, False) if twin != entry.child_qn else resolved

    def arity_sibling(self, entry: DeferredInherit) -> str | None:
        # Only C# overloads type names by generic arity, so only there can a
        # base that resolves to the declaring type itself legally name a
        # DIFFERENT type. The sibling conventionally lives beside the child
        # (Polly's Foo.cs + Foo.TResult.cs), so only a UNIQUE same-simple-name
        # type declaration under the module's parent package qualifies;
        # ambiguity keeps the no-edge answer rather than guessing.
        if entry.language != cs.SupportedLanguage.CSHARP:
            return None
        type_decls = (cs.NodeLabel.CLASS, cs.NodeLabel.INTERFACE, cs.NodeLabel.ENUM)
        # The written arity names the sibling (issue #764): a twin in the
        # same file carries it in its qn (`Foo`1`, issue #2579), one in
        # another file shares the child's declared form.
        if entry.written_ref is not None:
            arity = csharp_utils.split_type_ref(entry.written_ref)[1]
            twin = self._arity_index().twin(entry.child_qn, arity)
            if twin != entry.child_qn:
                return twin
        natural = qn_markers.strip_arity_marker(qn_markers.natural_qn(entry.child_qn))
        simple = natural.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        candidates = {
            qn
            for qn in self.function_registry.find_ending_with(simple)
            if qn != entry.child_qn
            and simple in qn.split(cs.SEPARATOR_DOT)
            and self.function_registry.get(qn) in type_decls
        }
        package_prefix = (
            entry.module_qn.rsplit(cs.SEPARATOR_DOT, 1)[0] + cs.SEPARATOR_DOT
        )
        in_package = {qn for qn in candidates if qn.startswith(package_prefix)}
        if len(in_package) == 1:
            return in_package.pop()
        if in_package:
            return self._single_partial_representative(in_package)
        # No same-package sibling: the pair can span projects (Polly's
        # legacy BrokenCircuitException<TResult> : the Polly.Core
        # non-generic), so fall back to a project-wide unique declaration.
        return candidates.pop() if len(candidates) == 1 else None

    def _single_partial_representative(self, in_package: set[str]) -> str | None:
        # Multiple same-package candidates can still be ONE type: a
        # `partial` sibling split across files (Polly's PredicateBuilder,
        # issue #764 shape 3). When every candidate sits in a single
        # partial group, pick the deterministic representative; genuine
        # distinct types keep the no-edge answer.
        groups = {
            frozenset(self.facts.csharp_partial_groups.get(qn, (qn,)))
            for qn in in_package
        }
        if len(groups) == 1:
            return min(in_package)
        return None

    def pin_base_module(self, entry: DeferredInherit, parent_qn: str) -> None:
        # A resolved first-party base pins the C# namespace import to the
        # module defining it (issue #1347).
        target_module = module_qn_for_entity(parent_qn, self.module_qn_to_file_path)
        if target_module is not None and target_module != entry.module_qn:
            self.import_processor.csharp.record_resolved_cross_module_use(
                entry.module_qn, target_module
            )

    def namespaced_class(self, class_name: str) -> str | None:
        # A namespace-qualified C# name (`Zeta.BaseC` in a base list) used to
        # match the tail of the class qn; a namespace the directory spells is
        # no longer in it, so the declared form is looked up instead, for a
        # C# reference only: a Python `Zeta.BaseC` is not this index's
        # business (issue #1629).
        return csharp_utils.unique_carrier(
            self.facts.csharp_namespaced_qns.get(class_name),
            self.facts.csharp_partial_groups,
        )
