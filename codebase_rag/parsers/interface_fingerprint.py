"""The interface fingerprint of a source file (issue #2531).

A sync re-parses an edited file and, until this fingerprint, every file that
depends on it, because a change there can move what a caller binds to. Most
edits move nothing a caller can see: a comment, a reformatted line, a new
statement inside a function. The fingerprint hashes what callers CAN see, so
the sync can tell those edits apart and leave the callers' edges as they are.

What a caller can see of a file is its definitions (names, kinds,
parameters, annotations, return types, bases, decorators), every statement
outside a function body (imports, re-exports, `__all__`, constants), and the
parts of function bodies that cgr's cross-file readers look into:

- a nested definition, which has a qualified name of its own;
- in Python, what a function returns: return-type inference reads a returned
  call or attribute, and reads the WHOLE body when a local name is returned;
  the returned-callable reader resolves a returned member (`return r.run`)
  for every caller that binds and invokes it;
- an assignment to an attribute or subscript (`self.x = Widget()` types
  `obj.x` for every caller), a `global`, an import, a generator;
- a call of one of the function's own parameters, which sends every callable
  a caller passes in through the callable-flow pass;
- a call that names something as an argument, which hands that function on
  to other files: a constructor stores it in a field that `spec.handler()`
  invokes in any file (Python's callable-field pre-pass), a function
  forwards it to one that calls it (the callable-flow pass). What the call
  calls, each such argument with its keyword or its position (counted as
  those readers count, a comment between arguments included), and whether
  the name is the body's own local are kept;
- in JS/TS, any assignment (prototype and `module.exports` members), any
  value return (return-type inference again), and a symbol-keyed
  installation (`{ [kX]: value }`), which types `recv[kX]` in every file
  through whatever the body declared its value as.

Each of those readers resolves names, and a name the body binds itself (an
assignment, a loop, `with` or `except` target, a walrus, a comprehension or
a nested scope) is typed from the body: `r = A(); return r.run` hands back
`A.run` or `B.run` as `r` is bound. So when what a reader resolves -- a
returned call's callee, a returned member, the callee of a call handing a
name on, a member it hands on -- names anything the body binds, the body is
hashed whole. The one exception is a bare name handed on as an argument:
the readers resolve it by its text, or to nothing when it is the caller's
local, and both of those are hashed.

A function body holding none of the whole-body readers is replaced by what
it defines, its calls' named arguments and, in Python, the shape of what it
returns. Everything else in the body, and every comment anywhere, is left
out. A body holding any of them is hashed whole. Positions are never hashed,
so code that only moves keeps its fingerprint; a qualified name minted from
a position (`f@12`, `anonymous_3_4`) is the caller's to distrust, since only
the graph knows which names those are.

One reader writes edges no fingerprint can vouch for: when a function here
invokes a callable another file passes it, the flow edge to that callable
starts here but is written by the passer's parse, so the sync re-parses
every dependent of such a file whatever its fingerprint says
(`interface_skip.without_flow_sources`).

A language without rules here has no fingerprint, and its callers are always
re-parsed, as before.

This module lives under `parsers/` on purpose: the parser fingerprint hashes
every file here, so a change to these rules re-indexes existing graphs rather
than comparing fingerprints of two generations.

The tree bridge computes the same digest in Rust (`native_trees`), from the
rules `bridge_rules` hands it; `python_interface_fingerprint` stays the
reference and runs whenever the bridge cannot.
"""

from __future__ import annotations

import hashlib
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..language_spec import LANGUAGE_SPECS
from ..types_defs import CallableScanRules, InterfaceLanguageRules, InterfaceRules
from . import native_trees
from .py.ast_analyzer import local_names, names_bound_within
from .utils import callable_parameter_indices, safe_decode_text


class _Rules(NamedTuple):
    functions: frozenset[str]
    blocks: frozenset[str]
    definitions: frozenset[str]
    blockers: frozenset[str]
    kept_names: frozenset[str]
    # Calls whose named arguments a cross-file binding reads.
    calls: frozenset[str]
    # Returns whose value return-type inference and the returned-callable
    # reader resolve (a JS/TS value return already keeps its body whole).
    returns: frozenset[str]


