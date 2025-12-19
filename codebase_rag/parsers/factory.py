from pathlib import Path
from typing import Any, Protocol

from tree_sitter import Node

from ..services import IngestorProtocol
from ..types_defs import SimpleNameLookup
from .call_processor import CallProcessor
from .definition_processor import DefinitionProcessor
from .import_processor import ImportProcessor
from .structure_processor import StructureProcessor
from .type_inference import TypeInferenceEngine


class ASTCacheProtocol(Protocol):
    def __setitem__(self, key: Path, value: tuple[Node, str]) -> None: ...
    def __getitem__(self, key: Path) -> tuple[Node, str]: ...
    def __delitem__(self, key: Path) -> None: ...
    def __contains__(self, key: Path) -> bool: ...
    def items(self) -> Any: ...


class ProcessorFactory:
    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        queries: dict[str, Any],
        function_registry: Any,
        simple_name_lookup: SimpleNameLookup,
        ast_cache: ASTCacheProtocol,
    ) -> None:
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.queries = queries
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.ast_cache = ast_cache

        self.module_qn_to_file_path: dict[str, Path] = {}

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
            )
        return self._structure_processor

    @property
    def definition_processor(self) -> DefinitionProcessor:
        """Get or create the definition processor."""
        if self._definition_processor is None:
            self._definition_processor = DefinitionProcessor(
                ingestor=self.ingestor,
                repo_path=self.repo_path,
                project_name=self.project_name,
                function_registry=self.function_registry,
                simple_name_lookup=self.simple_name_lookup,
                import_processor=self.import_processor,
                module_qn_to_file_path=self.module_qn_to_file_path,
            )
        return self._definition_processor

    @property
    def type_inference(self) -> TypeInferenceEngine:
        """Get or create the type inference engine."""
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
            )
        return self._type_inference

    @property
    def call_processor(self) -> CallProcessor:
        """Get or create the call processor."""
        if self._call_processor is None:
            self._call_processor = CallProcessor(
                ingestor=self.ingestor,
                repo_path=self.repo_path,
                project_name=self.project_name,
                function_registry=self.function_registry,
                import_processor=self.import_processor,
                type_inference=self.type_inference,
                class_inheritance=self.definition_processor.class_inheritance,
            )
        return self._call_processor
