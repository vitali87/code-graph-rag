from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from tree_sitter import QueryCursor

from ... import constants as cs
from ... import logs as ls
from ...node_text import get_cached_query, safe_decode_text
from ...types_defs import ASTNode, FunctionRegistryTrieProtocol, LanguageQueries
from ..definition_facts import DefinitionFacts
from ..utils import note_query_fallback
from .forward_refs import unquote_forward_refs
from .utils import resolve_class_name, resolve_dotted_class

# Deepest operand chain `_value_leaves` will walk. Each term of `a or b or c`
# or `x + y + z` is one level. Measured over every assignment in this repo the
# deepest chain is 6, so hand-written code never gets near this and only
# generated source reaches it. The cap exists because a RecursionError here is
# SILENT: the caller runs inside a broad `except Exception` that would drop the
# enclosing function's whole type map, which is the very defect #1868 fixes.
_MAX_ALIAS_DEPTH = 32

# Node types whose BODY is an inner scope; the named fields listed are the
# parts that still evaluate in the enclosing scope.
_INNER_SCOPE_OUTER_FIELDS: dict[str, tuple[str, ...]] = {
    cs.TS_PY_FUNCTION_DEFINITION: (cs.FIELD_PARAMETERS, cs.FIELD_RETURN_TYPE),
    cs.TS_PY_LAMBDA: (cs.FIELD_PARAMETERS, cs.FIELD_RETURN_TYPE),
    cs.TS_PY_CLASS_DEFINITION: (cs.FIELD_SUPERCLASSES,),
}
# Binding nodes whose target is the `left` field.
_LEFT_TARGET_TYPES = frozenset(
    {cs.TS_PY_ASSIGNMENT, cs.TS_PY_AUGMENTED_ASSIGNMENT, cs.TS_PY_FOR_STATEMENT}
)
# Destructuring targets whose identifiers are bindings; anything else under a
# target (attribute, subscript, call) is a mutation, not a rebinding. A plain
# `tuple` / `list` appears only under `as_pattern_target`
# (`with f() as (a, self):`), where it destructures like a pattern.
_PATTERN_TYPES = frozenset(
    {
        cs.TS_PY_PATTERN_LIST,
        cs.TS_PY_TUPLE_PATTERN,
        cs.TS_PY_LIST_PATTERN,
        cs.TS_PY_AS_PATTERN_TARGET,
        cs.TS_PY_TUPLE,
        cs.TS_PY_LIST,
    }
)


class _Alias(NamedTuple):
    """What a local variable was assigned, reduced to what it can evaluate to.

    `candidates` are the leaf expressions the right-hand side can take the
    value of, in preference order: names and attributes read the type map, a
    constructor call infers its own. For an operator expression they are the
    LEFT operand's leaves, `dunder` is the forward method (`__truediv__`) and
    `reflected` holds the right operand's leaves, consulted for the reflected
    method (`__rtruediv__`) when no left type supplies the forward one --
    which is Python's own dispatch order.
    """

    candidates: list[ASTNode]
    dunder: str | None
    reflected: list[ASTNode]


if TYPE_CHECKING:
    from ..import_processor import ImportProcessor
    from .ast_analyzer import PythonAstAnalyzer
    from .expression_analyzer import PythonExpressionAnalyzer


def _union_members(type_str: str) -> list[str]:
    """The non-None members of a type as written, in order: `A | None | B` -> [A, B]."""
    return [
        member
        for part in type_str.split(cs.PY_UNION_SEPARATOR)
        if (member := part.strip()) and member != cs.PY_NONE
    ]


def _non_none_members(type_str: str) -> frozenset[str]:
    """`Widget | None` and `Widget` name the same receiver; compare them as such."""
    return frozenset(_union_members(type_str))


class _DunderLookup(NamedTuple):
    """Where an operator method was found up a class's bases, and what it returns."""

    owner: str | None  # the class that defines it, None when nothing does
    returned: str | None


def _reflected_dunder(dunder: str) -> str:
    """`__truediv__` -> `__rtruediv__`: the method the RIGHT operand supplies."""
    return f"__r{dunder[2:]}"


def _name_spelling(name: str) -> str:
    """`user_repo`, `UserRepo` and `_UserRepo` all spell `userrepo`."""
    return name.replace(cs.CHAR_UNDERSCORE, "").lower()


def _container_element_type(type_str: str | None) -> str | None:
    """The element inside a canonical ``list[<element>]`` marker, else ``None``."""
    if (
        type_str
        and type_str.startswith(cs.PY_LIST_TYPE_PREFIX)
        and type_str.endswith("]")
    ):
        return type_str[len(cs.PY_LIST_TYPE_PREFIX) : -1] or None
    return None


def _first_parameter_name(def_node: ASTNode) -> str | None:
    params = def_node.child_by_field_name(cs.FIELD_PARAMETERS)
    first = params.named_children[0] if params and params.named_children else None
    if first is not None and first.type != cs.TS_PY_IDENTIFIER:
        first = next(
            (c for c in first.named_children if c.type == cs.TS_PY_IDENTIFIER), None
        )
    if first is None or first.text is None:
        return None
    return first.text.decode(cs.ENCODING_UTF8)


def _parameter_names(def_node: ASTNode) -> set[str]:
    params = def_node.child_by_field_name(cs.FIELD_PARAMETERS)
    names: set[str] = set()
    for param in params.named_children if params else []:
        ident = (
            param
            if param.type == cs.TS_PY_IDENTIFIER
            else next(
                (c for c in param.named_children if c.type == cs.TS_PY_IDENTIFIER),
                None,
            )
        )
        if ident is not None and ident.text is not None:
            names.add(ident.text.decode(cs.ENCODING_UTF8))
    return names


def _is_static_method(def_node: ASTNode) -> bool:
    parent = def_node.parent
    if parent is None or parent.type != cs.TS_PY_DECORATED_DEFINITION:
        return False
    for child in parent.named_children:
        if child.type == cs.TS_PY_DECORATOR and child.text is not None:
            text = child.text.decode(cs.ENCODING_UTF8).lstrip("@").strip()
            if text.split("(", 1)[0].rsplit(".", 1)[-1] in cs.STATIC_DECORATORS:
                return True
    return False


def _binds_name(target: ASTNode, name: str) -> bool:
    """Whether a binding target binds `name` as a plain local.

    A bare identifier, or one inside a destructuring pattern
    (`a, self = pair`). An identifier inside an attribute or subscript
    target (`self.cache = value`, `self[k] = v`) mutates something the
    receiver refers to and rebinds nothing, so those are not entered.
    """
    stack = [target]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PY_IDENTIFIER:
            if node.text is not None and node.text.decode(cs.ENCODING_UTF8) == name:
                return True
            continue
        if node.type in _PATTERN_TYPES:
            stack.extend(node.named_children)
    return False


