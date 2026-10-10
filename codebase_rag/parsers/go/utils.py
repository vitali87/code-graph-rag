from __future__ import annotations

from collections.abc import Callable, Collection

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol, NodeType
from ..utils import safe_decode_text

# What a Go type name can declare: a struct (Class), a named or alias type,
# or an interface. Receiver and constructor-return types name one of them.
TYPE_DECLARATION_TYPES = frozenset({NodeType.CLASS, NodeType.TYPE, NodeType.INTERFACE})


def package_level_definitions(
    registry: FunctionRegistryTrieProtocol,
    name: str,
    labels: Collection[str],
    in_package: Callable[[str], bool],
) -> list[str]:
    # A Go package spans its directory's files, and cgr files each package-level
    # declaration under its FILE module (`pkg.file.Name`), a segment the source
    # never writes. That segment is the file's stem, which can hold dots
    # (`helper.gen.go` files under `pkg.helper.gen`), so a qn's depth cannot
    # tell a file of `pkg` from one of the sub-package `pkg/helper` (whose
    # `gen.go` spells the same qn). `in_package` decides by the declaring
    # file, from its recorded path where the caller knows it (#2616 review).
    return [
        qn
        for qn in registry.find_ending_with(name)
        if registry.get(qn) in labels and in_package(qn)
    ]


# Statements whose `left` (or a type switch's `alias`) list declares names:
# `a, b := ...`, `for k, v := range m`, `case v := <-ch`, `switch t := x.(type)`.
_GO_BINDING_LIST_FIELDS = {
    cs.TS_GO_SHORT_VAR_DECLARATION: cs.FIELD_LEFT,
    cs.TS_GO_RANGE_CLAUSE: cs.FIELD_LEFT,
    cs.TS_GO_RECEIVE_STATEMENT: cs.FIELD_LEFT,
    cs.TS_GO_TYPE_SWITCH_STATEMENT: cs.FIELD_GO_ALIAS,
}
# Declarations whose `name` identifiers are the names they bind.
_GO_NAMED_BINDING_TYPES = frozenset(
    {
        cs.TS_GO_VAR_SPEC,
        cs.TS_GO_CONST_SPEC,
        cs.TS_GO_PARAMETER_DECLARATION,
        cs.TS_GO_VARIADIC_PARAMETER_DECLARATION,
    }
)
_GO_PARAMETER_TYPES = frozenset(
    {cs.TS_GO_PARAMETER_DECLARATION, cs.TS_GO_VARIADIC_PARAMETER_DECLARATION}
)
_GO_FUNCTION_TYPES = frozenset(
    {
        cs.TS_GO_FUNCTION_DECLARATION,
        cs.TS_GO_METHOD_DECLARATION,
        cs.TS_GO_FUNC_LITERAL,
    }
)
# The blocks, explicit and implicit, a local declaration is scoped to: the
# innermost one around it is where its name stops being in scope.
_GO_SCOPE_TYPES = _GO_FUNCTION_TYPES | frozenset(
    {
        cs.TS_GO_BLOCK,
        cs.TS_GO_IF_STATEMENT,
        cs.TS_GO_FOR_STATEMENT,
        cs.TS_GO_EXPRESSION_SWITCH_STATEMENT,
        cs.TS_GO_TYPE_SWITCH_STATEMENT,
        cs.TS_GO_SELECT_STATEMENT,
        cs.TS_GO_EXPRESSION_CASE,
        cs.TS_GO_TYPE_CASE,
        cs.TS_GO_COMMUNICATION_CASE,
        cs.TS_GO_DEFAULT_CASE,
    }
)

# A name a function binds for itself -> the byte spans it is in scope over.
GoLocalScopes = dict[str, tuple[tuple[int, int], ...]]


def local_binding_scopes(func_node: Node) -> GoLocalScopes:
    """Every name a function or method binds for itself (receiver,
    parameters, locals, and those of the closures inside it), with the byte
    spans where it is in scope. A bare call to one of them inside a span calls
    that value, whatever package-level function shares its name; outside
    every span the name is the package's again.

    The spans follow Go's scoping: a parameter covers its function's body; a
    local starts at the end of its declaration (`helper := helper()` reads
    the outer `helper`) and ends with the innermost block around it, an
    `if`/`for`/`switch` header's own implicit block included."""
    scopes: dict[str, list[tuple[int, int]]] = {}
    stack = [func_node]
    while stack:
        node = stack.pop()
        names = _bound_names(node)
        if names and (span := _binding_span(node)) is not None:
            for name in names:
                scopes.setdefault(name, []).append(span)
        stack.extend(node.named_children)
    return {name: tuple(spans) for name, spans in scopes.items()}


