from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LanguageSpec
from ..types_defs import (
    DeferredParentLink,
    FunctionLocation,
    FunctionSpanKey,
    PendingTypeFact,
    PropertyDict,
)
from ..utils import qn_markers
from ..utils.path_utils import cached_relative_path, cached_resolve_posix
from .anchor_hash import anchor_hash_props
from .ast_fingerprint import fingerprint_props
from .callable_params import callable_parameter_indices
from .cpp import function_ingest as cpp_function_ingest
from .cpp.utils import record_cpp_definition_span
from .csharp import function_ingest as csharp_function_ingest
from .dart import function_ingest as dart_function_ingest
from .dart import utils as dart_utils
from .endpoints import queue_endpoints
from .function_containment import FunctionContainment
from .function_identity import FunctionResolution, name_start_point
from .function_naming import FunctionNaming, extract_function_name
from .go import function_ingest as go_function_ingest
from .java import function_ingest as java_function_ingest
from .js_ts import function_ingest as js_ts_function_ingest
from .js_ts.utils import mark_js_ts_module_private
from .language_definitions import LanguageFunctionIngest
from .language_map import by_language
from .lua import function_ingest as lua_function_ingest
from .method_ingest import MethodIngestHost
from .parameter_nodes import PendingParameterType, emit_declared_parameters
from .php import function_ingest as php_function_ingest
from .python import function_ingest as python_function_ingest
from .rust import function_ingest as rust_function_ingest
from .rust import utils as rs_utils
from .type_facts import extract_type_facts, queue_type_facts, type_facts_props
from .utils import (
    decorated_start_props,
    extract_modifiers_and_decorators,
    function_span_key,
    get_function_captures,
    is_method_node,
)

if TYPE_CHECKING:
    from ..types_defs import LanguageQueries
    from .definition_context import DefinitionContext, LanguageStates

# Rust and Julia macros live in a separate namespace from functions (Julia's
# `@name` vs `name`).
_MACRO_DEFINITION_TYPES = (cs.TS_RS_MACRO_DEFINITION, cs.TS_JULIA_MACRO_DEFINITION)


