from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import FunctionLocation, FunctionSpanKey
from ..csharp_frontend import CallSiteKey
from ..frontends.protocol import ResolvedCallSite
from ..semantic_call_join import call_site_key, declared_location
from ..utils import safe_decode_text
from .utils import type_identifier_text

# Sentinel returned when the go/types frontend resolved a call OUTSIDE the module
# (stdlib, deps): the resolver suppresses the name-trie fallback there rather than
# fabricating a first-party edge (mirrors CSHARP_EXTERNAL_TARGET).
GO_EXTERNAL_TARGET: tuple[str, str] = ("", "")


def _selector_segments(selector: Node) -> list[str] | None:
    """`e.trees.get` -> `["e", "trees", "get"]`: the field identifiers of a
    selector chain, root first, or None when a link is not a plain identifier."""
    segments: list[str] = []
    current: Node | None = selector
    while current is not None and current.type == cs.TS_GO_SELECTOR_EXPRESSION:
        field = current.child_by_field_name(cs.FIELD_FIELD)
        if field is None or field.type != cs.TS_GO_FIELD_IDENTIFIER:
            return None
        segments.append(safe_decode_text(field) or "")
        current = current.child_by_field_name(cs.FIELD_OPERAND)
    if current is None or current.type != cs.TS_IDENTIFIER or not current.text:
        return None
    segments.append(safe_decode_text(current) or "")
    segments.reverse()
    return segments


