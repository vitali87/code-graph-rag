"""Issue #2415: one way to choose the project, on every command that reads one.

Project names are `<dir>__<hash>`, so nobody can guess them. `cgr graph`
derived the project from `--repo-path` (default `.`), but `dead-code` and
`duplicates` took `-n/--project-name` and otherwise failed with every indexed
name on a multi-project graph, `delete-project` needed `--name`, and `stats`
and `export` could not be scoped to the checkout at all. Every command now
takes `-p/--project`, or else the project `--repo-path` (default: the current
directory) was indexed as; the old spellings still work, hidden.
"""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner as ClickRunner
from rich.console import Console
from typer.testing import CliRunner

from codebase_rag import cli
from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.checkout_state import state_file
from codebase_rag.cli import app
from codebase_rag.graph_cli import cli as graph_cli
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.types_defs import DuplicatesReport, PropertyParams, ResultRow
from codebase_rag.utils.path_utils import derive_project_name

# Indexed elsewhere, with names nothing about the current checkout suggests.
ACME = "acme"
COBRA = "cobra__125fa53b"
EMPTY_REPORT = DuplicatesReport(groups=[], skipped_symbols=0, truncated=False)

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    # One line per message whatever the tmp path's length.
    monkeypatch.setattr(cli.app_context, "console", Console(width=1000))


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    # The repository `cgr start --update-graph` indexed: `click__<hash>`.
    root = tmp_path / "work" / "click"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def click_project(checkout: Path) -> str:
    return derive_project_name(checkout)


@pytest.fixture
def roots(checkout: Path, click_project: str) -> dict[str, str | None]:
    return {click_project: str(checkout), ACME: "/srv/acme", COBRA: "/srv/cobra"}


@pytest.fixture
def ingestor(roots: dict[str, str | None]) -> Generator[MagicMock, None, None]:
    mock = MagicMock()
    mock.list_projects.side_effect = lambda: sorted(roots)
    mock.list_project_roots.side_effect = lambda: dict(roots)

    def fetch(query: str, params: PropertyParams | None = None) -> list[ResultRow]:
        if query == cq.CYPHER_LIST_PROJECTS:
            return [
                {cs.KEY_NAME: name, cs.KEY_ROOT_PATH: root}
                for name, root in sorted(roots.items())
            ]
        return []

    mock.fetch_all.side_effect = fetch
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    with patch("codebase_rag.cli.connect_memgraph", return_value=mock) as connect:
        mock.connect = connect
        yield mock


@pytest.fixture
def dead_code_scan() -> Generator[MagicMock, None, None]:
    with patch(
        "codebase_rag.dead_code.collect_dead_code_with_coverage", return_value=([], 0)
    ) as scan:
        yield scan


@pytest.fixture
def duplicates_scan() -> Generator[MagicMock, None, None]:
    with patch(
        "codebase_rag.duplicates.collect_duplicates_with_coverage",
        return_value=EMPTY_REPORT,
    ) as scan:
        yield scan


@pytest.fixture
def exported() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli.export_graph_to_file", return_value=True) as export:
        yield export


@pytest.fixture
def embeddings() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli.delete_project_embeddings") as delete:
        yield delete


