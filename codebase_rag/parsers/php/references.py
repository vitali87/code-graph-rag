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
from ..php import namespaces as php_namespaces
from . import callables as php_callables

_METHOD_TYPES = frozenset({NodeType.METHOD, NodeType.FUNCTION})
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


def php_constructor_qn(resolver: CallResolver, class_qn: str) -> str | None:
    return _method_qn(resolver, class_qn, cs.PHP_CONSTRUCTOR)


def _ingest_new(host: _PhpEmitter, ctx: _CallView, call_node: Node) -> bool:
    if php_callables.is_dynamic_or_anonymous_new(call_node):
        return True
    relative = php_callables.relative_new(call_node)
    class_node = php_callables.creation_class_node(call_node)
    if relative is None and class_node is None:
        return False
    host._site_node = call_node
    if relative is not None:
        classes = _relative_classes(host, ctx.class_context, relative)
    else:
        assert class_node is not None
        classes = _classes_for_type(host, ctx.module_qn, class_node)
    _emit_constructions(host, ctx.caller_spec, classes)
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
        return _methods_on_classes(
            host, _classes_for_type(host, module_qn, scope), method
        )
    if receiver.type == cs.TS_PHP_VARIABLE_NAME:
        if safe_text(receiver) == cs.PHP_THIS:
            return _methods_on_classes(host, _one(class_context), method)
        type_node = php_callables.parameter_class_type(receiver)
        if type_node is None:
            return []
        return _methods_on_classes(
            host, _classes_for_type(host, module_qn, type_node), method
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
    for subclass in host._resolver._concrete_subclasses(class_context):
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
    for subclass in host._resolver._concrete_subclasses(class_context):
        if _declared_method(host._resolver, subclass, cs.PHP_CONSTRUCTOR):
            classes.append(subclass)
    return classes


def _methods_on_classes(
    host: _PhpEmitter, classes: list[str], method: str
) -> list[str]:
    return [
        qn
        for class_qn in classes
        if (qn := _method_qn(host._resolver, class_qn, method)) is not None
    ]


def _classes_for_type(host: _PhpEmitter, module_qn: str, node: Node) -> list[str]:
    dotted = _compile_class_name(host, module_qn, node)
    if dotted is None:
        return []
    return _classes_for_fqcn(host._resolver, dotted)


def _compile_class_name(host: _PhpEmitter, module_qn: str, node: Node) -> str | None:
    # Compiler resolution, not the string-callable rule. Unqualified names
    # take a `use` alias or the enclosing namespace. A qualified name applies
    # the import to its first segment, then prepends the namespace. A leading
    # slash is already absolute.
    text = safe_text(node)
    if not text:
        return None
    absolute = text.startswith(cs.PHP_NAMESPACE_SEPARATOR)
    parts = [part for part in text.lstrip("\\").split("\\") if part]
    if not parts:
        return None
    if absolute:
        return cs.SEPARATOR_DOT.join(parts)
    mapped = _class_import(host, module_qn, parts[0])
    if len(parts) == 1:
        if mapped is not None:
            return mapped
        namespace = php_namespaces.enclosing_namespace(node)
        return f"{namespace}.{parts[0]}" if namespace else parts[0]
    if mapped is not None:
        tail = cs.SEPARATOR_DOT.join(parts[1:])
        return f"{mapped}.{tail}" if tail else mapped
    namespace = php_namespaces.enclosing_namespace(node)
    body = cs.SEPARATOR_DOT.join(parts)
    return f"{namespace}.{body}" if namespace else body


def _class_import(host: _PhpEmitter, module_qn: str, name: str) -> str | None:
    # Class aliases live apart from `use function` and `use const`. Those
    # may reuse the same local name, and the shared import map keeps only
    # the later path, which is not what `new` means.
    class_map = host._resolver.import_processor.php_class_imports.get(module_qn, {})
    head = host._resolver._php_import_key(name, class_map)
    if head is None:
        return None
    return class_map[head]


def _classes_for_fqcn(resolver: CallResolver, dotted: str) -> list[str]:
    found = resolver.import_processor.php_class_index.get(_php_fold(dotted), set())
    return [
        class_qn
        for class_qn in found
        if resolver.function_registry.get(class_qn) in _CLASS_LABELS
    ]


def _parent_class(resolver: CallResolver, class_qn: str) -> str | None:
    # PHP `extends` is one class. Interfaces are IMPLEMENTS, not this map.
    # A same-file parent is stored as its class qn; a cross-file one can be
    # the dotted FQCN the import map recorded, which the index understands.
    for parent in resolver.class_inheritance.get(class_qn, ()):
        if resolver.function_registry.get(parent) == NodeType.CLASS:
            return parent
        found = _classes_for_fqcn(resolver, parent)
        if len(found) == 1:
            return found[0]
    return None


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
    registry = resolver.function_registry
    exact = f"{class_qn}{cs.SEPARATOR_DOT}{method}"
    if registry.get(exact) in _METHOD_TYPES:
        return exact
    folded = _php_fold(method)
    prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
    for qn, node_type in registry.find_with_prefix(prefix):
        if node_type not in _METHOD_TYPES:
            continue
        rest = qn[len(prefix) :]
        if cs.SEPARATOR_DOT not in rest and _php_fold(rest) == folded:
            return qn
    return None


def _emit_constructions(
    host: _PhpEmitter, caller_spec: tuple[str, str, str], classes: list[str]
) -> None:
    if not classes:
        return
    host._resolution = (
        cs.EdgeResolution.OVERLOAD if len(classes) > 1 else cs.EdgeResolution.EXACT
    )
    for class_qn in classes:
        host._emit_rel(
            caller_spec,
            cs.RelationshipType.INSTANTIATES,
            (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_qn),
        )
        ctor = php_constructor_qn(host._resolver, class_qn)
        if ctor is None:
            continue
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
