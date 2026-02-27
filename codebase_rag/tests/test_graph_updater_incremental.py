import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import (
    BoundedASTCache,
    FunctionRegistryTrie,
    GraphUpdater,
    _hash_file,
    _load_hash_cache,
    _save_hash_cache,
)
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )


@pytest.fixture
def py_project(temp_repo: Path) -> Path:
    (temp_repo / "__init__.py").touch()
    (temp_repo / "module_a.py").write_text("def func_a():\n    pass\n")
    (temp_repo / "module_b.py").write_text("def func_b():\n    pass\n")
    return temp_repo


class TestHashFile:
    def test_hash_returns_hex_string(self, temp_repo: Path) -> None:
        f = temp_repo / "test.py"
        f.write_text("hello")
        result = _hash_file(f)
        assert isinstance(result, str)
        assert len(result) == 64

    def test_same_content_same_hash(self, temp_repo: Path) -> None:
        f1 = temp_repo / "a.py"
        f2 = temp_repo / "b.py"
        f1.write_text("same content")
        f2.write_text("same content")
        assert _hash_file(f1) == _hash_file(f2)

    def test_different_content_different_hash(self, temp_repo: Path) -> None:
        f1 = temp_repo / "a.py"
        f2 = temp_repo / "b.py"
        f1.write_text("content one")
        f2.write_text("content two")
        assert _hash_file(f1) != _hash_file(f2)


class TestHashCacheIO:
    def test_save_and_load_cache(self, temp_repo: Path) -> None:
        cache_path = temp_repo / cs.HASH_CACHE_FILENAME
        data = {"module_a.py": "abc123", "module_b.py": "def456"}
        _save_hash_cache(cache_path, data)

        assert cache_path.is_file()
        loaded = _load_hash_cache(cache_path)
        assert loaded == data

    def test_load_nonexistent_returns_empty(self, temp_repo: Path) -> None:
        cache_path = temp_repo / cs.HASH_CACHE_FILENAME
        assert _load_hash_cache(cache_path) == {}

    def test_load_corrupted_returns_empty(self, temp_repo: Path) -> None:
        cache_path = temp_repo / cs.HASH_CACHE_FILENAME
        cache_path.write_text("not valid json {{{")
        assert _load_hash_cache(cache_path) == {}

    def test_save_creates_parent_dirs(self, temp_repo: Path) -> None:
        cache_path = temp_repo / "subdir" / "nested" / cs.HASH_CACHE_FILENAME
        _save_hash_cache(cache_path, {"a.py": "hash1"})
        assert cache_path.is_file()

    def test_cache_file_is_valid_json(self, temp_repo: Path) -> None:
        cache_path = temp_repo / cs.HASH_CACHE_FILENAME
        data = {"file.py": "sha256hash"}
        _save_hash_cache(cache_path, data)
        with cache_path.open() as f:
            parsed = json.load(f)
        assert parsed == data


class TestIncrementalUpdates:
    def test_unchanged_file_is_skipped(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        mock_ingestor.reset_mock()
        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        with patch.object(
            updater2, "_process_single_file", wraps=updater2._process_single_file
        ) as spy:
            updater2.run()
            assert spy.call_count == 0

    def test_changed_file_is_reparsed(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        (py_project / "module_a.py").write_text("def func_a_updated():\n    pass\n")

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        with patch.object(
            updater2, "_process_single_file", wraps=updater2._process_single_file
        ) as spy:
            updater2.run()
            processed_paths = [call.args[0] for call in spy.call_args_list]
            assert py_project / "module_a.py" in processed_paths

    def test_deleted_file_removed_from_state(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        (py_project / "module_b.py").unlink()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        with patch.object(
            updater2, "remove_file_from_state", wraps=updater2.remove_file_from_state
        ) as spy:
            updater2.run()
            removed_paths = [call.args[0] for call in spy.call_args_list]
            assert py_project / "module_b.py" in removed_paths

    def test_force_bypasses_cache(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        with patch.object(
            updater2, "_process_single_file", wraps=updater2._process_single_file
        ) as spy:
            updater2.run(force=True)
            assert spy.call_count > 0

    def test_new_file_is_processed(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        (py_project / "module_c.py").write_text("def func_c():\n    pass\n")

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        with patch.object(
            updater2, "_process_single_file", wraps=updater2._process_single_file
        ) as spy:
            updater2.run()
            processed_paths = [call.args[0] for call in spy.call_args_list]
            assert py_project / "module_c.py" in processed_paths

    def test_hash_cache_file_created_after_run(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        cache_path = py_project / cs.HASH_CACHE_FILENAME
        assert not cache_path.exists()

        updater.run()

        assert cache_path.is_file()
        with cache_path.open() as f:
            data = json.load(f)
        assert isinstance(data, dict)
        assert len(data) > 0

    def test_deleted_file_removed_from_hash_cache(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        cache_path = py_project / cs.HASH_CACHE_FILENAME
        with cache_path.open() as f:
            old_data = json.load(f)
        assert "module_b.py" in old_data

        (py_project / "module_b.py").unlink()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater2.run()

        with cache_path.open() as f:
            new_data = json.load(f)
        assert "module_b.py" not in new_data


class TestSlots:
    def test_function_registry_trie_has_slots(self) -> None:
        assert hasattr(FunctionRegistryTrie, "__slots__")
        trie = FunctionRegistryTrie()
        with pytest.raises(AttributeError):
            trie.nonexistent_attr = "value"  # type: ignore[attr-defined]

    def test_bounded_ast_cache_has_slots(self) -> None:
        assert hasattr(BoundedASTCache, "__slots__")
        cache = BoundedASTCache()
        with pytest.raises(AttributeError):
            cache.nonexistent_attr = "value"  # type: ignore[attr-defined]
