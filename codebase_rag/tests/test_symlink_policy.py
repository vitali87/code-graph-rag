"""Issue #2451: every repository walk treats a symlink the same way.

Indexing followed a FILE link wherever it pointed, so `pkg/linked.py ->
../../outside/private.py` and `NOTES.md -> ../outside/notes.txt` put content
from outside `--repo-path` into the shared graph, while a DIRECTORY link was
dropped without a word. An in-repo file link (`pkg/alias.py -> core.py`) was
indexed as a second module duplicating every definition of its target, and an
in-repo directory link left an empty Package whose absolute path was its
target's.

The policy now: a link is never followed, file or directory, wherever it
points. A target outside the repository is never read; a target inside it is
indexed once, under its own path, when the walk reaches it. Each skipped link
is logged at DEBUG and the run logs one INFO count.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger
from watchdog.events import FileCreatedEvent, FileDeletedEvent

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_spec import has_other_language_sibling
from codebase_rag.main import detect_excludable_directories
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import structure_processor
from codebase_rag.parsers.contracts import discover_contract_operations
from codebase_rag.tests.conftest import create_and_run_updater, get_node_names
from codebase_rag.tools.ast_grep_service import AstGrepService
from codebase_rag.utils import path_utils
from codebase_rag.utils.path_utils import (
    has_implementation_sibling,
    walk_eligible_files,
)
from evals.cgr_graph import _StatefulIngestor
from realtime_updater import CodeChangeEventHandler

SECRET = "sk-live-123"
# Every link in the `layout` fixture, by its path in the repository.
LINKS = ["NOTES.md", "app/vendored", "pkg/alias.py", "pkg/linkdir", "pkg/linked.py"]


def _link(link: Path, target: str, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:  # Windows without symlink privileges.
        pytest.skip("symlinks need privileges on this host")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def layout(tmp_path: Path) -> Path:
    """The issue's reproduction, plus an in-repo directory link."""
    outside = tmp_path / "outside"
    _write(outside / "private.py", "def outside_fn():\n    return 1\n")
    _write(outside / "notes.txt", f"# Private notes\n\nAPI key: {SECRET}\n")
    _write(outside / "lib" / "mod.py", "def lib_fn():\n    return 1\n")
    repo = tmp_path / "repo"
    _write(repo / "pkg" / "__init__.py", "")
    _write(
        repo / "pkg" / "core.py",
        "def helper():\n    a = 1\n    b = 2\n    return a + b\n",
    )
    _link(repo / "pkg" / "alias.py", "core.py")
    _link(repo / "pkg" / "linked.py", "../../outside/private.py")
    _link(repo / "NOTES.md", "../outside/notes.txt")
    _link(repo / "pkg" / "linkdir", "../../outside/lib", directory=True)
    (repo / "app").mkdir()
    _link(repo / "app" / "vendored", "../pkg", directory=True)
    return repo


def _walked(repo: Path, **kwargs: frozenset[str] | None) -> list[str]:
    return [rel for _dir, _name, rel in walk_eligible_files(repo, **kwargs)]


@pytest.fixture
def records() -> Generator[list[tuple[str, str]], None, None]:
    seen: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: seen.append(
            (message.record["level"].name, message.record["message"])
        ),
        level="DEBUG",
    )
    yield seen
    logger.remove(handler_id)


class TestWalk:
    def test_a_file_link_out_of_the_repository_is_not_walked(
        self, layout: Path
    ) -> None:
        walked = _walked(layout)
        assert "pkg/linked.py" not in walked
        assert "NOTES.md" not in walked

    def test_an_in_repo_file_link_is_not_walked_beside_its_target(
        self, layout: Path
    ) -> None:
        walked = _walked(layout)
        assert "pkg/alias.py" not in walked
        assert "pkg/core.py" in walked

    def test_every_skipped_link_is_reported_file_or_directory(
        self, layout: Path
    ) -> None:
        reported: list[str] = []
        list(walk_eligible_files(layout, on_symlink=reported.append))
        assert sorted(reported) == LINKS

    def test_a_link_an_ignore_rule_drops_is_not_reported(self, layout: Path) -> None:
        # Negative: the ignore rules decide first, so the count names only
        # links that would otherwise have been indexed.
        (layout / "node_modules").mkdir()
        _link(layout / "node_modules" / "dep.py", "../pkg/core.py")
        reported: list[str] = []
        walked = [
            rel
            for _d, _n, rel in walk_eligible_files(
                layout,
                exclude_paths=frozenset({"NOTES.md"}),
                on_symlink=reported.append,
            )
        ]
        assert "NOTES.md" not in reported
        assert not any(rel.startswith("node_modules/") for rel in reported)
        assert walked == ["pkg/__init__.py", "pkg/core.py"]

    def test_a_repository_reached_through_a_link_is_walked_in_full(
        self, layout: Path
    ) -> None:
        # Negative: only the entries inside the repository are judged, so a
        # `--repo-path` that is itself a link (or under one) loses nothing.
        entry = layout.parent / "repo-link"
        _link(entry, "repo", directory=True)
        assert _walked(entry) == _walked(layout)
        assert {"pkg/__init__.py", "pkg/core.py"} <= set(_walked(entry))


