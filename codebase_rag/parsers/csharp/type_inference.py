from __future__ import annotations

from collections import deque
from collections.abc import Iterator
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
from .utils import _normalize_type_name

if TYPE_CHECKING:
    from ..factory import ASTCacheProtocol

_TYPE_DECLS = (NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM)


def _arity(leaf: str) -> int:
    # (H) Parameter count of a (possibly signatured) method leaf: `M(int, string)`
    # (H) -> 2, `M` / `M()` -> 0. Only depth-0 commas separate parameters, so a
    # (H) qualified/array type never inflates the count.
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
        "csharp_local_functions",
        "csharp_generic_methods",
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
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        module_qn_to_file_path: dict[str, Path],
        class_inheritance: dict[str, list[str]],
        simple_name_lookup: SimpleNameLookup,
        class_field_types: dict[str, dict[str, str]],
        csharp_partial_groups: dict[str, list[str]] | None = None,
        csharp_extension_methods: dict[str, list[tuple[str, str, str]]] | None = None,
        csharp_call_sites: dict[CallSiteKey, CSharpCallSite] | None = None,
        csharp_local_functions: dict[str, tuple[FunctionSpanKey, int]] | None = None,
        csharp_generic_methods: set[str] | None = None,
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
        # (H) Shared references (populated by the Roslyn frontend / Pass 2 after
        # (H) this engine is constructed), so `or {}` would lose them.
        self.csharp_call_sites = (
            csharp_call_sites if csharp_call_sites is not None else {}
        )
        self.csharp_local_functions = (
            csharp_local_functions if csharp_local_functions is not None else {}
        )
        self.csharp_generic_methods = (
            csharp_generic_methods if csharp_generic_methods is not None else set()
        )
        self.function_locations = (
            function_locations if function_locations is not None else {}
        )
        self._rel_to_module: dict[str, str] = {}

    # (H) --- variable/field/parameter type map -------------------------------

    def build_variable_type_map(self, scope_node: Node) -> dict[str, str]:
        # (H) Parameters and locals only. Field types are looked up at resolve
        # (H) time against class_field_types (keyed by class qn), which also
        # (H) reaches fields inherited from a base class in another file -- the
        # (H) enclosing class qn is not known here, only at the call site.
        types: dict[str, str] = {}
        self._collect_parameters(scope_node, types)
        self._collect_locals(scope_node, types)
        return types

    def _collect_parameters(self, scope_node: Node, types: dict[str, str]) -> None:
        param_list = scope_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if param_list is None:
            return
        for child in param_list.children:
            if child.type != cs.TS_CSHARP_PARAMETER:
                continue
            name = safe_decode_text(child.child_by_field_name(cs.FIELD_NAME))
            type_text = safe_decode_text(child.child_by_field_name(cs.FIELD_TYPE))
            if name and type_text:
                types[name] = _normalize_type_name(type_text)

    def _collect_locals(self, scope_node: Node, types: dict[str, str]) -> None:
        # (H) One type map per method (as every language engine here builds), so
        # (H) sibling blocks are not distinguished: two `{ var x = ... }` blocks
        # (H) that declare `x` as DIFFERENT types cannot both be modelled. Rather
        # (H) than let the last declaration win and confidently misbind the other
        # (H) block's calls, a name seen with conflicting types is dropped so it
        # (H) falls back to (correct-when-unambiguous) bare-name resolution. Full
        # (H) block-scoped precision needs the Roslyn semantic model (follow-up).
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
            return _normalize_type_name(type_text)
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
        # (H) `var x = new T(...)` -> T (the object_creation `type` field). Other
        # (H) initializers (method calls, literals) are left untyped; chained
        # (H) return-type inference is Roslyn-follow-up territory. The initializer
        # (H) may be a direct child of the declarator or wrapped in an
        # (H) equals_value_clause depending on grammar version, so search the
        # (H) declarator's own subtree (a lambda body would be a separate scope,
        # (H) but an initializer expression is small and self-contained).
        for node in self._descendants_of_type(
            declarator, cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION
        ):
            if type_text := safe_decode_text(node.child_by_field_name(cs.FIELD_TYPE)):
                return _normalize_type_name(type_text)
        return None

    def _field_type(self, class_qn: str, field_name: str) -> str | None:
        # (H) The declared type of `field_name` on class_qn or any base class,
        # (H) read from the per-class maps recorded at ingestion (so it reaches a
        # (H) field inherited from a base in another file). Seed the BFS with every
        # (H) partial part of the class so a field declared on ANOTHER part
        # (H) (`helper` on P1, used in a method on P2) is found. BFS with a visited
        # (H) guard so a malformed inheritance cycle cannot loop.
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

    # (H) --- typed method-call resolution ------------------------------------

    def resolve_csharp_method_call(
        self,
        call_node: Node,
        local_var_types: dict[str, str] | None,
        module_qn: str,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        # (H) A Roslyn call fact for this exact site wins over every heuristic:
        # (H) it is the compiler's own overload resolution (argument types, not
        # (H) arity) and covers receivers no syntax walk can type (chained
        # (H) returns) plus reduced extension methods. Any key miss falls
        # (H) through to the heuristics below.
        if semantic := self._semantic_call_target(call_node, module_qn):
            return semantic
        func = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func is None:
            return None
        if func.type != cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            # (H) A bare `Foo(...)`/`Foo<T>(...)` follows C# simple-name lookup:
            # (H) an in-scope local function first (it shadows same-name method
            # (H) overloads), then an arity-matched member of the enclosing
            # (H) type. A miss falls to the generic simple-name path.
            if func.type in (cs.TS_CSHARP_IDENTIFIER, cs.TS_CSHARP_GENERIC_NAME):
                return self._resolve_bare_call(func, call_node, module_qn, caller_qn)
            return None
        method_name = safe_decode_text(func.child_by_field_name(cs.FIELD_NAME))
        receiver = func.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        if not method_name or receiver is None:
            return None
        arg_count = self._count_arguments(call_node)

        receiver_class_qn = self._resolve_receiver_class_qn(
            receiver, local_var_types or {}, module_qn, caller_qn
        )
        # (H) Resolution order matters: an EXACT-ARITY instance method wins, then
        # (H) an (always arity-exact) extension method, and only then the instance
        # (H) name-only fallback. Trying the name-only fallback before extensions
        # (H) would bind `c.Foo(1)` to a lone `C.Foo()` and never reach the
        # (H) arity-correct `static Foo(this C, int)` extension.
        if receiver_class_qn is not None:
            if arity_hit := self._find_arity_across_parts(
                receiver_class_qn, method_name, arg_count
            ):
                return cs.NodeLabel.METHOD.value, arity_hit
        # (H) An extension method (`static M(this T x, ...)` on an unrelated static
        # (H) class) whose `this` receiver type matches the call's receiver -- the
        # (H) only path that binds `x.M()` to a method not in x's hierarchy.
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
                return cs.NodeLabel.METHOD.value, name_hit
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
        # (H) `Handle<TException>(...)`: the callee name is the identifier
        # (H) without its type arguments (matching how methods register).
        name = name.split(cs.CHAR_ANGLE_OPEN, 1)[0]
        arg_count = self._count_arguments(call_node)
        if local := self._find_in_scope_local_function(
            name, arg_count, caller_qn, module_qn
        ):
            return cs.NodeLabel.FUNCTION.value, local
        # (H) Same-arity twins (`M(X) => M<Void>(x)` beside `M<T>(X)` on another
        # (H) partial part, Polly's ResiliencePipeline Get*/Initialize*Context):
        # (H) parameter arity cannot tell them apart, so prefer the overload
        # (H) whose GENERICNESS matches the callee shape (`M<TResult>(...)` is a
        # (H) generic_name, `M(...)` a plain identifier).
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
        return None

    def _find_in_scope_local_function(
        self, name: str, arg_count: int, caller_qn: str | None, module_qn: str
    ) -> str | None:
        # (H) Walk the caller's scope chain probing for a registered local
        # (H) function. Each level is probed both as-is and with the overload
        # (H) signature suffix stripped, because local functions register under
        # (H) the BARE method scope name while the caller_qn carries the host
        # (H) overload's signatured identity (`Handle(System.Func)` hosts
        # (H) `Handle.Handle`). A hit must match the call's arity AND be
        # (H) declared in a host the caller sits inside -- C# scoping, without
        # (H) which the parameterless sibling overload would capture the local
        # (H) fn textually nested under its own bare qn.
        if not caller_qn:
            return None
        scope = caller_qn
        while len(scope) > len(module_qn):
            stripped = scope.split(cs.CHAR_PAREN_OPEN, 1)[0]
            for probe_scope in dict.fromkeys((scope, stripped)):
                candidate = f"{probe_scope}{cs.SEPARATOR_DOT}{name}"
                # (H) Same-name local fns in SIBLING BLOCKS flatten to one scope
                # (H) qn; later declarations carry an `@line` duplicate suffix,
                # (H) so probe every registered variant, not just the natural qn
                # (H) (else an arity-matched later declaration is missed and the
                # (H) arity-blind fallback's duplicate fan-out fabricates a
                # (H) phantom edge onto the uncalled sibling).
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

    def _caller_within_host(self, caller_qn: str, host_key: FunctionSpanKey) -> bool:
        # (H) True when the caller IS the local function's host scope or a local
        # (H) function transitively hosted inside it (a sibling or nested local
        # (H) fn calling across/into its own nest). Spans join lazily against
        # (H) function_locations because the host's signatured identity was not
        # (H) registered yet when the local function was pinned.
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
        # (H) Enclosing-type candidates for a bare member call, outermost last:
        # (H) strip the overload signature (only the leaf carries one, and its
        # (H) qualified parameter types contain dots that would break a plain
        # (H) rsplit), then peel scope segments down to the module boundary.
        if not caller_qn:
            return
        scope = caller_qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
        while cs.SEPARATOR_DOT in scope:
            scope = scope.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if len(scope) <= len(module_qn):
                return
            yield scope

    def resolve_property_read(self, name: str, caller_qn: str | None) -> str | None:
        # (H) A bare-identifier read (`WrappedDictionary.Keys`) targets a
        # (H) property of the caller's enclosing type (implicit this); resolve
        # (H) across partial parts and base classes and accept ONLY a
        # (H) registered property, so same-name methods stay out of the read
        # (H) pass.
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
        # (H) `Cfg.Value` / `w.Inner`: the NAME field read resolved against the
        # (H) receiver's type (a class name for a static read, an inferred
        # (H) local/parameter type for an instance read). Accepts ONLY a
        # (H) registered property; an unresolvable receiver yields nothing, so
        # (H) unrelated `x.Value` chains never fabricate an edge.
        class_qn = self._type_name_to_qn(receiver_type, module_qn)
        if class_qn is None:
            return None
        qn = self._find_name_across_parts(class_qn, name)
        if qn is not None and self.function_registry.is_property(qn):
            return qn
        return None

    def _semantic_call_target(
        self, call_node: Node, module_qn: str
    ) -> tuple[str, str] | None:
        if not self.csharp_call_sites:
            return None
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
        # (H) Keyed on the callee NAME token (nested invocations share an
        # (H) expression start, never a name token), with generic arguments
        # (H) stripped to match Roslyn's symbol name.
        key: CallSiteKey = (
            rel,
            name_node.start_point[0] + 1,
            name_node.start_point[1],
            name.split(cs.CHAR_ANGLE_OPEN, 1)[0],
        )
        fact = self.csharp_call_sites.get(key)
        if fact is None:
            return None
        return self._declared_location(
            fact.target_file, fact.target_line, fact.target_col
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
            # (H) `recv?.Method(...)`: the name lives on the member_binding child
            # (H) (same token Roslyn keys its MemberBindingExpressionSyntax fact
            # (H) on).
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
        # (H) The fact's target declaration location resolves through the exact
        # (H) (module_qn, start_line, start_col) record Pass 2 registered, so the
        # (H) returned label/qn are the ingested node's, signature included.
        target_module = self._module_qn_for_rel_file(rel_file)
        if target_module is None:
            return None
        location = self.function_locations.get((target_module, line, col))
        if location is None:
            return None
        return location.label, location.qualified_name

    def _module_qn_for_rel_file(self, rel_file: str) -> str | None:
        # (H) Lazy inverse of module_qn_to_file_path (which Pass 2 fills after
        # (H) this engine is constructed); rebuilt when new modules appeared.
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
        return self._find_extension_method(type_name, method_name, arg_count)

    def _receiver_type_name(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        # (H) The receiver's declared type NAME (not its class qn), needed for the
        # (H) extension-method fallback: extensions frequently target BCL types
        # (H) (`string`, `int`) that are never registered as classes, so the raw
        # (H) type name is all we can match on. Mirrors _resolve_receiver_class_qn's
        # (H) branches but stops at the name.
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None:
            return None
        receiver = unwrapped
        # (H) `((Widget)o).Ext()`: the cast target IS the receiver type, so an
        # (H) extension-only method still binds on a cast receiver.
        if receiver.type == cs.TS_CSHARP_CAST_EXPRESSION:
            return self._cast_type_name(receiver)
        if receiver.type == cs.TS_CSHARP_THIS:
            return self._this_receiver_type(module_qn, caller_qn)
        if receiver.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            return self._this_field_receiver_type(receiver, caller_qn)
        if receiver.type == cs.TS_CSHARP_IDENTIFIER:
            return self._identifier_receiver_type(receiver, local_var_types, caller_qn)
        return None

    def _unwrap_receiver(self, receiver: Node) -> Node | None:
        # (H) Peel interleaved parens and null-forgiving postfix wrappers
        # (H) (`((Component)s)!` puts the `!` OUTSIDE the parens) to a fixpoint.
        while receiver.type in (
            cs.TS_PARENTHESIZED_EXPRESSION,
            cs.TS_CSHARP_POSTFIX_UNARY_EXPRESSION,
        ):
            inner = receiver.named_children[0] if receiver.named_children else None
            if inner is None:
                return None
            receiver = inner
        return receiver

    def _cast_type_name(self, cast_node: Node) -> str | None:
        cast_type = safe_decode_text(cast_node.child_by_field_name(cs.FIELD_TYPE))
        return _normalize_type_name(cast_type) if cast_type else None

    def _this_receiver_type(self, module_qn: str, caller_qn: str | None) -> str | None:
        qn = self._containing_class_qn(caller_qn)
        if qn is None:
            return None
        # (H) `this` names the exact containing class, so keep its
        # (H) namespace-qualified form (`N1.Widget`, module prefix stripped) rather
        # (H) than the bare simple name: that lets the matcher bind an exact `this
        # (H) N1.Widget` extension even when another `N2.Widget` exists.
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
        # (H) An unknown bare identifier is a TYPE name (a static call
        # (H) `Widget.M()`), not an instance -- extension methods bind on instances
        # (H) only, so do NOT treat it as an extension receiver (else `Widget.Poke()`
        # (H) would wrongly bind `static Poke(this Widget)`, invalid in C#).
        return None

    def _find_extension_method(
        self, receiver_type_name: str, method_name: str, arg_count: int
    ) -> str | None:
        candidates = self.csharp_extension_methods.get(method_name)
        if not candidates:
            return None
        recv_simple = receiver_type_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        recv_qualified = cs.SEPARATOR_DOT in receiver_type_name
        # (H) An UNqualified receiver whose simple name maps to more than one
        # (H) registered first-party type (`N1.Widget` vs `N2.Widget`) is
        # (H) genuinely ambiguous -- we can't tell which one it is, so an
        # (H) unqualified-vs-unqualified match must not guess. A qualified receiver
        # (H) or a BCL name (not registered) is not affected.
        ambiguous_unqualified = not recv_qualified and (
            len(
                [
                    qn
                    for qn in self.simple_name_lookup.get(recv_simple, set())
                    if self.function_registry.get(qn) in _TYPE_DECLS
                ]
            )
            > 1
        )
        matches: list[str] = []
        for qn, recv_type, ext_namespace in candidates:
            # (H) `_arity` reads the first `(`/last `)`, so pass the whole qn -- a
            # (H) leaf-split on `.` would land inside a qualified param type.
            if _arity(qn) != arg_count + 1:
                continue
            if recv_type.rsplit(cs.SEPARATOR_DOT, 1)[-1] != recv_simple:
                continue
            cand_qualified = cs.SEPARATOR_DOT in recv_type
            # (H) Namespace consistency between the call receiver and the stored
            # (H) `this` type, by qualification:
            # (H)  - both qualified: require the SAME fully-qualified name
            # (H)    (`N1.Widget` binds `this N1.Widget`, never `this N2.Widget`);
            # (H)  - recv qualified, cand not: resolve the ext's unqualified
            # (H)    `this Widget` to `<ext-namespace>.Widget` and require equality
            # (H)    (`N.Widget` binds a same-namespace `this Widget`);
            # (H)  - recv unqualified, cand qualified: the receiver's namespace is
            # (H)    unknown without a semantic model, so don't guess;
            # (H)  - both unqualified: match by simple name unless it's ambiguous.
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
            elif cand_qualified:  # (H) recv unqualified, cand qualified
                continue
            elif ambiguous_unqualified:
                continue
            matches.append(qn)
        # (H) Bind only on a unique match; an ambiguous name across static classes
        # (H) is left unresolved rather than guessed.
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
        # (H) A cast receiver `((Component)s!).Reload()` (Polly's
        # (H) CancellationToken.Register callback): the cast TYPE is the
        # (H) receiver's type by construction (mirrors the Java cast-receiver
        # (H) handling).
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None:
            return None
        receiver = unwrapped
        if receiver.type == cs.TS_CSHARP_CAST_EXPRESSION:
            if cast_type := self._cast_type_name(receiver):
                return self._type_name_to_qn(cast_type, module_qn)
            return None
        if receiver.type == cs.TS_CSHARP_THIS:
            return self._containing_class_qn(caller_qn)
        # (H) An explicit `this.field` receiver: the field's (possibly inherited)
        # (H) type on the enclosing class.
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
            # (H) A local/parameter of a known type resolves via its type; else a
            # (H) bare (possibly inherited) field of the enclosing class; else the
            # (H) receiver may itself be a type name (a static call `Foo.Bar()`).
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
        # (H) Strip any parameter signature before splitting off the method leaf,
        # (H) so a qualified param type (`M(System.String)`) does not fool rsplit.
        base = caller_qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
        class_qn = base.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return class_qn if self.function_registry.get(class_qn) in _TYPE_DECLS else None

    def _type_name_to_qn(self, type_name: str, module_qn: str) -> str | None:
        # (H) An already-qualified name that IS a registered type resolves directly,
        # (H) skipping the ambiguous simple-name sweep.
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
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # (H) Several candidates that are all parts of ONE partial class are a
            # (H) single logical type, not a real ambiguity; return one part (method
            # (H) resolution then spans the whole group).
            if part := self._single_partial_group_member(candidates):
                return part
            # (H) Prefer a candidate in the calling file's module; ambiguity across
            # (H) unrelated files is left unresolved rather than guessed.
            same_module = [q for q in candidates if q.startswith(f"{module_qn}.")]
            if len(same_module) == 1:
                return same_module[0]
        return None

    def _single_partial_group_member(self, candidates: list[str]) -> str | None:
        # (H) If every candidate belongs to the SAME partial-class group, they are
        # (H) one logical type; return its lexicographically-first part (stable
        # (H) across runs). A candidate outside the group (its group is None) means
        # (H) a genuine cross-type ambiguity, left unresolved.
        group = self.csharp_partial_groups.get(candidates[0])
        if group is None:
            return None
        if all(self.csharp_partial_groups.get(c) is group for c in candidates):
            return min(group)
        return None

    # (H) An exact-arity match ANYWHERE up the hierarchy (_find_arity_across_parts)
    # (H) wins before any same-name fallback, so an inherited correct-arity overload
    # (H) (`Base.Foo(int, int)`) is not lost to a wrong-arity same-name method
    # (H) (`Derived.Foo(int)`). resolve_csharp_method_call sequences the two around
    # (H) the extension-method lookup so an arity-correct extension is preferred over
    # (H) a lone-same-name instance fallback. Both phases span every part of a
    # (H) partial class (and each part's bases), so a member/base on another part
    # (H) binds.
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
        # (H) ALL exact-arity same-name overloads across partial parts and
        # (H) bases (the single-hit variant returns the first, which is
        # (H) arbitrary when same-arity twins exist).
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

    def csharp_method_group_family(self, name: str, caller_qn: str | None) -> list[str]:
        # (H) Every same-name METHOD of the caller's enclosing type (across
        # (H) partial parts and base classes): a bare method-group pass binds
        # (H) the enclosing type's method group, and which overload the
        # (H) delegate selects is invisible to syntax, so the whole family is
        # (H) referenced. Properties are excluded (a method group never names
        # (H) a property).
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
            # (H) Directly on this class only (a nested class's method has an extra
            # (H) dot in its name portion).
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

    # (H) --- ast helpers ------------------------------------------------------

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
        # (H) Every variable_declaration lexically in this method's own scope,
        # (H) pruning nested callables (lambdas, local functions, anonymous
        # (H) methods): their locals belong to a separate scope and must not leak
        # (H) into -- or shadow -- the enclosing method's type map.
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
