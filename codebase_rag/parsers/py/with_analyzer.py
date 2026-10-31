from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as lg
from ...types_defs import FunctionRegistryTrieProtocol, NodeType
from ..import_processor import ImportProcessor
from ..utils import follow_reexports, safe_decode_text
from .utils import resolve_class_name, resolve_dotted_class, split_top_level


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

        def _find_function_ast_node(self, fn_qn: str) -> Node | None: ...

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


def _own_yield_values(definition: Node) -> Iterator[Node | None]:
    """The values `definition` itself yields, None for a bare `yield`.

    Nested defs and classes are not descended into: their yields are
    theirs, and a lambda cannot yield.
    """
    body = definition.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return
    stack = list(body.children)
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PY_YIELD:
            children = node.named_children
            yield children[0] if children else None
            continue
        if node.type in (
            cs.TS_PY_FUNCTION_DEFINITION,
            cs.TS_PY_CLASS_DEFINITION,
            cs.TS_PY_LAMBDA,
        ):
            continue
        stack.extend(node.children)


def _enclosing_class_qn(node: Node, module_qn: str) -> str | None:
    """The `module.Class` of the class whose method body holds `node`,
    or None when no class encloses it."""
    current = node.parent
    while current is not None and current.type != cs.TS_PY_CLASS_DEFINITION:
        if current.type == cs.TS_PY_MODULE:
            return None
        current = current.parent
    if current is None:
        return None
    name = current.child_by_field_name(cs.FIELD_NAME)
    if (class_name := safe_decode_text(name)) is None:
        return None
    return f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"


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

    The exception is a `@contextmanager` / `@asynccontextmanager` factory
    (issue #3340): the entered value is the generator's yield type, from
    the `Iterator[T]` / `Generator[T, ...]` return annotation, else from
    the `yield` expressions. A factory without that decorator keeps the
    refusal above: its return type is the manager's, not the entered
    value's.
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
                # A `@contextmanager` factory with no readable return type
                # still hands its target the yielded value: resolve the
                # factory itself rather than wait on a manager type that
                # will never arrive (#3340).
                if entered := self._contextmanager_entered_type(target, module_qn):
                    local_var_types[target.name] = entered
                    logger.debug(lg.PY_TYPE_WITH, var=target.name, type=entered)
                    continue
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
            # A `@contextmanager` / `@asynccontextmanager` factory: the
            # entered value is the generator's yield type, read from the
            # factory itself. Any other factory keeps the refusal below:
            # its return type is the manager's, not the entered value's.
            if entered := self._contextmanager_entered_type(target, module_qn):
                return entered
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

    def _contextmanager_entered_type(
        self, target: WithTarget, module_qn: str
    ) -> str | None:
        """The yield type of the `@contextmanager` / `@asynccontextmanager`
        factory `target` calls, or None when the manager is not such a
        factory (issue #3340)."""
        factory = self._contextmanager_factory(target.manager, module_qn)
        if factory is None:
            return None
        definition, factory_module_qn = factory
        if not self._is_contextmanager_factory(definition, factory_module_qn):
            return None
        return self._contextmanager_yield_type(definition, factory_module_qn)

    def _contextmanager_factory(
        self, manager: Node, module_qn: str
    ) -> tuple[Node, str] | None:
        """The definition of the function or bare-`self`/`cls` method a
        `with` manager calls, with the module it is defined in.

        A call through any other attribute receiver (`self.proxy.release()`)
        is not followed: the receiver's type is not known here, and
        guessing it would type the target from the wrong method. A factory
        nested inside another def is not followed either: its definition
        carries no qualified name the by-name lookup can find.
        """
        if manager.type != cs.TS_PY_CALL:
            return None
        callee = manager.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if callee is None:
            return None
        if callee.type == cs.TS_PY_IDENTIFIER:
            name = safe_decode_text(callee)
            if not name:
                return None
            candidates = [f"{module_qn}{cs.SEPARATOR_DOT}{name}"]
            if imported := self.import_processor.import_mapping.get(module_qn, {}).get(
                name
            ):
                candidates.append(
                    follow_reexports(
                        imported,
                        self.import_processor.import_mapping,
                        self.function_registry,
                    )
                )
            for qn in candidates:
                if self.function_registry.get(qn) != NodeType.FUNCTION:
                    continue
                if (definition := self._find_function_ast_node(qn)) is not None:
                    return definition, qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
            return None
        if callee.type != cs.TS_PY_ATTRIBUTE:
            return None
        parts = (safe_decode_text(callee) or "").split(cs.SEPARATOR_DOT)
        if len(parts) == 2 and parts[0] in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS):
            enclosing = _enclosing_class_qn(manager, module_qn)
            if enclosing is None:
                return None
            # An inherited `@contextmanager` method is found through the
            # enclosing class's MRO, the way `_entered_type` finds an
            # inherited `__enter__`.
            for owner in self._get_mro(enclosing):
                method_qn = f"{owner}{cs.SEPARATOR_DOT}{parts[1]}"
                if (definition := self._find_method_ast_node(method_qn)) is not None:
                    return definition, owner.rsplit(cs.SEPARATOR_DOT, 1)[0]
            return None
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        if parts and parts[0] in import_map:
            qn = (
                f"{import_map[parts[0]]}"
                f"{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(parts[1:])}"
            )
            if (
                self.function_registry.get(qn) == NodeType.FUNCTION
                and (definition := self._find_function_ast_node(qn)) is not None
            ):
                return definition, qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return None

    def _is_contextmanager_factory(
        self, definition: Node, factory_module_qn: str
    ) -> bool:
        """Whether `definition` carries a contextlib contextmanager
        decorator, spelled bare, aliased or qualified.

        A bare name counts only when the defining module's import map
        resolves it to contextlib: an unrelated decorator that happens to
        be named `contextmanager` is not one. A dotted spelling counts
        only when it resolves to `contextlib.contextmanager` /
        `contextlib.asynccontextmanager` the same way.
        """
        statement = definition.parent
        if statement is None or statement.type != cs.TS_PY_DECORATED_DEFINITION:
            return False
        import_map = self.import_processor.import_mapping.get(factory_module_qn, {})
        for child in statement.named_children:
            if child.type != cs.TS_PY_DECORATOR or not child.named_children:
                continue
            spelling = "".join(
                (safe_decode_text(child.named_children[0]) or "").split()
            ).split(cs.CHAR_PAREN_OPEN, 1)[0]
            if not spelling:
                continue
            parts = spelling.split(cs.SEPARATOR_DOT)
            if parts[0] in import_map:
                resolved = import_map[parts[0]]
                if len(parts) > 1:
                    resolved = (
                        f"{resolved}"
                        f"{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(parts[1:])}"
                    )
            elif len(parts) == 1:
                continue
            else:
                resolved = spelling
            if resolved in {
                f"{cs.PY_CONTEXTLIB_MODULE}{cs.SEPARATOR_DOT}{name}"
                for name in cs.CONTEXTMANAGER_DECORATORS
            }:
                return True
        return False

    def _contextmanager_yield_type(
        self, definition: Node, factory_module_qn: str
    ) -> str | None:
        """The type a contextmanager factory yields: the element type of
        its `Iterator` / `Generator` return annotation, else the type of
        its `yield` values when they agree."""
        annotation = definition.child_by_field_name(cs.FIELD_RETURN_TYPE)
        if annotation is not None:
            text = (safe_decode_text(annotation) or "").strip().strip("\"'")
            if yielded := self._yield_annotation_type(text, factory_module_qn):
                return yielded
        return self._yield_expression_type(definition, factory_module_qn)

    def _yield_annotation_type(self, text: str, module_qn: str) -> str | None:
        """The first type argument of a generator annotation, or None for
        any other annotation: only `Iterator[T]`, `Generator[T, ...]`,
        `AsyncIterator[T]` and `AsyncGenerator[T, ...]` name the yielded
        type. The base name is resolved through the factory module's
        import map, so an alias (`Iterator as It`) counts only when it
        resolves to `typing` / `collections.abc`; a bare name with no
        import entry keeps the spelling check, so star-imports work."""
        bracket = text.find("[")
        if bracket < 0 or not text.endswith("]"):
            return None
        if not self._is_generator_annotation(text[:bracket].strip(), module_qn):
            return None
        parts = split_top_level(text[bracket + 1 : -1])
        first = parts[0].strip().strip("\"'") if parts else None
        if not first:
            return None
        return self._resolved_type(first, module_qn)

    def _is_generator_annotation(self, base: str, module_qn: str) -> bool:
        """Whether `base` names a generator annotation, resolved through
        the factory module's import map when it has an entry for the
        first segment: a same-named import from elsewhere is not one."""
        parts = base.split(cs.SEPARATOR_DOT)
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        if parts[0] in import_map:
            resolved = import_map[parts[0]]
            if len(parts) > 1:
                resolved = (
                    f"{resolved}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(parts[1:])}"
                )
            return resolved in {
                f"{module}.{name}"
                for module in cs.PY_GENERATOR_ANNOTATION_MODULES
                for name in cs.PY_GENERATOR_ANNOTATIONS
            }
        if len(parts) == 1:
            return parts[0] in cs.PY_GENERATOR_ANNOTATIONS
        return base in {
            f"{module}.{name}"
            for module in cs.PY_GENERATOR_ANNOTATION_MODULES
            for name in cs.PY_GENERATOR_ANNOTATIONS
        }

    def _factory_local_types(
        self, definition: Node, factory_module_qn: str
    ) -> dict[str, str]:
        """The types the factory's own simple bindings give its locals:
        `t = Thing()` before `yield t` in the standard try/finally shape.
        Only the factory's own body is read, and only plain identifier
        targets: a name the factory never binds, or binds from something
        unreadable, stays unknown rather than guessed. Parameters are not
        read: a factory yielding a parameter needs its annotation, which
        the annotation path above already covers."""
        local_types: dict[str, str] = {}
        body = definition.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return local_types
        stack = list(body.children)
        while stack:
            node = stack.pop()
            pair: tuple[Node | None, Node | None] | None = None
            if node.type == cs.TS_PY_ASSIGNMENT:
                pair = (
                    node.child_by_field_name(cs.TS_FIELD_LEFT),
                    node.child_by_field_name(cs.TS_FIELD_RIGHT),
                )
            elif node.type == cs.TS_PY_NAMED_EXPRESSION:
                pair = (
                    node.child_by_field_name(cs.FIELD_NAME),
                    node.child_by_field_name(cs.FIELD_VALUE),
                )
            if pair is not None:
                name_node, value_node = pair
                if (
                    name_node is not None
                    and name_node.type == cs.TS_PY_IDENTIFIER
                    and value_node is not None
                    and (name := safe_decode_text(name_node))
                    and (
                        inferred := self._infer_type_from_expression_simple(
                            value_node, factory_module_qn
                        )
                        or self._infer_type_from_expression_complex(
                            value_node, factory_module_qn, local_types
                        )
                    )
                ):
                    local_types[name] = inferred
                continue
            if node.type in (
                cs.TS_PY_FUNCTION_DEFINITION,
                cs.TS_PY_CLASS_DEFINITION,
                cs.TS_PY_LAMBDA,
            ):
                continue
            stack.extend(node.children)
        return local_types

    def _yield_expression_type(
        self, definition: Node, factory_module_qn: str
    ) -> str | None:
        """The one type every `yield` in the factory's own body yields, or
        None when they disagree or cannot be read: the same single-value
        rule `_enter_return_type` applies to `return` statements. A yield
        of a bare name reads the factory's own bindings for it, so the
        try/finally shape (`t = Thing(); yield t`) types too."""
        factory_types = self._factory_local_types(definition, factory_module_qn)
        found: set[str | None] = set()
        for value in _own_yield_values(definition):
            if value is None:
                found.add(None)
                continue
            if value.type == cs.TS_PY_IDENTIFIER:
                inferred = factory_types.get(safe_decode_text(value) or "")
            else:
                inferred = self._infer_type_from_expression_simple(
                    value, factory_module_qn
                ) or self._infer_type_from_expression_complex(
                    value, factory_module_qn, factory_types
                )
            found.add(
                self._resolved_type(inferred, factory_module_qn) if inferred else None
            )
        return found.pop() if len(found) == 1 else None

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
            qn = follow_reexports(type_name, import_mapping, self.function_registry)
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
        qn = follow_reexports(qn, import_mapping, self.function_registry)
        return qn if self.function_registry.get(qn) == NodeType.CLASS else None
