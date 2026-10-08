from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as lg
from ...types_defs import FunctionRegistryTrieProtocol, NodeType
from ..import_processor import ImportProcessor
from ..utils import follow_reexports, safe_decode_text
from .utils import resolve_class_name, resolve_dotted_class


class WithTarget(NamedTuple):
    """One `with <manager> as <name>` item: the plain name its `as` binds,
    the context manager expression, whether the statement is `async with`,
    and the byte offset of the name, which orders it among the body's other
    bindings of that name."""

    name: str
    manager: Node
    is_async: bool
    binds_at: int


if TYPE_CHECKING:
    # A plain class, not a Protocol: a protocol's stub methods count as
    # abstract, and this base sits ahead of the mixins that implement them in
    # the engine's MRO. It exists for the checker only.
    class _WithBindingDeps:
        def _infer_type_from_expression_simple(
            self, node: Node, module_qn: str
        ) -> str | None: ...

        def _infer_type_from_expression_complex(
            self, node: Node, module_qn: str, local_var_types: dict[str, str]
        ) -> str | None: ...

        def _get_mro(self, class_qn: str) -> list[str]: ...

        def _find_method_ast_node(self, method_qn: str) -> Node | None: ...

        def _find_return_statements(
            self, node: Node, return_nodes: list[Node]
        ) -> None: ...

        def _annotation_type_from_text(
            self, text: str, owner_qn: str, module_qn: str
        ) -> str | None: ...

        def own_class_rebinds_import(
            self, module_qn: str, name: str, scope: Node | None = None
        ) -> bool: ...

    _WithBase: type = _WithBindingDeps
else:
    _WithBase = object


def _receiver_parameter(method: Node) -> tuple[str | None, str | None]:
    """(name, annotation text) of a method's first parameter, the receiver."""
    params = method.child_by_field_name(cs.FIELD_PARAMETERS)
    first = params.named_children[0] if params and params.named_children else None
    if first is None:
        return None, None
    if first.type == cs.TS_PY_IDENTIFIER:
        return safe_decode_text(first), None
    ident = next(
        (c for c in first.named_children if c.type == cs.TS_PY_IDENTIFIER), None
    )
    annotation = first.child_by_field_name(cs.FIELD_TYPE)
    return (
        safe_decode_text(ident) if ident is not None else None,
        safe_decode_text(annotation) if annotation is not None else None,
    )


def _in_own_body(node: Node, method: Node) -> bool:
    """Whether `node` belongs to `method` itself rather than a nested def or class."""
    current = node.parent
    while current is not None and current.type not in (
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
    ):
        current = current.parent
    return current is not None and current.id == method.id


def _constructs_by_name(manager: Node) -> bool:
    """Whether the manager is a call to a capitalised name, `X(...)` or
    `mod.X(...)`: the constructor spelling the assignment passes already
    type a variable from."""
    if manager.type != cs.TS_PY_CALL:
        return False
    callee = manager.child_by_field_name(cs.TS_FIELD_FUNCTION)
    if callee is None or callee.type not in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
        return False
    name = (safe_decode_text(callee) or "").rsplit(cs.SEPARATOR_DOT, 1)[-1]
    return bool(name) and name[0].isupper()


def _is_class_path(type_name: str) -> bool:
    """A plain (dotted) name, not a union, a container marker or a subscript."""
    return all(part.isidentifier() for part in type_name.split(cs.SEPARATOR_DOT))