class FunctionIngest:
    """Writes every Function node a file's function captures yield, and the
    DEFINES edges held until every parent is registered. A language's own
    share is the collaborator it builds from that language's package, which
    holds what waits for every type until its `resolve_deferred_*` pass:
    C/C++'s out-of-class method definitions and held-back nodes, Go's
    receiver methods, C#'s `#if`-orphaned members, Rust's body-local items
    and JS/TS's anonymous function expressions. Each answers the
    `LanguageFunctionIngest` hooks this pass looks it up by language to
    call. The identity a function registers under is `FunctionNaming`'s
    (`function_naming.py`); its parent, and the DEFINES edges held for it,
    `FunctionContainment`'s (`function_containment.py`)."""

    __slots__ = (
        "_method_host",
        "ingestor",
        "repo_path",
        "function_registry",
        "simple_name_lookup",
        "module_qn_to_file_path",
        "facts",
        "cpp_definition_spans",
        "pending_endpoints",
        "pending_type_facts",
        "pending_parameter_types",
        "_naming",
        "_containment",
        "cpp",
        "rust",
        "csharp",
        "go",
        "js_ts",
        "java",
        "python",
        "dart",
        "php",
        "lua",
        "_languages",
        "_neutral",
    )

    def __init__(
        self,
        *,
        method_host: MethodIngestHost,
        context: DefinitionContext,
        states: LanguageStates,
        folded_overloads: dict[str, frozenset[int]],
        pending_endpoints: list[tuple[cs.NodeLabel, str, list[str], str | None]],
        pending_type_facts: list[PendingTypeFact],
        pending_parameter_types: list[PendingParameterType],
        deferred_parent_links: list[DeferredParentLink],
    ) -> None:
        # What a method the function pass finds is written through, and
        # where a definition's docstring is read from.
        self._method_host = method_host
        self.ingestor = context.ingestor
        self.repo_path = context.repo_path
        self.function_registry = context.function_registry
        self.simple_name_lookup = context.simple_name_lookup
        self.module_qn_to_file_path = context.module_qn_to_file_path
        self.facts = context.facts
        self.cpp_definition_spans = states.cpp.definition_spans
        self.pending_endpoints = pending_endpoints
        self.pending_type_facts = pending_type_facts
        self.pending_parameter_types = pending_parameter_types
        self._naming = FunctionNaming(self)
        self._containment = FunctionContainment(self, context, deferred_parent_links)
        self.cpp = cpp_function_ingest.CppFunctionIngest(
            self, context, states.cpp, method_host=method_host
        )
        self.rust = rust_function_ingest.RustFunctionIngest(
            self, context.ingestor, context.facts
        )
        self.csharp = csharp_function_ingest.CSharpFunctionIngest(
            states.csharp,
            method_host=method_host,
            facts=context.facts,
            deferred_parent_links=deferred_parent_links,
        )
        self.go = go_function_ingest.GoFunctionIngest(
            self,
            context,
            states.go,
            method_host=method_host,
            deferred_parent_links=deferred_parent_links,
        )
        self.js_ts = js_ts_function_ingest.JsTsFunctionIngest(
            self, context.function_registry, context.facts, folded_overloads
        )
        self.java = java_function_ingest.JavaFunctionIngest(states.java)
        self.python = python_function_ingest.PythonFunctionIngest(folded_overloads)
        self.dart = dart_function_ingest.DartFunctionIngest(context.facts)
        self.php = php_function_ingest.PhpFunctionIngest()
        self.lua = lua_function_ingest.LuaFunctionIngest()
        # The collaborator each language's functions go to; a language with
        # none takes the neutral answers.
        self._languages: dict[cs.SupportedLanguage, LanguageFunctionIngest] = (
            by_language(
                {
                    cs.SupportedLanguage.PYTHON: self.python,
                    cs.JS_TS_LANGUAGES: self.js_ts,
                    cs.SupportedLanguage.JAVA: self.java,
                    cs.SupportedLanguage.RUST: self.rust,
                    cs.SupportedLanguage.GO: self.go,
                    cs.SupportedLanguage.CPP: self.cpp,
                    cs.SupportedLanguage.LUA: self.lua,
                    cs.SupportedLanguage.PHP: self.php,
                    cs.SupportedLanguage.CSHARP: self.csharp,
                    cs.SupportedLanguage.DART: self.dart,
                }
            )
        )
        self._neutral = LanguageFunctionIngest()

    def _for(
        self, language: cs.SupportedLanguage | str | None
    ) -> LanguageFunctionIngest:
        # A spec's language may be a plain string (`LanguageSpec.language`).
        if language is None:
            return self._neutral
        return self._languages.get(language, self._neutral)

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
        language_ingest = self._for(language)

        for func_node in captures.get(cs.CAPTURE_FUNCTION, []):
            if has_classes and self._is_method(func_node, lang_config):
                continue
            if language_ingest.skips_function(
                func_node, module_qn, language, lang_config, lang_queries, file_path
            ):
                continue

            resolution = self._resolve_function_identity(
                func_node, module_qn, language, lang_config, file_path
            )
            if not resolution or language_ingest.defers_registration(
                func_node, resolution, module_qn, language, lang_config, lang_queries
            ):
                continue

            self._register_function(
                func_node, resolution, module_qn, language, lang_config, lang_queries
            )
            language_ingest.record_return_type(func_node, resolution.qualified_name)

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
        # The C++ collaborator's host protocol, and the containment's, name
        # this; the naming is `FunctionNaming`'s.
        return self._naming.resolve_identity(
            func_node, module_qn, language, lang_config, file_path
        )

    @staticmethod
    def _extract_function_name(func_node: Node) -> str | None:
        # The Go collaborator's host protocol names this.
        return extract_function_name(func_node)

    def _register_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
    ) -> None:
        language_ingest = self._for(language)
        unique_qn = self.function_registry.register_unique_qn(
            resolution.qualified_name,
            func_node.start_point[0] + 1,
            func_node.start_point[1],
            kind=(
                cs.NodeLabel.FUNCTION
                if language_ingest.separate_declaration_spaces
                else None
            ),
        )
        if unique_qn != resolution.qualified_name:
            resolution = resolution._replace(qualified_name=unique_qn)

        self._write_function_node(
            func_node, resolution, module_qn, language, lang_queries
        )
        location = self._index_function(func_node, resolution, module_qn, language)
        language_ingest.record_function(
            func_node, resolution, module_qn, lang_config, location
        )

        self._create_function_relationships(
            func_node, resolution, module_qn, language, lang_config
        )

    def _write_function_node(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
    ) -> None:
        # The Function node, and what is queued from its properties: its type
        # facts, its declared parameters and the endpoints its decorators name.
        func_props = self._build_function_props(
            func_node, resolution, module_qn, lang_queries, language
        )
        self._for(language).mark_name_scope(
            func_node, resolution.qualified_name, func_props
        )
        if func_node.type in _MACRO_DEFINITION_TYPES:
            # Pass-3 gates macro-invocation vs fn-call binding on macro_qns,
            # and the persisted property lets incremental runs rehydrate the
            # set for UNCHANGED files (the is_property pattern).
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

    def _index_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> FunctionLocation:
        # The registry, name lookups and location maps the later passes find
        # a registered function by; returns where it landed.
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
        if func_node.type in _MACRO_DEFINITION_TYPES:
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
        self._for(language).register_name_alias(module_qn, func_node, location)
        record_cpp_definition_span(
            self.cpp_definition_spans,
            language,
            self.module_qn_to_file_path.get(module_qn),
            self.repo_path,
            func_node,
            cs.NodeLabel.FUNCTION.value,
            resolution.qualified_name,
        )
        return location

    def _build_function_props(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        lang_queries: LanguageQueries,
        language: cs.SupportedLanguage,
    ) -> PropertyDict:
        file_path = self.module_qn_to_file_path.get(module_qn)
        language_ingest = self._for(language)
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
        if (
            positional := language_ingest.positional_params(func_node, language)
        ) is not None:
            props[cs.KEY_POSITIONAL_PARAMS] = positional
        props.update(type_facts_props(extract_type_facts(func_node, language)))
        if language_ingest.is_program_main(func_node):
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
        language_ingest = self._for(language)
        if language_ingest.defers_containment(
            func_node, resolution.qualified_name, module_qn, lang_config
        ):
            return

        parent_type, parent_qn, parent_span = (
            self._containment.determine_function_parent(
                func_node, resolution.qualified_name, module_qn, lang_config, language
            )
        )
        self._containment.emit_or_defer_defines(
            parent_type,
            parent_qn,
            cs.NodeLabel.FUNCTION,
            resolution.qualified_name,
            module_qn,
            parent_span=parent_span,
        )
        language_ingest.extra_relationships(
            func_node, resolution, module_qn, parent_type, parent_qn
        )

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool:
        if self._for(lang_config.language).is_body_local(func_node):
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
        """Write a DEFINES edge from a module now, or hold one from any other
        parent until `resolve_deferred_parent_links`."""
        self._containment.emit_or_defer_defines(
            parent_label,
            parent_qn,
            child_label,
            child_qn,
            module_qn,
            fallback_label,
            fallback_qn,
            parent_span,
        )

    def resolve_deferred_parent_links(self) -> int:
        """Emit deferred non-module DEFINES once every pass has registered."""
        return self._containment.resolve_deferred_parent_links()

    def determine_function_parent(
        self,
        func_node: Node,
        func_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str, FunctionSpanKey | None]:
        """The label, qn and pinned span of the parent `func_node` is
        defined in."""
        return self._containment.determine_function_parent(
            func_node, func_qn, module_qn, lang_config, language
        )
