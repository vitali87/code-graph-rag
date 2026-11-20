"""Edges for PHP `new self`, relative scopes and callable values (issue #3117).

`new self` / `static` / `parent` instantiate that class and call `__construct`.
`self::m()` is the enclosing method, `static::m()` also reaches a subclass that
declares `m`, and `parent::m()` is the single extends parent. Callable arrays
and `Class::method` strings are REFERENCES: nothing runs at the site, but
dead-code walks that edge. A closure passed straight into a call is issue
#2925 and is left alone.
"""

from typing import Protocol

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import NodeType
from ..call_resolver import CallResolver, _php_fold
from . import callables as php_callables
from . import class_names as php_class_names

_CLASS_LABELS = frozenset(
    {NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM, NodeType.TYPE}
)


class _PhpEmitter(Protocol):
    _resolver: CallResolver
    # The processor annotates this as str. The stored values are
    # EdgeResolution members, which are strings.
    _resolution: str
    _site_node: Node | None

    def _emit_rel(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
    ) -> None: ...


class _CallView(Protocol):
    caller_spec: tuple[str, str, str]
    module_qn: str
    class_context: str | None
    language: cs.SupportedLanguage


def ingest_php_call(host: _PhpEmitter, ctx: _CallView, call_node: Node) -> bool:
    """Handle one PHP `new` or `self::` / `static::` / `parent::` call.

    True means the generic name path must not also run: `self` is not a
    class name, and a relative call must not keep the bare-method edge.
    """
    if ctx.language != cs.SupportedLanguage.PHP:
        return False
    if call_node.type == cs.TS_PHP_OBJECT_CREATION_EXPRESSION:
        return _ingest_new(host, ctx, call_node)
    scoped = php_callables.relative_scoped_call(call_node)
    if scoped is None:
        return False
    host._site_node = call_node
    scope, method = scoped
    _emit_methods(host, ctx.caller_spec, _scope_methods(host, ctx, scope, method))
    return True


def ingest_php_callable_values(
    host: _PhpEmitter,
    caller_node: Node,
    caller_spec: tuple[str, str, str],
    module_qn: str,
    class_context: str | None,
) -> None:
    stack = list(caller_node.children)
    while stack:
        node = stack.pop()
        if php_callables.value_walk_stop(node.type):
            continue
        if php_callables.in_value_position(node) and _emit_value(
            host, caller_spec, module_qn, class_context, node
        ):
            # The method-name literal is not a second callable.
            if node.type == cs.TS_PHP_ARRAY_CREATION_EXPRESSION:
                continue
        stack.extend(node.children)


def _ingest_new(host: _PhpEmitter, ctx: _CallView, call_node: Node) -> bool:
    if php_callables.is_dynamic_or_anonymous_new(call_node):
        return True
    relative = php_callables.relative_new(call_node)
    if relative is None:
        # A written `new Name` belongs to the class-path resolver (issue
        # #2466). It already ignores `use function` and `use const`, and it
        # finds a constructor the trie would skip.
        return False
    host._site_node = call_node
    _emit_constructions(
        host, ctx.caller_spec, _relative_classes(host, ctx.class_context, relative)
    )
    return True


def _emit_value(
    host: _PhpEmitter,
    caller_spec: tuple[str, str, str],
    module_qn: str,
    class_context: str | None,
    node: Node,
) -> bool:
    array = php_callables.callable_array(node)
    if array is not None:
        receiver, method = array
        host._site_node = node
        _emit_methods(
            host,
            caller_spec,
            _receiver_methods(host, module_qn, class_context, receiver, method),
        )
        return True
    parsed = php_callables.string_callable(node)
    if parsed is None:
        return False
    dotted, method = parsed
    host._site_node = node
    methods = [
        qn
        for class_qn in _classes_for_fqcn(host._resolver, dotted)
        if (qn := _method_qn(host._resolver, class_qn, method)) is not None
    ]
    _emit_methods(host, caller_spec, methods)
    return True


