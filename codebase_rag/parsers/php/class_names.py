"""The class a PHP `new` constructs, named the way PHP names it (issue #2466).

PHP resolves a class name at compile time from three things the call site
alone does not spell: the enclosing `namespace`, the `use` imports in force,
and a leading `\\` that opts out of both. These helpers apply those rules to
the syntax tree and hand back the fully qualified namespace path; the
resolver maps that path onto the module that declares it.
"""

from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import LANGUAGE_FQN_SPECS
from ...utils.fqn_resolver import scoped_name_parts
from ..utils import safe_decode_text

ClassPath = tuple[str, ...]
# A scope's typed variables, keyed by the scope's byte span.
ScopeCache = dict[tuple[int, int], dict[str, ClassPath]]

# PHP folds A-Z only when comparing namespace, class and function names, so
# `use function app\text\FORMAT` binds to `App\Text\format`.
#
# NOT `str.casefold()`, which folds the full Unicode range: PHP treats
# identifiers differing outside ASCII as DISTINCT, so Unicode folding would
# match names the language does not, trading a missed binding for a wrong one.
# `str.lower()` has the same problem (Turkish dotless i, Kelvin sign).
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

_WRITTEN_CLASS_TYPES = frozenset(
    {cs.TS_PHP_NAME, cs.TS_PHP_QUALIFIED_NAME, cs.TS_PHP_RELATIVE_NAME}
)
_USE_TARGET_TYPES = frozenset({cs.TS_PHP_NAME, cs.TS_PHP_QUALIFIED_NAME})
# A `use function` / `use const` import lives in another symbol table and
# never names a class.
_NON_CLASS_USE_KEYWORDS = frozenset({cs.TS_PHP_FUNCTION, cs.TS_PHP_CONST})
# Bodies that open a variable scope of their own (or none, for a class).
_VARIABLE_SCOPE_TYPES = frozenset(cs.FQN_PHP_FUNCTION_TYPES)
_SCOPE_BARRIER_TYPES = _VARIABLE_SCOPE_TYPES | frozenset(cs.SPEC_PHP_CLASS_TYPES)


def ascii_fold(name: str) -> str:
    """ASCII-lowercase `name` for PHP-style case-insensitive comparison."""
    return name.translate(_ASCII_FOLD)


def new_expression_class_path(creation: Node) -> ClassPath | None:
    """The fully qualified path of the class `new` builds, `("App", "Box")`.

    None when the class is not written as a name: `new $cls`, `new class`,
    and `new self` / `static` / `parent`, which name a class by position.
    """
    written = next(
        (c for c in creation.named_children if c.type in _WRITTEN_CLASS_TYPES), None
    )
    if written is None or not (text := safe_decode_text(written)):
        return None
    segments = tuple(text.split(cs.PHP_NAMESPACE_SEPARATOR))
    if written.type == cs.TS_PHP_QUALIFIED_NAME and not segments[0]:
        return segments[1:]
    namespace, uses = _namespace_scope(creation)
    if written.type == cs.TS_PHP_RELATIVE_NAME:
        return (*namespace, *segments[1:])
    if written.type == cs.TS_PHP_NAME and (
        ascii_fold(text) in cs.PHP_RESERVED_CLASS_NAMES
    ):
        return None
    # Only the FIRST segment is looked up among the imports; a qualified
    # `Models\Box` extends whatever `Models` is bound to.
    if (imported := _class_imports(uses).get(ascii_fold(segments[0]))) is not None:
        return (*imported, *segments[1:])
    return (*namespace, *segments)


def declared_class_namespaces(root: Node, module_qn: str) -> dict[str, ClassPath]:
    """Each named class the file declares, keyed by the qn the definition pass
    registers it under, mapped to the namespace PHP declares it in.

    A file of braced blocks records no module-level namespace, and a class
    in one is registered under its block's name (`mod.Vendor.Box`), so only
    the declaration itself says which namespace a class belongs to.
    """
    spec = LANGUAGE_FQN_SPECS[cs.SupportedLanguage.PHP]
    declared: dict[str, ClassPath] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_CLASS_DECLARATION and (
            parts := scoped_name_parts(node, spec, module_qn, None)
        ):
            qn = cs.SEPARATOR_DOT.join([module_qn, *parts])
            declared[qn] = _namespace_scope(node)[0]
        stack.extend(node.named_children)
    return declared


