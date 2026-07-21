from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import (
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageQueries,
    NodeType,
    SimpleNameLookup,
)
from ...utils.path_utils import cached_relative_path
from ..csharp_frontend import CallSiteKey, CSharpCallSite
from ..import_processor import ImportProcessor
from ..utils import safe_decode_text
from .utils import (
    _normalize_type_name,
    annotate_type_ref,
    generic_arity_of_type_text,
    split_type_ref,
)

if TYPE_CHECKING:
    from ..factory import ASTCacheProtocol

_TYPE_DECLS = (NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM)

# Sentinel: the call's target is provably EXTERNAL (a BCL/base member, a
# static call on an unregistered type). The dispatcher must emit nothing
# and must NOT fall back to the name-only trie, which would fabricate an
# edge onto an unrelated same-name first-party member.
CSHARP_EXTERNAL_TARGET: tuple[str, str] = ("", "")


def _arity(leaf: str) -> int:
    # Parameter count of a (possibly signatured) method leaf: `M(int, string)`
    # -> 2, `M` / `M()` -> 0. Only depth-0 commas separate parameters, so a
    # qualified/array type never inflates the count.
    open_idx = leaf.find(cs.CHAR_PAREN_OPEN)
    if open_idx < 0:
        return 0
    inner = leaf[open_idx + 1 : leaf.rfind(cs.CHAR_PAREN_CLOSE)]
    if not inner.strip():
        return 0
    depth = 0
    count = 1
    for ch in inner:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif ch == cs.SEPARATOR_COMMA and depth == 0:
            count += 1
    return count


