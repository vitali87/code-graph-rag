"""Issue #2412: a project name never contains the qualified-name separator.

Qualified names are `<project>.<package path>.<module>.<symbol>`, and nodes
MERGE on `qualified_name`. A project named `acme.web` and a project `acme`
with a `web/` package therefore produced the same `acme.web.views` Module:
one node owned by two repositories, carrying whichever path was written last
and DEFINING functions from both. The default name was sanitised; an explicit
`--project-name` was stored verbatim.
"""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import logs as ls
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import indexed_scope
from codebase_rag.types_defs import PropertyDict, ResultRow
from codebase_rag.utils.path_utils import derive_project_name, project_name_error
from codebase_rag.workspaces import (
    WorkspaceError,
    add_repo,
    create_workspace,
    load_workspace,
)
from codebase_rag.workspaces.models import WorkspaceRepo
from evals import cgr_graph
from evals.cgr_graph import _StatefulIngestor

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "CGR_HOME", tmp_path / "cgr-home")
    return tmp_path / "cgr-home"


@pytest.fixture
def sync() -> Generator[MagicMock, None, None]:
    with (
        patch("codebase_rag.cli.connect_memgraph") as connect,
        patch("codebase_rag.cli._update_and_validate_models"),
        patch("codebase_rag.cli.main_single_query"),
        patch("codebase_rag.cli._run_graph_sync") as run_graph_sync,
    ):
        connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
        connect.return_value.__exit__ = MagicMock(return_value=False)
        yield run_graph_sync


def _start(repo: Path, *extra: str) -> list[str]:
    return ["start", "--repo-path", str(repo), "--no-start-stack", *extra]


def test_start_refuses_a_dotted_project_name(sync: MagicMock, tmp_path: Path) -> None:
    result = runner.invoke(
        app, _start(tmp_path, "--update-graph", "--project-name", "acme.web")
    )

    assert result.exit_code == 2, result.output
    assert "acme_web" in result.output
    sync.assert_not_called()


def test_the_refusal_also_covers_the_pre_chat_sync(
    sync: MagicMock, tmp_path: Path
) -> None:
    # `cgr start` without --update-graph still syncs before the chat.
    result = runner.invoke(
        app, _start(tmp_path, "--project-name", "acme.web", "--ask-agent", "hi")
    )

    assert result.exit_code == 2, result.output
    sync.assert_not_called()


@pytest.mark.parametrize("name", ["acme", "acme_web", "acme-web", "Acme2"])
def test_a_name_without_the_separator_is_stored_as_given(
    sync: MagicMock, tmp_path: Path, name: str
) -> None:
    # Negative: only the separator is refused, nothing is rewritten.
    result = runner.invoke(
        app, _start(tmp_path, "--update-graph", "--project-name", name)
    )

    assert result.exit_code == 0, result.output
    assert sync.call_args.kwargs["project_name"] == name


def test_the_derived_default_is_unchanged(sync: MagicMock, tmp_path: Path) -> None:
    # Negative: a dotted directory already derives a separator-free name.
    repo = tmp_path / "acme.web"
    repo.mkdir()

    result = runner.invoke(app, _start(repo, "--update-graph"))

    assert result.exit_code == 0, result.output
    assert sync.call_args.kwargs["project_name"] == derive_project_name(repo)


def test_workspace_add_repo_refuses_a_dotted_project_name(tmp_path: Path) -> None:
    create_workspace("mono")

    with pytest.raises(WorkspaceError, match="acme_web"):
        add_repo("mono", str(tmp_path), project_name="acme.web")

    assert load_workspace("mono").repos == []


def test_workspace_add_repo_cli_exits_with_the_reason(tmp_path: Path) -> None:
    runner.invoke(app, ["workspace", "create", "mono"])

    result = runner.invoke(
        app, ["workspace", "add-repo", "mono", str(tmp_path), "-p", "acme.web"]
    )

    assert result.exit_code == 1
    assert "acme_web" in result.output


def test_workspace_add_repo_keeps_a_plain_name(tmp_path: Path) -> None:
    # Negative.
    create_workspace("mono")

    _, repo = add_repo("mono", str(tmp_path), project_name="acme_web")

    assert repo.project_name == "acme_web"


