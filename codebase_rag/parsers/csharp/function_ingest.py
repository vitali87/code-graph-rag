"""C#'s share of the function pass: the local functions it drops as
parse-recovery artifacts or pins to their host scope, and the members a
`#if`-truncated type spilled outside its body, recovered as that type's
methods.

`FunctionIngest` owns what every language shares (the scan of a file's
function captures, a function's identity, node and DEFINES edge) and hands
C#'s part to the `CSharpFunctionIngest` it builds."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import LANGUAGE_FQN_SPECS
from ...node_text import safe_decode_text
from ...types_defs import DeferredParentLink, FunctionLocation
from ...utils.fqn_resolver import scoped_name_parts
from ..definition_facts import DefinitionFacts
from ..language_definitions import LanguageFunctionIngest
from ..method_ingest import MethodIngestHost, ingest_method
from ..parameter_nodes import csharp_call_shape
from ..utils import function_span_key
from . import utils as csharp_utils

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...types_defs import LanguageQueries
    from ..function_identity import FunctionResolution


def _nearest_preceding_type(
    func_node: Node, class_node_types: frozenset[str]
) -> Node | None:
    # Find the class a `#if`-detached member belongs to: scan preceding siblings
    # at each ancestor level up to the root (a `#if`-guarded member wraps in a
    # preproc_if, so its OWN siblings never include the truncated class) and
    # return the nearest class-like node, whose body ended early and spilled this
    # member after it.
    node: Node | None = func_node
    while node is not None:
        sib = node.prev_sibling
        while sib is not None:
            if sib.type in class_node_types:
                return sib
            sib = sib.prev_sibling
        node = node.parent
    return None


class CSharpFunctionIngest(LanguageFunctionIngest):
    """Drops or pins the C# local functions the function pass captures, and
    recovers the members a truncated type left at namespace level.

    Built and owned by `FunctionIngest`. The definition facts and the C#
    method sets are the pass's own, held by reference: the class pass tracks
    its methods in the same sets."""

    __slots__ = (
        "_method_host",
        "facts",
        "methods",
        "override_methods",
        "_deferred_parent_links",
    )

    # A C# method registers signature-suffixed (`Run(int)`), a shape
    # structural re-derivation cannot reproduce, and its parameterless
    # overload sibling exactly SHADOWS the guess. Methods register in the
    # class pass after the function pass, so the guess carries the parent
    # NODE's span for the deferred resolver to swap in the recorded identity.
    pins_parent_span = True

    def __init__(
        self,
        *,
        method_host: MethodIngestHost,
        facts: DefinitionFacts,
        methods: set[str],
        override_methods: set[str],
        deferred_parent_links: list[DeferredParentLink],
    ) -> None:
        # What a recovered member is written through.
        self._method_host = method_host
        self.facts = facts
        self.methods = methods
        self.override_methods = override_methods
        # Shared with the class pass, which emits them once every parent
        # is registered.
        self._deferred_parent_links = deferred_parent_links

    def skips_function(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
        file_path: Path | None,
    ) -> bool:
        # A C# local function whose name is a reserved keyword is a
        # parse-recovery artifact: a `#if` splitting an if/else chain
        # mid-method makes tree-sitter parse the trailing `else if` as a
        # local_function_statement named `if`. Drop it rather than emit a
        # bogus (or anonymised) Function node.
        if func_node.type == cs.TS_CSHARP_LOCAL_FUNCTION_STATEMENT:
            name_node = func_node.child_by_field_name(cs.FIELD_NAME)
            if (
                name_node
                and name_node.text
                and safe_decode_text(name_node) in cs.CSHARP_RESERVED_KEYWORDS
            ):
                return True

        # A C# member declaration (method/property/operator/ctor) that a
        # `#if`-truncated class node detached into the namespace's
        # declaration_list reaches here with no class ancestor. It is a real
        # class member by grammar invariant, so recover its class and emit a
        # Method rather than mislabelling it a module Function.
        return self._recover_orphan_method(
            func_node, module_qn, lang_config, lang_queries, file_path
        )

    def record_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        lang_config: LanguageSpec,
        location: FunctionLocation,
    ) -> None:
        # Pin a C# local function to its HOST scope (method/ctor/enclosing local
        # fn) by span, plus its declared parameter count, so bare-name call
        # resolution honours C# scoping and arity (the host's signatured identity
        # is not registered yet at this point).
        if func_node.type != cs.TS_CSHARP_LOCAL_FUNCTION_STATEMENT:
            return
        host = func_node.parent
        while host is not None and host.type not in cs.CSHARP_LOCAL_FN_HOST_TYPES:
            host = host.parent
        if host is None:
            return
        self.facts.csharp_local_functions[resolution.qualified_name] = (
            function_span_key(module_qn, host),
            len(csharp_utils.extract_parameter_type_names(func_node)),
        )

    def _scope_qn(self, node: Node, module_qn: str, file_path: Path | None) -> str:
        # Qualified name of a C# scope node (class/namespace) via the same walk the
        # definition FQN pass uses, so a recovered class qn matches the one the
        # class-ingest pass registered. `node` is itself a scope type, so start the
        # walk at it (its own name is the innermost segment).
        fqn_config = LANGUAGE_FQN_SPECS[cs.SupportedLanguage.CSHARP]
        parts = scoped_name_parts(node, fqn_config, module_qn, file_path)
        if not parts:
            return module_qn
        return module_qn + cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(parts)

    def _recover_orphan_method(
        self,
        func_node: Node,
        module_qn: str,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
        file_path: Path | None,
    ) -> bool:
        # A C# method/property/operator/ctor is grammatically only ever a type
        # member; a `local_function_statement` is the real top-level function, so it
        # is deliberately excluded and left to the Function path.
        if func_node.type not in cs.CSHARP_MEMBER_ONLY_TYPES:
            return False
        class_node = _nearest_preceding_type(
            func_node, frozenset(lang_config.class_node_types)
        )
        if class_node is None:
            return False

        from ..class_ingest.utils import csharp_has_override_modifier

        class_qn = self._scope_qn(class_node, module_qn, file_path)
        cs_name, cs_params = csharp_utils.extract_method_signature(func_node)
        method_qualified_name = None
        if cs_name and cs_params:
            param_sig = cs.SEPARATOR_COMMA_SPACE.join(cs_params)
            method_qualified_name = f"{class_qn}.{cs_name}({param_sig})"

        ingested_qn = ingest_method(
            self._method_host,
            func_node,
            class_qn,
            cs.NodeLabel.CLASS,
            cs.SupportedLanguage.CSHARP,
            lang_queries=lang_queries,
            method_qualified_name=method_qualified_name,
            file_path=file_path,
            # The class node was parse-truncated; defer the containment so it
            # resolves to DEFINES_METHOD if the class registered, else an audit-safe
            # module DEFINES (never a dangling edge).
            defer_containment=self._deferred_parent_links,
            module_qn=module_qn,
        )
        if ingested_qn is None:
            return False
        self.facts.csharp_call_shapes[ingested_qn] = csharp_call_shape(func_node)
        if (
            func_node.child_by_field_name(cs.TS_CSHARP_FIELD_TYPE_PARAMETERS)
            is not None
        ):
            self.facts.csharp_generic_methods.add(ingested_qn)
        if (
            rt_node := func_node.child_by_field_name(cs.TS_CSHARP_FIELD_RETURNS)
        ) is not None:
            if rt_text := csharp_utils.normalize_csharp_type_name(rt_node):
                raw = safe_decode_text(rt_node) or rt_text
                self.facts.csharp_method_return_types[ingested_qn] = (
                    rt_text,
                    csharp_utils.generic_arity_of_type_text(raw),
                )
        # Record where this method landed so Pass-3 call attribution reuses this
        # Method identity instead of re-deriving a module-Function qn from the node
        # (the FQN walk sees no class ancestor, so a call inside the recovered
        # member would otherwise source a phantom, dropped edge).
        self.facts.function_locations[function_span_key(module_qn, func_node)] = (
            FunctionLocation(
                label=cs.NodeLabel.METHOD.value,
                qualified_name=ingested_qn,
                container_qn=class_qn,
            )
        )
        # Track it like an in-class C# method so the override walk can gate a
        # class-parent OVERRIDES on the `override` modifier, and index it as an
        # extension method if it is one, so `recv.Ext()` binds to a recovered
        # extension just as the normal class-member pass would.
        self.methods.add(ingested_qn)
        if csharp_has_override_modifier(func_node):
            self.override_methods.add(ingested_qn)
        csharp_utils.index_extension_method(
            self.facts.csharp_extension_methods, ingested_qn, func_node
        )
        return True
