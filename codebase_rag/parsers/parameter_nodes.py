"""Declared parameters of a callable, in declaration order (issue #1804).

This is deliberately NOT the taint-flow slot table (`_lean_parameter_slots`,
`_py_positional_param_names`). Those answer "which parameter does positional
argument N bind to", so they stop at the first variadic, drop `self`/`cls` and
hide keyword-only parameters -- all correct for their purpose and all wrong for
a node that represents what a function declares. The two questions stay apart
so neither helper has to carry a flag that changes its meaning.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..services import IngestorProtocol
from .utils import safe_decode_text

if TYPE_CHECKING:
    from .type_facts import TypeReferenceResolver


class DeclaredParameter(NamedTuple):
    """One formal parameter as the source declares it."""

    name: str
    index: int
    start_line: int
    start_col: int
    type_name: str | None
    is_variadic: bool
    has_default: bool


# Python parameter node types that bind a name, and what each carries.
_PY_NAMED_PARAMETER_TYPES = frozenset(
    {
        cs.TS_PY_IDENTIFIER,
        cs.TS_PY_TYPED_PARAMETER,
        cs.TS_PY_DEFAULT_PARAMETER,
        cs.TS_PY_TYPED_DEFAULT_PARAMETER,
        cs.TS_PY_LIST_SPLAT_PATTERN,
        cs.TS_PY_DICTIONARY_SPLAT_PATTERN,
    }
)
_PY_VARIADIC_TYPES = frozenset(
    {cs.TS_PY_LIST_SPLAT_PATTERN, cs.TS_PY_DICTIONARY_SPLAT_PATTERN}
)
_PY_DEFAULTED_TYPES = frozenset(
    {cs.TS_PY_DEFAULT_PARAMETER, cs.TS_PY_TYPED_DEFAULT_PARAMETER}
)
_PY_IMPLICIT_RECEIVERS = frozenset({cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS})


def _py_binding(param: Node) -> tuple[Node | None, bool]:
    """(the identifier a parameter node binds, whether it is variadic).

    `*args: int` parses as `typed_parameter(list_splat_pattern, type)`, so the
    splat can sit one level down; the variadic flag comes from the node that
    actually carries the star, not from the wrapper.
    """
    if param.type == cs.TS_PY_IDENTIFIER:
        return param, False
    if param.type in _PY_VARIADIC_TYPES:
        return (
            next(
                (c for c in param.named_children if c.type == cs.TS_PY_IDENTIFIER), None
            ),
            True,
        )
    name = param.child_by_field_name(cs.TS_FIELD_NAME)
    if name is not None:
        return name, False
    # `typed_parameter` has no `name` field: its first named child is the
    # binding, which may itself be a splat pattern.
    inner = next(iter(param.named_children), None)
    if inner is not None and inner.type in _PY_VARIADIC_TYPES:
        return _py_binding(inner)
    return next(
        (c for c in param.children if c.type == cs.TS_PY_IDENTIFIER), None
    ), False


def python_declared_parameters(
    func_node: Node, *, has_receiver: bool = True
) -> list[DeclaredParameter]:
    """Every formal parameter a Python function declares, in source order.

    With `has_receiver`, a `self`/`cls` that is the FIRST BINDING is excluded
    -- it is the receiver, not a parameter the caller supplies, and it is 20%
    of all slots in this repo. "First binding" rather than first child: a
    comment can precede it. The CALLER decides `has_receiver`: a name alone
    cannot, because `def callback(self, value)` at module level and a
    `@staticmethod` both declare an explicit `self` that a caller supplies.
    The bare `*` and `/` separators bind nothing and take no index. `*args`
    and `**kwargs` are one parameter each, flagged variadic, annotated or not.

    `index` is the declaration position AFTER that exclusion. The owner's
    `param_types` list keeps the receiver (an empty string in first place on
    a method), so on such a method `param_types[index + 1]` is this
    parameter's annotation and on a function `param_types[index]` is. Each
    node also carries its own `type_name`, so nothing needs that join.
    """
    params_node = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params_node is None:
        return []
    declared: list[DeclaredParameter] = []
    seen_binding = False
    for param in params_node.named_children:
        if param.type not in _PY_NAMED_PARAMETER_TYPES:
            continue
        name_node, is_variadic = _py_binding(param)
        if name_node is None or not (name := safe_decode_text(name_node)):
            continue
        first_binding, seen_binding = not seen_binding, True
        if has_receiver and first_binding and name in _PY_IMPLICIT_RECEIVERS:
            continue
        type_node = param.child_by_field_name(cs.TS_FIELD_TYPE)
        declared.append(
            DeclaredParameter(
                name=name,
                index=len(declared),
                start_line=name_node.start_point[0] + 1,
                start_col=name_node.start_point[1],
                type_name=safe_decode_text(type_node) if type_node else None,
                is_variadic=is_variadic,
                has_default=param.type in _PY_DEFAULTED_TYPES,
            )
        )
    return declared


# --- Shared shape for the other languages -----------------------------------
#
# Every enumerator below walks the declaration in source order and advances
# one position per slot a CALLER supplies. A slot that binds no simple name
# (an unnamed C parameter, a JS destructuring pattern, Rust `_` or a tuple
# pattern, a Lua `...`) keeps its position and yields no entry, so the indices
# after it still agree with the source and with the owner's `param_types`.
# A receiver that no caller supplies (Rust `self`, a TypeScript `this`
# parameter, Java's `C this`) takes no position at all, the same policy as
# Python's `self`. Go's receiver is a separate field and Lua's `:` receiver is
# implicit, so neither ever appears in the list.


class _Slots:
    """Declaration-position accumulator."""

    def __init__(self) -> None:
        self.declared: list[DeclaredParameter] = []
        self._index = 0

    def add(
        self,
        name_node: Node | None,
        type_name: str | None,
        *,
        is_variadic: bool = False,
        has_default: bool = False,
    ) -> None:
        """One slot: an entry when it binds a name, a bare position otherwise."""
        name = safe_decode_text(name_node) if name_node is not None else None
        if name_node is not None and name:
            self.declared.append(
                DeclaredParameter(
                    name=name,
                    index=self._index,
                    start_line=name_node.start_point[0] + 1,
                    start_col=name_node.start_point[1],
                    type_name=type_name or None,
                    is_variadic=is_variadic,
                    has_default=has_default,
                )
            )
        self._index += 1

    def skip(self) -> None:
        """A slot that binds no single name: position consumed, no entry."""
        self._index += 1


def _field_type_text(node: Node) -> str | None:
    type_node = node.child_by_field_name(cs.FIELD_TYPE)
    return safe_decode_text(type_node) if type_node is not None else None


def _first_named(node: Node, node_type: str) -> Node | None:
    return next((c for c in node.named_children if c.type == node_type), None)


def _has_anonymous_child(node: Node, token: str) -> bool:
    return any(not c.is_named and c.type == token for c in node.children)


# --- Go ----------------------------------------------------------------------


def go_declared_parameters(func_node: Node) -> list[DeclaredParameter]:
    """`a, b int` is two slots of one type; `opts ...Opt` one variadic slot.

    The receiver lives in the `receiver` field, never in `parameters`. A
    blank `_` binds nothing and a type-only slot (`func f(int)`) has no
    identifier; both keep their position.
    """
    params = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return []
    slots = _Slots()
    for decl in params.named_children:
        type_text = _field_type_text(decl)
        if decl.type == cs.TS_GO_PARAMETER_DECLARATION:
            idents = [c for c in decl.children if c.type == cs.TS_IDENTIFIER]
            if not idents:
                slots.skip()
            for ident in idents:
                slots.add(_go_binding(ident), type_text)
        elif decl.type == cs.TS_GO_VARIADIC_PARAMETER_DECLARATION:
            ident = next((c for c in decl.children if c.type == cs.TS_IDENTIFIER), None)
            slots.add(
                _go_binding(ident),
                f"{cs.LANG_ELLIPSIS}{type_text or ''}",
                is_variadic=True,
            )
    return slots.declared


def _go_binding(ident: Node | None) -> Node | None:
    if ident is None or safe_decode_text(ident) == cs.CHAR_UNDERSCORE:
        return None
    return ident


# --- JavaScript / TypeScript -------------------------------------------------

_JS_TS_TYPED = frozenset({cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER})
_JS_TS_UNBOUND_PATTERNS = frozenset({cs.TS_OBJECT_PATTERN, cs.TS_ARRAY_PATTERN})


def js_ts_declared_parameters(func_node: Node) -> list[DeclaredParameter]:
    """A TS typed parameter wraps the real pattern, so it is unwrapped: that
    is what lets `...rest: T[]` flag variadic, `c: T = 1` flag default and
    a `this: T` parameter take no slot. A single-parameter arrow (`x => x`)
    has a `parameter` field and no list."""
    params = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    slots = _Slots()
    if params is None:
        single = func_node.child_by_field_name(cs.TS_FIELD_PARAMETER)
        if single is not None and single.type == cs.TS_IDENTIFIER:
            slots.add(single, None)
        return slots.declared
    for child in params.named_children:
        _js_ts_slot(child, slots, None, False)
    return slots.declared


def _js_ts_slot(
    node: Node, slots: _Slots, type_text: str | None, has_default: bool
) -> None:
    node_type = node.type
    if node_type == cs.TS_THIS_PARAMETER:
        return
    if node_type == cs.TS_IDENTIFIER:
        slots.add(node, type_text, has_default=has_default)
    elif node_type in _JS_TS_TYPED:
        pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
        if pattern is None:
            slots.skip()
            return
        # A `type_annotation` is `: T`; the type is what follows the colon.
        annotation = node.child_by_field_name(cs.FIELD_TYPE)
        raw = safe_decode_text(annotation) if annotation is not None else None
        text = raw.lstrip(cs.CHAR_COLON).strip() if raw else None
        _js_ts_slot(
            pattern,
            slots,
            text or None,
            node.child_by_field_name(cs.FIELD_VALUE) is not None,
        )
    elif node_type == cs.TS_ASSIGNMENT_PATTERN:
        left = node.child_by_field_name(cs.TS_FIELD_LEFT)
        if left is not None and left.type == cs.TS_IDENTIFIER:
            slots.add(left, type_text, has_default=True)
        else:
            slots.skip()
    elif node_type == cs.TS_REST_PATTERN:
        slots.add(_first_named(node, cs.TS_IDENTIFIER), type_text, is_variadic=True)
    elif node_type in _JS_TS_UNBOUND_PATTERNS:
        slots.skip()


# --- C / C++ -----------------------------------------------------------------

_C_FAMILY_PARAMETER_DECLARATIONS = frozenset(
    {
        cs.CppNodeType.PARAMETER_DECLARATION,
        cs.CppNodeType.OPTIONAL_PARAMETER_DECLARATION,
        cs.CppNodeType.VARIADIC_PARAMETER_DECLARATION,
    }
)
_C_FAMILY_DEFINITIONS = frozenset(
    {cs.CppNodeType.FUNCTION_DEFINITION, cs.TS_CPP_DECLARATION}
)
_C_FAMILY_NAME_NODES = frozenset(
    {cs.CppNodeType.IDENTIFIER, cs.CppNodeType.FIELD_IDENTIFIER}
)


def c_cpp_declared_parameters(func_node: Node) -> list[DeclaredParameter]:
    """`type_name` is the declared type specifier without the declarator's
    pointer or reference marks (`const char *b` -> `char`), the same reading
    `extract_type_facts` gives a C/C++ return type. A C `...` and an unnamed
    parameter keep their positions; a pack (`Args&&... args`) is one variadic
    slot. A C++ C-style `...` is an anonymous token and takes no position."""
    params = _c_family_parameter_list(func_node)
    if params is None:
        return []
    slots = _Slots()
    for decl in params.named_children:
        if decl.type == cs.CppNodeType.VARIADIC_PARAMETER:
            slots.skip()
        elif decl.type in _C_FAMILY_PARAMETER_DECLARATIONS:
            slots.add(
                _c_family_name_node(decl.child_by_field_name(cs.FIELD_DECLARATOR)),
                _field_type_text(decl),
                is_variadic=decl.type == cs.CppNodeType.VARIADIC_PARAMETER_DECLARATION,
                has_default=decl.type == cs.CppNodeType.OPTIONAL_PARAMETER_DECLARATION,
            )
    return slots.declared


def _c_family_parameter_list(func_node: Node) -> Node | None:
    node = func_node
    if node.type == cs.TS_CPP_TEMPLATE_DECLARATION:
        # `template<...> void f(...)`: the definition is the wrapped child.
        node = next(
            (c for c in node.named_children if c.type in _C_FAMILY_DEFINITIONS), node
        )
    declarator = node.child_by_field_name(cs.FIELD_DECLARATOR)
    if declarator is None:
        return None
    if declarator.type == cs.TS_CPP_ABSTRACT_FUNCTION_DECLARATOR:
        # A lambda's declarator carries the list directly.
        return declarator.child_by_field_name(cs.KEY_PARAMETERS)
    function_declarator = _first_descendant(
        declarator, cs.CppNodeType.FUNCTION_DECLARATOR
    )
    if function_declarator is None:
        return None
    return function_declarator.child_by_field_name(cs.KEY_PARAMETERS)


def _first_descendant(node: Node, node_type: str) -> Node | None:
    # Pre-order, so the OUTERMOST function_declarator wins over one nested in
    # a function-pointer parameter.
    if node.type == node_type:
        return node
    for child in node.children:
        found = _first_descendant(child, node_type)
        if found is not None:
            return found
    return None


def _c_family_name_node(declarator: Node | None) -> Node | None:
    # The node counterpart of `utils.cpp_declarator_name`: unwrap pointer,
    # reference, parenthesized, array, function and variadic declarators to
    # the bound identifier, which is what carries the position.
    current = declarator
    while current is not None:
        if current.type in _C_FAMILY_NAME_NODES:
            return current
        if (inner := current.child_by_field_name(cs.FIELD_DECLARATOR)) is not None:
            current = inner
            continue
        current = next(
            (
                child
                for child in current.children
                if child.is_named
                and (
                    cs.CPP_DECLARATOR_SUFFIX in child.type
                    or child.type in _C_FAMILY_NAME_NODES
                )
            ),
            None,
        )
    return None


# --- Java --------------------------------------------------------------------


def java_declared_parameters(func_node: Node) -> list[DeclaredParameter]:
    """`String... xs` is one variadic slot typed `String...`, matching the
    owner's `param_types`; `C this` is the receiver and takes no slot."""
    params = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return []
    slots = _Slots()
    for param in params.named_children:
        if param.type == cs.TS_FORMAL_PARAMETER:
            slots.add(param.child_by_field_name(cs.FIELD_NAME), _field_type_text(param))
        elif param.type == cs.TS_SPREAD_PARAMETER:
            element = next(iter(param.named_children), None)
            declarator = _first_named(param, cs.TS_VARIABLE_DECLARATOR)
            slots.add(
                declarator.child_by_field_name(cs.FIELD_NAME) if declarator else None,
                f"{safe_decode_text(element)}{cs.LANG_ELLIPSIS}" if element else None,
                is_variadic=True,
            )
    return slots.declared