def _rebinds(def_node: ASTNode, name: str) -> bool:
    """Whether the def's OWN body rebinds `name` in any binding form.

    Assignment and augmented assignment (`self = pick()`, `a, self = pair`),
    a `for self in ...` target, a `with ... as self` / `except ... as self`
    alias, a walrus `(self := pick())`, a `case` capture (`case self:`,
    `case Foo(x=self):`, `case [self, *rest]:`, `case Foo() as self:`)
    and an import (`import self`, `from m import f as self`). Nested def,
    lambda and class BODIES are not descended into (a binding there
    belongs to that scope); their parameter defaults, annotations, return
    annotation and superclass arguments are, because those evaluate in
    this scope. An attribute or subscript target (`self.cache = value`)
    is a mutation, not a rebinding.
    """
    stack = list(def_node.named_children)
    while stack:
        node = stack.pop()
        if node.type in _INNER_SCOPE_OUTER_FIELDS:
            stack.extend(_outer_scope_parts(node))
            continue
        if _node_binds(node, name):
            return True
        stack.extend(node.named_children)
    return False


def _outer_scope_parts(node: ASTNode) -> list[ASTNode]:
    """The children of a nested def, lambda or class that evaluate OUTSIDE it.

    The body is the inner scope. The PARAMETERS (default values and
    annotations such as `x=(self := pick())`), the RETURN ANNOTATION
    (`-> (self := pick())`) and a class's superclass arguments evaluate
    in the enclosing scope, so the walk continues into those alone.
    """
    parts = (
        node.child_by_field_name(field)
        for field in _INNER_SCOPE_OUTER_FIELDS[node.type]
    )
    return [part for part in parts if part is not None]


def _node_binds(node: ASTNode, name: str) -> bool:
    """Whether this ONE node, ignoring its descendants, binds `name`."""
    target = _binding_target(node)
    if target is not None:
        return _binds_name(target, name)
    if node.type in (cs.TS_PY_CASE_PATTERN, cs.TS_PY_KEYWORD_PATTERN):
        return _case_pattern_captures(node, name)
    if node.type == cs.TS_PY_IMPORT_FROM_STATEMENT:
        # `from m import self` binds the bare name; an aliased import
        # is handled as its own node by `_binding_target`.
        return any(
            _is_bare_name(imported, name)
            for imported in node.children_by_field_name(cs.FIELD_NAME)
        )
    if node.type == cs.TS_PY_IMPORT_STATEMENT:
        return any(
            _import_binds(imported, name)
            for imported in node.children_by_field_name(cs.FIELD_NAME)
        )
    return False


def _binding_target(node: ASTNode) -> ASTNode | None:
    """The target subtree of a binding node, or None for a non-binding node.

    Assignment, augmented assignment and `for` bind their left side; a
    walrus its name; a `with`/`except`/`case ... as` its alias; an
    `import a as b` its alias.
    """
    if node.type in _LEFT_TARGET_TYPES:
        return node.child_by_field_name(cs.FIELD_LEFT)
    if node.type == cs.TS_PY_NAMED_EXPRESSION:
        return node.child_by_field_name(cs.FIELD_NAME)
    if node.type == cs.TS_PY_ALIASED_IMPORT:
        return node.child_by_field_name(cs.FIELD_ALIAS)
    if node.type == cs.TS_PY_AS_PATTERN:
        alias = node.child_by_field_name(cs.FIELD_ALIAS)
        if alias is not None:
            return alias
        # `case <pattern> as self:` has no alias field; the capture is
        # the trailing identifier.
        return node.named_children[-1] if node.named_children else None
    return None


def _import_binds(imported: ASTNode, name: str) -> bool:
    """Whether `import self` / `import self.sub` binds `name`.

    A plain import binds its FIRST component; `import a as self` is the
    `aliased_import` node, handled by `_binding_target`.
    """
    if imported.type != cs.TS_PY_DOTTED_NAME or not imported.named_children:
        return False
    return _is_bare_name(imported.named_children[0], name)


def _is_bare_name(node: ASTNode, name: str) -> bool:
    """Whether `node` is a one-part `dotted_name` (or identifier) spelling `name`.

    In a case pattern a one-part dotted name is a CAPTURE; a multi-part
    one (`Colour.RED`) is a value pattern and binds nothing.
    """
    if node.type == cs.TS_PY_DOTTED_NAME:
        if node.named_child_count != 1:
            return False
        node = node.named_children[0]
    return (
        node.type == cs.TS_PY_IDENTIFIER
        and node.text is not None
        and node.text.decode(cs.ENCODING_UTF8) == name
    )


def _case_pattern_captures(node: ASTNode, name: str) -> bool:
    """Whether the DIRECT children of a case/keyword pattern capture `name`.

    A bare dotted name (`case self:`), or a splat (`case [*self]:`). The
    class name of `Foo(...)` sits under `class_pattern`, not here, and the
    keyword of `Foo(x=self)` is the first child of `keyword_pattern`, so
    neither is taken. Nested patterns are reached by the caller's walk.
    """
    children = node.named_children
    if node.type == cs.TS_PY_KEYWORD_PATTERN:
        children = children[1:]
    for child in children:
        if child.type == cs.TS_PY_SPLAT_PATTERN:
            child = child.named_children[0] if child.named_children else child
        if _is_bare_name(child, name):
            return True
    return False


def _receiver_parameter_names(caller_node: ASTNode) -> list[str]:
    """The bound receiver (`self` or `cls`) visible to `caller_node`, or nothing.

    Only the method DIRECTLY in the class body has a bound receiver, and
    only when it is not a staticmethod: its first parameter, named `self`
    or `cls`. A staticmethod's `self` or a nested def's own `self`
    parameter is a caller-supplied value of unknown type. A closure inside
    a method sees the method's receiver unless one of the defs between
    them declares a parameter of that name (shadowing). A def that
    rebinds the receiver in its body (`self = pick()`) gets no seed: the
    alias pass yields to an existing entry, so the seed would have kept
    the class type over the factory's.
    """
    chain: list[ASTNode] = []
    node: ASTNode | None = caller_node
    while node is not None and node.type != cs.TS_PY_CLASS_DEFINITION:
        if node.type == cs.TS_PY_FUNCTION_DEFINITION:
            chain.append(node)
        node = node.parent
    if node is None or not chain:
        return []  # not inside a class at all
    method = chain[-1]
    if _is_static_method(method):
        return []
    receiver = _first_parameter_name(method)
    if receiver not in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS):
        return []
    for inner in chain[:-1]:
        if receiver in _parameter_names(inner):
            return []
    if any(_rebinds(d, receiver) for d in chain):
        return []
    return [receiver]


