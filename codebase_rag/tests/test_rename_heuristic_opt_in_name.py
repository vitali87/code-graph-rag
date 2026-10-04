"""Issue #2886: a heuristic rename refusal names the opt-in each front end takes.

The refusal read "pass allow_heuristic to rewrite through them" everywhere.
That is the MCP parameter. On the command line the option is
`--allow-heuristic`, and `cgr rename ... allow_heuristic` is read as an extra
positional argument.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.core import TyperCommand, TyperGroup
from typer.testing import CliRunner, Result

from codebase_rag import cli_help as ch
from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

OPT_IN = re.compile(r"pass (\S+) to rewrite through them")


@pytest.fixture
def heuristic_repo(tmp_path: Path) -> tuple[Path, RecordedGraph]:
    root = tmp_path / "heur"
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/util.py", "def lonely():\n    return 1\n")
    # A star import: the call binds by name alone, so its site is heuristic.
    _write(
        root, "pkg/app.py", "from .util import *\n\n\ndef run():\n    return lonely()\n"
    )
    return root, _index(root, MagicMock())


def _cli_rename(repo: tuple[Path, RecordedGraph], new_name: str, *extra: str) -> Result:
    root, graph = repo
    with patch(
        "codebase_rag.graph_cli._project_and_fetch",
        return_value=(graph.project, graph.fetch_all, MagicMock()),
    ):
        return CliRunner().invoke(
            app,
            [
                "rename",
                f"{graph.project}.pkg.util.lonely",
                new_name,
                "--repo-path",
                str(root),
                *extra,
            ],
        )


def _rename_options() -> set[str]:
    command = typer.main.get_command(app)
    assert isinstance(command, TyperGroup)
    rename = command.commands[ch.CLICommandName.RENAME]
    assert isinstance(rename, TyperCommand)
    return {opt for param in rename.params for opt in param.opts}


def _mcp_refusal(repo: tuple[Path, RecordedGraph]) -> str:
    root, graph = repo
    ingestor = MagicMock()
    ingestor.fetch_all = graph.fetch_all
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        registry = MCPToolsRegistry(
            project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
        )
    result = registry._run_rename(
        graph.project, f"{graph.project}.pkg.util.lonely", "alone", False, True
    )
    assert isinstance(result, dict)
    return str(result[cs.DICT_KEY_ERROR])


def test_the_cli_refusal_names_the_cli_flag(
    heuristic_repo: tuple[Path, RecordedGraph],
) -> None:
    result = _cli_rename(heuristic_repo, "alone", "--dry-run")

    assert result.exit_code == 1
    assert "pass --allow-heuristic to rewrite through them" in result.stderr


def test_the_flag_the_cli_names_is_one_it_accepts(
    heuristic_repo: tuple[Path, RecordedGraph],
) -> None:
    result = _cli_rename(heuristic_repo, "alone", "--dry-run")

    named = OPT_IN.search(result.stderr)
    assert named is not None, result.stderr
    assert named.group(1) in _rename_options()


def test_the_cli_refusal_does_not_name_the_mcp_parameter(
    heuristic_repo: tuple[Path, RecordedGraph],
) -> None:
    result = _cli_rename(heuristic_repo, "alone", "--dry-run")

    assert cs.MCPParamName.ALLOW_HEURISTIC not in result.stderr


# Negative: what must not change.


def test_the_mcp_refusal_still_names_the_parameter(
    heuristic_repo: tuple[Path, RecordedGraph],
) -> None:
    refusal = _mcp_refusal(heuristic_repo)

    assert "pass allow_heuristic to rewrite through them" in refusal


def test_the_mcp_refusal_still_counts_the_heuristic_sites(
    heuristic_repo: tuple[Path, RecordedGraph],
) -> None:
    refusal = _mcp_refusal(heuristic_repo)

    assert "1 site(s) were resolved heuristically" in refusal


def test_the_cli_flag_still_lifts_the_refusal(
    heuristic_repo: tuple[Path, RecordedGraph],
) -> None:
    result = _cli_rename(heuristic_repo, "alone", "--allow-heuristic", "--dry-run")

    assert result.exit_code == 0, result.stderr
    assert '"applied": false' in result.stdout


def test_another_cli_refusal_is_printed_unchanged(
    heuristic_repo: tuple[Path, RecordedGraph],
) -> None:
    result = _cli_rename(heuristic_repo, "1bad", "--dry-run")

    assert result.exit_code == 1
    assert result.stderr == cs.RENAME_BAD_NAME.format(name="1bad") + "\n"