# --- C# ----------------------------------------------------------------------


def csharp_declared_parameters(func_node: Node) -> list[DeclaredParameter]:
    """`params T[] rest` is not wrapped in a `parameter`: the grammar inlines
    it as a bare `array_type` followed by a bare `identifier`, so the type
    opens the variadic slot and the next identifier names it. An extension
    method's `this C self` IS caller-supplied (`c.E(x)` or `E(c, x)`) and
    keeps its slot."""
    params = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return []
    slots = _Slots()
    pending_variadic: str | None = None
    for param in params.named_children:
        if param.type == cs.TS_CSHARP_PARAMETER:
            slots.add(
                param.child_by_field_name(cs.FIELD_NAME),
                _field_type_text(param),
                has_default=_has_anonymous_child(param, cs.CHAR_EQUALS),
            )
            pending_variadic = None
        elif param.type == cs.TS_CSHARP_ARRAY_TYPE:
            pending_variadic = f"{cs.CSHARP_PARAMS_PREFIX}{safe_decode_text(param)}"
        elif param.type == cs.TS_IDENTIFIER and pending_variadic is not None:
            slots.add(param, pending_variadic, is_variadic=True)
            pending_variadic = None
    return slots.declared


# --- Lua ---------------------------------------------------------------------


def lua_declared_parameters(func_node: Node) -> list[DeclaredParameter]:
    """Bare identifiers; a trailing `...` binds no name and keeps its
    position. A `function obj:m()` receiver is implicit and never listed."""
    params = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return []
    slots = _Slots()
    for param in params.named_children:
        if param.type == cs.TS_LUA_IDENTIFIER:
            slots.add(param, None)
        elif param.type == cs.TS_LUA_VARARG_EXPRESSION:
            slots.skip()
    return slots.declared


