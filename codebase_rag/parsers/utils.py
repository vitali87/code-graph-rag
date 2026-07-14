from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from tree_sitter import Language, Node, Query, QueryCursor

from .. import constants as cs
from .. import logs
from ..types_defs import (
    ASTNode,
    CppDefinitionSpan,
    DeferredParentLink,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageQueries,
    NodeType,
    PropertyDict,
    SimpleNameLookup,
    TreeSitterNodeProtocol,
)
from ..utils.path_utils import cached_relative_path, cached_resolve_posix

if TYPE_CHECKING:
    from ..language_spec import LanguageSpec
    from ..services import IngestorProtocol
    from ..types_defs import FunctionRegistryTrieProtocol


def follow_reexports(
    qn: str,
    import_mapping: dict[str, dict[str, str]],
    function_registry: FunctionRegistryTrieProtocol,
) -> str:
    # (H) `from .pkg import sym` records the importer's name against the re-export
    # (H) module (pkg.sym), not the symbol's real definition (pkg.mod.sym), so a
    # (H) qn that is not itself registered may be a re-export. Follow the module's
    # (H) own import map one hop at a time until a registered symbol is reached,
    # (H) guarding against cycles.
    seen: set[str] = set()
    current = qn
    while (
        current
        and current not in seen
        and current not in function_registry
        and cs.SEPARATOR_DOT in current
    ):
        seen.add(current)
        module_qn, _, name = current.rpartition(cs.SEPARATOR_DOT)
        following = import_mapping.get(module_qn, {}).get(name)
        if not following or following == current:
            break
        current = following
    return current


def function_span_key(module_qn: str, node: Node) -> FunctionSpanKey:
    # (H) tree-sitter points are 0-based; recorded lines are 1-based.
    return (module_qn, node.start_point[0] + 1, node.start_point[1])


_CPP_SPAN_LANGUAGES = frozenset({cs.SupportedLanguage.C, cs.SupportedLanguage.CPP})


def record_cpp_definition_span(
    spans: dict[str, list[CppDefinitionSpan]],
    language: cs.SupportedLanguage | None,
    file_path: Path | None,
    repo_path: Path,
    node: ASTNode,
    label: str,
    qualified_name: str,
) -> None:
    # (H) Record the full line span of a C/C++ definition the tree-sitter pass
    # (H) ingested, keyed by relative path: the hybrid C++ frontend attributes
    # (H) each macro use to the tightest enclosing tree-sitter span after
    # (H) Pass 2 (macro cursors are TU-level, and libclang's own spans carry
    # (H) wrong-scheme qns wherever macros hide namespaces).
    if language not in _CPP_SPAN_LANGUAGES or file_path is None:
        return
    rel = cached_relative_path(file_path, repo_path).as_posix()
    spans.setdefault(rel, []).append(
        CppDefinitionSpan(
            node.start_point[0] + 1, node.end_point[0] + 1, label, qualified_name
        )
    )


_QUERY_CACHE: dict[tuple[Language, str], Query] = {}
_QUERY_LAST: tuple[tuple[Language, str], Query] | None = None


def get_cached_query(language_obj: Language, query_text: str) -> Query:
    # (H) Key by the Language itself, never id(): Language hashes by grammar
    # (H) pointer, so wrappers dedupe, and the dict pins the key so a GC'd
    # (H) wrapper's address can't be reused to serve a wrong-grammar Query.
    global _QUERY_LAST
    key = (language_obj, query_text)
    if _QUERY_LAST is not None and _QUERY_LAST[0] == key:
        return _QUERY_LAST[1]
    if key not in _QUERY_CACHE:
        _QUERY_CACHE[key] = Query(language_obj, query_text)
    result = _QUERY_CACHE[key]
    _QUERY_LAST = (key, result)
    return result


class FunctionCapturesResult(NamedTuple):
    lang_config: LanguageSpec
    captures: dict[str, list[ASTNode]]