def test_a_workspace_saved_with_a_dotted_name_is_not_synced(
    sync: MagicMock, tmp_path: Path
) -> None:
    # A workspace file written before this fix can still hold a dotted name;
    # syncing it would merge nodes, so nothing in the workspace is written.
    plain, dotted = tmp_path / "plain", tmp_path / "frontend"
    plain.mkdir()
    dotted.mkdir()
    create_workspace(
        "mono",
        repos=[
            WorkspaceRepo(path=str(plain), project_name="acme"),
            WorkspaceRepo(path=str(dotted), project_name="acme.web"),
        ],
    )

    result = runner.invoke(
        app, _start(plain, "--workspace", "mono", "--ask-agent", "hi")
    )

    assert result.exit_code == 1, result.output
    assert "acme.web" in result.output
    assert "acme_web" in result.output
    sync.assert_not_called()


def test_a_workspace_of_plain_names_still_syncs(
    sync: MagicMock, tmp_path: Path
) -> None:
    # Negative.
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    create_workspace("mono")
    add_repo("mono", str(first), project_name="acme")
    add_repo("mono", str(second), project_name="acme_web")

    result = runner.invoke(
        app, _start(first, "--workspace", "mono", "--ask-agent", "hi")
    )

    assert result.exit_code == 0, result.output
    assert sync.call_count == 2


@pytest.mark.parametrize(
    ("name", "suggestion"),
    [("acme.web", "acme_web"), ("a..b.", "a_b"), (".hidden", "hidden")],
)
def test_the_refusal_suggests_a_usable_name(name: str, suggestion: str) -> None:
    error = project_name_error(name)

    assert error is not None
    assert f"'{suggestion}'" in error


@pytest.mark.parametrize("name", ["acme", "acme_web", "my-repo__4bd9a922"])
def test_names_without_the_separator_have_no_error(name: str) -> None:
    # Negative.
    assert project_name_error(name) is None


def _index(root: Path, store: _StatefulIngestor, name: str | None) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=name,
    ).run(force=True)


def _defined_by(store: _StatefulIngestor, module_qn: str) -> set[str]:
    return {
        str(edge[4])
        for edge in store.keyed_edges
        if edge[2] == "DEFINES" and edge[1] == module_qn
    }


def test_a_dotted_directory_does_not_merge_with_another_projects_package(
    tmp_path: Path,
) -> None:
    # `GraphUpdater`'s own default (used by `cgr index`) took the directory
    # name verbatim: a checkout at `acme.web/` wrote the nodes of project
    # `acme`'s package `web`.
    frontend = tmp_path / "acme.web"
    frontend.mkdir()
    (frontend / "views.py").write_text("def render_page():\n    return 1\n")
    monorepo = tmp_path / "monorepo"
    (monorepo / "web").mkdir(parents=True)
    (monorepo / "web" / "__init__.py").write_text("")
    (monorepo / "web" / "views.py").write_text("def start_server():\n    return 2\n")
    store = _StatefulIngestor()

    _index(frontend, store, None)
    _index(monorepo, store, "acme")

    frontend_name = derive_project_name(frontend)
    assert _defined_by(store, "acme.web.views") == {"acme.web.views.start_server"}
    assert _defined_by(store, f"{frontend_name}.views") == {
        f"{frontend_name}.views.render_page"
    }


def test_dotted_and_underscored_checkouts_keep_distinct_default_names(
    tmp_path: Path,
) -> None:
    # Review of PR 2497: dropping the `.` alone gave `acme.web/` and
    # `acme_web/` the same default, so their nodes merged instead.
    dotted = tmp_path / "acme.web"
    underscored = tmp_path / "acme_web"
    for root, name in ((dotted, "render_page"), (underscored, "list_users")):
        root.mkdir()
        (root / "views.py").write_text(f"def {name}():\n    return 1\n")
    store = _StatefulIngestor()

    _index(dotted, store, None)
    _index(underscored, store, None)

    projects = {uid for label, uid in store.nodes if label == cs.NodeLabel.PROJECT}
    assert projects == {derive_project_name(dotted), "acme_web"}
    assert _defined_by(store, "acme_web.views") == {"acme_web.views.list_users"}


def test_a_plain_directory_keeps_its_name_as_the_default(tmp_path: Path) -> None:
    # Negative: only a name holding the separator changes.
    root = tmp_path / "billing"
    root.mkdir()
    (root / "views.py").write_text("def charge():\n    return 1\n")
    store = _StatefulIngestor()

    _index(root, store, None)

    assert (cs.NodeLabel.PROJECT, "billing") in store.nodes


def _module_qns(store: _StatefulIngestor) -> set[str]:
    return {
        str(uid) for label, uid in store.nodes if label == cs.NodeLabel.MODULE.value
    }