def receiver_class_path(call: Node, scope_cache: ScopeCache) -> ClassPath | None:
    """The class a member call's receiver was built as, when the site shows it.

    `(new Box())->bump()` names it directly. `$b->bump()` does when every
    binding of `$b` in the enclosing callable is `$b = new Box(...)`; a
    parameter, a reassignment to anything else, or any other rebinding
    (`foreach`, `list()`, by-reference) leaves the receiver untyped.
    `scope_cache` holds each scope's answer, so one walk serves every call
    in it.
    """
    receiver = call.child_by_field_name(cs.FIELD_OBJECT)
    while receiver is not None and receiver.type == cs.TS_PHP_PARENTHESIZED_EXPRESSION:
        receiver = receiver.named_children[0] if receiver.named_children else None
    if receiver is None:
        return None
    if receiver.type == cs.TS_PHP_OBJECT_CREATION_EXPRESSION:
        return new_expression_class_path(receiver)
    if receiver.type != cs.TS_PHP_VARIABLE_NAME or not (
        name := _variable_identifier(receiver)
    ):
        return None
    scope = _variable_scope(receiver)
    key = (scope.start_byte, scope.end_byte)
    if (classes := scope_cache.get(key)) is None:
        classes = scope_cache[key] = _scope_variable_classes(scope)
    return classes.get(name)


def _namespace_scope(node: Node) -> tuple[ClassPath, list[Node]]:
    # The namespace `node` is compiled in and the `use` declarations that
    # precede it there. A braced `namespace A { ... }` scopes its own body;
    # a statement `namespace A;` rules the file up to the next one, and its
    # imports with it.
    block = node.parent
    while block is not None and not (
        block.type == cs.TS_PHP_NAMESPACE_DEFINITION
        and block.child_by_field_name(cs.FIELD_BODY) is not None
    ):
        block = block.parent
    namespace: ClassPath = ()
    if block is not None:
        namespace = _declared_namespace(block)
        body = block.child_by_field_name(cs.FIELD_BODY)
        statements = body.named_children if body is not None else []
    else:
        root = node
        while root.parent is not None:
            root = root.parent
        statements = root.named_children
    uses: list[Node] = []
    for statement in statements:
        if statement.start_byte >= node.start_byte:
            break
        if statement.type == cs.TS_PHP_NAMESPACE_DEFINITION:
            namespace, uses = _declared_namespace(statement), []
        elif statement.type == cs.TS_PHP_NAMESPACE_USE_DECLARATION:
            uses.append(statement)
    return namespace, uses


def _declared_namespace(definition: Node) -> ClassPath:
    name = safe_decode_text(definition.child_by_field_name(cs.FIELD_NAME))
    return tuple(name.split(cs.PHP_NAMESPACE_SEPARATOR)) if name else ()


def _class_imports(uses: list[Node]) -> dict[str, ClassPath]:
    # Folded local name -> imported path, for class imports only.
    imports: dict[str, ClassPath] = {}
    for declaration in uses:
        if _imports_non_class(declaration):
            continue
        prefix: ClassPath = ()
        clauses_parent = declaration
        if (group := declaration.child_by_field_name(cs.FIELD_BODY)) is not None:
            # `use App\{Box, Models\Crate as C}`: the clauses extend the prefix.
            prefix = _path_of(
                next(
                    (
                        c
                        for c in declaration.named_children
                        if c.type == cs.TS_PHP_NAMESPACE_NAME
                    ),
                    None,
                )
            )
            clauses_parent = group
        for clause in clauses_parent.named_children:
            if clause.type != cs.TS_PHP_NAMESPACE_USE_CLAUSE or _imports_non_class(
                clause
            ):
                continue
            target = next(
                (c for c in clause.named_children if c.type in _USE_TARGET_TYPES), None
            )
            path = (*prefix, *_path_of(target))
            if not path:
                continue
            alias = safe_decode_text(clause.child_by_field_name(cs.FIELD_ALIAS))
            imports[ascii_fold(alias or path[-1])] = path
    return imports


