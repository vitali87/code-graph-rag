"""Method and exported-function ingestion: the Method and Function nodes a
definition pass writes, and the edges, registrations and side facts that go
with them. Kept beside function_ingest and class_ingest rather than in the
general helpers of parsers/utils.py, where it used to live."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..types_defs import (
    ASTNode,
    DeferredParentLink,
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    OverloadSignature,
    PropertyDict,
    SimpleNameLookup,
)
from ..utils.path_utils import cached_relative_path, cached_resolve_posix
from .anchor_hash import anchor_hash_props
from .ast_fingerprint import fingerprint_props
from .endpoints import emit_endpoints, queue_endpoints
from .node_text import safe_decode_text
from .utils import (
    _decorator_tail_names,
    _is_abstract_member,
    callable_parameter_indices,
    decorated_start_props,
    extract_modifiers_and_decorators,
    is_static_decorator,
    python_positional_parameter_names,
    warn_if_name_truncated,
)

if TYPE_CHECKING:
    from ..services import IngestorProtocol


def _member_name_start_point(node: Node) -> tuple[int, int]:
    """(line, column) of the definition's NAME token, falling back to the
    node start (issue #1240: the persisted coordinates let incremental runs
    rehydrate both the span key and Go's name-token alias, whose name can
    sit on a later line than the declaration start)."""
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    anchor = name_node if name_node is not None else node
    return anchor.start_point[0] + 1, anchor.start_point[1]


def _method_end_line(node: ASTNode, language: cs.SupportedLanguage | None) -> int:
    if language == cs.SupportedLanguage.DART:
        from .dart import utils as dart_utils

        return dart_utils.definition_end_point(node)[0] + 1
    return node.end_point[0] + 1


def _resolve_method_name(
    method_node: ASTNode, language: cs.SupportedLanguage, skip_cpp_artifact_check: bool
) -> str | None:
    if language == cs.SupportedLanguage.CPP:
        from .cpp import utils as cpp_utils

        # Inside an intact class body a macro invocation parsed as a type-less
        # member (`FMT_CATCH(...) {}`) can never be a ctor of another class, so
        # the shape check alone is decisive here. skip_cpp_artifact_check: the
        # orphan-ctor flush already ran the class-registry tiebreak (a zero-param
        # orphan ctor SHARES the artifact shape and must not be re-dropped here).
        if not skip_cpp_artifact_check and cpp_utils.is_macro_invocation_artifact(
            method_node
        ):
            return None
        return cpp_utils.extract_function_name(method_node) or None
    if language == cs.SupportedLanguage.CSHARP:
        # Operators expose no `name` field (they would be dropped) and a
        # destructor's `name` field collides with the constructor; synthesize
        # the leaf so both register with the same name the FQN walk uses.
        from .csharp import utils as csharp_utils

        return csharp_utils.synthesize_method_name(method_node) or None
    if language == cs.SupportedLanguage.DART:
        # Constructors/factories expose no `name` field; take the last bare
        # identifier (`factory C.empty` -> `empty`) so they are not dropped.
        from .dart import utils as dart_utils

        return dart_utils.get_name(method_node) or None
    if (method_name_node := method_node.child_by_field_name(cs.FIELD_NAME)) is None:
        # A JS/TS class-field arrow / fn-expr (`helper = () => ...`) has no name
        # field on the function node; take the binding name from the enclosing
        # field definition so it is modelled as a member instead of dropped.
        return _js_ts_field_member_name(method_node, language) or None
    if (text := method_name_node.text) is None:
        return None
    return text.decode(cs.ENCODING_UTF8)


def _method_start_point(
    method_node: ASTNode, language: cs.SupportedLanguage
) -> tuple[int, int]:
    if language == cs.SupportedLanguage.CSHARP:
        # Skip a leading `#if [Attr] #endif` directive so the start line is the
        # conditional attribute, not the `#if` line (matches Roslyn's span).
        from .csharp import utils as csharp_utils

        return csharp_utils.definition_start_point(method_node)
    return method_node.start_point[0] + 1, method_node.start_point[1]


def _method_is_property(
    method_node: ASTNode, language: cs.SupportedLanguage, decorators: list[str]
) -> bool:
    # Persist @property status on the node so an incremental rebuild can restore
    # the registry's property-name set for unchanged files (it re-marks from this
    # flag rather than re-parsing decorators); property-dispatch call resolution
    # depends on it, so without persistence those edges drop (issue #532 parity).
    # A C# property_declaration IS a property by grammar (no decorator to
    # inspect); marking it lets the C# member-read pass find it, exactly as
    # Python's @property marking feeds its attribute-access pass. A Dart
    # getter_signature is the same shape (issue #869): its accesses are
    # attribute reads the Dart getter-read pass resolves through this flag.
    # A JS/TS getter is a `method_definition` carrying the `get` keyword; its
    # reads are member accesses (`obj.thing`), so mark it like the other
    # languages' properties to feed the JS/TS getter-read pass. The keyword is a
    # direct child but not always the first (`static get x()` leads with
    # `static`); a method named `get` carries a property_identifier, not a `get`
    # keyword node, so scanning direct children stays exact.
    is_js_ts_getter = (
        language in cs.JS_TS_LANGUAGES
        and method_node.type == cs.TS_METHOD_DEFINITION
        and any(
            child.type == cs.TS_GET_ACCESSOR_KEYWORD for child in method_node.children
        )
    )
    return (
        _is_property_decorator(decorators)
        or is_js_ts_getter
        or method_node.type
        in (
            cs.TS_CSHARP_PROPERTY_DECLARATION,
            cs.TS_DART_GETTER_SIGNATURE,
        )
    )


def _method_container_label(
    function_registry: FunctionRegistryTrieProtocol,
    container_qn: str,
    container_type: cs.NodeLabel,
) -> cs.NodeLabel:
    # The DEFINES_METHOD parent is matched by LABEL + qualified_name, so it must
    # carry the container's real node label. Callers pass Class by default, but a
    # trait/interface (Interface) or enum (Enum) container would then never match
    # and drop the containment edge. Prefer the label it was registered with.
    # Where a TS interface shares its name with a value (issue #2520) the
    # registry holds the value's kind, so the label the owner was declared with
    # decides: an interface's members stay on the Interface node.
    type_side = function_registry.type_kind(container_qn)
    if type_side is not None and type_side.value == container_type.value:
        return container_type
    container_label = container_type
    registered = function_registry.get(container_qn)
    if registered is not None and registered != cs.NodeLabel.METHOD:
        container_label = registered
    return container_label


def _record_method_overrides(
    method_props: PropertyDict,
    method_qn: str,
    method_name: str,
    container_qn: str,
    decorators: list[str],
    external_override_names: frozenset[str],
    annotated_override_sink: dict[str, list[tuple[str, str]]] | None,
) -> None:
    # Overriding a method of an EXTERNAL stdlib base (click's TextWrapper
    # subclass overriding textwrap's _wrap_chunks): the base's machinery invokes
    # it, so the dead-code surfaces root this property.
    if method_name in external_override_names:
        method_props[cs.KEY_OVERRIDES_EXTERNAL] = True
    # A Dart @override is only an EXTERNAL override if the resolved ancestry
    # says so, which is unknowable until every class is registered: record it
    # for resolve_deferred_inherits to judge.
    if (
        annotated_override_sink is not None
        and cs.DART_OVERRIDE_ANNOTATION in decorators
    ):
        annotated_override_sink.setdefault(container_qn, []).append(
            (method_qn, method_name)
        )


class _CppMember(NamedTuple):
    qualified_name: str
    signature: OverloadSignature | None
    declared_in_class: bool


def _register_cpp_member_qn(
    method_node: ASTNode,
    natural_qn: str,
    function_registry: FunctionRegistryTrieProtocol,
    start_line: int,
    start_col: int,
) -> _CppMember:
    # A C++ member is seen twice, in-class and out of it, so the plain
    # `@line` dedup would split one member in two; the signature keeps one
    # node per overload instead (issue #2455). Without a readable parameter
    # list the plain name stands, as it did for every member before.
    # Local import: cpp.overloads imports this module for its decode helper.
    from .cpp.overloads import cpp_overload_signature, declared_in_class_body

    declared = declared_in_class_body(method_node)
    signature = cpp_overload_signature(method_node)
    if signature is None:
        return _CppMember(natural_qn, None, declared)
    qualified_name = function_registry.register_overload_qn(
        natural_qn,
        signature,
        start_line,
        start_col,
        declared_in_class=declared,
    )
    return _CppMember(qualified_name, signature, declared)


def _register_method_qn(
    method_node: ASTNode,
    method_qn: str,
    language: cs.SupportedLanguage,
    function_registry: FunctionRegistryTrieProtocol,
    start_line: int,
    start_col: int,
    file_path: Path | None = None,
) -> tuple[str, _CppMember | None]:
    # The registered qn, and for C++ the member it names (issue #2455).
    if language == cs.SupportedLanguage.CPP:
        cpp_member = _register_cpp_member_qn(
            method_node, method_qn, function_registry, start_line, start_col
        )
        return cpp_member.qualified_name, cpp_member
    # TypeScript keeps a type and a same-named value as two nodes (#2520).
    kind = cs.NodeLabel.METHOD if language in cs.JS_TS_LANGUAGES else None
    # A Go method's natural qn is its receiver type's, which each build file
    # declaring the method again shares (#3360).
    origin = (
        str(file_path)
        if language == cs.SupportedLanguage.GO and file_path is not None
        else None
    )
    registered = function_registry.register_unique_qn(
        method_qn, start_line, start_col, kind=kind, origin=origin
    )
    return registered, None


def _cpp_member_props(cpp_member: _CppMember | None) -> PropertyDict:
    if cpp_member is None or cpp_member.signature is None:
        return {}
    props: PropertyDict = {cs.KEY_SIGNATURE: cpp_member.signature.text}
    # Written only by the class body, never as False: node writes merge,
    # so the definition written after its declaration leaves it standing.
    if cpp_member.declared_in_class:
        props[cs.KEY_DECLARED_IN_CLASS] = True
    return props


def _record_member_aliases(
    method_props: PropertyDict,
    function_registry: FunctionRegistryTrieProtocol,
    container_qn: str,
    method_qn: str,
    member_aliases: tuple[str, ...],
) -> None:
    if not member_aliases:
        return
    # Stored on the node so an incremental run that does not re-parse
    # this file can register the aliases again (issue #2620).
    method_props[cs.KEY_MEMBER_ALIASES] = list(member_aliases)
    for alias in member_aliases:
        function_registry.add_member_alias(
            f"{container_qn}{cs.SEPARATOR_DOT}{alias}", method_qn
        )


def _method_positional_params_props(
    method_node: ASTNode, language: cs.SupportedLanguage
) -> PropertyDict:
    """The method's stored positional parameter list, or nothing to store.

    Python's keeps the receiver deliberately: CPython counts the bound `self`
    in "takes N positional arguments", so a stored list that dropped it would
    under-count by one on every method (issue #227). The languages that
    declare optionality store their parameters marked with it (issue #2517);
    the rest leave the property absent.
    """
    if language == cs.SupportedLanguage.PYTHON:
        return {
            cs.KEY_POSITIONAL_PARAMS: python_positional_parameter_names(method_node)
        }
    # Local import: positional_params imports this module for its decoder.
    from .positional_params import declared_positional_params

    declared = declared_positional_params(method_node, language)
    return {} if declared is None else {cs.KEY_POSITIONAL_PARAMS: declared}


def _language_method_props(
    method_node: ASTNode, language: cs.SupportedLanguage
) -> PropertyDict:
    """The Method properties only one language records."""
    if language == cs.SupportedLanguage.CSHARP:
        # Local import: the C# helpers import this module.
        from .csharp import utils as csharp_utils

        return csharp_utils.extension_method_props(method_node)
    if language == cs.SupportedLanguage.JAVA:
        # Local import: the Java helpers import this module.
        from .java import utils as java_utils

        if java_utils.is_main_declaration(method_node):
            return {cs.KEY_IS_PROGRAM_MAIN: True}
    return {}


def ingest_method(
    method_node: ASTNode,
    container_qn: str,
    container_type: cs.NodeLabel,
    ingestor: IngestorProtocol,
    function_registry: FunctionRegistryTrieProtocol,
    simple_name_lookup: SimpleNameLookup,
    get_docstring_func: Callable[[ASTNode, cs.SupportedLanguage], str | None],
    language: cs.SupportedLanguage,
    lang_queries: LanguageQueries | None = None,
    method_qualified_name: str | None = None,
    file_path: Path | None = None,
    repo_path: Path | None = None,
    defer_containment: list[DeferredParentLink] | None = None,
    module_qn: str | None = None,
    external_override_names: frozenset[str] = frozenset(),
    annotated_override_sink: dict[str, list[tuple[str, str]]] | None = None,
    skip_cpp_artifact_check: bool = False,
    pending_endpoints: list | None = None,
    type_fact_sink: list | None = None,
    parameter_type_sink: list | None = None,
    member_aliases: tuple[str, ...] = (),
) -> str | None:
    # Returns the registered method qn (post register_unique_qn, so with any
    # @line dedup suffix) so a caller can wire further edges to the exact node,
    # e.g. an anonymous-class override method's OVERRIDES edge to its base. Returns
    # None only when the method has no resolvable name and nothing was registered.
    method_name = _resolve_method_name(method_node, language, skip_cpp_artifact_check)
    if method_name is None:
        return None
    method_start_line, method_start_col = _method_start_point(method_node, language)

    # Every language's method branch converges here, so this is the one place
    # that sees a method name whatever route produced it (issue #1810). The
    # function and class sites are guarded at their own extraction points.
    warn_if_name_truncated(method_node, method_name, file_path)

    method_qn = method_qualified_name or f"{container_qn}.{method_name}"
    method_qn, cpp_member = _register_method_qn(
        method_node,
        method_qn,
        language,
        function_registry,
        method_start_line,
        method_start_col,
        file_path,
    )

    decorators = []
    modifiers = []
    if lang_queries:
        modifiers, decorators = extract_modifiers_and_decorators(
            method_node, lang_queries
        )

    # Local import breaks the export_detection -> cpp.utils -> utils cycle.
    from . import export_detection

    method_props: PropertyDict = {
        cs.KEY_QUALIFIED_NAME: method_qn,
        cs.KEY_NAME: method_name,
        cs.KEY_MODIFIERS: modifiers,
        cs.KEY_DECORATORS: decorators,
        cs.KEY_START_LINE: method_start_line,
        cs.KEY_START_COL: method_start_col,
        cs.KEY_NAME_START_LINE: _member_name_start_point(method_node)[0],
        cs.KEY_NAME_START_COL: _member_name_start_point(method_node)[1],
        # Dart method signatures end before their sibling function_body;
        # extend the span over the body (no-op for other languages).
        cs.KEY_END_LINE: _method_end_line(method_node, language),
        cs.KEY_DOCSTRING: get_docstring_func(method_node, language),
        cs.KEY_IS_EXPORTED: export_detection.is_exported(
            method_node, method_name, language
        ),
    }
    if file_path is not None and repo_path is not None:
        method_props[cs.KEY_PATH] = cached_relative_path(
            file_path, repo_path
        ).as_posix()
        method_props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
    method_props.update(_cpp_member_props(cpp_member))
    method_props.update(_method_positional_params_props(method_node, language))
    # Local import: type_facts imports this module for safe_decode_with_fallback.
    from .type_facts import extract_type_facts, queue_type_facts, type_facts_props

    type_facts = extract_type_facts(method_node, language)
    method_props.update(type_facts_props(type_facts))
    method_props.update(_language_method_props(method_node, language))
    method_path = method_props.get(cs.KEY_PATH)
    queue_type_facts(
        type_fact_sink,
        cs.NodeLabel.METHOD,
        method_qn,
        module_qn,
        type_facts,
        method_path if isinstance(method_path, str) else None,
    )
    method_props.update(fingerprint_props(method_node))
    method_props.update(anchor_hash_props(method_node, decorators))
    method_props.update(decorated_start_props(method_node))

    is_property = _method_is_property(method_node, language, decorators)
    if is_property:
        method_props[cs.KEY_IS_PROPERTY] = True
    _record_member_aliases(
        method_props, function_registry, container_qn, method_qn, member_aliases
    )

    _record_method_overrides(
        method_props,
        method_qn,
        method_name,
        container_qn,
        decorators,
        external_override_names,
        annotated_override_sink,
    )

    logger.debug(ls.METHOD_FOUND.format(name=method_name, qn=method_qn))
    ingestor.ensure_node_batch(cs.NodeLabel.METHOD, method_props)
    # AFTER the Method node is queued: a batch flush writes nodes before
    # relationships, and a HAS_PARAMETER whose owner is still pending would
    # match nothing and be dropped. Local import for the same reason as
    # type_facts above.
    from .parameter_nodes import emit_declared_parameters

    emit_declared_parameters(
        ingestor,
        parameter_type_sink,
        cs.NodeLabel.METHOD,
        method_qn,
        module_qn,
        method_node,
        language,
        method_props,
        has_receiver=not is_static_decorator(decorators),
    )
    if pending_endpoints is not None:
        # Deferred so router mount prefixes can resolve after Pass 2 (#877).
        queue_endpoints(
            pending_endpoints,
            cs.NodeLabel.METHOD,
            method_qn,
            method_props.get(cs.KEY_DECORATORS),
            module_qn,
        )
    else:
        emit_endpoints(
            ingestor,
            cs.NodeLabel.METHOD,
            method_qn,
            method_props.get(cs.KEY_DECORATORS),
        )
    function_registry[method_qn] = cs.NodeLabel.METHOD
    if is_property:
        function_registry.mark_property(method_qn)
    if _is_abstract_member(decorators, method_node, language):
        function_registry.mark_abstract(method_qn)
    function_registry.mark_callable_params(
        method_qn, callable_parameter_indices(method_node, language)
    )
    simple_name_lookup[method_name].add(method_qn)

    # A container that may never register (a Rust impl on a primitive type)
    # defers so the edge is verified once every pass has run, falling back
    # to the module rather than a phantom the database would drop.
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

    container_label = _method_container_label(
        function_registry, container_qn, container_type
    )

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
    # Lazy import: export_detection reaches back into this module through
    # cpp.utils, so a top-level import would be circular.
    from . import export_detection

    props: PropertyDict = {
        cs.KEY_QUALIFIED_NAME: function_qn,
        cs.KEY_NAME: function_name,
        cs.KEY_MODIFIERS: [],
        cs.KEY_DECORATORS: [],
        cs.KEY_START_LINE: function_node.start_point[0] + 1,
        cs.KEY_START_COL: function_node.start_point[1],
        cs.KEY_NAME_START_LINE: _member_name_start_point(function_node)[0],
        cs.KEY_NAME_START_COL: _member_name_start_point(function_node)[1],
        cs.KEY_END_LINE: function_node.end_point[0] + 1,
        cs.KEY_DOCSTRING: docstring,
        # JS/TS only (per this helper's contract), so the JS branch of the
        # language dispatch applies regardless of which of the two it is.
        cs.KEY_IS_EXPORTED: export_detection.is_exported(
            function_node, function_name, cs.SupportedLanguage.JS
        ),
    }
    if file_path is not None and repo_path is not None:
        props[cs.KEY_PATH] = cached_relative_path(file_path, repo_path).as_posix()
        props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
    # Local import: positional_params imports this module for its decoder.
    # One reader serves both grammars, whose parameter node types differ.
    from .positional_params import declared_positional_params

    declared = declared_positional_params(function_node, cs.SupportedLanguage.JS)
    if declared is not None:
        props[cs.KEY_POSITIONAL_PARAMS] = declared
    props.update(fingerprint_props(function_node))
    props.update(anchor_hash_props(function_node))
    return props


def ingest_exported_function(
    function_node: ASTNode,
    function_name: str,
    module_qn: str,
    export_type: str,
    ingestor: IngestorProtocol,
    function_registry: FunctionRegistryTrieProtocol,
    simple_name_lookup: SimpleNameLookup,
    get_docstring_func: Callable[[ASTNode, cs.SupportedLanguage], str | None],
    language: cs.SupportedLanguage,
    is_export_inside_function_func: Callable[[ASTNode], bool],
    file_path: Path | None,
    repo_path: Path | None,
) -> str | None:
    # Returns the registered qn (None when skipped) so the caller can claim
    # the function node's span against later registration passes.
    if is_export_inside_function_func(function_node):
        return None

    function_qn = f"{module_qn}.{function_name}"
    # The definition pass already ingests an exported function / const-arrow at
    # its natural qn. Re-registering here would collide and mint a spurious
    # `qn@line` duplicate that call resolution then binds to (mangling the callee
    # qn). If the natural qn already exists, the node is done.
    if function_qn in function_registry:
        return None
    # Same for a nested export (TS namespace / module block): the main pass
    # already ingested it under its nested qn (e.g. lib.geo.helper), so a
    # module-level re-ingest would mint a phantom duplicate plus a spurious
    # Module-DEFINES edge. Walk ancestors rather than matching simple names, since
    # a top-level export may share a name with an unrelated method in the module.
    current = function_node.parent
    while current is not None:
        if current.type in (cs.TS_INTERNAL_MODULE, cs.TS_MODULE):
            return None
        current = current.parent
    # No variant is reachable here while the guard above returns early on any
    # qn already registered; the span is passed so the call stays correct if
    # that guard moves.
    function_qn = function_registry.register_unique_qn(
        function_qn, function_node.start_point[0] + 1, function_node.start_point[1]
    )

    function_props = module_function_props(
        function_qn,
        function_name,
        function_node,
        get_docstring_func(function_node, language),
        file_path,
        repo_path,
    )
    function_props[cs.KEY_IS_EXPORTED] = True

    logger.debug(
        ls.EXPORT_FOUND.format(
            export_type=export_type, name=function_name, qn=function_qn
        )
    )
    ingestor.ensure_node_batch(cs.NodeLabel.FUNCTION, function_props)
    emit_endpoints(
        ingestor,
        cs.NodeLabel.FUNCTION,
        function_qn,
        function_props.get(cs.KEY_DECORATORS),
    )
    function_registry[function_qn] = cs.NodeLabel.FUNCTION
    simple_name_lookup[function_name].add(function_qn)
    ingestor.ensure_relationship_batch(
        (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
        cs.RelationshipType.DEFINES,
        (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, function_qn),
    )
    return function_qn


def _is_property_decorator(decorators: list[str]) -> bool:
    return bool(_decorator_tail_names(decorators) & cs.PROPERTY_DECORATORS)


def _js_ts_field_member_name(
    node: ASTNode, language: cs.SupportedLanguage | None
) -> str | None:
    # The binding name of a JS/TS class-field arrow / fn-expr whose enclosing
    # field definition holds it as its `value` (`helper = () => ...`), so the
    # member is modelled as class_qn.helper. None for other languages/shapes.
    if language not in cs.JS_TS_LANGUAGES:
        return None
    if node.type not in (cs.TS_ARROW_FUNCTION, cs.TS_FUNCTION_EXPRESSION):
        return None
    parent = node.parent
    # `==` not `is`: py-tree-sitter returns a fresh Node wrapper on each access,
    # so identity comparison always fails; Node equality compares the node id.
    if parent is None or parent.child_by_field_name(cs.FIELD_VALUE) != node:
        return None
    name_node = parent.child_by_field_name(cs.FIELD_NAME)
    if name_node is None or name_node.type not in (
        cs.TS_IDENTIFIER,
        cs.TS_PROPERTY_IDENTIFIER,
    ):
        return None
    return safe_decode_text(name_node)