# --- Scala -------------------------------------------------------------------


def scala_declared_parameters(func_node: Node) -> list[DeclaredParameter]:
    """Every parameter list of a curried `def f(a: Int)(b: Int)(implicit c: C)`
    in order -- the taint table reads only the first, because only that one
    maps to a call site's `arg:<index>`; a declaration has them all. A
    repeated parameter (`xs: Int*`) is flagged by its type node."""
    slots = _Slots()
    for plist in func_node.named_children:
        if plist.type != cs.TS_SCALA_PARAMETERS:
            continue
        for param in plist.named_children:
            if param.type != cs.TS_SCALA_PARAMETER:
                continue
            type_node = param.child_by_field_name(cs.FIELD_TYPE)
            slots.add(
                param.child_by_field_name(cs.FIELD_NAME),
                safe_decode_text(type_node) if type_node else None,
                is_variadic=type_node is not None
                and type_node.type == cs.TS_SCALA_REPEATED_PARAMETER_TYPE,
                has_default=param.child_by_field_name(cs.TS_SCALA_FIELD_DEFAULT_VALUE)
                is not None,
            )
    return slots.declared


# --- Rust --------------------------------------------------------------------

_RUST_WRAPPING_PATTERNS = frozenset(
    {cs.TS_RS_REFERENCE_PATTERN, cs.TS_RS_REF_PATTERN, cs.TS_RS_MUT_PATTERN}
)


