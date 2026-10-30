from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LANGUAGE_FQN_SPECS, LanguageSpec
from ..node_text import safe_decode_text
from ..types_defs import (
    CppDefinitionSpan,
    DeferredParentLink,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    PendingTypeFact,
    PropertyDict,
    SimpleNameLookup,
)
from ..utils import qn_markers
from ..utils.fqn_resolver import scoped_name_parts
from ..utils.path_utils import cached_relative_path, cached_resolve_posix
from . import export_detection
from .anchor_hash import anchor_hash_props
from .ast_fingerprint import fingerprint_props
from .callable_params import (
    callable_parameter_indices,
    python_positional_parameter_names,
)
from .cpp import function_ingest as cpp_function_ingest
from .cpp.utils import record_cpp_definition_span
from .csharp import function_ingest as csharp_function_ingest
from .dart import utils as dart_utils
from .definition_facts import DefinitionFacts
from .endpoints import queue_endpoints
from .function_identity import FunctionResolution, name_start_point
from .go import function_ingest as go_function_ingest
from .go import interfaces as go_interfaces
from .go.builds import GoBuilds
from .java import function_ingest as java_function_ingest
from .js_ts import function_ingest as js_ts_function_ingest
from .js_ts import utils as js_ts_utils
from .js_ts.utils import mark_js_ts_module_private
from .lua import utils as lua_utils
from .method_ingest import MethodIngestHost
from .parameter_nodes import (
    PendingParameterType,
    emit_declared_parameters,
)
from .php import utils as php_utils
from .positional_params import declared_positional_params
from .rust import function_ingest as rust_function_ingest
from .rust import utils as rs_utils
from .type_facts import extract_type_facts, queue_type_facts, type_facts_props
from .utils import (
    decorated_start_props,
    extract_modifiers_and_decorators,
    function_span_key,
    get_function_captures,
    is_method_node,
    warn_if_name_truncated,
)

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from ..types_defs import LanguageQueries


