from collections.abc import Mapping
from typing import TYPE_CHECKING

from tree_sitter import Language, Node, QueryCursor

from ... import constants as cs
from ..utils import get_cached_query, safe_decode_text

if TYPE_CHECKING:
    from ...types_defs import LanguageQueries


def get_js_ts_language_obj(
    language: cs.SupportedLanguage,
    queries: Mapping[cs.SupportedLanguage, "LanguageQueries"],
) -> Language | None:
    if language not in cs.JS_TS_LANGUAGES:
        return None

    lang_queries = queries[language]
    return lang_queries.get(cs.QUERY_LANGUAGE)


# Callables that bind a `this` of their own; an arrow takes the one around it.
_OWN_THIS_CALLABLES = frozenset(
    {
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_GENERATOR_FUNCTION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
    }
)
# Class members whose `this` is the class when they are static.
_CLASS_THIS_MEMBERS = frozenset(
    {cs.TS_METHOD_DEFINITION, cs.TS_PUBLIC_FIELD_DEFINITION, cs.TS_JS_FIELD_DEFINITION}
)


def is_static_member(member: Node) -> bool:
    # The `static` keyword is an anonymous token among the member's own
    # children; a free function never has one.
    return any(child.type == cs.TS_STATIC for child in member.children)


def this_names_class_at(root: Node, point: int) -> bool | None:
    """What `this` written at byte `point` is: the class itself (``True``,
    inside a static method, field initialiser or static block), an instance
    (``False``), or neither (``None``: a plain function's or an object
    literal method's own `this`, or none at all). An arrow reads `this` from
    where it is written."""
    node = root.named_descendant_for_byte_range(point, point)
    while node is not None:
        if node.type == cs.TS_CLASS_STATIC_BLOCK:
            return True
        if node.type in _CLASS_THIS_MEMBERS:
            # A shorthand method, getter or setter of an object literal binds
            # `this` to that object; only a member of a class body is the
            # class's, so the walk never climbs past it to the class around.
            in_class = node.parent is not None and node.parent.type == cs.TS_CLASS_BODY
            return is_static_member(node) if in_class else None
        if node.type in _OWN_THIS_CALLABLES:
            return None
        node = node.parent
    return None


def _extract_class_qn(method_qn: str) -> str | None:
    qn_parts = method_qn.split(cs.SEPARATOR_DOT)
    return cs.SEPARATOR_DOT.join(qn_parts[:-1]) if len(qn_parts) >= 2 else None


def extract_method_call(member_expr_node: Node) -> str | None:
    object_node = member_expr_node.child_by_field_name(cs.FIELD_OBJECT)
    property_node = member_expr_node.child_by_field_name(cs.FIELD_PROPERTY)

    if object_node and property_node:
        object_text = object_node.text
        property_text = property_node.text

        if object_text and property_text:
            object_name = safe_decode_text(object_node)
            property_name = safe_decode_text(property_node)
            return f"{object_name}{cs.SEPARATOR_DOT}{property_name}"

    return None


def find_method_in_class_body(class_body_node: Node, method_name: str) -> Node | None:
    for child in class_body_node.children:
        if child.type == cs.TS_METHOD_DEFINITION:
            name_node = child.child_by_field_name(cs.FIELD_NAME)
            if name_node and name_node.text:
                found_name = safe_decode_text(name_node)
                if found_name == method_name:
                    return child

    return None


def repeats_member_signature(member_node: Node, seen_names: set[str]) -> bool:
    # An overloaded interface or abstract member is one signature per overload,
    # but a single member: only the first names it, so a later one registering
    # would mint an `@line` duplicate no call can bind and dead code reports.
    # `seen_names` is the caller's per-body record of names already declared.
    if member_node.type not in cs.TS_BODILESS_METHOD_TYPES:
        return False
    name = safe_decode_text(member_node.child_by_field_name(cs.FIELD_NAME))
    if name is None:
        return False
    if name in seen_names:
        return True
    seen_names.add(name)
    return False


_ANNOTATED_PARAMETER_TYPES = frozenset(
    {cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER}
)