def rust_declared_parameters(func_node: Node) -> list[DeclaredParameter]:
    """`&self` (a `self_parameter`) and `self: Box<Self>` (a `parameter` whose
    pattern is `self`) are the receiver and take no slot. `&mut d`, `ref e`
    and `mut g` unwrap to their identifier; a tuple or struct pattern and
    `_` bind no single name and keep their position. A closure's list holds
    bare identifiers beside `parameter` nodes."""
    params = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return []
    slots = _Slots()
    for param in params.named_children:
        if param.type == cs.TS_IDENTIFIER:
            slots.add(param, None)
        elif param.type == cs.TS_RS_PARAMETER:
            pattern = param.child_by_field_name(cs.TS_FIELD_PATTERN)
            if pattern is not None and pattern.type == cs.TS_RS_SELF:
                continue
            slots.add(_rust_binding(pattern), _field_type_text(param))
    return slots.declared


def _rust_binding(pattern: Node | None) -> Node | None:
    if pattern is None:
        return None
    if pattern.type == cs.TS_IDENTIFIER:
        return pattern
    if pattern.type in _RUST_WRAPPING_PATTERNS:
        inner = next(
            (c for c in pattern.named_children if c.type != cs.TS_RS_MUTABLE_SPECIFIER),
            None,
        )
        return _rust_binding(inner)
    return None


