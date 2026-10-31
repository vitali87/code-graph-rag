from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, NamedTuple

from ... import constants as cs
from ...types_defs import ASTNode, NodeType
from .utils import find_package_start_index, safe_decode_text

if TYPE_CHECKING:
    from pathlib import Path

    from ...types_defs import ASTCacheProtocol, FunctionRegistryTrieProtocol
    from ..import_processor import ImportProcessor

# Node types an imported Java name can declare (records and annotation types
# register as CLASS).
JAVA_TYPE_DECL_NODE_TYPES = (NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM)

# The declarations a module's "current class" is read from: the first of
# these in the file.
_JAVA_HEADER_DECL_TYPES = (
    cs.TS_CLASS_DECLARATION,
    cs.TS_INTERFACE_DECLARATION,
    cs.TS_ENUM_DECLARATION,
)

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


class JavaClassHeaders(NamedTuple):
    """What one file's type declarations name in their headers.

    Names are kept as written: resolving one reads the import map and the
    registry, so it happens at lookup time exactly as before. `superclasses`
    and `interfaces` hold, per class name, the first class declaration a
    pre-order walk of the file reaches that answers the lookup.
    """

    type_names: tuple[str, ...]
    superclasses: dict[str, str]
    interfaces: dict[str, tuple[str, ...]]


def _declared_name(node: ASTNode) -> str | None:
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    return safe_decode_text(name_node) if name_node else None


def _header_type_name(parent_node: ASTNode) -> str | None:
    for child in parent_node.children:
        if child.type == cs.TS_GENERIC_TYPE:
            for subchild in child.children:
                if subchild.type == cs.TS_TYPE_IDENTIFIER:
                    return safe_decode_text(subchild)
        elif child.type == cs.TS_TYPE_IDENTIFIER:
            return safe_decode_text(child)
    return None


def _interface_type_names(interfaces_node: ASTNode, names: list[str]) -> None:
    for child in interfaces_node.children:
        if child.type == cs.TS_TYPE_IDENTIFIER:
            if interface_name := safe_decode_text(child):
                names.append(interface_name)
        elif child.children:
            _interface_type_names(child, names)


def _record_superclass(
    class_node: ASTNode, name: str, superclasses: dict[str, str]
) -> None:
    if name in superclasses:
        return
    if (superclass_node := class_node.child_by_field_name(cs.FIELD_SUPERCLASS)) and (
        superclass := _header_type_name(superclass_node)
    ):
        superclasses[name] = superclass


def _record_interfaces(
    class_node: ASTNode,
    name: str,
    interfaces: dict[str, tuple[str, ...]],
    cut: frozenset[str],
) -> frozenset[str]:
    # A class whose `implements` clause names no type (error recovery)
    # answers nothing, and the lookup moves on to the next declaration of
    # that name without looking inside this one, as the per-name walk this
    # index replaced did. `cut` carries such names into the class's body.
    if name in interfaces or name in cut:
        return cut
    if not (interfaces_node := class_node.child_by_field_name(cs.FIELD_INTERFACES)):
        return cut
    names: list[str] = []
    _interface_type_names(interfaces_node, names)
    if names:
        interfaces[name] = tuple(names)
        return cut
    return cut | {name}


def collect_java_class_headers(root: ASTNode) -> JavaClassHeaders:
    """Every class header of the file, read off one pre-order walk."""
    type_names: list[str] = []
    superclasses: dict[str, str] = {}
    interfaces: dict[str, tuple[str, ...]] = {}
    stack: list[tuple[ASTNode, frozenset[str]]] = [(root, frozenset())]
    while stack:
        node, cut = stack.pop()
        if node.type in _JAVA_HEADER_DECL_TYPES and (name := _declared_name(node)):
            type_names.append(name)
            if node.type == cs.TS_CLASS_DECLARATION:
                _record_superclass(node, name, superclasses)
                cut = _record_interfaces(node, name, interfaces, cut)
        stack.extend((child, cut) for child in reversed(node.children))
    return JavaClassHeaders(tuple(type_names), superclasses, interfaces)