def _definitions(
    language: cs.SupportedLanguage, extra: frozenset[str] = frozenset()
) -> frozenset[str]:
    spec = LANGUAGE_SPECS[language]
    return (
        frozenset(spec.function_node_types) | frozenset(spec.class_node_types) | extra
    )


def _js_rules(language: cs.SupportedLanguage) -> _Rules:
    return _Rules(
        functions=cs.INTERFACE_FP_JS_FUNCTIONS,
        blocks=cs.INTERFACE_FP_JS_BLOCKS,
        definitions=_definitions(language),
        blockers=cs.INTERFACE_FP_JS_BLOCKERS,
        kept_names=cs.INTERFACE_FP_JS_KEPT_FUNCTIONS,
        calls=cs.INTERFACE_FP_JS_CALLS,
        returns=frozenset(),
    )


_RULES: dict[cs.SupportedLanguage, _Rules] = {
    cs.SupportedLanguage.PYTHON: _Rules(
        functions=cs.INTERFACE_FP_PY_FUNCTIONS,
        blocks=cs.INTERFACE_FP_PY_BLOCKS,
        definitions=_definitions(cs.SupportedLanguage.PYTHON),
        blockers=cs.INTERFACE_FP_PY_BLOCKERS,
        kept_names=frozenset(),
        calls=cs.INTERFACE_FP_PY_CALLS,
        returns=cs.INTERFACE_FP_PY_RETURNS,
    ),
    cs.SupportedLanguage.JAVA: _Rules(
        functions=cs.INTERFACE_FP_JAVA_FUNCTIONS,
        blocks=cs.INTERFACE_FP_JAVA_BLOCKS,
        definitions=_definitions(
            cs.SupportedLanguage.JAVA, cs.INTERFACE_FP_JAVA_EXTRA_DEFINITIONS
        ),
        blockers=frozenset(),
        kept_names=frozenset(),
        calls=frozenset(),
        returns=frozenset(),
    ),
    cs.SupportedLanguage.JS: _js_rules(cs.SupportedLanguage.JS),
    cs.SupportedLanguage.TS: _js_rules(cs.SupportedLanguage.TS),
    cs.SupportedLanguage.TSX: _js_rules(cs.SupportedLanguage.TSX),
}

INTERFACE_LANGUAGES = frozenset(_RULES)

# The returned expressions Python return-type inference reads; a returned
# local name (an identifier other than `self` / `cls`) keeps the whole body.
_PY_READ_RETURN_TYPES = frozenset(
    {cs.TS_PY_CALL, cs.TS_PY_ATTRIBUTE, cs.TS_PY_IDENTIFIER}
)

_Item = Node | bytes


def interface_fingerprint(
    root_node: Node, language: cs.SupportedLanguage
) -> str | None:
    """The hex digest of `root_node`'s interface, or None for a language
    without rules; in Rust when the tree bridge can (issue #3473)."""
    if language not in _RULES:
        return None
    if (bridge := native_trees.interface_bridge()) is not None:
        try:
            return bridge.interface_fingerprint(root_node, language)
        except native_trees.NotBorrowed:
            pass
    return python_interface_fingerprint(root_node, language)


def python_interface_fingerprint(
    root_node: Node, language: cs.SupportedLanguage
) -> str | None:
    """`interface_fingerprint` as Python computes it: what runs without the
    bridge, and the reference `TreeBridge.interface_fingerprint` must match."""
    rules = _RULES.get(language)
    if rules is None:
        return None
    digest = hashlib.sha256(cs.INTERFACE_FINGERPRINT_VERSION.encode())
    # Iterative: a deep tree would overflow Python recursion.
    stack: list[_Item] = [root_node]
    while stack:
        item = stack.pop()
        if isinstance(item, bytes):
            digest.update(item)
        elif not _is_comment(item):
            stack.extend(reversed(_expand(item, rules, language)))
    return digest.hexdigest()