def _imports_non_class(node: Node) -> bool:
    return any(child.type in _NON_CLASS_USE_KEYWORDS for child in node.children)


def _path_of(node: Node | None) -> ClassPath:
    text = safe_decode_text(node)
    if not text:
        return ()
    # A `use` path is always resolved from the global namespace, so a
    # leading `\` changes nothing.
    return tuple(s for s in text.split(cs.PHP_NAMESPACE_SEPARATOR) if s)


def _variable_scope(node: Node) -> Node:
    # The callable whose variables `node` names, or the file for top-level code.
    scope = node
    while scope.parent is not None and scope.type not in _VARIABLE_SCOPE_TYPES:
        scope = scope.parent
    return scope


def _scope_variable_classes(scope: Node) -> dict[str, ClassPath]:
    # Each variable every binding of which in `scope` is `$v = new C(...)` of
    # one class C, mapped to C's path.
    body = (
        scope.child_by_field_name(cs.FIELD_BODY)
        if scope.type in _VARIABLE_SCOPE_TYPES
        else scope
    )
    if body is None:
        return {}
    untyped: set[str] = set()
    if body is not scope:
        # A parameter or a closure's `use ($v)` binds the variable from
        # outside, with a type this scope cannot see.
        for header in scope.named_children:
            if header.start_byte < body.start_byte:
                untyped.update(
                    name
                    for variable in _scope_variables(header)
                    if (name := _variable_identifier(variable))
                )
    typed: dict[str, set[ClassPath]] = {}
    for variable in _scope_variables(body):
        if (name := _variable_identifier(variable)) is None:
            continue
        bound = _bound_class_path(variable)
        if bound is None:
            continue
        if bound:
            typed.setdefault(name, set()).add(bound)
        else:
            untyped.add(name)
    return {
        name: next(iter(paths))
        for name, paths in typed.items()
        if len(paths) == 1 and name not in untyped
    }


def _variable_identifier(variable: Node) -> str | None:
    name_node = next(
        (c for c in variable.named_children if c.type == cs.TS_PHP_NAME), None
    )
    return safe_decode_text(name_node)


def _scope_variables(node: Node) -> list[Node]:
    # Every `$var` under `node` in the same variable scope. A nested callable
    # has its own variables, except what its `use (&$var)` clause binds back.
    found: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_PHP_VARIABLE_NAME:
            found.append(current)
            continue
        if current is not node and current.type in _SCOPE_BARRIER_TYPES:
            stack.extend(
                c
                for c in current.named_children
                if c.type == cs.TS_PHP_ANONYMOUS_FUNCTION_USE_CLAUSE
            )
            continue
        stack.extend(current.named_children)
    return found


def _bound_class_path(occurrence: Node) -> ClassPath | None:
    # None: this occurrence reads the variable. An empty path: it rebinds the
    # variable to something other than one named class. Otherwise the class
    # path `$var = new C(...)` binds.
    parent = occurrence.parent
    if parent is None:
        return None
    if parent.type in cs.PHP_VARIABLE_REBINDING_PARENTS:
        return ()
    if parent.type not in cs.PHP_ASSIGNMENT_TYPES:
        return None
    left = parent.child_by_field_name(cs.FIELD_LEFT)
    if left is None or left.start_byte != occurrence.start_byte:
        return None
    right = parent.child_by_field_name(cs.FIELD_RIGHT)
    if (
        parent.type != cs.TS_PHP_ASSIGNMENT_EXPRESSION
        or right is None
        or right.type != cs.TS_PHP_OBJECT_CREATION_EXPRESSION
    ):
        return ()
    return new_expression_class_path(right) or ()
