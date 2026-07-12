from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import (
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    NodeType,
    SimpleNameLookup,
)
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

    # (H) --- variable/field/parameter type map -------------------------------

    def build_variable_type_map(self, scope_node: Node) -> dict[str, str]:
        types: dict[str, str] = {}
        self._collect_parameters(scope_node, types)
        self._collect_locals(scope_node, types)
        self._collect_fields(scope_node, types)
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

    def _collect_fields(self, scope_node: Node, types: dict[str, str]) -> None:
        class_node = self._containing_class(scope_node)
        if class_node is None:
            return
        body = class_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return
        for member in body.children:
            if member.type == cs.TS_CSHARP_FIELD_DECLARATION:
                self._collect_field_declaration(member, types)
            elif member.type == cs.TS_CSHARP_PROPERTY_DECLARATION:
                name = safe_decode_text(member.child_by_field_name(cs.FIELD_NAME))
                type_node = member.child_by_field_name(cs.FIELD_TYPE)
                if name and type_node and type_node.text:
                    self._record_field(types, name, type_node)

    def _collect_field_declaration(self, member: Node, types: dict[str, str]) -> None:
        var_decl = next(
            (c for c in member.children if c.type == cs.TS_CSHARP_VARIABLE_DECLARATION),
            None,
        )
        if var_decl is None:
            return
        type_node = var_decl.child_by_field_name(cs.FIELD_TYPE)
        if type_node is None or not type_node.text:
            return
        for declarator in var_decl.children:
            if declarator.type != cs.TS_CSHARP_VARIABLE_DECLARATOR:
                continue
            name = safe_decode_text(declarator.child_by_field_name(cs.FIELD_NAME))
            if name:
                self._record_field(types, name, type_node)

    def _record_field(self, types: dict[str, str], name: str, type_node: Node) -> None:
        # (H) Register both the bare field name and `this.field` so either access
        # (H) form resolves; a local of the same name (already set) wins, matching
        # (H) C# shadowing, so never overwrite an existing entry.
        type_name = _normalize_type_name(safe_decode_text(type_node) or "")
        types.setdefault(name, type_name)
        types.setdefault(f"{cs.TS_CSHARP_THIS}{cs.SEPARATOR_DOT}{name}", type_name)

    # (H) --- typed method-call resolution ------------------------------------

    def resolve_csharp_method_call(
        self,
        call_node: Node,
        local_var_types: dict[str, str] | None,
        module_qn: str,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        func = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func is None or func.type != cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            # (H) A bare `Foo(...)` is resolved by the generic simple-name path.
            return None
        method_name = safe_decode_text(func.child_by_field_name(cs.FIELD_NAME))
        receiver = func.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        if not method_name or receiver is None:
            return None
        arg_count = self._count_arguments(call_node)

        receiver_class_qn = self._resolve_receiver_class_qn(
            receiver, local_var_types or {}, module_qn, caller_qn
        )
        if receiver_class_qn is None:
            return None
        method_qn = self._find_method(receiver_class_qn, method_name, arg_count)
        if method_qn is None:
            return None
        return cs.NodeLabel.METHOD.value, method_qn

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
        if receiver.type == cs.TS_CSHARP_THIS:
            return self._containing_class_qn(caller_qn)
        # (H) A member-access receiver (`this._w`, `a.b`) is looked up by its full
        # (H) written text, which is exactly the key _record_field stores for a
        # (H) `this.field` access; without this an explicit `this.field.M()` call
        # (H) loses its typed edge and falls back to ambiguous bare-name matching.
        recv_text = safe_decode_text(receiver)
        if recv_text and (mapped := local_var_types.get(recv_text)) is not None:
            return self._type_name_to_qn(mapped, module_qn)
        if receiver.type == cs.TS_CSHARP_IDENTIFIER:
            name = safe_decode_text(receiver)
            if not name:
                return None
            # (H) A local/parameter/field of a known type resolves via its type;
            # (H) otherwise the receiver may itself be a type name (a static call
            # (H) `Foo.Bar()`), so try to bind it as a type directly.
            type_name = local_var_types.get(name)
            if type_name is not None:
                return self._type_name_to_qn(type_name, module_qn)
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
            # (H) Prefer a candidate in the calling file's module; ambiguity across
            # (H) unrelated files is left unresolved rather than guessed.
            same_module = [q for q in candidates if q.startswith(f"{module_qn}.")]
            if len(same_module) == 1:
                return same_module[0]
        return None

    def _find_method(
        self, class_qn: str, method_name: str, arg_count: int
    ) -> str | None:
        # (H) An exact-arity match ANYWHERE up the hierarchy wins before any
        # (H) same-name fallback, so an inherited correct-arity overload
        # (H) (`Base.Foo(int, int)`) is not lost to a wrong-arity same-name method
        # (H) on the derived class (`Derived.Foo(int)`). Only when no arity matches
        # (H) anywhere does the nearest lone same-name method (params-array /
        # (H) optional args) stand in.
        if resolved := self._find_method_by_arity(
            class_qn, method_name, arg_count, set()
        ):
            return resolved
        return self._find_method_by_name(class_qn, method_name, set())

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

    def _containing_class(self, node: Node) -> Node | None:
        current = node.parent
        while current is not None:
            if current.type in cs.SPEC_CSHARP_CLASS_TYPES:
                return current
            current = current.parent
        return None

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