def _token(kind: bytes, text: bytes) -> bytes:
    # Length-prefixed, so no leaf text can imitate a token boundary.
    return (
        kind
        + len(text).to_bytes(cs.INTERFACE_FP_LENGTH_BYTES, cs.INTERFACE_FP_BYTE_ORDER)
        + text
    )


def _is_comment(node: Node) -> bool:
    return cs.INTERFACE_FP_COMMENT_SUBSTRING in node.type


def _expand(node: Node, rules: _Rules, language: cs.SupportedLanguage) -> list[_Item]:
    """What `node` contributes, in order: a token, or an opening token, its
    children (its body swapped for a projection when the body is elided) and
    a closing token."""
    node_type = node.type.encode()
    if node.child_count == 0:
        kind = cs.INTERFACE_FP_MISSING if node.is_missing else cs.INTERFACE_FP_LEAF
        return [_token(kind, node_type), _token(kind, node.text or b"")]
    elided = _elidable_body(node, rules, language)
    items: list[_Item] = [_token(cs.INTERFACE_FP_OPEN, node_type)]
    for child in node.children:
        if elided is not None and child.id == elided.id:
            items.extend(_projection(child, rules, language))
        else:
            items.append(child)
    items.append(cs.INTERFACE_FP_CLOSE)
    return items


def _elidable_body(
    node: Node, rules: _Rules, language: cs.SupportedLanguage
) -> Node | None:
    """The body of a function no cross-file reader looks into, or None."""
    if node.type not in rules.functions:
        return None
    body = node.child_by_field_name(cs.FIELD_BODY)
    if body is None or body.type not in rules.blocks:
        return None
    name = safe_decode_text(node.child_by_field_name(cs.FIELD_NAME)) or ""
    if name in rules.kept_names or _is_python_dunder(name, language):
        return None
    if callable_parameter_indices(node, language) or _body_is_read(
        body, rules, language
    ):
        return None
    return body


def _is_python_dunder(name: str, language: cs.SupportedLanguage) -> bool:
    return (
        language == cs.SupportedLanguage.PYTHON
        and name.startswith(cs.INTERFACE_FP_DUNDER)
        and name.endswith(cs.INTERFACE_FP_DUNDER)
    )


def _body_is_read(body: Node, rules: _Rules, language: cs.SupportedLanguage) -> bool:
    # Nested definitions are searched too: Python's return-type inference
    # collects a nested function's returns with the enclosing function's,
    # and reads the ENCLOSING body's locals when one returns a name.
    stack = list(body.children)
    readers: list[Node] = []
    while stack:
        node = stack.pop()
        if node.type in rules.blockers or _is_read(node, language):
            return True
        if node.type in rules.calls or node.type in rules.returns:
            readers.append(node)
        stack.extend(node.children)
    return _resolves_through_a_local(body, readers, language)


def _resolves_through_a_local(
    body: Node, readers: list[Node], language: cs.SupportedLanguage
) -> bool:
    """Whether an expression a cross-file reader resolves names anything
    `body` binds: `r = A(); return r.run` hands back whichever class `r`
    holds, `r.relay(a)` hands `a` to it, and only the whole body says which
    class that is. A bare name handed on is the exception: a reader resolves
    it to a definition by its text, or to nothing when it is a local, and
    both are hashed (`_call_bindings`)."""
    references = [ref for node in readers for ref in _references(node)]
    if not references:
        return False
    bound = _names_bound_within(body, language)
    return any(not bound.isdisjoint(_names_in(ref)) for ref in references)


def _references(node: Node) -> list[Node]:
    """What a reader resolves in `node`: a returned call's callee or a
    returned member; or, for a call handing names on, its callee and each
    member it hands on."""
    if node.type == cs.TS_PY_RETURN_STATEMENT:
        value = _returned_value(node)
        if value is not None and value.type == cs.TS_PY_CALL:
            callee = _callee(value)
            return [callee] if callee is not None else []
        return [value] if value is not None and value.type == cs.TS_PY_ATTRIBUTE else []
    arguments = _named_arguments(node)
    if not arguments:
        return []
    references = [value for _key, value in arguments if value.type != cs.TS_IDENTIFIER]
    callee = _callee(node)
    return [*references, callee] if callee is not None else references