def _receiver_methods(
    host: _PhpEmitter,
    module_qn: str,
    class_context: str | None,
    receiver: Node,
    method: str,
) -> list[str]:
    scope = php_callables.class_const_scope(receiver)
    if scope is not None:
        if scope.type == cs.TS_PHP_RELATIVE_SCOPE:
            relative = php_callables.relative_scope(safe_text(scope))
            if relative is None:
                return []
            return _scope_methods_of(host, class_context, relative, method)
        return _methods_on_classes(host, _classes_for_type(host, scope), method)
    if receiver.type == cs.TS_PHP_VARIABLE_NAME:
        if safe_text(receiver) == cs.PHP_THIS:
            return _methods_on_classes(host, _one(class_context), method)
        type_node = php_callables.parameter_class_type(receiver)
        if type_node is None:
            return []
        return _methods_on_classes(host, _classes_for_type(host, type_node), method)
    # A class named by a string is absolute, as in `'App\\Handler::m'`
    # (PR #3652 review).
    dotted = php_callables.string_class(receiver)
    if dotted is not None:
        return _methods_on_classes(
            host, _classes_for_fqcn(host._resolver, dotted), method
        )
    return []


def _scope_methods(
    host: _PhpEmitter, ctx: _CallView, scope: cs.PhpRelativeScope, method: str
) -> list[str]:
    return _scope_methods_of(host, ctx.class_context, scope, method)


def _scope_methods_of(
    host: _PhpEmitter,
    class_context: str | None,
    scope: cs.PhpRelativeScope,
    method: str,
) -> list[str]:
    if not class_context:
        return []
    if scope == cs.PhpRelativeScope.PARENT:
        parent = _parent_class(host._resolver, class_context)
        found = _method_qn(host._resolver, parent, method) if parent else None
        return [found] if found else []
    found = _method_qn(host._resolver, class_context, method)
    methods = [found] if found else []
    if scope != cs.PhpRelativeScope.STATIC:
        return methods
    for subclass in host._resolver.php_concrete_subclasses(class_context):
        declared = _declared_method(host._resolver, subclass, method)
        if declared is not None:
            methods.append(declared)
    return methods


def _relative_classes(
    host: _PhpEmitter, class_context: str | None, scope: cs.PhpRelativeScope
) -> list[str]:
    if not class_context:
        return []
    if scope == cs.PhpRelativeScope.PARENT:
        parent = _parent_class(host._resolver, class_context)
        return [parent] if parent else []
    classes = [class_context]
    if scope != cs.PhpRelativeScope.STATIC:
        return classes
    # `new static` builds whichever subclass the method runs on, whether or
    # not that subclass declares a constructor: one that does not runs the
    # constructor it inherits, which `php_constructor` finds.
    classes.extend(sorted(host._resolver.php_concrete_subclasses(class_context)))
    return classes


def _methods_on_classes(
    host: _PhpEmitter, classes: list[str], method: str
) -> list[str]:
    return [
        qn
        for class_qn in classes
        if (qn := _method_qn(host._resolver, class_qn, method)) is not None
    ]


def _classes_for_type(host: _PhpEmitter, node: Node) -> list[str]:
    dotted = _compile_class_name(node)
    if dotted is None:
        return []
    return _classes_for_fqcn(host._resolver, dotted)


def _compile_class_name(node: Node) -> str | None:
    # Compiler resolution, not the string-callable rule: the same reading a
    # written `new Name` gets, with the `use` aliases of the site's own
    # namespace block (PR #3652 review).
    path = php_class_names.written_class_path(node)
    return cs.SEPARATOR_DOT.join(path) if path else None


def _classes_for_fqcn(resolver: CallResolver, dotted: str) -> list[str]:
    found = resolver.import_processor.php_class_index.get(_php_fold(dotted), set())
    return [
        class_qn
        for class_qn in found
        if resolver.function_registry.get(class_qn) in _CLASS_LABELS
    ]


