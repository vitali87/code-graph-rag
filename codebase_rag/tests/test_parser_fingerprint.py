# A graph is a function of (source files, parser code) but the incremental
# hash cache keys only the source files: after a parser change an
# incremental sync silently keeps every edge the OLD parser produced for
# unchanged files. These tests pin the parser-fingerprint safeguard: full
# syncs stamp the fingerprint of the parser that built the graph, and any
# later sync against a different parser warns loudly until a clean rebuild.
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

    @pytest.mark.parametrize("filename", ["function_registry.py", "ast_cache.py"])
    def test_changes_when_extracted_parser_module_changes(
        self, tmp_path: Path, filename: str
    ) -> None:
        # FunctionRegistryTrie and BoundedASTCache moved out of graph_updater.py
        # into their own modules; both still decide how sources become edges, so
        # an edit to either must trip the fingerprint even though graph_updater
        # itself is unchanged.
        assert filename in cs.PARSER_FINGERPRINT_SOURCE_FILES
        pkg = tmp_path / "pkg"
        pkg.mkdir(parents=True)
        source = pkg / filename
        source.write_text("A = 1\n")
        before = compute_parser_fingerprint(pkg)
        source.write_text("A = 2\n")
        assert compute_parser_fingerprint(pkg) != before

    def test_unchanged_tree_same_fingerprint(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        parsers_dir = pkg / cs.PARSER_FINGERPRINT_SOURCE_DIRS[0]
        parsers_dir.mkdir(parents=True)
        (parsers_dir / "some_parser.py").write_text("A = 1\n")
        first = compute_parser_fingerprint(pkg)
        second = compute_parser_fingerprint(pkg)
        assert first == second

    def test_changes_when_roslyn_tool_source_changes(self, tmp_path: Path) -> None:
        # The bundled Roslyn frontend tool (.cs/.csproj) is parser code: an
        # edit to it must change the fingerprint so a re-index warns even when
        # the user's C# sources are unchanged (issue #738).
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
        # The frontend selection is part of the parser identity: flipping it
        # rewrites edges for unchanged sources, so it must change the
        # fingerprint and trip the staleness warning (issue #738).
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
        # Incremental syncs keep old-parser edges for unchanged files, so
        # re-stamping would silence the warning while the graph stays stale.
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
        # The fast path skips all passes, which is exactly the silent
        # no-op that must not hide a stale graph.
        _make_updater(py_project, mock_ingestor).run()
        _fingerprint_path(py_project).write_text(STALE_FINGERPRINT, encoding="utf-8")

        updater = _make_updater(py_project, mock_ingestor)
        assert updater._is_already_in_sync() is True
        updater.run()

        assert any(ls.PARSER_FINGERPRINT_MISMATCH in m for m in warnings_sink)

    def test_missing_stamp_with_existing_cache_warns(
        self, py_project: Path, mock_ingestor: MagicMock, warnings_sink: list[str]
    ) -> None:
        # A graph synced before this safeguard existed was built by an
        # unknown parser: treat it as stale until a clean rebuild.
        _make_updater(py_project, mock_ingestor).run()
        _fingerprint_path(py_project).unlink()

        _make_updater(py_project, mock_ingestor).run()

        assert any(ls.PARSER_FINGERPRINT_MISMATCH in m for m in warnings_sink)


class TestStampIO:
    def test_unwritable_stamp_warns_without_raising(
        self, tmp_path: Path, warnings_sink: list[str]
    ) -> None:
        # The stamp is a best-effort safeguard: a failed write must not
        # abort the sync that just succeeded.
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


def test_fingerprint_resolves_auto_to_effective_frontend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AUTO's fingerprint must reflect what actually RAN: a graph built with
    # dotnet present carries hybrid edges, one built without does not, and
    # the two must not share a fingerprint just because the setting string
    # is the same.
    import codebase_rag.parser_fingerprint as pf
    from codebase_rag.parsers.csharp_frontend import frontend as fe

    monkeypatch.setattr(pf.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.AUTO)
    monkeypatch.setattr(fe, "csharp_frontend_available", lambda: True)
    fp_auto_with_dotnet = pf.compute_parser_fingerprint()
    monkeypatch.setattr(fe, "csharp_frontend_available", lambda: False)
    fp_auto_without_dotnet = pf.compute_parser_fingerprint()
    assert fp_auto_with_dotnet != fp_auto_without_dotnet

    monkeypatch.setattr(pf.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.HYBRID)
    monkeypatch.setattr(fe, "csharp_frontend_available", lambda: True)
    assert pf.compute_parser_fingerprint() == fp_auto_with_dotnet
    # An EXPLICIT hybrid request that cannot run degrades the build to
    # tree-sitter (graph_updater warns and returns), so the fingerprint
    # must record what actually ran there too, not the setting string.
    monkeypatch.setattr(fe, "csharp_frontend_available", lambda: False)
    assert pf.compute_parser_fingerprint() == fp_auto_without_dotnet
    monkeypatch.setattr(pf.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.TREESITTER)
    assert pf.compute_parser_fingerprint() == fp_auto_without_dotnet
