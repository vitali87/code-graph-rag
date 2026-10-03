from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import (
    CSharpCallShape,
    CSharpGenericShape,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageQueries,
    NodeType,
    SimpleNameLookup,
)
from ...utils import qn_markers
from ..csharp_frontend import CallSiteKey
from ..frontends.protocol import ResolvedCallSite
from ..import_processor import ImportProcessor
from ..semantic_call_join import call_site_key, declared_location
from ..utils import safe_decode_text
from .overloads import (
    ArgumentType,
    Fit,
    Relation,
    best_candidates,
    bindings_for_base,
    fit,
    plain_type_name,
    substitute,
    type_arguments,
    unshadowed,
)
from .utils import (
    _normalize_type_name,
    annotate_type_ref,
    generic_arity_of_type_text,
    leaf_type_segment,
    signature_type_name,
    split_type_ref,
    strip_generic_arguments,
    unique_carrier,
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
        elif ch == cs.CHAR_COMMA and depth == 0:
            count += 1
    return count


def _param_types(leaf: str) -> list[str]:
    # The depth-0 parameter types of a signatured leaf, split as _arity
    # counts them: `M(int, Dictionary<K, V>)` -> ["int", "Dictionary<K, V>"].
    open_idx = leaf.find(cs.CHAR_PAREN_OPEN)
    if open_idx < 0:
        return []
    inner = leaf[open_idx + 1 : leaf.rfind(cs.CHAR_PAREN_CLOSE)]
    if not inner.strip():
        return []
    types: list[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(inner):
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif ch == cs.CHAR_COMMA and depth == 0:
            types.append(inner[start:idx].strip())
            start = idx + 1
    types.append(inner[start:].strip())
    return types


def _accepts_arg_count(
    leaf: str, arg_count: int, shape: CSharpCallShape | None
) -> bool:
    # Can a call with `arg_count` arguments bind this overload without an
    # exact arity match? FEWER arguments than parameters compiles only when
    # the omitted ones have defaults, and MORE only through a `params` tail;
    # `shape`, recorded at ingest, says which (CodeRabbit, PR #2036). A method
    # this run did not parse has no record and is judged from its signature
    # alone: the call site vouches for the defaults, and a trailing array is
    # the only tail that can be `params`.
    types = _param_types(leaf)
    if arg_count < len(types):
        return shape is None or arg_count >= shape.required
    if shape is not None:
        return shape.variadic
    return bool(types) and types[-1].endswith(cs.CSHARP_ARRAY_SUFFIX)


def _numeric_literal_type(node_type: str, text: str) -> str | None:
    """The C# type of a numeric literal, from its suffix and value, or None
    when it cannot be told (an unsuffixed integer too large for int)."""
    body = text.lower().replace(cs.CSHARP_DIGIT_SEPARATOR, "")
    if node_type == cs.TS_CSHARP_REAL_LITERAL:
        suffix = body[-1:] if body[-1:] in cs.CSHARP_REAL_SUFFIX_TYPES else ""
        return cs.CSHARP_REAL_SUFFIX_TYPES[suffix]
    digits = body.rstrip(cs.CSHARP_INTEGER_SUFFIX_CHARS)
    suffix_type = cs.CSHARP_INTEGER_SUFFIX_TYPES.get(body[len(digits) :])
    if suffix_type != cs.CSHARP_INTEGER_SUFFIX_TYPES[""]:
        return suffix_type
    prefix, base = next(
        (
            (prefix, base)
            for prefix, base in cs.CSHARP_INTEGER_BASE_PREFIXES.items()
            if digits.startswith(prefix)
        ),
        ("", cs.CSHARP_DECIMAL_BASE),
    )
    try:
        value = int(digits.removeprefix(prefix), base)
    except ValueError:
        return None
    return suffix_type if value <= cs.CSHARP_INT_MAX else None


def _literals_fit(leaf: str, literal_types: list[str | None]) -> bool:
    # Could each literal argument bind its parameter? A parameter whose type
    # the literal tables do not list, and a non-literal argument, are not
    # judged; a `params` tail beyond the declared count is not either.
    for param_type, literal in zip(_param_types(leaf), literal_types, strict=False):
        if literal is None:
            continue
        param = plain_type_name(param_type)
        if (
            param in cs.CSHARP_JUDGED_PARAM_TYPES
            and param not in cs.CSHARP_LITERAL_ACCEPTS[literal]
        ):
            return False
    return True


class _Sentinel:
    """Distinct from every real result, including None."""

    __slots__ = ()


# A miss, as distinct from a cached None: None is a legitimate answer
# ("unresolved"), and caching it is most of the win on a chain whose links
# do not resolve.
_MISSING = _Sentinel()
# Set while a node's own resolution is on the stack; see the docstring on
# resolve_csharp_method_call.
_IN_PROGRESS = _Sentinel()

# What the memo stores: a resolved (label, qn), an unresolved None, or the
# in-progress marker.
type _MemoEntry = tuple[str, str] | None | _Sentinel


def _extension_receiver_matches(
    receiver_type_name: str,
    recv_type: str,
    ext_namespace: str,
    ambiguous_unqualified: bool,
) -> bool:
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
    recv_qualified = cs.SEPARATOR_DOT in receiver_type_name
    cand_qualified = cs.SEPARATOR_DOT in recv_type
    if recv_qualified and cand_qualified:
        return recv_type == receiver_type_name
    if recv_qualified:
        cand_qualified_name = (
            f"{ext_namespace}{cs.SEPARATOR_DOT}{recv_type}"
            if ext_namespace
            else recv_type
        )
        return cand_qualified_name == receiver_type_name
    if cand_qualified:
        return False
    return not ambiguous_unqualified


class _MemberCall(NamedTuple):
    """A member call being bound, as overload ranking reads it."""

    node: Node
    receiver: Node
    method_name: str
    arg_count: int
    local_var_types: dict[str, str]
    module_qn: str
    caller_qn: str | None


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
        "csharp_call_shapes",
        "csharp_class_generic_arity",
        "csharp_class_namespaced",
        "csharp_namespaced_qns",
        "csharp_method_return_types",
        "csharp_generic_shapes",
        "method_return_types",
        "function_locations",
        "_rel_to_module",
        "_call_memo",
        "_overload_families",
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
        csharp_call_sites: dict[CallSiteKey, ResolvedCallSite] | None = None,
        csharp_external_sites: set[CallSiteKey] | None = None,
        csharp_local_functions: dict[str, tuple[FunctionSpanKey, int]] | None = None,
        csharp_generic_methods: set[str] | None = None,
        csharp_call_shapes: dict[str, CSharpCallShape] | None = None,
        csharp_class_generic_arity: dict[str, int] | None = None,
        csharp_class_namespaced: dict[str, str] | None = None,
        csharp_namespaced_qns: dict[str, set[str]] | None = None,
        csharp_method_return_types: dict[str, tuple[str, int]] | None = None,
        method_return_types: dict[str, str] | None = None,
        function_locations: dict[FunctionSpanKey, FunctionLocation] | None = None,
        csharp_generic_shapes: dict[str, CSharpGenericShape] | None = None,
    ):
        # Memo for `resolve_csharp_method_call` (issue #1800). A chained
        # invocation types its receiver by resolving it, and THREE branches do
        # that independently for the same receiver node (the class-qn path,
        # the type-name path and the arity path), so an n-link fluent chain
        # re-resolved its whole prefix 3^(n/2)-ish times: measured 58 calls at
        # 4 links rising to 3,587,219 at 14, and a real Aspire file wedged
        # Pass 3 at 100% CPU for over half an hour. Memoising the entry point
        # collapses all three branches at once and makes the walk linear.
        #
        # Keyed by byte span rather than `id(node)`: CPython recycles ids of
        # freed objects, and this repo has already been bitten by that (see
        # `reset_resolution_caches`, "wildcard entries keyed by dict id, which
        # recycles"). A span is unique within a file, and `module_qn` scopes it
        # to one; `caller_qn` is included because the same node resolves
        # differently per enclosing method (`this`, locals, imports).
        self._call_memo: dict[tuple[str, str | None, int, int], _MemoEntry] = {}
        # The same-arity overloads a member call could not choose between
        # beyond the one it returned, under the memo's key, so the call pass
        # can fan the call out to them (issue #2619). Kept beside the memo
        # rather than recomputed: the memo may answer the call node long
        # after the ranking ran for it as a chained receiver.
        self._overload_families: dict[tuple[str, str | None, int, int], list[str]] = {}
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
        self.csharp_call_shapes = (
            csharp_call_shapes if csharp_call_shapes is not None else {}
        )
        self.csharp_class_generic_arity = (
            csharp_class_generic_arity if csharp_class_generic_arity is not None else {}
        )
        self.csharp_class_namespaced = (
            csharp_class_namespaced if csharp_class_namespaced is not None else {}
        )
        self.csharp_namespaced_qns = (
            csharp_namespaced_qns if csharp_namespaced_qns is not None else {}
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
        # Shared reference (as above): {class qn: its type parameters and the
        # arguments it passes its generic bases}, populated during ingestion.
        self.csharp_generic_shapes = (
            csharp_generic_shapes if csharp_generic_shapes is not None else {}
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

    def _binds_local(self, node: Node, name: str) -> bool:
        """Whether a local in scope at `node` binds `name`.

        Two binders, both of which can leave the name out of
        `local_var_types` and so read as a TYPE: a `foreach (var name in
        ...)` loop, whose element type is not inferred, and a local declared
        in an enclosing block whose initializer is not inferred
        (`var Config = Ext.Make();`, Copilot, #2011).

        The foreach binding is in scope in the loop BODY only; in the
        collection expression `foreach (var Config in Config.All())` the
        name is still the class (CodeRabbit, #2011). Only its IMPLICIT form
        counts: an explicitly typed binding declares its type, reaches
        `local_var_types`, and must keep resolving normally.
        """
        child: Node | None = None
        current: Node | None = node
        while current is not None:
            if current.type == cs.TS_CSHARP_FOREACH_STATEMENT:
                body = current.child_by_field_name(cs.FIELD_BODY)
                bound = current.child_by_field_name(cs.FIELD_LEFT)
                declared = current.child_by_field_name(cs.FIELD_TYPE)
                implicit = declared is None or (
                    declared.type == cs.TS_CSHARP_IMPLICIT_TYPE
                )
                if (
                    child is not None
                    and body is not None
                    and child.id == body.id
                    and bound is not None
                    and implicit
                    and safe_decode_text(bound) == name
                ):
                    return True
            elif current.type == cs.TS_CSHARP_BLOCK and self._block_declares(
                current, name
            ):
                return True
            child, current = current, current.parent
        return False

    def _block_declares(self, block: Node, name: str) -> bool:
        # A local declared directly in this block; C# forbids a use before
        # its declaration, so its position within the block is no signal.
        for statement in block.named_children:
            for decl in statement.named_children:
                if decl.type != cs.TS_CSHARP_VARIABLE_DECLARATION:
                    continue
                for declarator in decl.named_children:
                    if (
                        declarator.type == cs.TS_CSHARP_VARIABLE_DECLARATOR
                        and safe_decode_text(
                            declarator.child_by_field_name(cs.FIELD_NAME)
                        )
                        == name
                    ):
                        return True
        return False

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
        """Memoising front for `_resolve_csharp_method_call` (issue #1800).

        Wrapping the entry point rather than editing the three recursive
        branches means every path in and every path back out shares one memo,
        including future callers.

        `_IN_PROGRESS` doubles as a cycle guard. A chain cannot be cyclic in
        valid C#, but a malformed or recovered parse tree can present one, and
        without this the recursion would not terminate. Returning None for a
        re-entered node degrades that site to an unresolved call, which is what
        the issue asks a pathological input to do instead of hanging.
        """
        key = self._call_key(call_node, module_qn, caller_qn)
        cached = self._call_memo.get(key, _MISSING)
        if cached is _IN_PROGRESS:
            return None
        if not isinstance(cached, _Sentinel):
            return cached
        self._call_memo[key] = _IN_PROGRESS
        result = self._resolve_csharp_method_call(
            call_node, local_var_types, module_qn, caller_qn
        )
        self._call_memo[key] = result
        return result

    @staticmethod
    def _call_key(
        call_node: Node, module_qn: str, caller_qn: str | None
    ) -> tuple[str, str | None, int, int]:
        return (module_qn, caller_qn, call_node.start_byte, call_node.end_byte)

    def clear_call_memo(self) -> None:
        # The ranked families answer the same keys as the memo, so they
        # expire with it.
        self._call_memo.clear()
        self._overload_families.clear()

    def csharp_member_overload_family(
        self, call_node: Node, module_qn: str, caller_qn: str | None
    ) -> list[str]:
        """The other overloads a member call fits as well as the one it
        resolved to, or [] when its arguments chose one (issue #2619)."""
        return self._overload_families.get(
            self._call_key(call_node, module_qn, caller_qn), []
        )

    def _resolve_csharp_method_call(
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
            return self._resolve_non_member_call(func, call_node, module_qn, caller_qn)
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
            return self._resolve_base_call(method_name, arg_count, caller_qn)

        receiver_class_qn = self._resolve_receiver_class_qn(
            receiver, local_var_types or {}, module_qn, caller_qn
        )
        member_call = _MemberCall(
            call_node,
            receiver,
            method_name,
            arg_count,
            local_var_types or {},
            module_qn,
            caller_qn,
        )
        # Resolution order matters: an EXACT-ARITY instance method wins, then
        # an (always arity-exact) extension method, and only then the instance
        # name-only fallback. Trying the name-only fallback before extensions
        # would bind `c.Foo(1)` to a lone `C.Foo()` and never reach the
        # arity-correct `static Foo(this C, int)` extension.
        if arity_target := self._arity_member_target(member_call, receiver_class_qn):
            return arity_target
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
        if fallback_target := self._fallback_member_target(
            member_call, receiver_class_qn
        ):
            return fallback_target
        return self._external_or_unresolved(
            receiver,
            method_name,
            receiver_class_qn,
            local_var_types or {},
            caller_qn,
        )

    def _arity_member_target(
        self, member_call: _MemberCall, receiver_class_qn: str | None
    ) -> tuple[str, str] | None:
        """An exact-arity instance method of the receiver's class, if any."""
        if receiver_class_qn is None:
            return None
        arity_hit = self._find_arity_across_parts(
            receiver_class_qn, member_call.method_name, member_call.arg_count
        )
        if not arity_hit:
            return None
        # Same-arity overloads are told apart by argument type; the first
        # arity match stands when no argument can decide (issue #2619).
        arity_hit = (
            self._pick_member_overload(member_call, receiver_class_qn, compatible=False)
            or arity_hit
        )
        # A delegate-typed PROPERTY registers as a METHOD node with a bare
        # (arity-0) qn, so a 0-arg invoke slips through the ARITY path too:
        # `entry.Callback()` is Delegate.Invoke, not a call to the property.
        return self._method_or_property_target(arity_hit)

    def _fallback_member_target(
        self, member_call: _MemberCall, receiver_class_qn: str | None
    ) -> tuple[str, str] | None:
        """The receiver's class member a call binds to once neither an
        exact-arity method nor an extension method took it."""
        if receiver_class_qn is None:
            return None
        # A call that leaves defaulted parameters out (`v.ValidateAsync(p)`
        # for `ValidateAsync(T, CancellationToken = default)`) matches no
        # arity exactly. Ranked by argument type among the overloads that
        # accept it, it is not left to a name-only guess, which used to land
        # on whatever shared the name, an explicit implementation included.
        if defaulted_hit := self._pick_member_overload(
            member_call, receiver_class_qn, compatible=True
        ):
            return self._method_or_property_target(defaulted_hit)
        if name_hit := self._find_name_across_parts(
            receiver_class_qn, member_call.method_name
        ):
            # A delegate-typed PROPERTY invoked with call syntax
            # (`options.ShouldHandle(args)`) is Delegate.Invoke, not a method
            # call; binding the property as a METHOD fabricates an edge. Its
            # reachability comes from the read pass.
            return self._method_or_property_target(name_hit)
        return None

    def _external_or_unresolved(
        self,
        receiver: Node,
        method_name: str,
        receiver_class_qn: str | None,
        local_var_types: dict[str, str],
        caller_qn: str | None,
    ) -> tuple[str, str] | None:
        """The last word: an external target, or nothing.

        An object-virtual miss on a TYPED receiver (`severity.ToString()` on an
        enum, `options.GetType()`) resolves to System.Object/Enum; falling to
        the trie instead lands on whatever unrelated hide-object-members
        override exists (Polly's PolicyBuilder). An UNTYPED receiver is
        external when `_externally_targeted` says so; otherwise the call is
        simply unresolved.
        """
        if method_name in cs.CSHARP_OBJECT_VIRTUALS:
            return CSHARP_EXTERNAL_TARGET
        if receiver_class_qn is None and self._externally_targeted(
            receiver, method_name, local_var_types, caller_qn
        ):
            return CSHARP_EXTERNAL_TARGET
        return None

    def _resolve_non_member_call(
        self,
        func: Node,
        call_node: Node,
        module_qn: str,
        caller_qn: str | None,
    ) -> tuple[str, str] | None:
        """Resolve a call whose callee is not a member access.

        A bare `Foo(...)`/`Foo<T>(...)` follows C# simple-name lookup: an
        in-scope local function first (it shadows same-name method overloads),
        then an arity-matched member of the enclosing type. A miss falls to the
        generic simple-name path. Anything else (an invoked expression, say) is
        not resolvable here.
        """
        if func.type in (cs.TS_CSHARP_IDENTIFIER, cs.TS_CSHARP_GENERIC_NAME):
            return self._resolve_bare_call(func, call_node, module_qn, caller_qn)
        return None

    def _method_or_property_target(self, hit: str) -> tuple[str, str]:
        """A resolved qn as a call target, or external when it is a property.

        Both the arity and the name lookup end this way, so the property check
        lives here once rather than twice in the caller.
        """
        if self.function_registry.is_property(hit):
            return CSHARP_EXTERNAL_TARGET
        return cs.NodeLabel.METHOD.value, hit

    def _resolve_base_call(
        self, method_name: str, arg_count: int, caller_qn: str | None
    ) -> tuple[str, str] | None:
        """Resolve `base.X()` against the BASE chain only.

        Split out of `_resolve_csharp_method_call` to keep that function under
        the cognitive-complexity limit; the two nested walks below are its
        densest part and are self-contained.

        A first-party base's member wins when one exists; otherwise the base is
        external (`object.Equals` in Polly's hide-object-members regions) and
        the call must emit nothing, because the trie fallback was self-looping
        it onto the caller's own override. Arity-exact matches are tried across
        every partial part first, then name-only, mirroring the order the
        instance path uses.
        """
        class_qn = self._containing_class_qn(caller_qn)
        if class_qn is None:
            return CSHARP_EXTERNAL_TARGET
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
        # A `foreach (var x in ...)` binding is a LOCAL whose element type is
        # not inferred, so it never reaches `local_var_types`. Left to the
        # checks below it read as an external TYPE when PascalCase and fell
        # to the name trie otherwise, which bound the call to a registered
        # class of the same name: `foreach (var Config in items) {
        # Config.Each(); }` emitted an edge to `N.Config.Each` (Copilot,
        # PR #1998). A local owns the name whatever its type, so the call is
        # external to this graph, not a static call on that class.
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None:
            return False
        if self._is_untyped_local(unwrapped, local_var_types):
            return True
        if unwrapped.type not in (
            cs.TS_CSHARP_IDENTIFIER,
            cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION,
        ):
            return False
        text = safe_decode_text(unwrapped)
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

    def _is_untyped_local(self, bound: Node, local_var_types: dict[str, str]) -> bool:
        # Only an IMPLICIT binding: `foreach (Item it in ...)` declares a
        # type, reaches local_var_types, and resolves normally -- this
        # branch is never reached for it. Guarding on the `var` form
        # keeps that path working.
        if bound.type != cs.TS_CSHARP_IDENTIFIER:
            return False
        name = safe_decode_text(bound)
        return bool(
            name and name not in local_var_types and self._binds_local(bound, name)
        )

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
            matches = self._find_bare_call_matches(
                class_qn, name, arg_count, generic_call
            )
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
        # `using static N.T;` puts T's members in bare-call scope, but LAST:
        # a member of the enclosing type beats a static import in C#, so this
        # runs after the checks above that claim the name for the enclosing
        # type (a delegate-typed field's `Callback()` is Delegate.Invoke).
        return self._resolve_via_static_imports(
            name, call_node, arg_count, generic_call, module_qn, caller_qn
        )

    def _resolve_via_static_imports(
        self,
        name: str,
        call_node: Node,
        arg_count: int,
        generic_call: bool,
        module_qn: str,
        caller_qn: str | None,
    ) -> tuple[str, str] | None:
        # The imported types' same-name methods form ONE method group, so a
        # call is ambiguous only when overloads from two types still fit it.
        # Arity is checked first, then literal arguments (`M(1)` fits
        # `M(int)`, not `M(string)`, CodeRabbit, PR #2036); what remains
        # across two types is refused rather than picked from, since the
        # values are a set and an arbitrary pick would differ per run.
        # Same-arity overloads on ONE type are a family, not an ambiguity,
        # and are picked from as the enclosing-type tier above picks.
        type_qns: list[str] = []
        for static_type, context_qn in self._static_import_scope(module_qn, caller_qn):
            # The directive stores the C# type path (`Helpers.MathHelpers`);
            # the registry keys types by project-qualified qn, so resolve it
            # the same way a written type reference is resolved, from the
            # file that wrote it.
            type_qn = self._type_name_to_qn(static_type, context_qn)
            if type_qn and type_qn not in type_qns:
                type_qns.append(type_qn)
        literal_types = self._literal_argument_types(call_node)
        families = self._static_import_families(
            type_qns, name, arg_count, generic_call, literal_types
        )
        if len(families) == 1:
            (matches,) = families
            preferred = [
                m for m in matches if (m in self.csharp_generic_methods) == generic_call
            ]
            return cs.NodeLabel.METHOD.value, (preferred or matches)[0]
        return None

    def _static_import_families(
        self,
        type_qns: list[str],
        name: str,
        arg_count: int,
        generic_call: bool,
        literal_types: list[str | None],
    ) -> list[list[str]]:
        # Exact arity first, as C# prefers the unexpanded form; only when no
        # imported type has one may a defaulted or `params` overload bind.
        for compatible in (False, True):
            families = [
                fitting
                for type_qn in type_qns
                if (
                    fitting := [
                        qn
                        for qn in self._static_member_matches(
                            type_qn, name, arg_count, compatible
                        )
                        if _literals_fit(self._method_leaf(qn), literal_types)
                    ]
                )
            ]
            if generic_call:
                generic = [
                    kept
                    for family in families
                    if (kept := [m for m in family if m in self.csharp_generic_methods])
                ]
                families = generic or families
            if families:
                return families
        return []

    def _static_member_matches(
        self, type_qn: str, name: str, arg_count: int, compatible: bool
    ) -> list[str]:
        # `using static T` imports the static members T itself declares:
        # not its instance methods, and not what it inherits (CodeRabbit,
        # PR #2036). A method with no recorded shape (not parsed this run)
        # cannot be checked for `static` and is kept.
        out: list[str] = []
        for root in self._partial_roots(type_qn):
            prefix = f"{root}{cs.SEPARATOR_DOT}"
            for qn in self._direct_same_name_methods(root, name):
                shape = self.csharp_call_shapes.get(qn)
                if shape is not None and not shape.is_static:
                    continue
                leaf = qn[len(prefix) :]
                if (
                    _accepts_arg_count(leaf, arg_count, shape)
                    if compatible
                    else _arity(leaf) == arg_count
                ):
                    out.append(qn)
        return sorted(out, key=lambda qn: abs(_arity(qn) - arg_count))

    @staticmethod
    def _method_leaf(qn: str) -> str:
        # The signatured leaf of a method qn: dots inside the parameter list
        # (`M(System.Int32)`) are not separators.
        head, paren, params = qn.partition(cs.CHAR_PAREN_OPEN)
        return f"{head.rsplit(cs.SEPARATOR_DOT, 1)[-1]}{paren}{params}"

    def _literal_argument_types(self, call_node: Node) -> list[str | None]:
        # The C# type of each argument that is a bare literal, else None.
        arg_list = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if arg_list is None:
            return []
        out: list[str | None] = []
        for arg in arg_list.children:
            if arg.type != cs.TS_CSHARP_ARGUMENT:
                continue
            value = arg.named_children[-1] if arg.named_children else None
            out.append(None if value is None else self._literal_type(value))
        return out

    @staticmethod
    def _literal_type(value: Node) -> str | None:
        if value.type in (cs.TS_CSHARP_INTEGER_LITERAL, cs.TS_CSHARP_REAL_LITERAL):
            return _numeric_literal_type(value.type, safe_decode_text(value) or "")
        return cs.CSHARP_LITERAL_ARG_TYPES.get(value.type)

    def _static_import_scope(
        self, module_qn: str, caller_qn: str | None
    ) -> list[tuple[str, str]]:
        # (type path, module that wrote it): the file's own `using static`
        # directives, then every `global using static` in the project, which
        # C# puts in scope in every file of the compilation. A directive
        # written inside `namespace N { ... }` is in scope only there, which
        # the caller's qn shows: it embeds the namespace after the module qn
        # (CodeRabbit, PR #2036).
        imports = self.import_processor
        scope = [
            (path, module_qn)
            for namespace, path in imports.csharp_static_imports.get(module_qn, ())
            if not namespace
            or (
                caller_qn is not None
                and caller_qn.startswith(
                    f"{module_qn}{cs.SEPARATOR_DOT}{namespace}{cs.SEPARATOR_DOT}"
                )
            )
        ]
        scope.extend(
            (path, declaring_qn)
            for declaring_qn, paths in imports.csharp_global_static_imports.items()
            for path in paths
        )
        return scope

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
            return declared_location(
                fact.target_file,
                fact.target_line,
                fact.target_col,
                self.function_locations,
                self.module_qn_to_file_path,
                self.repo_path,
                self._rel_to_module,
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
        # Generic arguments stripped to match Roslyn's symbol name.
        return call_site_key(
            name_node,
            name.split(cs.CHAR_ANGLE_OPEN, 1)[0],
            module_qn,
            self.module_qn_to_file_path,
            self.repo_path,
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
        # namespace-qualified form (`N1.Widget`) rather than the bare simple
        # name: that lets the matcher bind an exact `this N1.Widget` extension
        # even when another `N2.Widget` exists. The form is recorded at
        # ingest from the declaration, because a namespace the module's
        # directory spells is no longer in the qn (issue #1629); the
        # prefix-strip below is the fallback for a class ingested without it.
        if (namespaced := self.csharp_class_namespaced.get(qn)) is not None:
            return namespaced
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
        if receiver.type in (
            cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION,
            cs.TS_CSHARP_CAST_EXPRESSION,
        ):
            type_node = receiver.child_by_field_name(cs.FIELD_TYPE)
            raw = safe_decode_text(type_node) if type_node else None
            return generic_arity_of_type_text(raw) if raw else None
        if receiver.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
            return self._invocation_return_arity(
                receiver, local_var_types, module_qn, caller_qn
            )
        if receiver.type == cs.TS_CSHARP_THIS:
            class_qn = self._containing_class_qn(caller_qn)
            if class_qn is not None:
                return self.csharp_class_generic_arity.get(class_qn, 0)
        return None

    def _invocation_return_arity(
        self,
        invocation: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> int | None:
        # Full caller context: locals and imports participate in the inner
        # resolution exactly as they did for the instance path.
        inner = self.resolve_csharp_method_call(
            invocation, local_var_types, module_qn, caller_qn
        )
        if inner is not None and (
            entry := self.csharp_method_return_types.get(inner[1])
        ):
            return entry[1]
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
        ambiguous_unqualified = not recv_qualified and self._same_arity_type_twins(
            recv_simple
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
            if _extension_receiver_matches(
                receiver_type_name, recv_type, ext_namespace, ambiguous_unqualified
            ):
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
            return self._cast_receiver_qn(receiver, module_qn)
        # `new Builder().Add()`: an object-creation receiver IS its type.
        if receiver.type == cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION:
            return self._object_creation_receiver_qn(receiver, module_qn)
        # `Policy.Handle<T>().Wrap(...)`: an invocation receiver types the
        # next hop via the resolved inner call's recorded return type
        # (Polly's whole fluent surface). Depth is bounded by chain length.
        if receiver.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
            return self._invocation_receiver_qn(
                receiver, local_var_types, module_qn, caller_qn
            )
        if receiver.type == cs.TS_CSHARP_THIS:
            return self._containing_class_qn(caller_qn)
        # An explicit `this.field` receiver: the field's (possibly inherited)
        # type on the enclosing class.
        if receiver.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            return self._member_access_receiver_qn(
                receiver, local_var_types, module_qn, caller_qn
            )
        if receiver.type == cs.TS_CSHARP_ALIAS_QUALIFIED_NAME:
            if raw := safe_decode_text(receiver):
                return self._qualified_type_name_to_qn(raw, module_qn)
            return None
        if receiver.type == cs.TS_CSHARP_IDENTIFIER:
            return self._identifier_receiver_qn(
                receiver, local_var_types, module_qn, caller_qn
            )
        return None

    def _cast_receiver_qn(self, receiver: Node, module_qn: str) -> str | None:
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

    def _object_creation_receiver_qn(
        self, receiver: Node, module_qn: str
    ) -> str | None:
        type_node = receiver.child_by_field_name(cs.FIELD_TYPE)
        if type_text := safe_decode_text(type_node) if type_node else None:
            return self._type_name_to_qn(
                _normalize_type_name(type_text),
                module_qn,
                generic_arity_of_type_text(type_text),
            )
        return None

    def _invocation_receiver_qn(
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
            rtype, rarity = entry
            return self._type_name_to_qn(rtype, module_qn, rarity)
        return None

    def _member_access_receiver_qn(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        expr = receiver.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        field = safe_decode_text(receiver.child_by_field_name(cs.FIELD_NAME))
        if expr is not None and expr.type == cs.TS_CSHARP_THIS and field:
            if class_qn := self._containing_class_qn(caller_qn):
                if ftype := self._field_type(class_qn, field):
                    return self._type_name_to_qn(ftype, module_qn)
        if raw := safe_decode_text(receiver):
            head, separator, tail = raw.partition(cs.SEPARATOR_DOT)
            if (
                separator
                and cs.SEPARATOR_DOUBLE_COLON not in raw
                and head in local_var_types
            ):
                return self._field_chain_qn(local_var_types[head], tail, module_qn)
            if qualified := self._qualified_type_name_to_qn(raw, module_qn):
                return qualified
        return None

    def _field_chain_qn(self, head_type: str, tail: str, module_qn: str) -> str | None:
        current_qn = self._type_name_to_qn(head_type, module_qn)
        for field_name in tail.split(cs.SEPARATOR_DOT):
            if current_qn is None:
                break
            field_type = self._field_type(current_qn, field_name)
            if field_type is None:
                current_qn = None
                break
            current_qn = self._type_name_to_qn(field_type, module_qn)
        return current_qn

    def _identifier_receiver_qn(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        name = safe_decode_text(receiver)
        if not name:
            return None
        # A local/parameter of a known type resolves via its type; else a
        # bare (possibly inherited) field of the enclosing class; else the
        # receiver may itself be a type name (a static call `Foo.Bar()`).
        type_name = local_var_types.get(name)
        if type_name is not None:
            return self._type_name_to_qn(type_name, module_qn)
        # A `foreach (var x in ...)` binding is a LOCAL whose type comes
        # from the sequence and is not inferred, so it never reaches
        # `local_var_types`. Falling through to the static branch below
        # read the name as a TYPE: `foreach (var Config in items) {
        # Config.Each(); }` emitted an edge to the registered class
        # `N.Config` (Copilot, PR #1998). A local owns the name here
        # whatever its type, so the receiver is unresolved, not static.
        if self._binds_local(receiver, name):
            return None
        if class_qn := self._containing_class_qn(caller_qn):
            if ftype := self._field_type(class_qn, name):
                return self._type_name_to_qn(ftype, module_qn)
        return self._type_name_to_qn(name, module_qn)

    def _qualified_type_name_to_qn(
        self,
        type_name: str,
        module_qn: str,
        generic_arity: int | None = None,
    ) -> str | None:
        type_name, annotated_arity = split_type_ref(type_name)
        if generic_arity is None:
            generic_arity = annotated_arity or generic_arity_of_type_text(type_name)
        type_name = _normalize_type_name(type_name)
        expanded = type_name.replace("::", cs.SEPARATOR_DOT)
        global_prefix = f"global{cs.SEPARATOR_DOT}"
        is_global = expanded.startswith(global_prefix)
        if is_global:
            expanded = expanded[len(global_prefix) :]
        import_map = self.import_processor.import_mapping.get(module_qn)
        first, separator, rest = expanded.partition(cs.SEPARATOR_DOT)
        if not is_global and import_map and (mapped := import_map.get(first)):
            expanded = f"{mapped}{separator}{rest}" if separator else mapped

        if self.function_registry.get(expanded) in _TYPE_DECLS:
            return expanded
        # The written path names the DECLARED form (`Zeta.Widget`), which a
        # folded qn no longer ends with, so it is looked up in the
        # declared-form index; the leading alias was expanded above, so
        # `Z.Widget` under `using Z = Zeta;` lands here as `Zeta.Widget`
        # (issues #2000, #2004).
        # `Widget` and `Widget<T>` share the declared form, so the written
        # arity picks between them first (bot review).
        carriers = self.csharp_namespaced_qns.get(expanded)
        if carriers and len(carriers) > 1 and generic_arity is not None:
            carriers = {
                qn
                for qn in carriers
                if self.csharp_class_generic_arity.get(qn, 0) == generic_arity
            } or carriers
        if declared := unique_carrier(carriers, self.csharp_partial_groups):
            return declared
        leaf = expanded.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        candidates = [
            qn
            for qn in self.simple_name_lookup.get(leaf, set())
            if self.function_registry.get(qn) in _TYPE_DECLS
            and self._csharp_qualified_qn_matches(qn, expanded, module_qn)
        ]
        return self._disambiguate_type_candidates(candidates, generic_arity, module_qn)

    def _csharp_qualified_qn_matches(
        self, candidate_qn: str, expanded: str, module_qn: str
    ) -> bool:
        """Accept a complete type path rooted at a known repository module."""
        natural_qn = qn_markers.natural_qn(candidate_qn)
        if natural_qn == expanded:
            return True
        suffix = f"{cs.SEPARATOR_DOT}{expanded}"
        if not natural_qn.endswith(suffix):
            return False
        prefix = natural_qn[: -len(suffix)]
        return prefix in self.module_qn_to_file_path or prefix in (
            self.project_name,
            module_qn,
        )

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
        if "::" in type_name or cs.SEPARATOR_DOT in type_name:
            return self._qualified_type_name_to_qn(type_name, module_qn, generic_arity)
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
            # A type alias (`using W = Zeta.Widget;`) maps to a WRITTEN path,
            # not a qn: resolve it as one (issue #2002).
            if mapped != simple and (
                aliased := self._qualified_type_name_to_qn(
                    mapped, module_qn, generic_arity
                )
            ):
                return aliased
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
        if not candidates:
            return None
        # Several candidates that are all parts of ONE partial class are a
        # single logical type, not a real ambiguity; return one part (method
        # resolution then spans the whole group).
        if part := self._single_partial_group_member(candidates):
            return part
        # Prefer a candidate in the calling file's module; ambiguity across
        # unrelated files is left unresolved rather than guessed.
        same_module = [q for q in candidates if q.startswith(f"{module_qn}.")]
        return same_module[0] if len(same_module) == 1 else None

    def _same_arity_type_twins(self, simple_name: str) -> bool:
        # An UNqualified receiver whose simple name maps to more than one
        # registered first-party type (`N1.Widget` vs `N2.Widget`) is
        # genuinely ambiguous, since we can't tell which one it is, so an
        # unqualified-vs-unqualified match must not guess. Same-name
        # declarations that all differ by GENERIC ARITY (`Builder` beside
        # `Builder<TResult>`, Polly's dual pipeline builders) are not that
        # namespace ambiguity: a compilable call binds the unique matching
        # extension regardless of which twin the receiver is. Only same-arity
        # twins (true `N1.Widget`/`N2.Widget` namespace splits) stay ambiguous.
        same_name_decls = [
            qn
            for qn in self.simple_name_lookup.get(simple_name, set())
            if self.function_registry.get(qn) in _TYPE_DECLS
        ]
        distinct_arities = {
            self.csharp_class_generic_arity.get(qn, 0) for qn in same_name_decls
        }
        return len(same_name_decls) > 1 and len(distinct_arities) != len(
            same_name_decls
        )

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

    # --- overloads by argument type (issue #2619) ------------------------

    def _pick_member_overload(
        self, call: _MemberCall, class_qn: str, compatible: bool
    ) -> str | None:
        """The overload a member call binds, or None when none applies.

        Overloads its arguments fit equally well are kept for the call pass,
        which fans the call out to all of them.
        """
        ranked = self._rank_member_overloads(call, class_qn, compatible)
        if len(ranked) > 1:
            self._overload_families[
                self._call_key(call.node, call.module_qn, call.caller_qn)
            ] = ranked[1:]
        return ranked[0] if ranked else None

    def _rank_member_overloads(
        self, call: _MemberCall, class_qn: str, compatible: bool
    ) -> list[str]:
        """The overloads a member call fits best, best first.

        Only exact-arity overloads compete unless `compatible`, which admits
        those that take the argument count through defaults or a `params`
        tail. Declaring types are visited from the receiver up, as the arity
        walk visits them, and the first holding an overload the arguments can
        bind decides: C# drops a base's methods once a derived one applies.
        Each type is seen with the type arguments its subclasses pass it, so
        `Validator<T>.Validate(T)` takes a Person on a `PersonValidator :
        Inline<Person>`, and the receiver's own with the ones its written
        type passes (`new Handler<Person>()`). Empty when no overload is
        applicable.
        """
        arguments: list[ArgumentType | None] | None = None

        def relation(
            arg: str, arg_arity: int, param: str, param_arity: int
        ) -> bool | None:
            return self._converts_to(arg, arg_arity, param, param_arity, call.module_qn)

        for declaring, bindings, open_names in self._typed_hierarchy(
            class_qn, self._receiver_bindings(call.receiver, class_qn)
        ):
            qns = self._applicable_methods(
                declaring, call.method_name, call.arg_count, compatible
            )
            if not qns:
                continue
            if arguments is None:
                arguments = self._argument_types(
                    call.node, call.local_var_types, call.module_qn, call.caller_qn
                )
            scored = [
                (qn, self._overload_fits(qn, arguments, bindings, open_names, relation))
                for qn in qns
            ]
            if best := best_candidates(scored):
                return best
        return []

    def _overload_fits(
        self,
        method_qn: str,
        arguments: list[ArgumentType | None],
        bindings: Mapping[str, str],
        open_names: frozenset[str],
        relation: Relation,
    ) -> list[Fit | None]:
        # How each argument binds its parameter of `method_qn`. A type
        # parameter the method declares itself shadows its class's of the
        # same name (`Handler<T>.Handle<T>(T)`), so the binding the receiver
        # gives the class's never reaches it; inference leaves it open.
        own = self._method_type_parameters(method_qn)
        visible = unshadowed(bindings, own)
        return [
            fit(substitute(parameter, visible), argument, open_names | own, relation)
            for parameter, argument in zip(
                self._call_parameters(method_qn, len(arguments)),
                arguments,
                strict=True,
            )
        ]

    def _call_parameters(self, method_qn: str, arg_count: int) -> list[str]:
        # The parameter each of `arg_count` arguments binds, one per argument,
        # so every overload is scored on every argument. A call that expands
        # a `params T[]` tail passes its trailing arguments as `T`; one that
        # leaves defaulted parameters out binds the leading ones. A position
        # no parameter takes (an overload that cannot apply) is unknown.
        types = _param_types(method_qn)
        shape = self.csharp_call_shapes.get(method_qn)
        variadic = (
            shape.variadic
            if shape is not None
            else bool(types) and types[-1].endswith(cs.CSHARP_ARRAY_SUFFIX)
        )
        if variadic and arg_count != len(types):
            element = types[-1].removesuffix(cs.CSHARP_ARRAY_SUFFIX)
            types = types[:-1] + [element] * max(arg_count - len(types) + 1, 0)
        return (types + [""] * arg_count)[:arg_count]

    def _receiver_bindings(self, receiver: Node, class_qn: str) -> dict[str, str]:
        # The type arguments the receiver's written type passes its class's
        # parameters: `new Handler<Person>()`, or `Handler<Person> h`. Empty
        # when the syntax spells none, or not one per parameter.
        shape = self.csharp_generic_shapes.get(class_qn)
        if shape is None or not shape.parameters:
            return {}
        written = self._written_receiver_type(receiver)
        if not written:
            return {}
        arguments = type_arguments(signature_type_name(written))
        if len(arguments) != len(shape.parameters):
            return {}
        return dict(zip(shape.parameters, arguments, strict=True))

    def _written_receiver_type(self, receiver: Node) -> str | None:
        # The type a receiver is written with: a construction's own type, or
        # the declared type of the parameter or local it names (a `var`
        # local's construction). Searched outward through the enclosing
        # blocks and callables, stopping at the type body.
        while (
            receiver.type == cs.TS_PARENTHESIZED_EXPRESSION and receiver.named_children
        ):
            receiver = receiver.named_children[0]
        if receiver.type == cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION:
            return safe_decode_text(receiver.child_by_field_name(cs.FIELD_TYPE))
        if receiver.type != cs.TS_CSHARP_IDENTIFIER:
            return None
        name = safe_decode_text(receiver)
        current = receiver.parent
        while name and current is not None:
            if current.type == cs.TS_CSHARP_DECLARATION_LIST:
                return None
            if current.type == cs.TS_CSHARP_BLOCK and (
                declared := self._block_local_type(current, name)
            ):
                return declared
            if declared := self._parameter_type(current, name):
                return declared
            current = current.parent
        return None

    def _block_local_type(self, block: Node, name: str) -> str | None:
        for statement in block.named_children:
            for decl in statement.named_children:
                if decl.type != cs.TS_CSHARP_VARIABLE_DECLARATION:
                    continue
                declarator = self._declarator_named(decl, name)
                if declarator is not None:
                    return self._declarator_written_type(decl, declarator)
        return None

    @staticmethod
    def _declarator_named(decl: Node, name: str) -> Node | None:
        for declarator in decl.named_children:
            if (
                declarator.type == cs.TS_CSHARP_VARIABLE_DECLARATOR
                and safe_decode_text(declarator.child_by_field_name(cs.FIELD_NAME))
                == name
            ):
                return declarator
        return None

    def _declarator_written_type(self, decl: Node, declarator: Node) -> str | None:
        # The declaration's own type, or, for a `var` local, the type its
        # construction spells.
        type_node = decl.child_by_field_name(cs.FIELD_TYPE)
        if type_node is not None and type_node.type != cs.TS_CSHARP_IMPLICIT_TYPE:
            return safe_decode_text(type_node)
        created = self._descendants_of_type(
            declarator, cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION
        )
        return (
            safe_decode_text(created[0].child_by_field_name(cs.FIELD_TYPE))
            if created
            else None
        )

    @staticmethod
    def _parameter_type(scope: Node, name: str) -> str | None:
        param_list = scope.child_by_field_name(cs.FIELD_PARAMETERS)
        if param_list is None:
            return None
        for param in param_list.named_children:
            if (
                param.type == cs.TS_CSHARP_PARAMETER
                and safe_decode_text(param.child_by_field_name(cs.FIELD_NAME)) == name
            ):
                return safe_decode_text(param.child_by_field_name(cs.FIELD_TYPE))
        return None

    def _typed_hierarchy(
        self, class_qn: str, receiver_bindings: dict[str, str]
    ) -> Iterator[tuple[str, dict[str, str], frozenset[str]]]:
        # (type, the type arguments bound to its parameters, its parameters
        # left unbound) from the receiver's parts up through every base, in
        # the depth-first order `_find_method_by_arity` walks.
        seen: set[str] = set()
        stack: list[tuple[str, dict[str, str]]] = [
            (root, receiver_bindings)
            for root in reversed(self._partial_roots(class_qn))
        ]
        while stack:
            current, bindings = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            shape = self.csharp_generic_shapes.get(current)
            parameters = shape.parameters if shape is not None else ()
            yield (
                current,
                bindings,
                frozenset(p for p in parameters if p not in bindings),
            )
            stack.extend(
                (
                    base,
                    bindings_for_base(
                        self.csharp_generic_shapes,
                        self.csharp_class_generic_arity,
                        current,
                        bindings,
                        base,
                    ),
                )
                for base in reversed(self.class_inheritance.get(current, []))
            )

    def _applicable_methods(
        self, class_qn: str, method_name: str, arg_count: int, compatible: bool
    ) -> list[str]:
        # A line-suffixed twin (`M(int)@12`, a `#if` branch) is the same
        # overload; the call pass reaches it through the registry variants.
        prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
        found = [
            qn
            for qn in self._direct_same_name_methods(class_qn, method_name)
            if _arity(leaf := qn[len(prefix) :]) == arg_count
            or (
                compatible
                and _accepts_arg_count(leaf, arg_count, self.csharp_call_shapes.get(qn))
            )
        ]
        spelled = set(found)
        return [
            qn
            for qn in found
            if (natural := qn_markers.strip_dup_marker(qn)) == qn
            or natural not in spelled
        ]

    def _method_type_parameters(self, method_qn: str) -> frozenset[str]:
        # The type parameters a generic method declares (`M<U>(U item)`),
        # as its declaration spells them.
        shape = self.csharp_call_shapes.get(method_qn)
        return frozenset(shape.type_parameters) if shape is not None else frozenset()

    def _argument_types(
        self,
        call_node: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> list[ArgumentType | None]:
        arg_list = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if arg_list is None:
            return []
        arguments = [c for c in arg_list.children if c.type == cs.TS_CSHARP_ARGUMENT]
        # A named argument may stand at any position, so with one present no
        # argument is matched to a parameter by where it is written.
        if any(a.child_by_field_name(cs.FIELD_NAME) is not None for a in arguments):
            return [None] * len(arguments)
        return [
            self._argument_type(a, local_var_types, module_qn, caller_qn)
            for a in arguments
        ]

    def _argument_type(
        self,
        argument: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> ArgumentType | None:
        value = argument.named_children[-1] if argument.named_children else None
        if value is None:
            return None
        if (literal := self._literal_type(value)) is not None:
            return ArgumentType(literal, None, 0, None)
        if value.type in (
            cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION,
            cs.TS_CSHARP_CAST_EXPRESSION,
        ):
            # The written type keeps its arguments: `new Ctx<Person>()` is
            # an identity for `Ctx<T>` with T := Person, not for `Ctx<Order>`.
            raw = safe_decode_text(value.child_by_field_name(cs.FIELD_TYPE))
            if not raw:
                return None
            written = signature_type_name(raw)
            return ArgumentType(
                None,
                strip_generic_arguments(written),
                generic_arity_of_type_text(written),
                type_arguments(written),
            )
        annotated = self._receiver_type_name(
            value, local_var_types, module_qn, caller_qn
        )
        if not annotated:
            return None
        name, arity = split_type_ref(annotated)
        return ArgumentType(None, name, arity, None)

    def _converts_to(
        self,
        arg_name: str,
        arg_arity: int,
        param_name: str,
        param_arity: int,
        module_qn: str,
    ) -> bool | None:
        # Known only between first-party types whose conversion the class
        # hierarchy settles: an argument class converts to a parameter
        # class it derives from, and to no other class. An interface on
        # either side is implemented outside `class_inheritance`, so it
        # stays unknown, as does anything external.
        param_qn = self._type_name_to_qn(param_name, module_qn, param_arity)
        arg_qn = self._type_name_to_qn(arg_name, module_qn, arg_arity)
        if param_qn is None or arg_qn is None:
            return None
        if self.function_registry.get(param_qn) != NodeType.CLASS or (
            self.function_registry.get(arg_qn) not in (NodeType.CLASS, NodeType.ENUM)
        ):
            return None
        return param_qn in self._class_ancestry(arg_qn)

    def _class_ancestry(self, class_qn: str) -> set[str]:
        seen: set[str] = set()
        queue = deque(self._partial_roots(class_qn))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            for base in self.class_inheritance.get(current, []):
                queue.extend(self._partial_roots(base))
        return seen

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

    def _find_bare_call_matches(
        self, class_qn: str, method_name: str, arg_count: int, generic_call: bool
    ) -> list[str]:
        # A bare call no longer falls to the name-wide trie (#2005), so an
        # overload bound through a default or a `params` tail must be found
        # here; exact arity still wins when the type has one. An explicit
        # type argument (`M<int>(1)`) cannot bind a non-generic method, so a
        # generic overload of either tier is taken before a non-generic exact
        # match (CodeRabbit, PR #2036); with no known generic overload the
        # tiers stand, since genericness is only recorded for parsed methods.
        exact = self._find_arity_matches_across_parts(class_qn, method_name, arg_count)
        compatible = self._find_compatible_matches(class_qn, method_name, arg_count)
        if generic_call:
            for tier in (exact, compatible):
                if generic := [m for m in tier if m in self.csharp_generic_methods]:
                    return generic
        return exact or compatible

    def _find_compatible_matches(
        self, class_qn: str, method_name: str, arg_count: int
    ) -> list[str]:
        # Nearest parameter count first, the overload C#'s betterness rules
        # favour (fewest defaults filled, fewest `params` elements).
        seen: set[str] = set()
        out: list[str] = []
        for root in self._partial_roots(class_qn):
            self._collect_arity_matches(
                root, method_name, arg_count, seen, out, compatible=True
            )
        return sorted(out, key=lambda qn: abs(_arity(qn) - arg_count))

    def _collect_arity_matches(
        self,
        class_qn: str,
        method_name: str,
        arg_count: int,
        seen: set[str],
        out: list[str],
        compatible: bool = False,
    ) -> None:
        if class_qn in seen:
            return
        seen.add(class_qn)
        prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
        out.extend(
            qn
            for qn in self._direct_same_name_methods(class_qn, method_name)
            if (
                _accepts_arg_count(
                    qn[len(prefix) :], arg_count, self.csharp_call_shapes.get(qn)
                )
                if compatible
                else _arity(qn[len(prefix) :]) == arg_count
            )
        )
        for base_qn in self.class_inheritance.get(class_qn, []):
            self._collect_arity_matches(
                base_qn, method_name, arg_count, seen, out, compatible
            )

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
        return self._method_group_on(class_qn, name)

    def csharp_member_group_argument(
        self,
        arg_node: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> list[str]:
        """The methods a `recv.Name` argument names as a method group.

        Only on a receiver the engine can type, and only METHODS: a property
        read is a value, and an untyped receiver (a foreach variable over a
        BCL collection, a BCL value) names nothing, where the simple-name
        fallback bound whichever first-party `Name` sat nearest (issue #1998).
        """
        if arg_node.type != cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            return []
        receiver = arg_node.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        name = safe_decode_text(arg_node.child_by_field_name(cs.FIELD_NAME))
        if receiver is None or not name:
            return []
        name = name.split(cs.CHAR_ANGLE_OPEN, 1)[0]
        if receiver.type == cs.TS_CSHARP_BASE:
            # `base.Handle`: the method group on the base chain (bot review).
            own = self._containing_class_qn(caller_qn)
            return sorted(
                {
                    qn
                    for root in self._partial_roots(own or "")
                    for base in self.class_inheritance.get(root, [])
                    for qn in self._method_group_on(base, name)
                }
            )
        class_qn = self._resolve_receiver_class_qn(
            receiver, local_var_types, module_qn, caller_qn
        )
        if class_qn is None and receiver.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            # `Outer.Inner.Go`, `Lib.Util.Helper`: a dotted receiver the
            # receiver typing does not cover is a TYPE written with its
            # enclosing type or namespace, but only when a registered type
            # sits at exactly that written path. The last segment alone
            # would also bind `Config.Default.Handle`, a property value
            # chain, to an unrelated `Default` type (bot review).
            class_qn = self._dotted_type_path_qn(
                safe_decode_text(receiver) or "", module_qn
            )
        if class_qn is None:
            return []
        return self._method_group_on(class_qn, name)

    def _dotted_type_path_qn(self, dotted: str, module_qn: str) -> str | None:
        # A written type path (`Outer.Inner`, `myLib.Util.Helper`, generic
        # arguments stripped from EVERY segment, so `Lib.Util<int>.Helper`
        # keeps its leaf) names a registered type whose qn ends with the
        # WHOLE path at a segment boundary; a namespace's case is no signal.
        written = strip_generic_arguments(dotted)
        if not written:
            return None
        if self.function_registry.get(written) in _TYPE_DECLS:
            return written
        simple = written.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        suffix = f"{cs.SEPARATOR_DOT}{written}"
        candidates = [
            qn
            for qn in self.simple_name_lookup.get(simple, set())
            if self.function_registry.get(qn) in _TYPE_DECLS
            # A same-file twin carries a duplicate marker (`Helper@12`)
            # after the path; the leaf's arity then picks between them.
            # Matched as `@<digits>`, never a bare `@`: a C# verbatim
            # identifier (`Lib.@Helper`) carries a leading `@` that IS part
            # of the name, and splitting at the first one truncated the
            # candidate to `Lib.`, rejecting the real type (Copilot, #1998).
            and qn_markers.strip_all_markers(qn).endswith(suffix)
        ]
        # The leaf's own arity picks between same-name twins; the leaf is
        # cut at the last dot outside generic arguments, so a qualified type
        # argument (`Helper<System.String>`) keeps its arity (bot review).
        return self._disambiguate_type_candidates(
            candidates,
            generic_arity_of_type_text(leaf_type_segment(dotted)),
            module_qn,
        )

    def _method_group_on(self, class_qn: str, name: str) -> list[str]:
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
