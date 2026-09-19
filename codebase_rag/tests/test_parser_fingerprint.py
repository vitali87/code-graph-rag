# A graph is a function of (source files, parser code) but the incremental
# hash cache keys only the source files: after a parser change an
# incremental sync silently keeps every edge the OLD parser produced for
# unchanged files. These tests pin the parser-fingerprint safeguard: full
# syncs stamp the fingerprint of the parser that built the graph, and any
# later sync against a different parser warns, re-parses every file once and
# re-stamps (issue #1977).
import ast
import inspect
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import IO
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.cli import _delete_hash_cache
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_fingerprint import compute_parser_fingerprint
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict
from codebase_rag.utils.path_utils import base_module_qn

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


def _make_updater(
    repo: Path,
    mock_ingestor: MagicMock,
    capture: "CaptureSelection | None" = None,
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=capture,
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

    def test_changes_when_the_module_qn_rule_changes(self, tmp_path: Path) -> None:
        """`base_module_qn` decides every module's identity, so a change to it
        must re-key the graph.

        It lives in `utils/path_utils.py`, which was in NEITHER the directory
        globs (`parsers`, `constants`) nor the file list, so a change touching
        only that file left existing indexes on their old module names with no
        staleness warning — the graph silently disagreeing with the code that
        built it (issue #1720 review).
        """
        assert "utils/path_utils.py" in cs.PARSER_FINGERPRINT_SOURCE_FILES
        pkg = tmp_path / "pkg"
        (pkg / "utils").mkdir(parents=True)
        source = pkg / "utils" / "path_utils.py"
        source.write_text("A = 1\n")
        before = compute_parser_fingerprint(pkg)
        source.write_text("A = 2\n")
        assert compute_parser_fingerprint(pkg) != before

    def test_every_module_qn_deriving_source_is_a_fingerprint_input(self) -> None:
        """The forcing function the case above cannot provide.

        That test names the file I already know about. This one asks the
        question from the other end, and follows the rule rather than one
        file: `base_module_qn` AND every package-internal module or callable
        it references must be a fingerprint input.

        Checking only the defining file would pass a SPLIT refactor -- the
        wrapper stays in `utils/path_utils.py` while the arithmetic moves to
        an unlisted helper, and edits to the helper then leave existing graphs
        on stale identities with the guard still green (raised on #1722). The
        property is "everything the identity rule depends on", not "the file
        it currently lives in".
        """
        package_root = Path(cs.__file__).resolve().parent.parent

        def _rel(obj: object) -> str | None:
            try:
                source = inspect.getsourcefile(obj)  # type: ignore[arg-type]
            except TypeError:
                return None
            if not source:
                return None
            path = Path(source).resolve()
            if not path.is_relative_to(package_root):
                return None  # stdlib and third-party are not ours to fingerprint
            return path.relative_to(package_root).as_posix()

        def _covered(rel: str) -> bool:
            return rel in cs.PARSER_FINGERPRINT_SOURCE_FILES or rel.startswith(
                tuple(f"{d}/" for d in cs.PARSER_FINGERPRINT_SOURCE_DIRS)
            )

        defining_module = inspect.getmodule(base_module_qn)
        assert defining_module is not None
        tree = ast.parse(textwrap.dedent(inspect.getsource(base_module_qn)))
        referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        # The definition itself, plus every package-internal name its body
        # actually reaches for (`cs` resolves to the constants package, which
        # the directory glob already covers).
        subjects = {base_module_qn} | {
            obj
            for name in referenced
            if (obj := getattr(defining_module, name, None)) is not None
        }
        uncovered = sorted(
            rel for obj in subjects if (rel := _rel(obj)) and not _covered(rel)
        )
        assert not uncovered, (
            "the module-qn rule depends on these files, and a change to any of "
            f"them would not refresh the parser fingerprint: {uncovered}"
        )

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
        # fingerprint and trip the staleness warning (issue #738). The
        # fingerprint records the RESOLVED mode, and without a dotnet
        # toolchain HYBRID degrades to TREESITTER so both fingerprints
        # collide; availability is stubbed so the test is hermetic on
        # hosts without dotnet while still exercising the real
        # resolution logic (issue #1101).
        from codebase_rag.config import settings as cfg
        from codebase_rag.parsers.csharp_frontend import frontend

        monkeypatch.setattr(frontend, "csharp_frontend_available", lambda: True)
        monkeypatch.setattr(cfg, "CSHARP_FRONTEND", cs.CSharpFrontend.TREESITTER)
        before = compute_parser_fingerprint()
        monkeypatch.setattr(cfg, "CSHARP_FRONTEND", cs.CSharpFrontend.HYBRID)
        assert compute_parser_fingerprint() != before

    def test_changes_when_cpp_frontend_setting_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codebase_rag.config import settings as cfg
        from codebase_rag.parsers.cpp_frontend import frontend

        monkeypatch.setattr(frontend, "cpp_frontend_available", lambda: True)
        monkeypatch.setattr(cfg, "CPP_FRONTEND", cs.CppFrontend.TREESITTER)
        before = compute_parser_fingerprint()
        monkeypatch.setattr(cfg, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
        assert compute_parser_fingerprint() != before

    def test_changes_when_libclang_becomes_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Installing the [cpp] extra flips the default HYBRID from degraded
        # tree-sitter output to real hybrid facts for unchanged sources, so
        # the fingerprint must record the RESOLVED mode and read the old
        # graph as stale (issue #1177), mirroring the C# entry above.
        from codebase_rag.config import settings as cfg
        from codebase_rag.parsers.cpp_frontend import frontend

        monkeypatch.setattr(cfg, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
        monkeypatch.setattr(frontend, "cpp_frontend_available", lambda: False)
        before = compute_parser_fingerprint()
        monkeypatch.setattr(frontend, "cpp_frontend_available", lambda: True)
        assert compute_parser_fingerprint() != before

    def test_streams_compilation_database_digest_in_bounded_chunks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # compile_commands.json is repository-controlled and can be very
        # large, so the digest is streamed in bounded chunks: reading it whole
        # lets an oversized database exhaust memory and fail indexing
        # (issue #1177 review).
        import codebase_rag.parser_fingerprint as pf
        from codebase_rag.config import settings as cfg
        from codebase_rag.parsers.cpp_frontend import frontend

        payload = b"x" * (pf._FILE_HASH_CHUNK_SIZE * 2 + 17)
        (tmp_path / "compile_commands.json").write_bytes(payload)
        monkeypatch.setattr(cfg, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
        monkeypatch.setattr(frontend, "cpp_frontend_available", lambda: True)

        read_sizes: list[int] = []
        original_open = Path.open

        class RecordingStream:
            def __init__(self, stream: IO[bytes]) -> None:
                self._stream = stream

            def read(self, size: int = -1) -> bytes:
                read_sizes.append(size)
                return self._stream.read(size)

            def __enter__(self) -> "RecordingStream":
                return self

            def __exit__(self, *_exc: object) -> None:
                self._stream.close()

        def tracked_open(self: Path, mode: str = "rb") -> RecordingStream:
            return RecordingStream(original_open(self, "rb"))

        monkeypatch.setattr(Path, "open", tracked_open)
        entries = pf._repo_frontend_inputs(tmp_path)

        assert any(entry.startswith("CPP_COMPDB=") for entry in entries), entries
        assert read_sizes, "compile_commands.json was never streamed"
        assert all(0 < size <= pf._FILE_HASH_CHUNK_SIZE for size in read_sizes), (
            read_sizes
        )


class TestFingerprintStamping:
    def test_full_sync_stamps_current_fingerprint(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        _make_updater(py_project, mock_ingestor).run()

        stamp = _fingerprint_path(py_project)
        assert stamp.is_file()
        assert stamp.read_text(encoding="utf-8").strip() == (
            compute_parser_fingerprint(repo_path=py_project)
        )

    def test_a_full_build_that_dies_before_the_flush_leaves_no_stamp(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """The stamp must not outlive a build whose writes never became durable.

        Stamped inside `_process_files`, before the final flush, a full build
        that died in between left a fingerprint claiming this parser's edges
        were in the graph while the graph held none of them; the next run
        compared equal and `_reparse_all_if_parser_changed` stayed silent for exactly
        the run that failed (issue #1634). The exclusion stamp, the hash cache
        and the directory mtimes already commit after the flush; this pins the
        fingerprint to the same point.

        `_prune_orphan_nodes` sits between the old stamp site and the final
        flush, so raising from it is a death in that window.
        """
        updater = _make_updater(py_project, mock_ingestor)
        with (
            patch.object(
                updater,
                "_prune_orphan_nodes",
                side_effect=RuntimeError("died before the final flush"),
            ),
            pytest.raises(RuntimeError, match="died before the final flush"),
        ):
            updater.run()

        assert not _fingerprint_path(py_project).exists(), (
            "a full build that died before its final flush left a parser "
            "fingerprint, so the next run will not warn that the graph was "
            "built by different parser code"
        )

        # The control: the same build, allowed to reach its commit point,
        # does stamp -- so the absence above is the deferral and not a stamp
        # that never happens.
        _make_updater(py_project, mock_ingestor).run()
        assert _fingerprint_path(py_project).is_file()

    def test_a_stale_stamp_reparses_every_file_once_and_is_refreshed(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """An incremental sync used to keep the old inputs' results for every
        unchanged file, so a property a newer parser adds was never written
        until the file happened to change (issue #1977). The sync now
        ignores the hash cache once: every file re-parses, the completed
        run stamps the new fingerprint, and the run after parses nothing."""
        (py_project / "module_b.py").write_text("def func_b():\n    pass\n")
        _make_updater(py_project, mock_ingestor).run()
        _fingerprint_path(py_project).write_text(STALE_FINGERPRINT, encoding="utf-8")

        updater = _make_updater(py_project, mock_ingestor)
        updater.run()

        assert updater._reparsed_file_keys == {"module_a.py", "module_b.py"}
        stored = _fingerprint_path(py_project).read_text(encoding="utf-8").strip()
        assert stored == compute_parser_fingerprint(repo_path=py_project)
        settled = _make_updater(py_project, mock_ingestor)
        assert settled._is_already_in_sync() is True
        settled.run()
        assert settled._reparsed_file_keys == set()

    def test_a_stale_stamp_re_indexes_rather_than_rebuilds(
        self, py_project: Path
    ) -> None:
        """The forced re-parse is a RE-INDEX of an indexed tree, not a first
        build: each file's previous subtree is deleted first and its registry
        entries dropped, so a reused updater does not register every
        definition again as an `@N` duplicate beside the old one (caught by
        the delombok overlay test in CI on the #1977 fix)."""
        from evals.cgr_graph import _StatefulIngestor

        store = _StatefulIngestor()
        updater = _make_updater(py_project, store)  # type: ignore[arg-type]
        updater.run()
        _fingerprint_path(py_project).write_text(STALE_FINGERPRINT, encoding="utf-8")
        updater.run()

        assert updater._is_full_build is False
        assert updater._reparsed_file_keys == {"module_a.py"}
        qn = f"{base_module_qn(Path('module_a.py'), py_project.name)}.func_a"
        functions = sorted(
            name for label, name in store.nodes if label == cs.NodeLabel.FUNCTION.value
        )
        assert functions == [qn]
        assert updater.factory.function_registry.variants(qn) == [qn]
        stored = _fingerprint_path(py_project).read_text(encoding="utf-8").strip()
        assert stored == compute_parser_fingerprint(repo_path=py_project)

    def test_a_stale_stamp_still_drops_a_file_deleted_since_the_last_run(
        self, py_project: Path
    ) -> None:
        """The cache's keys move into the forced re-parse set on a parser
        change, so a file the cache names and the walk no longer finds must
        still leave the graph: Module, definitions and File node."""
        from evals.cgr_graph import _StatefulIngestor

        store = _StatefulIngestor()
        (py_project / "module_b.py").write_text("def func_b():\n    pass\n")
        _make_updater(py_project, store).run()  # type: ignore[arg-type]
        module_b = base_module_qn(Path("module_b.py"), py_project.name)
        assert (cs.NodeLabel.MODULE.value, module_b) in store.nodes
        (py_project / "module_b.py").unlink()
        _fingerprint_path(py_project).write_text(STALE_FINGERPRINT, encoding="utf-8")
        # The store swallows a module delete and a File delete alike, so the
        # statements themselves are recorded: the ORDER shows the deletion
        # happened before the re-parse wrote anything (the same-stem rule the
        # overlay path follows), and the File delete is the one statement the
        # module subtree delete does not cover.
        events: list[tuple[str, str]] = []
        real_write, real_node = store.execute_write, store.ensure_node_batch

        def spy_write(query: str, params: PropertyDict | None = None) -> None:
            path = str((params or {}).get(cs.KEY_PATH, ""))
            if query == cs.CYPHER_DELETE_MODULE:
                events.append(("delete_module", path))
            elif query == cs.CYPHER_DELETE_FILE:
                events.append(("delete_file", path))
            real_write(query, params)

        def spy_node(label: str, properties: PropertyDict) -> None:
            events.append(("node", str(properties.get(cs.KEY_QUALIFIED_NAME, ""))))
            real_node(label, properties)

        store.execute_write = spy_write  # type: ignore[method-assign]
        store.ensure_node_batch = spy_node  # type: ignore[method-assign]

        updater = _make_updater(py_project, store)  # type: ignore[arg-type]
        updater.run()

        assert updater._reparsed_file_keys == {"module_a.py"}
        assert (cs.NodeLabel.MODULE.value, module_b) not in store.nodes
        assert (cs.NodeLabel.FUNCTION.value, f"{module_b}.func_b") not in store.nodes
        module_a = base_module_qn(Path("module_a.py"), py_project.name)
        gone_first = events.index(("delete_module", "module_b.py"))
        reparsed = events.index(("node", module_a))
        assert gone_first < reparsed, events
        assert any(
            kind == "delete_file" and path.endswith("module_b.py")
            for kind, path in events
        ), events

    def test_a_stale_stamp_over_an_empty_cache_is_a_full_build(
        self, py_project: Path
    ) -> None:
        """A cache file that loads empty (corrupt JSON here) names nothing to
        force, so the run stays the full build it always was and asks the
        graph what it holds; a reused updater still ends with one node per
        definition (bot review)."""
        from evals.cgr_graph import _StatefulIngestor

        store = _StatefulIngestor()
        updater = _make_updater(py_project, store)  # type: ignore[arg-type]
        updater.run()
        (py_project / cs.HASH_CACHE_FILENAME).write_text('{"module_a.py": "ab')
        _fingerprint_path(py_project).write_text(STALE_FINGERPRINT, encoding="utf-8")
        updater.run()

        assert updater._is_full_build is True
        qn = f"{base_module_qn(Path('module_a.py'), py_project.name)}.func_a"
        functions = sorted(
            name for label, name in store.nodes if label == cs.NodeLabel.FUNCTION.value
        )
        assert functions == [qn]
        assert updater.factory.function_registry.variants(qn) == [qn]

    def test_an_unchanged_stamp_reparses_nothing(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """The control: with the stamp matching, the cache is trusted and an
        unchanged file is not re-parsed."""
        _make_updater(py_project, mock_ingestor).run()
        (py_project / "module_b.py").write_text("def func_b():\n    pass\n")

        updater = _make_updater(py_project, mock_ingestor)
        updater.run()

        assert updater._reparsed_file_keys == {"module_b.py"}

    def test_forced_rebuild_refreshes_stale_stamp(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        _make_updater(py_project, mock_ingestor).run()
        _fingerprint_path(py_project).write_text(STALE_FINGERPRINT, encoding="utf-8")

        _make_updater(py_project, mock_ingestor).run(force=True)

        stored = _fingerprint_path(py_project).read_text(encoding="utf-8").strip()
        assert stored == compute_parser_fingerprint(repo_path=py_project)

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
        # A stale stamp must warn even when nothing on disk changed: the
        # run re-parses rather than taking the fast path (issue #1977).
        _make_updater(py_project, mock_ingestor).run()
        _fingerprint_path(py_project).write_text(STALE_FINGERPRINT, encoding="utf-8")

        updater = _make_updater(py_project, mock_ingestor)
        assert updater._is_already_in_sync() is True
        updater.run()

        assert any(ls.PARSER_FINGERPRINT_MISMATCH in m for m in warnings_sink)

    def test_enabling_a_capture_group_warns(
        self, py_project: Path, mock_ingestor: MagicMock, warnings_sink: list[str]
    ) -> None:
        # The defect in #1630, end to end through the updater rather than
        # against compute_parser_fingerprint directly: the stamp is written
        # with the selection that built the graph and compared against the
        # one running now, so dropping `capture=self.capture` at either call
        # site brings the silent no-op back.
        _make_updater(py_project, mock_ingestor).run()

        _make_updater(py_project, mock_ingestor, resolve_capture(["io"])).run()

        assert any(ls.PARSER_FINGERPRINT_MISMATCH in m for m in warnings_sink)

    def test_same_capture_group_twice_does_not_warn(
        self, py_project: Path, mock_ingestor: MagicMock, warnings_sink: list[str]
    ) -> None:
        # Control for the test above: it must warn because the selection
        # CHANGED, not because a non-default selection warns unconditionally
        # or because the entries hash differently each run. Without this, an
        # unstable digest would look like a working fix.
        _make_updater(py_project, mock_ingestor, resolve_capture(["io"])).run()

        _make_updater(py_project, mock_ingestor, resolve_capture(["io"])).run()

        assert not any(ls.PARSER_FINGERPRINT_MISMATCH in m for m in warnings_sink)

    def test_missing_stamp_with_existing_cache_warns(
        self, py_project: Path, mock_ingestor: MagicMock, warnings_sink: list[str]
    ) -> None:
        # A graph synced before this safeguard existed was built by an
        # unknown parser: treat it as stale and re-parse every file once.
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
    from codebase_rag.config import settings as cfg
    from codebase_rag.parsers.csharp_frontend import frontend as fe

    monkeypatch.setattr(cfg, "CSHARP_FRONTEND", cs.CSharpFrontend.AUTO)
    monkeypatch.setattr(fe, "csharp_frontend_available", lambda: True)
    fp_auto_with_dotnet = pf.compute_parser_fingerprint()
    monkeypatch.setattr(fe, "csharp_frontend_available", lambda: False)
    fp_auto_without_dotnet = pf.compute_parser_fingerprint()
    assert fp_auto_with_dotnet != fp_auto_without_dotnet

    monkeypatch.setattr(cfg, "CSHARP_FRONTEND", cs.CSharpFrontend.HYBRID)
    monkeypatch.setattr(fe, "csharp_frontend_available", lambda: True)
    assert pf.compute_parser_fingerprint() == fp_auto_with_dotnet
    # An EXPLICIT hybrid request that cannot run degrades the build to
    # tree-sitter (graph_updater warns and returns), so the fingerprint
    # must record what actually ran there too, not the setting string.
    monkeypatch.setattr(fe, "csharp_frontend_available", lambda: False)
    assert pf.compute_parser_fingerprint() == fp_auto_without_dotnet
    monkeypatch.setattr(cfg, "CSHARP_FRONTEND", cs.CSharpFrontend.TREESITTER)
    assert pf.compute_parser_fingerprint() == fp_auto_without_dotnet


def test_changes_when_a_compile_database_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Generating compile_commands.json after a tree-sitter-only index changes
    # the edges hybrid produces for unchanged sources, exactly like installing
    # libclang; the repo-aware fingerprint must read the old graph as stale
    # (issue #1177 review).
    from codebase_rag.config import settings as cfg
    from codebase_rag.parsers.cpp_frontend import frontend

    monkeypatch.setattr(cfg, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    monkeypatch.setattr(frontend, "cpp_frontend_available", lambda: True)
    repo = tmp_path / "repo"
    repo.mkdir()
    before = compute_parser_fingerprint(repo_path=repo)
    (repo / "compile_commands.json").write_text("[]", encoding="utf-8")
    assert compute_parser_fingerprint(repo_path=repo) != before


def test_compile_database_is_ignored_when_mode_resolves_to_treesitter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag.config import settings as cfg
    from codebase_rag.parsers.cpp_frontend import frontend

    monkeypatch.setattr(cfg, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    monkeypatch.setattr(frontend, "cpp_frontend_available", lambda: False)
    repo = tmp_path / "repo"
    repo.mkdir()
    before = compute_parser_fingerprint(repo_path=repo)
    (repo / "compile_commands.json").write_text("[]", encoding="utf-8")
    assert compute_parser_fingerprint(repo_path=repo) == before


def test_changes_when_compile_database_content_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Edited flags rebuild different facts for unchanged sources; presence
    # alone cannot see that, so the entry digests the selected database.
    from codebase_rag.config import settings as cfg
    from codebase_rag.parsers.cpp_frontend import frontend

    monkeypatch.setattr(cfg, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    monkeypatch.setattr(frontend, "cpp_frontend_available", lambda: True)
    repo = tmp_path / "repo"
    repo.mkdir()
    db = repo / "compile_commands.json"
    db.write_text('[{"arguments": ["c++", "-O0"]}]', encoding="utf-8")
    before = compute_parser_fingerprint(repo_path=repo)
    db.write_text('[{"arguments": ["c++", "-DFEATURE"]}]', encoding="utf-8")
    assert compute_parser_fingerprint(repo_path=repo) != before


class TestCaptureSelectionIsParserIdentity:
    # The capture selection decides which edges are produced for unchanged
    # sources -- the same criterion the frontend selection is hashed under --
    # so it belongs to the parser identity. Without it, enabling a capture
    # group on an indexed project reports "already in sync" and emits
    # nothing, and the only documented remedy wipes every project in the
    # shared graph (issue #1630).

    def test_changes_when_capture_env_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codebase_rag.config import settings as cfg

        monkeypatch.setattr(cfg, "CGR_CAPTURE", "")
        before = compute_parser_fingerprint()
        monkeypatch.setattr(cfg, "CGR_CAPTURE", "io")
        assert compute_parser_fingerprint() != before

    def test_changes_when_capture_passed_explicitly(self) -> None:
        # The CLI resolves CGR_CAPTURE and --capture together and hands the
        # GraphUpdater a CaptureSelection, so a fingerprint that consulted
        # only the environment would stay blind to `--capture io`.
        from codebase_rag.capture import resolve_capture

        before = compute_parser_fingerprint(capture=resolve_capture([]))
        after = compute_parser_fingerprint(capture=resolve_capture(["io"]))
        assert after != before

    def test_same_selection_same_fingerprint(self) -> None:
        from codebase_rag.capture import resolve_capture

        first = compute_parser_fingerprint(capture=resolve_capture(["io"]))
        second = compute_parser_fingerprint(capture=resolve_capture(["io"]))
        assert first == second

    def test_repeated_token_is_the_same_identity(self) -> None:
        # The RESOLVED selection is hashed, not the raw spec, so `io,io` and
        # `io` are one identity and do not force a spurious rebuild.
        from codebase_rag.capture import resolve_capture

        once = compute_parser_fingerprint(capture=resolve_capture(["io"]))
        twice = compute_parser_fingerprint(capture=resolve_capture(["io", "io"]))
        assert once == twice

    def test_changes_when_function_local_definitions_toggled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CAPTURE_FUNCTION_LOCAL_DEFINITIONS picks between two predicates for
        # whether a nested definition is ingested as a method, so flipping it
        # changes which Method nodes exist for unchanged sources -- the same
        # criterion as the frontend mode, and it was not hashed either.
        from codebase_rag.config import settings as cfg

        monkeypatch.setattr(cfg, "CAPTURE_FUNCTION_LOCAL_DEFINITIONS", True)
        before = compute_parser_fingerprint()
        monkeypatch.setattr(cfg, "CAPTURE_FUNCTION_LOCAL_DEFINITIONS", False)
        assert compute_parser_fingerprint() != before
