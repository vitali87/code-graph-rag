from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...types_defs import (
    ASTNode,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageQueries,
    SimpleNameLookup,
)
from ..frontends.protocol import CallSiteKey, ResolvedCallSite
from ..import_processor import ImportProcessor
from ..semantic_call_join import call_site_key, declared_location
from ..utils import safe_decode_text
from .method_resolver import JavaMethodResolverMixin
from .type_resolver import JavaTypeResolverMixin
from .utils import find_package_start_index
from .variable_analyzer import JavaVariableAnalyzerMixin

if TYPE_CHECKING:
    from ..factory import ASTCacheProtocol


# The sentinel a proven-external call resolves to: not a target, an instruction
# to emit no first-party edge at all.
JAVA_EXTERNAL_TARGET: tuple[str, str] = ("", "")


class JavaTypeInferenceEngine(
    JavaTypeResolverMixin,
    JavaVariableAnalyzerMixin,
    JavaMethodResolverMixin,
):
    __slots__ = (
        "import_processor",
        "function_registry",
        "repo_path",
        "project_name",
        "ast_cache",
        "queries",
        "module_qn_to_file_path",
        "class_inheritance",
        "simple_name_lookup",
        "_lookup_cache",
        "_lookup_in_progress",
        "_fqn_to_module_qn",
        "java_call_sites",
        "java_external_sites",
        "function_locations",
        "_rel_to_module",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        repo_path: Path,
        project_name: str,
        ast_cache: "ASTCacheProtocol",
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        module_qn_to_file_path: dict[str, Path],
        class_inheritance: dict[str, list[str]],
        simple_name_lookup: SimpleNameLookup,
        java_call_sites: dict[CallSiteKey, ResolvedCallSite] | None = None,
        java_external_sites: set[CallSiteKey] | None = None,
        function_locations: dict[FunctionSpanKey, FunctionLocation] | None = None,
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.repo_path = repo_path
        self.project_name = project_name
        self.ast_cache = ast_cache
        self.queries = queries
        self.module_qn_to_file_path = module_qn_to_file_path
        self.class_inheritance = class_inheritance
        self.simple_name_lookup = simple_name_lookup

        self.java_call_sites = java_call_sites if java_call_sites is not None else {}
        self.java_external_sites = (
            java_external_sites if java_external_sites is not None else set()
        )
        self.function_locations = (
            function_locations if function_locations is not None else {}
        )
        self._rel_to_module: dict[str, str] = {}

        self._lookup_cache: dict[str, str | None] = {}
        self._lookup_in_progress: set[str] = set()

        self._fqn_to_module_qn: dict[str, list[str]] = self._build_fqn_lookup_map()

    def resolve_java_call_site(
        self, call_node: ASTNode, module_qn: str
    ) -> tuple[str, str] | None:
        # The javac path: a HIT is the declaration the compiler bound this call
        # to (the overload argument types select, which name-and-arity matching
        # cannot), and the external sentinel is its proof that the call leaves
        # the repo. Any miss returns None so the caller falls back to the
        # tree-sitter heuristics.
        if not (self.java_call_sites or self.java_external_sites):
            return None
        key = self._java_call_site_key(call_node, module_qn)
        if key is None:
            return None
        fact = self.java_call_sites.get(key)
        if fact is not None:
            return declared_location(
                fact.target_file,
                fact.target_line,
                fact.target_col,
                self.function_locations,
                self.module_qn_to_file_path,
                self.repo_path,
                self._rel_to_module,
            )
        if key in self.java_external_sites:
            return JAVA_EXTERNAL_TARGET
        return None

    def _java_call_site_key(
        self, call_node: ASTNode, module_qn: str
    ) -> CallSiteKey | None:
        # The callee NAME token, matching the tool's own choice: the `name`
        # field of method_invocation carries it for both `m()` and `x.m()`.
        name_node = call_node.child_by_field_name(cs.TS_FIELD_NAME)
        if name_node is None:
            return None
        name = safe_decode_text(name_node)
        if not name:
            return None
        return call_site_key(
            name_node, name, module_qn, self.module_qn_to_file_path, self.repo_path
        )

    def _build_fqn_lookup_map(self) -> dict[str, list[str]]:
        fqn_map: dict[str, list[str]] = {}

        def _add_mapping(key: str, value: str) -> None:
            modules = fqn_map.setdefault(key, [])
            if value not in modules:
                modules.append(value)

        for module_qn in self.module_qn_to_file_path.keys():
            parts = module_qn.split(cs.SEPARATOR_DOT)
            # Without a recognised src/main/java layout find_package_start_index
            # returns None, leaving the whole map empty so cross-file Java resolution
            # (static calls, instance dispatch in sibling files) silently fails. Fall
            # back to the segment after the project root (index 1) so flat /
            # non-standard layouts still register their simple class names.
            # find_package_start_index never returns 0.
            package_start_idx = find_package_start_index(parts) or 1
            if simple_class_name := cs.SEPARATOR_DOT.join(parts[package_start_idx:]):
                _add_mapping(simple_class_name, module_qn)

                class_parts = simple_class_name.split(cs.SEPARATOR_DOT)
                for j in range(1, len(class_parts)):
                    suffix = cs.SEPARATOR_DOT.join(class_parts[j:])
                    _add_mapping(suffix, module_qn)

        return fqn_map

    def build_variable_type_map(
        self, scope_node: ASTNode, module_qn: str
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}

        try:
            self._collect_all_variable_types(scope_node, local_var_types, module_qn)
            logger.debug(ls.JAVA_VAR_TYPE_MAP_BUILT, count=len(local_var_types))

        except Exception as e:
            logger.error(ls.JAVA_VAR_TYPE_MAP_FAILED, error=e)

        return local_var_types

    def resolve_java_method_call(
        self,
        call_node: ASTNode,
        local_var_types: dict[str, str] | None,
        module_qn: str,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        return self._do_resolve_java_method_call(
            call_node, local_var_types or {}, module_qn, caller_qn
        )

    def _find_containing_java_class(self, node: ASTNode) -> ASTNode | None:
        current = node.parent
        while current:
            match current.type:
                case (
                    cs.TS_CLASS_DECLARATION
                    | cs.TS_INTERFACE_DECLARATION
                    | cs.TS_ENUM_DECLARATION
                    | cs.TS_RECORD_DECLARATION
                ):
                    return current
                case _:
                    pass
            current = current.parent
        return None