def _bound_names(node: Node) -> list[str]:
    if (field := _GO_BINDING_LIST_FIELDS.get(node.type)) is not None:
        bound = node.child_by_field_name(field)
        children = bound.named_children if bound is not None else []
    elif node.type in _GO_NAMED_BINDING_TYPES:
        children = node.children_by_field_name(cs.FIELD_NAME)
    else:
        return []
    return [
        name
        for child in children
        if child.type == cs.TS_GO_IDENTIFIER and (name := safe_decode_text(child))
    ]


def _binding_span(node: Node) -> tuple[int, int] | None:
    if node.type in _GO_PARAMETER_TYPES:
        # parameter_declaration -> parameter_list -> the function; a
        # parameter of a bare function TYPE binds nothing anywhere.
        owner = node.parent.parent if node.parent is not None else None
        if owner is None or owner.type not in _GO_FUNCTION_TYPES:
            return None
        body = owner.child_by_field_name(cs.FIELD_BODY)
        return (body.start_byte, body.end_byte) if body is not None else None
    if node.type == cs.TS_GO_TYPE_SWITCH_STATEMENT:
        # `switch t := x.(type) {...}`: `t` is declared in each clause, after
        # the guard is evaluated.
        guard = node.child_by_field_name(cs.FIELD_VALUE) or node.child_by_field_name(
            cs.FIELD_GO_ALIAS
        )
        start = guard.end_byte if guard is not None else node.start_byte
        return (start, node.end_byte)
    scope = node.parent
    while scope is not None and scope.type not in _GO_SCOPE_TYPES:
        scope = scope.parent
    return (node.end_byte, scope.end_byte) if scope is not None else None


def extract_package_name(root: Node) -> str | None:
    # The `package foo` clause names the Go package a file belongs to;
    # membership is (directory, package name), not directory alone.
    for child in root.named_children:
        if child.type != cs.TS_GO_PACKAGE_CLAUSE:
            continue
        for ident in child.named_children:
            if ident.type == cs.TS_GO_PACKAGE_IDENTIFIER:
                return safe_decode_text(ident)
    return None


def is_receiver_method(node: Node) -> bool:
    return (
        node.type == cs.TS_GO_METHOD_DECLARATION
        and node.child_by_field_name(cs.FIELD_RECEIVER) is not None
    )


def extract_receiver_type_name(node: Node) -> str | None:
    receiver = node.child_by_field_name(cs.FIELD_RECEIVER)
    if receiver is None:
        return None
    for param in receiver.children:
        if param.type != cs.TS_GO_PARAMETER_DECLARATION:
            continue
        type_node = param.child_by_field_name(cs.FIELD_TYPE)
        if type_node is not None:
            return type_identifier_text(type_node)
    return None


def extract_return_type_name(node: Node) -> str | None:
    # Name of a Go function/method's single return type as its file spells it
    # (`Root() *Command` -> "Command", `Item() *model.Item` -> "model.Item"), for
    # chained-call resolution. A parameter_list result (multiple/named returns)
    # is ambiguous for chaining, so it is skipped.
    result = node.child_by_field_name(cs.FIELD_RESULT)
    if result is None or result.type == cs.TS_GO_PARAMETER_LIST:
        return None
    name = _return_type_identifier(result)
    # A type parameter (`Identity[T any](v T) T`, `(h *Holder[T]) Get() T`)
    # stands for the call's type argument; a declared type sharing its name
    # (a struct `T`) is not what the call returns.
    if name is not None and name in _type_parameter_names(node):
        return None
    return name


def extract_first_return_type_name(node: Node) -> str | None:
    # FIRST return type of a Go function, for typing `v, err := f()` bindings under
    # the (T, error) idiom. Unlike extract_return_type_name (chaining, where a
    # multi-return callee is uncallable so the skip is correct), a parameter_list
    # result contributes its first declared type, and a qualified `pkg.T` keeps its
    # dotted text so a local bound to an external package's type stays typed rather
    # than trie-guessed.
    result = node.child_by_field_name(cs.FIELD_RESULT)
    if result is None:
        return None
    if result.type == cs.TS_GO_PARAMETER_LIST:
        for param in result.children:
            if param.type != cs.TS_GO_PARAMETER_DECLARATION:
                continue
            type_node = param.child_by_field_name(cs.FIELD_TYPE)
            return _first_return_identifier(type_node) if type_node else None
        return None
    return _first_return_identifier(result)


