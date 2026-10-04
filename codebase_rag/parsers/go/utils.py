from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


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
    node the chain starts from: a call, a type assertion (`a.(Dog).Fetch()`),
    or a composite literal with its parentheses and `&` taken off
    (`(&Box{}).Bump()`). None when the callee
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
        cs.TS_GO_TYPE_ASSERTION_EXPRESSION,
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
