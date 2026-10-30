"""What a call node says on its own: the callee name its syntax spells, the
arguments it passes, and the edge-site properties that describe it.

`CallProcessor` names every call node it scans through `call_target_name`,
reads the arguments its callback and flow passes follow through
`parse_call_arguments`, and stamps each edge it emits with the
`call_site_properties` of the node that produced it (issue #1522). Nothing
here reads the pass's state: a language's own reading of a call reaches
`call_target_name` as the hooks of its `LanguageCalls`."""

from tree_sitter import Node

from .. import constants as cs
from ..node_text import decode_node_text, safe_decode_text
from ..types_defs import PropertyDict
from .cpp import calls as cpp_calls
from .cpp import utils as cpp_utils
from .csharp import utils as csharp_utils
from .dart import calls as dart_calls
from .go import calls as go_calls
from .js_ts import calls as js_calls
from .language_calls import LanguageCalls
from .rust import calls as rs_calls
from .utils import call_site_start, node_site_properties


def _find_call_arguments_node(call_node: Node) -> Node | None:
    args_node = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
    if args_node is not None:
        return args_node
    # C# target-typed `new(...)` exposes NO fields at all; its argument_list
    # is an unfielded named child (Serilog hands its CreateLogger local
    # functions to `return new(...)`, which the fielded lookup silently
    # dropped).
    args_node = next(
        (
            child
            for child in call_node.named_children
            if child.type == cs.TS_CSHARP_ARGUMENT_LIST
        ),
        None,
    )
    if args_node is not None:
        return args_node
    return dart_calls.call_arguments_node(call_node)


def _add_py_keyword_argument(child: Node, keyword: dict[str, Node]) -> None:
    name_node = child.child_by_field_name(cs.FIELD_NAME)
    value_node = child.child_by_field_name(cs.FIELD_VALUE)
    if (
        name_node is not None
        and value_node is not None
        and (name := safe_decode_text(name_node)) is not None
    ):
        keyword[name] = value_node


def _split_call_arguments(args_node: Node) -> tuple[list[Node], dict[str, Node]]:
    positional: list[Node] = []
    keyword: dict[str, Node] = {}
    for child in args_node.named_children:
        # C# wraps every argument expression in an `argument` node (which
        # may carry a ref/out modifier or a name: colon); Dart wraps
        # positional values the same way. Unwrap to the expression itself,
        # its LAST named child, so downstream reference-type checks see
        # the identifier, not the wrapper.
        if (
            child.type in (cs.TS_CSHARP_ARGUMENT, cs.TS_DART_ARGUMENT)
            and child.named_children
        ):
            child = child.named_children[-1]
        if child.type == cs.TS_DART_NAMED_ARGUMENT:
            dart_calls.add_named_argument(child, keyword)
        elif child.type == cs.TS_PY_KEYWORD_ARGUMENT:
            _add_py_keyword_argument(child, keyword)
        else:
            positional.append(child)
    return positional, keyword


def parse_call_arguments(call_node: Node) -> tuple[list[Node], dict[str, Node]]:
    """A call's arguments: its positional values in order, and its keyword
    values by name; both empty for a node with no argument list."""
    args_node = _find_call_arguments_node(call_node)
    if args_node is None:
        return [], {}
    return _split_call_arguments(args_node)


_PY_UNPACKING_FLAGS = {
    cs.TS_PY_LIST_SPLAT: cs.KEY_STAR_ARGS,
    cs.TS_PY_DICTIONARY_SPLAT: cs.KEY_STAR_KWARGS,
}


def call_site_properties(node: Node) -> PropertyDict:
    """Edge-site properties for the expression that produced an edge (#1522).

    Every site carries its span; a node with an argument list (a call, a
    constructor invocation) also carries `arg_count` and the keyword names
    it passes, so a consumer can check arity at the site without re-parsing.
    A reference site (a bare function value, an attribute read) has no
    argument list and carries the span alone.

    A Python `*rest` or `**opts` passes an unknown number of arguments, so
    it is flagged instead of counted. Counted as one positional, it made
    `send(req, **opts)` pass two positionals to `def send(request,
    **kwargs)`, a `too_many` that failed `cgr check` on code that runs
    (issue #2635). The split itself keeps the unpacking in `positional`:
    the callback passes read argument slots from it.
    """
    props = node_site_properties(node)
    # A member call's node spans its receiver, so every hop of a chain began
    # where the chain begins: callers showed the wrong line for a multi-line
    # chain, and two `.put(...)` hops of one chain merged into one edge on
    # their shared (line, col) (issue #3166). The site starts at the name.
    row, col = call_site_start(node)
    props[cs.KEY_LINE] = row + 1
    props[cs.KEY_COL] = col
    args_node = _find_call_arguments_node(node)
    if args_node is not None:
        positional, keyword = _split_call_arguments(args_node)
        written = 0
        for argument in positional:
            if (flag := _PY_UNPACKING_FLAGS.get(argument.type)) is not None:
                props[flag] = True
            else:
                written += 1
        props[cs.KEY_ARG_COUNT] = written + len(keyword)
        props[cs.KEY_KWARG_NAMES] = list(keyword)
        if _count_unknown(args_node, positional):
            props[cs.KEY_SPREAD_ARGS] = True
        if (qualifier := _call_qualifier(node)) is not None:
            props[cs.KEY_CALL_QUALIFIER] = qualifier
    return props


