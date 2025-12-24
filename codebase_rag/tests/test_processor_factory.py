from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.call_processor import CallProcessor
from codebase_rag.parsers.definition_processor import DefinitionProcessor
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.structure_processor import StructureProcessor
from codebase_rag.parsers.type_inference import TypeInferenceEngine

if TYPE_CHECKING:
    from codebase_rag.parsers.factory import ProcessorFactory


@pytest.fixture
def factory(temp_repo: Path, mock_ingestor: MagicMock) -> ProcessorFactory:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )
    return updater.factory


class TestLazyInitialization:
    def test_import_processor_not_initialized_on_factory_creation(
        self, factory: ProcessorFactory
    ) -> None:
        assert factory._import_processor is None

    def test_structure_processor_not_initialized_on_factory_creation(
        self, factory: ProcessorFactory
    ) -> None:
        assert factory._structure_processor is None

    def test_definition_processor_not_initialized_on_factory_creation(
        self, factory: ProcessorFactory
    ) -> None:
        assert factory._definition_processor is None

    def test_type_inference_not_initialized_on_factory_creation(
        self, factory: ProcessorFactory
    ) -> None:
        assert factory._type_inference is None

    def test_call_processor_not_initialized_on_factory_creation(
        self, factory: ProcessorFactory
    ) -> None:
        assert factory._call_processor is None

    def test_import_processor_initialized_on_first_access(
        self, factory: ProcessorFactory
    ) -> None:
        _ = factory.import_processor
        assert factory._import_processor is not None

    def test_structure_processor_initialized_on_first_access(
        self, factory: ProcessorFactory
    ) -> None:
        _ = factory.structure_processor
        assert factory._structure_processor is not None

    def test_definition_processor_initialized_on_first_access(
        self, factory: ProcessorFactory
    ) -> None:
        _ = factory.definition_processor
        assert factory._definition_processor is not None

    def test_type_inference_initialized_on_first_access(
        self, factory: ProcessorFactory
    ) -> None:
        _ = factory.type_inference
        assert factory._type_inference is not None

    def test_call_processor_initialized_on_first_access(
        self, factory: ProcessorFactory
    ) -> None:
        _ = factory.call_processor
        assert factory._call_processor is not None


class TestSingletonBehavior:
    def test_import_processor_returns_same_instance(
        self, factory: ProcessorFactory
    ) -> None:
        first = factory.import_processor
        second = factory.import_processor
        assert first is second

    def test_structure_processor_returns_same_instance(
        self, factory: ProcessorFactory
    ) -> None:
        first = factory.structure_processor
        second = factory.structure_processor
        assert first is second

    def test_definition_processor_returns_same_instance(
        self, factory: ProcessorFactory
    ) -> None:
        first = factory.definition_processor
        second = factory.definition_processor
        assert first is second

    def test_type_inference_returns_same_instance(
        self, factory: ProcessorFactory
    ) -> None:
        first = factory.type_inference
        second = factory.type_inference
        assert first is second

    def test_call_processor_returns_same_instance(
        self, factory: ProcessorFactory
    ) -> None:
        first = factory.call_processor
        second = factory.call_processor
        assert first is second


class TestProcessorTypes:
    def test_import_processor_is_correct_type(self, factory: ProcessorFactory) -> None:
        assert isinstance(factory.import_processor, ImportProcessor)

    def test_structure_processor_is_correct_type(
        self, factory: ProcessorFactory
    ) -> None:
        assert isinstance(factory.structure_processor, StructureProcessor)

    def test_definition_processor_is_correct_type(
        self, factory: ProcessorFactory
    ) -> None:
        assert isinstance(factory.definition_processor, DefinitionProcessor)

    def test_type_inference_is_correct_type(self, factory: ProcessorFactory) -> None:
        assert isinstance(factory.type_inference, TypeInferenceEngine)

    def test_call_processor_is_correct_type(self, factory: ProcessorFactory) -> None:
        assert isinstance(factory.call_processor, CallProcessor)