class PythonWithBindingMixin(_WithBase):
    """Types the `as` target of a with statement (issue #2558).

    `with M as v` binds `v` to `M.__enter__()`, and `async with` to the
    awaited `M.__aenter__()`, so the target's type is what that method
    returns: its return annotation, else what its `return` statements
    return. `return self` and `-> Self` name the class being entered, which
    for an inherited method is the subclass, not the class that defines it.

    The convention, when nothing indexed defines the method: the target is
    typed as the manager's own class. That covers a class outside the project
    (`with httpx.Client() as c`) and one inheriting the method from an
    unindexed base such as `contextlib.AbstractContextManager`, whose
    `__enter__` returns `self` -- the overwhelmingly common shape. It is the
    type `v = X()` already gets, so a later `v.m()` reads as a call on a known
    class instead of falling to the name-only fallback. An indexed method
    that returns something the pass cannot read (`return self._conn`, a bare
    `return`) leaves the target untyped: unknown, never guessed as the class.
    """

    __slots__ = ()
    import_processor: ImportProcessor
    function_registry: FunctionRegistryTrieProtocol

    def _type_with_targets(
        self,
        targets: list[WithTarget],
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> list[WithTarget]:
        """Type each target; return the ones whose manager has no type yet.

        Those are retried once the complex assignment pass has run, for a
        manager that is a local typed only by that pass
        (`client = make_client(); with client as c`).
        """
        pending: list[WithTarget] = []
        for target in targets:
            manager_type = self._manager_type(
                target.manager, local_var_types, module_qn
            )
            if not manager_type:
                pending.append(target)
                continue
            if entered := self._entered_type(target, manager_type, module_qn):
                local_var_types[target.name] = entered
                logger.debug(lg.PY_TYPE_WITH, var=target.name, type=entered)
        return pending

    def _manager_type(
        self, manager: Node, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        """The type of the context manager expression, read the way an
        assignment's right-hand side is."""
        if manager.type == cs.TS_PY_IDENTIFIER:
            return local_var_types.get(safe_decode_text(manager) or "")
        return self._infer_type_from_expression_simple(
            manager, module_qn
        ) or self._infer_type_from_expression_complex(
            manager, module_qn, local_var_types
        )

    def _entered_type(
        self, target: WithTarget, manager_type: str, module_qn: str
    ) -> str | None:
        class_qn = self._indexed_class_qn(manager_type, module_qn, target.manager)
        if class_qn is None:
            # An external class's `__enter__` cannot be read, so the
            # convention applies -- but only to a constructor call: a
            # factory's return type (`-> Generator`, `-> Iterator[W]`) is not
            # the class entered, and typing the target by it would be wrong.
            if _constructs_by_name(target.manager) and _is_class_path(manager_type):
                return manager_type
            return None
        dunder = cs.PY_DUNDER_AENTER if target.is_async else cs.PY_DUNDER_ENTER
        for owner in self._get_mro(class_qn):
            method_qn = f"{owner}{cs.SEPARATOR_DOT}{dunder}"
            if (method := self._find_method_ast_node(method_qn)) is not None:
                returned = self._enter_return_type(method, method_qn, class_qn)
                # The entered class itself keeps the manager's spelling, the
                # one `v = X()` stores.
                return manager_type if returned == class_qn else returned
        return manager_type

    def _enter_return_type(
        self, method: Node, method_qn: str, receiver_qn: str
    ) -> str | None:
        owner_module = method_qn.rsplit(cs.SEPARATOR_DOT, 2)[0]
        receiver, receiver_annotation = _receiver_parameter(method)
        if (annotation := method.child_by_field_name(cs.FIELD_RETURN_TYPE)) is not None:
            text = (safe_decode_text(annotation) or "").strip().strip("\"'")
            # `-> Self`, or the pre-3.11 idiom `def __enter__(self: T) -> T`.
            if (
                text.rsplit(cs.SEPARATOR_DOT, 1)[-1] == cs.PY_ANNOTATION_SELF
                or text == receiver_annotation
            ):
                return receiver_qn
            declared = self._annotation_type_from_text(text, method_qn, owner_module)
            return self._resolved_type(declared, owner_module) if declared else None
        return_nodes: list[Node] = []
        self._find_return_statements(method, return_nodes)
        values = [
            node.named_children[0] if node.named_children else None
            for node in return_nodes
            if _in_own_body(node, method)
        ]
        # No `return` at all hands back None, as does a bare `return`.
        found = {
            self._returned_type(value, receiver, receiver_qn, owner_module)
            for value in values
        }
        return found.pop() if len(found) == 1 else None

    def _returned_type(
        self,
        value: Node | None,
        receiver: str | None,
        receiver_qn: str,
        module_qn: str,
    ) -> str | None:
        if value is None:
            return None
        if value.type == cs.TS_PY_IDENTIFIER:
            return (
                receiver_qn
                if receiver and safe_decode_text(value) == receiver
                else None
            )
        if value.type != cs.TS_PY_CALL:
            # An attribute (`return self._conn`) or anything else holds a
            # value of unknown type, not the receiver.
            return None
        receiver_types = {receiver: receiver_qn} if receiver else {}
        inferred = self._infer_type_from_expression_simple(
            value, module_qn
        ) or self._infer_type_from_expression_complex(value, module_qn, receiver_types)
        return self._resolved_type(inferred, module_qn) if inferred else None

    def _resolved_type(self, type_name: str, module_qn: str) -> str | None:
        """`type_name` as named in `module_qn`, spelled so it resolves anywhere.

        The target's type crosses into the caller's module, where a bare name
        from the manager's module may be unbound or bound to something else:
        an indexed class goes by its qn, an external one by the dotted path
        its import names. Anything else (a builtin, a TypeVar, a container
        marker) is left untyped.
        """
        if class_qn := self._indexed_class_qn(type_name, module_qn):
            return class_qn
        if cs.SEPARATOR_DOT not in type_name:
            import_map = self.import_processor.import_mapping.get(module_qn, {})
            type_name = import_map.get(type_name, "")
        return type_name if type_name and _is_class_path(type_name) else None

    def _indexed_class_qn(
        self, type_name: str, module_qn: str, scope: Node | None = None
    ) -> str | None:
        """The registry qn of the indexed class `type_name` names, following
        package re-exports, else None. `scope` is where the name is read."""
        import_mapping = self.import_processor.import_mapping
        if cs.SEPARATOR_DOT in type_name:
            qn = follow_reexports(
                type_name,
                import_mapping,
                self.function_registry,
                self.import_processor.python_module_all,
            )
            if self.function_registry.get(qn) == NodeType.CLASS:
                return qn
            return resolve_dotted_class(
                type_name,
                module_qn,
                self.import_processor,
                self.function_registry,
                self.own_class_rebinds_import(
                    module_qn, type_name.partition(cs.SEPARATOR_DOT)[0], scope
                ),
            )
        qn = resolve_class_name(
            type_name, module_qn, self.import_processor, self.function_registry
        )
        if not qn:
            return None
        qn = follow_reexports(
            qn,
            import_mapping,
            self.function_registry,
            self.import_processor.python_module_all,
        )
        return qn if self.function_registry.get(qn) == NodeType.CLASS else None