# --- PHP ---------------------------------------------------------------------

_PHP_NAMED_PARAMETERS = frozenset(
    {
        cs.TS_PHP_SIMPLE_PARAMETER,
        cs.TS_PHP_PROPERTY_PROMOTION_PARAMETER,
        cs.TS_PHP_VARIADIC_PARAMETER,
    }
)


def php_declared_parameters(func_node: Node) -> list[DeclaredParameter]:
    """The name is the identifier without its `$` sigil, as Field nodes
    record a promoted property, so `private int $x` names the same thing
    from both labels. The taint table keeps the sigil because it matches
    the parameter against in-body uses; a node does not."""
    params = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return []
    slots = _Slots()
    for param in params.named_children:
        if param.type not in _PHP_NAMED_PARAMETERS:
            continue
        variable = param.child_by_field_name(cs.FIELD_NAME)
        slots.add(
            _first_named(variable, cs.TS_PHP_NAME) if variable is not None else None,
            _field_type_text(param),
            is_variadic=param.type == cs.TS_PHP_VARIADIC_PARAMETER,
            has_default=param.child_by_field_name(cs.TS_PHP_FIELD_DEFAULT_VALUE)
            is not None,
        )
    return slots.declared


# --- Dart --------------------------------------------------------------------


def dart_declared_parameters(func_node: Node) -> list[DeclaredParameter]:
    """Optional-positional `[b = 'x']` and named `{required b}` groups wrap
    their parameters in `optional_formal_parameters`; Dart requires the
    group to come last, so flattening keeps every position. The default
    value is a SIBLING of the parameter (`formal_parameter = literal`), not
    a child. `this.x` binds the field's name and carries no type of its own.
    """
    plist = _first_named(func_node, cs.TS_DART_FORMAL_PARAMETER_LIST)
    if plist is None:
        return []
    slots = _Slots()
    for entry in plist.named_children:
        if entry.type == cs.TS_DART_FORMAL_PARAMETER:
            _dart_slot(entry, slots)
        elif entry.type == cs.TS_DART_OPTIONAL_FORMAL_PARAMETERS:
            for inner in entry.named_children:
                if inner.type == cs.TS_DART_FORMAL_PARAMETER:
                    _dart_slot(inner, slots)
    return slots.declared