class GoTypeInferenceEngine:
    # Maps local variable / parameter / receiver names to their bare Go type name
    # within a function or method body, so the resolver can bind a receiver-dispatch
    # call (`d.method()`) to the method node on the type. Bare names only: the
    # resolver turns a name into a class qn via the same _resolve_class_name path the
    # definition pass uses, so pointer/generic wrappers are stripped here down to the
    # underlying type identifier.
    #
    # The go/types semantic frontend (issue #1179) adds an exact-callee join on top:
    # the shared go_call_sites / go_external_sites facts (populated after
    # construction) resolve a call to its declared target or suppress a fabricated
    # first-party edge, with per-miss fallback to the stateless walker below.
    __slots__ = (
        "go_call_sites",
        "go_external_sites",
        "function_locations",
        "module_qn_to_file_path",
        "repo_path",
        "_rel_to_module",
    )

    def __init__(
        self,
        go_call_sites: dict[CallSiteKey, ResolvedCallSite] | None = None,
        go_external_sites: set[CallSiteKey] | None = None,
        function_locations: dict[FunctionSpanKey, FunctionLocation] | None = None,
        module_qn_to_file_path: dict[str, Path] | None = None,
        repo_path: Path | None = None,
    ) -> None:
        self.go_call_sites = go_call_sites if go_call_sites is not None else {}
        self.go_external_sites = (
            go_external_sites if go_external_sites is not None else set()
        )
        self.function_locations = (
            function_locations if function_locations is not None else {}
        )
        self.module_qn_to_file_path = (
            module_qn_to_file_path if module_qn_to_file_path is not None else {}
        )
        self.repo_path = repo_path if repo_path is not None else Path()
        self._rel_to_module: dict[str, str] = {}

    def resolve_go_call_site(
        self, call_node: Node, module_qn: str
    ) -> tuple[str, str] | None:
        # The go/types semantic path: an exact first-party callee (embedded-struct
        # promotion, scope shadowing) the walker cannot type, or the external
        # sentinel for a call the compiler proved leaves the module. Any miss
        # returns None so the caller falls back to the tree-sitter heuristics.
        if not (self.go_call_sites or self.go_external_sites):
            return None
        key = self._call_site_key(call_node, module_qn)
        if key is None:
            return None
        fact = self.go_call_sites.get(key)
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
        if key in self.go_external_sites:
            return GO_EXTERNAL_TARGET
        return None

    def _call_site_key(self, call_node: Node, module_qn: str) -> CallSiteKey | None:
        name_node = self._callee_name_node(call_node)
        if name_node is None:
            return None
        name = safe_decode_text(name_node)
        if not name:
            return None
        return call_site_key(
            name_node, name, module_qn, self.module_qn_to_file_path, self.repo_path
        )

    def _callee_name_node(self, call_node: Node) -> Node | None:
        # The callee NAME token, matching the gotypes tool's `calleeName`: a bare
        # identifier for `Foo()`, the `field` child for `x.M()` / `pkg.F()`, and
        # the inner name for generic (`Gen[T]()`) and parenthesised callees.
        func = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        return self._name_from_callee(func)

    def _name_from_callee(self, node: Node | None) -> Node | None:
        if node is None:
            return None
        if node.type == cs.TS_GO_IDENTIFIER:
            return node
        if node.type == cs.TS_GO_SELECTOR_EXPRESSION:
            return node.child_by_field_name(cs.FIELD_FIELD)
        if node.type == cs.TS_GO_INDEX_EXPRESSION:
            return self._name_from_callee(node.child_by_field_name(cs.FIELD_OPERAND))
        if node.type == cs.TS_PARENTHESIZED_EXPRESSION:
            inner = next((c for c in node.named_children), None)
            return self._name_from_callee(inner)
        return None

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str
    ) -> dict[str, str]:
        var_types: dict[str, str] = {}
        self._collect_receiver(caller_node, var_types)
        self._collect_parameters(caller_node, var_types)
        if body := caller_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_body_declarations(body, var_types)
        return var_types

    def build_field_type_map(self, class_node: Node) -> dict[str, str]:
        # Map a Go struct's field names to their bare type names (a `type_spec`:
        # `type Engine struct { trees methodTrees }` -> {"trees": "methodTrees"}), so
        # the resolver can type a field-hop receiver (`engine.trees.get()`) and a
        # local bound from such a call (`root := engine.trees.get(m)`) gets the
        # return type. Non-struct type_specs (aliases, interfaces) yield {}.
        fields: dict[str, str] = {}
        struct = next(
            (c for c in class_node.children if c.type == cs.TS_GO_STRUCT_TYPE), None
        )
        if struct is None:
            return fields
        field_list = next(
            (c for c in struct.children if c.type == cs.TS_GO_FIELD_DECLARATION_LIST),
            None,
        )
        if field_list is None:
            return fields
        for decl in field_list.children:
            if decl.type != cs.TS_GO_FIELD_DECLARATION:
                continue
            type_node = decl.child_by_field_name(cs.FIELD_TYPE)
            if type_node is None or not (type_name := type_identifier_text(type_node)):
                continue
            for child in decl.children:
                if child.type == cs.TS_GO_FIELD_IDENTIFIER and (
                    name := safe_decode_text(child)
                ):
                    fields[name] = type_name
        return fields

    def _collect_receiver(self, caller_node: Node, var_types: dict[str, str]) -> None:
        receiver = caller_node.child_by_field_name(cs.FIELD_RECEIVER)
        if receiver is not None:
            self._collect_parameter_list(receiver, var_types)

    def _collect_parameters(self, caller_node: Node, var_types: dict[str, str]) -> None:
        params = caller_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if params is not None:
            self._collect_parameter_list(params, var_types)

    def _collect_parameter_list(
        self, list_node: Node, var_types: dict[str, str]
    ) -> None:
        for param in list_node.children:
            if param.type != cs.TS_GO_PARAMETER_DECLARATION:
                continue
            type_node = param.child_by_field_name(cs.FIELD_TYPE)
            if type_node is None or not (type_name := type_identifier_text(type_node)):
                continue
            for child in param.children:
                if child.type == cs.TS_IDENTIFIER and (name := safe_decode_text(child)):
                    var_types[name] = type_name

    def _collect_body_declarations(self, node: Node, var_types: dict[str, str]) -> None:
        match node.type:
            case cs.TS_GO_VAR_DECLARATION:
                self._collect_var_declaration(node, var_types)
            case cs.TS_GO_SHORT_VAR_DECLARATION:
                self._collect_short_var_declaration(node, var_types)
        for child in node.children:
            self._collect_body_declarations(child, var_types)

    def _collect_var_declaration(self, node: Node, var_types: dict[str, str]) -> None:
        # `var a, b T` binds every name in the spec to the declared type.
        for spec in node.children:
            if spec.type != cs.TS_GO_VAR_SPEC:
                continue
            type_node = spec.child_by_field_name(cs.FIELD_TYPE)
            if type_node is None or not (type_name := type_identifier_text(type_node)):
                continue
            for child in spec.children:
                if child.type == cs.TS_IDENTIFIER and (name := safe_decode_text(child)):
                    var_types[name] = type_name

    def _collect_short_var_declaration(
        self, node: Node, var_types: dict[str, str]
    ) -> None:
        # `x := T{}` / `x := &T{}`: pair each left name with the type inferred from
        # the value at the same position; non-literal initialisers (calls) are left
        # unresolved rather than guessed.
        left = node.child_by_field_name(cs.FIELD_LEFT)
        right = node.child_by_field_name(cs.FIELD_RIGHT)
        if left is None or right is None:
            return
        names = [
            safe_decode_text(c) for c in left.children if c.type == cs.TS_IDENTIFIER
        ]
        values = [c for c in right.children if c.is_named]
        for name, value in zip(names, values, strict=False):
            if name and (type_name := self.infer_value_type(value)):
                var_types[name] = type_name

    def collect_call_var_bindings(
        self, caller_node: Node
    ) -> list[tuple[str, list[str]]]:
        # `x := recv.m(...)` / `x := e.field.m(...)` / `x := f(...)`: pair the bound
        # name with the callee selector segments (`e.trees.get` -> ['e','trees',
        # 'get']). The unified engine resolves the segments to the call's return type
        # (needs field + method-return maps this stateless engine lacks) and types
        # `x`. Only clean identifier-dotted callees are collected; any index/paren/
        # generic in the callee is skipped and stays unresolved.
        bindings: list[tuple[str, list[str]]] = []
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        if body is not None:
            self._collect_call_bindings(body, bindings)
        return bindings

    def _collect_call_bindings(
        self, node: Node, bindings: list[tuple[str, list[str]]]
    ) -> None:
        if node.type == cs.TS_GO_SHORT_VAR_DECLARATION:
            self._collect_call_binding(node, bindings)
        for child in node.children:
            self._collect_call_bindings(child, bindings)

    def _collect_call_binding(
        self, node: Node, bindings: list[tuple[str, list[str]]]
    ) -> None:
        left = node.child_by_field_name(cs.FIELD_LEFT)
        right = node.child_by_field_name(cs.FIELD_RIGHT)
        if left is None or right is None:
            return
        names = [
            safe_decode_text(c) for c in left.children if c.type == cs.TS_IDENTIFIER
        ]
        values = [c for c in right.children if c.is_named]
        for name, value in zip(names, values, strict=False):
            if not name or value.type != cs.TS_GO_CALL_EXPRESSION:
                continue
            if segments := self.callee_segments(value):
                bindings.append((name, segments))

    def callee_segments(self, call: Node) -> list[str] | None:
        # The callee selector of a call, split into identifier segments. A plain
        # function is one segment; `e.trees.get` is three. Returns None for any
        # non-identifier part (index/paren/generic) so callers stay unresolved.
        func = call.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func is None:
            return None
        if func.type == cs.TS_IDENTIFIER:
            return [safe_decode_text(func) or ""] if func.text else None
        if func.type != cs.TS_GO_SELECTOR_EXPRESSION:
            return None
        segments = _selector_segments(func)
        return segments if segments and all(segments) else None

    def infer_value_type(self, value: Node) -> str | None:
        if value.type == cs.TS_GO_COMPOSITE_LITERAL:
            type_node = value.child_by_field_name(cs.FIELD_TYPE)
            return type_identifier_text(type_node) if type_node else None
        if value.type == cs.TS_GO_UNARY_EXPRESSION:
            # `&T{}` wraps the composite literal in its operand.
            operand = value.child_by_field_name(cs.FIELD_OPERAND)
            return self.infer_value_type(operand) if operand else None
        if value.type == cs.TS_GO_TYPE_ASSERTION_EXPRESSION:
            # `x := y.(T)` / `y.(*T)` (gin's `c := pool.Get().(*Context)`): the
            # asserted type is x's type, so a later field-hop / method call on x
            # resolves. type_identifier_text unwraps the `*T` pointer.
            type_node = value.child_by_field_name(cs.FIELD_TYPE)
            return type_identifier_text(type_node) if type_node else None
        return None
