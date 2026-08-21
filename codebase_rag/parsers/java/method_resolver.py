from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...decorators import recursion_guard
from ...types_defs import ASTNode, NodeType
from ..utils import safe_decode_text
from .utils import (
    extract_class_info,
    extract_method_call_info,
    extract_method_info,
    get_class_context_from_qn,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ...types_defs import ASTCacheProtocol, FunctionRegistryTrieProtocol
    from ..import_processor import ImportProcessor


def _java_signature_arity(qn_or_member: str) -> int | None:
    # Top-level parameter count of a signatured Java method name
    # (`resolve(A,B,Map<K, V>)` -> 3, `create()` -> 0); None if unsignatured.
    # Generic commas (`Map<K, V>`) are nested, so only depth-0 commas separate
    # parameters. Picks the arity-matching overload for a call.
    open_idx = qn_or_member.find(cs.CHAR_PAREN_OPEN)
    if open_idx < 0:
        return None
    inner = qn_or_member[open_idx + 1 : qn_or_member.rfind(cs.CHAR_PAREN_CLOSE)]
    if not inner.strip():
        return 0
    depth = 0
    count = 1
    for ch in inner:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


def _java_param_type_names(qn: str) -> list[str]:
    # Simple parameter type names from a signatured method qn
    # (`isX(Class<?>,String)` -> ['Class', 'String']): generics and package/scope
    # stripped so they compare with inferred argument-type simple names.
    open_idx = qn.find(cs.CHAR_PAREN_OPEN)
    close_idx = qn.rfind(cs.CHAR_PAREN_CLOSE)
    if open_idx < 0 or close_idx <= open_idx:
        return []
    inner = qn[open_idx + 1 : close_idx]
    if not inner.strip():
        return []
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in inner:
        if ch in "<([":
            depth += 1
            cur += ch
        elif ch in ">)]":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [
        p.split(cs.CHAR_ANGLE_OPEN, 1)[0].rsplit(cs.SEPARATOR_DOT, 1)[-1].strip()
        for p in parts
    ]


def _simple_type_name(type_text: str) -> str:
    return (
        type_text.split(cs.CHAR_ANGLE_OPEN, 1)[0]
        .rsplit(cs.SEPARATOR_DOT, 1)[-1]
        .strip()
    )


def _pick_declared_overload(
    declarations: list[ASTNode], param_types: tuple[str, ...]
) -> ASTNode | None:
    # Without a signature to match, the first declaration is all there is. With
    # one, prefer the declaration whose parameter types match it: overloads may
    # differ in return type, and the caller has already chosen which one it is.
    if not declarations:
        return None
    if not param_types:
        return declarations[0]
    for declaration in declarations:
        declared = tuple(
            _simple_type_name(text)
            for text in extract_method_info(declaration)[cs.KEY_PARAMETERS]
        )
        if declared == param_types:
            return declaration
    return declarations[0]


def _overload_rank(qn: str, arg_types: tuple[str | None, ...]) -> int | None:
    # How well a candidate fits the KNOWN argument types: the summed per-argument
    # conversion rank, or None when some argument cannot reach its parameter at
    # all. Unknown args (None) are wildcards and cost nothing. Summing lets the
    # MOST SPECIFIC applicable overload win, so take(Integer) beats take(Object)
    # for an int argument, the way the language resolves it.
    params = _java_param_type_names(qn)
    if len(params) != len(arg_types):
        return None
    total = 0
    for at, pt in zip(arg_types, params, strict=False):
        if at is None:
            continue
        rank = _argument_rank(at.split(cs.SEPARATOR_DOT)[-1], pt)
        if rank is None:
            return None
        total += rank
    return total


def _argument_rank(arg_type: str, param_type: str) -> int | None:
    # The conversion the language would apply, ranked by preference (JLS 5.3).
    if arg_type == param_type:
        return cs.JAVA_RANK_EXACT
    if param_type in cs.JAVA_WIDENING_PRIMITIVES.get(arg_type, ()):
        return cs.JAVA_RANK_WIDENED
    boxed = cs.JAVA_BOXED_TYPES.get(arg_type, arg_type)
    if param_type == boxed:
        return cs.JAVA_RANK_BOXED
    if param_type in cs.JAVA_REFERENCE_SUPERTYPES.get(boxed, ()):
        return cs.JAVA_RANK_SUPERTYPE
    if param_type == cs.JAVA_TYPE_OBJECT_NAME:
        return cs.JAVA_RANK_OBJECT
    return None


def _callable_visible_to_caller(
    entity_type: str, qn: str, caller_qn: str | None
) -> bool:
    # A Java FUNCTION entry is a method declared inside another method's body, i.e.
    # an anonymous/local class method, only visible lexically. The unqualified
    # module-wide fallback must not let a call OUTSIDE that anon bind to it (M.use()
    # -> an anon-local helper() elsewhere). Accept a FUNCTION only when the caller is
    # lexically within its owning scope (owner qn prefixes the caller). METHOD and
    # CONSTRUCTOR entries are top-level and stay module-visible.
    if entity_type != cs.ENTITY_FUNCTION:
        return True
    if not caller_qn:
        return False
    owner = qn.split(cs.CHAR_PAREN_OPEN, maxsplit=1)[0].rsplit(cs.SEPARATOR_DOT, 1)[0]
    return caller_qn == owner or caller_qn.startswith(f"{owner}{cs.SEPARATOR_DOT}")


def _pick_overload(
    matches: list[tuple[str, str]],
    arg_count: int | None,
    arg_types: tuple[str | None, ...],
) -> tuple[str, str] | None:
    # Choose among same-name candidates: prefer an argument-TYPE match (resolves
    # same-arity overloads like isX(String) vs isX(Class)), then an argument-COUNT
    # match, then the first. A type match implies an arity match, so it is the most
    # specific.
    if not matches:
        return None
    if len(matches) > 1 and any(at is not None for at in arg_types):
        ranked = [
            (rank, index, match)
            for index, match in enumerate(matches)
            if (rank := _overload_rank(match[1], arg_types)) is not None
        ]
        if ranked:
            # Ties keep declaration order, so the choice stays deterministic.
            return min(ranked)[2]
    if len(matches) > 1 and arg_count is not None:
        for match in matches:
            if _java_signature_arity(match[1]) == arg_count:
                return match
    return matches[0]


class JavaMethodResolverMixin:
    __slots__ = ()
    import_processor: ImportProcessor
    function_registry: FunctionRegistryTrieProtocol
    project_name: str
    module_qn_to_file_path: dict[str, Path]
    ast_cache: ASTCacheProtocol
    class_inheritance: dict[str, list[str]]
    _fqn_to_module_qn: dict[str, list[str]]

    @abstractmethod
    def _resolve_java_type_name(self, type_name: str, module_qn: str) -> str: ...

    @abstractmethod
    def _infer_java_type_from_expression(
        self,
        expr_node: ASTNode,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None: ...

    @abstractmethod
    def _imported_class_qn(self, target: str, type_name: str) -> str: ...

    @abstractmethod
    def _rank_module_candidates(
        self, candidates: list[str], class_qn: str, current_module_qn: str | None
    ) -> list[str]: ...

    @abstractmethod
    def _find_registry_entries_under(
        self, prefix: str
    ) -> Iterable[tuple[str, str]]: ...

    @abstractmethod
    def _get_superclass_name(self, class_qn: str) -> str | None: ...

    @abstractmethod
    def _get_implemented_interfaces(self, class_qn: str) -> list[str]: ...

    @abstractmethod
    def _get_current_class_name(self, module_qn: str) -> str | None: ...

    @abstractmethod
    def _lookup_variable_type(self, var_name: str, module_qn: str) -> str | None: ...

    @abstractmethod
    def _lookup_java_field_type(
        self, class_type: str, field_name: str, module_qn: str
    ) -> str | None: ...

    @abstractmethod
    def _find_containing_java_class(self, node: ASTNode) -> ASTNode | None: ...

    def _resolve_java_object_type(
        self,
        object_ref: str,
        local_var_types: dict[str, str],
        module_qn: str,
        context_node: ASTNode | None = None,
    ) -> str | None:
        if object_ref in local_var_types:
            return local_var_types[object_ref]

        # 'this' reference: inside a method-body anonymous class `this` is the anon
        # (its base type), which the lexical named-class walk misses; prefer that,
        # then the lexical containing class (precise in multi-class files); fall back
        # to the first class under the module otherwise.
        if object_ref == cs.JAVA_KEYWORD_THIS:
            if anon_base := self._enclosing_anon_base_qn(context_node, module_qn):
                return anon_base
            if lexical := self._lexical_class_qn(context_node, module_qn):
                return lexical
            return next(
                (
                    str(qn)
                    for qn, entity_type in self.function_registry.find_with_prefix(
                        module_qn
                    )
                    if entity_type == NodeType.CLASS
                ),
                None,
            )

        # 'super' reference: resolve the lexical class then its parent when
        # available; otherwise the first class under the module with a parent.
        if object_ref == cs.JAVA_KEYWORD_SUPER:
            if (lexical := self._lexical_class_qn(context_node, module_qn)) and (
                parent_qn := self._find_parent_class(lexical)
            ):
                return parent_qn
            for qn, entity_type in self.function_registry.find_with_prefix(module_qn):
                if entity_type == NodeType.CLASS:
                    if parent_qn := self._find_parent_class(qn):
                        return parent_qn
            return None

        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if object_ref in import_map:
                return self._imported_class_qn(import_map[object_ref], object_ref)

        simple_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{object_ref}"
        if (
            simple_class_qn in self.function_registry
            and self.function_registry[simple_class_qn] == NodeType.CLASS
        ):
            return simple_class_qn

        # A nested class referenced by its simple name as a static receiver base
        # (`Checker.INSTANCE...`, gson's `AccessChecker.INSTANCE`) has qn
        # `module.Outer.Nested`, which the direct check above misses; use the
        # nested-aware type resolver so the static-field access chain resolves.
        nested_qn = self._resolve_java_type_name(object_ref, module_qn)
        if nested_qn != object_ref and self.function_registry.get(nested_qn) in (
            NodeType.CLASS,
            NodeType.INTERFACE,
            NodeType.ENUM,
        ):
            return nested_qn

        # An unqualified class-name receiver for a static call (`T.make()`) defined in
        # a sibling file: imports and the current module were checked above, so the
        # remaining unqualified case is a same-package class.
        if sibling_class_qn := self._resolve_sibling_class_qn(object_ref, module_qn):
            return sibling_class_qn

        # A receiver like `obj.engine` (field access on a typed variable) is not a
        # single name: resolve the base, then walk each field's declared type across
        # classes so `obj.engine.start()` and deeper chains resolve to a method.
        if cs.SEPARATOR_DOT in object_ref:
            return self._resolve_field_access_chain_type(
                object_ref, local_var_types, module_qn, context_node
            )

        return None

    def _lexical_class_qn(
        self, context_node: ASTNode | None, module_qn: str
    ) -> str | None:
        if context_node is None:
            return None
        if not (class_node := self._find_containing_java_class(context_node)):
            return None
        if not (class_name := extract_class_info(class_node).get(cs.FIELD_NAME)):
            return None
        return self._resolve_java_type_name(class_name, module_qn)

    def _enclosing_anon_base_qn(
        self, context_node: ASTNode | None, module_qn: str
    ) -> str | None:
        # If `context_node` sits inside a method-body anonymous class
        # (`new Base(){ ... }`) before any named class, return the anon's base type
        # qn: an unqualified call inside the anon is `this.m()`, dispatched on the
        # anon (its base), not the enclosing named class. None otherwise.
        if context_node is None:
            return None
        named = (
            cs.TS_CLASS_DECLARATION,
            cs.TS_INTERFACE_DECLARATION,
            cs.TS_ENUM_DECLARATION,
            cs.TS_RECORD_DECLARATION,
        )
        current = context_node.parent
        while current is not None:
            if current.type in named:
                return None
            if current.type == cs.TS_CLASS_BODY:
                parent = current.parent
                if (
                    parent is not None
                    and parent.type == cs.TS_OBJECT_CREATION_EXPRESSION
                    and (type_node := parent.child_by_field_name(cs.FIELD_TYPE))
                    is not None
                    and type_node.text is not None
                ):
                    base = type_node.text.decode(cs.ENCODING_UTF8).split(
                        cs.CHAR_ANGLE_OPEN, 1
                    )[0]
                    return self._resolve_java_type_name(base, module_qn)
                return None
            current = current.parent
        return None

    def _resolve_field_access_chain_type(
        self,
        object_ref: str,
        local_var_types: dict[str, str],
        module_qn: str,
        context_node: ASTNode | None = None,
    ) -> str | None:
        parts = object_ref.split(cs.SEPARATOR_DOT)
        if len(parts) < 2:
            return None

        current_type = self._resolve_java_object_type(
            parts[0], local_var_types, module_qn, context_node
        )
        if not current_type:
            return None

        for field_name in parts[1:]:
            next_type = self._lookup_java_field_type(
                current_type, field_name, module_qn
            )
            if not next_type:
                return None
            current_type = next_type

        return current_type

    def _find_parent_class(self, class_qn: str) -> str | None:
        parent_classes = self.class_inheritance.get(class_qn, [])
        return parent_classes[0] if parent_classes else None

    def _resolve_sibling_class_qn(self, class_name: str, module_qn: str) -> str | None:
        # Resolve a bare class name to a registered Class/Interface in a SIBLING file
        # of the same package (directory), so an unqualified same-package reference
        # resolves without an import. A bare receiver with no import is only valid for
        # the current package in Java, so a class in another package is NOT a match:
        # linking it would be a wrong cross-package edge, so leave the receiver
        # unresolved instead.
        if not (candidate_modules := self._fqn_to_module_qn.get(class_name)):
            return None
        if not (current_file := self.module_qn_to_file_path.get(module_qn)):
            return None
        current_dir = current_file.parent
        for candidate_module in candidate_modules:
            candidate_qn = f"{candidate_module}{cs.SEPARATOR_DOT}{class_name}"
            if candidate_qn not in self.function_registry or self.function_registry[
                candidate_qn
            ] not in (NodeType.CLASS, NodeType.INTERFACE):
                continue
            candidate_file = self.module_qn_to_file_path.get(candidate_module)
            if candidate_file and candidate_file.parent == current_dir:
                return candidate_qn
        return None

    def _resolve_static_or_local_method(
        self,
        method_name: str,
        module_qn: str,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        matches = [
            (entity_type, qn)
            for qn, entity_type in self.function_registry.find_with_prefix(module_qn)
            if entity_type in cs.JAVA_CALLABLE_ENTITY_TYPES
            and qn.split(cs.CHAR_PAREN_OPEN)[0].endswith(
                f"{cs.SEPARATOR_DOT}{method_name}"
            )
            and _callable_visible_to_caller(entity_type, qn, caller_qn)
        ]
        return _pick_overload(matches, arg_count, arg_types)

    def _resolve_instance_method(
        self,
        object_type: str,
        method_name: str,
        module_qn: str,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        resolved_type = self._resolve_java_type_name(object_type, module_qn)

        if method_result := self._find_method_with_any_signature(
            resolved_type, method_name, module_qn, arg_count, arg_types
        ):
            return method_result

        if inherited_result := self._find_inherited_method(
            resolved_type, method_name, module_qn, arg_count, arg_types
        ):
            return inherited_result

        return self._find_interface_method(
            resolved_type, method_name, module_qn, arg_count, arg_types
        )

    def _find_method_with_any_signature(
        self,
        class_qn: str,
        method_name: str,
        current_module_qn: str | None = None,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        if class_qn:
            if result := self._search_method_in_class(
                class_qn, method_name, arg_count, arg_types
            ):
                return result

        if class_qn and not class_qn.startswith(self.project_name):
            return self._search_method_in_alternate_modules(
                class_qn, method_name, current_module_qn, arg_count, arg_types
            )

        return None

    def _search_method_in_class(
        self,
        class_qn: str,
        method_name: str,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        matches: list[tuple[str, str]] = []
        for qn, method_type in self._find_registry_entries_under(class_qn):
            if qn == class_qn:
                continue
            suffix = qn[len(class_qn) :]
            if not suffix.startswith(cs.SEPARATOR_DOT):
                continue
            member = suffix[1:]
            if self._is_matching_method(member, method_name):
                matches.append((method_type, qn))
        return _pick_overload(matches, arg_count, arg_types)

    def _search_method_in_alternate_modules(
        self,
        class_qn: str,
        method_name: str,
        current_module_qn: str | None,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        suffixes = class_qn.split(cs.SEPARATOR_DOT)
        lookup_keys = [
            cs.SEPARATOR_DOT.join(suffixes[i:]) for i in range(len(suffixes))
        ] or [class_qn]

        candidate_modules = self._collect_candidate_modules(lookup_keys)
        ranked_candidates = self._rank_module_candidates(
            candidate_modules, class_qn, current_module_qn
        )

        simple_class_name = class_qn.rsplit(cs.SEPARATOR_DOT, maxsplit=1)[-1]

        for module_qn in ranked_candidates:
            registry_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{simple_class_name}"
            if result := self._search_method_in_class(
                registry_class_qn, method_name, arg_count, arg_types
            ):
                return result

        return None

    def _collect_candidate_modules(self, lookup_keys: list[str]) -> list[str]:
        candidate_modules: list[str] = []
        seen_modules: set[str] = set()

        for key in lookup_keys:
            if key in self._fqn_to_module_qn:
                for module_candidate in self._fqn_to_module_qn[key]:
                    if module_candidate not in seen_modules:
                        candidate_modules.append(module_candidate)
                        seen_modules.add(module_candidate)

        return candidate_modules

    def _is_matching_method(self, member: str, method_name: str) -> bool:
        return (
            member == method_name
            or member.startswith(f"{method_name}{cs.CHAR_PAREN_OPEN}")
            or member == f"{method_name}{cs.EMPTY_PARENS}"
        )

    @recursion_guard(
        key_func=lambda self, class_qn, *_, **__: class_qn,
        guard_name=cs.GUARD_INHERITED_METHOD,
    )
    def _find_inherited_method(
        self,
        class_qn: str,
        method_name: str,
        module_qn: str,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        if not (superclass_qn := self._get_superclass_name(class_qn)):
            return None

        if method_result := self._find_method_with_any_signature(
            superclass_qn, method_name, module_qn, arg_count, arg_types
        ):
            return method_result

        return self._find_inherited_method(
            superclass_qn, method_name, module_qn, arg_count, arg_types
        )

    def _find_interface_method(
        self,
        class_qn: str,
        method_name: str,
        module_qn: str,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        for interface_qn in self._get_implemented_interfaces(class_qn):
            if method_result := self._find_method_with_any_signature(
                interface_qn, method_name, module_qn, arg_count, arg_types
            ):
                return method_result

        return None

    def _resolve_java_method_return_type(
        self, method_call: str, module_qn: str
    ) -> str | None:
        if not method_call:
            return None

        parts = method_call.split(cs.SEPARATOR_DOT)
        if len(parts) < 2:
            method_name = method_call
            if (current_class_qn := self._get_current_class_name(module_qn)) and (
                result := self._find_method_return_type(current_class_qn, method_name)
            ):
                return result
        else:
            object_part = cs.SEPARATOR_DOT.join(parts[:-1])
            method_name = parts[-1]

            if object_part in self.function_registry:
                return self._find_method_return_type(object_part, method_name)

            if object_type := self._lookup_variable_type(object_part, module_qn):
                return self._find_method_return_type(object_type, method_name)

            potential_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{object_part}"
            if potential_class_qn in self.function_registry:
                return self._find_method_return_type(potential_class_qn, method_name)

        return self._heuristic_method_return_type(method_call)

    def _find_method_return_type(
        self,
        class_qn: str,
        method_name: str,
        param_types: tuple[str, ...] = (),
    ) -> str | None:
        if not class_qn or not method_name:
            return None

        ctx = get_class_context_from_qn(
            class_qn, self.module_qn_to_file_path, self.ast_cache
        )
        if not ctx:
            return None

        return self._find_method_return_type_in_ast(
            ctx.root_node,
            ctx.target_class_name,
            method_name,
            ctx.module_qn,
            param_types,
        )

    def _find_method_return_type_in_ast(
        self,
        node: ASTNode,
        class_name: str,
        method_name: str,
        module_qn: str,
        param_types: tuple[str, ...] = (),
    ) -> str | None:
        # Interfaces, enums and records declare methods too, and instance-method
        # lookup already binds through them, so a chain whose inner call is
        # declared on one must be typeable the same way.
        if node.type in cs.JAVA_CLASS_NODE_TYPES:
            if (
                name_node := node.child_by_field_name(cs.KEY_NAME)
            ) and safe_decode_text(name_node) == class_name:
                if body_node := node.child_by_field_name(cs.FIELD_BODY):
                    return self._search_methods_in_class_body(
                        body_node, method_name, module_qn, param_types
                    )

        for child in node.children:
            if result := self._find_method_return_type_in_ast(
                child, class_name, method_name, module_qn, param_types
            ):
                return result

        return None

    def _search_methods_in_class_body(
        self,
        body_node: ASTNode,
        method_name: str,
        module_qn: str,
        param_types: tuple[str, ...] = (),
    ) -> str | None:
        named = [
            child
            for child in body_node.children
            if child.type == cs.TS_METHOD_DECLARATION
            and (name_node := child.child_by_field_name(cs.KEY_NAME)) is not None
            and safe_decode_text(name_node) == method_name
        ]
        chosen = _pick_declared_overload(named, param_types)
        if chosen is None:
            return None
        if (type_node := chosen.child_by_field_name(cs.KEY_TYPE)) and (
            return_type := safe_decode_text(type_node)
        ):
            return self._resolve_java_type_name(return_type, module_qn)
        return None

    def _heuristic_method_return_type(self, method_call: str) -> str | None:
        method_lower = method_call.lower()
        if cs.JAVA_GETTER_PATTERN in method_lower:
            if cs.JAVA_NAME_PATTERN in method_lower:
                return cs.JAVA_TYPE_STRING_FQN
            if cs.JAVA_ID_PATTERN in method_lower:
                return cs.JAVA_TYPE_LONG
            if (
                cs.JAVA_SIZE_PATTERN in method_lower
                or cs.JAVA_LENGTH_PATTERN in method_lower
            ):
                return cs.JAVA_TYPE_INT

        if (
            cs.JAVA_CREATE_PATTERN in method_lower
            or cs.JAVA_NEW_PATTERN in method_lower
        ):
            parts = method_call.split(cs.SEPARATOR_DOT)
            if len(parts) >= 2:
                method_name_lower = parts[-1].lower()
                if cs.JAVA_USER_PATTERN in method_name_lower:
                    return cs.JAVA_HEURISTIC_USER
                if cs.JAVA_ORDER_PATTERN in method_name_lower:
                    return cs.JAVA_HEURISTIC_ORDER

        if cs.JAVA_IS_PATTERN in method_lower or cs.JAVA_HAS_PATTERN in method_lower:
            return cs.JAVA_TYPE_BOOLEAN

        return None

    def _infer_arg_types(
        self, call_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> tuple[str | None, ...]:
        # Infer the simple type of each argument so same-arity overloads can be told
        # apart (isX(String) vs isX(Class)). An argument whose type cannot be
        # inferred is None (unknown), which _overload_rank treats as
        # a wildcard.
        args_node = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if not args_node:
            return ()
        arg_types: list[str | None] = []
        for child in args_node.children:
            if child.type in cs.DELIMITER_TOKENS:
                continue
            if child.type != cs.TS_IDENTIFIER:
                # A literal carries its type in its node type, and a `new T()`
                # or a call carries it in the expression: the shared inference
                # types all of them (issue #1344).
                arg_types.append(
                    self._infer_java_type_from_expression(
                        child, module_qn, local_var_types
                    )
                )
                continue
            name = safe_decode_text(child)
            var_type = local_var_types.get(name) if name else None
            if not var_type and name:
                var_type = self._lookup_variable_type(name, module_qn)
            arg_types.append(var_type or None)
        return tuple(arg_types)

    @staticmethod
    def _has_call_receiver(call_node: ASTNode) -> bool:
        receiver = call_node.child_by_field_name(cs.TS_FIELD_OBJECT)
        return receiver is not None and receiver.type == cs.TS_METHOD_INVOCATION

    def _chained_receiver_type(
        self,
        call_node: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> str | None:
        receiver = call_node.child_by_field_name(cs.TS_FIELD_OBJECT)
        if receiver is None:
            return None
        # Recursion walks the chain leftwards; the leftmost receiver is an
        # identifier or field access, which the existing paths already type.
        resolved = self._do_resolve_java_method_call(
            receiver, local_var_types, module_qn
        )
        if not resolved:
            return None
        return self._declared_return_type_of(resolved[1])

    def _declared_return_type_of(self, method_qn: str) -> str | None:
        open_idx = method_qn.find(cs.CHAR_PAREN_OPEN)
        unsignatured = method_qn[:open_idx] if open_idx >= 0 else method_qn
        class_qn, _, method_name = unsignatured.rpartition(cs.SEPARATOR_DOT)
        if not class_qn or not method_name:
            return None
        # The signature of the OVERLOAD the inner call resolved to: overloads
        # may declare different return types, and a name-only lookup would type
        # the chain from whichever one is declared first.
        return self._find_method_return_type(
            class_qn, method_name, tuple(_java_param_type_names(method_qn))
        )

    def _do_resolve_java_method_call(
        self,
        call_node: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        if call_node.type != cs.TS_METHOD_INVOCATION:
            return None

        call_info = extract_method_call_info(call_node)
        if not call_info:
            return None

        method_name = call_info[cs.FIELD_NAME]
        object_ref = call_info[cs.FIELD_OBJECT]
        arg_count = call_info[cs.FIELD_ARGUMENTS]
        arg_types = self._infer_arg_types(call_node, local_var_types, module_qn)

        if not method_name:
            logger.debug(ls.JAVA_NO_METHOD_NAME)
            return None

        logger.debug(ls.JAVA_RESOLVING_CALL, method=method_name, object=object_ref)

        # A chained step (`from(..).where(..)`) has a method_invocation receiver,
        # which carries no name to look up. Typing it means resolving the inner
        # call first and reading its DECLARED return type -- the same thing the
        # compiler does, and what makes `return this;` builders resolve.
        if self._has_call_receiver(call_node):
            if chained_type := self._chained_receiver_type(
                call_node, local_var_types, module_qn
            ):
                logger.debug(ls.JAVA_OBJ_TYPE_RESOLVED, type=chained_type)
                return self._resolve_instance_method(
                    chained_type, str(method_name), module_qn, arg_count, arg_types
                )
            # An untypeable receiver (a call into a third-party type) leaves the
            # step unresolved. It must not reach the unqualified path below:
            # `expr().m()` is never `this.m()`, and that scan binds by name
            # alone, which is how the chain used to land on an unrelated class.
            logger.debug(ls.JAVA_OBJ_TYPE_UNKNOWN, object=method_name)
            return None

        if not object_ref:
            logger.debug(ls.JAVA_RESOLVING_STATIC, method=method_name)
            # An unqualified call `m(...)` is `this.m(...)`. Inside a method-body
            # anonymous class (`new Base(){ read(){ m(); } }`), `this` is the anon,
            # so bind against the anon's base type FIRST: `_lexical_class_qn` only
            # sees the enclosing NAMED class and would mis-bind an inherited call to
            # a same-named method there. Then the enclosing class hierarchy; the bare
            # module-wide scan is the last resort (it ignores lexical scope).
            if (
                anon_base_qn := self._enclosing_anon_base_qn(call_node, module_qn)
            ) and (
                result := self._resolve_instance_method(
                    anon_base_qn, str(method_name), module_qn, arg_count, arg_types
                )
            ):
                logger.debug(ls.JAVA_FOUND_STATIC, result=result)
                return result
            if (enclosing_qn := self._lexical_class_qn(call_node, module_qn)) and (
                result := self._resolve_instance_method(
                    enclosing_qn, str(method_name), module_qn, arg_count, arg_types
                )
            ):
                logger.debug(ls.JAVA_FOUND_STATIC, result=result)
                return result
            result = self._resolve_static_or_local_method(
                str(method_name), module_qn, arg_count, arg_types, caller_qn
            )
            if result:
                logger.debug(ls.JAVA_FOUND_STATIC, result=result)
            else:
                logger.debug(ls.JAVA_STATIC_NOT_FOUND, method=method_name)
            return result

        logger.debug(ls.JAVA_RESOLVING_OBJ_TYPE, object=object_ref)
        if not (
            object_type := self._resolve_java_object_type(
                str(object_ref), local_var_types, module_qn, call_node
            )
        ):
            logger.debug(ls.JAVA_OBJ_TYPE_UNKNOWN, object=object_ref)
            return None

        logger.debug(ls.JAVA_OBJ_TYPE_RESOLVED, type=object_type)
        result = self._resolve_instance_method(
            object_type, str(method_name), module_qn, arg_count, arg_types
        )
        if result:
            logger.debug(ls.JAVA_FOUND_INSTANCE, result=result)
        else:
            logger.debug(
                ls.JAVA_INSTANCE_NOT_FOUND, type=object_type, method=method_name
            )
        return result