def _process_parameter(param: ASTNode, local_var_types: dict[str, str]) -> None:
    match param.type:
        case cs.TS_PY_TYPED_PARAMETER:
            _process_typed_parameter(param, local_var_types)
        case cs.TS_PY_TYPED_DEFAULT_PARAMETER:
            _process_typed_default_parameter(param, local_var_types)


def _process_typed_parameter(param: ASTNode, local_var_types: dict[str, str]) -> None:
    param_name_node = next(
        (c for c in param.children if c.type == cs.TS_PY_IDENTIFIER), None
    )
    param_type_node = param.child_by_field_name(cs.TS_FIELD_TYPE)
    if not (
        param_name_node
        and param_type_node
        and param_name_node.text
        and param_type_node.text
        and (param_name := safe_decode_text(param_name_node))
        and (param_type := safe_decode_text(param_type_node))
    ):
        return
    local_var_types[param_name] = unquote_forward_refs(param_type)


def _process_typed_default_parameter(
    param: ASTNode, local_var_types: dict[str, str]
) -> None:
    param_name_node = param.child_by_field_name(cs.TS_FIELD_NAME)
    param_type_node = param.child_by_field_name(cs.TS_FIELD_TYPE)
    if not (
        param_name_node
        and param_type_node
        and param_name_node.text
        and param_type_node.text
        and (param_name := safe_decode_text(param_name_node))
        and (param_type := safe_decode_text(param_type_node))
    ):
        return
    local_var_types[param_name] = unquote_forward_refs(param_type)


def _find_best_class_match(
    param_name: str, available_class_names: list[str]
) -> str | None:
    # Only the parameter's WHOLE name spelling a class names it: `payload`
    # a `Payload`, `user_repo` a `UserRepo`. A suffix or substring does not
    # (`data` is no `A`, `user` no `AppUser`), nor does a class name too
    # short to tell from an ordinary variable, and two classes spelled
    # alike leave the parameter untyped rather than pick one (#2608).
    spelled = _name_spelling(param_name)
    matches = {
        class_name
        for class_name in available_class_names
        if len(class_spelling := _name_spelling(class_name))
        >= cs.PY_PARAM_NAME_MIN_CLASS_LEN
        and class_spelling == spelled
    }
    match = next(iter(matches)) if len(matches) == 1 else None
    logger.debug(ls.PY_NAME_MATCH, param=param_name, match=match)
    return match


def _infer_list_element_type(list_node: ASTNode) -> str | None:
    for child in list_node.children:
        if child.type != cs.TS_PY_CALL:
            continue
        func_node = child.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if (
            func_node
            and func_node.type == cs.TS_PY_IDENTIFIER
            and func_node.text
            and (class_name := safe_decode_text(func_node))
            and class_name[0].isupper()
        ):
            return class_name
    return None


def _enclosing_class_node(node: ASTNode) -> ASTNode | None:
    current = node.parent
    while current is not None:
        if current.type == cs.TS_PY_CLASS_DEFINITION:
            return current
        current = current.parent
    return None


def _find_init_method_node(class_node: ASTNode) -> ASTNode | None:
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return None
    for child in body.children:
        if child.type == cs.TS_PY_DECORATED_DEFINITION:
            func = next(
                (c for c in child.children if c.type == cs.TS_PY_FUNCTION_DEFINITION),
                None,
            )
        elif child.type == cs.TS_PY_FUNCTION_DEFINITION:
            func = child
        else:
            continue
        if func is None:
            continue
        name_node = func.child_by_field_name(cs.FIELD_NAME)
        if (
            name_node
            and (text := name_node.text)
            and text.decode(cs.ENCODING_UTF8) == cs.PY_METHOD_INIT
        ):
            return func
    return None


def _has_property_decorator(decorated_node: ASTNode) -> bool:
    for child in decorated_node.children:
        if child.type == cs.TS_PY_DECORATOR and (text := child.text):
            tail = (
                text.decode(cs.ENCODING_UTF8)
                .lstrip(cs.DECORATOR_AT)
                .split(cs.SEPARATOR_DOT)[-1]
            )
            if tail in cs.PROPERTY_DECORATORS:
                return True
    return False


def _infer_property_return_types(
    caller_node: ASTNode, local_var_types: dict[str, str]
) -> None:
    # self.prop where prop is an @property carries the property's declared
    # return type, so a chained self.prop.method() resolves against the
    # returned class rather than an ambiguous same-named method elsewhere.
    if (class_node := _enclosing_class_node(caller_node)) is None:
        return
    _collect_property_return_types(class_node, local_var_types)


def _collect_property_return_types(class_node: ASTNode, out: dict[str, str]) -> None:
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return
    for child in body.children:
        if child.type != cs.TS_PY_DECORATED_DEFINITION:
            continue
        if not _has_property_decorator(child):
            continue
        func = next(
            (c for c in child.children if c.type == cs.TS_PY_FUNCTION_DEFINITION),
            None,
        )
        if func is None:
            continue
        name_node = func.child_by_field_name(cs.FIELD_NAME)
        return_node = func.child_by_field_name(cs.FIELD_RETURN_TYPE)
        if not (
            name_node
            and (name_text := name_node.text)
            and return_node
            and (return_text := return_node.text)
        ):
            continue
        # The return_type field wraps a type node; only a bare class name (not
        # a union, subscripted generic, or string forward ref) seeds a type.
        return_type = return_text.decode(cs.ENCODING_UTF8)
        if return_type.isidentifier():
            out.setdefault(
                f"{cs.PY_SELF_PREFIX}{name_text.decode(cs.ENCODING_UTF8)}",
                return_type,
            )


def _infer_class_annotation_types(
    caller_node: ASTNode, local_var_types: dict[str, str]
) -> None:
    # A class-level annotation (`_store: Store`) declares an instance
    # attribute's type even when it is assigned from a factory call whose return
    # type cannot be inferred, so seed self.<name> from the annotation.
    if (class_node := _enclosing_class_node(caller_node)) is None:
        return
    _collect_class_annotation_types(class_node, local_var_types)


def _collect_class_annotation_types(class_node: ASTNode, out: dict[str, str]) -> None:
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return
    for child in body.children:
        if child.type != cs.TS_PY_EXPRESSION_STATEMENT:
            continue
        assignment = child.children[0] if child.children else None
        if assignment is None or assignment.type != cs.TS_PY_ASSIGNMENT:
            continue
        left_node = assignment.child_by_field_name(cs.TS_FIELD_LEFT)
        type_node = assignment.child_by_field_name(cs.TS_FIELD_TYPE)
        if not (
            left_node
            and left_node.type == cs.TS_PY_IDENTIFIER
            and type_node
            and (name := safe_decode_text(left_node))
            and (type_text := safe_decode_text(type_node))
            and type_text.isidentifier()
        ):
            continue
        out.setdefault(f"{cs.PY_SELF_PREFIX}{name}", type_text)