def _first_return_identifier(type_node: Node) -> str | None:
    if type_node.type == cs.TS_GO_QUALIFIED_TYPE:
        return safe_decode_text(type_node)
    if type_node.type == cs.TS_GO_POINTER_TYPE:
        for child in type_node.named_children:
            return _first_return_identifier(child)
        return None
    return _return_type_identifier(type_node)


def _return_type_identifier(type_node: Node) -> str | None:
    # Like type_identifier_text but does NOT unwrap composite types: a
    # `[]Command`/`map[k]Command`/`chan Command` return is a container, and a chained
    # call lands on the container, not the element, so it must not be unwrapped to
    # "Command" (which would emit a false edge). Only a plain type_identifier, a
    # pointer to one (`*Command`), or a generic base resolves. A type of another
    # package keeps its qualifier (`model.Item`, issue #2467): the bare `Item`
    # would name whatever the reader's own package calls Item, so the reader
    # resolves the qualifier through the declaring file's imports instead.
    if type_node.type in cs.TS_GO_CONTAINER_TYPES:
        return None
    if type_node.type == cs.TS_TYPE_IDENTIFIER and type_node.text:
        return safe_decode_text(type_node)
    if type_node.type == cs.TS_GO_QUALIFIED_TYPE:
        package = type_node.child_by_field_name(cs.FIELD_GO_PACKAGE)
        name = type_node.child_by_field_name(cs.FIELD_NAME)
        if package is None or name is None:
            return None
        return f"{safe_decode_text(package)}{cs.SEPARATOR_DOT}{safe_decode_text(name)}"
    if type_node.type in (cs.TS_GO_POINTER_TYPE, cs.TS_GENERIC_TYPE):
        for child in type_node.children:
            if name := _return_type_identifier(child):
                return name
    return None


def type_identifier_text(type_node: Node) -> str | None:
    if type_node.type == cs.TS_TYPE_IDENTIFIER and type_node.text:
        return safe_decode_text(type_node)
    # Unwrap pointer (*T) and generic (T[P]) receivers to the base name.
    for child in type_node.children:
        if name := type_identifier_text(child):
            return name
    return None


def call_receiver_chain(call_node: Node) -> tuple[Node, list[str]] | None:
    """`NewBox().With(1).Bump()` -> (the `NewBox()` call, ["With", "Bump"]).

    The method names called along a receiver chain, innermost first, and the
    node the chain starts from: a call, or a composite literal with its
    parentheses and `&` taken off (`(&Box{}).Bump()`). None when the callee
    is not a method on such a value; a variable, field or package receiver
    (`b.Bump()`, `pkg.F()`) is what the name-based resolver types already.
    """
    methods: list[str] = []
    node = call_node
    while node.type == cs.TS_GO_CALL_EXPRESSION:
        selector = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if selector is None or selector.type != cs.TS_GO_SELECTOR_EXPRESSION:
            break
        field = selector.child_by_field_name(cs.FIELD_FIELD)
        receiver = _value_receiver(selector.child_by_field_name(cs.FIELD_OPERAND))
        if field is None or receiver is None or not (name := safe_decode_text(field)):
            break
        methods.append(name)
        node = receiver
    if not methods:
        return None
    methods.reverse()
    return node, methods


def _value_receiver(operand: Node | None) -> Node | None:
    # A call or a composite literal under any parentheses. A unary operand
    # counts only around a literal, where `&Box{}` is Go's pointer to a fresh
    # value; `(*p).M()` and `(&x).M()` start from a variable instead.
    node = _unparenthesized(operand)
    if node is not None and node.type == cs.TS_GO_UNARY_EXPRESSION:
        inner = _unparenthesized(node.child_by_field_name(cs.FIELD_OPERAND))
        if inner is not None and inner.type == cs.TS_GO_COMPOSITE_LITERAL:
            return inner
        return None
    if node is not None and node.type in (
        cs.TS_GO_CALL_EXPRESSION,
        cs.TS_GO_COMPOSITE_LITERAL,
    ):
        return node
    return None


def _unparenthesized(node: Node | None) -> Node | None:
    while node is not None and node.type == cs.TS_PARENTHESIZED_EXPRESSION:
        node = next(iter(node.named_children), None)
    return node