def sorted_captures(cursor: QueryCursor, node: ASTNode) -> dict[str, list[ASTNode]]:
    # (H) tree-sitter v0.25 captures() returns nodes in non-deterministic order
    # (H) across invocations; sort by (start_byte, end_byte) for reproducibility.
    # (H) start_byte alone leaves nested same-start captures (the outer
    # (H) `Greeter().greet()` chain and its inner `Greeter()` call) in raw order,
    # (H) which flips between runs and swaps their emitted edges.
    raw = cursor.captures(node)
    result: dict[str, list[ASTNode]] = {}
    for name, nodes in raw.items():
        if len(nodes) <= 1:
            result[name] = nodes
        else:
            is_sorted = True
            prev_key = _span_key(nodes[0])
            for i in range(1, len(nodes)):
                cur_key = _span_key(nodes[i])
                if cur_key < prev_key:
                    is_sorted = False
                    break
                prev_key = cur_key
            result[name] = nodes if is_sorted else sorted(nodes, key=_span_key)
    return result


def _span_key(n: ASTNode) -> tuple[int, int]:
    return (n.start_byte, n.end_byte)


def get_function_captures(
    root_node: ASTNode,
    language: cs.SupportedLanguage,
    queries: dict[cs.SupportedLanguage, LanguageQueries],
) -> FunctionCapturesResult | None:
    lang_queries = queries[language]
    lang_config = lang_queries[cs.QUERY_CONFIG]

    if not (query := lang_queries[cs.QUERY_FUNCTIONS]):
        return None

    cursor = QueryCursor(query)
    captures = sorted_captures(cursor, root_node)
    return FunctionCapturesResult(lang_config, captures)


def extract_modifiers_and_decorators(
    node: ASTNode, lang_queries: LanguageQueries
) -> tuple[list[str], list[str]]:
    query = lang_queries.get(cs.QUERY_HIGHLIGHTS)
    if not query:
        return [], []

    cursor = get_query_cursor(query)

    body_node = node.child_by_field_name(cs.FIELD_BODY)
    header_end_byte = body_node.start_byte if body_node else node.end_byte

    target_node = node
    if node.parent and node.parent.type in (
        cs.TS_PY_DECORATED_DEFINITION,
        cs.TS_EXPORT_STATEMENT,
    ):
        target_node = node.parent

    query_nodes = [target_node]
    curr_sibling = target_node.prev_named_sibling
    while curr_sibling and (
        curr_sibling.type == cs.TS_RS_ATTRIBUTE_ITEM
        or (
            target_node.type == cs.TS_METHOD_DEFINITION
            and curr_sibling.type == cs.TS_DECORATOR
        )
    ):
        query_nodes.insert(0, curr_sibling)
        curr_sibling = curr_sibling.prev_named_sibling

    modifiers: list[str] = []
    decorators: list[str] = []

    for q_node in query_nodes:
        if q_node == target_node:
            cursor.set_byte_range(q_node.start_byte, header_end_byte)
        else:
            cursor.set_byte_range(q_node.start_byte, q_node.end_byte)

        captures = sorted_captures(cursor, q_node)
        for name, nodes in captures.items():
            if (
                name.startswith(cs.CAPTURE_KEYWORD_MODIFIER)
                or name == cs.CAPTURE_KEYWORD
            ):
                for n in nodes:
                    text = safe_decode_text(n)
                    if (
                        text
                        and text not in modifiers
                        and text not in cs.EXCLUDED_KEYWORDS
                    ):
                        modifiers.append(text)
            elif name.startswith(cs.CAPTURE_ATTRIBUTE) or name.startswith(
                cs.CAPTURE_FUNCTION_DECORATOR
            ):
                for n in nodes:
                    text = safe_decode_text(n)
                    if text and text not in decorators:
                        decorators.append(text)

    return modifiers, decorators


@lru_cache(maxsize=50000)
def _cached_decode_bytes(text_bytes: bytes) -> str:
    return text_bytes.decode(cs.ENCODING_UTF8)


def safe_decode_text(node: ASTNode | TreeSitterNodeProtocol | None) -> str | None:
    if node is None or (text_bytes := node.text) is None:
        return None
    if isinstance(text_bytes, bytes):
        return _cached_decode_bytes(text_bytes)
    return str(text_bytes)