def get_assignment_alias(right: ASTNode) -> _Alias | None:
    """Reduce an assignment's rhs to an `_Alias`, or None if it types nothing.

    A bare call (`x = Engine()`) is left to `traverse_single_pass`, which
    already types it. An operator whose dunder is not in the table makes
    no claim: its result type is unknown, and untyped is the safe answer.
    """
    if right.type == cs.TS_PY_CALL:
        return None
    dunder: str | None = None
    reflected: list[ASTNode] = []
    if right.type == cs.TS_PY_BINARY_OPERATOR:
        operator = right.child_by_field_name(cs.FIELD_OPERATOR)
        token = safe_decode_text(operator) if operator is not None else None
        dunder = cs.PY_BINARY_OPERATOR_DUNDERS.get(token or "")
        if dunder is None:
            return None
        left = right.child_by_field_name(cs.TS_FIELD_LEFT)
        right_operand = right.child_by_field_name(cs.TS_FIELD_RIGHT)
        candidates = _get_value_leaves(left, 1) if left is not None else []
        if right_operand is not None:
            reflected = _get_value_leaves(right_operand, 1)
    else:
        candidates = _get_value_leaves(right, 0)
    if not candidates and not reflected:
        return None
    return _Alias(candidates, dunder, reflected)


def _get_value_leaves(node: ASTNode, depth: int) -> list[ASTNode]:
    """The leaf expressions an rhs can evaluate to, in preference order.

    Without this a variable assigned from any expression got NO type, and a
    later `var.method()` fell back to matching the bare method name against
    every class that defines it (issue #1868): `resolved = target or
    Path("x")` followed by `resolved.resolve()` emitted a CALLS edge to every
    `resolve` method in the project.

    `depth` bounds the descent: a RecursionError here would not surface --
    this runs inside the broad `except Exception` in `type_inference.py`,
    which would discard the whole function's type map and revert every
    receiver in it to bare-name matching, reintroducing #1868 from an
    unrelated statement elsewhere in the same function.
    """
    if depth > _MAX_ALIAS_DEPTH:
        return []
    if node.type in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE, cs.TS_PY_CALL):
        return [node]
    if node.type == cs.TS_PY_BOOLEAN_OPERATOR:
        picks = _get_boolean_value_operands(node)
    elif node.type == cs.TS_PY_CONDITIONAL_EXPRESSION:
        # `a if cond else b`: the VALUE operands are the first and last
        # named children; the middle one is the condition, whose type is
        # irrelevant. None of the three carries a field name.
        kids = node.named_children
        picks = [kids[0], kids[-1]] if kids else []
    elif node.type == cs.TS_PY_BINARY_OPERATOR:
        # Nested under another expression (`(a / b) or c`): the left
        # operand is the value's origin and the operator is not tracked
        # this deep -- an approximation that is right for path idioms.
        left = node.child_by_field_name(cs.TS_FIELD_LEFT)
        picks = [left] if left is not None else []
    elif node.type == cs.TS_PY_PARENTHESIZED_EXPRESSION:
        picks = list(node.named_children[:1])
    else:
        return []
    return [leaf for pick in picks for leaf in _get_value_leaves(pick, depth + 1)]


def _get_boolean_value_operands(node: ASTNode) -> list[ASTNode]:
    """The operands `a or b` / `a and b` can evaluate to.

    `or` yields whichever operand is truthy first, so both are candidates,
    left preferred. `and` yields the RIGHT operand whenever the left is
    truthy, and a falsy left otherwise -- never something a method is then
    called on -- so only the right operand is a receiver candidate. Taking
    the left would type `flag and Engine()` as bool and drop the call.
    """
    operator = node.child_by_field_name(cs.FIELD_OPERATOR)
    is_and = operator is not None and safe_decode_text(operator) == cs.PY_OP_AND
    fields = [cs.TS_FIELD_RIGHT] if is_and else [cs.TS_FIELD_LEFT, cs.TS_FIELD_RIGHT]
    return [
        operand
        for field in fields
        if (operand := node.child_by_field_name(field)) is not None
    ]


def _collect_local_aliases(caller_node: ASTNode) -> dict[str, _Alias]:
    # Record what each local variable was assigned (resolver = self._resolver,
    # resolved = target or Path("x")), so its type propagates from what it
    # can evaluate to. Skip nested scopes.
    aliases: dict[str, _Alias] = {}
    boundary = (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION)
    stack: list[ASTNode] = list(caller_node.children)
    while stack:
        node = stack.pop()
        if node.type in boundary:
            continue
        if node.type == cs.TS_PY_ASSIGNMENT:
            left = node.child_by_field_name(cs.TS_FIELD_LEFT)
            right = node.child_by_field_name(cs.TS_FIELD_RIGHT)
            if (
                left is not None
                and left.type == cs.TS_PY_IDENTIFIER
                and right is not None
                and (local := safe_decode_text(left))
                and local not in aliases
                and (alias := get_assignment_alias(right)) is not None
            ):
                aliases[local] = alias
        stack.extend(node.children)
    return aliases


def _extract_variable_name(node: ASTNode) -> str | None:
    if node.type != cs.TS_PY_IDENTIFIER or node.text is None:
        return None
    return safe_decode_text(node) or None


