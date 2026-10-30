"""`LegacyRetirement`: what an older cgr wrote for a checkout, and when it goes.

Driven directly, against a scripted graph, so each rule of the two sweeps is
pinned on its own: which out-of-repo File keys count as this project's alone
(issues #1156, #2405), and when the pre-#2412 dotted project of a checkout is
retired or kept (PR 2497). The end-to-end runs through `GraphUpdater` live in
test_graph_updater_pruning.py and test_project_name_separator.py.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.services.resource_cleanup import CYPHER_DELETE_UNANCHORED_RESOURCES
from codebase_rag.types_defs import PropertyParams, ResultRow
from codebase_rag.updater.legacy_retirement import LegacyRetirement, repo_scope_params
from codebase_rag.utils.path_utils import cached_file_identity_posix

type Answer = list[ResultRow] | Callable[[PropertyParams | None], list[ResultRow]]


class _Graph:
    """Answers each query from a scripted list (or a function of its params),
    raises for the queries in `failing`, and records every write."""

    def __init__(
        self, answers: dict[str, Answer] | None = None, failing: set[str] = frozenset()
    ) -> None:
        self.answers = answers or {}
        self.failing = set(failing)
        self.reads: list[str] = []
        self.writes: list[tuple[str, PropertyParams | None]] = []

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        self.reads.append(query)
        if query in self.failing:
            raise RuntimeError("graph unreachable")
        answer = self.answers.get(query, [])
        return answer(params) if callable(answer) else list(answer)

    def execute_write(self, query: str, params: PropertyParams | None = None) -> None:
        if query in self.failing:
            raise RuntimeError("write refused")
        self.writes.append((query, params))

    def written(self) -> list[str]:
        return [query for query, _params in self.writes]


def _retirement(
    graph: _Graph | None,
    repo: Path,
    *,
    project_name: str = "proj",
    project_named: bool = False,
    single_file: Path | None = None,
) -> LegacyRetirement:
    def graph_rows(query: str, params: PropertyParams | None) -> list[ResultRow]:
        if graph is None:
            raise TypeError("write-only sink")
        return graph.fetch_all(query, params)

    return LegacyRetirement(
        graph=graph,
        graph_rows=graph_rows,
        repo_path=repo,
        project_name=project_name,
        project_named=project_named,
        single_file=single_file,
    )


def test_repo_scope_params_bound_the_prefix_at_a_path_separator() -> None:
    assert repo_scope_params("/srv/repo") == {
        cs.KEY_REPO_ROOT: "/srv/repo",
        cs.KEY_REPO_PREFIX: "/srv/repo/",
    }


class TestSweepFileIdentities:
    @staticmethod
    def _graph(
        repo: Path,
        candidates: list[ResultRow],
        containers: dict[str, list[ResultRow]],
        failing: set[str] = frozenset(),
    ) -> _Graph:
        def container_rows(params: PropertyParams | None) -> list[ResultRow]:
            keys = (params or {}).get(cs.CYPHER_PARAM_PATHS)
            assert isinstance(keys, list)
            return [
                {**row, cs.KEY_FILE_KEY: key}
                for key in keys
                if isinstance(key, str)
                for row in containers.get(key, [])
            ]

        return _Graph(
            {
                cs.CYPHER_PROJECT_OUTSIDE_FILE_KEYS: candidates,
                cs.CYPHER_FILE_CONTAINERS: container_rows,
            },
            failing,
        )

    @staticmethod
    def _folder(path: str | None) -> ResultRow:
        return {"labels": ["Folder"], "name": None, "absolute_path": path}

    @staticmethod
    def _deleted(graph: _Graph) -> list[PropertyParams | None]:
        return [p for q, p in graph.writes if q == cs.CYPHER_DELETE_FILE]

    def test_only_keys_this_project_alone_contains_are_deleted(
        self, temp_repo: Path
    ) -> None:
        repo_abs = temp_repo.resolve().as_posix()
        graph = self._graph(
            temp_repo,
            [
                {"path": "cfg/a.yaml", "absolute_path": "/outside/a.yaml"},
                {"path": "cfg/b.yaml", "absolute_path": "/outside/b.yaml"},
                {"path": "c.yaml", "absolute_path": "/outside/c.yaml"},
            ],
            {
                "/outside/a.yaml": [self._folder(f"{repo_abs}/cfg")],
                # Also held by another checkout's folder: theirs too.
                "/outside/b.yaml": [
                    self._folder(f"{repo_abs}/cfg"),
                    self._folder("/elsewhere/cfg"),
                ],
                # Directly under this project's own Project node.
                "/outside/c.yaml": [
                    {"labels": ["Project"], "name": "proj", "absolute_path": None}
                ],
            },
        )

        swept = _retirement(graph, temp_repo).sweep_file_identities()

        assert swept == 2
        assert self._deleted(graph) == [
            {cs.KEY_PATH: "/outside/a.yaml"},
            {cs.KEY_PATH: "/outside/c.yaml"},
        ]

    def test_the_candidate_read_is_scoped_to_this_repo_and_project(
        self, temp_repo: Path
    ) -> None:
        seen: list[PropertyParams | None] = []

        def candidates(params: PropertyParams | None) -> list[ResultRow]:
            seen.append(params)
            return []

        graph = _Graph({cs.CYPHER_PROJECT_OUTSIDE_FILE_KEYS: candidates})

        assert _retirement(graph, temp_repo).sweep_file_identities() == 0
        repo_abs = temp_repo.resolve().as_posix()
        assert seen == [{**repo_scope_params(repo_abs), cs.KEY_PROJECT_NAME: "proj"}]
        # No candidate, no ownership read.
        assert graph.reads == [cs.CYPHER_PROJECT_OUTSIDE_FILE_KEYS]

    def test_a_sibling_directory_sharing_the_repo_name_prefix_is_foreign(
        self, temp_repo: Path
    ) -> None:
        # `/x/repo-old` starts with `/x/repo` but is not inside it.
        repo_abs = temp_repo.resolve().as_posix()
        graph = self._graph(
            temp_repo,
            [{"path": "cfg/a.yaml", "absolute_path": "/outside/a.yaml"}],
            {"/outside/a.yaml": [self._folder(f"{repo_abs}-old/cfg")]},
        )

        assert _retirement(graph, temp_repo).sweep_file_identities() == 0
        assert self._deleted(graph) == []

    def test_another_project_node_vetoes_the_delete(self, temp_repo: Path) -> None:
        graph = self._graph(
            temp_repo,
            [{"path": "a.yaml", "absolute_path": "/outside/a.yaml"}],
            {
                "/outside/a.yaml": [
                    {"labels": ["Project"], "name": "other", "absolute_path": None}
                ]
            },
        )

        assert _retirement(graph, temp_repo).sweep_file_identities() == 0

    def test_a_key_matching_its_derivable_identity_is_no_candidate(
        self, temp_repo: Path
    ) -> None:
        stored = cached_file_identity_posix(temp_repo / "sub" / "x.py")
        graph = self._graph(
            temp_repo,
            [
                {"path": "sub/x.py", "absolute_path": stored},
                # Rows without a usable path or key are not candidates either.
                {"path": "", "absolute_path": "/outside/y.py"},
                {"path": "z.py", "absolute_path": None},
            ],
            {},
        )

        assert _retirement(graph, temp_repo).sweep_file_identities() == 0
        assert cs.CYPHER_FILE_CONTAINERS not in graph.reads

    @pytest.mark.parametrize(
        "failing",
        [cs.CYPHER_PROJECT_OUTSIDE_FILE_KEYS, cs.CYPHER_FILE_CONTAINERS],
        ids=["candidates", "containers"],
    )
    def test_a_failed_read_deletes_nothing_and_does_not_raise(
        self, temp_repo: Path, failing: str
    ) -> None:
        repo_abs = temp_repo.resolve().as_posix()
        graph = self._graph(
            temp_repo,
            [{"path": "cfg/a.yaml", "absolute_path": "/outside/a.yaml"}],
            {"/outside/a.yaml": [self._folder(f"{repo_abs}/cfg")]},
            failing={failing},
        )

        assert _retirement(graph, temp_repo).sweep_file_identities() == 0
        assert graph.writes == []


class TestRetireDottedProject:
    @pytest.fixture
    def checkout(self, tmp_path: Path) -> Path:
        root = tmp_path / "acme.web"
        root.mkdir()
        return root

    @staticmethod
    def _graph(
        projects: dict[str, Path | None],
        node_ids: list[int] | None = None,
        failing: set[str] = frozenset(),
    ) -> _Graph:
        return _Graph(
            {
                cq.CYPHER_LIST_PROJECTS: [
                    {
                        "name": name,
                        "root_path": None if root is None else str(root),
                    }
                    for name, root in projects.items()
                ],
                cs.CYPHER_QUERY_PROJECT_NODE_IDS: [
                    {cs.KEY_NODE_ID: node_id} for node_id in node_ids or []
                ],
            },
            failing,
        )

    def test_the_old_dotted_project_of_this_checkout_is_retired(
        self, checkout: Path
    ) -> None:
        graph = self._graph({"acme.web": checkout, "acme_web_1a2b": checkout})

        _retirement(
            graph, checkout, project_name="acme_web_1a2b"
        ).retire_dotted_project()

        assert graph.writes == [
            (
                cq.CYPHER_RETIRE_PROJECT,
                {
                    cs.KEY_PROJECT_NAME: "acme.web",
                    cs.KEY_PROJECT_PREFIX: "acme.web.",
                },
            ),
            (cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES, None),
            (CYPHER_DELETE_UNANCHORED_RESOURCES, None),
        ]

    def test_its_vectors_go_before_its_nodes(
        self, checkout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codebase_rag.vectors import store

        graph = self._graph({"acme.web": checkout}, node_ids=[3, 5])
        order: list[str] = []

        def delete_embeddings(project: str, node_ids: list[int]) -> bool:
            order.append(f"vectors:{project}:{node_ids}")
            assert graph.writes == [], "the nodes went before their vectors"
            return True

        monkeypatch.setattr(store, "delete_project_embeddings", delete_embeddings)

        _retirement(graph, checkout, project_name="acme_web").retire_dotted_project()

        assert order == ["vectors:acme.web:[3, 5]"]
        assert graph.written()[0] == cq.CYPHER_RETIRE_PROJECT

    def test_vectors_that_cannot_be_deleted_keep_the_project(
        self, checkout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codebase_rag.vectors import store

        graph = self._graph({"acme.web": checkout}, node_ids=[3])
        monkeypatch.setattr(store, "delete_project_embeddings", lambda *_a: False)

        _retirement(graph, checkout, project_name="acme_web").retire_dotted_project()

        assert graph.writes == []

    def test_an_unreadable_node_id_list_keeps_the_project(self, checkout: Path) -> None:
        graph = self._graph(
            {"acme.web": checkout}, failing={cs.CYPHER_QUERY_PROJECT_NODE_IDS}
        )

        _retirement(graph, checkout, project_name="acme_web").retire_dotted_project()

        assert graph.writes == []

    def test_a_failed_retire_write_is_left_for_the_next_sync(
        self, checkout: Path
    ) -> None:
        graph = self._graph({"acme.web": checkout}, failing={cq.CYPHER_RETIRE_PROJECT})
        warnings: list[str] = []
        sink = logger.add(warnings.append, level="WARNING", format="{message}")
        try:
            _retirement(
                graph, checkout, project_name="acme_web"
            ).retire_dotted_project()
        finally:
            logger.remove(sink)

        assert graph.writes == []
        assert any("acme.web" in w for w in warnings)

    @pytest.mark.parametrize(
        "projects",
        [
            pytest.param({"acme.web": None}, id="no-recorded-root"),
            pytest.param({"acme.web": Path("/elsewhere/acme.web")}, id="other-root"),
            pytest.param({}, id="not-in-graph"),
        ],
    )
    def test_a_dotted_project_rooted_elsewhere_is_not_this_checkouts(
        self, checkout: Path, projects: dict[str, Path | None]
    ) -> None:
        graph = self._graph(projects)

        _retirement(graph, checkout, project_name="acme_web").retire_dotted_project()

        assert graph.writes == []

    @pytest.mark.parametrize(
        "neighbour", ["acme", "acme.web.api"], ids=["prefix", "descendant"]
    )
    def test_a_project_whose_names_overlap_it_keeps_it(
        self, checkout: Path, tmp_path: Path, neighbour: str
    ) -> None:
        graph = self._graph({"acme.web": checkout, neighbour: tmp_path / "other"})

        _retirement(graph, checkout, project_name="acme_web").retire_dotted_project()

        assert graph.writes == []

    def test_a_project_merely_starting_with_the_same_letters_does_not_keep_it(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        # `acme.webstore` neither prefixes nor extends `acme.web` at a dot.
        graph = self._graph({"acme.web": checkout, "acme.webstore": tmp_path / "store"})

        _retirement(graph, checkout, project_name="acme_web").retire_dotted_project()

        assert graph.written()[0] == cq.CYPHER_RETIRE_PROJECT

    def test_named_and_single_file_runs_never_retire(self, checkout: Path) -> None:
        graph = self._graph({"acme.web": checkout})

        _retirement(
            graph, checkout, project_name="frontend", project_named=True
        ).retire_dotted_project()
        _retirement(
            graph,
            checkout,
            project_name="acme_web",
            single_file=checkout / "views.py",
        ).retire_dotted_project()

        assert graph.writes == []
        # Decided without reading the graph at all.
        assert graph.reads == []

    def test_an_undotted_checkout_has_no_old_project(self, tmp_path: Path) -> None:
        plain = tmp_path / "acme_web"
        plain.mkdir()
        graph = self._graph({"acme_web": plain})

        _retirement(graph, plain, project_name="acme_web").retire_dotted_project()

        assert graph.reads == []
        assert graph.writes == []

    def test_an_unreadable_project_list_retires_nothing(self, checkout: Path) -> None:
        graph = self._graph({"acme.web": checkout}, failing={cq.CYPHER_LIST_PROJECTS})

        _retirement(graph, checkout, project_name="acme_web").retire_dotted_project()

        assert graph.writes == []


def test_a_write_only_sink_is_never_read(tmp_path: Path) -> None:
    # The reader raises for a sink with no graph, so reaching it would fail
    # the sync instead of skipping the retirement.
    checkout = tmp_path / "acme.web"
    checkout.mkdir()
    retirement = _retirement(None, checkout, project_name="acme_web")

    assert retirement.sweep_file_identities() == 0
    retirement.retire_dotted_project()