class _Retiring(_StatefulIngestor):
    """Records the projects a run retires, and the modules present then."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []
        self.calls: list[str] = []
        self.modules_at_retirement: set[str] = set()

    def _record(self, project_name: str) -> None:
        self.deleted.append(project_name)
        self.calls.append("graph")
        self.modules_at_retirement = _module_qns(self)

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        if query == cq.CYPHER_RETIRE_PROJECT and params is not None:
            self._record(str(params[cs.KEY_PROJECT_NAME]))
        super().execute_write(query, params)

    def delete_project(self, project_name: str) -> None:
        # What CYPHER_DELETE_PROJECT deletes: everything its containment walk
        # reaches. Kept so a retirement through it is caught by the tests
        # below; the walk crosses the Folder nodes two projects of one
        # checkout share.
        self._record(project_name)
        project = (cs.NodeLabel.PROJECT.value, project_name)
        doomed = {project} | self._reachable(
            project, cgr_graph._PROJECT_CONTAINMENT_RELS
        )
        for node in set(doomed):
            doomed |= self._reachable(node, cgr_graph._PROJECT_DEFINED_RELS)
        self._detach_delete(doomed)


def _legacy_project(store: _StatefulIngestor, name: str, root: Path | None) -> None:
    properties: dict[str, str] = {cs.KEY_NAME: name}
    if root is not None:
        properties[cs.KEY_ROOT_PATH] = str(root.resolve())
    store.nodes[(cs.NodeLabel.PROJECT, name)] = properties


@pytest.fixture
def dotted_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "acme.web"
    root.mkdir()
    (root / "views.py").write_text("def render_page():\n    return 1\n")
    return root


def test_the_project_a_dotted_checkout_was_indexed_under_is_retired(
    dotted_checkout: Path,
) -> None:
    # Review of PR 2497: a re-sync under the new default left the old
    # `acme.web` project, with its colliding qualified names, in the graph.
    store = _Retiring()
    _legacy_project(store, "acme.web", dotted_checkout)

    _index(dotted_checkout, store, None)

    assert store.deleted == ["acme.web"]
    assert (cs.NodeLabel.PROJECT, derive_project_name(dotted_checkout)) in store.nodes


@pytest.mark.parametrize(
    "owner",
    ["another-checkout", "no-recorded-root"],
)
def test_an_old_dotted_project_this_checkout_does_not_own_is_kept(
    dotted_checkout: Path, tmp_path: Path, owner: str
) -> None:
    # Negative: only a project whose root is this checkout is its own.
    store = _Retiring()
    elsewhere = tmp_path / "elsewhere" / "acme.web"
    elsewhere.mkdir(parents=True)
    _legacy_project(
        store, "acme.web", elsewhere if owner == "another-checkout" else None
    )

    _index(dotted_checkout, store, None)

    assert store.deleted == []
    assert (cs.NodeLabel.PROJECT, "acme.web") in store.nodes


def test_an_old_dotted_project_sharing_nodes_is_kept_with_a_warning(
    dotted_checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Negative: with a project `acme` in the graph, `acme.web`'s containers
    # may be `acme`'s package `web` too, and deleting them would delete it.
    store = _Retiring()
    _legacy_project(store, "acme.web", dotted_checkout)
    _legacy_project(store, "acme", dotted_checkout.parent / "acme")
    records: list[str] = []
    sink = logger.add(records.append, level="WARNING", format="{message}")
    try:
        _index(dotted_checkout, store, None)
    finally:
        logger.remove(sink)

    assert store.deleted == []
    assert any("acme.web" in r and "cgr delete-project" in r for r in records)


def test_a_named_run_never_retires_a_project(dotted_checkout: Path) -> None:
    # Negative: an explicit name says nothing about the directory's default.
    store = _Retiring()
    _legacy_project(store, "acme.web", dotted_checkout)

    _index(dotted_checkout, store, "frontend")

    assert store.deleted == []


def _with_folder(root: Path) -> Path:
    # A plain directory is a Folder keyed on its absolute path, so the old
    # project and the new one of this checkout share it.
    (root / "app").mkdir()
    (root / "app" / "routes.py").write_text("def list_pages():\n    return []\n")
    return root


def test_the_new_projects_nodes_exist_before_the_old_project_goes(
    dotted_checkout: Path,
) -> None:
    # CodeRabbit on PR 2497: the old project was deleted before indexing, and
    # a parse or flush failure after that left neither project whole.
    store = _Retiring()
    _index(_with_folder(dotted_checkout), store, "acme.web")
    new = derive_project_name(dotted_checkout)

    _index(dotted_checkout, store, None)

    assert store.deleted == ["acme.web"]
    assert {f"{new}.views", f"{new}.app.routes"} <= store.modules_at_retirement


def test_retiring_the_old_project_keeps_every_node_of_the_new_one(
    dotted_checkout: Path,
) -> None:
    # The two projects share the checkout's Folder nodes; a delete that
    # walked through them would take the new project's modules with it.
    store = _Retiring()
    _index(_with_folder(dotted_checkout), store, "acme.web")
    new = derive_project_name(dotted_checkout)

    _index(dotted_checkout, store, None)

    assert _module_qns(store) == {f"{new}.views", f"{new}.app.routes"}
    assert _defined_by(store, f"{new}.app.routes") == {f"{new}.app.routes.list_pages"}
    assert (cs.NodeLabel.PROJECT, "acme.web") not in store.nodes
    folder = (dotted_checkout / "app").resolve().as_posix()
    assert (cs.NodeLabel.FOLDER.value, folder) in store.nodes


class _FailingFlush(_Retiring):
    def flush_all(self) -> None:
        raise RuntimeError("flush failed")


def test_a_failed_sync_keeps_the_old_project(dotted_checkout: Path) -> None:
    # CodeRabbit on PR 2497: the old project goes only once its replacement
    # is written.
    store = _FailingFlush()
    _legacy_project(store, "acme.web", dotted_checkout)

    with pytest.raises(RuntimeError, match="flush failed"):
        _index(dotted_checkout, store, None)

    assert store.deleted == []
    assert (cs.NodeLabel.PROJECT, "acme.web") in store.nodes


def test_an_in_sync_run_retires_an_old_project_left_behind(
    dotted_checkout: Path,
) -> None:
    # The replacement is already whole, so a sync with nothing to do still
    # finishes the move.
    store = _Retiring()
    parsers, queries = load_parsers()
    GraphUpdater(store, dotted_checkout, parsers, queries).run()
    _legacy_project(store, "acme.web", dotted_checkout)

    updater = GraphUpdater(store, dotted_checkout, parsers, queries)
    updater.run()

    assert updater.skipped_because_in_sync
    assert store.deleted == ["acme.web"]


class _WithVectors(_Retiring):
    """Answers the read of a project's vector ids, which the double lacks."""

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if query == cs.CYPHER_QUERY_PROJECT_NODE_IDS:
            assert params == {cs.KEY_PROJECT_NAME: "acme.web"}
            return [{cs.KEY_NODE_ID: 7}, {cs.KEY_NODE_ID: 9}]
        return super().fetch_all(query, params)