def _parent_class(resolver: CallResolver, class_qn: str) -> str | None:
    # PHP `extends` is one class. Interfaces are IMPLEMENTS, not this map.
    for parent in resolver.class_inheritance.get(class_qn, ()):
        if (found := php_class_for_base(resolver, parent)) is not None:
            return found
    return None


def php_class_for_base(resolver: CallResolver, base: str) -> str | None:
    """The class one `class_inheritance` entry of a PHP class names.

    A same-file parent is stored as its class qn; a cross-file one can be
    the dotted FQCN the import map recorded, which the index understands.
    """
    if resolver.function_registry.get(base) == NodeType.CLASS:
        return base
    found = _classes_for_fqcn(resolver, base)
    return found[0] if len(found) == 1 else None


def _method_qn(resolver: CallResolver, class_qn: str, method: str) -> str | None:
    declared = _declared_method(resolver, class_qn, method)
    if declared is not None:
        return declared
    seen = {class_qn}
    queue = list(resolver.class_inheritance.get(class_qn, ()))
    while queue:
        parent = queue.pop(0)
        resolved = parent
        if resolver.function_registry.get(parent) != NodeType.CLASS:
            found = _classes_for_fqcn(resolver, parent)
            resolved = found[0] if len(found) == 1 else parent
        if resolved in seen:
            continue
        seen.add(resolved)
        declared = _declared_method(resolver, resolved, method)
        if declared is not None:
            return declared
        queue.extend(resolver.class_inheritance.get(resolved, ()))
    return None


def _declared_method(resolver: CallResolver, class_qn: str, method: str) -> str | None:
    # A prefix walk misses a folded name and a method that starts with `__`.
    return resolver._php_declared_method(class_qn, method, _php_fold(method))


def _emit_constructions(
    host: _PhpEmitter, caller_spec: tuple[str, str, str], classes: list[str]
) -> None:
    if not classes:
        return
    host._resolution = (
        cs.EdgeResolution.OVERLOAD if len(classes) > 1 else cs.EdgeResolution.EXACT
    )
    called: set[str] = set()
    for class_qn in classes:
        host._emit_rel(
            caller_spec,
            cs.RelationshipType.INSTANTIATES,
            (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_qn),
        )
        found = host._resolver.php_constructor(class_qn)
        # Subclasses that inherit one constructor share its single edge.
        if found is None or found[1] in called:
            continue
        _, ctor = found
        called.add(ctor)
        host._emit_rel(
            caller_spec,
            cs.RelationshipType.CALLS,
            (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, ctor),
        )


def _emit_methods(
    host: _PhpEmitter, caller_spec: tuple[str, str, str], methods: list[str]
) -> None:
    unique = list(dict.fromkeys(methods))
    if not unique:
        return
    host._resolution = (
        cs.EdgeResolution.OVERLOAD if len(unique) > 1 else cs.EdgeResolution.EXACT
    )
    for method_qn in unique:
        node_type = host._resolver.function_registry.get(method_qn)
        label = (
            cs.NodeLabel.METHOD
            if node_type == NodeType.METHOD
            else cs.NodeLabel.FUNCTION
        )
        rel = (
            cs.RelationshipType.CALLS
            if host._site_node is not None
            and host._site_node.type
            in (
                cs.TS_PHP_SCOPED_CALL_EXPRESSION,
                cs.TS_PHP_OBJECT_CREATION_EXPRESSION,
            )
            else cs.RelationshipType.REFERENCES
        )
        host._emit_rel(
            caller_spec,
            rel,
            (label, cs.KEY_QUALIFIED_NAME, method_qn),
        )


def _one(class_qn: str | None) -> list[str]:
    return [class_qn] if class_qn else []


def safe_text(node: Node) -> str:
    from ..utils import safe_decode_text

    return safe_decode_text(node) or ""