def _names_in(node: Node) -> set[str]:
    names: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_IDENTIFIER and (name := safe_decode_text(current)):
            names.add(name)
        stack.extend(current.children)
    return names


def _names_bound_within(body: Node, language: cs.SupportedLanguage) -> frozenset[str]:
    if language == cs.SupportedLanguage.PYTHON:
        return names_bound_within(body)
    names: set[str] = set()
    stack = [body]
    while stack:
        node = stack.pop()
        for field in cs.INTERFACE_FP_JS_BINDER_FIELDS.get(node.type, ()):
            if (target := node.child_by_field_name(field)) is not None:
                names.update(_js_bound_names(target))
        stack.extend(node.children)
    return frozenset(names)


def _js_bound_names(target: Node) -> set[str]:
    # Every name inside a binding pattern; a default value or a type that
    # names something only makes the set larger, which keeps more bodies.
    names: set[str] = set()
    stack = [target]
    while stack:
        node = stack.pop()
        if node.type in cs.INTERFACE_FP_JS_BOUND_NAMES and (
            name := safe_decode_text(node)
        ):
            names.add(name)
        stack.extend(node.children)
    return names


def _callee(call: Node) -> Node | None:
    return next(
        (
            callee
            for field in cs.INTERFACE_FP_CALLEE_FIELDS
            if (callee := call.child_by_field_name(field)) is not None
        ),
        None,
    )


def _named_arguments(call: Node) -> list[tuple[str, Node]]:
    """(position or keyword, value) of each argument of `call` that names
    something, numbered as `_split_call_arguments` numbers them: every
    non-keyword child of the argument list counts, a comment included."""
    arguments = call.child_by_field_name(cs.FIELD_ARGUMENTS)
    if arguments is None:
        return []
    named: list[tuple[str, Node]] = []
    position = 0
    for child in arguments.named_children:
        if child.type == cs.TS_PY_KEYWORD_ARGUMENT:
            key = safe_decode_text(child.child_by_field_name(cs.FIELD_NAME))
            value = child.child_by_field_name(cs.FIELD_VALUE)
        else:
            key, value = str(position), child
            position += 1
        if key is not None and value is not None:
            value = _unwrapped_reference(value)
            if value.type in cs.INTERFACE_FP_NAME_ARGUMENTS:
                named.append((key, value))
    return named


def _unwrapped_reference(node: Node) -> Node:
    # The callable-flow pass reads `a` through `a as any` and through
    # `a.bind(this)` (or `.call` / `.apply`), to a fixpoint.
    while True:
        while node.type in cs.TS_CAST_WRAPPER_TYPES and node.named_child_count:
            node = node.named_children[0]
        bound = _bound_function(node)
        if bound is None:
            return node
        node = bound


def _bound_function(node: Node) -> Node | None:
    if node.type != cs.TS_CALL_EXPRESSION:
        return None
    member = node.child_by_field_name(cs.FIELD_FUNCTION)
    if member is None or member.type != cs.TS_MEMBER_EXPRESSION:
        return None
    method = safe_decode_text(member.child_by_field_name(cs.FIELD_PROPERTY))
    if method not in cs.JS_FUNCTION_PROTOTYPE_METHODS:
        return None
    return member.child_by_field_name(cs.FIELD_OBJECT)


def _is_read(node: Node, language: cs.SupportedLanguage) -> bool:
    match language:
        case cs.SupportedLanguage.PYTHON:
            return _python_is_read(node)
        case (
            cs.SupportedLanguage.JS | cs.SupportedLanguage.TS | cs.SupportedLanguage.TSX
        ):
            return _js_is_read(node)
        case _:
            return False


