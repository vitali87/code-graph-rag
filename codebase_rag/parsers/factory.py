from collections.abc import Mapping
from pathlib import Path

from ..capture import ALL_ENABLED, CaptureSelection
from ..constants import RelationshipType, SupportedLanguage
from ..services import IngestorProtocol
from ..types_defs import (
    ASTCacheProtocol,
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    SimpleNameLookup,
)
from .call_processor import CallProcessor
from .definition_facts import DefinitionFacts
from .definition_processor import DefinitionProcessor
from .import_processor import ImportProcessor
from .py.ast_analyzer import clear_scope_facts
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
        "func_class_captures_cache",
    )

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        queries: Mapping[SupportedLanguage, LanguageQueries],
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
        self.func_class_captures_cache: dict[Path, dict] = {}

        self._import_processor: ImportProcessor | None = None
        self._structure_processor: StructureProcessor | None = None
        self._definition_processor: DefinitionProcessor | None = None
        self._type_inference: TypeInferenceEngine | None = None
        self._call_processor: CallProcessor | None = None

    def propagate_unignore_paths(
        self, resolved: frozenset[str] | None
    ) -> tuple[str, ...]:
        """Push a re-resolved unignore policy to every ALREADY-BUILT processor.

        Generated-source discovery re-resolves this policy per run, and every
        processor that prunes must prune identically or a sweep reads files the
        indexer skipped (issue #1088). That propagation used to be two named
        assignments at the call site, which is an inventory that fails open: a
        processor added later keeps a stale policy and nothing says so. The
        declaration tie-break (#1720) became the third consumer and was missed
        exactly that way (PR #1728).

        So the consumers are DERIVED from ``__slots__`` rather than listed. A
        processor built later picks the policy up at construction, because it
        reads the value updated here.

        Returns the slots updated, so a caller can assert coverage rather than
        assume it.
        """
        self.unignore_paths = resolved
        updated: list[str] = []
        for slot in self.__slots__:
            if not slot.startswith("_"):
                continue
            built = getattr(self, slot, None)
            if built is None or not hasattr(built, "unignore_paths"):
                continue
            built.unignore_paths = resolved
            updated.append(slot)
        return tuple(updated)

    def clear_inference_caches(self) -> None:
        """Drop the type-inference memos a previous pass left. Clears what
        is built and builds nothing."""
        if self._type_inference is not None:
            self._type_inference.clear_pass_caches()
        else:
            clear_scope_facts()

    def begin_run(self) -> None:
        """Start a full run: the inference memos go, and each built pass
        forgets what it recorded about the previous run."""
        self.clear_inference_caches()
        if self._definition_processor is not None:
            self._definition_processor.begin_run()
        if self._call_processor is not None:
            self._call_processor.begin_run()

    @property
    def import_processor(self) -> ImportProcessor:
        if self._import_processor is None:
            self._import_processor = ImportProcessor(
                repo_path=self.repo_path,
                project_name=self.project_name,
                ingestor=self.ingestor,
                function_registry=self.function_registry,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
                module_qn_to_file_path=self.module_qn_to_file_path,
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
                func_class_captures_cache=self.func_class_captures_cache,
                flow_capture_enabled=(
                    RelationshipType.FLOWS_TO in self.capture.enabled_rels
                ),
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
            )
        return self._definition_processor

    @property
    def facts(self) -> DefinitionFacts:
        """What the definition pass records for the passes after it."""
        return self.definition_processor.facts

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
                facts=self.facts,
                simple_name_lookup=self.simple_name_lookup,
            )
        return self._type_inference

    @property
    def built_call_processor(self) -> CallProcessor | None:
        """The call processor if one was built, without building it: built
        before the definition pass, it would snapshot half-built state."""
        return self._call_processor

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
                facts=self.facts,
                capture=self.capture,
                module_qn_to_file_path=self.module_qn_to_file_path,
                ast_cache=self.ast_cache,
                go_links=self.definition_processor.go_link_state(),
            )
        return self._call_processor