def annotated_parameter_types(function_node: Node) -> list[tuple[str, str]]:
    """(name, written type name) for each plainly named, annotated parameter.

    The type name is the one a member call can dispatch on: `Repo`, the head of
    `Repo<T>`, or the one non-nullish member of `Repo | undefined`. A
    destructured parameter, or any other type shape, is left out.
    """
    parameters = function_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if parameters is None:
        return []
    found: list[tuple[str, str]] = []
    for parameter in parameters.named_children:
        if parameter.type not in _ANNOTATED_PARAMETER_TYPES:
            continue
        pattern = parameter.child_by_field_name(cs.TS_FIELD_PATTERN)
        annotation = parameter.child_by_field_name(cs.FIELD_TYPE)
        if pattern is None or pattern.type != cs.TS_IDENTIFIER or annotation is None:
            continue
        name = safe_decode_text(pattern)
        type_name = _dispatch_type_name(next(iter(annotation.named_children), None))
        if name and type_name:
            found.append((name, type_name))
    return found


def _dispatch_type_name(type_node: Node | None) -> str | None:
    if type_node is not None and type_node.type == cs.TS_UNION_TYPE:
        members = [
            member
            for member in type_node.named_children
            if (safe_decode_text(member) or "") not in cs.TS_NULLISH_TYPE_TEXTS
        ]
        type_node = members[0] if len(members) == 1 else None
    if type_node is not None and type_node.type == cs.TS_GENERIC_TYPE:
        type_node = type_node.child_by_field_name(cs.FIELD_NAME)
    if type_node is None or type_node.type != cs.TS_TYPE_IDENTIFIER:
        return None
    return safe_decode_text(type_node)


_CLASS_BODY_CACHE: dict[str, Node | None] = {}
# The OWNER is a strong reference, never a bare id(): a freed tree's heap
# address gets recycled, so an integer owner could masquerade as current and
# serve Node values from a dead tree (the xdist worker-distribution flake,
# issue #1042). Holding the reference pins the owner's tree, so no live tree
# can alias its address — which is what makes NODE EQUALITY sound here, and
# equality (not identity) is required because each `tree.root_node` access
# mints a fresh wrapper object over the same tree node. Pins exactly one
# tree, the one the cache describes.
_CLASS_BODY_CACHE_OWNER: Node | None = None


def find_method_in_ast(
    root_node: Node, class_name: str, method_name: str
) -> Node | None:
    global _CLASS_BODY_CACHE_OWNER
    if _CLASS_BODY_CACHE_OWNER != root_node:
        _CLASS_BODY_CACHE.clear()
        _CLASS_BODY_CACHE_OWNER = root_node
    cache_key = class_name
    if cache_key in _CLASS_BODY_CACHE:
        body_node = _CLASS_BODY_CACHE[cache_key]
        if body_node is not None:
            return find_method_in_class_body(body_node, method_name)
        return None

    declaration = _class_declaration_named(root_node, class_name)
    body_node = (
        declaration.child_by_field_name(cs.FIELD_BODY)
        if declaration is not None
        else None
    )
    _CLASS_BODY_CACHE[cache_key] = body_node
    if body_node:
        return find_method_in_class_body(body_node, method_name)
    return None


# Statements that wrap a declaration without opening a scope: `export`,
# `export default`, `declare`, and the expression statement the grammar
# parses a bare `namespace N {}` as.
_DECLARATION_WRAPPERS = frozenset(
    {cs.TS_EXPORT_STATEMENT, cs.TS_AMBIENT_DECLARATION, cs.TS_EXPRESSION_STATEMENT}
)
_CLASS_DECLARATIONS = frozenset(
    {cs.TS_CLASS_DECLARATION, cs.TS_ABSTRACT_CLASS_DECLARATION}
)
# What may hold a class on its qn path: a namespace or `declare module`
# block, a function, a class, or one of a class's methods.
_CLASS_PATH_HOLDERS = _CLASS_DECLARATIONS | {
    cs.TS_INTERNAL_MODULE,
    cs.TS_MODULE,
    cs.TS_FUNCTION_DECLARATION,
    cs.TS_GENERATOR_FUNCTION_DECLARATION,
    cs.TS_METHOD_DEFINITION,
}


def find_class_body_at(root_node: Node, path: list[str]) -> Node | None:
    """The body of the class a qn names, `path` being its segments below
    the module: each but the last names the namespace, function, class or
    method declaring the next, read in that scope alone. A same-named class
    declared anywhere else in the file is a different class and never
    answers, so a function-local `Box` does not stand in for the module's.
    """
    scope: Node | None = root_node
    for index, name in enumerate(path):
        wanted = _CLASS_DECLARATIONS if index == len(path) - 1 else _CLASS_PATH_HOLDERS
        member = _scope_member(scope, name, wanted) if scope is not None else None
        scope = member.child_by_field_name(cs.FIELD_BODY) if member else None
    return scope