class JavaTypeResolverMixin:
    __slots__ = ()
    import_processor: ImportProcessor
    function_registry: FunctionRegistryTrieProtocol
    module_qn_to_file_path: dict[str, Path]
    ast_cache: ASTCacheProtocol
    _fqn_to_module_qn: dict[str, list[str]]
    class_inheritance: dict[str, list[str]]
    _class_headers: dict[Path, JavaClassHeaders]
    _supertype_walks: dict[str, tuple[str, ...]]
    # Annotation only, never a def: this mixin precedes JavaMethodResolverMixin
    # in the MRO, so a stub here would shadow the real implementation.
    _java_direct_supertypes: Callable[[str], tuple[list[str], bool]]

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
            and self.function_registry[target] in JAVA_TYPE_DECL_NODE_TYPES
        ):
            return target
        class_qn = f"{target}{cs.SEPARATOR_DOT}{type_name}"
        if (
            class_qn in self.function_registry
            and self.function_registry[class_qn] in JAVA_TYPE_DECL_NODE_TYPES
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
                    and self.function_registry[member] in JAVA_TYPE_DECL_NODE_TYPES
                ):
                    return member
        return None

    def _type_and_supertypes(self, class_qn: str) -> tuple[str, ...]:
        """`class_qn`, then its superclasses and superinterfaces, nearest
        first, each once."""
        # Every scoped parameter and local walks the hierarchies around it,
        # and reading a type's interfaces reads its declaration, so a walk is
        # kept until the pass's memos are reset.
        if (walk := self._supertype_walks.get(class_qn)) is not None:
            return walk
        order: list[str] = []
        seen = {class_qn}
        pending = [class_qn]
        while pending:
            current = pending.pop(0)
            order.append(current)
            # class_inheritance holds a superclass and an interface's
            # superinterfaces but never the interfaces a class implements,
            # which are read off its declaration, nested or not (CodeRabbit,
            # PR #2973).
            for supertype in self._java_direct_supertypes(current)[0]:
                if supertype not in seen:
                    seen.add(supertype)
                    pending.append(supertype)
        walk = self._supertype_walks[class_qn] = tuple(order)
        return walk

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
            resolved_base = self._resolve_java_type_name(base_type, module_qn, scope)
            return f"{resolved_base}{cs.JAVA_ARRAY_SUFFIX}"

        if cs.CHAR_ANGLE_OPEN in type_name and cs.CHAR_ANGLE_CLOSE in type_name:
            base_type = type_name.split(cs.CHAR_ANGLE_OPEN, maxsplit=1)[0]
            return self._resolve_java_type_name(base_type, module_qn, scope)

        if scope is not None and (
            member := self._scoped_member_type(type_name, module_qn, scope)
        ):
            return member

        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if type_name in import_map:
                return self._imported_class_qn(import_map[type_name], type_name)

        return self._module_local_type(type_name, module_qn) or type_name

    def _module_local_type(self, type_name: str, module_qn: str) -> str | None:
        # An enum is a type declaration like a class or an interface: a
        # receiver of a nested enum's type was left untyped (issue #2922).
        same_package_qn = f"{module_qn}{cs.SEPARATOR_DOT}{type_name}"
        if (
            same_package_qn in self.function_registry
            and self.function_registry[same_package_qn] in JAVA_TYPE_DECL_NODE_TYPES
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
            if qn.endswith(suffix) and entity_type in JAVA_TYPE_DECL_NODE_TYPES
        ]
        if len(nested) == 1:
            return nested[0]

        return None

    def _java_class_headers(self, module_qn: str) -> JavaClassHeaders | None:
        # Call resolution asks for a class's superclass and interfaces once
        # per call site and per level of an inherited-method climb; walking
        # the whole file for each answer cost gson most of its sync (#2470).
        # A tree holds still for a whole call pass, so one walk serves every
        # lookup until the pass's memos are reset.
        file_path = self.module_qn_to_file_path.get(module_qn)
        if file_path is None:
            return None
        if (headers := self._class_headers.get(file_path)) is not None:
            return headers
        if not (entry := self.ast_cache.load(file_path)):
            return None
        headers = collect_java_class_headers(entry[0])
        self._class_headers[file_path] = headers
        return headers

    def _get_superclass_name(self, class_qn: str) -> str | None:
        module_qn, _, class_name = class_qn.rpartition(cs.SEPARATOR_DOT)
        if not module_qn or not (headers := self._java_class_headers(module_qn)):
            return None
        if superclass := headers.superclasses.get(class_name):
            return self._resolve_java_type_name(superclass, module_qn)
        return None

    def _get_implemented_interfaces(self, class_qn: str) -> list[str]:
        module_qn, _, class_name = class_qn.rpartition(cs.SEPARATOR_DOT)
        if not module_qn or not (headers := self._java_class_headers(module_qn)):
            return []
        return [
            self._resolve_java_type_name(interface, module_qn)
            for interface in headers.interfaces.get(class_name, ())
        ]

    def _get_current_class_name(self, module_qn: str) -> str | None:
        if cs.SEPARATOR_DOT not in module_qn or not (
            headers := self._java_class_headers(module_qn)
        ):
            return None
        if not headers.type_names:
            return None
        return f"{module_qn}{cs.SEPARATOR_DOT}{headers.type_names[0]}"
