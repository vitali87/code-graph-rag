# (H) A graph is a function of (source files, parser code) but the incremental
# (H) hash cache keys only the source files: after a parser change an
# (H) incremental sync silently keeps every edge the OLD parser produced for
# (H) unchanged files. These tests pin the parser-fingerprint safeguard: full
# (H) syncs stamp the fingerprint of the parser that built the graph, and any
# (H) later sync against a different parser warns loudly until a clean rebuild.
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.cli import _delete_hash_cache
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_fingerprint import compute_parser_fingerprint
from codebase_rag.parser_loader import load_parsers

STALE_FINGERPRINT = "0" * 32


@pytest.fixture
def py_project(temp_repo: Path) -> Path:
    (temp_repo / "module_a.py").write_text("def func_a():\n    pass\n")
    return temp_repo


@pytest.fixture
def warnings_sink() -> Iterator[list[str]]:
    messages: list[str] = []
    handler_id = logger.add(
        lambda m: messages.append(str(m)), level="WARNING", format="{message}"
    )
    yield messages
    logger.remove(handler_id)


def _make_updater(repo: Path, mock_ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
    )


def _fingerprint_path(repo: Path) -> Path:
    return repo / cs.PARSER_FINGERPRINT_FILENAME


class TestComputeParserFingerprint:
    def test_deterministic_hex_digest(self) -> None:
        first = compute_parser_fingerprint()
        second = compute_parser_fingerprint()
        assert first == second
        assert len(first) == 32
        int(first, 16)

    def test_changes_when_parser_source_changes(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        parsers_dir = pkg / cs.PARSER_FINGERPRINT_SOURCE_DIRS[0]
        parsers_dir.mkdir(parents=True)
        source = parsers_dir / "some_parser.py"
        source.write_text("A = 1\n")
        before = compute_parser_fingerprint(pkg)
        source.write_text("A = 2\n")
        assert compute_parser_fingerprint(pkg) != before

    def test_unchanged_tree_same_fingerprint(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        parsers_dir = pkg / cs.PARSER_FINGERPRINT_SOURCE_DIRS[0]
        parsers_dir.mkdir(parents=True)
        (parsers_dir / "some_parser.py").write_text("A = 1\n")
        assert compute_parser_fingerprint(pkg) == compute_parser_fingerprint(pkg)

    def test_changes_when_roslyn_tool_source_changes(self, tmp_path: Path) -> None:
        # (H) The bundled Roslyn frontend tool (.cs/.csproj) is parser code: an
        # (H) edit to it must change the fingerprint so a re-index warns even when
        # (H) the user's C# sources are unchanged (issue #738).
        pkg = tmp_path / "pkg"
        tool_dir = pkg / cs.PARSER_FINGERPRINT_TOOL_DIR
        tool_dir.mkdir(parents=True)
        source = tool_dir / "Frontend.cs"
        source.write_text("class A { }\n")
        before = compute_parser_fingerprint(pkg)
        source.write_text("class A { void M() { } }\n")
        assert compute_parser_fingerprint(pkg) != before

    def test_changes_when_csharp_frontend_setting_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # (H) The frontend selection is part of the parser identity: flipping it
        # (H) rewrites edges for unchanged sources, so it must change the
        # (H) fingerprint and trip the staleness warning (issue #738).
        from codebase_rag.config import settings as cfg

        monkeypatch.setattr(cfg, "CSHARP_FRONTEND", cs.CSharpFrontend.TREESITTER)
        before = compute_parser_fingerprint()
        monkeypatch.setattr(cfg, "CSHARP_FRONTEND", cs.CSharpFrontend.HYBRID)
        assert compute_parser_fingerprint() != before


class TestFingerprintStamping:
    def test_full_sync_stamps_current_fingerprint(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        _make_updater(py_project, mock_ingestor).run()

        stamp = _fingerprint_path(py_project)
        assert stamp.is_file()
        assert stamp.read_text(encoding="utf-8").strip() == (
            compute_parser_fingerprint()
        )

    def test_incremental_sync_does_not_overwrite_stale_stamp(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # (H) Incremental syncs keep old-parser edges for unchanged files, so
        # (H) re-stamping would silence the warning while the graph stays stale.
        _make_updater(py_project, mock_ingestor).run()
        _fingerprint_path(py_project).write_text(STALE_FINGERPRINT, encoding="utf-8")

        (py_project / "module_b.py").write_text("def func_b():\n    pass\n")
        _make_updater(py_project, mock_ingestor).run()

        stored = _fingerprint_path(py_project).read_text(encoding="utf-8").strip()
        assert stored == STALE_FINGERPRINT

    def test_forced_rebuild_refreshes_stale_stamp(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        _make_updater(py_project, mock_ingestor).run()
        _fingerprint_path(py_project).write_text(STALE_FINGERPRINT, encoding="utf-8")

        _make_updater(py_project, mock_ingestor).run(force=True)

        stored = _fingerprint_path(py_project).read_text(encoding="utf-8").strip()
        assert stored == compute_parser_fingerprint()

    def test_stamp_file_is_not_indexed(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        _make_updater(py_project, mock_ingestor).run()
        _make_updater(py_project, mock_ingestor).run()

        from codebase_rag.graph_updater import _load_hash_cache

        hashes = _load_hash_cache(py_project / cs.HASH_CACHE_FILENAME)
        assert cs.PARSER_FINGERPRINT_FILENAME not in hashes

    def test_stamp_file_does_not_break_fast_path(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        _make_updater(py_project, mock_ingestor).run()
        assert _fingerprint_path(py_project).is_file()

        updater = _make_updater(py_project, mock_ingestor)
        assert updater._is_already_in_sync() is True


class TestStalenessWarning:
    def test_fresh_sync_does_not_warn(
        self, py_project: Path, mock_ingestor: MagicMock, warnings_sink: list[str]
    ) -> None:
        _make_updater(py_project, mock_ingestor).run()
        assert not any(ls.PARSER_FINGERPRINT_MISMATCH in m for m in warnings_sink)

    def test_incremental_sync_with_matching_stamp_does_not_warn(
        self, py_project: Path, mock_ingestor: MagicMock, warnings_sink: list[str]
    ) -> None:
        _make_updater(py_project, mock_ingestor).run()
        _make_updater(py_project, mock_ingestor).run()
        assert not any(ls.PARSER_FINGERPRINT_MISMATCH in m for m in warnings_sink)

    def test_incremental_sync_with_stale_stamp_warns(
        self, py_project: Path, mock_ingestor: MagicMock, warnings_sink: list[str]
    ) -> None:
        _make_updater(py_project, mock_ingestor).run()
        _fingerprint_path(py_project).write_text(STALE_FINGERPRINT, encoding="utf-8")

        _make_updater(py_project, mock_ingestor).run()

        assert any(ls.PARSER_FINGERPRINT_MISMATCH in m for m in warnings_sink)

    def test_in_sync_fast_path_still_warns_on_stale_stamp(
        self, py_project: Path, mock_ingestor: MagicMock, warnings_sink: list[str]
    ) -> None:
        # (H) The fast path skips all passes, which is exactly the silent
        # (H) no-op that must not hide a stale graph.
        _make_updater(py_project, mock_ingestor).run()
        _fingerprint_path(py_project).write_text(STALE_FINGERPRINT, encoding="utf-8")

        updater = _make_updater(py_project, mock_ingestor)
        assert updater._is_already_in_sync() is True
        updater.run()

        assert any(ls.PARSER_FINGERPRINT_MISMATCH in m for m in warnings_sink)

    def test_missing_stamp_with_existing_cache_warns(
        self, py_project: Path, mock_ingestor: MagicMock, warnings_sink: list[str]
    ) -> None:
        # (H) A graph synced before this safeguard existed was built by an
        # (H) unknown parser: treat it as stale until a clean rebuild.
        _make_updater(py_project, mock_ingestor).run()
        _fingerprint_path(py_project).unlink()

        _make_updater(py_project, mock_ingestor).run()

        assert any(ls.PARSER_FINGERPRINT_MISMATCH in m for m in warnings_sink)


class TestStampIO:
    def test_unwritable_stamp_warns_without_raising(
        self, tmp_path: Path, warnings_sink: list[str]
    ) -> None:
        # (H) The stamp is a best-effort safeguard: a failed write must not
        # (H) abort the sync that just succeeded.
        from codebase_rag.graph_updater import _save_parser_fingerprint

        stamp_dir = tmp_path / cs.PARSER_FINGERPRINT_FILENAME
        stamp_dir.mkdir()

        _save_parser_fingerprint(stamp_dir, STALE_FINGERPRINT)

        assert any(str(stamp_dir) in m for m in warnings_sink)


class TestCleanRemovesStamp:
    def test_delete_hash_cache_removes_fingerprint_stamp(self, tmp_path: Path) -> None:
        for name in (
            cs.HASH_CACHE_FILENAME,
            cs.DIR_MTIMES_FILENAME,
            cs.PARSER_FINGERPRINT_FILENAME,
        ):
            (tmp_path / name).write_text(cs.JSON_EMPTY_OBJECT, encoding="utf-8")

        _delete_hash_cache(tmp_path)

        assert not (tmp_path / cs.PARSER_FINGERPRINT_FILENAME).exists()
        assert not (tmp_path / cs.HASH_CACHE_FILENAME).exists()
        assert not (tmp_path / cs.DIR_MTIMES_FILENAME).exists()