class TestDependencyInjection:
    def test_import_processor_receives_repo_path(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.import_processor
        assert processor.repo_path == factory.repo_path

    def test_import_processor_receives_project_name(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.import_processor
        assert processor.project_name == factory.project_name

    def test_import_processor_receives_function_registry(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.import_processor
        assert processor.function_registry is factory.function_registry

    def test_structure_processor_receives_repo_path(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.structure_processor
        assert processor.repo_path == factory.repo_path

    def test_structure_processor_receives_project_name(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.structure_processor
        assert processor.project_name == factory.project_name

    def test_structure_processor_receives_queries(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.structure_processor
        assert processor.queries is factory.queries

    def test_definition_processor_receives_repo_path(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.definition_processor
        assert processor.repo_path == factory.repo_path

    def test_definition_processor_receives_project_name(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.definition_processor
        assert processor.project_name == factory.project_name

    def test_definition_processor_receives_function_registry(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.definition_processor
        assert processor.function_registry is factory.function_registry

    def test_definition_processor_receives_simple_name_lookup(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.definition_processor
        assert processor.simple_name_lookup is factory.simple_name_lookup

    def test_definition_processor_receives_import_processor(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.definition_processor
        assert processor.import_processor is factory.import_processor

    def test_definition_processor_shares_module_qn_to_file_path(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.definition_processor
        assert processor.module_qn_to_file_path is factory.module_qn_to_file_path

    def test_type_inference_receives_import_processor(
        self, factory: ProcessorFactory
    ) -> None:
        engine = factory.type_inference
        assert engine.import_processor is factory.import_processor

    def test_type_inference_receives_function_registry(
        self, factory: ProcessorFactory
    ) -> None:
        engine = factory.type_inference
        assert engine.function_registry is factory.function_registry

    def test_type_inference_receives_repo_path(self, factory: ProcessorFactory) -> None:
        engine = factory.type_inference
        assert engine.repo_path == factory.repo_path

    def test_type_inference_receives_project_name(
        self, factory: ProcessorFactory
    ) -> None:
        engine = factory.type_inference
        assert engine.project_name == factory.project_name

    def test_type_inference_receives_ast_cache(self, factory: ProcessorFactory) -> None:
        engine = factory.type_inference
        assert engine.ast_cache is factory.ast_cache

    def test_type_inference_receives_queries(self, factory: ProcessorFactory) -> None:
        engine = factory.type_inference
        assert engine.queries is factory.queries

    def test_type_inference_shares_module_qn_to_file_path(
        self, factory: ProcessorFactory
    ) -> None:
        engine = factory.type_inference
        assert engine.module_qn_to_file_path is factory.module_qn_to_file_path

    def test_type_inference_receives_class_inheritance_from_definition_processor(
        self, factory: ProcessorFactory
    ) -> None:
        engine = factory.type_inference
        definition_proc = factory.definition_processor
        assert engine.class_inheritance is definition_proc.class_inheritance

    def test_type_inference_receives_simple_name_lookup(
        self, factory: ProcessorFactory
    ) -> None:
        engine = factory.type_inference
        assert engine.simple_name_lookup is factory.simple_name_lookup

    def test_call_processor_receives_repo_path(self, factory: ProcessorFactory) -> None:
        processor = factory.call_processor
        assert processor.repo_path == factory.repo_path

    def test_call_processor_receives_project_name(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.call_processor
        assert processor.project_name == factory.project_name

    def test_call_processor_receives_function_registry(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.call_processor
        assert processor._resolver.function_registry is factory.function_registry

    def test_call_processor_receives_import_processor(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.call_processor
        assert processor._resolver.import_processor is factory.import_processor

    def test_call_processor_receives_type_inference(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.call_processor
        assert processor._resolver.type_inference is factory.type_inference

    def test_call_processor_receives_class_inheritance_from_definition_processor(
        self, factory: ProcessorFactory
    ) -> None:
        processor = factory.call_processor
        definition_proc = factory.definition_processor
        assert (
            processor._resolver.class_inheritance is definition_proc.class_inheritance
        )


class TestDependencyOrdering:
    def test_accessing_type_inference_initializes_definition_processor(
        self, factory: ProcessorFactory
    ) -> None:
        assert factory._definition_processor is None
        _ = factory.type_inference
        assert factory._definition_processor is not None

    def test_accessing_type_inference_initializes_import_processor(
        self, factory: ProcessorFactory
    ) -> None:
        assert factory._import_processor is None
        _ = factory.type_inference
        assert factory._import_processor is not None

    def test_accessing_call_processor_initializes_type_inference(
        self, factory: ProcessorFactory
    ) -> None:
        assert factory._type_inference is None
        _ = factory.call_processor
        assert factory._type_inference is not None

    def test_accessing_call_processor_initializes_definition_processor(
        self, factory: ProcessorFactory
    ) -> None:
        assert factory._definition_processor is None
        _ = factory.call_processor
        assert factory._definition_processor is not None

    def test_accessing_call_processor_initializes_import_processor(
        self, factory: ProcessorFactory
    ) -> None:
        assert factory._import_processor is None
        _ = factory.call_processor
        assert factory._import_processor is not None

    def test_accessing_definition_processor_initializes_import_processor(
        self, factory: ProcessorFactory
    ) -> None:
        assert factory._import_processor is None
        _ = factory.definition_processor
        assert factory._import_processor is not None


class TestSharedState:
    def test_module_qn_to_file_path_is_shared_dict(
        self, factory: ProcessorFactory
    ) -> None:
        definition_proc = factory.definition_processor
        type_inf = factory.type_inference

        test_path = Path("/test/path.py")
        factory.module_qn_to_file_path["test.module"] = test_path

        assert definition_proc.module_qn_to_file_path["test.module"] == test_path
        assert type_inf.module_qn_to_file_path["test.module"] == test_path

    def test_class_inheritance_is_shared_between_type_inference_and_call_processor(
        self, factory: ProcessorFactory
    ) -> None:
        definition_proc = factory.definition_processor
        type_inf = factory.type_inference
        call_proc = factory.call_processor

        definition_proc.class_inheritance["test.Child"] = ["test.Parent"]

        assert type_inf.class_inheritance["test.Child"] == ["test.Parent"]
        assert call_proc._resolver.class_inheritance["test.Child"] == ["test.Parent"]

    def test_function_registry_is_shared_across_processors(
        self, factory: ProcessorFactory
    ) -> None:
        from codebase_rag.types_defs import NodeType

        import_proc = factory.import_processor
        definition_proc = factory.definition_processor
        type_inf = factory.type_inference
        call_proc = factory.call_processor

        factory.function_registry["test.module.func"] = NodeType.FUNCTION

        assert "test.module.func" in import_proc.function_registry
        assert "test.module.func" in definition_proc.function_registry
        assert "test.module.func" in type_inf.function_registry
        assert "test.module.func" in call_proc._resolver.function_registry