# A block also declares names through `const` / `let` / `var` statements.
_BINDING_WRAPPERS = _DECLARATION_WRAPPERS | {
    cs.TS_LEXICAL_DECLARATION,
    cs.TS_VARIABLE_DECLARATION,
}
# What binds a name a type annotation or a `new` reads.
# TypeScript keeps types and values in separate namespaces. In a type
# position only a type declaration shadows (a class, interface, type alias or
# enum; a type parameter, read separately); a namespace is no type, and a
# `const` or plain `function` is a value. In a value position (`new X()`) any
# value declaration shadows: a class, function, variable, enum or namespace.
_TYPE_BINDINGS = _CLASS_DECLARATIONS | {
    cs.TS_INTERFACE_DECLARATION,
    cs.TS_TYPE_ALIAS_DECLARATION,
    cs.TS_ENUM_DECLARATION,
}
_VALUE_BINDINGS = _CLASS_DECLARATIONS | {
    cs.TS_FUNCTION_DECLARATION,
    cs.TS_GENERATOR_FUNCTION_DECLARATION,
    cs.TS_VARIABLE_DECLARATOR,
    cs.TS_ENUM_DECLARATION,
    cs.TS_INTERNAL_MODULE,
}
# Nodes a declaration's qn path passes through without a segment of its own.
_PATH_TRANSPARENT = _DECLARATION_WRAPPERS | {cs.TS_STATEMENT_BLOCK, cs.TS_CLASS_BODY}


def enclosing_type_declaration(site: Node, name: str) -> Node | None:
    """The declaration a type name read at `site` binds to below module
    level: the nearest enclosing block's own type declaration of it, or a
    type parameter of an enclosing class, function or method. ``None`` when
    only the module, an import, or nothing declares it."""
    return _enclosing_binding(site, name, _TYPE_BINDINGS, type_parameters=True)


def enclosing_value_declaration(site: Node, name: str) -> Node | None:
    """The nearest enclosing block's own value declaration of `name`, read
    at `site`, below module level. Parameters, hoisted `var`s and catch or
    loop bindings are left to the callable's binding index."""
    return _enclosing_binding(site, name, _VALUE_BINDINGS, type_parameters=False)


def declaring_block(declaration: Node) -> Node | None:
    """The block a declaration's name is bound in (past `export`/`const`)."""
    node = declaration.parent
    while node is not None and node.type != cs.TS_STATEMENT_BLOCK:
        node = node.parent
    return node


def _enclosing_binding(
    site: Node, name: str, kinds: frozenset[str], type_parameters: bool
) -> Node | None:
    node = site.parent
    while node is not None and node.type != cs.TS_PROGRAM:
        if node.type == cs.TS_STATEMENT_BLOCK and (
            found := _scope_member(node, name, kinds, _BINDING_WRAPPERS)
        ):
            return found
        if type_parameters and (param := _type_parameter(node, name)) is not None:
            return param
        node = node.parent
    return None


def _type_parameter(node: Node, name: str) -> Node | None:
    type_params = node.child_by_field_name(cs.TS_FIELD_TYPE_PARAMETERS)
    if type_params is None:
        return None
    return next(
        (p for p in type_params.named_children if _declared_name(p) == name), None
    )


def class_qn_path(declaration: Node) -> list[str] | None:
    """A class declaration's qn segments below its module, the inverse of
    `find_class_body_at`; ``None`` for anything but a class, or when a scope
    holding it has no name a qn can carry (an arrow, an `if` block)."""
    if declaration.type not in _CLASS_DECLARATIONS:
        return None
    path: list[str] = []
    node: Node | None = declaration
    while node is not None and node.type != cs.TS_PROGRAM:
        if node.type in _CLASS_PATH_HOLDERS:
            if not (segment := _declared_name(node)):
                return None
            path.append(segment)
        elif node.type not in _PATH_TRANSPARENT:
            return None
        node = node.parent
    return path[::-1]


