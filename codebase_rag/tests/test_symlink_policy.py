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
from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import MagicMock, call

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
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    force_mtime_after_cache,
    get_node_names,
    get_nodes,
)
from codebase_rag.tools.ast_grep_service import AstGrepService
from codebase_rag.utils import path_utils
from codebase_rag.utils.path_utils import (
    has_implementation_sibling,
    python_stub_has_implementation,
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
        for node_call in mock_ingestor.ensure_node_batch.call_args_list:
            properties = node_call.args[1]
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
            node_call.args[1].get(cs.KEY_PATH)
            for node_call in mock_ingestor.ensure_node_batch.call_args_list
            if node_call.args[0] in (cs.NodeLabel.PACKAGE, cs.NodeLabel.FOLDER)
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


def _file_paths(store: _StatefulIngestor) -> set[str]:
    return {
        str(props[cs.KEY_PATH])
        for (label, _uid), props in store.nodes.items()
        if label == cs.NodeLabel.FILE.value
    }


def _remove_indexed_file(
    tmp_path: Path, *, replace_with_link: bool
) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    """Index `core.py` and `old.py`, then delete `old.py` or replace it with
    a link to `core.py`."""
    repo = tmp_path / "repo"
    _write(repo / "core.py", "def helper():\n    return 1\n")
    _write(repo / "old.py", "def old_fn():\n    return 2\n")
    store = _StatefulIngestor()
    updater = _stateful_updater(repo, store)
    updater.run(force=True)
    assert _file_paths(store) == {"core.py", "old.py"}
    (repo / "old.py").unlink()
    if replace_with_link:
        _link(repo / "old.py", "core.py")
    return repo, store, updater


class TestIncrementalSync:
    def test_a_link_in_an_unchanged_directory_is_dropped_by_the_next_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No link at the root, whose mtime the cache write itself moves: only
        # the cached files' own check can see that `sub/linked.py` is a link.
        _write(tmp_path / "outside" / "x.py", "def outside_fn():\n    return 1\n")
        repo = tmp_path / "repo"
        _write(repo / "main.py", "def main():\n    return 0\n")
        _write(repo / "sub" / "own.py", "def own():\n    return 1\n")
        _link(repo / "sub" / "linked.py", "../../outside/x.py")
        store = _StatefulIngestor()
        with monkeypatch.context() as patched:
            for module in (path_utils, structure_processor):
                patched.setattr(
                    module, "is_symlink_entry", lambda _path: False, raising=False
                )
            _stateful_updater(repo, store).run(force=True)
        assert "repo.sub.linked.outside_fn" in _function_qns(store)

        _stateful_updater(repo, store).run()

        assert "repo.sub.linked.outside_fn" not in _function_qns(store)
        assert "repo.sub.own.own" in _function_qns(store)
        cache = json.loads((repo / cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8"))
        assert "sub/linked.py" not in cache

    def test_a_sync_after_a_file_became_a_link_keeps_the_targets_file(
        self, tmp_path: Path
    ) -> None:
        # The replaced file's own File node goes; resolving the link named
        # the target's, which the sync deleted instead (#2451 review).
        repo, store, _updater = _remove_indexed_file(tmp_path, replace_with_link=True)
        _stateful_updater(repo, store).run()
        assert _file_paths(store) == {"core.py"}

    def test_a_sync_after_a_file_was_deleted_keeps_the_others_file(
        self, tmp_path: Path
    ) -> None:
        # Negative: a plain deletion still drops exactly its own File node.
        repo, store, _updater = _remove_indexed_file(tmp_path, replace_with_link=False)
        _stateful_updater(repo, store).run()
        assert _file_paths(store) == {"core.py"}

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
        mock_updater.has_indexed.return_value = False
        handler.dispatch(FileCreatedEvent(str(watched / "linked.py")))
        handler.dispatch(FileCreatedEvent(str(watched / "alias.py")))
        mock_updater.reingest.assert_not_called()

    def test_an_indexed_file_replaced_by_a_link_is_removed_not_reingested(
        self, mock_updater: MagicMock, watched: Path
    ) -> None:
        # Both events arrive after the swap, so the path is a link by the
        # time each is handled; the graph still holds the file it replaced.
        handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
        mock_updater.has_indexed.return_value = True
        path = watched / "alias.py"
        handler.dispatch(FileDeletedEvent(str(path)))
        handler.dispatch(FileCreatedEvent(str(path)))
        assert mock_updater.reingest.call_args_list == [call((), deleted=(path,))] * 2

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


class TestWatcherReplacement:
    def test_replacing_an_indexed_file_with_a_link_drops_its_definitions(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _write(repo / "core.py", "def helper():\n    return 1\n")
        _write(repo / "old.py", "def old_fn():\n    return 2\n")
        store = _StatefulIngestor()
        updater = _stateful_updater(repo, store)
        updater.run(force=True)
        assert "repo.old.old_fn" in _function_qns(store)

        (repo / "old.py").unlink()
        _link(repo / "old.py", "core.py")
        handler = CodeChangeEventHandler(updater, debounce_seconds=0)
        handler.dispatch(FileDeletedEvent(str(repo / "old.py")))
        handler.dispatch(FileCreatedEvent(str(repo / "old.py")))

        assert _function_qns(store) == {"repo.core.helper"}
        cache = json.loads((repo / cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8"))
        assert sorted(cache) == ["core.py"]

    def test_reingesting_a_file_replaced_by_a_link_deletes_its_own_file(
        self, tmp_path: Path
    ) -> None:
        # The deletion keys on the replaced entry, as the File was indexed,
        # not on the link's target (#2451 review).
        repo, store, updater = _remove_indexed_file(tmp_path, replace_with_link=True)
        updater.reingest((), deleted=(repo / "old.py",))
        assert _file_paths(store) == {"core.py"}

    def test_reingesting_a_deleted_file_deletes_its_own_file(
        self, tmp_path: Path
    ) -> None:
        # Negative: with no link in the way the same File node goes.
        repo, store, updater = _remove_indexed_file(tmp_path, replace_with_link=False)
        updater.reingest((), deleted=(repo / "old.py",))
        assert _file_paths(store) == {"core.py"}


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


def _package_repo(root: Path, name: str) -> Path:
    repo = root / name
    _write(repo / "pkg" / "__init__.py", "")
    _write(repo / "pkg" / "m.py", "X = 1\n")
    return repo


def _sync(repo: Path, store: _StatefulIngestor, project: str, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
    ).run(force=force)


def _packages(store: _StatefulIngestor) -> set[str]:
    return {
        str(uid) for label, uid in store.nodes if label == cs.NodeLabel.PACKAGE.value
    }


class TestPackagePruneScope:
    # The orphan prune now deletes a Package by qualified name (the key its
    # upsert MERGEs on), scoped to the project and repository it read the
    # row from, so it never reaches another project's or checkout's package.

    def test_pruning_one_project_leaves_another_projects_package(
        self, tmp_path: Path
    ) -> None:
        store = _StatefulIngestor()
        alpha = _package_repo(tmp_path, "alpha")
        beta = _package_repo(tmp_path, "beta")
        _sync(alpha, store, "alpha", force=True)
        _sync(beta, store, "beta", force=True)
        assert {"alpha.pkg", "beta.pkg"} <= _packages(store)

        # `pkg` is a Folder now, so alpha's Package is a stale orphan.
        (alpha / "pkg" / "__init__.py").unlink()
        _sync(alpha, store, "alpha", force=False)

        assert "alpha.pkg" not in _packages(store)
        assert "beta.pkg" in _packages(store)

    def test_a_shared_project_name_leaves_the_other_checkouts_package(
        self, tmp_path: Path
    ) -> None:
        # Two checkouts given one project name share every package qn, so the
        # upsert puts both on one node; the one that wrote it last owns it.
        store = _StatefulIngestor()
        mine = _package_repo(tmp_path, "mine")
        other = _package_repo(tmp_path, "other")
        _sync(mine, store, "shared", force=True)
        _sync(other, store, "shared", force=True)
        node = store.nodes[(cs.NodeLabel.PACKAGE.value, "shared.pkg")]
        assert node[cs.KEY_ABSOLUTE_PATH] == (other / "pkg").resolve().as_posix()

        (mine / "pkg" / "__init__.py").unlink()
        _sync(mine, store, "shared", force=False)

        assert "shared.pkg" in _packages(store)

    def test_the_package_delete_itself_is_scoped(self, tmp_path: Path) -> None:
        # The delete query refuses on its own, not only through the rows the
        # prune reads: another project's name or another checkout's path is
        # left alone, and the owner's own delete goes through.
        store = _StatefulIngestor()
        other = _package_repo(tmp_path, "other")
        _sync(other, store, "shared", force=True)
        mine = _package_repo(tmp_path, "mine")

        def updater(repo: Path, project: str) -> GraphUpdater:
            parsers, queries = load_parsers()
            return GraphUpdater(
                ingestor=store,
                repo_path=repo,
                parsers=parsers,
                queries=queries,
                project_name=project,
            )

        mine_pkg = (mine / "pkg").resolve().as_posix()
        other_pkg = (other / "pkg").resolve().as_posix()
        updater(mine, "shared")._delete_package(store, "shared.pkg", mine_pkg)
        updater(other, "elsewhere")._delete_package(store, "shared.pkg", other_pkg)
        assert "shared.pkg" in _packages(store)

        updater(other, "shared")._delete_package(store, "shared.pkg", other_pkg)
        assert "shared.pkg" not in _packages(store)

    def test_a_package_behind_an_outside_link_is_pruned(self, tmp_path: Path) -> None:
        # Its absolute path is the link's target, outside the repository, so
        # the prune passed over it before checking whether anything derives it.
        repo = _package_repo(tmp_path, "repo")
        target = tmp_path / "outside" / "lib"
        _write(target / "__init__.py", "")
        _link(repo / "pkg" / "linkdir", "../../outside/lib", directory=True)
        store = _StatefulIngestor()
        _sync(repo, store, "proj", force=True)
        package = cs.NodeLabel.PACKAGE.value
        store.ensure_node_batch(
            package,
            {
                cs.KEY_QUALIFIED_NAME: "proj.pkg.linkdir",
                cs.KEY_NAME: "linkdir",
                cs.KEY_PATH: "pkg/linkdir",
                cs.KEY_ABSOLUTE_PATH: target.resolve().as_posix(),
            },
        )
        store.ensure_relationship_batch(
            (package, cs.KEY_QUALIFIED_NAME, "proj.pkg"),
            cs.RelationshipType.CONTAINS_PACKAGE,
            (package, cs.KEY_QUALIFIED_NAME, "proj.pkg.linkdir"),
        )
        # Negative: an outside path with no link of this repository behind
        # it (the package another checkout under this name wrote) stays.
        store.ensure_node_batch(
            package,
            {
                cs.KEY_QUALIFIED_NAME: "proj.vendor",
                cs.KEY_NAME: "vendor",
                cs.KEY_PATH: "vendor",
                cs.KEY_ABSOLUTE_PATH: (tmp_path / "elsewhere" / "vendor").as_posix(),
            },
        )
        edited = _write(repo / "pkg" / "m.py", "X = 2\n")
        force_mtime_after_cache(repo, edited)

        _sync(repo, store, "proj", force=False)

        packages = _packages(store)
        assert "proj.pkg.linkdir" not in packages
        assert {"proj.pkg", "proj.vendor"} <= packages

    def test_a_link_to_the_other_checkouts_package_leaves_it(
        self, tmp_path: Path
    ) -> None:
        # `mine/pkg` links to `other/pkg`, which the other checkout under the
        # same name indexed: the node at `pkg` holds `other/pkg`, the shape
        # that checkout writes for its own directory, so it is not this
        # repository's link-derived leftover (#2451 review).
        store = _StatefulIngestor()
        other = _package_repo(tmp_path, "other")
        mine = tmp_path / "mine"
        main = _write(mine / "main.py", "X = 1\n")
        _link(mine / "pkg", "../other/pkg", directory=True)
        _sync(mine, store, "shared", force=True)
        _sync(other, store, "shared", force=True)
        node = store.nodes[(cs.NodeLabel.PACKAGE.value, "shared.pkg")]
        assert node[cs.KEY_ABSOLUTE_PATH] == (other / "pkg").resolve().as_posix()
        _write(mine / "main.py", "X = 2\n")
        force_mtime_after_cache(mine, main)

        _sync(mine, store, "shared", force=False)

        assert "shared.pkg" in _packages(store)

    def test_a_link_derived_package_beside_the_other_checkouts_is_pruned(
        self, tmp_path: Path
    ) -> None:
        # Negative: a node an older build derived through this repository's
        # link to the same target still goes, and the other checkout's stays.
        store = _StatefulIngestor()
        other = _package_repo(tmp_path, "other")
        mine = tmp_path / "mine"
        main = _write(mine / "main.py", "X = 1\n")
        _link(mine / "vendored", "../other/pkg", directory=True)
        _sync(mine, store, "shared", force=True)
        _sync(other, store, "shared", force=True)
        store.ensure_node_batch(
            cs.NodeLabel.PACKAGE.value,
            {
                cs.KEY_QUALIFIED_NAME: "shared.vendored",
                cs.KEY_NAME: "vendored",
                cs.KEY_PATH: "vendored",
                cs.KEY_ABSOLUTE_PATH: (other / "pkg").resolve().as_posix(),
            },
        )
        _write(mine / "main.py", "X = 2\n")
        force_mtime_after_cache(mine, main)

        _sync(mine, store, "shared", force=False)

        packages = _packages(store)
        assert "shared.vendored" not in packages
        assert "shared.pkg" in packages


_STUB = "def f() -> int: ...\n"
_IMPL = "def f():\n    return 1\n"


def _implementation(root: Path, rel: str, *, inside: bool) -> str:
    """Write the real implementation; return its path relative to `proj/`.

    Inside the repository it is walked under its own path, and only the
    symlink rule tells the stub not to yield to the link. Outside it,
    `should_skip_path`'s containment check already refused the link.
    """
    if inside:
        _write(root / "proj" / "lib" / rel, _IMPL)
        return f"lib/{rel}"
    _write(root / "vendor" / rel, _IMPL)
    return f"../vendor/{rel}"


def _linked_file(root: Path, *, inside: bool) -> Path:
    target = _implementation(root, "x.py", inside=inside)
    stub = _write(root / "proj" / "x.pyi", _STUB)
    _link(root / "proj" / "x.py", target)
    return stub


def _linked_package_dir(root: Path, *, inside: bool) -> Path:
    target = _implementation(root, "x/__init__.py", inside=inside)
    stub = _write(root / "proj" / "x.pyi", _STUB)
    _link(root / "proj" / "x", Path(target).parent.as_posix(), directory=True)
    return stub


def _linked_package_init(root: Path, *, inside: bool) -> Path:
    # A real `x/` the walk enters, whose `__init__.py` is the link.
    target = _implementation(root, "x.py", inside=inside)
    stub = _write(root / "proj" / "x.pyi", _STUB)
    (root / "proj" / "x").mkdir()
    _link(root / "proj" / "x" / "__init__.py", f"../{target}")
    return stub


def _linked_init_beside_init_stub(root: Path, *, inside: bool) -> Path:
    target = _implementation(root, "x.py", inside=inside)
    stub = _write(root / "proj" / "pkg" / "__init__.pyi", _STUB)
    _link(root / "proj" / "pkg" / "__init__.py", f"../{target}")
    return stub


# (layout builder, the module qn the stub must define)
_LINKED_IMPLEMENTATIONS = [
    pytest.param(_linked_file, "proj.x", id="x.py-is-a-link"),
    pytest.param(_linked_package_dir, "proj.x", id="x-dir-is-a-link"),
    pytest.param(_linked_package_init, "proj.x", id="x/__init__.py-is-a-link"),
    pytest.param(_linked_init_beside_init_stub, "proj.pkg", id="__init__.py-link"),
]
_TARGET_INSIDE = pytest.mark.parametrize(
    "inside", [True, False], ids=["target-inside", "target-outside"]
)


def _module_paths(ingestor: MagicMock) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for node_call in get_nodes(ingestor, cs.NodeLabel.MODULE.value):
        props = node_call[0][1]
        path = props.get(cs.KEY_PATH)
        entry = found.setdefault(props[cs.KEY_QUALIFIED_NAME], set())
        if isinstance(path, str):
            entry.add(path)
    return found


class TestStubBesideALinkedImplementation:
    # A `.pyi` stub yields its module to the `.py` (or package) beside it
    # (issue #2445) only when the walk will index that implementation. A
    # link, or a package behind a linked directory, is never walked, so a
    # stub yielding to one left NOTHING defining the module.

    @_TARGET_INSIDE
    @pytest.mark.parametrize(("build", "module"), _LINKED_IMPLEMENTATIONS)
    def test_the_stub_defines_the_module(
        self,
        tmp_path: Path,
        mock_ingestor: MagicMock,
        build: Callable[..., Path],
        module: str,
        inside: bool,
    ) -> None:
        stub = build(tmp_path, inside=inside)
        repo = tmp_path / "proj"
        create_and_run_updater(repo, mock_ingestor)
        assert _module_paths(mock_ingestor).get(module) == {
            stub.relative_to(repo).as_posix()
        }
        assert f"{module}.f" in get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)

    @_TARGET_INSIDE
    @pytest.mark.parametrize(("build", "module"), _LINKED_IMPLEMENTATIONS)
    def test_a_linked_implementation_does_not_count(
        self, tmp_path: Path, build: Callable[..., Path], module: str, inside: bool
    ) -> None:
        stub = build(tmp_path, inside=inside)
        assert not python_stub_has_implementation(stub, tmp_path / "proj")

    def test_a_real_implementation_still_counts(self, tmp_path: Path) -> None:
        # Negative: the #2445 yield is unchanged for a file that is walked.
        stub = _write(tmp_path / "x.pyi", _STUB)
        _write(tmp_path / "x.py", _IMPL)
        assert python_stub_has_implementation(stub, tmp_path)

    def test_a_real_package_still_counts(self, tmp_path: Path) -> None:
        # Negative: a package directory that is not a link still wins.
        stub = _write(tmp_path / "x.pyi", _STUB)
        _write(tmp_path / "x" / "__init__.py", _IMPL)
        assert python_stub_has_implementation(stub, tmp_path)

    def test_a_real_init_still_counts(self, tmp_path: Path) -> None:
        # Negative.
        stub = _write(tmp_path / "pkg" / "__init__.pyi", _STUB)
        _write(tmp_path / "pkg" / "__init__.py", _IMPL)
        assert python_stub_has_implementation(stub, tmp_path)

    def test_a_stub_with_no_implementation_does_not_yield(self, tmp_path: Path) -> None:
        # Negative.
        stub = _write(tmp_path / "x.pyi", _STUB)
        assert not python_stub_has_implementation(stub, tmp_path)

    def test_a_repository_reached_through_a_link_keeps_its_implementation(
        self, tmp_path: Path
    ) -> None:
        # Negative: only entries inside the repository are judged, so a
        # `--repo-path` that is itself a link still has a walked `x.py`.
        _write(tmp_path / "repo" / "x.py", _IMPL)
        _write(tmp_path / "repo" / "x.pyi", _STUB)
        entry = tmp_path / "repo-link"
        _link(entry, "repo", directory=True)
        assert python_stub_has_implementation(entry / "x.pyi", entry)

    def test_a_real_implementation_keeps_the_module_in_the_graph(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative, end to end: the `.py` owns the module, the stub adds none.
        repo = tmp_path / "proj"
        _write(repo / "x.py", _IMPL)
        _write(repo / "x.pyi", _STUB + "def only_in_stub() -> None: ...\n")
        create_and_run_updater(repo, mock_ingestor)
        assert _module_paths(mock_ingestor).get("proj.x") == {"x.py"}
        functions = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        assert "proj.x.f" in functions
        assert "proj.x.only_in_stub" not in functions


def _stub_whose_implementation_becomes_a_link(
    tmp_path: Path,
) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    """Index a real `x.py` beside `x.pyi`, then replace it with a link."""
    repo = tmp_path / "proj"
    _write(repo / "lib" / "x.py", _IMPL)
    _write(repo / "x.py", "def g():\n    return 2\n")
    _write(repo / "x.pyi", _STUB)
    store = _StatefulIngestor()
    updater = _stateful_updater(repo, store)
    updater.run(force=True)
    assert _stored_module_path(store, "proj.x") == "x.py"
    (repo / "x.py").unlink()
    _link(repo / "x.py", "lib/x.py")
    return repo, store, updater


def _stored_module_path(store: _StatefulIngestor, qn: str) -> object:
    return store.nodes.get((cs.NodeLabel.MODULE.value, qn), {}).get(cs.KEY_PATH)


class TestImplementationBecomesALink:
    # The stub's answer flips when its `x.py` turns into a link, and the
    # update must hand `proj.x` to the stub rather than leave it unowned.

    def test_the_next_sync_hands_the_module_to_the_stub(self, tmp_path: Path) -> None:
        repo, store, _updater = _stub_whose_implementation_becomes_a_link(tmp_path)
        _stateful_updater(repo, store).run()
        assert _stored_module_path(store, "proj.x") == "x.pyi"
        assert _function_qns(store) == {"proj.x.f", "proj.lib.x.f"}

    def test_a_reingest_hands_the_module_to_the_stub(self, tmp_path: Path) -> None:
        repo, store, updater = _stub_whose_implementation_becomes_a_link(tmp_path)
        updater.reingest((), deleted=(repo / "x.py",))
        assert _stored_module_path(store, "proj.x") == "x.pyi"
        assert _function_qns(store) == {"proj.x.f", "proj.lib.x.f"}


class TestReingestSkipsLinks:
    # A scoped re-ingest re-parses files the call did not name: same-stem
    # survivors (issue #1569) and the files under a directory whose
    # package-ness flipped (issue #1798). Both listed the directory and kept
    # whatever `is_file()` accepted, which follows a link, so a re-ingest
    # indexed links the walk leaves out.

    def test_a_package_flip_reparses_no_link(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "pkg" / "core.py", "def helper():\n    return 1\n")
        _link(repo / "pkg" / "alias.py", "core.py")
        store = _StatefulIngestor()
        updater = _stateful_updater(repo, store)
        updater.run(force=True)
        assert _function_qns(store) == {"repo.pkg.core.helper"}

        updater.reingest((_write(repo / "pkg" / "__init__.py", ""),))

        assert _function_qns(store) == {"repo.pkg.core.helper"}
        assert "pkg/alias.py" not in _file_paths(store)
        cache = json.loads((repo / cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8"))
        assert "pkg/alias.py" not in cache

    def test_a_same_stem_survivor_is_no_link(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "web" / "m.js", "export function shown() {\n  return 1;\n}\n")
        (repo / "pkg").mkdir()
        _link(repo / "pkg" / "m.js", "../web/m.js")
        store = _StatefulIngestor()
        updater = _stateful_updater(repo, store)
        updater.run(force=True)
        before = _function_qns(store)
        assert not any(qn.startswith("repo.pkg.") for qn in before)

        updater.reingest((_write(repo / "pkg" / "m.py", "def own():\n    return 1\n"),))

        assert _function_qns(store) == before | {"repo.pkg.m.own"}
        assert "pkg/m.js" not in _file_paths(store)

    def test_a_package_flip_still_reparses_a_real_sibling(self, tmp_path: Path) -> None:
        # Negative: the real files under a flipped directory are re-parsed
        # onto the new Package, as before.
        repo = tmp_path / "repo"
        _write(repo / "pkg" / "core.py", "def helper():\n    return 1\n")
        store = _StatefulIngestor()
        updater = _stateful_updater(repo, store)
        updater.run(force=True)

        updater.reingest((_write(repo / "pkg" / "__init__.py", ""),))

        assert "repo.pkg" in _packages(store)
        assert _function_qns(store) == {"repo.pkg.core.helper"}
        contains = {
            (edge[1], edge[4])
            for edge in store.keyed_edges
            if edge[2] == cs.RelationshipType.CONTAINS_MODULE.value
        }
        assert ("repo.pkg", "repo.pkg.core") in contains