class CSharpTypeInferenceEngine:
    __slots__ = (
        "import_processor",
        "function_registry",
        "repo_path",
        "project_name",
        "ast_cache",
        "queries",
        "module_qn_to_file_path",
        "class_inheritance",
        "simple_name_lookup",
        "class_field_types",
        "csharp_partial_groups",
        "csharp_extension_methods",
        "csharp_call_sites",
        "csharp_external_sites",
        "csharp_local_functions",
        "csharp_generic_methods",
        "csharp_class_generic_arity",
        "csharp_method_return_types",
        "method_return_types",
        "function_locations",
        "_rel_to_module",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        repo_path: Path,
        project_name: str,
        ast_cache: ASTCacheProtocol,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        module_qn_to_file_path: dict[str, Path],
        class_inheritance: dict[str, list[str]],
        simple_name_lookup: SimpleNameLookup,
        class_field_types: dict[str, dict[str, str]],
        csharp_partial_groups: dict[str, list[str]] | None = None,
        csharp_extension_methods: dict[str, list[tuple[str, str, str, int]]]
        | None = None,
        csharp_call_sites: dict[CallSiteKey, CSharpCallSite] | None = None,
        csharp_external_sites: set[CallSiteKey] | None = None,
        csharp_local_functions: dict[str, tuple[FunctionSpanKey, int]] | None = None,
        csharp_generic_methods: set[str] | None = None,
        csharp_class_generic_arity: dict[str, int] | None = None,
        csharp_method_return_types: dict[str, tuple[str, int]] | None = None,
        method_return_types: dict[str, str] | None = None,
        function_locations: dict[FunctionSpanKey, FunctionLocation] | None = None,
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.repo_path = repo_path
        self.project_name = project_name
        self.ast_cache = ast_cache
        self.queries = queries
        self.module_qn_to_file_path = module_qn_to_file_path
        self.class_inheritance = class_inheritance
        self.simple_name_lookup = simple_name_lookup
        self.class_field_types = class_field_types
        self.csharp_partial_groups = (
            csharp_partial_groups if csharp_partial_groups is not None else {}
        )
        self.csharp_extension_methods = (
            csharp_extension_methods if csharp_extension_methods is not None else {}
        )
        # Shared references (populated by the Roslyn frontend / Pass 2 after
        # this engine is constructed), so `or {}` would lose them.
        self.csharp_call_sites = (
            csharp_call_sites if csharp_call_sites is not None else {}
        )
        self.csharp_external_sites = (
            csharp_external_sites if csharp_external_sites is not None else set()
        )
        self.csharp_local_functions = (
            csharp_local_functions if csharp_local_functions is not None else {}
        )
        self.csharp_generic_methods = (
            csharp_generic_methods if csharp_generic_methods is not None else set()
        )
        self.csharp_class_generic_arity = (
            csharp_class_generic_arity if csharp_class_generic_arity is not None else {}
        )
        self.csharp_method_return_types = (
            csharp_method_return_types if csharp_method_return_types is not None else {}
        )
        # Shared reference (as above): {method qn: normalized return type},
        # populated during ingestion, read by chained-receiver typing.
        self.method_return_types = (
            method_return_types if method_return_types is not None else {}
        )
        self.function_locations = (
            function_locations if function_locations is not None else {}
        )
        self._rel_to_module: dict[str, str] = {}

    # --- variable/field/parameter type map -------------------------------

    def build_variable_type_map(self, scope_node: Node) -> dict[str, str]:
        # Parameters and locals only. Field types are looked up at resolve
        # time against class_field_types (keyed by class qn), which also
        # reaches fields inherited from a base class in another file; the
        # enclosing class qn is not known here, only at the call site.
        types: dict[str, str] = {}
        self._collect_parameters(scope_node, types)
        self._collect_locals(scope_node, types)
        return types

    def _collect_parameters(self, scope_node: Node, types: dict[str, str]) -> None:
        param_list = scope_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if param_list is None:
            return
        prev_loose_type: str | None = None
        for child in param_list.children:
            # A `params string[] xs` tail is not wrapped in a `parameter`
            # node (grammar quirk, same as extract_parameter_type_names):
            # its array_type and identifier sit loose under the list.
            if child.type == cs.TS_CSHARP_ARRAY_TYPE:
                prev_loose_type = safe_decode_text(child)
                continue
            if child.type == cs.TS_CSHARP_IDENTIFIER and prev_loose_type:
                if name := safe_decode_text(child):
                    types[name] = annotate_type_ref(prev_loose_type)
                prev_loose_type = None
                continue
            if child.type != cs.TS_CSHARP_PARAMETER:
                continue
            name = safe_decode_text(child.child_by_field_name(cs.FIELD_NAME))
            type_text = safe_decode_text(child.child_by_field_name(cs.FIELD_TYPE))
            if name and type_text:
                types[name] = annotate_type_ref(type_text)

    def _collect_locals(self, scope_node: Node, types: dict[str, str]) -> None:
        # One type map per method (as every language engine here builds), so
        # sibling blocks are not distinguished: two `{ var x = ... }` blocks
        # declaring `x` as DIFFERENT types cannot both be modelled. Rather than
        # let the last declaration win and misbind the other block's calls, a
        # name seen with conflicting types is dropped so it falls back to
        # bare-name resolution. Full block-scoped precision needs the Roslyn
        # semantic model (follow-up).
        conflicted: set[str] = set()
        for decl in self._local_variable_declarations(scope_node):
            declared = self._declared_type_name(decl)
            for declarator in decl.children:
                if declarator.type == cs.TS_CSHARP_VARIABLE_DECLARATOR:
                    self._record_local(declarator, declared, types, conflicted)

    def _declared_type_name(self, decl: Node) -> str | None:
        type_node = decl.child_by_field_name(cs.FIELD_TYPE)
        if type_node is None or type_node.type == cs.TS_CSHARP_IMPLICIT_TYPE:
            return None
        if type_text := safe_decode_text(type_node):
            return annotate_type_ref(type_text)
        return None

    def _record_local(
        self,
        declarator: Node,
        declared: str | None,
        types: dict[str, str],
        conflicted: set[str],
    ) -> None:
        var_name = safe_decode_text(declarator.child_by_field_name(cs.FIELD_NAME))
        if not var_name or var_name in conflicted:
            return
        var_type = declared or self._infer_initializer_type(declarator)
        if not var_type:
            return
        existing = types.get(var_name)
        if existing is not None and existing != var_type:
            del types[var_name]
            conflicted.add(var_name)
        else:
            types[var_name] = var_type

    def _infer_initializer_type(self, declarator: Node) -> str | None:
        # `var x = new T(...)` -> T (the object_creation `type` field). Other
        # initializers (method calls, literals) are left untyped; chained
        # return-type inference is Roslyn-follow-up territory. The initializer
        # may be a direct child of the declarator or wrapped in an
        # equals_value_clause by grammar version, so search the declarator's
        # own subtree (a lambda body is a separate scope, but an initializer
        # expression is small and self-contained).
        for node in self._descendants_of_type(
            declarator, cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION
        ):
            if type_text := safe_decode_text(node.child_by_field_name(cs.FIELD_TYPE)):
                return annotate_type_ref(type_text)
        return None

    def _field_type(self, class_qn: str, field_name: str) -> str | None:
        # The declared type of `field_name` on class_qn or any base class,
        # read from the per-class maps recorded at ingestion (so it reaches a
        # field inherited from a base in another file). Seed the BFS with every
        # partial part of the class so a field declared on ANOTHER part
        # (`helper` on P1, used in a method on P2) is found; a visited guard
        # stops a malformed inheritance cycle looping.
        seen: set[str] = set()
        queue = deque(self.csharp_partial_groups.get(class_qn) or [class_qn])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            fields = self.class_field_types.get(current)
            if fields and field_name in fields:
                return fields[field_name]
            queue.extend(self.class_inheritance.get(current, []))
        return None

    # --- typed method-call resolution ------------------------------------

    def resolve_csharp_method_call(
        self,
        call_node: Node,
        local_var_types: dict[str, str] | None,
        module_qn: str,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        # A Roslyn call fact for this exact site wins over every heuristic: it
        # is the compiler's own overload resolution (argument types, not arity)
        # and covers receivers no syntax walk can type (chained returns) plus
        # reduced extension methods. Any key miss falls through to the
        # heuristics below.
        if semantic := self._semantic_call_target(call_node, module_qn):
            return semantic
        func = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func is None:
            return None
        if func.type != cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            # A bare `Foo(...)`/`Foo<T>(...)` follows C# simple-name lookup:
            # an in-scope local function first (it shadows same-name method
            # overloads), then an arity-matched member of the enclosing
            # type. A miss falls to the generic simple-name path.
            if func.type in (cs.TS_CSHARP_IDENTIFIER, cs.TS_CSHARP_GENERIC_NAME):
                return self._resolve_bare_call(func, call_node, module_qn, caller_qn)
            return None
        method_name = safe_decode_text(func.child_by_field_name(cs.FIELD_NAME))
        receiver = func.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        if not method_name or receiver is None:
            return None
        # `Policy.Handle<TException>()`: a generic member's NAME field is a
        # generic_name; methods register generic-free, so strip the type
        # arguments or the fluent entry point never matches.
        method_name = method_name.split(cs.CHAR_ANGLE_OPEN, 1)[0]
        arg_count = self._count_arguments(call_node)

        # `base.X()` binds the BASE chain only: a first-party base's member
        # when one exists, otherwise the base is external (object.Equals in
        # Polly's hide-object-members regions) and the call must emit nothing;
        # the trie fallback was self-looping it onto the caller's own override.
        if receiver.type == cs.TS_CSHARP_BASE_EXPRESSION:
            if class_qn := self._containing_class_qn(caller_qn):
                seen: set[str] = set()
                for root in self._partial_roots(class_qn):
                    for base_qn in self.class_inheritance.get(root, []):
                        if hit := self._find_method_by_arity(
                            base_qn, method_name, arg_count, seen
                        ):
                            return cs.NodeLabel.METHOD.value, hit
                seen = set()
                for root in self._partial_roots(class_qn):
                    for base_qn in self.class_inheritance.get(root, []):
                        if hit := self._find_method_by_name(base_qn, method_name, seen):
                            return cs.NodeLabel.METHOD.value, hit
            return CSHARP_EXTERNAL_TARGET

        receiver_class_qn = self._resolve_receiver_class_qn(
            receiver, local_var_types or {}, module_qn, caller_qn
        )
        # Resolution order matters: an EXACT-ARITY instance method wins, then
        # an (always arity-exact) extension method, and only then the instance
        # name-only fallback. Trying the name-only fallback before extensions
        # would bind `c.Foo(1)` to a lone `C.Foo()` and never reach the
        # arity-correct `static Foo(this C, int)` extension.
        if receiver_class_qn is not None:
            if arity_hit := self._find_arity_across_parts(
                receiver_class_qn, method_name, arg_count
            ):
                # A delegate-typed PROPERTY registers as a METHOD node with
                # a bare (arity-0) qn, so a 0-arg invoke slips through the
                # ARITY path too: `entry.Callback()` is Delegate.Invoke,
                # not a call to the property node.
                if self.function_registry.is_property(arity_hit):
                    return CSHARP_EXTERNAL_TARGET
                return cs.NodeLabel.METHOD.value, arity_hit
        # An extension method (`static M(this T x, ...)` on an unrelated static
        # class) whose `this` receiver type matches the call's receiver; the
        # only path that binds `x.M()` to a method not in x's hierarchy.
        if ext := self._try_extension_call(
            receiver,
            local_var_types or {},
            module_qn,
            caller_qn,
            method_name,
            arg_count,
        ):
            return cs.NodeLabel.METHOD.value, ext
        if receiver_class_qn is not None:
            if name_hit := self._find_name_across_parts(receiver_class_qn, method_name):
                # A delegate-typed PROPERTY invoked with call syntax
                # (`options.ShouldHandle(args)`) is Delegate.Invoke, not a
                # method call; binding the property as a METHOD fabricates
                # an edge. Its reachability comes from the read pass.
                if self.function_registry.is_property(name_hit):
                    return CSHARP_EXTERNAL_TARGET
                return cs.NodeLabel.METHOD.value, name_hit
        # An object-virtual miss on a TYPED receiver (`severity.ToString()`
        # on an enum, `options.GetType()`) resolves to System.Object/Enum;
        # falling to the trie lands on whatever unrelated
        # hide-object-members override exists (Polly's PolicyBuilder).
        if method_name in cs.CSHARP_OBJECT_VIRTUALS:
            return CSHARP_EXTERNAL_TARGET
        if receiver_class_qn is None and self._externally_targeted(
            receiver, method_name, local_var_types or {}, caller_qn
        ):
            return CSHARP_EXTERNAL_TARGET
        return None

    def _externally_targeted(
        self,
        receiver: Node,
        method_name: str,
        local_var_types: dict[str, str],
        caller_qn: str | None,
    ) -> bool:
        # Only for UNTYPED receivers (a typed miss keeps today's trie
        # rescue): (a) a PascalCase identifier/dotted path that is no
        # local, no field, and no registered type is an external TYPE
        # (`Console`, `System.Console`); (b) an object-virtual member name
        # on an untyped receiver resolves to System.Object.
        if method_name in cs.CSHARP_OBJECT_VIRTUALS:
            return True
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None:
            return False
        receiver = unwrapped
        if receiver.type not in (
            cs.TS_CSHARP_IDENTIFIER,
            cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION,
        ):
            return False
        text = safe_decode_text(receiver)
        if not text:
            return False
        segments = text.split(cs.SEPARATOR_DOT)
        head = segments[0]
        if head in local_var_types:
            # A local/param (any casing) whose DECLARED type resolves to
            # no registered type (`string[]`, reflection FieldInfo) is
            # external; its members cannot be attributed by name
            # (`names.Contains(x)` bound Context.Contains).
            return not self._registered_type_declares(
                local_var_types[head], method_name
            )
        if not all(seg[:1].isupper() for seg in segments):
            return False
        if class_qn := self._containing_class_qn(caller_qn):
            verdict = self._enclosing_member_external(
                class_qn, head, method_name, caller_qn
            )
            if verdict is not None:
                return verdict
        # Any registered type with this simple name means the receiver may
        # be first-party (even when twin ambiguity kept it untyped).
        if any(
            self.function_registry.get(qn) in _TYPE_DECLS
            for qn in self.simple_name_lookup.get(head, set())
        ):
            return False
        return True

    def _enclosing_member_external(
        self,
        class_qn: str,
        head: str,
        method_name: str,
        caller_qn: str | None,
    ) -> bool | None:
        # Tri-state: True/False decide externality from what the enclosing
        # type knows about the receiver head; None leaves it to the
        # registered-simple-name sweep.
        if member_type := self._field_type(class_qn, head):
            # A field/property receiver whose declared type is external
            # (`Wrapper` of BCL RateLimiter) makes the call external (the
            # trie self-looped `Wrapper.DisposeAsync()` onto the enclosing
            # class).
            return not self._registered_type_declares(member_type, method_name)
        if prop_qn := self.resolve_property_read(head, caller_qn):
            if entry := self.csharp_method_return_types.get(prop_qn):
                return not self._registered_type_declares(entry[0], method_name)
        # A PascalCase receiver is very often a PROPERTY or member of
        # the enclosing type (`Pipeline.Execute(...)`); anything the
        # enclosing type declares by that name is first-party, not an
        # external type.
        if self._find_name_across_parts(class_qn, head) is not None:
            return False
        return None

    def _resolve_bare_call(
        self,
        func: Node,
        call_node: Node,
        module_qn: str,
        caller_qn: str | None,
    ) -> tuple[str, str] | None:
        name = safe_decode_text(func)
        if not name:
            return None
        # `Handle<TException>(...)`: the callee name is the identifier
        # without its type arguments (matching how methods register).
        name = name.split(cs.CHAR_ANGLE_OPEN, 1)[0]
        arg_count = self._count_arguments(call_node)
        if local := self._find_in_scope_local_function(
            name, arg_count, caller_qn, module_qn
        ):
            return cs.NodeLabel.FUNCTION.value, local
        # Same-arity twins (`M(X) => M<Void>(x)` beside `M<T>(X)` on another
        # partial part, Polly's ResiliencePipeline Get*/Initialize*Context):
        # parameter arity cannot tell them apart, so prefer the overload
        # whose GENERICNESS matches the callee shape (`M<TResult>(...)` is a
        # generic_name, `M(...)` a plain identifier).
        generic_call = func.type == cs.TS_CSHARP_GENERIC_NAME
        for class_qn in self._caller_class_candidates(caller_qn, module_qn):
            matches = self._find_arity_matches_across_parts(class_qn, name, arg_count)
            if matches:
                preferred = [
                    m
                    for m in matches
                    if (m in self.csharp_generic_methods) == generic_call
                ]
                return cs.NodeLabel.METHOD.value, (preferred or matches)[0]
        # A bare object-virtual with no local declaration is
        # `this.GetType()` -> System.Object (Polly's PolicyBase.PolicyKey);
        # a bare name that IS a delegate-typed member of the enclosing type
        # (`Callback();` on a record positional property) is
        # Delegate.Invoke. Neither may fall to the bare-name trie.
        if name in cs.CSHARP_OBJECT_VIRTUALS:
            return CSHARP_EXTERNAL_TARGET
        if self.resolve_property_read(name, caller_qn) is not None:
            return CSHARP_EXTERNAL_TARGET
        if (class_qn := self._containing_class_qn(caller_qn)) and self._field_type(
            class_qn, name
        ):
            return CSHARP_EXTERNAL_TARGET
        return None

    def _find_in_scope_local_function(
        self, name: str, arg_count: int, caller_qn: str | None, module_qn: str
    ) -> str | None:
        # Walk the caller's scope chain probing for a registered local
        # function. Each level is probed both as-is and with the overload
        # signature suffix stripped, because local functions register under the
        # BARE method scope name while the caller_qn carries the host overload's
        # signatured identity (`Handle(System.Func)` hosts `Handle.Handle`). A
        # hit must match the call's arity AND be declared in a host the caller
        # sits inside (C# scoping), without which the parameterless sibling
        # overload would capture the local fn textually nested under its own
        # bare qn.
        if not caller_qn:
            return None
        scope = caller_qn
        while len(scope) > len(module_qn):
            stripped = scope.split(cs.CHAR_PAREN_OPEN, 1)[0]
            for probe_scope in dict.fromkeys((scope, stripped)):
                candidate = f"{probe_scope}{cs.SEPARATOR_DOT}{name}"
                # Same-name local fns in SIBLING BLOCKS flatten to one scope
                # qn; later declarations carry an `@line` duplicate suffix,
                # so probe every registered variant, not just the natural qn
                # (else an arity-matched later declaration is missed and the
                # arity-blind fallback's duplicate fan-out fabricates a
                # phantom edge onto the uncalled sibling).
                for variant in self.function_registry.variants(candidate):
                    entry = self.csharp_local_functions.get(variant)
                    if (
                        entry is not None
                        and entry[1] == arg_count
                        and self._caller_within_host(caller_qn, entry[0])
                    ):
                        return variant
            if cs.SEPARATOR_DOT not in stripped:
                return None
            scope = stripped.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return None

    def csharp_local_function_group(
        self, name: str, caller_qn: str | None, module_qn: str
    ) -> list[str]:
        # Every in-scope local function with this name, arity-blind: a
        # method GROUP argument (`return new(..., Dispose)` handing
        # Serilog's CreateLogger locals to the Logger ctor) carries no call
        # arity, so the whole registered group at the nearest declaring
        # scope is the referenced set. Locals shadow members, matching
        # _find_in_scope_local_function's scope-chain discipline.
        if not caller_qn:
            return []
        scope = caller_qn
        while len(scope) > len(module_qn):
            stripped = scope.split(cs.CHAR_PAREN_OPEN, 1)[0]
            if matches := self._local_function_group_at_scope(
                name, caller_qn, scope, stripped
            ):
                return matches
            if cs.SEPARATOR_DOT not in stripped:
                return []
            scope = stripped.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return []

    def _local_function_group_at_scope(
        self, name: str, caller_qn: str, scope: str, stripped: str
    ) -> list[str]:
        matches: list[str] = []
        for probe_scope in dict.fromkeys((scope, stripped)):
            candidate = f"{probe_scope}{cs.SEPARATOR_DOT}{name}"
            for variant in self.function_registry.variants(candidate):
                entry = self.csharp_local_functions.get(variant)
                if entry is not None and self._caller_within_host(caller_qn, entry[0]):
                    matches.append(variant)
        return matches

    def _caller_within_host(self, caller_qn: str, host_key: FunctionSpanKey) -> bool:
        # True when the caller IS the local function's host scope or a local
        # function transitively hosted inside it (a sibling or nested local
        # fn calling across/into its own nest). Spans join lazily against
        # function_locations because the host's signatured identity was not
        # registered yet when the local function was pinned.
        host_loc = self.function_locations.get(host_key)
        if host_loc is None:
            return False
        host_qn = host_loc.qualified_name
        seen: set[str] = set()
        current = caller_qn
        while current not in seen:
            seen.add(current)
            if current == host_qn:
                return True
            entry = self.csharp_local_functions.get(current)
            if entry is None:
                return False
            next_loc = self.function_locations.get(entry[0])
            if next_loc is None:
                return False
            current = next_loc.qualified_name
        return False

    def _caller_class_candidates(
        self, caller_qn: str | None, module_qn: str
    ) -> Iterator[str]:
        # Enclosing-type candidates for a bare member call, outermost last:
        # strip the overload signature (only the leaf carries one, and its
        # qualified parameter types contain dots that would break a plain
        # rsplit), then peel scope segments down to the module boundary.
        if not caller_qn:
            return
        scope = caller_qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
        while cs.SEPARATOR_DOT in scope:
            scope = scope.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if len(scope) <= len(module_qn):
                return
            yield scope

    def resolve_property_read(self, name: str, caller_qn: str | None) -> str | None:
        # A bare-identifier read (`WrappedDictionary.Keys`) targets a
        # property of the caller's enclosing type (implicit this); resolve
        # across partial parts and base classes and accept ONLY a
        # registered property, so same-name methods stay out of the read
        # pass.
        class_qn = self._containing_class_qn(caller_qn)
        if class_qn is None:
            return None
        qn = self._find_name_across_parts(class_qn, name)
        if qn is not None and self.function_registry.is_property(qn):
            return qn
        return None

    def resolve_member_property_read(
        self, receiver_type: str, name: str, module_qn: str
    ) -> str | None:
        # `Cfg.Value` / `w.Inner`: the NAME field read resolved against the
        # receiver's type (a class name for a static read, an inferred
        # local/parameter type for an instance read). Accepts ONLY a
        # registered property; an unresolvable receiver yields nothing, so
        # unrelated `x.Value` chains never fabricate an edge.
        class_qn = self._type_name_to_qn(receiver_type, module_qn)
        if class_qn is None:
            return None
        qn = self._find_name_across_parts(class_qn, name)
        if qn is not None and self.function_registry.is_property(qn):
            return qn
        return None

    def semantic_fact_resolved(self, call_node: Node, module_qn: str) -> bool:
        # True when a Roslyn call fact pinned this exact site: the target is
        # the compiler's own overload choice, so arity-based widening (the
        # same-arity family fan-out) must stay off for it.
        return self._semantic_call_target(call_node, module_qn) is not None

    def _semantic_call_target(
        self, call_node: Node, module_qn: str
    ) -> tuple[str, str] | None:
        if not (self.csharp_call_sites or self.csharp_external_sites):
            return None
        key = self._call_site_key(call_node, module_qn)
        if key is None:
            return None
        fact = self.csharp_call_sites.get(key)
        if fact is not None:
            return self._declared_location(
                fact.target_file, fact.target_line, fact.target_col
            )
        if key in self.csharp_external_sites:
            # Roslyn resolved this site to a METADATA method: the call
            # provably leaves the repo, so return the external sentinel and
            # keep the name trie from fabricating a first-party edge (the
            # untypeable-receiver fp class the pure frontend cannot see).
            return CSHARP_EXTERNAL_TARGET
        return None

    def _call_site_key(self, call_node: Node, module_qn: str) -> CallSiteKey | None:
        name_node = self._callee_name_node(call_node)
        if name_node is None:
            return None
        name = safe_decode_text(name_node)
        if not name:
            return None
        file_path = self.module_qn_to_file_path.get(module_qn)
        if file_path is None:
            return None
        rel = cached_relative_path(file_path, self.repo_path).as_posix()
        # Keyed on the callee NAME token (nested invocations share an
        # expression start, never a name token), with generic arguments
        # stripped to match Roslyn's symbol name.
        return (
            rel,
            name_node.start_point[0] + 1,
            name_node.start_point[1],
            name.split(cs.CHAR_ANGLE_OPEN, 1)[0],
        )

    def _callee_name_node(self, call_node: Node) -> Node | None:
        func = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func is None:
            return None
        if func.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            return func.child_by_field_name(cs.FIELD_NAME)
        if func.type in (cs.TS_CSHARP_IDENTIFIER, cs.TS_CSHARP_GENERIC_NAME):
            return func
        if func.type == cs.TS_CSHARP_CONDITIONAL_ACCESS_EXPRESSION:
            # `recv?.Method(...)`: the name lives on the member_binding child
            # (same token Roslyn keys its MemberBindingExpressionSyntax fact
            # on).
            binding = next(
                (
                    child
                    for child in func.children
                    if child.type == cs.TS_CSHARP_MEMBER_BINDING_EXPRESSION
                ),
                None,
            )
            if binding is not None:
                return binding.child_by_field_name(cs.FIELD_NAME)
        return None

    def _declared_location(
        self, rel_file: str, line: int, col: int
    ) -> tuple[str, str] | None:
        # The fact's target declaration location resolves through the exact
        # (module_qn, start_line, start_col) record Pass 2 registered, so the
        # returned label/qn are the ingested node's, signature included.
        target_module = self._module_qn_for_rel_file(rel_file)
        if target_module is None:
            return None
        location = self.function_locations.get((target_module, line, col))
        if location is None:
            return None
        return location.label, location.qualified_name

    def _module_qn_for_rel_file(self, rel_file: str) -> str | None:
        # Lazy inverse of module_qn_to_file_path (which Pass 2 fills after
        # this engine is constructed); rebuilt when new modules appeared.
        if len(self._rel_to_module) != len(self.module_qn_to_file_path):
            self._rel_to_module = {
                cached_relative_path(path, self.repo_path).as_posix(): qn
                for qn, path in self.module_qn_to_file_path.items()
            }
        return self._rel_to_module.get(rel_file)

    def _try_extension_call(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
        method_name: str,
        arg_count: int,
    ) -> str | None:
        if not self.csharp_extension_methods:
            return None
        type_name = self._receiver_type_name(
            receiver, local_var_types, module_qn, caller_qn
        )
        if not type_name:
            return None
        return self._find_extension_method(
            type_name,
            method_name,
            arg_count,
            self._receiver_type_arity(receiver, local_var_types, module_qn, caller_qn),
        )

    def _receiver_type_name(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        # The receiver's declared type NAME (not its class qn), needed for the
        # extension-method fallback: extensions frequently target BCL types
        # (`string`, `int`) that are never registered as classes, so the raw
        # type name is all we can match on. Mirrors _resolve_receiver_class_qn's
        # branches but stops at the name.
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None:
            return None
        receiver = unwrapped
        # `((Widget)o).Ext()`: the cast target IS the receiver type, so an
        # extension-only method still binds on a cast receiver; same for a
        # `new Widget(...)` receiver.
        if receiver.type in (
            cs.TS_CSHARP_CAST_EXPRESSION,
            cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION,
        ):
            return self._annotated_type_field(receiver)
        # Same chained-receiver typing as the instance path, stopping at the
        # (arity-annotated) type name; extensions often target unregistered BCL
        # types like `string`, and both sides of the matcher carry the
        # annotation consistently.
        if receiver.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
            return self._invocation_return_type_name(
                receiver, local_var_types, module_qn, caller_qn
            )
        if receiver.type == cs.TS_CSHARP_THIS:
            return self._this_receiver_type(module_qn, caller_qn)
        if receiver.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            return self._this_field_receiver_type(receiver, caller_qn)
        if receiver.type == cs.TS_CSHARP_IDENTIFIER:
            return self._identifier_receiver_type(receiver, local_var_types, caller_qn)
        return None

    def _annotated_type_field(self, receiver: Node) -> str | None:
        type_node = receiver.child_by_field_name(cs.FIELD_TYPE)
        raw = safe_decode_text(type_node) if type_node else None
        return annotate_type_ref(raw) if raw else None

    def _invocation_return_type_name(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        inner = self.resolve_csharp_method_call(
            receiver, local_var_types, module_qn, caller_qn
        )
        if inner is None:
            return None
        if entry := self.csharp_method_return_types.get(inner[1]):
            rname, rarity = entry
            return f"{rname}`{rarity}" if rarity else rname
        return None

    def _unwrap_receiver(self, receiver: Node) -> Node | None:
        # Peel interleaved parens and null-forgiving postfix wrappers
        # (`((Component)s)!` puts the `!` OUTSIDE the parens) to a fixpoint.
        while receiver.type in (
            cs.TS_PARENTHESIZED_EXPRESSION,
            cs.TS_CSHARP_POSTFIX_UNARY_EXPRESSION,
        ):
            inner = receiver.named_children[0] if receiver.named_children else None
            if inner is None:
                return None
            receiver = inner
        return receiver

    def _this_receiver_type(self, module_qn: str, caller_qn: str | None) -> str | None:
        qn = self._containing_class_qn(caller_qn)
        if qn is None:
            return None
        # `this` names the exact containing class, so keep its
        # namespace-qualified form (`N1.Widget`, module prefix stripped) rather
        # than the bare simple name: that lets the matcher bind an exact `this
        # N1.Widget` extension even when another `N2.Widget` exists.
        if qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}"):
            return qn[len(module_qn) + 1 :]
        return qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]

    def _this_field_receiver_type(
        self, receiver: Node, caller_qn: str | None
    ) -> str | None:
        expr = receiver.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        field = safe_decode_text(receiver.child_by_field_name(cs.FIELD_NAME))
        if expr is not None and expr.type == cs.TS_CSHARP_THIS and field:
            if class_qn := self._containing_class_qn(caller_qn):
                return self._field_type(class_qn, field)
        return None

    def _identifier_receiver_type(
        self, receiver: Node, local_var_types: dict[str, str], caller_qn: str | None
    ) -> str | None:
        name = safe_decode_text(receiver)
        if not name:
            return None
        if (type_name := local_var_types.get(name)) is not None:
            return type_name
        if class_qn := self._containing_class_qn(caller_qn):
            if ftype := self._field_type(class_qn, name):
                return ftype
        # An unknown bare identifier is a TYPE name (a static call
        # `Widget.M()`), not an instance; extension methods bind on instances
        # only, so do NOT treat it as an extension receiver (else `Widget.Poke()`
        # would wrongly bind `static Poke(this Widget)`, invalid in C#).
        return None

    def _receiver_type_arity(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> int | None:
        # The receiver's WRITTEN generic arity where it is knowable
        # (object-creation/cast text, a chained call's recorded return, or
        # `this` inside a generic type); None when unknowable (an untyped
        # identifier), which skips the arity gate rather than guessing.
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None:
            return None
        receiver = unwrapped
        if receiver.type == cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION:
            type_node = receiver.child_by_field_name(cs.FIELD_TYPE)
            raw = safe_decode_text(type_node) if type_node else None
            return generic_arity_of_type_text(raw) if raw else None
        if receiver.type == cs.TS_CSHARP_CAST_EXPRESSION:
            type_node = receiver.child_by_field_name(cs.FIELD_TYPE)
            raw = safe_decode_text(type_node) if type_node else None
            return generic_arity_of_type_text(raw) if raw else None
        if receiver.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
            # Full caller context: locals and imports participate in the
            # inner resolution exactly as they did for the instance path.
            inner = self.resolve_csharp_method_call(
                receiver, local_var_types, module_qn, caller_qn
            )
            if inner is not None and (
                entry := self.csharp_method_return_types.get(inner[1])
            ):
                return entry[1]
            return None
        if receiver.type == cs.TS_CSHARP_THIS:
            class_qn = self._containing_class_qn(caller_qn)
            if class_qn is not None:
                return self.csharp_class_generic_arity.get(class_qn, 0)
        return None

    def _registered_type_declares(self, type_name: str, method_name: str) -> bool:
        # A registered type merely SHARING the receiver type's simple name
        # (Polly's Snippets.Docs.RateLimiter demo class vs BCL RateLimiter)
        # must not defeat the external gate: the candidate counts only if
        # it actually DECLARES the called member somewhere in its
        # parts/bases.
        simple = split_type_ref(type_name)[0].rsplit(cs.SEPARATOR_DOT, 1)[-1]
        for qn in self.simple_name_lookup.get(simple, set()):
            if self.function_registry.get(qn) in _TYPE_DECLS:
                if self._find_name_across_parts(qn, method_name) is not None:
                    return True
        return False

    def _find_extension_method(
        self,
        receiver_type_name: str,
        method_name: str,
        arg_count: int,
        receiver_arity: int | None = None,
    ) -> str | None:
        candidates = self.csharp_extension_methods.get(method_name)
        if not candidates:
            return None
        recv_simple = receiver_type_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        recv_qualified = cs.SEPARATOR_DOT in receiver_type_name
        # An UNqualified receiver whose simple name maps to more than one
        # registered first-party type (`N1.Widget` vs `N2.Widget`) is
        # genuinely ambiguous, since we can't tell which one it is, so an
        # unqualified-vs-unqualified match must not guess. A qualified receiver
        # or a BCL name (not registered) is not affected.
        same_name_decls = [
            qn
            for qn in self.simple_name_lookup.get(recv_simple, set())
            if self.function_registry.get(qn) in _TYPE_DECLS
        ]
        # Same-name declarations that all differ by GENERIC ARITY (`Builder`
        # beside `Builder<TResult>`, Polly's dual pipeline builders) are not
        # the namespace ambiguity this guard exists for: a compilable call
        # binds the unique matching extension regardless of which twin the
        # receiver is. Only same-arity twins (true `N1.Widget`/`N2.Widget`
        # namespace splits) stay ambiguous.
        distinct_arities = {
            self.csharp_class_generic_arity.get(qn, 0) for qn in same_name_decls
        }
        ambiguous_unqualified = (
            not recv_qualified
            and len(same_name_decls) > 1
            and len(distinct_arities) != len(same_name_decls)
        )
        matches: list[str] = []
        for qn, recv_type, ext_namespace, cand_recv_arity in candidates:
            # A receiver of KNOWN written arity never binds an extension
            # declared for the other generic twin (`new Builder<int>()`
            # cannot take a `this Builder` extension).
            if receiver_arity is not None and cand_recv_arity != receiver_arity:
                continue
            # `_arity` reads the first `(`/last `)`, so pass the whole qn; a
            # leaf-split on `.` would land inside a qualified param type.
            if _arity(qn) != arg_count + 1:
                continue
            if recv_type.rsplit(cs.SEPARATOR_DOT, 1)[-1] != recv_simple:
                continue
            cand_qualified = cs.SEPARATOR_DOT in recv_type
            # Namespace consistency between the call receiver and the stored
            # `this` type, by qualification:
            #  - both qualified: require the SAME fully-qualified name
            #    (`N1.Widget` binds `this N1.Widget`, never `this N2.Widget`);
            #  - recv qualified, cand not: resolve the ext's unqualified
            #    `this Widget` to `<ext-namespace>.Widget` and require equality
            #    (`N.Widget` binds a same-namespace `this Widget`);
            #  - recv unqualified, cand qualified: the receiver's namespace is
            #    unknown without a semantic model, so don't guess;
            #  - both unqualified: match by simple name unless it's ambiguous.
            if recv_qualified and cand_qualified:
                if recv_type != receiver_type_name:
                    continue
            elif recv_qualified and not cand_qualified:
                cand_qualified_name = (
                    f"{ext_namespace}{cs.SEPARATOR_DOT}{recv_type}"
                    if ext_namespace
                    else recv_type
                )
                if cand_qualified_name != receiver_type_name:
                    continue
            elif cand_qualified:  # recv unqualified, cand qualified
                continue
            elif ambiguous_unqualified:
                continue
            matches.append(qn)
        # Bind only on a unique match; an ambiguous name across static classes
        # is left unresolved rather than guessed.
        return matches[0] if len(matches) == 1 else None

    def _count_arguments(self, call_node: Node) -> int:
        arg_list = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if arg_list is None:
            return 0
        return sum(1 for c in arg_list.children if c.type == cs.TS_CSHARP_ARGUMENT)

    def _resolve_receiver_class_qn(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        # A cast receiver `((Component)s!).Reload()` (Polly's
        # CancellationToken.Register callback): the cast TYPE is the
        # receiver's type by construction (mirrors the Java cast-receiver
        # handling).
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None:
            return None
        receiver = unwrapped
        if receiver.type == cs.TS_CSHARP_CAST_EXPRESSION:
            type_node = receiver.child_by_field_name(cs.FIELD_TYPE)
            raw = safe_decode_text(type_node) if type_node else None
            if raw:
                # The cast's WRITTEN arity picks between simple-name twins
                # (`(Opt<int>)o` names the generic Opt<T>, never plain Opt).
                return self._type_name_to_qn(
                    _normalize_type_name(raw),
                    module_qn,
                    generic_arity_of_type_text(raw),
                )
            return None
        # `new Builder().Add()`: an object-creation receiver IS its type.
        if receiver.type == cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION:
            type_node = receiver.child_by_field_name(cs.FIELD_TYPE)
            if type_text := safe_decode_text(type_node) if type_node else None:
                return self._type_name_to_qn(
                    _normalize_type_name(type_text),
                    module_qn,
                    generic_arity_of_type_text(type_text),
                )
            return None
        # `Policy.Handle<T>().Wrap(...)`: an invocation receiver types the
        # next hop via the resolved inner call's recorded return type
        # (Polly's whole fluent surface). Depth is bounded by chain length.
        if receiver.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
            inner = self.resolve_csharp_method_call(
                receiver, local_var_types, module_qn, caller_qn
            )
            if inner is None:
                return None
            if entry := self.csharp_method_return_types.get(inner[1]):
                rtype, rarity = entry
                return self._type_name_to_qn(rtype, module_qn, rarity)
            return None
        if receiver.type == cs.TS_CSHARP_THIS:
            return self._containing_class_qn(caller_qn)
        # An explicit `this.field` receiver: the field's (possibly inherited)
        # type on the enclosing class.
        if receiver.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            expr = receiver.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
            field = safe_decode_text(receiver.child_by_field_name(cs.FIELD_NAME))
            if expr is not None and expr.type == cs.TS_CSHARP_THIS and field:
                if class_qn := self._containing_class_qn(caller_qn):
                    if ftype := self._field_type(class_qn, field):
                        return self._type_name_to_qn(ftype, module_qn)
            return None
        if receiver.type == cs.TS_CSHARP_IDENTIFIER:
            name = safe_decode_text(receiver)
            if not name:
                return None
            # A local/parameter of a known type resolves via its type; else a
            # bare (possibly inherited) field of the enclosing class; else the
            # receiver may itself be a type name (a static call `Foo.Bar()`).
            type_name = local_var_types.get(name)
            if type_name is not None:
                return self._type_name_to_qn(type_name, module_qn)
            if class_qn := self._containing_class_qn(caller_qn):
                if ftype := self._field_type(class_qn, name):
                    return self._type_name_to_qn(ftype, module_qn)
            return self._type_name_to_qn(name, module_qn)
        return None

    def _containing_class_qn(self, caller_qn: str | None) -> str | None:
        if not caller_qn:
            return None
        # Strip any parameter signature before splitting off the method leaf,
        # so a qualified param type (`M(System.String)`) does not fool rsplit.
        base = caller_qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
        class_qn = base.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return class_qn if self.function_registry.get(class_qn) in _TYPE_DECLS else None

    def _type_name_to_qn(
        self,
        type_name: str,
        module_qn: str,
        generic_arity: int | None = None,
    ) -> str | None:
        # Stored type refs carry their written generic arity CLR-style
        # (`Options`0` is implicit: plain means arity 0); parse it so twin
        # filtering works for every map-sourced reference.
        if generic_arity is None:
            type_name, generic_arity = split_type_ref(type_name)
        # An already-qualified name that IS a registered type resolves directly,
        # skipping the ambiguous simple-name sweep.
        if self.function_registry.get(type_name) in _TYPE_DECLS:
            return type_name
        simple = type_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        import_map = self.import_processor.import_mapping.get(module_qn)
        if import_map and (mapped := import_map.get(simple)):
            if self.function_registry.get(mapped) in _TYPE_DECLS:
                return mapped
        candidates = [
            qn
            for qn in self.simple_name_lookup.get(simple, set())
            if self.function_registry.get(qn) in _TYPE_DECLS
        ]
        return self._disambiguate_type_candidates(candidates, generic_arity, module_qn)

    def _disambiguate_type_candidates(
        self,
        candidates: list[str],
        generic_arity: int | None,
        module_qn: str,
    ) -> str | None:
        # `Builder` vs `Builder<TResult>` share a simple name; when the
        # reference's WRITTEN generic arity is known, keep only the
        # declarations with that type-parameter count (Polly's dual
        # builders, where the ambiguity killed every fluent second hop).
        if generic_arity is not None and len(candidates) > 1:
            arity_matched = [
                qn
                for qn in candidates
                if self.csharp_class_generic_arity.get(qn, 0) == generic_arity
            ]
            if arity_matched:
                candidates = arity_matched
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # Several candidates that are all parts of ONE partial class are a
            # single logical type, not a real ambiguity; return one part (method
            # resolution then spans the whole group).
            if part := self._single_partial_group_member(candidates):
                return part
            # Prefer a candidate in the calling file's module; ambiguity across
            # unrelated files is left unresolved rather than guessed.
            same_module = [q for q in candidates if q.startswith(f"{module_qn}.")]
            if len(same_module) == 1:
                return same_module[0]
        return None

    def _single_partial_group_member(self, candidates: list[str]) -> str | None:
        # If every candidate belongs to the SAME partial-class group, they are
        # one logical type; return its lexicographically-first part (stable
        # across runs). A candidate outside the group (its group is None) means
        # a genuine cross-type ambiguity, left unresolved.
        group = self.csharp_partial_groups.get(candidates[0])
        if group is None:
            return None
        if all(self.csharp_partial_groups.get(c) is group for c in candidates):
            return min(group)
        return None

    # An exact-arity match ANYWHERE up the hierarchy (_find_arity_across_parts)
    # wins before any same-name fallback, so an inherited correct-arity overload
    # (`Base.Foo(int, int)`) is not lost to a wrong-arity same-name method
    # (`Derived.Foo(int)`). resolve_csharp_method_call sequences the two around
    # the extension-method lookup so an arity-correct extension beats a
    # lone-same-name instance fallback. Both phases span every part of a partial
    # class (and each part's bases), so a member/base on another part binds.
    def _partial_roots(self, class_qn: str) -> list[str]:
        return self.csharp_partial_groups.get(class_qn) or [class_qn]

    def _find_arity_across_parts(
        self, class_qn: str, method_name: str, arg_count: int
    ) -> str | None:
        seen: set[str] = set()
        for root in self._partial_roots(class_qn):
            if resolved := self._find_method_by_arity(
                root, method_name, arg_count, seen
            ):
                return resolved
        return None

    def _find_arity_matches_across_parts(
        self, class_qn: str, method_name: str, arg_count: int
    ) -> list[str]:
        # ALL exact-arity same-name overloads across partial parts and
        # bases (the single-hit variant returns the first, which is
        # arbitrary when same-arity twins exist).
        seen: set[str] = set()
        out: list[str] = []
        for root in self._partial_roots(class_qn):
            self._collect_arity_matches(root, method_name, arg_count, seen, out)
        return out

    def _collect_arity_matches(
        self,
        class_qn: str,
        method_name: str,
        arg_count: int,
        seen: set[str],
        out: list[str],
    ) -> None:
        if class_qn in seen:
            return
        seen.add(class_qn)
        prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
        out.extend(
            qn
            for qn in self._direct_same_name_methods(class_qn, method_name)
            if _arity(qn[len(prefix) :]) == arg_count
        )
        for base_qn in self.class_inheritance.get(class_qn, []):
            self._collect_arity_matches(base_qn, method_name, arg_count, seen, out)

    def csharp_same_arity_family(self, method_qn: str) -> list[str]:
        # Signature-suffixed siblings of a resolved bare call that differ
        # only in parameter TYPES: a switch-arm dispatch
        # (`FormatExact(i, o)` / `FormatExact(s, o)`) is untypeable by
        # arity alone, so the whole same-arity family stays reachable.
        if cs.CHAR_PAREN_OPEN not in method_qn:
            return []
        base = method_qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
        if cs.SEPARATOR_DOT not in base:
            return []
        class_qn, name = base.rsplit(cs.SEPARATOR_DOT, 1)
        return [
            qn
            for qn in self._find_arity_matches_across_parts(
                class_qn, name, _arity(method_qn)
            )
            if qn != method_qn
        ]

    def csharp_method_group_family(self, name: str, caller_qn: str | None) -> list[str]:
        # Every same-name METHOD of the caller's enclosing type (across
        # partial parts and base classes): a bare method-group pass binds
        # the enclosing type's method group, and which overload the
        # delegate selects is invisible to syntax, so the whole family is
        # referenced. Properties are excluded (a method group never names
        # a property).
        class_qn = self._containing_class_qn(caller_qn)
        if class_qn is None:
            return []
        seen: set[str] = set()
        out: list[str] = []
        queue = deque(self._partial_roots(class_qn))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            out.extend(
                qn
                for qn in self._direct_same_name_methods(current, name)
                if not self.function_registry.is_property(qn)
            )
            queue.extend(self.class_inheritance.get(current, []))
        return sorted(set(out))

    def _find_name_across_parts(self, class_qn: str, method_name: str) -> str | None:
        seen: set[str] = set()
        for root in self._partial_roots(class_qn):
            if resolved := self._find_method_by_name(root, method_name, seen):
                return resolved
        return None

    def _direct_same_name_methods(self, class_qn: str, method_name: str) -> list[str]:
        prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
        matches: list[str] = []
        for qn, node_type in self.function_registry.find_with_prefix(class_qn):
            if node_type != NodeType.METHOD or not qn.startswith(prefix):
                continue
            leaf = qn[len(prefix) :]
            base = leaf.split(cs.CHAR_PAREN_OPEN, 1)[0]
            # Directly on this class only (a nested class's method has an extra
            # dot in its name portion).
            if cs.SEPARATOR_DOT in base or base != method_name:
                continue
            matches.append(qn)
        return matches

    def _find_method_by_arity(
        self, class_qn: str, method_name: str, arg_count: int, seen: set[str]
    ) -> str | None:
        if class_qn in seen:
            return None
        seen.add(class_qn)
        prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
        for qn in self._direct_same_name_methods(class_qn, method_name):
            if _arity(qn[len(prefix) :]) == arg_count:
                return qn
        for base_qn in self.class_inheritance.get(class_qn, []):
            if resolved := self._find_method_by_arity(
                base_qn, method_name, arg_count, seen
            ):
                return resolved
        return None

    def _find_method_by_name(
        self, class_qn: str, method_name: str, seen: set[str]
    ) -> str | None:
        if class_qn in seen:
            return None
        seen.add(class_qn)
        same_name = self._direct_same_name_methods(class_qn, method_name)
        if len(same_name) == 1:
            return same_name[0]
        for base_qn in self.class_inheritance.get(class_qn, []):
            if resolved := self._find_method_by_name(base_qn, method_name, seen):
                return resolved
        return None

    # --- ast helpers ------------------------------------------------------

    def _descendants_of_type(self, node: Node, node_type: str) -> list[Node]:
        found: list[Node] = []
        stack = list(node.children)
        while stack:
            current = stack.pop()
            if current.type == node_type:
                found.append(current)
            stack.extend(current.children)
        return found

    def _local_variable_declarations(self, scope_node: Node) -> list[Node]:
        # Every variable_declaration lexically in this method's own scope,
        # pruning nested callables (lambdas, local functions, anonymous
        # methods): their locals belong to a separate scope and must not leak
        # into or shadow the enclosing method's type map.
        found: list[Node] = []
        stack = list(scope_node.children)
        while stack:
            current = stack.pop()
            if current.type in cs.TS_CSHARP_NESTED_SCOPE_TYPES:
                continue
            if current.type == cs.TS_CSHARP_VARIABLE_DECLARATION:
                found.append(current)
            stack.extend(current.children)
        return found