def _scope_member(
    scope: Node,
    name: str,
    wanted: frozenset[str],
    wrappers: frozenset[str] = _DECLARATION_WRAPPERS,
) -> Node | None:
    # The first declaration of `name` among the scope's own statements.
    stack = list(reversed(scope.named_children))
    while stack:
        node = stack.pop()
        if node.type in wrappers:
            stack.extend(reversed(node.named_children))
        elif node.type in wanted and _declared_name(node) == name:
            return node
    return None


def _declared_name(node: Node) -> str | None:
    # `declare module "x"` names its block with a string literal.
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    if name_node is None:
        return None
    fragment = next(
        (c for c in name_node.named_children if c.type == cs.TS_STRING_FRAGMENT),
        None,
    )
    return safe_decode_text(fragment or name_node)


def _class_declaration_named(root_node: Node, class_name: str) -> Node | None:
    # The first `class_declaration` named `class_name`, in source order.
    stack: list[Node] = [root_node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_CLASS_DECLARATION:
            name_node = current.child_by_field_name(cs.FIELD_NAME)
            if (
                name_node
                and name_node.text
                and safe_decode_text(name_node) == class_name
            ):
                return current
        stack.extend(reversed(current.children))
    return None


_JS_RETURN_QUERY = "(return_statement) @return_stmt"


def find_return_statements(
    node: Node, return_nodes: list[Node], language_obj=None
) -> None:
    if language_obj is not None:
        try:
            q = get_cached_query(language_obj, _JS_RETURN_QUERY)
            cursor = QueryCursor(q)
            captures = cursor.captures(node)
            return_nodes.extend(captures.get("return_stmt", []))
            return
        except Exception:  # noqa: S110 - a failed query falls back to the walk below
            pass
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_RETURN_STATEMENT:
            return_nodes.append(current)
        stack.extend(reversed(current.children))


_CONSTRUCTOR_WRAPPER_TYPES = cs.TS_CAST_WRAPPER_TYPES | {cs.TS_PARENTHESIZED_EXPRESSION}
_NAMED_CONSTRUCTOR_TYPES = frozenset({cs.TS_IDENTIFIER, cs.TS_MEMBER_EXPRESSION})


def constructor_of(new_expr_node: Node) -> Node | None:
    """The constructor a `new` expression names, under any parentheses or casts.

    `new (X)(...)`, `new (X as any)(...)`, `new (X satisfies T)(...)` and
    `new (X!)(...)` all construct `X`, as `new X(...)` does; reading the
    sub-expression as written named nothing (zod's
    `new (Internals as any)(def)`, issue #2874). Unwrapping stops at a name
    or a member path: `new (getCtor())()` or `new (flag ? A : B)()`
    constructs a computed value, so the node is returned as written.
    """
    constructor_node = new_expr_node.child_by_field_name(cs.FIELD_CONSTRUCTOR)
    current = constructor_node
    while current is not None and current.type in _CONSTRUCTOR_WRAPPER_TYPES:
        current = next(
            (c for c in current.named_children if c.type != cs.TS_COMMENT), None
        )
    if current is not None and current.type in _NAMED_CONSTRUCTOR_TYPES:
        return current
    return constructor_node


def extract_constructor_name(new_expr_node: Node) -> str | None:
    if new_expr_node.type != cs.TS_NEW_EXPRESSION:
        return None

    constructor_node = constructor_of(new_expr_node)
    if constructor_node and constructor_node.type == cs.TS_IDENTIFIER:
        constructor_text = constructor_node.text
        if constructor_text:
            return safe_decode_text(constructor_node)

    return None


def construction_at(root: Node, start: int, text: str) -> Node | None:
    # A call name carries a chain's receiver as text that starts where the
    # call does. Finding that span in the file's own tree, rather than
    # parsing the text alone, keeps the scopes around it, so `new Box()`
    # reads the `Box` bound there. Only a receiver that IS the construction,
    # under any parentheses, is returned: `(new Box() || other)` can
    # evaluate to something else.
    text = text.rstrip()
    if not text.lstrip(cs.JS_RECEIVER_LEADING_CHARS).startswith(cs.JS_NEW_KEYWORD):
        return None
    end = start + len(text.encode(cs.ENCODING_UTF8))
    node = root.named_descendant_for_byte_range(start, end)
    if node is None or node.start_byte != start or node.end_byte != end:
        return None
    while node.type == cs.TS_PARENTHESIZED_EXPRESSION and node.named_child_count == 1:
        node = node.named_children[0]
    return node if node.type == cs.TS_NEW_EXPRESSION else None


_BINDING_WRAPPER_TYPES = cs.TS_CAST_WRAPPER_TYPES | {cs.TS_PARENTHESIZED_EXPRESSION}


def arrow_binding_name(func_node: Node) -> str | None:
    # An arrow / function expression has no `name` field. Recover the binding
    # name for the two named forms whose VALUE is the arrow: a module/local
    # `const f = () => ...` (variable_declarator) and a class field
    # `helper = () => ...` (public_field_definition). Both the definition pass
    # (registering the arrow's qn) and the call pass (attributing the body's
    # calls) must derive the SAME name, or the caller qn is a phantom and the
    # arrow's body callbacks report dead; sharing this one helper keeps them in
    # step. Anonymous / destructured arrows, and arrows that are merely an
    # argument to a call bound to a name (`const m = useMutation(() => {})`),
    # stay unnamed: the arrow is not the binding's own value there.
    if func_node.type not in (
        cs.TS_ARROW_FUNCTION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_GENERATOR_FUNCTION,
    ):
        return None
    return _value_binding_name(func_node)


def _value_binding_name(node: Node) -> str | None:
    # Recover the name a nameless expression is bound to: the `name` of the
    # variable_declarator / public_field_definition whose `value` is this node.
    # The node may sit behind transparent wrappers (parens, TS casts:
    # `export const create = ((s) => ...) as Create`); climb them first so the
    # node is recognised as the binding's value.
    parent = node.parent
    while parent is not None and parent.type in _BINDING_WRAPPER_TYPES:
        node = parent
        parent = node.parent
    if parent is None:
        return None
    # `==` not `is`: py-tree-sitter returns a fresh Node wrapper per access, so
    # identity comparison always fails.
    if parent.child_by_field_name(cs.FIELD_VALUE) != node:
        return None
    name_node = parent.child_by_field_name(cs.FIELD_NAME)
    if name_node is None or name_node.type not in (
        cs.TS_IDENTIFIER,
        cs.TS_PROPERTY_IDENTIFIER,
    ):
        return None
    return safe_decode_text(name_node)


def name_is_body_scoped(func_node: Node) -> bool:
    # A named function expression binds its own name inside its body alone;
    # code elsewhere reaches it through whatever the VALUE is stored under.
    # Where that is the same name (`var f = function f`, `exports.f =
    # function f`) a lookup by the name still lands on a real binding. The
    # module's own export (`module.exports = function f`) is bound by each
    # importer under a name of its choosing, commonly `f`, so it keeps its
    # name too. What is left, a value stored under another name or none (a
    # callback argument, a return value, `var g = function f`), has a name
    # nothing outside its body can call (issue #2402).
    if func_node.type not in (cs.TS_FUNCTION_EXPRESSION, cs.TS_GENERATOR_FUNCTION):
        return False
    name_node = func_node.child_by_field_name(cs.FIELD_NAME)
    if name_node is None:
        return False
    value, holder = func_node, func_node.parent
    while holder is not None and holder.type in _BINDING_WRAPPER_TYPES:
        value, holder = holder, holder.parent
    if holder is None:
        return True
    if _holds_module_export(holder, value):
        return False
    return safe_decode_text(name_node) != _stored_under_name(holder, value)


def _holds_module_export(holder: Node, value: Node) -> bool:
    # `export default (function f () {})`, `module.exports = ...`, and the
    # `module.exports = exports = ...` chain's inner link.
    if holder.type == cs.TS_EXPORT_STATEMENT:
        return True
    target = _assignment_target(holder, value)
    if target is None:
        return False
    if target.type == cs.TS_IDENTIFIER:
        return safe_decode_text(target) == cs.JS_EXPORTS_KEYWORD
    if target.type != cs.TS_MEMBER_EXPRESSION:
        return False
    owner = target.child_by_field_name(cs.FIELD_OBJECT)
    member = target.child_by_field_name(cs.FIELD_PROPERTY)
    return (
        owner is not None
        and member is not None
        and safe_decode_text(owner) == cs.JS_MODULE_KEYWORD
        and safe_decode_text(member) == cs.JS_EXPORTS_KEYWORD
    )


def _assignment_target(holder: Node, value: Node) -> Node | None:
    if (
        holder.type != cs.TS_JS_ASSIGNMENT_EXPRESSION
        or holder.child_by_field_name(cs.FIELD_RIGHT) != value
    ):
        return None
    return holder.child_by_field_name(cs.FIELD_LEFT)


def _stored_under_name(holder: Node, value: Node) -> str | None:
    # The name `holder` stores `value` under: an object key, an assignment
    # target (its property, for `a.b = ...`), or a declarator / class field.
    if holder.type == cs.TS_PY_PAIR:
        key = holder.child_by_field_name(cs.FIELD_KEY)
        if key is None or holder.child_by_field_name(cs.FIELD_VALUE) != value:
            return None
        return safe_decode_text(key)
    if holder.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
        target = _assignment_target(holder, value)
        if target is not None and target.type == cs.TS_MEMBER_EXPRESSION:
            target = target.child_by_field_name(cs.FIELD_PROPERTY)
        return safe_decode_text(target) if target is not None else None
    return _value_binding_name(value)


def class_binding_name(class_node: Node) -> str | None:
    # An anonymous CLASS EXPRESSION (`static Proxy = class {...}`,
    # `const Proxy = class {...}`) has no `name` field. Recover the field /
    # declarator binding name so its methods attribute to `Outer.Proxy.<method>`
    # and are enumerated as callers; without it the class expression is skipped
    # in the caller pass and every callback inside its methods reports dead
    # (issue #970). A NAMED class expression (`class Named {}`) keeps its own
    # name. Non-class nodes return None.
    #
    # A class with no binding still needs a name, or it gets no Class node,
    # its members no Method nodes, and closures in them hang off a parent that
    # does not exist (issue #2567). The definition pass (via `_js_get_name`),
    # the caller pass and the closure-path walk all name the class here, so
    # they agree on its qn.
    if class_node.type != cs.TS_CLASS_EXPRESSION:
        return None
    name_node = class_node.child_by_field_name(cs.FIELD_NAME)
    if name_node is not None and name_node.text:
        return safe_decode_text(name_node)
    if (bound := _value_binding_name(class_node)) is not None:
        return bound
    if _is_default_export_value(class_node):
        return cs.JS_DEFAULT_EXPORT_NAME
    # Same position-based name as an anonymous function, so it is stable
    # across re-indexes of an unchanged file.
    row, col = class_node.start_point
    return f"{cs.PREFIX_ANONYMOUS}{row}_{col}"


def _is_default_export_value(node: Node) -> bool:
    # `export default class {...}`: the class is the statement's `value`. A
    # class merely nested in a default export (`export default function () {
    # return class {} }`) is not the default export and keeps a positional name.
    parent = node.parent
    while parent is not None and parent.type in _BINDING_WRAPPER_TYPES:
        node = parent
        parent = node.parent
    return (
        parent is not None
        and parent.type == cs.TS_EXPORT_STATEMENT
        and parent.child_by_field_name(cs.FIELD_VALUE) == node
        and any(child.type == cs.TS_EXPORT_DEFAULT for child in parent.children)
    )


def is_object_literal_method(func_node: Node) -> bool:
    # `{delay () {...}}` (getters and setters too): a method written in an
    # object literal, which binds no name in any scope.
    parent = func_node.parent
    return (
        func_node.type == cs.TS_METHOD_DEFINITION
        and parent is not None
        and parent.type == cs.TS_OBJECT
    )


def analyze_return_expression(expr_node: Node, method_qn: str) -> str | None:
    match expr_node.type:
        case cs.TS_NEW_EXPRESSION:
            if class_name := extract_constructor_name(expr_node):
                return _extract_class_qn(method_qn) or class_name
            return None

        case cs.TS_THIS:
            return _extract_class_qn(method_qn)

        case cs.TS_MEMBER_EXPRESSION:
            object_node = expr_node.child_by_field_name(cs.FIELD_OBJECT)
            if not object_node:
                return None

            match object_node.type:
                case cs.TS_THIS:
                    return _extract_class_qn(method_qn)
                case cs.TS_IDENTIFIER:
                    if object_node.text:
                        object_name = safe_decode_text(object_node)
                        qn_parts = method_qn.split(cs.SEPARATOR_DOT)
                        if len(qn_parts) >= 2 and object_name == qn_parts[-2]:
                            return cs.SEPARATOR_DOT.join(qn_parts[:-1])
            return None

        case _:
            return None


_JS_TS_DECLARATION_NAMED = frozenset(
    {
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
        cs.TS_CLASS_DECLARATION,
    }
)
# `const cb = function r() {}`: the expression binds `r` inside itself only.
_JS_TS_SELF_NAMED_FUNCTIONS = frozenset(
    {cs.TS_FUNCTION_EXPRESSION, cs.TS_GENERATOR_FUNCTION}
)
_JS_TS_PATTERN_CONTAINERS = frozenset(
    {
        cs.TS_OBJECT_PATTERN,
        cs.TS_ARRAY_PATTERN,
        cs.TS_REST_PATTERN,
        cs.TS_JS_FORMAL_PARAMETERS,
        cs.TS_REQUIRED_PARAMETER,
        cs.TS_OPTIONAL_PARAMETER,
    }
)


def js_ts_pattern_names(target: Node) -> set[str]:
    # The names a binding target binds. Binding positions only: a default's
    # right side, a computed key and a pair key read the enclosing scope.
    names: set[str] = set()
    stack: list[Node] = [target]
    while stack:
        node = stack.pop()
        if node.type in (
            cs.TS_IDENTIFIER,
            cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN,
        ):
            if name := safe_decode_text(node):
                names.add(name)
        elif node.type in (cs.TS_ASSIGNMENT_PATTERN, cs.TS_OBJECT_ASSIGNMENT_PATTERN):
            if (left := node.child_by_field_name(cs.FIELD_LEFT)) is not None:
                stack.append(left)
        elif node.type == cs.TS_PAIR_PATTERN:
            if (value := node.child_by_field_name(cs.FIELD_VALUE)) is not None:
                stack.append(value)
        elif node.type in _JS_TS_PATTERN_CONTAINERS:
            stack.extend(node.named_children)
    return names


def js_ts_own_scope_names(func_node: Node, assigned: bool = True) -> frozenset[str]:
    """Every name a function binds itself rather than reads from the
    function around it: a function expression's own name, its parameters,
    its declarations, loop and catch variables, the functions and classes
    it declares, and a plain name it assigns (which may hold another type
    by the time it is read), unless `assigned` is False. Nested functions
    are their own scopes and are not entered (issue #3200)."""
    names: set[str] = set()
    if func_node.type in _JS_TS_SELF_NAMED_FUNCTIONS and (
        (name_node := func_node.child_by_field_name(cs.FIELD_NAME)) is not None
        and (own_name := safe_decode_text(name_node))
    ):
        names.add(own_name)
    for field in (cs.FIELD_PARAMETERS, cs.TS_FIELD_PARAMETER):
        if (params := func_node.child_by_field_name(field)) is not None:
            names |= js_ts_pattern_names(params)
    body = func_node.child_by_field_name(cs.FIELD_BODY)
    stack: list[Node] = list(body.children) if body is not None else []
    while stack:
        node = stack.pop()
        names |= _js_ts_names_bound_by(node, assigned)
        # A nested function binds its own name here, the rest in its scope.
        if node.type not in cs.JS_TS_FUNCTION_NODES:
            stack.extend(node.children)
    return frozenset(names)


# The node types that bind every name of a pattern, and the field holding it.
_JS_TS_PATTERN_FIELDS = {
    cs.TS_VARIABLE_DECLARATOR: cs.FIELD_NAME,
    cs.TS_JS_FOR_IN_STATEMENT: cs.FIELD_LEFT,
    cs.TS_JS_CATCH_CLAUSE: cs.FIELD_PARAMETER,
}


def _js_ts_names_bound_by(node: Node, assigned: bool = True) -> set[str]:
    # The names one node binds in the scope it sits in.
    names: set[str] = set()
    if node.type in _JS_TS_DECLARATION_NAMED:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        if name_node is not None and (name := safe_decode_text(name_node)):
            names.add(name)
    if (field := _JS_TS_PATTERN_FIELDS.get(node.type)) is not None:
        if (target := node.child_by_field_name(field)) is not None:
            names |= js_ts_pattern_names(target)
    elif assigned and node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
        left = node.child_by_field_name(cs.FIELD_LEFT)
        if (
            left is not None
            and left.type == cs.TS_IDENTIFIER
            and (name := safe_decode_text(left))
        ):
            names.add(name)
    return names