def _type_parameter_names(node: Node) -> frozenset[str]:
    # The function's own `[T any, U comparable]` list, plus for a method the
    # names its generic receiver binds (`(h *Holder[T])` binds `T`).
    names: set[str] = set()
    params = node.child_by_field_name(cs.FIELD_GO_TYPE_PARAMETERS)
    if params is not None:
        for decl in params.named_children:
            if decl.type == cs.TS_GO_TYPE_PARAMETER_DECLARATION:
                names.update(
                    text
                    for child in decl.children_by_field_name(cs.FIELD_NAME)
                    if (text := safe_decode_text(child))
                )
    receiver = node.child_by_field_name(cs.FIELD_RECEIVER)
    if receiver is not None:
        for param in receiver.named_children:
            if param.type == cs.TS_GO_PARAMETER_DECLARATION:
                names.update(_receiver_type_arguments(param))
    return frozenset(names)


def _receiver_type_arguments(param: Node) -> set[str]:
    type_node = param.child_by_field_name(cs.FIELD_TYPE)
    if type_node is not None and type_node.type == cs.TS_GO_POINTER_TYPE:
        type_node = next(iter(type_node.named_children), None)
    if type_node is None or type_node.type != cs.TS_GO_GENERIC_TYPE:
        return set()
    arguments = type_node.child_by_field_name(cs.FIELD_GO_TYPE_ARGUMENTS)
    if arguments is None:
        return set()
    return {
        text
        for elem in arguments.named_children
        if elem.type == cs.TS_GO_TYPE_ELEM
        for ident in elem.named_children
        if ident.type == cs.TS_TYPE_IDENTIFIER and (text := safe_decode_text(ident))
    }


def binds_locally(node: Node, name: str) -> bool:
    # Whether `name`, used at `node`, is bound inside its function rather than
    # at package level: a parameter, receiver or named result of an enclosing
    # function or closure, or a declaration earlier in an enclosing block.
    # Go scopes a local from the end of its declaration to the end of its
    # block, so a declaration counts only when it precedes the use in a block
    # that encloses it (`NewBox := NewBox()` still calls the package's).
    child = node
    parent = node.parent
    while parent is not None and parent.type != cs.TS_GO_SOURCE_FILE:
        if parent.type in cs.TS_GO_FUNCTION_SCOPES:
            if name in _signature_names(parent):
                return True
            if parent.type != cs.TS_GO_FUNC_LITERAL:
                return False
        elif any(
            name in _declared_names(parent, sibling)
            for sibling in parent.children
            if sibling.end_byte <= child.start_byte
        ):
            return True
        child, parent = parent, parent.parent
    return False


def _signature_names(function: Node) -> set[str]:
    names: set[str] = set()
    for field in (cs.FIELD_RECEIVER, cs.FIELD_PARAMETERS, cs.FIELD_RESULT):
        params = function.child_by_field_name(field)
        if params is None or params.type != cs.TS_GO_PARAMETER_LIST:
            continue
        for param in params.named_children:
            if param.type in (
                cs.TS_GO_PARAMETER_DECLARATION,
                cs.TS_GO_VARIADIC_PARAMETER_DECLARATION,
            ):
                names.update(
                    _identifier_texts(param.children_by_field_name(cs.FIELD_NAME))
                )
    return names


def _declared_names(parent: Node, statement: Node) -> set[str]:
    if statement.type in cs.TS_GO_LEFT_BINDING_STATEMENTS:
        left = statement.child_by_field_name(cs.FIELD_LEFT)
        return _identifier_texts(left.named_children) if left is not None else set()
    if statement.type in cs.TS_GO_SPEC_DECLARATIONS:
        specs = [
            spec
            for child in statement.named_children
            for spec in (
                child.named_children
                if child.type == cs.TS_GO_VAR_SPEC_LIST
                else [child]
            )
            if spec.type in cs.TS_GO_BINDING_SPECS
        ]
        return {
            text
            for spec in specs
            for text in _identifier_texts(spec.children_by_field_name(cs.FIELD_NAME))
        }
    if statement.type == cs.TS_GO_FOR_CLAUSE:
        initializer = statement.child_by_field_name(cs.FIELD_GO_INITIALIZER)
        return _declared_names(statement, initializer) if initializer else set()
    # `switch t := x.(type)` binds `t` in its alias list, before every case.
    if parent.type == cs.TS_GO_TYPE_SWITCH_STATEMENT and statement == (
        parent.child_by_field_name(cs.FIELD_ALIAS)
    ):
        return _identifier_texts(statement.named_children)
    return set()


def _identifier_texts(nodes: list[Node]) -> set[str]:
    return {
        text
        for node in nodes
        if node.type == cs.TS_GO_IDENTIFIER and (text := safe_decode_text(node))
    }
