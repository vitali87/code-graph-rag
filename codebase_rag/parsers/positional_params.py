"""Declared parameters of non-Python definitions, as a call fills them (#2517).

Python's `positional_params` (issue #227) is the list CPython counts in
"takes N positional arguments". The structural delta gives every call site
of a changed signature an arity verdict from that list, and no other
frontend wrote it, so a TypeScript function that gained a required
parameter changed nothing `cgr check` reported.

The languages read here spell optionality out in the signature, so the list
carries it:

- `name` must be passed;
- `name?` may be left out (a TypeScript `x?`, any default value);
- `...name` takes any number of trailing arguments (rest, variadic, `params`).

A receiver that one call form passes and another does not keeps its place,
written `self` (Rust: `s.m(1)` and `S::m(s, 1)`) or `this name` (a C#
extension method: `s.Ext(1)` and `Util.Ext(s, 1)`). A receiver no call
passes takes none: a TypeScript `this:` parameter, Java's `C this`, Go's
separate receiver field. A slot that binds no single name keeps its place
under its source text (`{ a, b }`, `(x, y)`) or `_`.

Every other language gets no list, which reads as "kinds unknown" and never
as "no parameters": C and C++ put defaults on a header declaration the
definition does not repeat, Scala and Dart have named and curried parameter
lists a count cannot check, and Lua accepts any count. A bodiless TypeScript
signature (an overload, an interface or abstract member) gets none either: a
call matches one of possibly several. A list this module cannot read in full
is withheld for the same reason: an entry missed would turn every correct
call into a surplus.
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from .. import constants as cs
from .utils import safe_decode_text


class _Unreadable(Exception):
    """A parameter-list child this module does not know how to count."""


def _source(node: Node | None) -> str:
    text = safe_decode_text(node) if node is not None else None
    if not text:
        raise _Unreadable
    # A destructuring pattern can span lines; one spelling per slot keeps a
    # reformat from reading as a signature change.
    return " ".join(text.split())


def _optional(entry: str, optional: bool) -> str:
    return f"{entry}{cs.POSITIONAL_OPTIONAL_SUFFIX}" if optional else entry


def _rest(entry: str) -> str:
    return f"{cs.POSITIONAL_REST_PREFIX}{entry}"


def _has_token(node: Node, token: str) -> bool:
    return any(not child.is_named and child.type == token for child in node.children)


def _parameter_list(node: Node, list_type: str) -> Node | None:
    params = node.child_by_field_name(cs.FIELD_PARAMETERS)
    return params if params is not None and params.type == list_type else None


def _named(params: Node) -> list[Node]:
    # `comment`, Rust's `line_comment` / `block_comment`: no slot.
    return [
        child
        for child in params.named_children
        if cs.AST_FP_COMMENT_SUBSTRING not in child.type
    ]


# --- JavaScript / TypeScript ---------------------------------------------------

_JS_TS_WRAPPED = frozenset({cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER})
# A call matching any of several overload signatures binds to the first,
# so no single signature's count can judge it.
_JS_TS_SIGNATURES = frozenset(
    {
        cs.TS_FUNCTION_SIGNATURE,
        cs.TS_METHOD_SIGNATURE,
        cs.TS_ABSTRACT_METHOD_SIGNATURE,
    }
)


def _js_ts(node: Node) -> list[str] | None:
    if node.type in _JS_TS_SIGNATURES:
        return None
    params = _parameter_list(node, cs.TS_JS_FORMAL_PARAMETERS)
    if params is None:
        # `x => x` has a lone `parameter` field and no list.
        single = node.child_by_field_name(cs.TS_FIELD_PARAMETER)
        return [_source(single)] if single is not None else None
    entries: list[str] = []
    for child in _named(params):
        if child.type in _JS_TS_WRAPPED:
            pattern = child.child_by_field_name(cs.TS_FIELD_PATTERN)
            if pattern is not None and pattern.type == cs.TS_THIS_PARAMETER:
                continue
            optional = (
                child.type == cs.TS_OPTIONAL_PARAMETER
                or child.child_by_field_name(cs.FIELD_VALUE) is not None
            )
            entries.append(_js_ts_pattern(pattern, optional))
        elif child.type == cs.TS_ASSIGNMENT_PATTERN:
            entries.append(
                _js_ts_pattern(child.child_by_field_name(cs.TS_FIELD_LEFT), True)
            )
        else:
            entries.append(_js_ts_pattern(child, False))
    return entries


def _js_ts_pattern(pattern: Node | None, optional: bool) -> str:
    if pattern is not None and pattern.type == cs.TS_REST_PATTERN:
        inner = pattern.named_children[0] if pattern.named_children else None
        return _rest(_source(inner))
    return _optional(_source(pattern), optional)


# --- Go ------------------------------------------------------------------------


def _go(node: Node) -> list[str] | None:
    params = _parameter_list(node, cs.TS_GO_PARAMETER_LIST)
    if params is None:
        return None
    entries: list[str] = []
    for decl in _named(params):
        names = [_source(n) for n in decl.children_by_field_name(cs.FIELD_NAME)]
        if decl.type == cs.TS_GO_PARAMETER_DECLARATION:
            # `a, b int` is two slots; a type-only `int` is one unnamed slot.
            entries.extend(names or [cs.CHAR_UNDERSCORE])
        elif decl.type == cs.TS_GO_VARIADIC_PARAMETER_DECLARATION:
            entries.append(_rest(names[0] if names else cs.CHAR_UNDERSCORE))
        else:
            raise _Unreadable
    return entries


# --- Rust ----------------------------------------------------------------------

_RUST_WRAPPING_PATTERNS = frozenset(
    {cs.TS_RS_REFERENCE_PATTERN, cs.TS_RS_REF_PATTERN, cs.TS_RS_MUT_PATTERN}
)


def _rust(node: Node) -> list[str] | None:
    # A closure's `closure_parameters` is not read: its call sites bind
    # through a local, never a definition the delta compares.
    params = _parameter_list(node, cs.TS_RS_PARAMETERS)
    if params is None:
        return None
    entries: list[str] = []
    for child in _named(params):
        if child.type == cs.TS_RS_SELF_PARAMETER:
            entries.append(cs.POSITIONAL_RECEIVER_SELF)
        elif child.type == cs.TS_RS_PARAMETER:
            pattern = child.child_by_field_name(cs.TS_FIELD_PATTERN)
            if pattern is not None and pattern.type == cs.TS_RS_SELF:
                entries.append(cs.POSITIONAL_RECEIVER_SELF)
            else:
                entries.append(_source(_rust_binding(pattern)))
        elif child.type == cs.TS_RS_VARIADIC_PARAMETER:
            # A foreign `fn printf(fmt: *const u8, ...)` takes any count.
            entries.append(cs.POSITIONAL_REST_PREFIX)
        elif child.type != cs.TS_RS_ATTRIBUTE_ITEM:
            raise _Unreadable
    return entries


def _rust_binding(pattern: Node | None) -> Node | None:
    # `&y`, `ref z` and `mut w` bind the identifier inside them.
    while pattern is not None and pattern.type in _RUST_WRAPPING_PATTERNS:
        pattern = next(
            (c for c in pattern.named_children if c.type != cs.TS_RS_MUTABLE_SPECIFIER),
            None,
        )
    return pattern


# --- PHP -----------------------------------------------------------------------

_PHP_PLAIN = frozenset(
    {cs.TS_PHP_SIMPLE_PARAMETER, cs.TS_PHP_PROPERTY_PROMOTION_PARAMETER}
)


def _php(node: Node) -> list[str] | None:
    params = _parameter_list(node, cs.TS_PHP_FORMAL_PARAMETERS)
    if params is None:
        return None
    entries: list[str] = []
    for child in _named(params):
        variable = child.child_by_field_name(cs.FIELD_NAME)
        # Without the `$`, as Parameter nodes and named arguments spell it.
        name = _source(
            next((c for c in variable.named_children if c.type == cs.TS_PHP_NAME), None)
            if variable is not None
            else None
        )
        if child.type == cs.TS_PHP_VARIADIC_PARAMETER:
            entries.append(_rest(name))
        elif child.type in _PHP_PLAIN:
            optional = child.child_by_field_name(cs.TS_PHP_FIELD_DEFAULT_VALUE)
            entries.append(_optional(name, optional is not None))
        else:
            raise _Unreadable
    return entries


# --- Java ----------------------------------------------------------------------


def _java(node: Node) -> list[str] | None:
    # A lambda's `inferred_parameters` or lone identifier is left unread.
    params = _parameter_list(node, cs.TS_JAVA_FORMAL_PARAMETERS)
    if params is None:
        return None
    entries: list[str] = []
    for child in _named(params):
        if child.type == cs.TS_FORMAL_PARAMETER:
            entries.append(_source(child.child_by_field_name(cs.FIELD_NAME)))
        elif child.type == cs.TS_SPREAD_PARAMETER:
            declarator = next(
                (
                    c
                    for c in child.named_children
                    if c.type == cs.TS_VARIABLE_DECLARATOR
                ),
                None,
            )
            name = (
                declarator.child_by_field_name(cs.FIELD_NAME)
                if declarator is not None
                else None
            )
            entries.append(_rest(_source(name)))
        elif child.type != cs.TS_RECEIVER_PARAMETER:
            raise _Unreadable
    return entries


# --- C# ------------------------------------------------------------------------


def _csharp(node: Node) -> list[str] | None:
    params = _parameter_list(node, cs.TS_CSHARP_PARAMETER_LIST)
    if params is None:
        return None
    entries: list[str] = []
    in_params_array = False
    for child in _named(params):
        if child.type == cs.TS_CSHARP_PARAMETER:
            entries.append(_csharp_parameter(child))
        elif child.type == cs.TS_CSHARP_ARRAY_TYPE:
            # `params int[] xs` is not wrapped in a `parameter`: the grammar
            # puts the array type and the name straight under the list.
            in_params_array = True
        elif child.type == cs.TS_IDENTIFIER and in_params_array:
            entries.append(_rest(_source(child)))
            in_params_array = False
        else:
            raise _Unreadable
    return entries


def _csharp_parameter(parameter: Node) -> str:
    name = _source(parameter.child_by_field_name(cs.FIELD_NAME))
    if any(
        child.type == cs.TS_CSHARP_MODIFIER
        and safe_decode_text(child) == cs.TS_CSHARP_THIS
        for child in parameter.children
    ):
        return f"{cs.POSITIONAL_RECEIVER_THIS_PREFIX}{name}"
    return _optional(name, _has_token(parameter, cs.CHAR_EQUALS))


_EXTRACTORS: dict[cs.SupportedLanguage, Callable[[Node], list[str] | None]] = {
    cs.SupportedLanguage.JS: _js_ts,
    cs.SupportedLanguage.TS: _js_ts,
    cs.SupportedLanguage.TSX: _js_ts,
    cs.SupportedLanguage.GO: _go,
    cs.SupportedLanguage.RUST: _rust,
    cs.SupportedLanguage.PHP: _php,
    cs.SupportedLanguage.JAVA: _java,
    cs.SupportedLanguage.CSHARP: _csharp,
}


def declared_positional_params(
    node: Node, language: cs.SupportedLanguage | None
) -> list[str] | None:
    """The definition's parameters with their optionality, or None.

    None for a language not read here (Python has its own extractor), for a
    node without a parameter list of that language's shape, and for a list
    holding a child this module cannot count.
    """
    extractor = _EXTRACTORS.get(language) if language is not None else None
    if extractor is None:
        return None
    try:
        return extractor(node)
    except _Unreadable:
        return None