def get_query_cursor(query: Query) -> QueryCursor:
    return QueryCursor(query)


def safe_decode_with_fallback(node: ASTNode | None, fallback: str = "") -> str:
    return result if (result := safe_decode_text(node)) is not None else fallback


def contains_node(parent: ASTNode, target: ASTNode) -> bool:
    return parent == target or any(
        contains_node(child, target) for child in parent.children
    )


def _decorator_tail_names(decorators: list[str]) -> set[str]:
    return {
        decorator.lstrip("@#[]() ")
        .split("(")[0]
        .split(cs.SEPARATOR_DOT)[-1]
        .rstrip(")] ")
        for decorator in decorators
    }


def _is_property_decorator(decorators: list[str]) -> bool:
    return bool(_decorator_tail_names(decorators) & cs.PROPERTY_DECORATORS)


def _is_abstract_decorator(decorators: list[str]) -> bool:
    return bool(_decorator_tail_names(decorators) & cs.ABSTRACT_DECORATORS)


_PY_NAMED_PARAMETERS = frozenset(
    {cs.TS_PY_DEFAULT_PARAMETER, cs.TS_PY_TYPED_DEFAULT_PARAMETER}
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
    # (H) Node types that let the invoked-parameter scan work per language: the call
    # (H) node whose `function` field names the callee, the identifier node for a bare
    # (H) callee, the nested closure scopes that capture an enclosing parameter, and
    # (H) the class-like scopes that are not closures (skipped, never descended).
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
_JS_TS_TYPED_PARAMETERS = frozenset(
    {cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER}
)
_CPP_CLOSURE_SCOPES = frozenset({cs.TS_CPP_LAMBDA_EXPRESSION})
_CPP_SCAN = _CallableScanConfig(
    cs.TS_CPP_CALL_EXPRESSION,
    cs.CppNodeType.IDENTIFIER,
    _CPP_CLOSURE_SCOPES,
    frozenset(),
)
_CPP_PARAMETER_DECLARATIONS = frozenset(
    {
        cs.CppNodeType.PARAMETER_DECLARATION,
        cs.CppNodeType.OPTIONAL_PARAMETER_DECLARATION,
    }
)


def _python_invoked_parameter_names(body_node: Node, candidates: set[str]) -> set[str]:
    invoked: set[str] = set()
    _scan_invoked_parameters(
        body_node, set(candidates), invoked, _PY_SCAN, _python_scope_bound_names
    )
    return invoked


def _go_invoked_parameter_names(body_node: Node, candidates: set[str]) -> set[str]:
    invoked: set[str] = set()
    _scan_invoked_parameters(
        body_node, set(candidates), invoked, _GO_SCAN, _go_scope_bound_names
    )
    return invoked


def _js_ts_invoked_parameter_names(body_node: Node, candidates: set[str]) -> set[str]:
    invoked: set[str] = set()
    _scan_invoked_parameters(
        body_node, set(candidates), invoked, _JS_SCAN, _js_ts_scope_bound_names
    )
    return invoked


def _cpp_invoked_parameter_names(body_node: Node, candidates: set[str]) -> set[str]:
    invoked: set[str] = set()
    _scan_invoked_parameters(
        body_node, set(candidates), invoked, _CPP_SCAN, _cpp_scope_bound_names
    )
    return invoked


def _cpp_scope_bound_names(scope_node: Node) -> set[str]:
    # (H) A C++ lambda's own parameters shadow a same-named captured parameter of the
    # (H) enclosing function, so they must be subtracted before scanning the lambda
    # (H) body -- otherwise the lambda invoking its own `cb` looks like an invocation
    # (H) of the outer `cb`. The lambda's parameters hang off its `declarator` (an
    # (H) abstract_function_declarator whose `parameters` list mirrors a function's).
    declarator = scope_node.child_by_field_name(cs.FIELD_DECLARATOR)
    return set(_cpp_declarator_param_names(declarator))


def cpp_declarator_name(declarator: Node | None) -> str | None:
    # (H) Unwrap pointer/reference/parenthesized/function declarators down to the
    # (H) bound identifier (`int (*cb)()` -> cb, `T& x` -> x, `Fn cb` -> cb).
    current = declarator
    while current is not None:
        if current.type in (
            cs.CppNodeType.IDENTIFIER,
            cs.CppNodeType.FIELD_IDENTIFIER,
        ):
            return safe_decode_text(current)
        if (inner := current.child_by_field_name(cs.FIELD_DECLARATOR)) is not None:
            current = inner
            continue
        current = next(
            (
                child
                for child in current.children
                if child.is_named and cs.CPP_DECLARATOR_SUFFIX in child.type
            ),
            None,
        )
    return None


def cpp_parameter_names(func_node: Node) -> list[str]:
    # (H) Ordered parameter names from the function declarator's parameter_list,
    # (H) unwrapping each parameter's declarator to its bound identifier.
    declarator = func_node.child_by_field_name(cs.FIELD_DECLARATOR)
    func_declarator = _find_descendant(declarator, cs.CppNodeType.FUNCTION_DECLARATOR)
    return _cpp_declarator_param_names(func_declarator)


def _cpp_declarator_param_names(declarator: Node | None) -> list[str]:
    # (H) Bound parameter names from a (function|abstract_function) declarator's
    # (H) `parameters` list, unwrapping each parameter's declarator to its identifier.
    if declarator is None:
        return []
    params = declarator.child_by_field_name(cs.KEY_PARAMETERS)
    if params is None:
        return []
    names: list[str] = []
    for declaration in params.named_children:
        if declaration.type not in _CPP_PARAMETER_DECLARATIONS:
            continue
        param_declarator = declaration.child_by_field_name(cs.FIELD_DECLARATOR)
        if (name := cpp_declarator_name(param_declarator)) is not None:
            names.append(name)
    return names


def _find_descendant(node: Node | None, node_type: str) -> Node | None:
    if node is None:
        return None
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        if current.type == node_type:
            return current
        stack.extend(current.children)
    return None


def _js_ts_scope_bound_names(scope_node: Node) -> set[str]:
    # (H) A nested arrow/function's own parameters shadow a same-named captured
    # (H) parameter of the enclosing function.
    return set(js_ts_parameter_names(scope_node))


def js_ts_parameter_names(func_node: Node) -> list[str]:
    # (H) Ordered parameter names. TypeScript wraps each in required_parameter /
    # (H) optional_parameter (name under the `pattern` field); JavaScript uses a bare
    # (H) identifier. A single-parameter arrow without parens has no formal_parameters
    # (H) list -- its parameter is on the `parameter` field. Destructuring patterns
    # (H) bind no single callable name and are skipped.
    names: list[str] = []
    params = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is not None:
        for child in params.named_children:
            if child.type == cs.TS_IDENTIFIER:
                if name := safe_decode_text(child):
                    names.append(name)
            elif child.type in _JS_TS_TYPED_PARAMETERS:
                pattern = child.child_by_field_name(cs.TS_FIELD_PATTERN)
                if (
                    pattern is not None
                    and pattern.type == cs.TS_IDENTIFIER
                    and (name := safe_decode_text(pattern))
                ):
                    names.append(name)
        return names
    single = func_node.child_by_field_name(cs.TS_FIELD_PARAMETER)
    if single is not None and single.type == cs.TS_IDENTIFIER:
        if name := safe_decode_text(single):
            names.append(name)
    return names


def _scan_invoked_parameters(
    scope_node: Node,
    candidates: set[str],
    invoked: set[str],
    config: _CallableScanConfig,
    bound_names: Callable[[Node], set[str]],
) -> None:
    # (H) Mark a candidate parameter invoked when it is called by bare name in this
    # (H) lexical scope. Descend into nested closures that CAPTURE a candidate (do not
    # (H) rebind it) so `outer(cb) { inner() { cb() } }` still attributes cb to outer
    # (H) -- the closure form used by decorator/formatter factories. A nested scope's
    # (H) own bound names are removed first so a shadowing local cannot masquerade as
    # (H) the captured outer parameter. Class-like scopes are skipped entirely.
    if not candidates:
        return
    stack: list[Node] = [scope_node]
    while stack:
        node = stack.pop()
        for child in node.children:
            if child.type == config.call_type:
                fn = child.child_by_field_name(cs.FIELD_FUNCTION)
                if (
                    fn is not None
                    and fn.type == config.identifier_type
                    and (name := safe_decode_text(fn)) in candidates
                ):
                    invoked.add(name)
            if child.type in config.closure_types:
                inner = candidates - bound_names(child)
                _scan_invoked_parameters(child, inner, invoked, config, bound_names)
                continue
            if child.type in config.opaque_types:
                continue
            stack.append(child)


def _go_scope_bound_names(scope_node: Node) -> set[str]:
    # (H) A nested func_literal's own parameters shadow a same-named captured
    # (H) parameter of the enclosing function.
    return set(go_parameter_names(scope_node))


def _python_scope_bound_names(scope_node: Node) -> set[str]:
    # (H) Names a nested function/lambda binds itself, which shadow a same-named
    # (H) captured parameter of an enclosing function: its parameters plus local
    # (H) assignment targets and nested def/class names in its body.
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
            child_type = child.type
            if child_type in _PY_SCOPE_BOUNDARIES:
                # (H) A nested def/class NAME binds here, but its body has its own
                # (H) scope; record the name and do not descend. A decorated_definition
                # (H) has no `name` field of its own -- the name is on the inner
                # (H) function/class definition it wraps.
                named = child
                if child_type == cs.TS_PY_DECORATED_DEFINITION:
                    named = next(
                        (
                            c
                            for c in child.children
                            if c.type
                            in (
                                cs.TS_PY_FUNCTION_DEFINITION,
                                cs.TS_PY_CLASS_DEFINITION,
                            )
                        ),
                        child,
                    )
                name_node = named.child_by_field_name(cs.FIELD_NAME)
                if name_node is not None and (name := safe_decode_text(name_node)):
                    out.add(name)
                continue
            if child_type == cs.TS_PY_ASSIGNMENT:
                left = child.child_by_field_name(cs.TS_FIELD_LEFT)
                if left is not None:
                    _python_collect_target_identifiers(left, out)
            stack.append(child)


def _python_collect_target_identifiers(node: Node, out: set[str]) -> None:
    if node.type == cs.TS_PY_IDENTIFIER:
        if name := safe_decode_text(node):
            out.add(name)
        return
    for child in node.children:
        _python_collect_target_identifiers(child, out)


def python_parameter_names(func_node: Node) -> list[str]:
    # (H) Ordered parameter names with a leading self/cls dropped, so positions line
    # (H) up with how call-site arguments map to parameters for bound methods.
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


def callable_parameter_indices(
    func_node: Node, language: cs.SupportedLanguage | None
) -> dict[str, int]:
    # (H) Maps each parameter that is invoked as a call inside the function body
    # (H) to its positional index in the call-site argument list (self/cls
    # (H) dropped so the index lines up with how bound methods are invoked).
    if language == cs.SupportedLanguage.PYTHON:
        names = python_parameter_names(func_node)
        invoke = _python_invoked_parameter_names
    elif language == cs.SupportedLanguage.GO:
        names = go_parameter_names(func_node)
        invoke = _go_invoked_parameter_names
    elif language in cs.JS_TS_LANGUAGES:
        names = js_ts_parameter_names(func_node)
        invoke = _js_ts_invoked_parameter_names
    elif language == cs.SupportedLanguage.CPP:
        names = cpp_parameter_names(func_node)
        invoke = _cpp_invoked_parameter_names
    else:
        return {}
    body_node = func_node.child_by_field_name(cs.FIELD_BODY)
    if body_node is None or not names:
        return {}
    invoked = invoke(body_node, set(names))
    if not invoked:
        return {}
    return {name: index for index, name in enumerate(names) if name in invoked}


def go_parameter_names(func_node: Node) -> list[str]:
    # (H) Ordered parameter names from the `parameters` list (the receiver of a
    # (H) method is a separate field, so indices line up with call-site arguments).
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


def _js_ts_field_member_name(
    node: ASTNode, language: cs.SupportedLanguage | None
) -> str | None:
    # (H) The binding name of a JS/TS class-field arrow / fn-expr whose enclosing
    # (H) field definition holds it as its `value` (`helper = () => ...`), so the
    # (H) member is modelled as class_qn.helper. None for other languages/shapes.
    if language not in cs.JS_TS_LANGUAGES:
        return None
    if node.type not in (cs.TS_ARROW_FUNCTION, cs.TS_FUNCTION_EXPRESSION):
        return None
    parent = node.parent
    # (H) `==` not `is`: py-tree-sitter returns a fresh Node wrapper on each access,
    # (H) so identity comparison always fails; Node equality compares the node id.
    if parent is None or parent.child_by_field_name(cs.FIELD_VALUE) != node:
        return None
    name_node = parent.child_by_field_name(cs.FIELD_NAME)
    if name_node is None or name_node.type not in (
        cs.TS_IDENTIFIER,
        cs.TS_PROPERTY_IDENTIFIER,
    ):
        return None
    return safe_decode_text(name_node)


def _method_end_line(node: ASTNode, language: cs.SupportedLanguage | None) -> int:
    if language == cs.SupportedLanguage.DART:
        from .dart import dart_definition_end_point

        return dart_definition_end_point(node)[0] + 1
    return node.end_point[0] + 1


def ingest_method(
    method_node: ASTNode,
    container_qn: str,
    container_type: cs.NodeLabel,
    ingestor: IngestorProtocol,
    function_registry: FunctionRegistryTrieProtocol,
    simple_name_lookup: SimpleNameLookup,
    get_docstring_func: Callable[[ASTNode], str | None],
    language: cs.SupportedLanguage | None = None,
    lang_queries: LanguageQueries | None = None,
    method_qualified_name: str | None = None,
    file_path: Path | None = None,
    repo_path: Path | None = None,
    defer_containment: list[DeferredParentLink] | None = None,
    module_qn: str | None = None,
    external_override_names: frozenset[str] = frozenset(),
) -> str | None:
    # (H) Returns the registered method qn (post register_unique_qn, so with any
    # (H) @line dedup suffix) so a caller can wire further edges to the exact node --
    # (H) e.g. an anonymous-class override method's OVERRIDES edge to its base. Returns
    # (H) None only when the method has no resolvable name and nothing was registered.
    if language == cs.SupportedLanguage.CPP:
        from .cpp import utils as cpp_utils

        method_name = cpp_utils.extract_function_name(method_node)
        if not method_name:
            return None
    elif language == cs.SupportedLanguage.CSHARP:
        # (H) Operators expose no `name` field (they would be dropped) and a
        # (H) destructor's `name` field collides with the constructor; synthesize
        # (H) the leaf so both register with the same name the FQN walk uses.
        from .csharp import utils as csharp_utils

        method_name = csharp_utils.synthesize_method_name(method_node)
        if not method_name:
            return None
    elif language == cs.SupportedLanguage.DART:
        # (H) Constructors/factories expose no `name` field; take the last bare
        # (H) identifier (`factory C.empty` -> `empty`) so they are not dropped.
        from .dart import dart_get_name

        if not (method_name := dart_get_name(method_node)):
            return None
    elif (method_name_node := method_node.child_by_field_name(cs.FIELD_NAME)) is None:
        # (H) A JS/TS class-field arrow / fn-expr (`helper = () => ...`) has no name
        # (H) field on the function node; take the binding name from the enclosing
        # (H) field definition so it is modelled as a member instead of dropped.
        if not (method_name := _js_ts_field_member_name(method_node, language)):
            return None
    elif (text := method_name_node.text) is None:
        return None
    else:
        method_name = text.decode(cs.ENCODING_UTF8)

    if language == cs.SupportedLanguage.CSHARP:
        # (H) Skip a leading `#if [Attr] #endif` directive so the start line is the
        # (H) conditional attribute, not the `#if` line (matches Roslyn's span).
        from .csharp import utils as csharp_utils

        method_start_line = csharp_utils.definition_start_line(method_node)
    else:
        method_start_line = method_node.start_point[0] + 1

    method_qn = method_qualified_name or f"{container_qn}.{method_name}"
    if language != cs.SupportedLanguage.CPP:
        method_qn = function_registry.register_unique_qn(method_qn, method_start_line)

    decorators = []
    modifiers = []
    if lang_queries:
        modifiers, decorators = extract_modifiers_and_decorators(
            method_node, lang_queries
        )

    # (H) Local import breaks the export_detection -> cpp.utils -> utils cycle.
    from . import export_detection

    method_props: PropertyDict = {
        cs.KEY_QUALIFIED_NAME: method_qn,
        cs.KEY_NAME: method_name,
        cs.KEY_MODIFIERS: modifiers,
        cs.KEY_DECORATORS: decorators,
        cs.KEY_START_LINE: method_start_line,
        # (H) Dart method signatures end before their sibling function_body;
        # (H) extend the span over the body (no-op for other languages).
        cs.KEY_END_LINE: _method_end_line(method_node, language),
        cs.KEY_DOCSTRING: get_docstring_func(method_node),
        cs.KEY_IS_EXPORTED: (
            export_detection.is_exported(method_node, method_name, language)
            if language is not None
            else False
        ),
    }
    if file_path is not None and repo_path is not None:
        method_props[cs.KEY_PATH] = cached_relative_path(
            file_path, repo_path
        ).as_posix()
        method_props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)

    # (H) Persist @property status on the node so an incremental rebuild can restore
    # (H) the registry's property-name set for unchanged files (it re-marks from this
    # (H) flag rather than re-parsing decorators); property-dispatch call resolution
    # (H) depends on it, so without persistence those edges drop (issue #532 parity).
    is_property = _is_property_decorator(decorators)
    if is_property:
        method_props[cs.KEY_IS_PROPERTY] = True

    # (H) Overriding a method of an EXTERNAL stdlib base (click's TextWrapper
    # (H) subclass overriding textwrap's _wrap_chunks): the base's machinery invokes
    # (H) it, so the dead-code surfaces root this property.
    if method_name in external_override_names:
        method_props[cs.KEY_OVERRIDES_EXTERNAL] = True

    logger.info(logs.METHOD_FOUND.format(name=method_name, qn=method_qn))
    ingestor.ensure_node_batch(cs.NodeLabel.METHOD, method_props)
    function_registry[method_qn] = NodeType.METHOD
    if is_property:
        function_registry.mark_property(method_qn)
    if _is_abstract_decorator(decorators):
        function_registry.mark_abstract(method_qn)
    function_registry.mark_callable_params(
        method_qn, callable_parameter_indices(method_node, language)
    )
    simple_name_lookup[method_name].add(method_qn)

    # (H) A container that may never register (a Rust impl on a primitive type)
    # (H) defers so the edge is verified once every pass has run, falling back
    # (H) to the module rather than a phantom the database would drop.
    if defer_containment is not None and module_qn is not None:
        defer_containment.append(
            DeferredParentLink(
                parent_label_guess=container_type,
                parent_qn=container_qn,
                child_label=cs.NodeLabel.METHOD,
                child_qn=method_qn,
                module_qn=module_qn,
                rel_type=cs.RelationshipType.DEFINES_METHOD.value,
            )
        )
        return method_qn

    # (H) The DEFINES_METHOD parent is matched in the graph by LABEL +
    # (H) qualified_name, so it must carry the container's real node label. Callers
    # (H) pass Class by default, but a trait/interface (Interface) or enum (Enum)
    # (H) container would then never match, dropping the containment edge. Prefer
    # (H) the label the container was actually registered with.
    container_label = container_type
    registered = function_registry.get(container_qn)
    if registered is not None and registered != NodeType.METHOD:
        container_label = cs.NodeLabel(registered.value)

    ingestor.ensure_relationship_batch(
        (container_label, cs.KEY_QUALIFIED_NAME, container_qn),
        cs.RelationshipType.DEFINES_METHOD,
        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
    )
    return method_qn


