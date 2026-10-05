from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

from ... import constants as cs
from ...types_defs import ASTNode, NodeType
from .utils import (
    find_package_start_index,
    get_class_context_from_qn,
    get_root_node_from_module_qn,
    safe_decode_text,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ...types_defs import ASTCacheProtocol, FunctionRegistryTrieProtocol
    from ..import_processor import ImportProcessor

# Node types an imported Java name can declare (records and annotation types
# register as CLASS).
_JAVA_TYPE_DECL_NODE_TYPES = (NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM)

# The declarations that open a member-type scope: a simple type name read
# inside one resolves first to a type it declares.
_JAVA_TYPE_DECL_AST_TYPES = frozenset(
    {
        cs.TS_CLASS_DECLARATION,
        cs.TS_INTERFACE_DECLARATION,
        cs.TS_ENUM_DECLARATION,
        cs.TS_RECORD_DECLARATION,
    }
)


class JavaTypeResolverMixin:
    __slots__ = ()
    import_processor: ImportProcessor
    function_registry: FunctionRegistryTrieProtocol
    module_qn_to_file_path: dict[str, Path]
    ast_cache: ASTCacheProtocol
    _fqn_to_module_qn: dict[str, list[str]]
    class_inheritance: dict[str, list[str]]

    def _module_qn_to_java_fqn(self, module_qn: str) -> str | None:
        parts = module_qn.split(cs.SEPARATOR_DOT)
        package_start_idx = find_package_start_index(parts)
        if package_start_idx is None:
            return None
        class_parts = parts[package_start_idx:]
        return cs.SEPARATOR_DOT.join(class_parts) if class_parts else None

    def _calculate_module_distance(
        self, candidate_qn: str, caller_module_qn: str
    ) -> int:
        caller_parts = caller_module_qn.split(cs.SEPARATOR_DOT)
        candidate_parts = candidate_qn.split(cs.SEPARATOR_DOT)

        common_prefix = 0
        for caller_part, candidate_part in zip(caller_parts, candidate_parts):
            if caller_part == candidate_part:
                common_prefix += 1
            else:
                break

        base_distance = max(len(caller_parts), len(candidate_parts)) - common_prefix

        if (
            len(caller_parts) > 1
            and candidate_parts[: len(caller_parts) - 1] == caller_parts[:-1]
        ):
            base_distance -= 1

        return max(base_distance, 0)

    def _rank_module_candidates(
        self,
        candidates: list[str],
        class_qn: str,
        current_module_qn: str | None,
    ) -> list[str]:
        if not candidates or not current_module_qn:
            return candidates

        ranked: list[tuple[tuple[int, int, int], str]] = []
        for idx, candidate in enumerate(candidates):
            candidate_fqn = self._module_qn_to_java_fqn(candidate)

            if candidate_fqn == class_qn:
                match_penalty = 0
            elif candidate_fqn and class_qn.endswith(candidate_fqn):
                match_penalty = 1
            else:
                match_penalty = 2

            distance = self._calculate_module_distance(candidate, current_module_qn)
            ranked.append(((match_penalty, distance, idx), candidate))

        ranked.sort(key=lambda item: item[0])
        return [candidate for _, candidate in ranked]

    def _find_registry_entries_under(self, prefix: str) -> Iterable[tuple[str, str]]:
        finder = getattr(self.function_registry, cs.METHOD_FIND_WITH_PREFIX, None)
        if callable(finder):
            if matches := list(finder(prefix)):
                return matches

        items = getattr(self.function_registry, cs.METHOD_ITEMS, None)
        if callable(items):
            prefix_with_dot = f"{prefix}{cs.SEPARATOR_DOT}"
            return [
                (qn, method_type)
                for qn, method_type in items()
                if qn.startswith(prefix_with_dot) or qn == prefix
            ]

        return []

    def _imported_class_qn(self, target: str, type_name: str) -> str:
        # A LOCAL Java import is recorded as the imported file's MODULE qn
        # (repo.com.foo.util.Helper), but the registered class qn duplicates the
        # class segment (repo.com.foo.util.Helper.Helper); the raw target then
        # dead-ends because the project prefix also disables the fqn-map fallback.
        # Append the imported simple name when THAT is the registered class.
        # Registry-guarded, so targets already class qns and external fqns pass
        # through unchanged.
        if (
            target in self.function_registry
            and self.function_registry[target] in _JAVA_TYPE_DECL_NODE_TYPES
        ):
            return target
        class_qn = f"{target}{cs.SEPARATOR_DOT}{type_name}"
        if (
            class_qn in self.function_registry
            and self.function_registry[class_qn] in _JAVA_TYPE_DECL_NODE_TYPES
        ):
            return class_qn
        return target

    def _enclosing_type_qns(self, scope: ASTNode, module_qn: str) -> list[str]:
        """The qualified names of the named types enclosing `scope`,
        innermost first: `module.Outer.Inner`, then `module.Outer`."""
        names: list[str] = []
        current = scope.parent
        while current is not None:
            if current.type in _JAVA_TYPE_DECL_AST_TYPES:
                name_node = current.child_by_field_name(cs.FIELD_NAME)
                if name_node is None or not (name := safe_decode_text(name_node)):
                    return []
                names.append(name)
            current = current.parent
        names.reverse()
        return [
            cs.SEPARATOR_DOT.join([module_qn, *names[:depth]])
            for depth in range(len(names), 0, -1)
        ]

    def _scoped_member_type(
        self, type_name: str, module_qn: str, scope: ASTNode
    ) -> str | None:
        # Java reads a simple type name as a member of the innermost
        # enclosing type that declares one, ahead of the file's other types
        # and its imports, so a same-named type elsewhere in the file does
        # not make it ambiguous (Greptile, PR #2973).
        # Each enclosing type's members include those it inherits, ahead of
        # the next type out (JLS 8.5; CodeRabbit, PR #2973).
        for enclosing in self._enclosing_type_qns(scope, module_qn):
            for owner in self._type_and_supertypes(enclosing):
                member = f"{owner}{cs.SEPARATOR_DOT}{type_name}"
                if (
                    member in self.function_registry
                    and self.function_registry[member] in _JAVA_TYPE_DECL_NODE_TYPES
                ):
                    return member
        return None

    def _type_and_supertypes(self, class_qn: str) -> Iterator[str]:
        """`class_qn`, then its superclasses and superinterfaces, nearest
        first, each once."""
        seen = {class_qn}
        pending = [class_qn]
        while pending:
            current = pending.pop(0)
            yield current
            # The hierarchy the definition pass resolved: superclass and
            # interfaces, by their registered qns.
            for supertype in self.class_inheritance.get(current, ()):
                if supertype not in seen:
                    seen.add(supertype)
                    pending.append(supertype)

    def _resolve_java_type_name(
        self, type_name: str, module_qn: str, scope: ASTNode | None = None
    ) -> str:
        if not type_name:
            return cs.JAVA_TYPE_OBJECT

        if cs.SEPARATOR_DOT in type_name:
            return type_name

        if type_name in cs.JAVA_PRIMITIVE_TYPES:
            return type_name

        if type_name in cs.JAVA_WRAPPER_TYPES:
            return f"{cs.JAVA_LANG_PREFIX}{type_name}"

        if type_name.endswith(cs.JAVA_ARRAY_SUFFIX):
            base_type = type_name[:-2]
            resolved_base = self._resolve_java_type_name(base_type, module_qn)
            return f"{resolved_base}{cs.JAVA_ARRAY_SUFFIX}"

        if cs.CHAR_ANGLE_OPEN in type_name and cs.CHAR_ANGLE_CLOSE in type_name:
            base_type = type_name.split(cs.CHAR_ANGLE_OPEN, maxsplit=1)[0]
            return self._resolve_java_type_name(base_type, module_qn)

        if scope is not None and (
            member := self._scoped_member_type(type_name, module_qn, scope)
        ):
            return member

        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if type_name in import_map:
                return self._imported_class_qn(import_map[type_name], type_name)

        # An enum is a type declaration like a class or an interface: a
        # receiver of a nested enum's type was left untyped (issue #2922).
        same_package_qn = f"{module_qn}{cs.SEPARATOR_DOT}{type_name}"
        if (
            same_package_qn in self.function_registry
            and self.function_registry[same_package_qn] in _JAVA_TYPE_DECL_NODE_TYPES
        ):
            return same_package_qn

        # A nested class referenced by its simple name from within the same file
        # (`RECORD_HELPER` typed by the nested `RecordHelper`): the qn is
        # `module.Outer.Nested`, not `module.Nested`, so the direct check above
        # misses it. Search only this module's trie subtree (bounded, not a
        # whole-registry scan) for a type declaration whose last segment is the simple
        # name, used only when unambiguous so a same-named nested type elsewhere
        # cannot mis-resolve. The trie indexes by dot segment, so find_with_prefix
        # already excludes character-level prefix collisions.
        suffix = f"{cs.SEPARATOR_DOT}{type_name}"
        nested = [
            qn
            for qn, entity_type in self.function_registry.find_with_prefix(module_qn)
            if qn.endswith(suffix) and entity_type in _JAVA_TYPE_DECL_NODE_TYPES
        ]
        if len(nested) == 1:
            return nested[0]

        return type_name

    def _get_superclass_name(self, class_qn: str) -> str | None:
        ctx = get_class_context_from_qn(
            class_qn, self.module_qn_to_file_path, self.ast_cache
        )
        if not ctx:
            return None

        return self._find_superclass_using_ast(
            ctx.root_node, ctx.target_class_name, ctx.module_qn
        )

    def _find_superclass_using_ast(
        self, node: ASTNode, target_class_name: str, module_qn: str
    ) -> str | None:
        if node.type == cs.TS_CLASS_DECLARATION:
            if (
                name_node := node.child_by_field_name(cs.FIELD_NAME)
            ) and safe_decode_text(name_node) == target_class_name:
                if superclass_node := node.child_by_field_name(cs.FIELD_SUPERCLASS):
                    if superclass_name := self._extract_type_name_from_node(
                        superclass_node
                    ):
                        return self._resolve_java_type_name(superclass_name, module_qn)

        for child in node.children:
            if result := self._find_superclass_using_ast(
                child, target_class_name, module_qn
            ):
                return result

        return None

    def _extract_type_name_from_node(self, parent_node: ASTNode) -> str | None:
        for child in parent_node.children:
            if child.type == cs.TS_GENERIC_TYPE:
                for subchild in child.children:
                    if subchild.type == cs.TS_TYPE_IDENTIFIER:
                        return safe_decode_text(subchild)
            elif child.type == cs.TS_TYPE_IDENTIFIER:
                return safe_decode_text(child)
        return None

    def _get_implemented_interfaces(self, class_qn: str) -> list[str]:
        parts = class_qn.split(cs.SEPARATOR_DOT)
        if len(parts) < 2:
            return []

        module_qn = cs.SEPARATOR_DOT.join(parts[:-1])
        target_class_name = parts[-1]

        file_path = self.module_qn_to_file_path.get(module_qn)
        if file_path is None or not (entry := self.ast_cache.load(file_path)):
            return []

        root_node, _ = entry

        return self._find_interfaces_using_ast(root_node, target_class_name, module_qn)

    def _find_interfaces_using_ast(
        self, node: ASTNode, target_class_name: str, module_qn: str
    ) -> list[str]:
        if node.type == cs.TS_CLASS_DECLARATION:
            if (
                name_node := node.child_by_field_name(cs.FIELD_NAME)
            ) and safe_decode_text(name_node) == target_class_name:
                if interfaces_node := node.child_by_field_name(cs.FIELD_INTERFACES):
                    interface_list: list[str] = []
                    self._extract_interface_names(
                        interfaces_node, interface_list, module_qn
                    )
                    return interface_list

        for child in node.children:
            if result := self._find_interfaces_using_ast(
                child, target_class_name, module_qn
            ):
                return result

        return []

    def _extract_interface_names(
        self, interfaces_node: ASTNode, interface_list: list[str], module_qn: str
    ) -> None:
        for child in interfaces_node.children:
            if child.type == cs.TS_TYPE_IDENTIFIER:
                if interface_name := safe_decode_text(child):
                    resolved_interface = self._resolve_java_type_name(
                        interface_name, module_qn
                    )
                    interface_list.append(resolved_interface)
            elif child.children:
                self._extract_interface_names(child, interface_list, module_qn)

    def _get_current_class_name(self, module_qn: str) -> str | None:
        root_node = get_root_node_from_module_qn(
            module_qn, self.module_qn_to_file_path, self.ast_cache
        )
        if not root_node:
            return None

        class_names: list[str] = []
        self._traverse_for_class_declarations(root_node, class_names)

        return f"{module_qn}{cs.SEPARATOR_DOT}{class_names[0]}" if class_names else None

    def _traverse_for_class_declarations(
        self, node: ASTNode, class_names: list[str]
    ) -> None:
        match node.type:
            case (
                cs.TS_CLASS_DECLARATION
                | cs.TS_INTERFACE_DECLARATION
                | cs.TS_ENUM_DECLARATION
            ):
                if (name_node := node.child_by_field_name(cs.FIELD_NAME)) and (
                    class_name := safe_decode_text(name_node)
                ):
                    class_names.append(class_name)

        for child in node.children:
            self._traverse_for_class_declarations(child, class_names)
