"""Call-registered route extraction (issue #886).

JS and Go frameworks register routes through calls rather than decorators:
Express-style ``app.get('/path', handler)`` and ``app.route('/p').get(fn)``
chains, options-object calls whose single argument carries method, path and
handler (fastify ``.route({...})``, hapi ``server.route({...})``, in-house
``app.endpoint({...})`` gateways, issue #907), Go ``http.HandleFunc("/path",
h)`` (including Go 1.22 ``"GET /p"`` patterns) and echo/gin/chi-style verb
methods (``e.GET("/p", h)``). This walker recognises them in cached module
ASTs and yields ``METHOD /template`` registrations so they become ENDPOINT
resources like Python decorators do.

The evidence gate is twofold. The path must be a literal opening with ``/``
(or a ``VERB /path`` pattern); backtick literals count when they carry no
substitution, and Go paths may concatenate module-level string consts
(``BaseURL+"/x"``, the oapi-codegen shape, issue #909). And the call must
look like a SERVER registration, not an outbound client request: either its
receiver is bound in-module to a known framework factory (``express()``,
``echo.New()``, ``chi.NewRouter()``, ...), or ``http.Handle*`` with
``net/http`` imported, or one of its handler arguments is an inline
function, a function declared in the module, or (Go verb methods on a
concatenated path, the generated shape) an attribute expression like
``wrapper.GetMe``. A bare ``apiClient.get('/users')`` has none of these
and is ignored.

EXPOSES attribution is a ladder: a bare-identifier handler defined in the
module, else the registering call's enclosing function, else the module
itself, so the endpoint always stays anchored and the trace lands at the
wiring site.

Ceilings (each simply yields nothing, never a wrong template): sub-router
mounting (``app.use('/prefix', router)``), gorilla mux ``.Methods()``
chains, JS handlers referenced through imports or attributes, factories and
consts bound in a different module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import constants as cs

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tree_sitter import Node

METHOD_ANY = "ANY"

# Modules whose route registrations this pass can read.
ROUTE_CALL_LANGUAGES = frozenset(
    {cs.SupportedLanguage.JS, cs.SupportedLanguage.TS, cs.SupportedLanguage.GO}
)

# Only route-capable modules are fetched back from the graph; everything else
# is filtered out at the query so a large polyglot project stays cheap.
ROUTE_MODULE_EXTENSIONS = tuple(cs.JS_TS_ALL_EXTENSIONS) + tuple(cs.GO_EXTENSIONS)

CYPHER_PROJECT_MODULES = (
    "MATCH (m:Module) WHERE m.qualified_name STARTS WITH $project_prefix "
    "AND any(ext IN $extensions WHERE m.path ENDS WITH ext) "
    "RETURN m.qualified_name AS qualified_name, m.path AS path"
)

# Stale-route cleanup is keyed on the scanned MODULES so a module whose
# last registration disappeared still sheds its old EXPOSES edges.
# Ownership is the containment closure from the Module node, never a
# qualified-name prefix: foo.js (project.foo) can sit beside a foo/
# package whose functions share the prefix but belong to other modules.
CYPHER_DELETE_MODULE_EXPOSES = (
    "MATCH (mod:Module) WHERE mod.qualified_name IN $module_qns "
    "MATCH (mod)-[:DEFINES|DEFINES_METHOD*0..]->(f)"
    "-[e:EXPOSES]->(:Resource {kind: 'ENDPOINT'}) DELETE e"
)

_JS_VERBS = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "head": "HEAD",
    "options": "OPTIONS",
    "all": METHOD_ANY,
}
_JS_ROUTE_CHAIN = "route"
_JS_FRAMEWORK_FACTORIES = frozenset({"express", "express.Router"})
_JS_INLINE_HANDLER_TYPES = frozenset({cs.TS_FUNCTION_EXPRESSION, cs.TS_ARROW_FUNCTION})

# Options-object registrations (issue #907): one member call whose single
# argument is an object literal carrying method, path and handler, as in
# fastify `.route({...})`, hapi `server.route({...})` and in-house
# `app.endpoint({...})` gateways.
_JS_OPTIONS_PATH_KEYS = ("route", "url", "path")
_JS_OPTIONS_METHOD_KEY = "method"
_JS_OPTIONS_HANDLER_KEY = "handler"
# Only registration members accept the options-object shape; a client SDK's
# `.request({...})` can carry a local callback under a handler key too.
_JS_OPTIONS_MEMBERS = frozenset({"route", "endpoint"})

_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
# echo/gin use uppercase verb methods; chi uses Go-idiomatic PascalCase.
_GO_VERB_METHODS = {m: m for m in _HTTP_METHODS} | {
    m.capitalize(): m for m in _HTTP_METHODS
}
_GO_HANDLE_METHODS = frozenset({"HandleFunc", "Handle"})
_GO_FRAMEWORK_FACTORIES = frozenset(
    {
        "echo.New",
        "gin.Default",
        "gin.New",
        "chi.NewRouter",
        "mux.NewRouter",
        "http.NewServeMux",
    }
)
_GO_HTTP_PACKAGE = "http"
_GO_NET_HTTP_IMPORT = "net/http"
_GO_CONCAT_OPERATOR = "+"
# Go 1.22 ServeMux patterns: "GET /products/{id}".
_GO_PATTERN_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (/\S*)$")

_CHAIN_DEPTH_LIMIT = 8

_JS_PATH_LITERALS = (
    (cs.TS_STRING, cs.TS_STRING_FRAGMENT),
    (cs.TS_TEMPLATE_STRING, cs.TS_STRING_FRAGMENT),
)
_GO_PATH_LITERALS = (
    (cs.TS_GO_INTERPRETED_STRING, cs.TS_GO_INTERPRETED_STRING_CONTENT),
    (cs.TS_GO_RAW_STRING, cs.TS_GO_RAW_STRING_CONTENT),
)


@dataclass(frozen=True)
class RouteRegistration:
    method: str
    path: str
    handler_name: str | None  # bare-identifier handler, when present
    scope: str  # enclosing function chain, '' at module level


@dataclass(frozen=True)
class _ModuleEvidence:
    # Server-side evidence collected in a pre-pass: functions declared in the
    # module, receivers bound to a framework factory, whether net/http is
    # imported (which legitimises `http.Handle*`), and module-level string
    # consts (which resolve generated `BaseURL+"/x"` paths, issue #909).
    # Module scope only: a flat map of function locals could leak one
    # function's binding into another and mint a WRONG template.
    declared_functions: frozenset[str]
    framework_receivers: frozenset[str]
    net_http_imported: bool
    module_consts: Mapping[str, str]
    # Names bound to a composite literal of a module-declared type
    # (`wrapper := ServerInterfaceWrapper{...}`): the generated wiring
    # shape that legitimises selector handlers (issue #909).
    wrapper_receivers: frozenset[str]


def _decode(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode(cs.ENCODING_UTF8)


def _literal_path(
    node: Node | None, literal_types: tuple[tuple[str, str], ...]
) -> str | None:
    # A plain literal only; a template with a substitution is dynamic route
    # evidence and yields nothing.
    if node is None:
        return None
    for string_type, content_type in literal_types:
        if node.type != string_type:
            continue
        if any(
            child.type == cs.TS_TEMPLATE_SUBSTITUTION for child in node.named_children
        ):
            return None
        value = "".join(
            _decode(child) or ""
            for child in node.named_children
            if child.type == content_type
        )
        return value if value else None
    return None


def _call_args(call: Node) -> list[Node]:
    args = call.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
    if args is None:
        return []
    return [c for c in args.named_children if c.type != cs.TS_COMMENT]


def _handler_identifier(node: Node | None, identifier_type: str) -> str | None:
    if node is not None and node.type == identifier_type:
        return _decode(node)
    return None


def _receiver_is_framework(fn: Node, evidence: _ModuleEvidence) -> bool:
    receiver = fn.child_by_field_name(cs.TS_FIELD_OBJECT) or fn.child_by_field_name(
        cs.FIELD_OPERAND
    )
    if receiver is None or receiver.type not in (
        cs.TS_PY_IDENTIFIER,
        cs.TS_GO_IDENTIFIER,
    ):
        return False
    return (_decode(receiver) or "") in evidence.framework_receivers


def _handler_evidence(
    args: list[Node],
    evidence: _ModuleEvidence,
    inline_types: frozenset[str],
    identifier_type: str,
) -> bool:
    # Any post-path argument that is an inline function or a module-declared
    # function marks the call as a server registration.
    for arg in args:
        if arg.type in inline_types:
            return True
        name = _handler_identifier(arg, identifier_type)
        if name is not None and name in evidence.declared_functions:
            return True
    return False


def _js_chain_path(call: Node, depth: int = 0) -> str | None:
    # `app.route('/todos').get(list).post(create)`: walk the receiver chain
    # down to the `.route('/p')` call and return its literal path.
    if depth > _CHAIN_DEPTH_LIMIT:
        return None
    fn = call.child_by_field_name(cs.FIELD_FUNCTION)
    if fn is None or fn.type != cs.TS_MEMBER_EXPRESSION:
        return None
    prop = _decode(fn.child_by_field_name(cs.FIELD_PROPERTY))
    if prop == _JS_ROUTE_CHAIN:
        args = _call_args(call)
        return _literal_path(args[0] if args else None, _JS_PATH_LITERALS)
    if prop in _JS_VERBS:
        receiver = fn.child_by_field_name(cs.TS_FIELD_OBJECT)
        if receiver is not None and receiver.type == cs.TS_CALL_EXPRESSION:
            return _js_chain_path(receiver, depth + 1)
    return None


def _js_chain_root_is_framework(call: Node, evidence: _ModuleEvidence) -> bool:
    # The `.route('/p')` call at the bottom of the chain; its receiver is the
    # app/router object.
    node: Node | None = call
    for _ in range(_CHAIN_DEPTH_LIMIT + 1):
        if node is None or node.type != cs.TS_CALL_EXPRESSION:
            return False
        fn = node.child_by_field_name(cs.FIELD_FUNCTION)
        if fn is None or fn.type != cs.TS_MEMBER_EXPRESSION:
            return False
        if _decode(fn.child_by_field_name(cs.FIELD_PROPERTY)) == _JS_ROUTE_CHAIN:
            return _receiver_is_framework(fn, evidence)
        node = fn.child_by_field_name(cs.TS_FIELD_OBJECT)
    return False


def _js_registration(
    call: Node, scope: str, evidence: _ModuleEvidence
) -> RouteRegistration | None:
    fn = call.child_by_field_name(cs.FIELD_FUNCTION)
    if fn is None or fn.type != cs.TS_MEMBER_EXPRESSION:
        return None
    prop = _decode(fn.child_by_field_name(cs.FIELD_PROPERTY))
    method = _JS_VERBS.get(prop or "")
    if method is None:
        return None
    args = _call_args(call)
    return _js_direct_registration(
        fn, args, method, scope, evidence
    ) or _js_chained_registration(fn, args, method, scope, evidence)


def _js_direct_registration(
    fn: Node, args: list[Node], method: str, scope: str, evidence: _ModuleEvidence
) -> RouteRegistration | None:
    path = _literal_path(args[0] if args else None, _JS_PATH_LITERALS)
    if path is None or not path.startswith("/"):
        return None
    if not (
        _receiver_is_framework(fn, evidence)
        or _handler_evidence(
            args[1:], evidence, _JS_INLINE_HANDLER_TYPES, cs.TS_PY_IDENTIFIER
        )
    ):
        return None
    handler = _handler_identifier(
        args[1] if len(args) > 1 else None, cs.TS_PY_IDENTIFIER
    )
    return RouteRegistration(method, path, handler, scope)


def _js_chained_registration(
    fn: Node, args: list[Node], method: str, scope: str, evidence: _ModuleEvidence
) -> RouteRegistration | None:
    receiver = fn.child_by_field_name(cs.TS_FIELD_OBJECT)
    if receiver is None or receiver.type != cs.TS_CALL_EXPRESSION:
        return None
    path = _js_chain_path(receiver)
    if path is None or not path.startswith("/"):
        return None
    if not (
        _js_chain_root_is_framework(receiver, evidence)
        or _handler_evidence(
            args, evidence, _JS_INLINE_HANDLER_TYPES, cs.TS_PY_IDENTIFIER
        )
    ):
        return None
    handler = _handler_identifier(args[0] if args else None, cs.TS_PY_IDENTIFIER)
    return RouteRegistration(method, path, handler, scope)


def _object_entries(obj: Node) -> dict[str, Node]:
    # Key -> value node for an object literal; a shorthand property maps the
    # name to its own identifier node.
    entries: dict[str, Node] = {}
    for child in obj.named_children:
        if child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER:
            name = _decode(child)
            if name:
                entries[name] = child
            continue
        if child.type != cs.TS_PAIR:
            continue
        key = child.child_by_field_name(cs.FIELD_KEY)
        value = child.child_by_field_name(cs.FIELD_VALUE)
        if key is None or value is None:
            continue
        name = (
            _decode(key)
            if key.type == cs.TS_PROPERTY_IDENTIFIER
            else _literal_path(key, _JS_PATH_LITERALS)
        )
        if name:
            entries[name] = value
    return entries


def _options_methods(node: Node | None) -> list[str]:
    # A literal verb, or an array of them; anything dynamic yields nothing.
    if node is None:
        return []
    values = node.named_children if node.type == cs.TS_ARRAY else [node]
    methods = []
    for value in values:
        verb = (_literal_path(value, _JS_PATH_LITERALS) or "").upper()
        if verb in _HTTP_METHODS:
            methods.append(verb)
    return methods


def _js_options_registrations(
    call: Node, scope: str, evidence: _ModuleEvidence
) -> list[RouteRegistration]:
    fn = call.child_by_field_name(cs.FIELD_FUNCTION)
    if fn is None or fn.type != cs.TS_MEMBER_EXPRESSION:
        return []
    if _decode(fn.child_by_field_name(cs.FIELD_PROPERTY)) not in _JS_OPTIONS_MEMBERS:
        return []
    args = _call_args(call)
    if len(args) != 1 or args[0].type != cs.TS_OBJECT:
        return []
    entries = _object_entries(args[0])
    path = next(
        (
            p
            for key in _JS_OPTIONS_PATH_KEYS
            if (p := _literal_path(entries.get(key), _JS_PATH_LITERALS)) is not None
        ),
        None,
    )
    if path is None or not path.startswith("/"):
        return []
    methods = _options_methods(entries.get(_JS_OPTIONS_METHOD_KEY))
    if not methods:
        return []
    # The handler shape is the server evidence: an inline function, or an
    # identifier declared in the module. A client's request({url, method})
    # has neither; a handler referenced through an import stays a ceiling.
    handler = entries.get(_JS_OPTIONS_HANDLER_KEY)
    handler_name = (
        _decode(handler)
        if handler is not None
        and handler.type in (cs.TS_PY_IDENTIFIER, cs.TS_SHORTHAND_PROPERTY_IDENTIFIER)
        else None
    )
    if not (
        (handler is not None and handler.type in _JS_INLINE_HANDLER_TYPES)
        or (handler_name is not None and handler_name in evidence.declared_functions)
    ):
        return []
    return [RouteRegistration(m, path, handler_name, scope) for m in methods]


def _js_registrations(
    call: Node, scope: str, evidence: _ModuleEvidence
) -> list[RouteRegistration]:
    single = _js_registration(call, scope, evidence)
    if single is not None:
        return [single]
    return _js_options_registrations(call, scope, evidence)


def _go_registrations(
    call: Node, scope: str, evidence: _ModuleEvidence
) -> list[RouteRegistration]:
    single = _go_registration(call, scope, evidence)
    return [] if single is None else [single]


def _go_server_evidence(
    fn: Node,
    field: str,
    args: list[Node],
    evidence: _ModuleEvidence,
    concat_path: bool,
) -> bool:
    if _receiver_is_framework(fn, evidence):
        return True
    if (
        field in _GO_HANDLE_METHODS
        and evidence.net_http_imported
        and _decode(fn.child_by_field_name(cs.FIELD_OPERAND)) == _GO_HTTP_PACKAGE
    ):
        return True
    # A generated `router.Get(BaseURL+"/x", wrapper.GetMe)` hands an
    # attribute-expression handler to a verb method (issue #909). The
    # generated shape is narrow: a concatenated path AND a selector whose
    # receiver is bound to a composite literal of a module-declared type
    # (`wrapper := ServerInterfaceWrapper{...}`). A client passing
    # `opts.Header` after a concat path has no such binding and stays out.
    if (
        concat_path
        and field in _GO_VERB_METHODS
        and any(
            arg.type == cs.TS_GO_SELECTOR_EXPRESSION
            and (_decode(arg.child_by_field_name(cs.FIELD_OPERAND)) or "")
            in evidence.wrapper_receivers
            for arg in args[1:]
        )
    ):
        return True
    return _handler_evidence(
        args[1:], evidence, frozenset({cs.TS_GO_FUNC_LITERAL}), cs.TS_GO_IDENTIFIER
    )


def _go_path_value(
    node: Node | None, evidence: _ModuleEvidence, depth: int = 0
) -> str | None:
    # A literal, a module-level string const, or a `+` concatenation of
    # those (oapi-codegen keeps BaseURL adjacent to its routes, issue #909).
    if node is None or depth > _CHAIN_DEPTH_LIMIT:
        return None
    literal = _literal_path(node, _GO_PATH_LITERALS)
    if literal is not None:
        return literal
    if node.type == cs.TS_GO_IDENTIFIER:
        return evidence.module_consts.get(_decode(node) or "")
    if (
        node.type == cs.TS_BINARY_EXPRESSION
        and _decode(node.child_by_field_name(cs.FIELD_OPERATOR)) == _GO_CONCAT_OPERATOR
    ):
        left = _go_path_value(
            node.child_by_field_name(cs.TS_FIELD_LEFT), evidence, depth + 1
        )
        right = _go_path_value(
            node.child_by_field_name(cs.TS_FIELD_RIGHT), evidence, depth + 1
        )
        if left is not None and right is not None:
            return left + right
    return None


def _go_registration(
    call: Node, scope: str, evidence: _ModuleEvidence
) -> RouteRegistration | None:
    fn = call.child_by_field_name(cs.FIELD_FUNCTION)
    if fn is None or fn.type != cs.TS_GO_SELECTOR_EXPRESSION:
        return None
    field = _decode(fn.child_by_field_name(cs.FIELD_FIELD))
    args = _call_args(call)
    path_node = args[0] if args else None
    path = _go_path_value(path_node, evidence)
    if path is None or field is None:
        return None
    # Only a CONCATENATION is the generated registration shape; a bare
    # const identifier can just as well hold a client's URL.
    concat_path = path_node is not None and path_node.type == cs.TS_BINARY_EXPRESSION
    if not _go_server_evidence(fn, field, args, evidence, concat_path):
        return None
    handler = _handler_identifier(
        args[1] if len(args) > 1 else None, cs.TS_PY_IDENTIFIER
    )
    method = _GO_VERB_METHODS.get(field)
    if method is not None and path.startswith("/"):
        return RouteRegistration(method, path, handler, scope)
    if field in _GO_HANDLE_METHODS:
        pattern = _GO_PATTERN_RE.match(path)
        if pattern is not None:
            return RouteRegistration(pattern.group(1), pattern.group(2), handler, scope)
        if path.startswith("/"):
            return RouteRegistration(METHOD_ANY, path, handler, scope)
    return None


_JS_SCOPE_TYPES = frozenset({cs.TS_FUNCTION_DECLARATION, cs.TS_METHOD_DEFINITION})
_GO_SCOPE_TYPES = frozenset({"function_declaration", "method_declaration"})


def _factory_callee(node: Node) -> str | None:
    # The dotted/bare callee text of a declarator's value, when it is a call.
    if node.type not in (cs.TS_CALL_EXPRESSION, cs.TS_GO_CALL_EXPRESSION):
        return None
    return _decode(node.child_by_field_name(cs.FIELD_FUNCTION))


def _js_evidence(node: Node, declared: set[str], receivers: set[str]) -> None:
    if node.type == cs.TS_FUNCTION_DECLARATION:
        name = _decode(node.child_by_field_name(cs.TS_FIELD_NAME))
        if name:
            declared.add(name)
    elif node.type == cs.TS_VARIABLE_DECLARATOR:
        name = _handler_identifier(
            node.child_by_field_name(cs.TS_FIELD_NAME), cs.TS_PY_IDENTIFIER
        )
        value = node.child_by_field_name(cs.FIELD_VALUE)
        if name and value is not None:
            if _factory_callee(value) in _JS_FRAMEWORK_FACTORIES:
                receivers.add(name)
            elif value.type in _JS_INLINE_HANDLER_TYPES:
                # `const handler = async () => {}` declares a function just
                # as much as a function statement does.
                declared.add(name)


def _go_bound_names_and_value(node: Node) -> tuple[list[str], Node | None]:
    # `e := echo.New()` / `var r = chi.NewRouter()`: bound identifiers on the
    # left, the (single) value expression on the right.
    if node.type == cs.TS_GO_SHORT_VAR_DECLARATION:
        left = node.child_by_field_name(cs.TS_FIELD_LEFT)
        right = node.child_by_field_name(cs.TS_FIELD_RIGHT)
    elif node.type == cs.TS_GO_VAR_SPEC:
        left = node
        right = node.child_by_field_name(cs.FIELD_VALUE)
    else:
        return [], None
    names = [
        _decode(child) or ""
        for child in (left.named_children if left is not None else [])
        if child.type == cs.TS_GO_IDENTIFIER
    ]
    value = right
    if right is not None and right.type == cs.TS_GO_EXPRESSION_LIST:
        values = right.named_children
        value = values[0] if len(values) == 1 else None
    return names, value


def _go_module_const(node: Node) -> tuple[str, str] | None:
    # `const BaseURL = "/api/v1"` at FILE scope (also inside a const block):
    # (name, literal). Anything scoped, multi-named, or non-literal is None.
    if node.type != cs.TS_GO_CONST_SPEC:
        return None
    declaration = node.parent
    if (
        declaration is None
        or declaration.parent is None
        or declaration.parent.type != cs.TS_GO_SOURCE_FILE
    ):
        return None
    names = [c for c in node.named_children if c.type == cs.TS_GO_IDENTIFIER]
    value = node.child_by_field_name(cs.FIELD_VALUE)
    if value is not None and value.type == cs.TS_GO_EXPRESSION_LIST:
        values = value.named_children
        value = values[0] if len(values) == 1 else None
    if len(names) != 1 or value is None:
        return None
    name = _decode(names[0])
    literal = _literal_path(value, _GO_PATH_LITERALS)
    if name is None or literal is None:
        return None
    return name, literal


def _go_evidence(
    node: Node,
    declared: set[str],
    receivers: set[str],
    consts: dict[str, str],
    types: set[str],
    composites: list[tuple[str, str]],
) -> bool:
    # Returns True when the node imports net/http.
    if node.type == cs.TS_GO_FUNCTION_DECLARATION:
        name = _decode(node.child_by_field_name(cs.TS_FIELD_NAME))
        if name:
            declared.add(name)
        return False
    if node.type == cs.TS_GO_TYPE_SPEC:
        type_name = _decode(node.child_by_field_name(cs.TS_FIELD_NAME))
        if type_name:
            types.add(type_name)
        return False
    const = _go_module_const(node)
    if const is not None:
        consts[const[0]] = const[1]
        return False
    names, value = _go_bound_names_and_value(node)
    if names and value is not None:
        if _factory_callee(value) in _GO_FRAMEWORK_FACTORIES:
            receivers.update(n for n in names if n)
        elif value.type == cs.TS_GO_COMPOSITE_LITERAL and len(names) == 1:
            literal_type = value.child_by_field_name(cs.FIELD_TYPE)
            type_name = (
                _decode(literal_type)
                if literal_type is not None
                and literal_type.type == cs.TS_GO_TYPE_IDENTIFIER
                else None
            )
            if names[0] and type_name:
                composites.append((names[0], type_name))
        return False
    return node.type == cs.TS_GO_IMPORT_DECLARATION and f'"{_GO_NET_HTTP_IMPORT}"' in (
        _decode(node) or ""
    )


def _collect_evidence(root: Node, language: cs.SupportedLanguage) -> _ModuleEvidence:
    declared: set[str] = set()
    receivers: set[str] = set()
    consts: dict[str, str] = {}
    types: set[str] = set()
    composites: list[tuple[str, str]] = []
    net_http = False
    stack = [root]
    is_go = language is cs.SupportedLanguage.GO
    while stack:
        node = stack.pop()
        if is_go:
            net_http = (
                _go_evidence(node, declared, receivers, consts, types, composites)
                or net_http
            )
        else:
            _js_evidence(node, declared, receivers)
        stack.extend(node.named_children)
    # The type set only completes after the walk, so wrapper bindings are
    # judged here rather than in-flight.
    wrappers = frozenset(name for name, type_name in composites if type_name in types)
    return _ModuleEvidence(
        frozenset(declared), frozenset(receivers), net_http, consts, wrappers
    )


def _child_scope(scope: str, node: Node) -> str:
    name = _decode(node.child_by_field_name(cs.TS_FIELD_NAME)) or ""
    if scope and name:
        return f"{scope}{cs.SEPARATOR_DOT}{name}"
    return name or scope


def collect_route_registrations(
    root: Node, language: cs.SupportedLanguage
) -> list[RouteRegistration]:
    """All call-registered routes in one module AST."""
    evidence = _collect_evidence(root, language)
    if language is cs.SupportedLanguage.GO:
        scope_types = _GO_SCOPE_TYPES
        call_type = cs.TS_GO_CALL_EXPRESSION
        extract = _go_registrations
    else:
        scope_types = _JS_SCOPE_TYPES
        call_type = cs.TS_CALL_EXPRESSION
        extract = _js_registrations
    out: list[RouteRegistration] = []
    stack: list[tuple[Node, str]] = [(root, "")]
    while stack:
        node, scope = stack.pop()
        if node.type in scope_types:
            inner = _child_scope(scope, node)
            stack.extend((child, inner) for child in node.named_children)
            continue
        if node.type == call_type:
            out.extend(extract(node, scope, evidence))
        stack.extend((child, scope) for child in node.named_children)
    return out