def test_the_old_projects_vectors_go_before_its_nodes(dotted_checkout: Path) -> None:
    # CodeRabbit on PR 2497: vectors are keyed by node id, and a deleted
    # project's stale ones crowd out live results.
    store = _WithVectors()
    _legacy_project(store, "acme.web", dotted_checkout)

    def delete_vectors(project: str, node_ids: list[int]) -> bool:
        store.calls.append(f"vectors {project} {node_ids}")
        return True

    with patch(
        "codebase_rag.vector_store.delete_project_embeddings",
        side_effect=delete_vectors,
    ):
        _index(dotted_checkout, store, None)

    assert store.calls == ["vectors acme.web [7, 9]", "graph"]


class _UnreadableVectors(_Retiring):
    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if query == cs.CYPHER_QUERY_PROJECT_NODE_IDS:
            raise RuntimeError("graph read failed")
        return super().fetch_all(query, params)


def test_an_unreadable_vector_list_keeps_the_old_project(
    dotted_checkout: Path,
) -> None:
    # Negative: without the ids its vectors could never be found again, so
    # the project stays for the next sync and the sync itself succeeds.
    store = _UnreadableVectors()
    _legacy_project(store, "acme.web", dotted_checkout)
    records: list[str] = []
    sink = logger.add(records.append, level="WARNING", format="{message}")
    try:
        _index(dotted_checkout, store, None)
    finally:
        logger.remove(sink)

    assert store.deleted == []
    assert (cs.NodeLabel.PROJECT, "acme.web") in store.nodes
    assert any("acme.web" in r and "graph read failed" in r for r in records)


