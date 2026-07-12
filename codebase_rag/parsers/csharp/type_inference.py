from __future__ import annotations

from collections import deque
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
        "class_field_types",
        "csharp_extension_methods",
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
        csharp_extension_methods: dict[str, list[tuple[str, str]]] | None = None,
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
        self.csharp_extension_methods = (
            csharp_extension_methods if csharp_extension_methods is not None else {}
        )

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
        # (H) field inherited from a base in another file). BFS with a visited
        # (H) guard so a malformed inheritance cycle cannot loop.
        seen: set[str] = set()
        queue = deque([class_qn])
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
        if receiver_class_qn is not None:
            method_qn = self._find_method(receiver_class_qn, method_name, arg_count)
            if method_qn is not None:
                return cs.NodeLabel.METHOD.value, method_qn
        # (H) Not found on the receiver's own type hierarchy: try an extension
        # (H) method (`static M(this T x, ...)` on an unrelated static class) whose
        # (H) `this` receiver type matches the call's receiver. This is the only
        # (H) path that can bind `x.M()` to a method not in x's hierarchy.
        if self.csharp_extension_methods:
            type_name = self._receiver_type_name(
                receiver, local_var_types or {}, caller_qn
            )
            if type_name and (
                ext := self._find_extension_method(type_name, method_name, arg_count)
            ):
                return cs.NodeLabel.METHOD.value, ext
        return None

    def _receiver_type_name(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        caller_qn: str | None,
    ) -> str | None:
        # (H) The receiver's declared type NAME (not its class qn), needed for the
        # (H) extension-method fallback: extensions frequently target BCL types
        # (H) (`string`, `int`) that are never registered as classes, so the raw
        # (H) type name is all we can match on. Mirrors _resolve_receiver_class_qn's
        # (H) branches but stops at the name.
        if receiver.type == cs.TS_CSHARP_THIS:
            qn = self._containing_class_qn(caller_qn)
            return qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] if qn else None
        if receiver.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            expr = receiver.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
            field = safe_decode_text(receiver.child_by_field_name(cs.FIELD_NAME))
            if expr is not None and expr.type == cs.TS_CSHARP_THIS and field:
                if class_qn := self._containing_class_qn(caller_qn):
                    return self._field_type(class_qn, field)
            return None
        if receiver.type == cs.TS_CSHARP_IDENTIFIER:
            name = safe_decode_text(receiver)
            if not name:
                return None
            if (type_name := local_var_types.get(name)) is not None:
                return type_name
            if class_qn := self._containing_class_qn(caller_qn):
                if ftype := self._field_type(class_qn, name):
                    return ftype
            # (H) An unknown bare identifier is a TYPE name (a static call
            # (H) `Widget.M()`), not an instance -- extension methods bind on
            # (H) instances only, so do NOT treat it as an extension receiver
            # (H) (else `Widget.Poke()` would wrongly bind `static Poke(this
            # (H) Widget)`, a call C# does not allow).
            return None
        return None

    def _find_extension_method(
        self, receiver_type_name: str, method_name: str, arg_count: int
    ) -> str | None:
        candidates = self.csharp_extension_methods.get(method_name)
        if not candidates:
            return None
        recv_simple = receiver_type_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        matches = [
            qn
            for qn, recv_type in candidates
            # (H) The `this` receiver counts as the first parameter, so the
            # (H) extension method's arity is the call's arg count + 1.
            if recv_type.rsplit(cs.SEPARATOR_DOT, 1)[-1] == recv_simple
            and _arity(qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]) == arg_count + 1
        ]
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
