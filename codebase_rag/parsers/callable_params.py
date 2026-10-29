"""A definition's parameter names, and which of them its body invokes.

Interprocedural callable-parameter flow (a callback handed to a first-party
function whose parameter the function calls, directly or from a nested
closure) needs both: the ordered names a definition binds, and the subset it
calls by bare name. Python, Go, JavaScript/TypeScript and C++ share one scan
here and differ only in a `_CallableScanConfig` and the rule for the names a
nested scope binds itself. Python's free variables and import-bound names
are read the same way, for the closures the scan descends into.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..node_text import safe_decode_text
from .cpp.utils import CPP_PARAMETER_DECLARATIONS, cpp_declarator_name
from .js_ts.utils import JS_TS_TYPED_PARAMETERS
from .utils import find_descendant

_PY_NAMED_PARAMETERS = frozenset(
    {cs.TS_PY_DEFAULT_PARAMETER, cs.TS_PY_TYPED_DEFAULT_PARAMETER}
)


_PY_KEYWORD_ONLY_BOUNDARIES = frozenset(
    {cs.TS_PY_KEYWORD_SEPARATOR, cs.TS_PY_LIST_SPLAT_PATTERN}
)


_PY_SCOPE_BOUNDARIES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_DECORATED_DEFINITION,
    }
)


def _python_parameter_name(param_node: Node) -> str | None:
    if param_node.type == cs.TS_PY_IDENTIFIER:
        return safe_decode_text(param_node)
    if param_node.type in _PY_NAMED_PARAMETERS:
        name_node = param_node.child_by_field_name(cs.FIELD_NAME)
        if name_node is not None and name_node.type == cs.TS_PY_IDENTIFIER:
            return safe_decode_text(name_node)
        return None
    if param_node.type == cs.TS_PY_TYPED_PARAMETER:
        for child in param_node.children:
            if child.type == cs.TS_PY_IDENTIFIER:
                return safe_decode_text(child)
    return None


_PY_CLOSURE_SCOPES = frozenset({cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_LAMBDA})


_GO_CLOSURE_SCOPES = frozenset({cs.TS_GO_FUNC_LITERAL})


class _CallableScanConfig(NamedTuple):
    # Node types the invoked-parameter scan needs per language: the call node
    # naming the callee via its `function` field, the identifier for a bare
    # callee, the closure scopes that capture an enclosing parameter, and the
    # class-like scopes that are skipped and never descended.
    call_type: str
    identifier_type: str
    closure_types: frozenset[str]
    opaque_types: frozenset[str]


_PY_SCAN = _CallableScanConfig(
    cs.TS_PY_CALL,
    cs.TS_PY_IDENTIFIER,
    _PY_CLOSURE_SCOPES,
    frozenset({cs.TS_PY_CLASS_DEFINITION}),
)


_GO_SCAN = _CallableScanConfig(
    cs.TS_GO_CALL_EXPRESSION,
    cs.TS_IDENTIFIER,
    _GO_CLOSURE_SCOPES,
    frozenset(),
)


_JS_TS_CLOSURE_SCOPES = frozenset(
    {
        cs.TS_ARROW_FUNCTION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_FUNCTION_DECLARATION,
    }
)


_JS_SCAN = _CallableScanConfig(
    cs.TS_CALL_EXPRESSION,
    cs.TS_IDENTIFIER,
    _JS_TS_CLOSURE_SCOPES,
    frozenset({cs.TS_CLASS_DECLARATION}),
)


_CPP_CLOSURE_SCOPES = frozenset({cs.TS_CPP_LAMBDA_EXPRESSION})


_CPP_SCAN = _CallableScanConfig(
    cs.TS_CPP_CALL_EXPRESSION,
    cs.CppNodeType.IDENTIFIER,
    _CPP_CLOSURE_SCOPES,
    frozenset(),
)


def _invoked_parameter_names(
    body_node: Node,
    candidates: set[str],
    scan: _CallableScanConfig,
    scope_bound_names: Callable[[Node], set[str]],
) -> set[str]:
    # Every language differs only in its scan config and its scope-binding
    # rule; the scan itself is shared.
    invoked: set[str] = set()
    _scan_invoked_parameters(
        body_node, set(candidates), invoked, scan, scope_bound_names
    )
    return invoked


def _cpp_scope_bound_names(scope_node: Node) -> set[str]:
    # A C++ lambda's own parameters shadow a same-named captured parameter of the
    # enclosing function, so subtract them before scanning the lambda body.
    # Otherwise the lambda invoking its own `cb` looks like an invocation of the
    # outer `cb`. The parameters hang off the lambda's `declarator`, an
    # abstract_function_declarator whose `parameters` list mirrors a function's.
    declarator = scope_node.child_by_field_name(cs.FIELD_DECLARATOR)
    return set(_cpp_declarator_param_names(declarator))


def cpp_parameter_names(func_node: Node) -> list[str]:
    # Ordered parameter names from the function declarator's parameter_list,
    # unwrapping each parameter's declarator to its bound identifier.
    declarator = func_node.child_by_field_name(cs.FIELD_DECLARATOR)
    func_declarator = find_descendant(declarator, cs.CppNodeType.FUNCTION_DECLARATOR)
    return _cpp_declarator_param_names(func_declarator)


def _cpp_declarator_param_names(declarator: Node | None) -> list[str]:
    # Bound parameter names from a (function|abstract_function) declarator's
    # `parameters` list, unwrapping each parameter's declarator to its identifier.
    if declarator is None:
        return []
    params = declarator.child_by_field_name(cs.KEY_PARAMETERS)
    if params is None:
        return []
    names: list[str] = []
    for declaration in params.named_children:
        if declaration.type not in CPP_PARAMETER_DECLARATIONS:
            continue
        param_declarator = declaration.child_by_field_name(cs.FIELD_DECLARATOR)
        if (name := cpp_declarator_name(param_declarator)) is not None:
            names.append(name)
    return names


def _js_ts_scope_bound_names(scope_node: Node) -> set[str]:
    # A nested arrow/function's own parameters shadow a same-named captured
    # parameter of the enclosing function.
    return set(js_ts_parameter_names(scope_node))


def js_ts_parameter_names(func_node: Node) -> list[str]:
    # Ordered parameter names. TypeScript wraps each in required_parameter /
    # optional_parameter (name under the `pattern` field); JavaScript uses a bare
    # identifier. A single-parameter arrow without parens keeps its parameter on
    # the `parameter` field. Destructuring patterns bind no callable name, skipped.
    names: list[str] = []
    params = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is not None:
        for child in params.named_children:
            if name := _js_ts_parameter_name(child):
                names.append(name)
        return names
    single = func_node.child_by_field_name(cs.TS_FIELD_PARAMETER)
    if single is not None and single.type == cs.TS_IDENTIFIER:
        if name := safe_decode_text(single):
            names.append(name)
    return names


def _js_ts_parameter_name(param: Node) -> str | None:
    # A bare identifier, or the identifier `pattern` of a TS typed parameter.
    if param.type == cs.TS_IDENTIFIER:
        return safe_decode_text(param)
    if param.type in JS_TS_TYPED_PARAMETERS:
        pattern = param.child_by_field_name(cs.TS_FIELD_PATTERN)
        if pattern is not None and pattern.type == cs.TS_IDENTIFIER:
            return safe_decode_text(pattern)
    return None


def _scan_invoked_parameters(
    scope_node: Node,
    candidates: set[str],
    invoked: set[str],
    config: _CallableScanConfig,
    bound_names: Callable[[Node], set[str]],
    sites: dict[str, list[Node]] | None = None,
) -> None:
    # Mark a candidate parameter invoked when it is called by bare name in this
    # lexical scope. Descend into nested closures that CAPTURE a candidate (do not
    # rebind it) so `outer(cb) { inner() { cb() } }` still attributes cb to outer,
    # the closure form decorator/formatter factories use. A nested scope's own
    # bound names are removed first so a shadowing local cannot masquerade as the
    # captured outer parameter. Class-like scopes are skipped entirely. `sites`,
    # when given, also collects every invoking call node per parameter.
    if not candidates:
        return
    stack: list[Node] = [scope_node]
    while stack:
        node = stack.pop()
        for child in node.children:
            _record_invocation(child, candidates, invoked, config, sites)
            if child.type in config.closure_types:
                inner = candidates - bound_names(child)
                _scan_invoked_parameters(
                    child, inner, invoked, config, bound_names, sites
                )
            elif child.type not in config.opaque_types:
                stack.append(child)


def _record_invocation(
    node: Node,
    candidates: set[str],
    invoked: set[str],
    config: _CallableScanConfig,
    sites: dict[str, list[Node]] | None,
) -> None:
    # When `node` calls a candidate by bare name (`cb()`), mark that candidate
    # invoked and, if `sites` collects them, keep `node` as one of its sites.
    name = _bare_call_name(node, config)
    if name is None or name not in candidates:
        return
    invoked.add(name)
    if sites is not None:
        sites.setdefault(name, []).append(node)


def _bare_call_name(node: Node, config: _CallableScanConfig) -> str | None:
    # The callee name of a call by bare identifier (`cb()`), else None.
    if node.type != config.call_type:
        return None
    fn = node.child_by_field_name(cs.FIELD_FUNCTION)
    if fn is None or fn.type != config.identifier_type:
        return None
    return safe_decode_text(fn)


def _go_scope_bound_names(scope_node: Node) -> set[str]:
    # A nested func_literal's own parameters shadow a same-named captured
    # parameter of the enclosing function.
    return set(go_parameter_names(scope_node))


def _python_scope_bound_names(scope_node: Node) -> set[str]:
    # Names a nested function/lambda binds itself, which shadow a same-named
    # captured parameter of an enclosing function: its parameters plus local
    # assignment targets and nested def/class names in its body.
    bound: set[str] = set()
    params = scope_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is not None:
        for child in params.named_children:
            if (name := _python_parameter_name(child)) is not None:
                bound.add(name)
    body = scope_node.child_by_field_name(cs.FIELD_BODY)
    if body is not None:
        _python_collect_bound_targets(body, bound)
    return bound


def _python_collect_bound_targets(node: Node, out: set[str]) -> None:
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        for child in current.children:
            if child.type in _PY_SCOPE_BOUNDARIES:
                # A nested def/class NAME binds here, but its body has its own
                # scope; record the name and do not descend.
                if name := _python_scope_boundary_name(child):
                    out.add(name)
                continue
            _python_collect_direct_bindings(child, out)
            stack.append(child)


def _python_scope_boundary_name(child: Node) -> str | None:
    # A decorated_definition has no `name` field of its own, since the name is
    # on the inner function/class definition it wraps.
    named = child
    if child.type == cs.TS_PY_DECORATED_DEFINITION:
        named = next(
            (
                c
                for c in child.children
                if c.type in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION)
            ),
            child,
        )
    name_node = named.child_by_field_name(cs.FIELD_NAME)
    return safe_decode_text(name_node) if name_node is not None else None


def _python_collect_direct_bindings(child: Node, out: set[str]) -> None:
    child_type = child.type
    if child_type in (cs.TS_PY_ASSIGNMENT, cs.TS_PY_FOR_STATEMENT):
        # Both bind whatever their `left` names.
        left = child.child_by_field_name(cs.TS_FIELD_LEFT)
        if left is not None:
            _python_collect_target_identifiers(left, out)
    elif child_type == cs.TS_PY_AS_PATTERN_TARGET:
        # `with ... as x` and `except ... as x` bind x here.
        _python_collect_target_identifiers(child, out)
    elif child_type in _PY_IMPORT_STATEMENTS:
        _python_collect_import_bound_names(child, out)
    elif child_type == cs.TS_PY_GLOBAL_STATEMENT:
        # `global x` rebinds x to module scope: it is not a capture of the
        # enclosing function, so exclude it like a local binding.
        for c in child.named_children:
            if c.type == cs.TS_PY_IDENTIFIER and (name := safe_decode_text(c)):
                out.add(name)
    elif child_type == cs.TS_PY_CASE_PATTERN:
        # A `match` case binds only its CAPTURE names, not value patterns.
        _python_collect_case_pattern_bindings(child, out)


def _python_collect_target_identifiers(node: Node, out: set[str]) -> None:
    if node.type == cs.TS_PY_IDENTIFIER:
        if name := safe_decode_text(node):
            out.add(name)
        return
    for child in node.children:
        _python_collect_target_identifiers(child, out)


def _python_collect_case_pattern_bindings(node: Node, out: set[str]) -> None:
    # A `match` case binds its CAPTURE patterns and nothing else. A bare name
    # (`case token:`, `case [token]:`) parses as a single-identifier dotted_name and
    # binds; a multi-part dotted_name (`case sentinel.token:`) is a VALUE pattern
    # that compares and binds nothing -- collecting its identifiers would wrongly
    # exclude a captured name used in a value position. `as` aliases are collected
    # by the general as_pattern_target branch. Only single-identifier dotted_names
    # (and their `_` wildcard, which decodes to nothing) are captured here.
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_PY_DOTTED_NAME:
            idents = [
                c for c in current.named_children if c.type == cs.TS_PY_IDENTIFIER
            ]
            if len(idents) == 1 and (name := safe_decode_text(idents[0])):
                out.add(name)
            continue
        stack.extend(current.children)


_PY_IMPORT_STATEMENTS = frozenset(
    {cs.TS_PY_IMPORT_STATEMENT, cs.TS_PY_IMPORT_FROM_STATEMENT}
)


def python_import_bound_names(node: Node) -> set[str]:
    # The names one `import` / `from ... import` statement binds.
    out: set[str] = set()
    _python_collect_import_bound_names(node, out)
    return out


def _python_collect_import_bound_names(node: Node, out: set[str]) -> None:
    # `import a.b` binds `a`; `import a.b as c` binds `c`; `from m import x` binds
    # `x`; `from m import x as y` binds `y`. The imported items live under the
    # `name` field; the from-module (`module_name` field) is the source, not a
    # local binding, so it is skipped by keying off `name` only.
    for item in node.children_by_field_name(cs.FIELD_NAME):
        if name := _python_import_item_bound_name(item):
            out.add(name)


def _python_import_item_bound_name(item: Node) -> str | None:
    if item.type == cs.TS_ALIASED_IMPORT:
        alias = item.child_by_field_name(cs.FIELD_ALIAS)
        return safe_decode_text(alias) if alias is not None else None
    if item.type == cs.TS_PY_DOTTED_NAME:
        first = next(
            (c for c in item.named_children if c.type == cs.TS_PY_IDENTIFIER),
            None,
        )
        return safe_decode_text(first) if first is not None else None
    return None


def _python_collect_identifier_reads(node: Node, out: set[str]) -> None:
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_PY_IDENTIFIER:
            if name := safe_decode_text(current):
                out.add(name)
            continue
        stack.extend(current.children)


def python_free_variable_names(func_node: Node) -> set[str]:
    # Names READ in a nested function's body that it does not bind itself (its
    # parameters, local assignment targets, and nested def/class names). Descends
    # into inner nested scopes so a variable used only in a doubly-nested body is
    # still free for this function -- needed so a capture composes transitively.
    # Conservative: over-approximates (globals, builtins, and attribute names are
    # swept in), but a capture only composes when a bare-name read reaches a sink
    # AND that name is tainted at the definition site, so the extras record
    # summaries that never compose -- sound and inert (issue #1197).
    body = func_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return set()
    reads: set[str] = set()
    _python_collect_identifier_reads(body, reads)
    return reads - _python_scope_bound_names(func_node)


def python_parameter_names(func_node: Node) -> list[str]:
    # Ordered parameter names with a leading self/cls dropped, so positions line
    # up with how call-site arguments map to parameters for bound methods.
    params_node = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params_node is None:
        return []
    names: list[str] = []
    for child in params_node.named_children:
        if (name := _python_parameter_name(child)) is not None:
            names.append(name)
    if names and names[0] in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS):
        names = names[1:]
    return names


def python_positional_parameter_names(func_node: Node) -> list[str]:
    # Declared POSITIONAL parameters, in order, with the receiver kept.
    #
    # Distinct from `python_parameter_names` in both respects, and both
    # differences are load-bearing for arity diagnosis (issue #227):
    # `*`/`*args` end the positional run because CPython counts nothing after
    # them in "takes N positional arguments", and the receiver stays because
    # CPython counts the bound `self` in that same N. `/` marks the preceding
    # parameters positional-ONLY and does not end the run.
    params_node = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params_node is None:
        return []
    names: list[str] = []
    for child in params_node.named_children:
        if child.type in _PY_KEYWORD_ONLY_BOUNDARIES:
            break
        if child.type == cs.TS_PY_DICTIONARY_SPLAT_PATTERN:
            continue
        if child.type == cs.TS_PY_POSITIONAL_SEPARATOR:
            continue
        if (name := _python_parameter_name(child)) is not None:
            names.append(name)
    return names


class _CallableScanInputs(NamedTuple):
    names: list[str]
    body: Node
    scan: _CallableScanConfig
    scope_bound: Callable[[Node], set[str]]


def _callable_scan_inputs(
    func_node: Node, language: cs.SupportedLanguage | None
) -> _CallableScanInputs | None:
    # The ordered parameters, the body and the language's scan rules; None
    # for a language without callable-parameter flow or a function with no
    # body or no parameters.
    if language == cs.SupportedLanguage.PYTHON:
        names = python_parameter_names(func_node)
        scan, scope_bound = _PY_SCAN, _python_scope_bound_names
    elif language == cs.SupportedLanguage.GO:
        names = go_parameter_names(func_node)
        scan, scope_bound = _GO_SCAN, _go_scope_bound_names
    elif language in cs.JS_TS_LANGUAGES:
        names = js_ts_parameter_names(func_node)
        scan, scope_bound = _JS_SCAN, _js_ts_scope_bound_names
    elif language == cs.SupportedLanguage.CPP:
        names = cpp_parameter_names(func_node)
        scan, scope_bound = _CPP_SCAN, _cpp_scope_bound_names
    else:
        return None
    body_node = func_node.child_by_field_name(cs.FIELD_BODY)
    if body_node is None or not names:
        return None
    return _CallableScanInputs(names, body_node, scan, scope_bound)


def callable_parameter_indices(
    func_node: Node, language: cs.SupportedLanguage | None
) -> dict[str, int]:
    # Maps each parameter invoked as a call inside the function body to its
    # positional index in the call-site argument list (self/cls dropped so the
    # index lines up with how bound methods are invoked).
    if (inputs := _callable_scan_inputs(func_node, language)) is None:
        return {}
    names = inputs.names
    invoked = _invoked_parameter_names(
        inputs.body, set(names), inputs.scan, inputs.scope_bound
    )
    if not invoked:
        return {}
    return {name: index for index, name in enumerate(names) if name in invoked}


def callable_parameter_invocations(
    func_node: Node, language: cs.SupportedLanguage | None
) -> dict[str, list[Node]]:
    # The call nodes behind callable_parameter_indices, in source order: where
    # the function itself invokes each parameter. A callback it receives runs
    # there, in this function's file, which is where the CALLS edge to that
    # callback is located (issue #2459).
    if (inputs := _callable_scan_inputs(func_node, language)) is None:
        return {}
    sites: dict[str, list[Node]] = {}
    _scan_invoked_parameters(
        inputs.body,
        set(inputs.names),
        set(),
        inputs.scan,
        inputs.scope_bound,
        sites,
    )
    for nodes in sites.values():
        nodes.sort(key=lambda node: node.start_byte)
    return sites


def go_parameter_names(func_node: Node) -> list[str]:
    # Ordered parameter names from the `parameters` list (the receiver of a
    # method is a separate field, so indices line up with call-site arguments).
    params = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return []
    names: list[str] = []
    for declaration in params.named_children:
        if declaration.type != cs.TS_GO_PARAMETER_DECLARATION:
            continue
        for child in declaration.children:
            if child.type == cs.TS_IDENTIFIER and (name := safe_decode_text(child)):
                names.append(name)
    return names