@pytest.fixture
def vector_client() -> Generator[MagicMock, None, None]:
    # The real vector-store path down to the backend client, which swallows a
    # failed delete into a warning; patched past the dependency check so the
    # base install, which has no qdrant-client, runs it too.
    from codebase_rag import vector_store

    client = MagicMock()
    with (
        patch.object(
            vector_store.settings,
            "VECTOR_STORE_BACKEND",
            cs.VectorStoreBackend.QDRANT,
        ),
        patch.object(vector_store, "has_qdrant_client", return_value=True),
        patch.object(vector_store, "get_qdrant_client", return_value=client),
    ):
        yield client


def test_an_old_project_whose_vectors_could_not_be_deleted_is_kept(
    dotted_checkout: Path, vector_client: MagicMock
) -> None:
    # Greptile on PR 2497: the store logged the failed delete and returned,
    # the project and its node ids went, and the vectors stayed in unscoped
    # search with nothing left to find them by.
    store = _WithVectors()
    _legacy_project(store, "acme.web", dotted_checkout)
    vector_client.delete.side_effect = RuntimeError("vector store down")
    records: list[str] = []
    sink = logger.add(records.append, level="WARNING", format="{message}")
    try:
        _index(dotted_checkout, store, None)
    finally:
        logger.remove(sink)

    assert store.deleted == []
    assert (cs.NodeLabel.PROJECT, "acme.web") in store.nodes
    assert ls.LEGACY_DOTTED_PROJECT_VECTORS_KEPT.format(legacy="acme.web") in [
        r.rstrip("\n") for r in records
    ]


def test_the_next_sync_retires_the_old_project_once_its_vectors_go(
    dotted_checkout: Path, vector_client: MagicMock
) -> None:
    # Negative: a delete that succeeds lets the project go, on the sync after
    # a failed one as on the first.
    store = _WithVectors()
    _legacy_project(store, "acme.web", dotted_checkout)
    vector_client.delete.side_effect = RuntimeError("vector store down")
    _index(dotted_checkout, store, None)
    assert store.deleted == []
    vector_client.delete.side_effect = None
    vector_client.delete.reset_mock()

    parsers, queries = load_parsers()
    GraphUpdater(store, dotted_checkout, parsers, queries).run()

    assert store.deleted == ["acme.web"]
    assert (cs.NodeLabel.PROJECT, "acme.web") not in store.nodes
    assert vector_client.delete.call_count == 1
    assert vector_client.delete.call_args.kwargs["points_selector"] == [7, 9]


def _shared_module_graph(tmp_path: Path, store: _StatefulIngestor) -> Path:
    # Projects `acme.web` (package `api`) and `acme.web.api`, both from before
    # #2412, wrote one Module `acme.web.api.views`.
    web = tmp_path / "acme.web"
    (web / "api").mkdir(parents=True)
    (web / "api" / "__init__.py").write_text("")
    (web / "api" / "views.py").write_text("def web_view():\n    return 1\n")
    api = tmp_path / "acme.web.api"
    api.mkdir()
    (api / "views.py").write_text("def api_view():\n    return 2\n")
    _index(web, store, "acme.web")
    _index(api, store, "acme.web.api")
    return web


def test_a_project_named_under_the_old_one_keeps_their_shared_module(
    tmp_path: Path,
) -> None:
    # CodeRabbit on PR 2497: the guard covered `acme` but not `acme.web.api`,
    # and retiring `acme.web` deleted the Module both had written.
    store = _Retiring()
    web = _shared_module_graph(tmp_path, store)
    assert "acme.web.api.views.api_view" in _defined_by(store, "acme.web.api.views")
    records: list[str] = []
    sink = logger.add(records.append, level="INFO", format="{message}")
    try:
        _index(web, store, None)
    finally:
        logger.remove(sink)

    assert store.deleted == []
    assert "acme.web.api.views.api_view" in _defined_by(store, "acme.web.api.views")
    assert (cs.NodeLabel.PROJECT, "acme.web") in store.nodes
    assert ls.LEGACY_DOTTED_PROJECT_KEPT_FOR_DESCENDANTS.format(
        legacy="acme.web",
        project=derive_project_name(web),
        descendants="acme.web.api",
    ) in [r.rstrip("\n") for r in records]


def test_a_project_that_only_starts_with_the_old_name_does_not_block_it(
    dotted_checkout: Path, tmp_path: Path
) -> None:
    # Negative: `acme.webapp` is not named under `acme.web`, so the old
    # project still goes.
    store = _Retiring()
    _legacy_project(store, "acme.web", dotted_checkout)
    _legacy_project(store, "acme.webapp", tmp_path / "acme.webapp")

    _index(dotted_checkout, store, None)

    assert store.deleted == ["acme.web"]
    assert (cs.NodeLabel.PROJECT, "acme.webapp") in store.nodes


