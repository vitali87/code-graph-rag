from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...types_defs import FunctionRegistryTrieProtocol, TreeSitterNodeProtocol
from ..utils import safe_decode_text
from . import utils as lua_utils

if TYPE_CHECKING:
    from ..import_processor import ImportProcessor


def _lua_method_index_names(
    index_expr: TreeSitterNodeProtocol,
) -> tuple[str | None, str | None]:
    # `Class:method(...)` / `Class.func(...)`: the first identifier names the
    # class, the last the method.
    class_name: str | None = None
    method_name: str | None = None
    for child in index_expr.children:
        if child.type != cs.TS_LUA_IDENTIFIER:
            continue
        if class_name is None:
            class_name = safe_decode_text(child)
        else:
            method_name = safe_decode_text(child)
    return class_name, method_name


class LuaTypeInferenceEngine:
    __slots__ = (
        "import_processor",
        "function_registry",
        "project_name",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        project_name: str,
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.project_name = project_name

    def build_local_variable_type_map(
        self, caller_node: TreeSitterNodeProtocol, module_qn: str
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}
        # Seeded before the walk, as Python and Rust seed their own `self`
        # from the enclosing class: `self` inside `function T:m()` is a T.
        # A `local self = ...` in the body is met later and wins.
        if isinstance(caller_node, Node) and (
            owner := lua_utils.method_self_owner(caller_node)
        ):
            local_var_types[cs.KEYWORD_SELF] = f"{module_qn}{cs.SEPARATOR_DOT}{owner}"
        stack: list[TreeSitterNodeProtocol] = [caller_node]

        while stack:
            current = stack.pop()
            if current.type == cs.TS_LUA_VARIABLE_DECLARATION:
                self._process_variable_declaration(current, module_qn, local_var_types)
            stack.extend(reversed(current.children))

        logger.debug(ls.LUA_VAR_TYPE_MAP_BUILT, count=len(local_var_types))
        return local_var_types

    def _process_variable_declaration(
        self,
        decl_node: TreeSitterNodeProtocol,
        module_qn: str,
        local_var_types: dict[str, str],
    ) -> None:
        assignment = next(
            (c for c in decl_node.children if c.type == cs.TS_LUA_ASSIGNMENT_STATEMENT),
            None,
        )
        if not assignment:
            return

        var_names = self._extract_var_names(assignment)
        func_calls = self._extract_function_calls(assignment)

        for i, var_name in enumerate(var_names):
            if i >= len(func_calls):
                break
            if var_type := self._infer_lua_variable_type_from_value(
                func_calls[i], module_qn, local_var_types
            ):
                local_var_types[var_name] = var_type
                logger.debug(ls.LUA_VAR_INFERRED, var_name=var_name, var_type=var_type)

    def _extract_var_names(self, assignment: TreeSitterNodeProtocol) -> list[str]:
        names: list[str] = []
        for child in assignment.children:
            if child.type != cs.TS_LUA_VARIABLE_LIST:
                continue
            for var_node in child.children:
                if var_node.type == cs.TS_LUA_IDENTIFIER:
                    if decoded := safe_decode_text(var_node):
                        names.append(decoded)
        return names

    def _extract_function_calls(
        self, assignment: TreeSitterNodeProtocol
    ) -> list[TreeSitterNodeProtocol]:
        calls: list[TreeSitterNodeProtocol] = []
        for child in assignment.children:
            if child.type != cs.TS_LUA_EXPRESSION_LIST:
                continue
            calls.extend(
                expr for expr in child.children if expr.type == cs.TS_LUA_FUNCTION_CALL
            )
        return calls

    def _infer_lua_variable_type_from_value(
        self,
        value_node: TreeSitterNodeProtocol,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        if value_node.type != cs.TS_LUA_FUNCTION_CALL:
            return None
        for child in value_node.children:
            match child.type:
                case cs.TS_LUA_METHOD_INDEX_EXPRESSION:
                    class_qn = self._infer_from_method_call(child, module_qn)
                case cs.TS_DOT_INDEX_EXPRESSION:
                    class_qn = self._infer_from_constructor_call(child, module_qn)
                case cs.TS_LUA_IDENTIFIER if safe_decode_text(
                    child
                ) == cs.LUA_SETMETATABLE and isinstance(value_node, Node):
                    class_qn = self._infer_from_setmetatable(
                        value_node, module_qn, local_var_types or {}
                    )
                case _:
                    continue
            if class_qn:
                return class_qn
        return None

    def _infer_from_method_call(
        self, index_expr: TreeSitterNodeProtocol, module_qn: str
    ) -> str | None:
        class_name, method_name = _lua_method_index_names(index_expr)
        if not (class_name and method_name):
            return None
        if class_qn := self._resolve_lua_class_name(class_name, module_qn):
            logger.debug(
                ls.LUA_TYPE_INFERENCE_RETURN,
                class_name=class_name,
                method_name=method_name,
                class_qn=class_qn,
            )
            return class_qn
        return None

    def _infer_from_constructor_call(
        self, index_expr: TreeSitterNodeProtocol, module_qn: str
    ) -> str | None:
        # `T.new(...)` builds a T exactly as `T:new(...)` does, but a dot call
        # is also how a plain function table (`util.split(s)`) is used. So it
        # types its result only when the callee is T's own member and T is a
        # class, i.e. declares colon-methods an instance could call.
        class_name, func_name = _lua_method_index_names(index_expr)
        if not (class_name and func_name):
            return None
        class_qn = self._resolve_lua_class_name(class_name, module_qn)
        if class_qn is None or not self._has_colon_methods(class_qn):
            return None
        if not any(
            qn in self.function_registry
            for qn in lua_utils.member_spellings(class_qn, func_name)
        ):
            return None
        logger.debug(
            ls.LUA_TYPE_INFERENCE_RETURN,
            class_name=class_name,
            method_name=func_name,
            class_qn=class_qn,
        )
        return class_qn

    def _infer_from_setmetatable(
        self, call_node: Node, module_qn: str, local_var_types: dict[str, str]
    ) -> str | None:
        # `setmetatable(obj, T)` returns `obj` with T's methods behind it. The
        # PIL constructor `function T:new(o) ... setmetatable(o, self)` names
        # T through `self`, typed already when the walk reaches this call.
        arguments = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if arguments is None or len(arguments.named_children) != 2:
            return None
        metatable = arguments.named_children[1]
        if metatable.type != cs.TS_LUA_IDENTIFIER:
            return None
        name = safe_decode_text(metatable)
        if not name:
            return None
        if name == cs.KEYWORD_SELF:
            return local_var_types.get(name)
        if class_qn := self._resolve_lua_class_name(name, module_qn):
            return class_qn
        # A metatable IS the type, so a table whose members are all
        # dot-defined (`function T.push(self, v)`) qualifies too.
        table_qn = f"{module_qn}{cs.SEPARATOR_DOT}{name}"
        return table_qn if self.function_registry.has_prefix(table_qn) else None

    def _has_colon_methods(self, table_qn: str) -> bool:
        # `function T:m()` registers `T:m` as ONE qn segment, beside `T` in
        # the registry trie rather than under it, so a prefix search on `T`
        # never sees it; search T's parent and match the colon prefix.
        parent_qn = table_qn.rpartition(cs.SEPARATOR_DOT)[0]
        method_prefix = f"{table_qn}{cs.LUA_METHOD_SEPARATOR}"
        return any(
            qn.startswith(method_prefix)
            for qn, _ in self.function_registry.find_with_prefix(parent_qn)
        )

    def _resolve_lua_class_name(self, class_name: str, module_qn: str) -> str | None:
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if class_name in import_map:
                imported_qn = import_map[class_name]
                full_class_qn = f"{imported_qn}{cs.SEPARATOR_DOT}{class_name}"
                return full_class_qn

        local_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
        if local_class_qn in self.function_registry:
            return local_class_qn

        return local_class_qn if self._has_colon_methods(local_class_qn) else None