def _dart_slot(param: Node, slots: _Slots) -> None:
    following = param.next_sibling
    has_default = following is not None and following.type == cs.CHAR_EQUALS
    name_node = param.child_by_field_name(cs.TS_FIELD_NAME)
    if name_node is not None:
        slots.add(name_node, _dart_type_text(param, name_node), has_default=has_default)
        return
    initialiser = _first_named(param, cs.TS_DART_CONSTRUCTOR_PARAM)
    field_name = (
        next(
            (
                c
                for c in reversed(initialiser.children)
                if c.type == cs.TS_DART_IDENTIFIER
            ),
            None,
        )
        if initialiser is not None
        else None
    )
    slots.add(field_name, None, has_default=has_default)


def _dart_type_text(param: Node, name_node: Node) -> str | None:
    # The type is not a field: it is whatever precedes the name, minus any
    # leading `final` / `covariant` keyword.
    raw = param.text[: name_node.start_byte - param.start_byte] if param.text else b""
    tokens = raw.decode(cs.ENCODING_UTF8, errors="replace").split()
    while tokens and tokens[0] in cs.DART_PARAMETER_MODIFIERS:
        tokens.pop(0)
    return " ".join(tokens) or None


_ENUMERATORS: dict[cs.SupportedLanguage, Callable[[Node], list[DeclaredParameter]]] = {
    cs.SupportedLanguage.GO: go_declared_parameters,
    cs.SupportedLanguage.JS: js_ts_declared_parameters,
    cs.SupportedLanguage.TS: js_ts_declared_parameters,
    cs.SupportedLanguage.TSX: js_ts_declared_parameters,
    cs.SupportedLanguage.C: c_cpp_declared_parameters,
    cs.SupportedLanguage.CPP: c_cpp_declared_parameters,
    cs.SupportedLanguage.JAVA: java_declared_parameters,
    cs.SupportedLanguage.CSHARP: csharp_declared_parameters,
    cs.SupportedLanguage.LUA: lua_declared_parameters,
    cs.SupportedLanguage.SCALA: scala_declared_parameters,
    cs.SupportedLanguage.RUST: rust_declared_parameters,
    cs.SupportedLanguage.PHP: php_declared_parameters,
    cs.SupportedLanguage.DART: dart_declared_parameters,
}


class PendingParameterType(NamedTuple):
    """A parameter's annotation, held until every file's types are registered."""

    parameter_qn: str
    module_qn: str
    type_name: str
    # The owning file's relative path. Scoped re-ingestion discards the facts
    # of files it re-parses by THIS, not by module_qn: `foo.py` and
    # `foo/__init__.py` share a module qn, and keying on it dropped the
    # unchanged file's facts along with the re-parsed one's.
    path: str


def declared_parameters(
    func_node: Node, language: cs.SupportedLanguage | None, *, has_receiver: bool
) -> list[DeclaredParameter]:
    """Per-language dispatch. A missing entry means "not covered" (SQL),
    never "no parameters". Only Python needs `has_receiver`: everywhere else
    the receiver is either outside the list or a distinct node shape."""
    if language == cs.SupportedLanguage.PYTHON:
        return python_declared_parameters(func_node, has_receiver=has_receiver)
    enumerator = _ENUMERATORS.get(language) if language is not None else None
    return enumerator(func_node) if enumerator is not None else []


