from pathlib import Path

from ..capture import ALL_ENABLED, CaptureSelection
from ..constants import SupportedLanguage
from ..services import IngestorProtocol
from ..types_defs import (
    ASTCacheProtocol,
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    SimpleNameLookup,
)
from .call_processor import CallProcessor
from .definition_processor import DefinitionProcessor
from .import_processor import ImportProcessor
from .structure_processor import StructureProcessor
from .type_inference import TypeInferenceEngine


class ProcessorFactory:
    __slots__ = (
        "ingestor",
        "repo_path",
        "project_name",
        "queries",
        "function_registry",
        "simple_name_lookup",
        "ast_cache",
        "unignore_paths",
        "exclude_paths",
        "capture",
        "module_qn_to_file_path",
        "_import_processor",
        "_structure_processor",
        "_definition_processor",
        "_type_inference",
        "_call_processor",
        "_func_class_captures_cache",
    )

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        queries: dict[SupportedLanguage, LanguageQueries],
        function_registry: FunctionRegistryTrieProtocol,
        simple_name_lookup: SimpleNameLookup,
        ast_cache: ASTCacheProtocol,
        unignore_paths: frozenset[str] | None = None,
        exclude_paths: frozenset[str] | None = None,
        capture: CaptureSelection | None = None,
    ) -> None:
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.queries = queries
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.ast_cache = ast_cache
        self.unignore_paths = unignore_paths
        self.exclude_paths = exclude_paths
        self.capture = capture if capture is not None else ALL_ENABLED

        self.module_qn_to_file_path: dict[str, Path] = {}
        self._func_class_captures_cache: dict[Path, dict] = {}

        self._import_processor: ImportProcessor | None = None
        self._structure_processor: StructureProcessor | None = None
        self._definition_processor: DefinitionProcessor | None = None
        self._type_inference: TypeInferenceEngine | None = None
        self._call_processor: CallProcessor | None = None

    @property
    def import_processor(self) -> ImportProcessor:
        if self._import_processor is None:
            self._import_processor = ImportProcessor(
                repo_path=self.repo_path,
                project_name=self.project_name,
                ingestor=self.ingestor,
                function_registry=self.function_registry,
            )
        return self._import_processor

    @property
    def structure_processor(self) -> StructureProcessor:
        if self._structure_processor is None:
            self._structure_processor = StructureProcessor(
                ingestor=self.ingestor,
                repo_path=self.repo_path,
                project_name=self.project_name,
                queries=self.queries,
                unignore_paths=self.unignore_paths,
                exclude_paths=self.exclude_paths,
            )
        return self._structure_processor

    @property
    def definition_processor(self) -> DefinitionProcessor:
        if self._definition_processor is None:
            self._definition_processor = DefinitionProcessor(
                ingestor=self.ingestor,
                repo_path=self.repo_path,
                project_name=self.project_name,
                function_registry=self.function_registry,
                simple_name_lookup=self.simple_name_lookup,
                import_processor=self.import_processor,
                module_qn_to_file_path=self.module_qn_to_file_path,
                func_class_captures_cache=self._func_class_captures_cache,
            )
        return self._definition_processor

    @property
    def type_inference(self) -> TypeInferenceEngine:
        if self._type_inference is None:
            self._type_inference = TypeInferenceEngine(
                import_processor=self.import_processor,
                function_registry=self.function_registry,
                repo_path=self.repo_path,
                project_name=self.project_name,
                ast_cache=self.ast_cache,
                queries=self.queries,
                module_qn_to_file_path=self.module_qn_to_file_path,
                class_inheritance=self.definition_processor.class_inheritance,
                simple_name_lookup=self.simple_name_lookup,
                class_field_types=self.definition_processor.class_field_types,
                class_field_guard_inner=self.definition_processor.class_field_guard_inner,
                method_return_types=self.definition_processor.method_return_types,
                csharp_partial_groups=self.definition_processor.csharp_partial_groups,
                csharp_extension_methods=self.definition_processor.csharp_extension_methods,
                csharp_call_sites=self.definition_processor.csharp_call_sites,
                csharp_external_sites=self.definition_processor.csharp_external_sites,
                csharp_local_functions=self.definition_processor.csharp_local_functions,
                csharp_generic_methods=self.definition_processor.csharp_generic_methods,
                csharp_class_generic_arity=self.definition_processor.csharp_class_generic_arity,
                csharp_method_return_types=self.definition_processor.csharp_method_return_types,
                function_locations=self.definition_processor.function_locations,
            )
        return self._type_inference

    @property
    def call_processor(self) -> CallProcessor:
        if self._call_processor is None:
            self._call_processor = CallProcessor(
                ingestor=self.ingestor,
                repo_path=self.repo_path,
                project_name=self.project_name,
                function_registry=self.function_registry,
                import_processor=self.import_processor,
                type_inference=self.type_inference,
                class_inheritance=self.definition_processor.class_inheritance,
                type_aliases=self.definition_processor.type_aliases,
                interface_implementers=self.definition_processor.interface_implementers,
                capture=self.capture,
                module_qn_to_file_path=self.module_qn_to_file_path,
                cpp_out_of_class_methods=self.definition_processor.cpp_out_of_class_methods,
                function_locations=self.definition_processor.function_locations,
                macro_qns=self.definition_processor.macro_qns,
            )
        return self._call_processor