def module_function_props(
    function_qn: str,
    function_name: str,
    function_node: ASTNode,
    docstring: str | None,
    file_path: Path | None,
    repo_path: Path | None,
) -> PropertyDict:
    """Standard Function node properties for module-scoped JS/TS functions."""
    # (H) Lazy import: export_detection reaches back into this module through
    # (H) cpp.utils, so a top-level import would be circular.
    from . import export_detection

    props: PropertyDict = {
        cs.KEY_QUALIFIED_NAME: function_qn,
        cs.KEY_NAME: function_name,
        cs.KEY_MODIFIERS: [],
        cs.KEY_DECORATORS: [],
        cs.KEY_START_LINE: function_node.start_point[0] + 1,
        cs.KEY_END_LINE: function_node.end_point[0] + 1,
        cs.KEY_DOCSTRING: docstring,
        # (H) JS/TS only (per this helper's contract), so the JS branch of the
        # (H) language dispatch applies regardless of which of the two it is.
        cs.KEY_IS_EXPORTED: export_detection.is_exported(
            function_node, function_name, cs.SupportedLanguage.JS
        ),
    }
    if file_path is not None and repo_path is not None:
        props[cs.KEY_PATH] = cached_relative_path(file_path, repo_path).as_posix()
        props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
    return props