def _python_is_read(node: Node) -> bool:
    if node.type in cs.INTERFACE_FP_PY_TARGET_HOLDERS:
        target = node.child_by_field_name(cs.FIELD_LEFT)
        return target is not None and _holds_member(target)
    if node.type in cs.INTERFACE_FP_PY_TARGET_WRAPPERS:
        return _holds_member(node)
    if node.type == cs.TS_PY_RETURN_STATEMENT:
        value = _returned_value(node)
        # A returned local is typed from every binding in the body.
        return (
            value is not None
            and value.type == cs.TS_PY_IDENTIFIER
            and safe_decode_text(value) not in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS)
        )
    return False


def _js_is_read(node: Node) -> bool:
    match node.type:
        case cs.TS_RETURN_STATEMENT:
            return _returned_value(node) is not None
        case cs.TS_CALL_EXPRESSION:
            callee = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
            return (
                callee is not None
                and callee.type == cs.TS_IDENTIFIER
                and safe_decode_text(callee) == cs.JS_REQUIRE_KEYWORD
            )
        case cs.TS_PAIR:
            return _installs_under_a_symbol(node)
        case _:
            return False


def _installs_under_a_symbol(pair: Node) -> bool:
    # `{ [kX]: value }`: the symbol pre-pass records the class of `value`,
    # chased through the body's declarations, under the symbol `kX`.
    key = pair.child_by_field_name(cs.FIELD_KEY)
    if key is None or key.type != cs.TS_JS_COMPUTED_PROPERTY_NAME:
        return False
    index = next((c for c in key.named_children if not _is_comment(c)), None)
    return index is not None and index.type == cs.TS_IDENTIFIER


def _holds_member(target: Node) -> bool:
    stack = [target]
    while stack:
        node = stack.pop()
        if node.type in cs.INTERFACE_FP_PY_MEMBER_TARGETS:
            return True
        stack.extend(node.children)
    return False


def _returned_value(return_node: Node) -> Node | None:
    return next(
        (child for child in return_node.named_children if not _is_comment(child)),
        None,
    )


def _projection(
    body: Node, rules: _Rules, language: cs.SupportedLanguage
) -> list[_Item]:
    """What an elided body still contributes: each statement that holds a
    definition, whole, and in Python the shape of each return."""
    items: list[_Item] = [cs.INTERFACE_FP_ELIDED_BODY]
    for statement in body.named_children:
        if _is_comment(statement):
            continue
        if _holds_definition(statement, rules):
            items.append(statement)
        elif language == cs.SupportedLanguage.PYTHON:
            items.extend(_python_returns(statement))
    items.extend(_call_bindings(body, rules, language))
    return items


def _call_bindings(
    body: Node, rules: _Rules, language: cs.SupportedLanguage
) -> list[bytes]:
    """Each call in `body` that names an argument: what it calls, and each
    such argument's position or keyword and text. A bare name the caller
    binds itself is the local's, never the function it shadows, so whether
    it is one is kept too (Python; a JS local the flow pass reads through
    is a nested definition, kept whole)."""
    items: list[bytes] = []
    if not rules.calls:
        return items
    scopes: dict[int, frozenset[str]] = {}
    stack = list(reversed(body.children))
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.children))
        if node.type not in rules.calls or not (arguments := _named_arguments(node)):
            continue
        callee = _callee(node)
        items.append(_token(cs.INTERFACE_FP_CALL, _text(callee) or node.type.encode()))
        own = _caller_locals(node, language, scopes)
        for key, value in arguments:
            items.append(_token(cs.INTERFACE_FP_ARGUMENT, key.encode()))
            items.append(_token(cs.INTERFACE_FP_ARGUMENT, _text(value)))
            if value.type == cs.TS_IDENTIFIER and safe_decode_text(value) in own:
                items.append(cs.INTERFACE_FP_LOCAL)
    return items


def _text(node: Node | None) -> bytes:
    return (node.text or b"") if node is not None else b""