def emit_declared_parameters(
    ingestor: IngestorProtocol,
    sink: list[PendingParameterType] | None,
    label: cs.NodeLabel,
    qualified_name: str,
    module_qn: str | None,
    func_node: Node,
    language: cs.SupportedLanguage | None,
    owner_props: dict,
    *,
    has_receiver: bool,
) -> int:
    """Parameter nodes and HAS_PARAMETER edges for one Function or Method.

    `has_receiver` is the call site's knowledge: a Method's first binding is
    the receiver unless the method is static; a Function's never is.

    Gated on the capture selection the same way `link_contracts` is: a
    filtering sink that would drop the edge must not receive the node either,
    or the Parameter is orphaned. Returns the number of parameters emitted.
    """
    rel_gate = getattr(ingestor, "rel_enabled", None)
    if callable(rel_gate) and not rel_gate(cs.RelationshipType.HAS_PARAMETER):
        return 0
    declared = declared_parameters(func_node, language, has_receiver=has_receiver)
    if not declared:
        return 0
    path = owner_props.get(cs.KEY_PATH)
    absolute_path = owner_props.get(cs.KEY_ABSOLUTE_PATH)
    owner = (label.value, cs.KEY_QUALIFIED_NAME, qualified_name)
    for param in declared:
        param_qn = f"{qualified_name}{cs.SEPARATOR_DOT}{param.index}"
        props: dict = {
            cs.KEY_QUALIFIED_NAME: param_qn,
            cs.KEY_NAME: param.name,
            cs.KEY_INDEX: param.index,
            cs.KEY_START_LINE: param.start_line,
            cs.KEY_START_COL: param.start_col,
            cs.KEY_IS_VARIADIC: param.is_variadic,
            cs.KEY_HAS_DEFAULT: param.has_default,
        }
        if path is not None:
            props[cs.KEY_PATH] = path
        if absolute_path is not None:
            props[cs.KEY_ABSOLUTE_PATH] = absolute_path
        if param.type_name:
            props[cs.KEY_TYPE_NAME] = param.type_name
        ingestor.ensure_node_batch(cs.NodeLabel.PARAMETER, props)
        ingestor.ensure_relationship_batch(
            owner,
            cs.RelationshipType.HAS_PARAMETER,
            (cs.NodeLabel.PARAMETER.value, cs.KEY_QUALIFIED_NAME, param_qn),
            properties={cs.KEY_INDEX: param.index},
        )
        if (
            sink is not None
            and module_qn is not None
            and param.type_name
            and isinstance(path, str)
        ):
            sink.append(
                PendingParameterType(param_qn, module_qn, param.type_name, path)
            )
    return len(declared)


def emit_parameter_type_edges(
    pending: list[PendingParameterType],
    resolver: TypeReferenceResolver,
    ingestor: IngestorProtocol,
) -> int:
    """OF_TYPE edges for every queued parameter, after Pass 2.

    One resolve per DISTINCT (annotation, module) rather than per parameter:
    measured on this repo, 7,868 annotated parameters carry 575 distinct
    annotation strings, so the memo is where the cost goes, not the dedup
    `_emit_accepts` does per function (which saves 13%).
    """
    memo: dict[tuple[str, str], list[str]] = {}
    emitted = 0
    for fact in pending:
        key = (fact.type_name, fact.module_qn)
        targets = memo.get(key)
        if targets is None:
            targets = memo[key] = resolver.resolve_annotation(
                fact.type_name, fact.module_qn
            )
        source = (
            cs.NodeLabel.PARAMETER.value,
            cs.KEY_QUALIFIED_NAME,
            fact.parameter_qn,
        )
        for target_qn in targets:
            ingestor.ensure_relationship_batch(
                source,
                cs.RelationshipType.OF_TYPE,
                (
                    str(resolver._registry[target_qn]),
                    cs.KEY_QUALIFIED_NAME,
                    target_qn,
                ),
            )
            emitted += 1
    pending.clear()
    return emitted