def _qns_of(store: _StatefulIngestor, project: str) -> set[str]:
    # A project's qualified names: the bare name (its root Package and
    # Module) and everything under `<project>.`.
    return {
        qn
        for props in store.nodes.values()
        if isinstance(qn := props.get(cs.KEY_QUALIFIED_NAME), str)
        and (qn == project or qn.startswith(f"{project}{cs.SEPARATOR_DOT}"))
    }


def _with_root_package(root: Path) -> Path:
    # A repository-root `__init__.py` makes the checkout itself a Package,
    # whose qualified name, and its Module's, is the bare project name.
    (root / "__init__.py").write_text("def root_helper():\n    return 0\n")
    return root


def test_retiring_the_old_project_takes_its_root_package_and_module(
    dotted_checkout: Path,
) -> None:
    # Greptile on PR 2497: the retirement matched only `acme.web.` prefixes,
    # so the root Package and Module `acme.web`, and what they define, stayed.
    store = _Retiring()
    _index(_with_root_package(dotted_checkout), store, "acme.web")
    assert {"acme.web", "acme.web.root_helper"} <= _qns_of(store, "acme.web")
    new = derive_project_name(dotted_checkout)

    _index(dotted_checkout, store, None)

    assert store.deleted == ["acme.web"]
    assert _qns_of(store, "acme.web") == set()
    assert {new, f"{new}.root_helper"} <= _qns_of(store, new)


def test_retiring_the_old_project_keeps_one_whose_name_only_starts_with_it(
    dotted_checkout: Path,
) -> None:
    # Negative: `acme.webapp`'s names start with `acme.web` but are not that
    # name or under `acme.web.`. Its checkout sits inside the old one's, so
    # the retirement walk reaches its modules through the Folders they share.
    # It is indexed first: indexing it after drops the outer project's Folder
    # at its root, which would cut the walk off before it got there.
    root = _with_root_package(dotted_checkout)
    nested = root / "plugins"
    (nested / "app").mkdir(parents=True)
    (nested / "app" / "hooks.py").write_text("def on_load():\n    return 1\n")
    store = _Retiring()
    _index(nested, store, "acme.webapp")
    _index(root, store, "acme.web")
    kept = _qns_of(store, "acme.webapp")
    assert "acme.webapp.app.hooks.on_load" in kept

    _index(root, store, None)

    assert store.deleted == ["acme.web"]
    assert _qns_of(store, "acme.web") == set()
    assert _qns_of(store, "acme.webapp") == kept


def _rels_in(query: str, anchor: str) -> frozenset[str]:
    match = re.search(rf"{re.escape(anchor)}-\[:([A-Z_|]+)\*", query)
    assert match is not None, query
    return frozenset(match.group(1).split("|"))


def test_the_double_walks_what_the_retire_query_walks() -> None:
    query = cq.CYPHER_RETIRE_PROJECT

    assert _rels_in(query, "(p)") == cgr_graph._PROJECT_CONTAINMENT_RELS
    assert _rels_in(query, "(container)") == cgr_graph._PROJECT_DEFINED_RELS
    assert _rels_in(cq.CYPHER_DELETE_PROJECT, "(p)") == _rels_in(query, "(p)")


def test_check_reads_the_scope_an_unnamed_run_stamped_on_a_dotted_directory(
    tmp_path: Path,
) -> None:
    # `cgr index` without a name stamps the updater's default and `cgr check`
    # derives the digest-suffixed name; the stamp answers to both spellings
    # of the tree's identity, including the separator-free default.
    root = tmp_path / "acme.web"
    root.mkdir()
    (root / "views.py").write_text("def render_page():\n    return 1\n")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=_StatefulIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        exclude_paths=frozenset({"gen"}),
    ).run(force=True)

    assert indexed_scope(root, derive_project_name(root)) == (
        frozenset({"gen"}),
        None,
    )


def test_a_stamp_written_under_the_old_dotted_default_is_still_read(
    tmp_path: Path,
) -> None:
    # Negative: a stamp from before this change carries the verbatim name.
    root = tmp_path / "acme.web"
    root.mkdir()
    (root / cs.EXCLUSION_STATE_FILENAME).write_text(
        json.dumps({"project": "acme.web", "exclude": ["gen"], "unignore": []})
    )

    assert indexed_scope(root, derive_project_name(root)) == (
        frozenset({"gen"}),
        None,
    )
