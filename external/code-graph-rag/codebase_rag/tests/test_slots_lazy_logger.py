from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.graph_loader import GraphLoader
from codebase_rag.providers.base import (
    GoogleProvider,
    ModelProvider,
    OllamaProvider,
    OpenAIProvider,
)
from codebase_rag.services.llm import CypherGenerator
from codebase_rag.tools.code_retrieval import CodeRetriever
from codebase_rag.tools.directory_lister import DirectoryLister
from codebase_rag.tools.document_analyzer import DocumentAnalyzer, _NotSupportedClient
from codebase_rag.tools.file_editor import FileEditor
from codebase_rag.tools.file_reader import FileReader
from codebase_rag.tools.file_writer import FileWriter
from codebase_rag.tools.health_checker import HealthChecker
from codebase_rag.tools.shell_command import CommandGroup, ShellCommander

REPO_ROOT = Path(__file__).resolve().parent.parent


SLOTS_CLASSES: list[tuple[type, tuple[str, ...]]] = [
    (_NotSupportedClient, ()),
    (DocumentAnalyzer, ("project_root", "client")),
    (FileEditor, ("project_root", "dmp", "parsers")),
    (CodeRetriever, ("project_root", "ingestor")),
    (FileReader, ("project_root",)),
    (FileWriter, ("project_root",)),
    (DirectoryLister, ("project_root",)),
    (CommandGroup, ("commands", "operator")),
    (ShellCommander, ("project_root", "timeout")),
    (HealthChecker, ("results",)),
    (CypherGenerator, ("agent",)),
    (ModelProvider, ("config",)),
    (
        GoogleProvider,
        (
            "api_key",
            "provider_type",
            "project_id",
            "region",
            "service_account_file",
            "thinking_budget",
        ),
    ),
    (OpenAIProvider, ("api_key", "endpoint")),
    (OllamaProvider, ("endpoint", "api_key")),
]

GRAPH_LOADER_SLOTS = (
    "file_path",
    "_data",
    "_nodes",
    "_relationships",
    "_nodes_by_id",
    "_nodes_by_label",
    "_outgoing_rels",
    "_incoming_rels",
    "_property_indexes",
)


class TestSlotsPresence:
    @pytest.mark.parametrize(
        ("cls", "expected_slots"),
        SLOTS_CLASSES,
        ids=[c.__name__ for c, _ in SLOTS_CLASSES],
    )
    def test_class_has_slots(self, cls: type, expected_slots: tuple[str, ...]) -> None:
        assert hasattr(cls, "__slots__")
        assert set(cls.__slots__) == set(expected_slots)

    def test_graph_loader_has_slots(self) -> None:
        assert hasattr(GraphLoader, "__slots__")
        assert set(GraphLoader.__slots__) == set(GRAPH_LOADER_SLOTS)


class TestSlotsBlockDict:
    def test_not_supported_client_no_dict(self) -> None:
        obj = _NotSupportedClient()
        with pytest.raises(NotImplementedError):
            obj.__dict__

    def test_command_group_no_dict(self) -> None:
        obj = CommandGroup(commands=["ls"], operator=None)
        assert not hasattr(obj, "__dict__")

    def test_directory_lister_no_dict(self, tmp_path: Path) -> None:
        obj = DirectoryLister(str(tmp_path))
        assert not hasattr(obj, "__dict__")

    def test_file_reader_no_dict(self, tmp_path: Path) -> None:
        obj = FileReader(str(tmp_path))
        assert not hasattr(obj, "__dict__")

    def test_file_writer_no_dict(self, tmp_path: Path) -> None:
        obj = FileWriter(str(tmp_path))
        assert not hasattr(obj, "__dict__")

    def test_health_checker_no_dict(self) -> None:
        obj = HealthChecker()
        assert not hasattr(obj, "__dict__")

    def test_shell_commander_no_dict(self, tmp_path: Path) -> None:
        obj = ShellCommander(str(tmp_path))
        assert not hasattr(obj, "__dict__")

    def test_code_retriever_no_dict(self, tmp_path: Path) -> None:
        mock_ingestor = MagicMock()
        obj = CodeRetriever(str(tmp_path), mock_ingestor)
        assert not hasattr(obj, "__dict__")


class TestSlotsRejectArbitraryAttrs:
    def test_not_supported_client_rejects_attr(self) -> None:
        obj = _NotSupportedClient()
        with pytest.raises((AttributeError, NotImplementedError)):
            obj.arbitrary = 42

    def test_command_group_rejects_attr(self) -> None:
        obj = CommandGroup(commands=["ls"], operator=None)
        with pytest.raises(AttributeError):
            obj.arbitrary = 42

    def test_directory_lister_rejects_attr(self, tmp_path: Path) -> None:
        obj = DirectoryLister(str(tmp_path))
        with pytest.raises(AttributeError):
            obj.arbitrary = 42

    def test_health_checker_rejects_attr(self) -> None:
        obj = HealthChecker()
        with pytest.raises(AttributeError):
            obj.arbitrary = 42

    def test_shell_commander_rejects_attr(self, tmp_path: Path) -> None:
        obj = ShellCommander(str(tmp_path))
        with pytest.raises(AttributeError):
            obj.arbitrary = 42


LAZY_LOGGER_FILES: list[str] = [
    "parser_loader.py",
    "utils/fqn_resolver.py",
    "utils/source_extraction.py",
    "tools/document_analyzer.py",
    "tools/file_editor.py",
]


def _find_eager_debug_calls(source: str) -> list[str]:
    results = []
    lines = source.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("logger.debug("):
            block = stripped
            j = i
            paren_count = block.count("(") - block.count(")")
            while paren_count > 0 and j + 1 < len(lines):
                j += 1
                block += " " + lines[j].strip()
                paren_count += lines[j].count("(") - lines[j].count(")")
            if ".format(" in block:
                results.append(block[:80])
            i = j + 1
        else:
            i += 1
    return results


class TestLazyLoggerFormat:
    @pytest.mark.parametrize("rel_path", LAZY_LOGGER_FILES)
    def test_no_eager_debug_format(self, rel_path: str) -> None:
        file_path = REPO_ROOT / rel_path
        source = file_path.read_text(encoding="utf-8")
        eager_calls = _find_eager_debug_calls(source)
        assert len(eager_calls) == 0, (
            f"Found {len(eager_calls)} eager logger.debug(.format()) calls in {rel_path}: {eager_calls}"
        )


class TestProviderSlotsInheritance:
    def test_google_provider_inherits_config_slot(self) -> None:
        assert "config" in ModelProvider.__slots__
        assert "config" not in GoogleProvider.__slots__

    def test_openai_provider_inherits_config_slot(self) -> None:
        assert "config" not in OpenAIProvider.__slots__

    def test_ollama_provider_inherits_config_slot(self) -> None:
        assert "config" not in OllamaProvider.__slots__

    @patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"})
    def test_google_provider_instance_has_all_attrs(self) -> None:
        provider = GoogleProvider(api_key="test-key")
        assert provider.api_key == "test-key"
        assert provider.config == {}

    def test_openai_provider_instance_has_all_attrs(self) -> None:
        provider = OpenAIProvider(api_key="test-key")
        assert provider.api_key == "test-key"
        assert provider.config == {}

    @patch("codebase_rag.providers.base.settings")
    def test_ollama_provider_instance_has_all_attrs(
        self, mock_settings: MagicMock
    ) -> None:
        mock_settings.ollama_endpoint = "http://localhost:11434/v1/"
        provider = OllamaProvider()
        assert provider.endpoint == "http://localhost:11434/v1/"
        assert provider.config == {}