def _count_unknown(args_node: Node, positional: list[Node]) -> bool:
    """Whether the call passes a number of values its arguments do not show.

    Counted as one argument, `f(...args)` would read as too few for
    `f(a, b)` on a call that type-checks (issue #2517). A tagged template
    passes its strings array and one value per substitution, not the
    fragments its node holds.
    """
    if args_node.type == cs.TS_TEMPLATE_STRING:
        return True
    return any(
        argument.type in _SPREAD_ARGUMENTS for argument in positional
    ) or go_calls.passes_every_result(args_node, positional)


# Arguments passing an unknown number of values. PHP's `argument` wrapper is
# unwrapped with C#'s, which shares its node type, so the unpacking is seen.
_SPREAD_ARGUMENTS = frozenset(
    {
        cs.TS_SPREAD_ELEMENT,
        cs.TS_GO_VARIADIC_ARGUMENT,
        cs.TS_PHP_VARIADIC_UNPACKING,
    }
)


def _call_qualifier(node: Node) -> str | None:
    """What a Rust or C# call is written through, for receiver counting.

    `S::m(s, 1)` passes the receiver that `s.m(1)` leaves implicit, and so
    does C#'s `Util.Ext(s, 1)` against `s.Ext(1)` (issue #2517). A path, or
    a C# name that binds no value at the call, records its last segment
    (`S`, `Self`, `Util`); a value records "": every Rust `.` call, and a
    C# call through a local, parameter, field or property, `this`, a
    literal or `s?.Ext(1)`. A bare call records nothing.
    """
    function = node.child_by_field_name(cs.FIELD_FUNCTION)
    if function is None:
        return None
    if node.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
        return _csharp_call_qualifier(node, function)
    if node.type != cs.TS_RS_CALL_EXPRESSION:
        return None
    if function.type == cs.TS_GENERIC_FUNCTION:
        # `S::m::<u8>(s)`: the turbofish wraps the path.
        function = function.child_by_field_name(cs.FIELD_FUNCTION)
    if function is None:
        return None
    if function.type == cs.TS_SCOPED_IDENTIFIER:
        path = function.child_by_field_name(cs.TS_RS_FIELD_PATH)
        return (_last_name(path) or None) if path is not None else None
    # C++ shares the node type; only Rust's carries a `value` field.
    if (
        function.type == cs.TS_RS_FIELD_EXPRESSION
        and function.child_by_field_name(cs.FIELD_VALUE) is not None
    ):
        return ""
    return None


# A bare C# callee: `Ext(s, 1)` under `using static`, receiver as argument.
_CSHARP_BARE_CALLEES = frozenset({cs.TS_CSHARP_IDENTIFIER, cs.TS_CSHARP_GENERIC_NAME})


def _csharp_call_qualifier(call: Node, function: Node) -> str | None:
    if function.type in _CSHARP_BARE_CALLEES:
        return None
    if function.type != cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
        # `s?.Ext(1)` and every other non-member form take it from a value.
        return ""
    left = function.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
    head = left
    while head is not None and head.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
        head = head.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
    if head is None:
        return ""
    if head.type == cs.TS_CSHARP_IDENTIFIER:
        # A string named `Util` makes `Util.Ext(1, 2)` an instance call
        # even where an extension class `Util` exists (#2832).
        name = safe_decode_text(head)
        if not name or csharp_utils.binds_value(call, name):
            return ""
    elif head.type != cs.TS_CSHARP_ALIAS_QUALIFIED_NAME:
        # `this.Util`, `Make().Util`, a literal: a value.
        return ""
    return _last_name(left)


def _last_name(node: Node | None) -> str:
    """The last segment of a plain or dotted name, "" for anything else."""
    if node is None:
        return ""
    if node.type == cs.TS_IDENTIFIER:
        return safe_decode_text(node) or ""
    if node.type in (
        cs.TS_SCOPED_IDENTIFIER,
        cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION,
        cs.TS_CSHARP_ALIAS_QUALIFIED_NAME,
    ):
        return _last_name(node.child_by_field_name(cs.FIELD_NAME))
    # Rust `<S as Trait>` and other non-name paths still name a type.
    if node.type == cs.TS_RS_BRACKETED_TYPE:
        return " ".join((safe_decode_text(node) or "").split())
    return ""


