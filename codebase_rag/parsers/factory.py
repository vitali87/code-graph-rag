"""Factory for creating processor instances with proper dependencies."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..services.graph_service import MemgraphIngestor
from .call_processor import CallProcessor
from .definition_processor import DefinitionProcessor
from .import_processor import ImportProcessor
from .structure_processor import StructureProcessor
from .type_inference import TypeInferenceEngine


class ProcessorFactory:
    """Factory for creating processor instances with proper dependency injection."""

    def __init__(
        self,
        ingestor: MemgraphIngestor,
        repo_path_getter: Callable[[], Path] | Path,
        project_name_getter: Callable[[], str] | str,
        queries: dict[str, Any],
        function_registry: Any,
        simple_name_lookup: dict[str, set[str]],
        ast_cache: dict[Path, tuple[Any, str]],
    ) -> None:
        self.ingestor = ingestor
        self._repo_path_getter = repo_path_getter
        self._project_name_getter = project_name_getter
        self.queries = queries
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.ast_cache = ast_cache

        # Create processors with proper dependencies
        self._import_processor: ImportProcessor | None = None
        self._structure_processor: StructureProcessor | None = None
        self._definition_processor: DefinitionProcessor | None = None
        self._type_inference: TypeInferenceEngine | None = None
        self._call_processor: CallProcessor | None = None

    @property
    def repo_path(self) -> Path:
        """Get the current repo path dynamically."""
        if callable(self._repo_path_getter):
            return self._repo_path_getter()
        return (
            Path(self._repo_path_getter)
            if isinstance(self._repo_path_getter, str)
            else self._repo_path_getter
        )

    @property
    def project_name(self) -> str:
        """Get the current project name dynamically."""
        if callable(self._project_name_getter):
            return self._project_name_getter()
        return str(self._project_name_getter)

    @property
    def import_processor(self) -> ImportProcessor:
        """Get or create the import processor."""
        if self._import_processor is None:
            self._import_processor = ImportProcessor(
                repo_path_getter=lambda: self.repo_path,
                project_name_getter=lambda: self.project_name,
            )
        return self._import_processor

    @property
    def structure_processor(self) -> StructureProcessor:
        """Get or create the structure processor."""
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