class FunctionIngest:
    """Writes every Function node a file's function captures yield, and the
    DEFINES edges held until every parent is registered. A language's own
    share is the collaborator it builds from that language's package, which
    holds what waits for every type until its `resolve_deferred_*` pass:
    C/C++'s out-of-class method definitions and held-back nodes, Go's
    receiver methods, C#'s `#if`-orphaned members, Rust's body-local items
    and JS/TS's anonymous function expressions."""

    __slots__ = (
        "_method_host",
        "ingestor",
        "repo_path",
        "function_registry",
        "simple_name_lookup",
        "module_qn_to_file_path",
        "facts",
        "cpp_definition_spans",
        "folded_overloads",
        "pending_endpoints",
        "pending_type_facts",
        "pending_parameter_types",
        "_deferred_parent_links",
        "cpp",
        "rust",
        "csharp",
        "go",
        "js_ts",
        "java",
    )

    def __init__(
        self,
        *,
        method_host: MethodIngestHost,
        ingestor: IngestorProtocol,
        repo_path: Path,
        function_registry: FunctionRegistryTrieProtocol,
        simple_name_lookup: SimpleNameLookup,
        module_qn_to_file_path: dict[str, Path],
        facts: DefinitionFacts,
        go_builds: GoBuilds,
        go_methods: dict[str, go_interfaces.GoMethodSpec],
        java_anon_overrides: list[tuple[str, str, str, str]],
        cpp_definition_spans: dict[str, list[CppDefinitionSpan]],
        csharp_methods: set[str],
        csharp_override_methods: set[str],
        folded_overloads: dict[str, frozenset[int]],
        pending_endpoints: list[tuple[cs.NodeLabel, str, list[str], str | None]],
        pending_type_facts: list[PendingTypeFact],
        pending_parameter_types: list[PendingParameterType],
        deferred_parent_links: list[DeferredParentLink],
    ) -> None:
        # What a method the function pass finds is written through, and
        # where a definition's docstring is read from.
        self._method_host = method_host
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.module_qn_to_file_path = module_qn_to_file_path
        self.facts = facts
        self.cpp_definition_spans = cpp_definition_spans
        self.folded_overloads = folded_overloads
        self.pending_endpoints = pending_endpoints
        self.pending_type_facts = pending_type_facts
        self.pending_parameter_types = pending_parameter_types
        # Shared with the class pass, whose methods defer their containment
        # here too; emptied in place by resolve_deferred_parent_links.
        self._deferred_parent_links = deferred_parent_links
        self.rust = rust_function_ingest.RustFunctionIngest(self, ingestor, facts)
        self.js_ts = js_ts_function_ingest.JsTsFunctionIngest(
            self, function_registry, facts
        )
        self.java = java_function_ingest.JavaFunctionIngest(java_anon_overrides)
        self.cpp = cpp_function_ingest.CppFunctionIngest(
            self,
            method_host=method_host,
            ingestor=ingestor,
            repo_path=repo_path,
            function_registry=function_registry,
            simple_name_lookup=simple_name_lookup,
            module_qn_to_file_path=module_qn_to_file_path,
            facts=facts,
            cpp_definition_spans=cpp_definition_spans,
        )
        self.csharp = csharp_function_ingest.CSharpFunctionIngest(
            method_host=method_host,
            facts=facts,
            methods=csharp_methods,
            override_methods=csharp_override_methods,
            deferred_parent_links=deferred_parent_links,
        )
        self.go = go_function_ingest.GoFunctionIngest(
            self,
            method_host=method_host,
            repo_path=repo_path,
            function_registry=function_registry,
            simple_name_lookup=simple_name_lookup,
            module_qn_to_file_path=module_qn_to_file_path,
            facts=facts,
            builds=go_builds,
            methods=go_methods,
            deferred_parent_links=deferred_parent_links,
        )

    def ingest_all_functions(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list] | None = None,
    ) -> None:
        if combined_captures is not None:
            lang_queries = queries[language]
            lang_config: LanguageSpec = lang_queries[cs.QUERY_CONFIG]
            captures = combined_captures
        else:
            result = get_function_captures(root_node, language, queries)
            if not result:
                return
            lang_config, captures = result
            lang_queries = queries[language]
        file_path = self.module_qn_to_file_path.get(module_qn)
        has_classes = bool(captures.get(cs.CAPTURE_CLASS))

        for func_node in captures.get(cs.CAPTURE_FUNCTION, []):
            if has_classes and self._is_method(func_node, lang_config):
                continue
            if self._skip_language_function(
                func_node, module_qn, language, lang_config, lang_queries, file_path
            ):
                continue

            resolution = self._resolve_function_identity(
                func_node, module_qn, language, lang_config, file_path
            )
            if not resolution or self._defer_function_registration(
                func_node, resolution, module_qn, language, lang_config, lang_queries
            ):
                continue

            self._register_function(
                func_node, resolution, module_qn, language, lang_config, lang_queries
            )
            self._record_free_function_return_type(
                func_node, language, resolution.qualified_name
            )

    def _skip_language_function(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
        file_path: Path | None,
    ) -> bool:
        match language:
            case cs.SupportedLanguage.CSHARP:
                return self.csharp.skips_function(
                    func_node, module_qn, lang_config, lang_queries, file_path
                )
            case cs.SupportedLanguage.CPP:
                return self.cpp.skips_function(
                    func_node, module_qn, lang_config, lang_queries
                )
            case cs.SupportedLanguage.GO:
                return self.go.defers_receiver_method(func_node, module_qn)
            case (
                cs.SupportedLanguage.PYTHON
                | cs.SupportedLanguage.TS
                | cs.SupportedLanguage.TSX
            ):
                return func_node.start_byte in self.folded_overloads.get(
                    module_qn, frozenset()
                )
            case _:
                return False

    def _defer_function_registration(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
    ) -> bool:
        if language in cs.JS_TS_LANGUAGES:
            return self.js_ts.defers_anonymous(
                func_node, resolution, module_qn, language, lang_config, lang_queries
            )
        if language == cs.SupportedLanguage.RUST:
            return self.rust.defers_body_local(
                func_node, resolution, module_qn, lang_config, lang_queries
            )
        return False

    def _record_free_function_return_type(
        self, func_node: Node, language: cs.SupportedLanguage, qualified_name: str
    ) -> None:
        if language == cs.SupportedLanguage.CPP:
            self.cpp.record_return_type(func_node, qualified_name)

        if language == cs.SupportedLanguage.RUST:
            self.rust.record_return_type(func_node, qualified_name)

        # A Dart free function's return type: a call-bound local
        # (`var g = makeIt()` via the enrichment pass) types from
        # this map.
        if language == cs.SupportedLanguage.DART and (
            return_type := dart_utils.return_type_name(func_node)
        ):
            self.facts.method_return_types[qualified_name] = return_type

        if language == cs.SupportedLanguage.GO:
            self.go.record_return_types(func_node, qualified_name)

    def _function_span_claimed(self, module_qn: str, func_node: Node) -> bool:
        # A span is claimed when a pass recorded THIS function node's location;
        # the column in the key keeps a same-line neighbour's claim from masking
        # this node's own.
        return function_span_key(module_qn, func_node) in self.facts.function_locations

    def span_claimed_for_qn(
        self, module_qn: str, func_node: Node, candidate_qn: str
    ) -> bool:
        # A named pass re-deriving the SAME qn another pass already registered for
        # this span is a pure duplicate (it would collide into a spurious `qn@line`
        # twin). A DIFFERENT qn is the deliberate twin model:
        # `X.prototype.m = function m()` keeps both the fn-expr's own-name node and
        # the member-name node (duplicate-QN design: keep both, CALLS-to-both), so
        # it must still register.
        loc = self.facts.function_locations.get(function_span_key(module_qn, func_node))
        if loc is None:
            return False
        claimed_base = qn_markers.natural_qn(loc.qualified_name)
        return claimed_base == candidate_qn

    def claim_function_span(
        self, module_qn: str, func_node: Node, label: str, qualified_name: str
    ) -> None:
        # First claim wins; a later pass deriving a different qn for the same
        # source function skips registration rather than mint a twin.
        key = function_span_key(module_qn, func_node)
        if key not in self.facts.function_locations:
            self.facts.function_locations[key] = FunctionLocation(
                label=label,
                qualified_name=qualified_name,
                container_qn=None,
            )

    def _resolve_function_identity(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        file_path: Path | None,
    ) -> FunctionResolution | None:
        resolution = self._try_unified_fqn_resolution(
            func_node, module_qn, language, file_path
        )
        if resolution:
            return resolution

        return self._fallback_function_resolution(
            func_node, module_qn, language, lang_config
        )

    def _try_unified_fqn_resolution(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        file_path: Path | None,
    ) -> FunctionResolution | None:
        fqn_config = LANGUAGE_FQN_SPECS.get(language)
        if not fqn_config or not file_path:
            return None

        func_name = fqn_config.get_name(func_node)
        if not func_name:
            return None
        warn_if_name_truncated(func_node, func_name, file_path)

        parts = [
            *scoped_name_parts(func_node.parent, fqn_config, module_qn, file_path),
            func_name,
        ]

        # Prefix with the module's resolved (collision-disambiguated) qn rather
        # than recomputing from the path, so same-stem cross-language siblings stay
        # distinct.
        func_qn = module_qn + cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(parts)
        # A plain dotted name (Lua `M.foo`) keeps only its final segment, as
        # the qn rsplit always did. A computed bracket name is different: its
        # dot belongs to the name itself, and re-splitting registered an
        # object-literal `[Symbol.iterator]` member as `iterator]`
        # (issue #998).
        simple_name = (
            func_name
            if func_name.startswith(cs.JS_COMPUTED_NAME_PREFIX)
            and func_name.endswith(cs.JS_COMPUTED_NAME_SUFFIX)
            else func_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        )

        is_exported = export_detection.is_exported(func_node, simple_name, language)
        return FunctionResolution(func_qn, simple_name, is_exported)

    def _fallback_function_resolution(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> FunctionResolution | None:
        if language == cs.SupportedLanguage.CPP:
            return self.cpp.resolve_function(func_node, module_qn)
        return self._resolve_generic_function(
            func_node, module_qn, language, lang_config
        )

    def _resolve_generic_function(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> FunctionResolution:
        func_name = self._extract_function_name(func_node)
        # A function expression that is the VALUE of a table-constructor field
        # (`s = { set = function() end }`) is named by its key, under the table
        # it is built into: qn `s.set`, name `set`. Before, the enclosing
        # assignment's left side named it, so every function in the table was
        # `s` and only an `@line` suffix told them apart (issue #1631).
        display_name: str | None = None

        if (
            not func_name
            and language == cs.SupportedLanguage.LUA
            and func_node.type == cs.TS_LUA_FUNCTION_DEFINITION
        ):
            func_name, display_name = self._extract_lua_field_function_name(func_node)
            # A field value whose field has no name is anonymous; only a
            # function that is NOT a field value may take the assignment's
            # name (#1750).
            if not func_name and not lua_utils.is_field_value(func_node):
                func_name = self._extract_lua_assignment_function_name(func_node)

        is_anonymous = not func_name
        if not func_name:
            func_name = self._generate_anonymous_function_name(func_node)

        func_qn = self._build_function_qn(
            func_node, module_qn, func_name, language, lang_config
        )
        is_exported = export_detection.is_exported(func_node, func_name, language)
        return FunctionResolution(
            func_qn, display_name or func_name, is_exported, is_anonymous
        )

    def _build_function_qn(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> str:
        if language == cs.SupportedLanguage.RUST:
            return self.rust.qualified_name(func_node, module_qn, func_name)

        nested_qn = self._build_nested_qualified_name(
            func_node, module_qn, func_name, lang_config
        )
        return nested_qn or f"{module_qn}.{func_name}"

    def _register_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
    ) -> None:
        unique_qn = self.function_registry.register_unique_qn(
            resolution.qualified_name,
            func_node.start_point[0] + 1,
            func_node.start_point[1],
            kind=cs.NodeLabel.FUNCTION if language in cs.JS_TS_LANGUAGES else None,
        )
        if unique_qn != resolution.qualified_name:
            resolution = resolution._replace(qualified_name=unique_qn)

        func_props = self._build_function_props(
            func_node, resolution, module_qn, lang_queries, language
        )
        if language in cs.JS_TS_LANGUAGES:
            self.js_ts.mark_name_scope(func_node, resolution.qualified_name, func_props)
        is_macro = func_node.type in (
            cs.TS_RS_MACRO_DEFINITION,
            cs.TS_JULIA_MACRO_DEFINITION,
        )
        if is_macro:
            # Rust and Julia macros live in a separate namespace from
            # functions (Julia's `@name` vs `name`); Pass-3 gates
            # macro-invocation vs fn-call binding on macro_qns, and the persisted
            # property lets incremental runs rehydrate the set for UNCHANGED files
            # (the is_property pattern).
            func_props[cs.KEY_IS_MACRO] = True
        logger.debug(
            ls.FUNC_FOUND.format(name=resolution.name, qn=resolution.qualified_name)
        )
        self.ingestor.ensure_node_batch(cs.NodeLabel.FUNCTION, func_props)
        func_path = func_props.get(cs.KEY_PATH)
        queue_type_facts(
            self.pending_type_facts,
            cs.NodeLabel.FUNCTION,
            resolution.qualified_name,
            module_qn,
            extract_type_facts(func_node, language),
            func_path if isinstance(func_path, str) else None,
        )
        emit_declared_parameters(
            self.ingestor,
            self.pending_parameter_types,
            cs.NodeLabel.FUNCTION,
            resolution.qualified_name,
            module_qn,
            func_node,
            language,
            func_props,
            has_receiver=False,
        )
        # Deferred: emission happens after Pass 2 so router mount prefixes
        # (possibly declared in other modules) can resolve (issue #877).
        queue_endpoints(
            self.pending_endpoints,
            cs.NodeLabel.FUNCTION,
            resolution.qualified_name,
            func_props.get(cs.KEY_DECORATORS),
            module_qn,
        )

        self.function_registry[resolution.qualified_name] = cs.NodeLabel.FUNCTION
        mark_js_ts_module_private(
            self.function_registry,
            resolution.qualified_name,
            resolution.is_exported,
            language,
        )
        # Keyed by the UNIQUE qn, since a collision rename above (issue #1017)
        # is the key Pass-3 will look this up by.
        rs_utils.record_effective_module(
            self.facts.rust_function_modules,
            resolution.qualified_name,
            func_node,
            module_qn,
            language,
        )
        if is_macro:
            self.facts.macro_qns.add(resolution.qualified_name)
        self.function_registry.mark_callable_params(
            resolution.qualified_name,
            callable_parameter_indices(func_node, language),
        )
        if resolution.name:
            self.simple_name_lookup[resolution.name].add(resolution.qualified_name)
        # Record where this function landed so Pass-3 call attribution reuses the
        # registered qn/label instead of re-deriving it (C++ preprocessor
        # distortion, TS declaration merging, and duplicate-suffixed qns all make
        # the walks diverge).
        location = FunctionLocation(
            label=cs.NodeLabel.FUNCTION.value,
            qualified_name=resolution.qualified_name,
            container_qn=None,
            is_named=not resolution.is_anonymous,
        )
        self.facts.function_locations[function_span_key(module_qn, func_node)] = (
            location
        )
        if language == cs.SupportedLanguage.GO:
            self.go.register_name_alias(module_qn, func_node, location)
        if language == cs.SupportedLanguage.CSHARP:
            self.csharp.record_local_function(
                func_node, resolution.qualified_name, module_qn
            )
        record_cpp_definition_span(
            self.cpp_definition_spans,
            language,
            self.module_qn_to_file_path.get(module_qn),
            self.repo_path,
            func_node,
            cs.NodeLabel.FUNCTION.value,
            resolution.qualified_name,
        )
        if language == cs.SupportedLanguage.CPP:
            self.cpp.record_template_child_location(func_node, module_qn, location)

        if language == cs.SupportedLanguage.JAVA and resolution.name:
            self.java.record_anonymous_override(
                func_node, resolution, module_qn, lang_config
            )

        self._create_function_relationships(
            func_node, resolution, module_qn, language, lang_config
        )

    def _build_function_props(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        lang_queries: LanguageQueries,
        language: cs.SupportedLanguage,
    ) -> PropertyDict:
        file_path = self.module_qn_to_file_path.get(module_qn)
        modifiers, decorators = extract_modifiers_and_decorators(
            func_node, lang_queries
        )
        props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: resolution.qualified_name,
            cs.KEY_NAME: resolution.name,
            cs.KEY_MODIFIERS: modifiers,
            cs.KEY_DECORATORS: decorators,
            cs.KEY_START_LINE: func_node.start_point[0] + 1,
            cs.KEY_START_COL: func_node.start_point[1],
            cs.KEY_NAME_START_LINE: name_start_point(func_node)[0],
            cs.KEY_NAME_START_COL: name_start_point(func_node)[1],
            # Dart splits a definition into a signature node and a sibling
            # function_body; extend the end over that body so the snippet covers the
            # whole function (no-op for every other language).
            cs.KEY_END_LINE: dart_utils.definition_end_point(func_node)[0] + 1,
            cs.KEY_DOCSTRING: self._method_host._get_docstring(func_node, language),
            cs.KEY_IS_EXPORTED: resolution.is_exported,
        }
        if file_path is not None:
            props[cs.KEY_PATH] = cached_relative_path(
                file_path, self.repo_path
            ).as_posix()
            props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
        # Python's list is CPython's positional names; the languages that
        # declare optionality store every parameter marked with it (issue
        # #2517). The rest stay absent, which reads as "kinds unknown"
        # downstream rather than as "zero positional parameters".
        if language == cs.SupportedLanguage.PYTHON:
            props[cs.KEY_POSITIONAL_PARAMS] = python_positional_parameter_names(
                func_node
            )
        elif (declared := declared_positional_params(func_node, language)) is not None:
            props[cs.KEY_POSITIONAL_PARAMS] = declared
        props.update(type_facts_props(extract_type_facts(func_node, language)))
        if language == cs.SupportedLanguage.DART and dart_utils.is_top_level_main(
            func_node
        ):
            props[cs.KEY_IS_PROGRAM_MAIN] = True
        props.update(fingerprint_props(func_node))
        props.update(anchor_hash_props(func_node, decorators))
        props.update(decorated_start_props(func_node))
        return props

    def _create_function_relationships(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> None:
        if language == cs.SupportedLanguage.CPP and self.cpp.defers_containment(
            func_node, resolution.qualified_name, module_qn, lang_config
        ):
            return

        parent_type, parent_qn, parent_span = self.determine_function_parent(
            func_node, resolution.qualified_name, module_qn, lang_config, language
        )
        self.emit_or_defer_defines(
            parent_type,
            parent_qn,
            cs.NodeLabel.FUNCTION,
            resolution.qualified_name,
            module_qn,
            parent_span=parent_span,
        )

        if language == cs.SupportedLanguage.RUST:
            self.rust.reference_closure(
                func_node, parent_type, parent_qn, resolution.qualified_name
            )

        if resolution.is_exported and language == cs.SupportedLanguage.CPP:
            self.ingestor.ensure_relationship_batch(
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                cs.RelationshipType.EXPORTS,
                (
                    cs.NodeLabel.FUNCTION,
                    cs.KEY_QUALIFIED_NAME,
                    resolution.qualified_name,
                ),
            )

    def _extract_function_name(self, func_node: Node) -> str | None:
        name_node = func_node.child_by_field_name(cs.FIELD_NAME)
        if name_node and name_node.text:
            return safe_decode_text(name_node)

        # An anonymous arrow / function expression bound to a name (a
        # `const f = () => ...` declarator OR a `helper = () => ...` class field)
        # takes that binding name, in lock-step with the call pass's
        # binding-name climb (they must agree or the caller qn is a phantom and
        # the arrow's body callbacks report dead). One shared helper keeps both
        # passes' naming identical.
        return js_ts_utils.arrow_binding_name(func_node)

    def _generate_anonymous_function_name(self, func_node: Node) -> str:
        parent = func_node.parent
        if parent and parent.type == cs.TS_PARENTHESIZED_EXPRESSION:
            grandparent = parent.parent
            if (
                grandparent
                and grandparent.type == cs.TS_CALL_EXPRESSION
                and grandparent.child_by_field_name(cs.FIELD_FUNCTION) == parent
            ):
                func_type = (
                    cs.PREFIX_ARROW
                    if func_node.type == cs.TS_ARROW_FUNCTION
                    else cs.PREFIX_FUNC
                )
                return f"{cs.PREFIX_IIFE}{func_type}_{func_node.start_point[0]}_{func_node.start_point[1]}"

        if (
            parent
            and parent.type == cs.TS_CALL_EXPRESSION
            and parent.child_by_field_name(cs.FIELD_FUNCTION) == func_node
        ):
            return f"{cs.PREFIX_IIFE_DIRECT}{func_node.start_point[0]}_{func_node.start_point[1]}"

        return f"{cs.PREFIX_ANONYMOUS}{func_node.start_point[0]}_{func_node.start_point[1]}"

    def _extract_lua_assignment_function_name(self, func_node: Node) -> str | None:
        return lua_utils.extract_assigned_name(
            func_node,
            accepted_var_types=cs.LUA_NAMING_ASSIGNMENT_TARGETS,
        )

    def _extract_lua_field_function_name(
        self, func_node: Node
    ) -> tuple[str | None, str | None]:
        """(`table.key` path, `key`) for a table field's function value, else Nones.

        The walk lives in `lua_utils.field_function_path`, shared with the
        call pass so both name the node identically (#1631).
        """
        found = lua_utils.field_function_path(func_node)
        return found if found is not None else (None, None)

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
        skip_classes: bool = False,
    ) -> str | None:
        current = func_node.parent
        if not isinstance(current, Node):
            logger.warning(
                ls.CALL_UNEXPECTED_PARENT.format(
                    node=func_node, parent_type=type(current)
                )
            )
            return None

        path_parts = self._collect_ancestor_path_parts(
            func_node, current, lang_config, skip_classes
        )
        if path_parts is None:
            return None

        return self._format_nested_qn(module_qn, path_parts, func_name)

    def _collect_ancestor_path_parts(
        self,
        func_node: Node,
        current: Node | None,
        lang_config: LanguageSpec,
        skip_classes: bool,
    ) -> list[str] | None:
        path_parts: list[str] = []

        while current and current.type not in lang_config.module_node_types:
            result = self._process_ancestor_for_path(
                func_node, current, lang_config, skip_classes
            )
            if result is False:
                return None
            if result is not None:
                path_parts.append(result)
            # A PHP anonymous class's part already spells the callable it is
            # written in, as that callable is registered; naming the
            # callables around it again here would put the closure under a
            # different path than the class's own methods (issue #2538).
            current = (
                php_utils.anonymous_class_anchor_stop(current)
                if result is not None and current.type == cs.TS_PHP_ANONYMOUS_CLASS
                else current.parent
            )

        path_parts.reverse()
        return path_parts

    def _process_ancestor_for_path(
        self,
        func_node: Node,
        current: Node,
        lang_config: LanguageSpec,
        skip_classes: bool,
    ) -> str | None | Literal[False]:
        if current.type in lang_config.function_node_types:
            return self._get_name_from_function_ancestor(current)

        if current.type in lang_config.class_node_types:
            return self._handle_class_ancestor(
                func_node, current, skip_classes, lang_config
            )

        if current.type == cs.TS_METHOD_DEFINITION:
            return self._extract_node_name(current)

        return None

    def _get_name_from_function_ancestor(self, node: Node) -> str | None:
        if name := self._extract_node_name(node):
            return name
        return self._extract_function_name(node)

    def _handle_class_ancestor(
        self,
        func_node: Node,
        class_node: Node,
        skip_classes: bool,
        lang_config: LanguageSpec,
    ) -> str | None | Literal[False]:
        if skip_classes:
            return None
        # A function that is a DIRECT class member is a method, handled by the
        # class-ingest path: return False so the whole qn collapses to the flat
        # module.name form that path expects. But a function NESTED inside a method
        # body (a Promise executor `new Promise(cb)`, a defineProperty getter, any
        # closure) must keep the full class.method.<name> path so the call pass,
        # which always builds that path, references the same node; otherwise the
        # closure is orphaned and reports as dead.
        if self._is_nested_within_class_member(func_node, class_node, lang_config):
            if name := self._extract_node_name(class_node):
                return name
            # A PHP anonymous class is named as the definition pass names it,
            # under the callable it is written in (issue #2538).
            if name := php_utils.anonymous_class_scope_name(class_node):
                return name
            # An anonymous class expression (`static Proxy = class {...}`) has no
            # `name` field; recover its binding name so a closure nested in its
            # methods keeps the full `Outer.Proxy.method.<name>` path the call
            # pass builds, instead of an orphaned `Outer.method.<name>` (issue
            # #970).
            return js_ts_utils.class_binding_name(class_node)
        return False

    def _is_nested_within_class_member(
        self, func_node: Node, class_node: Node, lang_config: LanguageSpec
    ) -> bool:
        # True when a function/method boundary sits between func_node and its
        # enclosing class, i.e. func_node lives inside a member's body rather than
        # being the member itself. A direct method has no such intervening boundary
        # (its parent chain reaches the class body directly).
        current = func_node.parent
        while current is not None and current != class_node:
            if (
                current.type == cs.TS_METHOD_DEFINITION
                or current.type in lang_config.function_node_types
            ):
                return True
            current = current.parent
        return False

    def _extract_node_name(self, node: Node) -> str | None:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        if name_node and name_node.text is not None:
            return safe_decode_text(name_node)
        return None

    def _format_nested_qn(
        self, module_qn: str, path_parts: list[str], func_name: str
    ) -> str:
        if path_parts:
            return f"{module_qn}.{cs.SEPARATOR_DOT.join(path_parts)}.{func_name}"
        return f"{module_qn}.{func_name}"

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool:
        # A const/static initializer is a body like a function's, but it is no
        # function node, so the shared walk climbs out of it to the impl above
        # and calls the item inside it a method of that type. It is a free
        # function local to the initializer (issue #1037).
        if lang_config.language == cs.SupportedLanguage.RUST and rs_utils.is_body_local(
            func_node
        ):
            return False
        return is_method_node(func_node, lang_config)

    def emit_or_defer_defines(
        self,
        parent_label: str,
        parent_qn: str,
        child_label: str,
        child_qn: str,
        module_qn: str,
        fallback_label: str | None = None,
        fallback_qn: str | None = None,
        parent_span: FunctionSpanKey | None = None,
    ) -> None:
        # Module nodes always exist, so module-parented edges emit directly. Any
        # other parent may be registered by a later pass (methods land after
        # functions) or may be a phantom recomputed qn the database would drop; both
        # resolve in resolve_deferred_parent_links, where the optional fallback (a
        # nested child's lexical enclosing function) beats the module anchor.
        if parent_label == cs.NodeLabel.MODULE:
            self.ingestor.ensure_relationship_batch(
                (parent_label, cs.KEY_QUALIFIED_NAME, parent_qn),
                cs.RelationshipType.DEFINES,
                (child_label, cs.KEY_QUALIFIED_NAME, child_qn),
            )
            return
        self._deferred_parent_links.append(
            DeferredParentLink(
                parent_label_guess=parent_label,
                parent_qn=parent_qn,
                child_label=child_label,
                child_qn=child_qn,
                module_qn=module_qn,
                fallback_label=fallback_label,
                fallback_qn=fallback_qn,
                parent_span=parent_span,
            )
        )

    def _claimed_qn_for_anonymous_guess(
        self, module_qn: str, parent_qn: str
    ) -> tuple[str, str] | None:
        # An `anonymous_row_col` guess names the SPAN it stood for; if a named pass
        # claimed that span, the claim is the real parent.
        tail = parent_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        if not tail.startswith(cs.PREFIX_ANONYMOUS):
            return None
        row_col = tail[len(cs.PREFIX_ANONYMOUS) :].split(cs.CHAR_UNDERSCORE)
        if len(row_col) != 2 or not all(part.isdigit() for part in row_col):
            return None
        loc = self.facts.function_locations.get(
            (module_qn, int(row_col[0]) + 1, int(row_col[1]))
        )
        if loc is None or loc.qualified_name not in self.function_registry:
            return None
        return loc.label, loc.qualified_name

    def _recorded_parent_at_span(
        self, entry: DeferredParentLink
    ) -> FunctionLocation | None:
        # The span pins the parent NODE, so the location recorded for it in the
        # class pass carries the exact registered identity, including a C# overload's
        # signature suffix that the guessed qn cannot (and that a parameterless
        # sibling overload exactly shadows).
        if entry.parent_span is None:
            return None
        recorded = self.facts.function_locations.get(entry.parent_span)
        if recorded is None or recorded.qualified_name not in self.function_registry:
            return None
        return recorded

    def resolve_deferred_parent_links(self) -> int:
        """Emit deferred non-module DEFINES once every pass has registered.

        A registered parent gets the edge under its real label; an
        unregistered parent qn is a phantom, so the child anchors to its
        module rather than losing the edge at the database MERGE.
        """
        deferred = self._deferred_parent_links
        if not deferred:
            return 0
        emitted = 0
        for entry in deferred:
            if recorded := self._recorded_parent_at_span(entry):
                parent_spec = (
                    cs.NodeLabel(recorded.label),
                    cs.KEY_QUALIFIED_NAME,
                    recorded.qualified_name,
                )
                rel_type = entry.rel_type
            elif registered := self.function_registry.get(entry.parent_qn):
                parent_spec = (
                    registered,
                    cs.KEY_QUALIFIED_NAME,
                    entry.parent_qn,
                )
                rel_type = entry.rel_type
            elif (
                entry.fallback_qn is not None
                and entry.fallback_label is not None
                and self.function_registry.get(entry.fallback_qn) is not None
            ):
                # The parent guess never registered but the child's lexical
                # enclosing function did (a prototype assignment on a parameter
                # inside a function body); the lexical parent is the true
                # containment, not the module.
                parent_spec = (
                    cs.NodeLabel(entry.fallback_label),
                    cs.KEY_QUALIFIED_NAME,
                    entry.fallback_qn,
                )
                rel_type = cs.RelationshipType.DEFINES.value
            elif claimed := self._claimed_qn_for_anonymous_guess(
                entry.module_qn, entry.parent_qn
            ):
                # The parent guess was an anonymous placeholder for a span a NAMED
                # pass claimed after the child registered
                # (`exports.receiver = function () { function helper() }`): the
                # placeholder embeds its (row, col), so the claim record recovers
                # the registered enclosing node.
                claimed_label, claimed_qn = claimed
                parent_spec = (
                    cs.NodeLabel(claimed_label),
                    cs.KEY_QUALIFIED_NAME,
                    claimed_qn,
                )
                rel_type = cs.RelationshipType.DEFINES.value
            else:
                # A method whose container never registered (impl on a primitive,
                # macro-corrupted class) anchors to its module with DEFINES;
                # DEFINES_METHOD from a Module is not a documented shape.
                parent_spec = (
                    cs.NodeLabel.MODULE,
                    cs.KEY_QUALIFIED_NAME,
                    entry.module_qn,
                )
                rel_type = cs.RelationshipType.DEFINES.value
            self.ingestor.ensure_relationship_batch(
                parent_spec,
                rel_type,
                (entry.child_label, cs.KEY_QUALIFIED_NAME, entry.child_qn),
            )
            emitted += 1
        self._deferred_parent_links.clear()
        return emitted

    def _enclosing_function_parent(
        self,
        current: Node,
        func_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
        language: cs.SupportedLanguage | None,
        file_path: Path | None,
    ) -> tuple[str, str, FunctionSpanKey | None] | None:
        # The parent a function nested in `current` (an enclosing function)
        # binds to; None when no distinct parent qn can be derived.
        parent_label = (
            cs.NodeLabel.METHOD
            if self._is_method(current, lang_config)
            else cs.NodeLabel.FUNCTION
        )
        # Bind to the enclosing function's OWN qn, recomputed from its node.
        # A function nested in an anonymous callback otherwise loses that
        # callback: anonymous scopes contribute no segment to the child qn,
        # so trimming would skip the callback and hoist the child to the
        # nearest named ancestor.
        if language == cs.SupportedLanguage.GO and (
            receiver_parent := self.go.receiver_method_parent(current, module_qn)
        ):
            return receiver_parent
        if language in cs.JS_TS_LANGUAGES and (
            recorded_parent := self.js_ts.recorded_parent(current, func_qn, module_qn)
        ):
            return recorded_parent
        resolution = (
            self._resolve_function_identity(
                current, module_qn, language, lang_config, file_path
            )
            if language is not None
            else None
        )
        parent_qn = (
            resolution.qualified_name
            if resolution
            else func_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        )
        if not parent_qn or parent_qn == func_qn:
            return None
        # A C# method registers signature-suffixed (`Run(int)`), a shape
        # structural re-derivation cannot reproduce, and its parameterless
        # overload sibling exactly SHADOWS the guess. Methods register in
        # the class pass after this one, so the guess carries the parent
        # NODE's span for the deferred resolver to swap in the recorded
        # identity.
        span = (
            function_span_key(module_qn, current)
            if language == cs.SupportedLanguage.CSHARP
            else None
        )
        return parent_label, parent_qn, span

    def determine_function_parent(
        self,
        func_node: Node,
        func_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str, FunctionSpanKey | None]:
        current = func_node.parent
        if not isinstance(current, Node):
            return cs.NodeLabel.MODULE, module_qn, None

        file_path = self.module_qn_to_file_path.get(module_qn)
        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                parent = self._enclosing_function_parent(
                    current, func_qn, module_qn, lang_config, language, file_path
                )
                if parent is None:
                    break
                return parent

            current = current.parent

        if language == cs.SupportedLanguage.RUST and (
            inline_parent := self.rust.inline_module_parent(func_node, func_qn)
        ):
            return inline_parent

        return cs.NodeLabel.MODULE, module_qn, None