def call_target_name(call_node: Node, calls: LanguageCalls) -> str | None:
    """The callee name a call node spells, read through its language's call
    strategy first; None for a call that names nothing."""
    # A macro-internal call (Rust `name(args)` inside a token_tree) is
    # captured as the bare identifier node; its text is the callee name. A
    # macro tokenizes `server.run()` into loose tokens (`server . run ( )`),
    # dropping the field_expression, so reconstruct any `<recv>.method`
    # receiver chain from the preceding sibling tokens; else the bare method
    # mis-resolves (`server.run()` in tokio::select! to the same-module free
    # fn `run` instead of Listener.run).
    if call_node.type == cs.TS_IDENTIFIER and call_node.text is not None:
        return rs_calls.macro_token_call_name(call_node)
    # A call node the language names in a shape of its own (a SQL
    # `invocation`, a Dart selector, a Julia call, a JS/TS function called
    # in place) is decided there, whole.
    if (own := calls.call_name(call_node)).decided:
        return own.name
    if func_child := call_node.child_by_field_name(cs.TS_FIELD_FUNCTION):
        if func_child.type == cs.TS_PARENTHESIZED_EXPRESSION:
            return js_calls.iife_target_name(func_child)
        if (name := function_field_call_name(func_child, calls)) is not None:
            return name
    return _node_shaped_call_name(call_node, calls)


def function_field_call_name(func_child: Node, calls: LanguageCalls) -> str | None:
    """The callee name a call's `function` field spells; None means no case
    named it and the caller falls back to the call node's own shape."""
    if (name := calls.function_field_name(func_child)) is not None:
        return name
    match func_child.type:
        case (
            cs.TS_IDENTIFIER
            | cs.TS_ATTRIBUTE
            | cs.TS_MEMBER_EXPRESSION
            | cs.CppNodeType.QUALIFIED_IDENTIFIER
            | cs.TS_SCOPED_IDENTIFIER
            | cs.TS_SELECTOR_EXPRESSION
            | cs.TS_PHP_NAME
        ):
            if func_child.text is not None:
                return decode_node_text(func_child.text)
        case cs.TS_GENERIC_FUNCTION:
            # turbofish: unwrap to the underlying callee identifier
            inner = func_child.child_by_field_name(cs.TS_FIELD_FUNCTION)
            if inner and inner.text:
                return decode_node_text(inner.text)
        case cs.TS_CPP_FIELD_EXPRESSION:
            return cpp_calls.field_call_name(func_child)
    return None


def _node_shaped_call_name(call_node: Node, calls: LanguageCalls) -> str | None:
    # The callee name of a call node with no `function` field (or whose
    # `function` field named nothing): construction, operator, method
    # invocation and macro shapes, then the node's own `name` field.
    if (name := calls.node_call_name(call_node)) is not None:
        return name
    if (name := _construction_call_name(call_node, calls)) is not None:
        return name
    if (name := _operator_call_name(call_node)) is not None:
        return name

    if name_node := call_node.child_by_field_name(cs.FIELD_NAME):
        if name_node.text is not None:
            return decode_node_text(name_node.text)

    return None


def _construction_call_name(call_node: Node, calls: LanguageCalls) -> str | None:
    if call_node.type == cs.TS_OBJECT_CREATION_EXPRESSION and calls.new_constructs_type:
        # Java/C# `new Foo(...)` names the class via the `type` field (no
        # `function` field). Returning the base type name routes construction
        # through the normal resolve loop: the class gets INSTANTIATES and its
        # constructor(s) get CALLS. Strip generic args (`new ArrayList<T>()`
        # -> ArrayList); a scoped name (`Outer.Inner`) is left for the resolver.
        type_node = call_node.child_by_field_name(cs.FIELD_TYPE)
        if type_node is not None and type_node.text is not None:
            return decode_node_text(type_node.text).split(cs.CHAR_ANGLE_OPEN, 1)[0]
    return None


def _operator_call_name(call_node: Node) -> str | None:
    match call_node.type:
        case (
            cs.TS_CPP_BINARY_EXPRESSION
            | cs.TS_CPP_UNARY_EXPRESSION
            | cs.TS_CPP_UPDATE_EXPRESSION
        ):
            operator_node = call_node.child_by_field_name(cs.FIELD_OPERATOR)
            if operator_node and operator_node.text:
                operator_text = decode_node_text(operator_node.text)
                return cpp_utils.convert_operator_symbol_to_name(operator_text)
        case cs.TS_METHOD_INVOCATION:
            return _method_invocation_call_name(call_node)
    return None


def _method_invocation_call_name(call_node: Node) -> str | None:
    object_node = call_node.child_by_field_name(cs.FIELD_OBJECT)
    name_node = call_node.child_by_field_name(cs.FIELD_NAME)
    if name_node and name_node.text:
        method_name = decode_node_text(name_node.text)
        if not object_node or not object_node.text:
            return method_name
        object_text = decode_node_text(object_node.text)
        return f"{object_text}{cs.SEPARATOR_DOT}{method_name}"
    return None