def ingest_exported_function(
    function_node: ASTNode,
    function_name: str,
    module_qn: str,
    export_type: str,
    ingestor: IngestorProtocol,
    function_registry: FunctionRegistryTrieProtocol,
    simple_name_lookup: SimpleNameLookup,
    get_docstring_func: Callable[[ASTNode], str | None],
    is_export_inside_function_func: Callable[[ASTNode], bool],
    file_path: Path | None,
    repo_path: Path | None,
) -> str | None:
    # (H) Returns the registered qn (None when skipped) so the caller can claim
    # (H) the function node's span against later registration passes.
    if is_export_inside_function_func(function_node):
        return None

    function_qn = f"{module_qn}.{function_name}"
    # (H) The definition pass already ingests an exported function / const-arrow at
    # (H) its natural qn. Re-registering here would collide and mint a spurious
    # (H) `qn@line` duplicate node, onto which call resolution then binds (mangling
    # (H) the callee qn). If the natural qn already exists, the node is done.
    if function_qn in function_registry:
        return None
    # (H) Same for a nested export (TS namespace / module block): the main pass
    # (H) already ingested it under its nested qn (e.g. lib.geo.helper), so a
    # (H) module-level re-ingest would mint a phantom duplicate node plus a
    # (H) spurious Module-DEFINES edge. Walk ancestors instead of matching
    # (H) simple names so a top-level export may share a name with an
    # (H) unrelated method elsewhere in the module.
    current = function_node.parent
    while current is not None:
        if current.type in (cs.TS_INTERNAL_MODULE, cs.TS_MODULE):
            return None
        current = current.parent
    function_qn = function_registry.register_unique_qn(
        function_qn, function_node.start_point[0] + 1
    )

    function_props = module_function_props(
        function_qn,
        function_name,
        function_node,
        get_docstring_func(function_node),
        file_path,
        repo_path,
    )
    function_props[cs.KEY_IS_EXPORTED] = True

    logger.info(
        logs.EXPORT_FOUND.format(
            export_type=export_type, name=function_name, qn=function_qn
        )
    )
    ingestor.ensure_node_batch(cs.NodeLabel.FUNCTION, function_props)
    function_registry[function_qn] = NodeType.FUNCTION
    simple_name_lookup[function_name].add(function_qn)
    ingestor.ensure_relationship_batch(
        (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
        cs.RelationshipType.DEFINES,
        (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, function_qn),
    )
    return function_qn


def is_method_node(func_node: ASTNode, lang_config: LanguageSpec) -> bool:
    current = func_node.parent
    if not isinstance(current, Node):
        return False

    class_types = lang_config.class_node_types
    func_types = lang_config.function_node_types
    module_types = lang_config.module_node_types
    body_field = cs.FIELD_BODY

    while current is not None:
        current_type = current.type
        if current_type in module_types:
            return False
        if current_type in class_types:
            return True
        if (
            current_type in func_types
            and current.child_by_field_name(body_field) is not None
        ):
            return False
        current = current.parent
    return False