def _caller_locals(
    call: Node, language: cs.SupportedLanguage, scopes: dict[int, frozenset[str]]
) -> frozenset[str]:
    # The names the resolver reads as the calling def's own. A body-level
    # import is a blocker, so an elided body has none for the import map to
    # tell from a shadow, and an empty map answers as the module's would.
    if language != cs.SupportedLanguage.PYTHON:
        return frozenset()
    scope = call.parent
    while scope is not None and scope.type != cs.INTERFACE_FP_PY_CALLER_SCOPE:
        scope = scope.parent
    if scope is None:
        return frozenset()
    if scope.id not in scopes:
        scopes[scope.id] = local_names(scope, {})
    return scopes[scope.id]


def _holds_definition(statement: Node, rules: _Rules) -> bool:
    stack = [statement]
    while stack:
        node = stack.pop()
        if node.type in rules.definitions:
            return True
        stack.extend(node.children)
    return False


def _python_returns(statement: Node) -> list[_Item]:
    # Return-type inference reads a returned call or attribute (and `self` /
    # `cls`); any other returned expression infers nothing, so only its
    # node type is kept.
    items: list[_Item] = []
    stack = [statement]
    while stack:
        node = stack.pop()
        if node.type != cs.TS_PY_RETURN_STATEMENT:
            stack.extend(reversed(node.children))
            continue
        value = _returned_value(node)
        if value is None:
            items.append(cs.INTERFACE_FP_RETURN)
        elif value.type in _PY_READ_RETURN_TYPES:
            items.extend((cs.INTERFACE_FP_RETURN, value))
        else:
            items.append(_token(cs.INTERFACE_FP_RETURN, value.type.encode()))
    return items