class PythonVariableAnalyzer:
    __slots__ = (
        "import_processor",
        "function_registry",
        "queries",
        "facts",
        "ast_analyzer",
        "expression_analyzer",
        "_available_classes_cache",
        "_class_member_type_cache",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        facts: DefinitionFacts,
        available_classes_cache: dict[str, list[str]],
        class_member_type_cache: dict[str, dict[str, str]],
        ast_analyzer: PythonAstAnalyzer,
        expression_analyzer: PythonExpressionAnalyzer,
    ) -> None:
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.queries = queries
        self.facts = facts
        self.ast_analyzer = ast_analyzer
        self.expression_analyzer = expression_analyzer
        # Owned by the engine, which empties them between call passes.
        self._available_classes_cache = available_classes_cache
        self._class_member_type_cache = class_member_type_cache

    def build_local_variable_type_map(
        self, caller_node: ASTNode, module_qn: str, class_context: str | None = None
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}

        # No catch: a failing pass reaches the call pass, which records the
        # caller in the sync summary (#3173), as with every sibling engine.
        # Catching here returned a half-built map that kept the seeds.
        self._infer_parameter_types(caller_node, local_var_types, module_qn)
        # A method's `self` (a classmethod's `cls`) IS the enclosing
        # class. Seeded HERE, before the assignment walk, because that
        # walk types `w = self.parse()` through the receiver's entry:
        # without one the local stayed untyped and a later `w.render()`
        # fell to the bare-name fallback (issue #1901). Only a name that
        # is the FIRST PARAMETER of this def or of an enclosing def under
        # the class is seeded: a staticmethod has neither, and a body
        # binding `cls = pick()` in any def must keep the type the alias
        # pass gives it, which it could not with a seed already present
        # (that pass yields to an existing entry). The seed is an aid to
        # the walk only and is removed again below: `self.m()` CALLS keep
        # their own resolution (the concrete-sibling-over-abstract-stub
        # policy the resolver applies to self calls), which a typed `self`
        # in the returned map would override. An annotated parameter of
        # that name is kept as it is.
        seeded: list[str] = []
        if class_context:
            for name in _receiver_parameter_names(caller_node):
                if name not in local_var_types:
                    local_var_types[name] = class_context
                    seeded.append(name)
        # Single-pass traversal avoids O(5*N) traversals for type inference.
        comprehensions, for_statements = self.ast_analyzer.traverse_single_pass(
            caller_node, local_var_types, module_qn
        )
        self._infer_instance_attributes_from_init(
            caller_node, local_var_types, module_qn
        )
        _infer_property_return_types(caller_node, local_var_types)
        _infer_class_annotation_types(caller_node, local_var_types)
        # Attribute-backed iterables (`for w in self.widgets`) only type
        # after the attribute passes above populated `self.x`; re-running
        # the loop analyzers picks them up (they never downgrade a type).
        self.ast_analyzer.analyze_scoped_comprehensions(
            caller_node, comprehensions, local_var_types, module_qn
        )
        for for_stmt in for_statements:
            self.analyze_for_loop(for_stmt, local_var_types, module_qn)
        aliases = _collect_local_aliases(caller_node)
        self._expand_chained_attribute_types(local_var_types, module_qn, aliases)
        # The seed itself goes (a body binding that replaced it stays), and
        # so do the `cls.<field>` entries the chained-attribute pass
        # derives from a seeded `cls`, which no pass produces otherwise.
        # `self.<attr>` entries are KEPT: the instance-attribute passes
        # produce them without any seed and the property / chained-
        # attribute resolution reads them, so removing them broke it.
        cls_prefix = f"{cs.PY_KEYWORD_CLS}{cs.SEPARATOR_DOT}"
        for key in [
            k
            for k in local_var_types
            if (k in seeded and local_var_types[k] == class_context)
            or (cs.PY_KEYWORD_CLS in seeded and k.startswith(cls_prefix))
        ]:
            del local_var_types[key]

        return local_var_types

    def name_typed_parameters(
        self, caller_node: ASTNode, module_qn: str
    ) -> dict[str, str]:
        """The untyped parameters of `caller_node` typed only by their name.

        `payload` typed as `Payload` is a guess about the receiver, so a call
        through it is `heuristic`, never `exact` (issue #2608). A parameter
        the body rebinds gets no guess at all: it holds what its bindings
        give it (`payload = Payload()` binds exactly), and a binding of
        unknown type (`payload = factory()`) leaves it untyped rather than
        keep a guess no label would mark.
        """
        params_node = caller_node.child_by_field_name(cs.TS_FIELD_PARAMETERS)
        if params_node is None:
            return {}
        guessed: dict[str, str] = {}
        for param in params_node.named_children:
            if (
                param.type == cs.TS_PY_IDENTIFIER
                and param.text is not None
                and (name := param.text.decode(cs.ENCODING_UTF8))
                and (
                    class_name := self._infer_type_from_parameter_name(name, module_qn)
                )
                and not _rebinds(caller_node, name)
            ):
                guessed[name] = class_name
        return guessed

    def _infer_parameter_types(
        self, caller_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        params_node = caller_node.child_by_field_name(cs.TS_FIELD_PARAMETERS)
        if not params_node:
            return

        for param in params_node.children:
            _process_parameter(param, local_var_types)
        # An untyped parameter's name guess comes from the one list the call
        # resolver labels heuristic, so no guess binds a call exactly (#2608).
        guessed = self.name_typed_parameters(caller_node, module_qn)
        for param_name, class_name in guessed.items():
            local_var_types[param_name] = class_name
            logger.debug(ls.PY_PARAM_TYPE_INFERRED, param=param_name, type=class_name)

    def _infer_type_from_parameter_name(
        self, param_name: str, module_qn: str
    ) -> str | None:
        # `self` / `cls` ARE the enclosing class, which the type-map builder
        # seeds from the class itself; matched by name, an imported `F`
        # ("self".endswith("f")) took every receiver in the module (#2608).
        if param_name in cs.PY_RECEIVER_PARAM_NAMES:
            return None
        logger.debug(ls.PY_TYPE_INFER_ATTEMPT, param=param_name, module=module_qn)
        available_class_names = self._collect_available_classes(module_qn)
        logger.debug(ls.PY_AVAILABLE_CLASSES, classes=available_class_names)
        return _find_best_class_match(param_name, available_class_names)

    def _collect_available_classes(self, module_qn: str) -> list[str]:
        if module_qn in self._available_classes_cache:
            return self._available_classes_cache[module_qn]
        available_class_names: list[str] = []
        for qn, node_type in self.function_registry.find_with_prefix(module_qn):
            if node_type != cs.NodeLabel.CLASS:
                continue
            if cs.SEPARATOR_DOT.join(qn.split(cs.SEPARATOR_DOT)[:-1]) == module_qn:
                available_class_names.append(qn.split(cs.SEPARATOR_DOT)[-1])

        if module_qn not in self.import_processor.import_mapping:
            self._available_classes_cache[module_qn] = available_class_names
            return available_class_names

        for local_name, imported_qn in self.import_processor.import_mapping[
            module_qn
        ].items():
            if self.function_registry.get(imported_qn) == cs.NodeLabel.CLASS:
                available_class_names.append(local_name)

        self._available_classes_cache[module_qn] = available_class_names
        return available_class_names

    def analyze_comprehension(
        self, comp_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        for child in comp_node.children:
            if child.type == cs.TS_PY_FOR_IN_CLAUSE:
                self._analyze_for_clause(child, local_var_types, module_qn)

    def analyze_for_loop(
        self, for_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._analyze_for_clause(for_node, local_var_types, module_qn)

    def _analyze_for_clause(
        self, node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if (left_node := node.child_by_field_name(cs.TS_FIELD_LEFT)) and (
            right_node := node.child_by_field_name(cs.TS_FIELD_RIGHT)
        ):
            self._infer_loop_var_from_iterable(
                left_node, right_node, local_var_types, module_qn
            )

    def _infer_loop_var_from_iterable(
        self,
        left_node: ASTNode,
        right_node: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        if not (loop_var := _extract_variable_name(left_node)):
            return

        if element_type := self._infer_iterable_element_type(
            right_node, local_var_types, module_qn
        ):
            local_var_types[loop_var] = element_type
            logger.debug(ls.PY_LOOP_VAR_INFERRED, var=loop_var, type=element_type)

    def _infer_iterable_element_type(
        self, iterable_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        if iterable_node.type == cs.TS_PY_LIST:
            return _infer_list_element_type(iterable_node)

        if iterable_node.type == cs.TS_PY_CALL:
            # `for w in load_widgets():` types through the callee's container
            # return annotation (`-> list[Widget]`); a scalar return is not an
            # element type, so only the container marker unwraps. A
            # `self.load_more()` iterable falls back to the enclosing class's
            # own method.
            call_text = safe_decode_text(iterable_node)
            if not call_text:
                return None
            return _container_element_type(
                self.expression_analyzer.infer_method_call_return_type(
                    call_text, module_qn, local_var_types
                )
                or self._self_method_element_type(iterable_node, module_qn)
            )

        if iterable_node.type == cs.TS_PY_ATTRIBUTE:
            # `for w in self.widgets:` reads the attribute's already-inferred
            # container type (keyed as `self.widgets` by the self-assignment
            # pass).
            attr_text = safe_decode_text(iterable_node)
            if not attr_text:
                return None
            return _container_element_type(local_var_types.get(attr_text))

        if (
            iterable_node.type != cs.TS_PY_IDENTIFIER
            or iterable_node.text is None
            or (var_name := safe_decode_text(iterable_node)) is None
        ):
            return None
        return self._infer_variable_element_type(var_name, local_var_types, module_qn)

    def infer_instance_variable_types_from_assignments(
        self,
        assignments: list[ASTNode],
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        for assignment in assignments:
            self._process_self_assignment(assignment, local_var_types, module_qn)

    def _process_self_assignment(
        self, assignment: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = assignment.child_by_field_name(cs.TS_FIELD_LEFT)
        right_node = assignment.child_by_field_name(cs.TS_FIELD_RIGHT)
        if not (
            left_node
            and left_node.type == cs.TS_PY_ATTRIBUTE
            and (left_text := left_node.text)
            and (attr_name := left_text.decode(cs.ENCODING_UTF8)).startswith(
                cs.PY_SELF_PREFIX
            )
        ):
            return
        annotation_node = assignment.child_by_field_name(cs.TS_FIELD_TYPE)
        if annotation_node is not None:
            annotation = safe_decode_text(annotation_node)
            if annotation:
                annotation = unquote_forward_refs(annotation)
                # The shared normalizer accepts optional pipe unions, but not
                # their typing.Union spelling.
                if union := re.fullmatch(
                    r"(?:typing\.)?Union\[([^,\[\]]+),\s*None\]", annotation
                ):
                    annotation = f"{union.group(1)} | None"
            assigned_type = (
                self.ast_analyzer.type_of_annotated_name(annotation, module_qn)
                if annotation
                else None
            )
            if (
                assigned_type
                and cs.SEPARATOR_DOT in assigned_type
                and not self._get_project_class_qn(assigned_type, module_qn)
            ):
                # `models.Store` / `m.Store` names the class through the
                # module's imports; the registry only knows `proj.models.Store`.
                assigned_type = resolve_dotted_class(
                    assigned_type,
                    module_qn,
                    self.import_processor,
                    self.function_registry,
                )
            if assigned_type and self._get_project_class_qn(assigned_type, module_qn):
                local_var_types[attr_name] = assigned_type
            else:
                local_var_types.pop(attr_name, None)
            return
        if right_node is None:
            return
        # Read without its body's type map (a whole module's self-assignments,
        # or `__init__`'s for another method), so the assignment goes along:
        # `self.client = pkg.Client()` under a parameter `pkg` is not the
        # imported package's class.
        assigned_type = self.expression_analyzer.infer_type_from_expression(
            right_node, module_qn, assignment
        )
        if not assigned_type and right_node.type == cs.TS_PY_IDENTIFIER:
            # self.x = param: a bare identifier carries the type of the matching
            # (already-seeded) parameter or local, so flow it onto the attribute.
            ident = safe_decode_text(right_node)
            assigned_type = local_var_types.get(ident) if ident else None
        if not assigned_type:
            return
        local_var_types[attr_name] = assigned_type
        logger.debug(ls.PY_INSTANCE_VAR_INFERRED, attr=attr_name, type=assigned_type)

    def analyze_self_assignments(
        self, node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        py_lang_queries = self.queries.get(cs.SupportedLanguage.PYTHON)
        py_lang_obj = py_lang_queries["language"] if py_lang_queries else None
        if py_lang_obj is not None:
            # Only the query is guarded. Covering the processing too re-ran
            # every assignment through the walk after a failure part-way.
            try:
                q = get_cached_query(py_lang_obj, cs.PY_ASSIGNMENT_QUERY)
                captures = QueryCursor(q).captures(node)
            except Exception as exc:  # noqa: BLE001 -- a failed query falls back to the tree walk
                note_query_fallback(cs.PY_ASSIGNMENT_QUERY, exc)
            else:
                for assign_node in captures.get("assignment", []):
                    self._process_self_assignment(
                        assign_node, local_var_types, module_qn
                    )
                return
        stack: list[ASTNode] = [node]
        while stack:
            current = stack.pop()
            if current.type == cs.TS_PY_ASSIGNMENT:
                self._process_self_assignment(current, local_var_types, module_qn)
            stack.extend(reversed(current.children))

    def _self_method_element_type(
        self, call_node: ASTNode, module_qn: str
    ) -> str | None:
        """The return type of a ``self.m()`` call, resolved on the enclosing
        class.

        Deliberately scoped to iterable position: a general ``self`` receiver
        must stay override-aware (an abstract stub's concrete sibling wins),
        but an iterated call only needs the visible method's container
        annotation.
        """
        func_node = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func_node is None or func_node.type != cs.TS_PY_ATTRIBUTE:
            return None
        text = safe_decode_text(func_node)
        if not text or not text.startswith(cs.PY_SELF_PREFIX):
            return None
        parts = text.split(cs.SEPARATOR_DOT)
        if len(parts) != 2:
            return None
        if (class_node := _enclosing_class_node(call_node)) is None:
            return None
        name_node = class_node.child_by_field_name(cs.FIELD_NAME)
        if name_node is None or not (class_name := safe_decode_text(name_node)):
            return None
        class_qn = (
            self.expression_analyzer.find_class_in_scope(class_name, module_qn)
            or class_name
        )
        if cs.SEPARATOR_DOT not in class_qn:
            # A same-module class resolves to its bare name; the method lookup
            # needs the full module.Class.method shape.
            class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_qn}"
        return self.expression_analyzer.get_method_return_type_from_ast(
            f"{class_qn}{cs.SEPARATOR_DOT}{parts[1]}"
        )

    def _infer_instance_attributes_from_init(
        self, caller_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        # Instance attributes are assigned in __init__ (self.x = T()), so a method
        # that only reads self.x has no local assignment to infer from. Scan the
        # enclosing class's __init__ and seed the attribute types, letting a
        # reassignment in the calling method win (setdefault).
        if (class_node := _enclosing_class_node(caller_node)) is None:
            return
        init_node = _find_init_method_node(class_node)
        if init_node is None or init_node is caller_node:
            return
        init_types: dict[str, str] = {}
        # Seed __init__ parameter types first so self.x = param flows the
        # annotation onto the attribute.
        self._infer_parameter_types(init_node, init_types, module_qn)
        self.analyze_self_assignments(init_node, init_types, module_qn)
        for attr, attr_type in init_types.items():
            if attr.startswith(cs.PY_SELF_PREFIX):
                local_var_types.setdefault(attr, attr_type)

    def _expand_chained_attribute_types(
        self,
        local_var_types: dict[str, str],
        module_qn: str,
        aliases: dict[str, _Alias] | None = None,
        max_depth: int = 4,
    ) -> None:
        # A chained reference a.b.c needs the type of a.b (member b on a's class).
        # Each pass: (1) propagate local aliases (x = ref) from the referent's type,
        # (2) for every typed ref, seed ref.member -> member type (full QN), so
        # deeper chains and aliases resolve next pass until a fixpoint.
        aliases = aliases or {}
        for _ in range(max_depth):
            # Both passes run every round; stop at the fixpoint.
            aliased = self._propagate_alias_types(aliases, local_var_types, module_qn)
            seeded = self._seed_member_types(local_var_types, module_qn)
            if not (aliased or seeded):
                break

    def _propagate_alias_types(
        self,
        aliases: dict[str, _Alias],
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> bool:
        added = False
        for local, alias in aliases.items():
            if local not in local_var_types and (
                alias_type := self.get_alias_type(alias, local_var_types, module_qn)
            ):
                local_var_types[local] = alias_type
                added = True
        return added

    def _seed_member_types(
        self, local_var_types: dict[str, str], module_qn: str
    ) -> bool:
        added = False
        for ref, type_name in list(local_var_types.items()):
            class_qn = self._class_qn_of_type(type_name, module_qn)
            if not class_qn:
                continue
            for member, member_type in self._class_member_types_by_qn(class_qn).items():
                key = f"{ref}{cs.SEPARATOR_DOT}{member}"
                if key not in local_var_types:
                    local_var_types[key] = member_type
                    added = True
        return added

    def get_alias_type(
        self, alias: _Alias, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        """The type an alias gives the variable, after any operator.

        Candidates of different types -- `widget if flag else engine` -- become
        the union an annotation would spell (`Widget | Engine`), so the resolver
        applies the one policy it already has for a genuine multi-type union
        (`_strip_optional`: it "stays unresolved"). Typing only the first branch
        drops the other's call, and leaving the receiver untyped hands it to the
        bare-name fallback, which picks one of the two arbitrarily. Only `| None`
        is ignored when comparing: an Optional and its bare type are the same
        receiver.
        """
        left = self._get_candidate_types(alias.candidates, local_var_types, module_qn)
        if alias.dunder is None:
            return left
        right = self._get_candidate_types(alias.reflected, local_var_types, module_qn)
        return self._get_operator_result_type(left, alias.dunder, right, module_qn)

    def _get_candidate_types(
        self,
        candidates: list[ASTNode],
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> str | None:
        found: dict[frozenset[str], str] = {}
        for node in candidates:
            if node.type == cs.TS_PY_CALL:
                inferred = self.expression_analyzer.infer_type_from_expression(
                    node, module_qn, node
                )
            else:
                inferred = local_var_types.get(safe_decode_text(node) or "")
            if inferred:
                found.setdefault(_non_none_members(inferred), inferred)
        if not found:
            return None
        return f" {cs.PY_UNION_SEPARATOR} ".join(found.values())

    def _get_operator_result_type(
        self, left: str | None, dunder: str, right: str | None, module_qn: str
    ) -> str | None:
        """What `left <op> right` evaluates to, following Python's dispatch.

        Each member of the left type is tried first: a project class answers
        with the return type of the operator method found anywhere up its
        bases (`Sub / cfg` where `Base.__truediv__ -> Product` is a Product);
        a type outside the project -- `pathlib.Path`, `str` -- keeps its own
        type, which is right for `Path / "sub"`. A right operand whose class
        is a strict subclass of the left's and provides its OWN reflected
        method goes before the left's forward one. A project class that
        defines the method nowhere hands the operation to the right operand's
        reflected method, exactly as the interpreter would; if that answers
        nothing either the receiver stays untyped rather than guessing.
        """
        results: dict[str, None] = {}
        left_members = _union_members(left) if left else []
        right_classes = self._get_project_classes(right, module_qn)
        forward_missing = not left_members
        for member in left_members:
            class_qn = self._get_project_class_qn(member, module_qn)
            if class_qn is None:
                results.setdefault(member, None)
                continue
            lookup, missing = self._get_forward_result(class_qn, right_classes, dunder)
            forward_missing = forward_missing or missing
            if lookup.returned:
                results.setdefault(lookup.returned, None)
        if forward_missing:
            for returned in self._get_reflected_results(right_classes, dunder):
                results.setdefault(returned, None)
        if not results:
            return None
        return f" {cs.PY_UNION_SEPARATOR} ".join(results)

    def _get_project_classes(self, union: str | None, module_qn: str) -> list[str]:
        return [
            qn
            for member in (_union_members(union) if union else [])
            if (qn := self._get_project_class_qn(member, module_qn)) is not None
        ]

    def _get_forward_result(
        self, class_qn: str, right_classes: list[str], dunder: str
    ) -> tuple[_DunderLookup, bool]:
        """(the lookup that decides for this left class, is the forward method missing)."""
        priority = self._get_subclass_reflected_type(class_qn, right_classes, dunder)
        if priority is not None:
            return priority, False
        lookup = self._get_dunder_return_type(class_qn, dunder)
        return lookup, lookup.owner is None

    def _get_reflected_results(
        self, right_classes: list[str], dunder: str
    ) -> list[str]:
        reflected = _reflected_dunder(dunder)
        results: list[str] = []
        for right_qn in right_classes:
            returned = self._get_dunder_return_type(right_qn, reflected).returned
            if returned:
                results.append(returned)
        return results

    def _get_project_class_qn(self, type_name: str, module_qn: str) -> str | None:
        """The registry qn of a class THIS project defines, else None.

        `_class_qn_of_type` also answers for an imported external class
        (`pathlib.Path`), spelled as a dotted name that no AST backs. Treating
        that as a project class whose operator is "missing" would hand
        `Path / "sub"` to reflected dispatch and then leave it untyped; the
        registry is what separates a class we can read from one we cannot.
        """
        class_qn = self._class_qn_of_type(type_name, module_qn)
        if not class_qn or self.function_registry.get(class_qn) != cs.NodeLabel.CLASS:
            return None
        return class_qn

    def _get_subclass_reflected_type(
        self, left_qn: str, right_classes: list[str], dunder: str
    ) -> _DunderLookup | None:
        """The reflected method of a right operand that subclasses the left.

        Python gives it priority only when the subclass PROVIDES the reflected
        method -- a distinct implementation, not one inherited from the left's
        own class. `Base` defining both `__truediv__ -> First` and
        `__rtruediv__ -> Second` with `Derived(Base)` defining neither runs
        the forward method for `base / derived` and yields First; the
        inherited `__rtruediv__` is Base's, not Derived's. Returns None when
        no right class qualifies, so the caller falls through to the forward
        method.
        """
        reflected = _reflected_dunder(dunder)
        left_owner = self._get_dunder_return_type(left_qn, reflected).owner
        for right_qn in right_classes:
            if right_qn == left_qn or left_qn not in self.get_mro(right_qn):
                continue
            lookup = self._get_dunder_return_type(right_qn, reflected)
            if lookup.owner is not None and lookup.owner != left_owner:
                return lookup
        return None

    def _get_dunder_return_type(self, class_qn: str, dunder: str) -> _DunderLookup:
        """The first class up the bases that defines `dunder`, and its return type."""
        for cls in self.get_mro(class_qn):
            method_qn = f"{cls}{cs.SEPARATOR_DOT}{dunder}"
            if self.ast_analyzer.find_method_ast_node(method_qn) is not None:
                return _DunderLookup(
                    cls,
                    self.expression_analyzer.get_method_return_type_from_ast(method_qn),
                )
        return _DunderLookup(None, None)

    def get_mro(self, class_qn: str) -> list[str]:
        """Python's method resolution order for a project class.

        C3 linearisation over `class_inheritance`, which keeps each class's
        bases in declaration order, so `Child(A, B)` with `A(X)` resolves
        `Child, A, X, B` -- the order the interpreter uses -- rather than the
        breadth-first `Child, A, B, X` that put a base's operator ahead of a
        grandparent's. A hierarchy C3 rejects (inconsistent bases, a cycle, or
        one it cannot see all of) falls back to breadth-first, which is what
        CallResolver._mro still uses; a wrong order there costs an operator's
        result type, never a crash.
        """
        return self._get_c3_mro(class_qn, ()) or self._get_bfs_mro(class_qn)

    def _get_c3_mro(self, class_qn: str, stack: tuple[str, ...]) -> list[str] | None:
        if class_qn in stack or len(stack) > _MAX_ALIAS_DEPTH:
            return None
        bases = list(self.facts.class_inheritance.get(class_qn, []))
        sequences: list[list[str]] = []
        for base in bases:
            linear = self._get_c3_mro(base, (*stack, class_qn))
            if linear is None:
                return None
            sequences.append(linear)
        sequences.append(bases)
        order = [class_qn]
        while any(sequences):
            sequences = [seq for seq in sequences if seq]
            head = next(
                (
                    seq[0]
                    for seq in sequences
                    if not any(seq[0] in other[1:] for other in sequences)
                ),
                None,
            )
            if head is None:
                return None
            order.append(head)
            for seq in sequences:
                if seq[0] == head:
                    seq.pop(0)
        return order

    def _get_bfs_mro(self, class_qn: str) -> list[str]:
        seen: set[str] = set()
        order: list[str] = []
        queue = [class_qn]
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            order.append(cur)
            queue.extend(self.facts.class_inheritance.get(cur, []))
        return order

    def _class_qn_of_type(self, type_name: str, module_qn: str) -> str | None:
        if cs.SEPARATOR_DOT in type_name:
            return type_name
        return resolve_class_name(
            type_name, module_qn, self.import_processor, self.function_registry
        )

    def _class_member_types_by_qn(self, class_qn: str) -> dict[str, str]:
        if class_qn in self._class_member_type_cache:
            return self._class_member_type_cache[class_qn]
        members: dict[str, str] = {}
        class_node = self.ast_analyzer.find_class_node(class_qn)
        if class_node is not None:
            class_module_qn = class_qn.rpartition(cs.SEPARATOR_DOT)[0]
            raw: dict[str, str] = {}
            _collect_property_return_types(class_node, raw)
            _collect_class_annotation_types(class_node, raw)
            if (init_node := _find_init_method_node(class_node)) is not None:
                init_types: dict[str, str] = {}
                self._infer_parameter_types(init_node, init_types, class_module_qn)
                self.analyze_self_assignments(init_node, init_types, class_module_qn)
                for attr, attr_type in init_types.items():
                    raw.setdefault(attr, attr_type)
            for attr, attr_type in raw.items():
                if not attr.startswith(cs.PY_SELF_PREFIX):
                    continue
                member = attr[len(cs.PY_SELF_PREFIX) :]
                resolved = self._class_qn_of_type(attr_type, class_module_qn)
                members[member] = resolved or attr_type
        self._class_member_type_cache[class_qn] = members
        return members

    def _infer_variable_element_type(
        self, var_name: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        if (
            var_name in local_var_types
            and (var_type := local_var_types[var_name])
            and var_type != cs.TYPE_INFERENCE_LIST
        ):
            # A container-marked variable (`widgets = load_widgets()` with a
            # `-> list[Widget]` annotation) iterates as its element type.
            return _container_element_type(var_type) or var_type
        return self._infer_method_return_element_type(var_name, module_qn)

    def _infer_method_return_element_type(
        self, var_name: str, module_qn: str
    ) -> str | None:
        if cs.PY_VAR_PATTERN_ALL in var_name or var_name.endswith(
            cs.PY_VAR_SUFFIX_PLURAL
        ):
            return self._analyze_repository_item_type(module_qn)
        return None

    def _analyze_repository_item_type(self, module_qn: str) -> str | None:
        repo_qn_patterns = [
            f"{module_qn.split(cs.SEPARATOR_DOT, maxsplit=1)[0]}{cs.PY_MODELS_BASE_PATH}{cs.PY_CLASS_REPOSITORY}",
            cs.PY_CLASS_REPOSITORY,
        ]

        for repo_qn in repo_qn_patterns:
            create_method = f"{repo_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_CREATE}"
            if create_method in self.function_registry:
                return cs.TYPE_INFERENCE_BASE_MODEL

        return None