class TestIndex:
    def test_nothing_from_outside_the_repository_reaches_the_graph(
        self, layout: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(layout, mock_ingestor)
        root = layout.resolve()
        for call in mock_ingestor.ensure_node_batch.call_args_list:
            properties = call.args[1]
            absolute = properties.get(cs.KEY_ABSOLUTE_PATH)
            if isinstance(absolute, str):
                assert Path(absolute).is_relative_to(root), properties
            assert SECRET not in json.dumps(properties, default=str)
        modules = get_node_names(mock_ingestor, cs.NodeLabel.MODULE)
        assert "repo.pkg.linked" not in modules
        assert "repo.NOTES_md" not in modules
        assert not get_node_names(mock_ingestor, cs.NodeLabel.SECTION)

    def test_an_in_repo_link_does_not_duplicate_its_target(
        self, layout: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(layout, mock_ingestor)
        functions = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        assert functions == {"repo.pkg.core.helper"}

    def test_a_directory_link_in_the_repository_gets_no_container(
        self, layout: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(layout, mock_ingestor)
        containers = {
            call.args[1].get(cs.KEY_PATH)
            for call in mock_ingestor.ensure_node_batch.call_args_list
            if call.args[0] in (cs.NodeLabel.PACKAGE, cs.NodeLabel.FOLDER)
        }
        assert "app/vendored" not in containers
        assert "pkg/linkdir" not in containers
        assert "pkg" in containers

    def test_the_hash_cache_holds_no_link(
        self, layout: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(layout, mock_ingestor)
        cache = json.loads(
            (layout / cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
        )
        assert sorted(cache) == ["pkg/__init__.py", "pkg/core.py"]

    def test_each_skipped_link_is_logged_with_one_summary(
        self,
        layout: Path,
        mock_ingestor: MagicMock,
        records: list[tuple[str, str]],
    ) -> None:
        create_and_run_updater(layout, mock_ingestor)
        debug = [text for level, text in records if level == "DEBUG"]
        for link in LINKS:
            assert any(link in text and "symlink" in text for text in debug), link
        outside = (layout.parent / "outside").resolve()
        assert any(
            "pkg/linked.py" in text and str(outside / "private.py") in text
            for text in debug
        )
        summaries = [
            text
            for level, text in records
            if level == "INFO" and "symlink" in text.lower()
        ]
        assert len(summaries) == 1
        assert str(len(LINKS)) in summaries[0]

    def test_a_link_named_as_the_single_file_target_indexes_its_target(
        self, layout: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: an explicit file target is resolved as before, so it is
        # indexed under the target's own path, never as a second module.
        (layout / ".git").mkdir()
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=layout / "pkg" / "alias.py",
            parsers=parsers,
            queries=queries,
        )
        updater.run()
        functions = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        assert functions == {"repo.pkg.core.helper"}


def _stateful_updater(root: Path, store: _StatefulIngestor) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store, repo_path=root, parsers=parsers, queries=queries
    )


def _function_qns(store: _StatefulIngestor) -> set[str]:
    return {
        str(uid) for label, uid in store.nodes if label == cs.NodeLabel.FUNCTION.value
    }


class TestIncrementalSync:
    def test_a_repository_with_links_stays_in_sync(self, layout: Path) -> None:
        # Negative: the in-sync listing skips the links the walk skips, or
        # every later sync would see them as new files and re-run.
        store = _StatefulIngestor()
        _stateful_updater(layout, store).run(force=True)
        assert _stateful_updater(layout, store)._is_already_in_sync() is True

    def test_an_older_index_loses_its_link_nodes_on_the_next_sync(
        self, layout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A graph built before the fix holds the links' modules; the next
        # ordinary sync removes them, leaving the target's.
        store = _StatefulIngestor()
        with monkeypatch.context() as patched:
            for module in (path_utils, structure_processor):
                patched.setattr(
                    module, "is_symlink_entry", lambda _path: False, raising=False
                )
            _stateful_updater(layout, store).run(force=True)
        assert "repo.pkg.alias.helper" in _function_qns(store)
        assert (cs.NodeLabel.PACKAGE.value, "repo.app.vendored") in store.nodes

        upgraded = _stateful_updater(layout, store)
        assert upgraded._is_already_in_sync() is False
        upgraded.run()

        assert _function_qns(store) == {"repo.pkg.core.helper"}
        packages = {
            uid for label, uid in store.nodes if label == cs.NodeLabel.PACKAGE.value
        }
        # The link's Package shared its target's absolute path, so removing
        # it by that path took the real Package with it.
        assert packages == {"repo.pkg"}


@pytest.fixture
def watched(temp_repo: Path) -> Generator[Path, None, None]:
    outside = temp_repo.parent / f"{temp_repo.name}-outside.py"
    _write(outside, "def outside_fn():\n    return 1\n")
    _write(temp_repo / "core.py", "def helper():\n    return 1\n")
    _link(temp_repo / "alias.py", "core.py")
    _link(temp_repo / "linked.py", str(outside))
    yield temp_repo
    outside.unlink(missing_ok=True)


class TestWatcher:
    def test_the_watcher_ignores_a_link(
        self, mock_updater: MagicMock, watched: Path
    ) -> None:
        handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
        assert not handler._is_relevant(str(watched / "alias.py"))
        assert not handler._is_relevant(str(watched / "linked.py"))
        assert handler._is_relevant(str(watched / "core.py"))

    def test_a_created_link_is_not_reingested(
        self, mock_updater: MagicMock, watched: Path
    ) -> None:
        handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
        handler.dispatch(FileCreatedEvent(str(watched / "linked.py")))
        handler.dispatch(FileCreatedEvent(str(watched / "alias.py")))
        mock_updater.reingest.assert_not_called()

    def test_the_watcher_and_the_walk_agree_on_links(
        self, mock_updater: MagicMock, watched: Path
    ) -> None:
        handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
        names = ["alias.py", "core.py", "linked.py"]
        walked = set(_walked(watched))
        assert {n for n in names if handler._is_relevant(str(watched / n))} == walked

    def test_a_deleted_link_still_reaches_the_graph(
        self, mock_updater: MagicMock, watched: Path
    ) -> None:
        # Negative: once the link is gone there is nothing to follow, and a
        # graph built before the fix may still hold its module.
        handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
        (watched / "alias.py").unlink()
        handler.dispatch(FileDeletedEvent(str(watched / "alias.py")))
        mock_updater.reingest.assert_called_once_with(
            (), deleted=(watched / "alias.py",)
        )


class TestSiblingPredicates:
    def test_a_linked_implementation_does_not_take_the_declarations_name(
        self, tmp_path: Path
    ) -> None:
        # The walk no longer indexes `foo.ts`, so `foo.d.ts` must not yield
        # the shared module name to it, or nothing would own that name.
        _write(tmp_path / "src" / "foo.ts", "export const a = 1;\n")
        declaration = _write(tmp_path / "types" / "foo.d.ts", "export const a: 1;\n")
        _link(tmp_path / "types" / "foo.ts", "../src/foo.ts")
        assert not has_implementation_sibling(declaration, tmp_path)

    def test_a_real_implementation_still_takes_the_declarations_name(
        self, tmp_path: Path
    ) -> None:
        # Negative.
        declaration = _write(tmp_path / "foo.d.ts", "export const a: 1;\n")
        _write(tmp_path / "foo.ts", "export const a = 1;\n")
        assert has_implementation_sibling(declaration, tmp_path)

    def test_a_linked_file_of_another_language_is_no_sibling(
        self, tmp_path: Path
    ) -> None:
        module = _write(tmp_path / "pkg" / "m.py", "X = 1\n")
        _write(tmp_path / "web" / "m.js", "export const x = 1;\n")
        _link(tmp_path / "pkg" / "m.js", "../web/m.js")
        assert not has_other_language_sibling(module, tmp_path)


_OPENAPI = {
    "openapi": "3.0.0",
    "paths": {"/things": {"get": {"operationId": "listThings"}}},
}


class TestOtherWalks:
    def test_a_linked_contract_is_read_once(self, tmp_path: Path) -> None:
        _write(tmp_path / "api" / "things.json", json.dumps(_OPENAPI))
        _link(tmp_path / "api" / "alias.json", "things.json")
        operations = discover_contract_operations(tmp_path)
        assert [op.contract for op in operations] == ["api/things"]

    def test_structural_search_reports_a_linked_file_once(self, tmp_path: Path) -> None:
        _write(tmp_path / "core.py", "API_TOKEN = 1\n")
        _link(tmp_path / "alias.py", "core.py")
        matches = AstGrepService(project_root=str(tmp_path)).search(
            pattern="API_TOKEN", language="python"
        )
        assert [match["file"] for match in matches] == ["core.py"]

    def test_interactive_setup_offers_no_directory_behind_a_link(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "shared" / "node_modules" / "dep" / "index.js", "x\n")
        repo = tmp_path / "repo"
        _write(repo / "node_modules" / "own" / "index.js", "x\n")
        _link(repo / "linked", "../shared", directory=True)
        # Negative half: a real directory is still offered.
        assert detect_excludable_directories(repo) == {"node_modules"}