def bridge_rules() -> InterfaceRules:
    """The constants this module and the callable-parameter scan it asks
    (`callable_parameter_indices`) read, as the tree bridge's `interface`
    rules, so the two fingerprints cannot drift apart on them."""
    return InterfaceRules(
        languages={
            language.value: _bridge_language_rules(language, rules)
            for language, rules in _RULES.items()
        },
        version=cs.INTERFACE_FINGERPRINT_VERSION,
        open=cs.INTERFACE_FP_OPEN,
        close=cs.INTERFACE_FP_CLOSE,
        leaf=cs.INTERFACE_FP_LEAF,
        missing=cs.INTERFACE_FP_MISSING,
        elided_body=cs.INTERFACE_FP_ELIDED_BODY,
        returned=cs.INTERFACE_FP_RETURN,
        call=cs.INTERFACE_FP_CALL,
        argument=cs.INTERFACE_FP_ARGUMENT,
        local=cs.INTERFACE_FP_LOCAL,
        length_bytes=cs.INTERFACE_FP_LENGTH_BYTES,
        byte_order=cs.INTERFACE_FP_BYTE_ORDER,
        comment=cs.INTERFACE_FP_COMMENT_SUBSTRING,
        dunder=cs.INTERFACE_FP_DUNDER,
        body_field=cs.FIELD_BODY,
        name_field=cs.FIELD_NAME,
        left_field=cs.FIELD_LEFT,
        arguments_field=cs.FIELD_ARGUMENTS,
        value_field=cs.FIELD_VALUE,
        key_field=cs.FIELD_KEY,
        function_field=cs.FIELD_FUNCTION,
        property_field=cs.FIELD_PROPERTY,
        object_field=cs.FIELD_OBJECT,
        parameters_field=cs.FIELD_PARAMETERS,
        parameter_field=cs.TS_FIELD_PARAMETER,
        pattern_field=cs.TS_FIELD_PATTERN,
        alias_field=cs.FIELD_ALIAS,
        callee_fields=list(cs.INTERFACE_FP_CALLEE_FIELDS),
        identifier=cs.TS_IDENTIFIER,
        keyword_argument=cs.TS_PY_KEYWORD_ARGUMENT,
        name_arguments=sorted(cs.INTERFACE_FP_NAME_ARGUMENTS),
        cast_wrappers=sorted(cs.TS_CAST_WRAPPER_TYPES),
        call_expression=cs.TS_CALL_EXPRESSION,
        member_expression=cs.TS_MEMBER_EXPRESSION,
        prototype_methods=sorted(cs.JS_FUNCTION_PROTOTYPE_METHODS),
        js_binder_fields={
            node_type: list(fields)
            for node_type, fields in cs.INTERFACE_FP_JS_BINDER_FIELDS.items()
        },
        js_bound_names=sorted(cs.INTERFACE_FP_JS_BOUND_NAMES),
        js_return=cs.TS_RETURN_STATEMENT,
        js_pair=cs.TS_PAIR,
        js_computed_property_name=cs.TS_JS_COMPUTED_PROPERTY_NAME,
        js_require=cs.JS_REQUIRE_KEYWORD,
        js_typed_parameters=sorted(cs.JS_TS_TYPED_PARAMETER_TYPES),
        py_identifier=cs.TS_PY_IDENTIFIER,
        py_call=cs.TS_PY_CALL,
        py_attribute=cs.TS_PY_ATTRIBUTE,
        py_return=cs.TS_PY_RETURN_STATEMENT,
        py_read_returns=sorted(_PY_READ_RETURN_TYPES),
        receivers=[cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS],
        py_target_holders=sorted(cs.INTERFACE_FP_PY_TARGET_HOLDERS),
        py_target_wrappers=sorted(cs.INTERFACE_FP_PY_TARGET_WRAPPERS),
        py_member_targets=sorted(cs.INTERFACE_FP_PY_MEMBER_TARGETS),
        py_caller_scope=cs.INTERFACE_FP_PY_CALLER_SCOPE,
        py_named_parameters=sorted(cs.PY_NAMED_PARAMETER_TYPES),
        py_typed_parameter=cs.TS_PY_TYPED_PARAMETER,
        py_scope_boundaries=sorted(cs.PY_SCOPE_BOUNDARY_TYPES),
        py_decorated_definition=cs.TS_PY_DECORATED_DEFINITION,
        py_decorated_definitions=sorted(cs.PY_DECORATED_DEFINITION_TYPES),
        py_shadow_binders=sorted(cs.PY_CAPTURE_SHADOW_BINDERS),
        py_as_pattern_target=cs.TS_PY_AS_PATTERN_TARGET,
        py_imports=sorted(cs.PY_IMPORT_TYPES),
        py_global=cs.TS_PY_GLOBAL_STATEMENT,
        py_case_pattern=cs.TS_PY_CASE_PATTERN,
        py_dotted_name=cs.TS_PY_DOTTED_NAME,
        py_aliased_import=cs.TS_ALIASED_IMPORT,
        python_scan=CallableScanRules(
            call=cs.TS_PY_CALL,
            identifier=cs.TS_PY_IDENTIFIER,
            closures=sorted(cs.PY_CALLABLE_CLOSURE_TYPES),
            opaque=sorted(cs.PY_CALLABLE_OPAQUE_TYPES),
        ),
        js_scan=CallableScanRules(
            call=cs.TS_CALL_EXPRESSION,
            identifier=cs.TS_IDENTIFIER,
            closures=sorted(cs.JS_TS_CALLABLE_CLOSURE_TYPES),
            opaque=sorted(cs.JS_TS_CALLABLE_OPAQUE_TYPES),
        ),
    )


def _bridge_language_rules(
    language: cs.SupportedLanguage, rules: _Rules
) -> InterfaceLanguageRules:
    return InterfaceLanguageRules(
        flavor=_flavor(language),
        functions=sorted(rules.functions),
        blocks=sorted(rules.blocks),
        definitions=sorted(rules.definitions),
        blockers=sorted(rules.blockers),
        kept_names=sorted(rules.kept_names),
        calls=sorted(rules.calls),
        returns=sorted(rules.returns),
    )


def _flavor(language: cs.SupportedLanguage) -> cs.InterfaceFlavor:
    # The language branches above: `_is_read`, `_names_bound_within`,
    # `_caller_locals`, `_projection`, `_is_python_dunder`, and the
    # callable-parameter scan `_elidable_body` asks.
    if language == cs.SupportedLanguage.PYTHON:
        return cs.InterfaceFlavor.PYTHON
    if language in cs.JS_TS_LANGUAGES:
        return cs.InterfaceFlavor.JS
    return cs.InterfaceFlavor.OTHER
