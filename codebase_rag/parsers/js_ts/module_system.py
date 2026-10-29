from __future__ import annotations

import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Language, QueryCursor

from ... import constants as cs
from ... import logs as ls
from ...node_text import (
    get_cached_query,
    safe_decode_text,
    safe_decode_with_fallback,
    sorted_captures,
)
from ...types_defs import ASTNode
from ..definition_facts import DefinitionFacts
from ..method_ingest import DocstringHost, ingest_exported_function
from ..utils import function_span_key
from .utils import get_js_ts_language_obj, is_export_inside_function

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import (
        FunctionRegistryTrieProtocol,
        LanguageQueries,
        SimpleNameLookup,
    )
    from ..function_ingest import FunctionIngest
    from ..import_processor import ImportProcessor


def _es6_declaration_name(export_function: ASTNode | None) -> str | None:
    # `export function f() {}` captures the declaration itself; its name is
    # its own `name` field.
    if not export_function:
        return None
    name_node = export_function.child_by_field_name(cs.FIELD_NAME)
    if not name_node or not name_node.text:
        return None
    return safe_decode_text(name_node)


class JsTsModuleSystem:
    """A JS/TS file's module system: the IMPORTS a destructured `require()`
    implies, and the functions CommonJS and ES6 exports define."""

    __slots__ = (
        "_host",
        "_function_ingest",
        "ingestor",
        "repo_path",
        "function_registry",
        "simple_name_lookup",
        "module_qn_to_file_path",
        "import_processor",
        "facts",
        "_processed_imports",
        "_pending_direct_module_exports",
    )

    def __init__(
        self,
        *,
        host: DocstringHost,
        function_ingest: FunctionIngest,
        ingestor: IngestorProtocol,
        repo_path: Path,
        function_registry: FunctionRegistryTrieProtocol,
        simple_name_lookup: SimpleNameLookup,
        module_qn_to_file_path: dict[str, Path],
        import_processor: ImportProcessor,
        facts: DefinitionFacts,
    ) -> None:
        self._host = host
        # The span-claim protocol every JS/TS registration path shares: one
        # source function mints exactly one node PER NAME, so each path checks
        # the claim before registering and claims after (deliberate
        # different-name twins still register).
        self._function_ingest = function_ingest
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.module_qn_to_file_path = module_qn_to_file_path
        self.import_processor = import_processor
        self.facts = facts
        self._processed_imports: set[str] = set()
        self._pending_direct_module_exports: list[tuple[ASTNode, bool]] = []

    def ingest_missing_import_patterns(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        language_obj = get_js_ts_language_obj(language, queries)
        if not language_obj:
            return

        # The one catch for the pass: a failure here is a bug, so it is
        # reported, and the rest of the module's definitions still ingest.
        try:
            query = get_cached_query(language_obj, cs.JS_COMMONJS_DESTRUCTURE_QUERY)
            captures = sorted_captures(QueryCursor(query), root_node)
            for declarator in captures.get(cs.CAPTURE_VARIABLE_DECLARATOR, []):
                self._process_variable_declarator_for_commonjs(
                    declarator, module_qn, language
                )
        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            logger.warning(
                ls.JS_COMMONJS_DESTRUCTURE_FAILED, module_qn=module_qn, error=e
            )

    def _extract_require_module_name(self, declarator: ASTNode) -> str | None:
        name_node = declarator.child_by_field_name(cs.FIELD_NAME)
        if not name_node or name_node.type != cs.TS_OBJECT_PATTERN:
            return None

        value_node = declarator.child_by_field_name(cs.FIELD_VALUE)
        if not value_node or value_node.type != cs.TS_CALL_EXPRESSION:
            return None

        function_node = value_node.child_by_field_name(cs.FIELD_FUNCTION)
        if not function_node or function_node.type != cs.TS_IDENTIFIER:
            return None

        if safe_decode_text(function_node) != cs.JS_REQUIRE_KEYWORD:
            return None

        arguments_node = value_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if not arguments_node or not arguments_node.children:
            return None

        module_string_node = next(
            (c for c in arguments_node.children if c.type == cs.TS_STRING),
            None,
        )
        if not module_string_node or module_string_node.text is None:
            return None

        return safe_decode_with_fallback(module_string_node).strip("'\"")

    def _process_destructured_child(
        self,
        child: ASTNode,
        module_name: str,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        if child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN:
            if name := safe_decode_text(child):
                self._process_commonjs_import(name, module_name, module_qn, language)
            return

        if child.type != cs.TS_PAIR_PATTERN:
            return

        key_node = child.child_by_field_name(cs.FIELD_KEY)
        value_node = child.child_by_field_name(cs.FIELD_VALUE)

        if not (key_node and key_node.type == cs.TS_PROPERTY_IDENTIFIER):
            return
        if not (value_node and value_node.type == cs.TS_IDENTIFIER):
            return
        if value_node.text is None:
            return

        if alias_name := safe_decode_text(value_node):
            self._process_commonjs_import(alias_name, module_name, module_qn, language)

    def _process_variable_declarator_for_commonjs(
        self, declarator: ASTNode, module_qn: str, language: cs.SupportedLanguage
    ) -> None:
        module_name = self._extract_require_module_name(declarator)
        if not module_name:
            return

        name_node = declarator.child_by_field_name(cs.FIELD_NAME)
        if not name_node:
            return

        for child in name_node.children:
            self._process_destructured_child(child, module_name, module_qn, language)

    def _process_commonjs_import(
        self,
        imported_name: str,
        module_name: str,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        # A destructured `require()` reads a dual-package exports map from
        # the require side, so the CommonJS condition, not the ESM one,
        # names the source module.
        resolved_source_module = self.import_processor.js_ts.resolve_js_module_path(
            module_name, module_qn, require=True
        )

        import_key = f"{module_qn}->{resolved_source_module}"
        if import_key not in self._processed_imports:
            # Route through the same deferred verification as every
            # other IMPORTS edge: an internal target must be a real
            # module, an external one gets its ExternalModule node at
            # flush (issue #652: this path emitted directly and was the
            # last source of phantom import targets).
            self.import_processor.defer_import_edge(
                module_qn, resolved_source_module, language
            )

            logger.debug(
                ls.JS_MISSING_IMPORT_PATTERN,
                module_qn=module_qn,
                imported_name=imported_name,
                resolved_source_module=resolved_source_module,
            )

            self._processed_imports.add(import_key)

    def _ingest_export_function(
        self,
        export_function: ASTNode,
        function_name: str,
        module_qn: str,
        export_type: str,
        language: cs.SupportedLanguage,
    ) -> None:
        if self._function_ingest.span_claimed_for_qn(
            module_qn,
            export_function,
            f"{module_qn}{cs.SEPARATOR_DOT}{function_name}",
        ):
            return
        function_qn = ingest_exported_function(
            export_function,
            function_name,
            module_qn,
            export_type,
            self.ingestor,
            self.function_registry,
            self.simple_name_lookup,
            self._host._get_docstring,
            language,
            is_export_inside_function,
            self.module_qn_to_file_path.get(module_qn),
            self.repo_path,
        )
        if function_qn is not None:
            self._function_ingest.claim_function_span(
                module_qn,
                export_function,
                cs.NodeLabel.FUNCTION.value,
                function_qn,
            )

    def _process_exports_pattern(
        self,
        exports_objs: list[ASTNode],
        export_names: list[ASTNode],
        export_functions: list[ASTNode],
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        for exports_obj, export_name, export_function in zip(
            exports_objs, export_names, export_functions
        ):
            if not (exports_obj.text and export_name.text):
                continue
            if safe_decode_text(exports_obj) != cs.JS_EXPORTS_KEYWORD:
                continue
            if function_name := safe_decode_text(export_name):
                self._ingest_export_function(
                    export_function,
                    function_name,
                    module_qn,
                    cs.JS_EXPORT_TYPE_COMMONJS,
                    language,
                )

    def _process_module_exports_pattern(
        self,
        module_objs: list[ASTNode],
        exports_props: list[ASTNode],
        export_names: list[ASTNode],
        export_functions: list[ASTNode],
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        for module_obj, exports_prop, export_name, export_function in zip(
            module_objs, exports_props, export_names, export_functions
        ):
            if not (module_obj.text and exports_prop.text and export_name.text):
                continue
            if safe_decode_text(module_obj) != cs.JS_MODULE_KEYWORD:
                continue
            if safe_decode_text(exports_prop) != cs.JS_EXPORTS_KEYWORD:
                continue
            if function_name := safe_decode_text(export_name):
                self._ingest_export_function(
                    export_function,
                    function_name,
                    module_qn,
                    cs.JS_EXPORT_TYPE_COMMONJS_MODULE,
                    language,
                )

    def _ingest_direct_module_export(
        self,
        root_node: ASTNode,
        module_qn: str,
        language_obj: Language,
        language: cs.SupportedLanguage,
    ) -> list[tuple[ASTNode, bool]]:
        # `module.exports = function (...) {...}` makes the WHOLE module one
        # function; `module.exports = function (...) {...}(args)` (with or
        # without parens, fastify's generated error-serializer) exports the
        # IIFE's RETURN value and runs the wrapper at module load. Collect
        # the function nodes now; the caller finalises AFTER the deferred
        # anonymous flush, resolving each node's qn by its own SOURCE SPAN so
        # a name collision or refused registration can never point the edge
        # or the alias map at an unrelated namesake.
        pending: list[tuple[ASTNode, bool]] = []
        try:
            cursor = QueryCursor(
                get_cached_query(language_obj, cs.JS_COMMONJS_DIRECT_EXPORT_QUERY)
            )
            captures = sorted_captures(cursor, root_node)
        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            logger.debug(ls.JS_COMMONJS_EXPORTS_QUERY_FAILED, error=e)
            return pending
        for module_obj, exports_prop, export_function in zip(
            captures.get(cs.CAPTURE_MODULE_OBJ, []),
            captures.get(cs.CAPTURE_EXPORTS_PROP, []),
            captures.get(cs.CAPTURE_EXPORT_FUNCTION, []),
        ):
            if safe_decode_text(module_obj) != cs.JS_MODULE_KEYWORD:
                continue
            if safe_decode_text(exports_prop) != cs.JS_EXPORTS_KEYWORD:
                continue
            if is_export_inside_function(export_function):
                # Assigned when the enclosing function runs, not at module
                # load: neither a load-time call nor the module's export.
                continue
            entry = self._pending_direct_export_entry(
                export_function, module_qn, language
            )
            if entry is not None:
                pending.append(entry)
        return pending

    def _pending_direct_export_entry(
        self,
        export_function: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> tuple[ASTNode, bool] | None:
        if export_function.type == cs.TS_CALL_EXPRESSION:
            callee: ASTNode | None = export_function.child_by_field_name(
                cs.FIELD_FUNCTION
            )
            while callee is not None and callee.type == cs.TS_PARENTHESIZED_EXPRESSION:
                callee = callee.named_children[0] if callee.named_children else None
            if callee is None or callee.type not in (
                cs.TS_FUNCTION_EXPRESSION,
                cs.TS_ARROW_FUNCTION,
            ):
                # Any other call expression is not a function export.
                return None
            return callee, True
        # The function IS the export: register it (own name, or its position
        # when anonymous) marked exported. A refusal (span already claimed by
        # the node's earlier registration) is fine: finalisation reads the
        # claimed span either way.
        name_node = export_function.child_by_field_name(cs.FIELD_NAME)
        function_name = safe_decode_text(name_node) if name_node is not None else None
        if not function_name:
            row, col = export_function.start_point
            function_name = f"{cs.PREFIX_ANONYMOUS}{row}_{col}"
        self._ingest_export_function(
            export_function,
            function_name,
            module_qn,
            cs.JS_EXPORT_TYPE_COMMONJS_MODULE,
            language,
        )
        return export_function, False

    def finalise_direct_module_exports(self, module_qn: str) -> None:
        # Runs AFTER the deferred anonymous flush: every function node's
        # minted qn is now in the span registry. An unregistered span means
        # the node was refused everywhere; emit nothing rather than guess.
        for node, invoked in self._pending_direct_module_exports:
            loc = self.facts.function_locations.get(function_span_key(module_qn, node))
            if loc is None or loc.qualified_name not in self.function_registry:
                # No claim, or a STALE claim from before a watch-mode removal
                # purged the registry: writing anything would resurrect a
                # sparse node for a function that no longer exists.
                continue
            if invoked:
                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                    cs.RelationshipType.CALLS,
                    (loc.label, cs.KEY_QUALIFIED_NAME, loc.qualified_name),
                )
            else:
                # The module's one export: aliases calling the whole-module
                # require resolve to it, and it is exported by definition
                # (the named form's earlier registration may have recorded
                # is_exported False; the merge fixes it up).
                self.ingestor.ensure_node_batch(
                    loc.label,
                    {
                        cs.KEY_QUALIFIED_NAME: loc.qualified_name,
                        cs.KEY_IS_EXPORTED: True,
                    },
                )
                self.import_processor.commonjs_direct_exports[module_qn] = (
                    loc.qualified_name
                )
        self._pending_direct_module_exports = []

    def ingest_commonjs_exports(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        language_obj = get_js_ts_language_obj(language, queries)
        if not language_obj:
            return

        # Reset first: an exception in a later pass must not leak a stale
        # pending list into the next file's finalisation.
        self._pending_direct_module_exports = []
        query_texts = [
            cs.JS_COMMONJS_EXPORTS_FUNCTION_QUERY,
            cs.JS_COMMONJS_MODULE_EXPORTS_QUERY,
        ]
        self._pending_direct_module_exports = self._ingest_direct_module_export(
            root_node, module_qn, language_obj, language
        )

        for query_text in query_texts:
            try:
                cursor = QueryCursor(get_cached_query(language_obj, query_text))
                captures = sorted_captures(cursor, root_node)

                self._process_exports_pattern(
                    captures.get(cs.CAPTURE_EXPORTS_OBJ, []),
                    captures.get(cs.CAPTURE_EXPORT_NAME, []),
                    captures.get(cs.CAPTURE_EXPORT_FUNCTION, []),
                    module_qn,
                    language,
                )

                self._process_module_exports_pattern(
                    captures.get(cs.CAPTURE_MODULE_OBJ, []),
                    captures.get(cs.CAPTURE_EXPORTS_PROP, []),
                    captures.get(cs.CAPTURE_EXPORT_NAME, []),
                    captures.get(cs.CAPTURE_EXPORT_FUNCTION, []),
                    module_qn,
                    language,
                )

            except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
                logger.debug(ls.JS_COMMONJS_EXPORTS_QUERY_FAILED, error=e)

    def _ingest_es6_export_query(
        self,
        root_node: ASTNode,
        lang_query: Language,
        query_text: str,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        try:
            cleaned_query = textwrap.dedent(query_text).strip()
            query = get_cached_query(lang_query, cleaned_query)
            captures = sorted_captures(QueryCursor(query), root_node)
            export_names = captures.get(cs.CAPTURE_EXPORT_NAME, [])
            export_functions = captures.get(cs.CAPTURE_EXPORT_FUNCTION, [])

            for export_name, export_function in zip(export_names, export_functions):
                if export_name.text and (
                    function_name := safe_decode_text(export_name)
                ):
                    self._ingest_export_function(
                        export_function,
                        function_name,
                        module_qn,
                        cs.JS_EXPORT_TYPE_ES6_FUNCTION,
                        language,
                    )

            if export_names:
                return
            for export_function in export_functions:
                if function_name := _es6_declaration_name(export_function):
                    self._ingest_export_function(
                        export_function,
                        function_name,
                        module_qn,
                        cs.JS_EXPORT_TYPE_ES6_FUNCTION_DECL,
                        language,
                    )
        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            logger.debug(ls.JS_ES6_EXPORTS_QUERY_FAILED, error=e)

    def ingest_es6_exports(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        try:
            lang_query = queries[language][cs.QUERY_LANGUAGE]

            for query_text in [
                cs.JS_ES6_EXPORT_CONST_QUERY,
                cs.JS_ES6_EXPORT_FUNCTION_QUERY,
            ]:
                self._ingest_es6_export_query(
                    root_node, lang_query, query_text, module_qn, language
                )

        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            logger.debug(ls.JS_ES6_EXPORTS_DETECT_FAILED, error=e)