def _run(args: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(app, args)
    return result.exit_code, click.unstyle(result.output)


def _scanned(scan: MagicMock) -> str:
    scan.assert_called_once()
    return str(scan.call_args.args[1])


def _stats_scope(ingestor: MagicMock) -> list[str] | None:
    for call in ingestor.fetch_all.call_args_list:
        if call.args[0] == cq.CYPHER_STATS_PROJECT_NODE_COUNTS:
            return list(call.args[1][cs.KEY_PROJECT_NAMES])
    return None


def _export_scope(export: MagicMock) -> list[str]:
    export.assert_called_once()
    return list(export.call_args.args[2])


# --- the current directory picks the project ----------------------------------


class TestCurrentDirectoryDefault:
    def test_dead_code_scans_the_checkout_it_runs_in(
        self,
        checkout: Path,
        click_project: str,
        ingestor: MagicMock,
        dead_code_scan: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(checkout)

        code, out = _run(["dead-code", "--format", "json"])

        assert code == 0, out
        assert _scanned(dead_code_scan) == click_project

    def test_duplicates_scans_the_checkout_it_runs_in(
        self,
        checkout: Path,
        click_project: str,
        ingestor: MagicMock,
        duplicates_scan: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(checkout)

        code, out = _run(["duplicates", "--format", "json"])

        assert code == 0, out
        assert _scanned(duplicates_scan) == click_project


class TestRepoPath:
    def test_dead_code_takes_the_project_of_a_repo_path(
        self,
        checkout: Path,
        click_project: str,
        ingestor: MagicMock,
        dead_code_scan: MagicMock,
    ) -> None:
        code, out = _run(["dead-code", "--repo-path", str(checkout)])

        assert code == 0, out
        assert _scanned(dead_code_scan) == click_project

    def test_duplicates_takes_the_project_of_a_repo_path(
        self,
        checkout: Path,
        click_project: str,
        ingestor: MagicMock,
        duplicates_scan: MagicMock,
    ) -> None:
        code, out = _run(["duplicates", "--repo-path", str(checkout)])

        assert code == 0, out
        assert _scanned(duplicates_scan) == click_project

    def test_stats_counts_the_project_of_a_repo_path(
        self, checkout: Path, click_project: str, ingestor: MagicMock
    ) -> None:
        code, out = _run(["stats", "--repo-path", str(checkout)])

        assert code == 0, out
        assert _stats_scope(ingestor) == [click_project]

    def test_export_writes_the_project_of_a_repo_path(
        self,
        checkout: Path,
        click_project: str,
        ingestor: MagicMock,
        exported: MagicMock,
        tmp_path: Path,
    ) -> None:
        code, out = _run(
            ["export", "-o", str(tmp_path / "g.json"), "--repo-path", str(checkout)]
        )

        assert code == 0, out
        assert _export_scope(exported) == [click_project]

    def test_delete_project_deletes_this_repos_project_and_its_cache(
        self,
        checkout: Path,
        click_project: str,
        ingestor: MagicMock,
        embeddings: MagicMock,
    ) -> None:
        cache = state_file(checkout, cs.HASH_CACHE_FILENAME)
        cache.write_text(json.dumps({"a.py": "abc"}))

        code, out = _run(["delete-project", "--repo-path", str(checkout)])

        assert code == 0, out
        ingestor.delete_project.assert_called_once_with(click_project)
        assert not cache.exists()


# --- one spelling: -p/--project ---------------------------------------------


class TestProjectOption:
    @pytest.mark.parametrize("flag", ["-p", "--project"])
    def test_dead_code(
        self, flag: str, ingestor: MagicMock, dead_code_scan: MagicMock
    ) -> None:
        code, out = _run(["dead-code", flag, COBRA])

        assert code == 0, out
        assert _scanned(dead_code_scan) == COBRA

    @pytest.mark.parametrize("flag", ["-p", "--project"])
    def test_duplicates(
        self, flag: str, ingestor: MagicMock, duplicates_scan: MagicMock
    ) -> None:
        code, out = _run(["duplicates", flag, COBRA])

        assert code == 0, out
        assert _scanned(duplicates_scan) == COBRA

    def test_stats_repeats_it(self, ingestor: MagicMock) -> None:
        code, out = _run(["stats", "-p", ACME, "--project", COBRA])

        assert code == 0, out
        assert _stats_scope(ingestor) == [ACME, COBRA]

    def test_export_repeats_it(
        self, ingestor: MagicMock, exported: MagicMock, tmp_path: Path
    ) -> None:
        code, out = _run(
            ["export", "-o", str(tmp_path / "g.json"), "-p", ACME, "--project", COBRA]
        )

        assert code == 0, out
        assert _export_scope(exported) == [ACME, COBRA]

    @pytest.mark.parametrize("flag", ["-p", "--project"])
    def test_delete_project(
        self, flag: str, ingestor: MagicMock, embeddings: MagicMock
    ) -> None:
        code, out = _run(["delete-project", flag, COBRA])

        assert code == 0, out
        ingestor.delete_project.assert_called_once_with(COBRA)

    def test_graph(
        self, checkout: Path, click_project: str, ingestor: MagicMock
    ) -> None:
        with patch("codebase_rag.cli_runtime.connect_memgraph", return_value=ingestor):
            result = ClickRunner().invoke(
                graph_cli, ["resolve", "f", "-p", ACME, "--repo-path", str(checkout)]
            )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == []

    def test_check(self, checkout: Path, ingestor: MagicMock) -> None:
        # Through the check's own not-indexed refusal, which names the
        # project it was given.
        with patch("codebase_rag.cli.load_parsers", return_value=({}, {})):
            result = CliRunner().invoke(
                app, ["check", "-p", "ghost", "--repo-path", str(checkout)]
            )

        assert result.exit_code == 1, result.output
        assert result.stderr == cs.CHECK_NOT_INDEXED.format(project="ghost") + "\n"

    @pytest.mark.parametrize(
        "command",
        [
            ["dead-code"],
            ["duplicates"],
            ["stats"],
            ["export"],
            ["delete-project"],
            ["check"],
            ["rename"],
            ["context"],
            ["graph", "resolve"],
            ["edits", "undo"],
        ],
    )
    def test_help_shows_one_spelling(self, command: list[str]) -> None:
        code, out = _run(["help", *command])

        assert code == 0, out
        assert "--project" in out
        assert re.search(r"(?<![\w-])-p\b", out), out
        assert "--project-name" not in out
        assert "--name" not in out


# --- refusals suggest, rather than list every project ------------------------


class TestRefusals:
    def test_another_checkout_of_the_same_directory_name_is_suggested(
        self,
        tmp_path: Path,
        click_project: str,
        ingestor: MagicMock,
        dead_code_scan: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A second `click` checkout that was never indexed.
        elsewhere = tmp_path / "elsewhere" / "click"
        elsewhere.mkdir(parents=True)
        monkeypatch.chdir(elsewhere)

        code, out = _run(["dead-code"])

        assert code == 1, out
        assert f"No project is indexed for {elsewhere.resolve()}" in out
        assert f"Did you mean: {click_project}" in out
        assert ACME not in out
        dead_code_scan.assert_not_called()

    def test_a_bare_directory_name_is_answered_with_its_hashed_names(
        self, click_project: str, ingestor: MagicMock, duplicates_scan: MagicMock
    ) -> None:
        code, out = _run(["duplicates", "-p", "click"])

        assert code == 1, out
        assert "Project 'click' is not indexed" in out
        assert f"Did you mean: {click_project}" in out
        assert ACME not in out
        duplicates_scan.assert_not_called()

    def test_a_repository_indexed_under_its_own_name_is_suggested(
        self,
        tmp_path: Path,
        roots: dict[str, str | None],
        ingestor: MagicMock,
        embeddings: MagicMock,
    ) -> None:
        # `cgr start --project-name shop` there: the derived name is not in
        # the graph, the repository is, under the name it was given.
        named = tmp_path / "shop-checkout"
        named.mkdir()
        roots["shop"] = str(named)

        code, out = _run(["delete-project", "--repo-path", str(named)])

        assert code == 1, out
        assert f"No project is indexed for {named.resolve()}" in out
        assert "Did you mean: shop" in out
        ingestor.delete_project.assert_not_called()
        embeddings.assert_not_called()

    def test_stats_suggests_the_close_match_of_a_typo(
        self, click_project: str, ingestor: MagicMock
    ) -> None:
        typo = click_project[:-1] + ("1" if click_project[-1] != "1" else "2")

        code, out = _run(["stats", "-p", typo])

        assert code == 1, out
        assert f"Not indexed: {typo}." in out
        assert f"Did you mean: {click_project}" in out
        assert ACME not in out
        assert _stats_scope(ingestor) is None


# --- negative: what must not change ---------------------------------------------


class TestExplicitWins:
    def test_a_name_beats_the_current_directory(
        self,
        checkout: Path,
        ingestor: MagicMock,
        dead_code_scan: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(checkout)

        code, out = _run(["dead-code", "-p", COBRA])

        assert code == 0, out
        assert _scanned(dead_code_scan) == COBRA

    def test_a_name_beats_a_repo_path(
        self, checkout: Path, ingestor: MagicMock, embeddings: MagicMock
    ) -> None:
        # The repo path still names the cache to drop, as before.
        code, out = _run(["delete-project", "-p", COBRA, "--repo-path", str(checkout)])

        assert code == 0, out
        ingestor.delete_project.assert_called_once_with(COBRA)

    def test_a_repo_path_beats_the_current_directory(
        self,
        tmp_path: Path,
        checkout: Path,
        ingestor: MagicMock,
        duplicates_scan: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(checkout)
        unindexed = tmp_path / "unindexed"
        unindexed.mkdir()

        code, out = _run(["duplicates", "--repo-path", str(unindexed)])

        assert code == 1, out
        assert f"No project is indexed for {unindexed.resolve()}" in out
        duplicates_scan.assert_not_called()

    @pytest.mark.parametrize(
        ("command", "alias"),
        [
            (["dead-code"], "-n"),
            (["duplicates"], "--project-name"),
            (["delete-project"], "--name"),
        ],
    )
    def test_two_different_names_are_refused(
        self, command: list[str], alias: str, ingestor: MagicMock
    ) -> None:
        code, out = _run([*command, "-p", ACME, alias, COBRA])

        assert code == 1, out
        assert ACME in out
        assert COBRA in out
        ingestor.connect.assert_not_called()


class TestOldSpellings:
    @pytest.mark.parametrize("flag", ["-n", "--project-name"])
    def test_dead_code(
        self, flag: str, ingestor: MagicMock, dead_code_scan: MagicMock
    ) -> None:
        code, out = _run(["dead-code", flag, ACME])

        assert code == 0, out
        assert _scanned(dead_code_scan) == ACME

    @pytest.mark.parametrize("flag", ["-n", "--project-name"])
    def test_duplicates(
        self, flag: str, ingestor: MagicMock, duplicates_scan: MagicMock
    ) -> None:
        code, out = _run(["duplicates", flag, ACME])

        assert code == 0, out
        assert _scanned(duplicates_scan) == ACME

    def test_stats_mixes_them_with_the_new_one(self, ingestor: MagicMock) -> None:
        code, out = _run(["stats", "-n", ACME, "--project-name", COBRA, "-p", ACME])

        assert code == 0, out
        assert _stats_scope(ingestor) == [ACME, COBRA]

    def test_export(
        self, ingestor: MagicMock, exported: MagicMock, tmp_path: Path
    ) -> None:
        code, out = _run(["export", "-o", str(tmp_path / "g.json"), "-n", ACME])

        assert code == 0, out
        assert _export_scope(exported) == [ACME]

    @pytest.mark.parametrize("flag", ["-n", "--name"])
    def test_delete_project(
        self, flag: str, ingestor: MagicMock, embeddings: MagicMock
    ) -> None:
        code, out = _run(["delete-project", flag, ACME])

        assert code == 0, out
        ingestor.delete_project.assert_called_once_with(ACME)

    def test_the_same_name_under_both_spellings_is_fine(
        self, ingestor: MagicMock, dead_code_scan: MagicMock
    ) -> None:
        code, out = _run(["dead-code", "-p", ACME, "-n", ACME])

        assert code == 0, out
        assert _scanned(dead_code_scan) == ACME


class TestOutsideAnyIndexedRepo:
    @pytest.fixture
    def outside(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        here = tmp_path / "scratch"
        here.mkdir()
        monkeypatch.chdir(here)
        return here

    def test_the_only_project_is_still_scanned(
        self,
        outside: Path,
        roots: dict[str, str | None],
        ingestor: MagicMock,
        dead_code_scan: MagicMock,
    ) -> None:
        roots.clear()
        roots[ACME] = "/srv/acme"

        code, out = _run(["dead-code"])

        assert code == 0, out
        assert _scanned(dead_code_scan) == ACME

    def test_several_projects_are_refused_with_the_indexed_names(
        self, outside: Path, ingestor: MagicMock, duplicates_scan: MagicMock
    ) -> None:
        # Nothing close to `scratch`: the list is the only lead left.
        code, out = _run(["duplicates"])

        assert code == 1, out
        assert f"No project is indexed for {outside.resolve()}" in out
        assert ACME in out
        assert COBRA in out
        duplicates_scan.assert_not_called()

    def test_an_empty_graph_is_refused(
        self,
        outside: Path,
        roots: dict[str, str | None],
        ingestor: MagicMock,
        dead_code_scan: MagicMock,
    ) -> None:
        roots.clear()

        code, out = _run(["dead-code"])

        assert code == 1, out
        assert "cgr start --update-graph" in out
        dead_code_scan.assert_not_called()

    def test_an_explicit_repo_path_never_falls_back_to_the_only_project(
        self,
        outside: Path,
        roots: dict[str, str | None],
        ingestor: MagicMock,
        dead_code_scan: MagicMock,
    ) -> None:
        roots.clear()
        roots[ACME] = "/srv/acme"

        code, out = _run(["dead-code", "--repo-path", str(outside)])

        assert code == 1, out
        dead_code_scan.assert_not_called()

    def test_stats_and_export_still_cover_the_whole_graph(
        self,
        outside: Path,
        ingestor: MagicMock,
        exported: MagicMock,
        tmp_path: Path,
    ) -> None:
        stats_code, stats_out = _run(["stats"])
        export_code, export_out = _run(["export", "-o", str(tmp_path / "g.json")])

        assert stats_code == 0, stats_out
        assert _stats_scope(ingestor) is None
        assert export_code == 0, export_out
        assert _export_scope(exported) == []

    def test_delete_project_never_defaults_to_the_current_directory(
        self,
        outside: Path,
        ingestor: MagicMock,
        embeddings: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        checkout: Path,
    ) -> None:
        # Even standing in an indexed checkout: deleting is never implied.
        monkeypatch.chdir(checkout)

        code, out = _run(["delete-project"])

        assert code == 1, out
        assert "--project" in out
        ingestor.connect.assert_not_called()
        ingestor.delete_project.assert_not_called()


# --- MCP keeps its project scoping ------------------------------------------------


class TestMcpScopingUnchanged:
    @pytest.fixture
    def registry(
        self, checkout: Path, ingestor: MagicMock
    ) -> Generator[MCPToolsRegistry, None, None]:
        with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
            yield MCPToolsRegistry(
                project_root=str(checkout), ingestor=ingestor, cypher_gen=MagicMock()
            )

    @staticmethod
    def _prefixes(ingestor: MagicMock) -> set[str]:
        return {
            str(call.args[1][cs.KEY_PROJECT_PREFIX])
            for call in ingestor.fetch_all.call_args_list
            if len(call.args) > 1
            and isinstance(call.args[1], dict)
            and cs.KEY_PROJECT_PREFIX in call.args[1]
        }

    async def test_an_omitted_project_is_the_server_roots(
        self, registry: MCPToolsRegistry, ingestor: MagicMock, click_project: str
    ) -> None:
        # The name the CLI derives for the same directory.
        assert await registry.resolve("f") == []
        assert self._prefixes(ingestor) == {f"{click_project}{cs.SEPARATOR_DOT}"}

    async def test_a_named_project_still_wins(
        self, registry: MCPToolsRegistry, ingestor: MagicMock
    ) -> None:
        assert await registry.resolve("f", project=ACME) == []
        assert self._prefixes(ingestor) == {f"{ACME}{cs.SEPARATOR_DOT}"}
