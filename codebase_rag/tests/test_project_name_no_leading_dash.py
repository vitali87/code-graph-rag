"""Issue #2638: a derived project name never starts with `-`.

`derive_project_name` replaced characters outside `[A-Za-z0-9_-]` with `_`
and stripped only underscores, so `мой-repo` became `-repo__<hash>`. Every
qualified name of that project then began with `-`, and Click read it as an
option wherever a command takes one as a positional argument (`cgr graph
callers`, `cgr rename`): "No such option: -r".
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.types_defs import ResultRow
from codebase_rag.utils.path_utils import derive_project_name


def _base(name: str) -> str:
    return name.split(cs.PROJECT_NAME_DIGEST_MARKER, 1)[0]


@pytest.mark.parametrize(
    ("directory", "base"),
    [
        ("мой-repo", "repo"),
        ("日本語-tool", "tool"),
        ("-leading", "leading"),
        ("проект с пробелом-ü", "repo"),
        ("trailing-", "trailing"),
        ("--both--", "both"),
        ("_-mixed-_", "mixed"),
    ],
)
def test_a_derived_name_never_starts_or_ends_with_a_dash(
    tmp_path: Path, directory: str, base: str
) -> None:
    repo = tmp_path / directory
    repo.mkdir()

    name = derive_project_name(repo)

    assert not name.startswith("-")
    assert _base(name) == base


def test_a_qualified_name_of_such_a_project_is_accepted_as_an_argument(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "мой-repo"
    repo.mkdir()
    project = derive_project_name(repo)
    qn = f"{project}.app.helper"

    # Indexed, and holding the name: an empty graph is refused as one that
    # never indexed the repo (issue #2461), before the name is looked at.
    def fetch_all(query: str, _params: object = None) -> list[ResultRow]:
        if query == cq.CYPHER_LIST_PROJECTS:
            return [{cs.KEY_NAME: project, cs.KEY_ROOT_PATH: str(repo)}]
        if query == cq.CYPHER_GRAPH_NODE_EXISTS:
            return [{cs.KEY_QUALIFIED_NAME: qn}]
        return []

    ingestor = MagicMock()
    ingestor.fetch_all.side_effect = fetch_all
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)

    with patch("codebase_rag.cli_runtime.connect_memgraph", return_value=ingestor):
        result = CliRunner().invoke(
            app, ["graph", "callers", qn, "--repo-path", str(repo)]
        )

    assert "No such option" not in result.output
    assert result.exit_code == 0, result.output


# Negative: what must not change.


@pytest.mark.parametrize(
    ("directory", "base"),
    [
        ("my-repo", "my-repo"),
        ("code-graph-rag", "code-graph-rag"),
        ("a_b", "a_b"),
        ("under_", "under"),
        ("café", "caf"),
        ("проект", "repo"),
    ],
)
def test_other_names_derive_as_before(
    tmp_path: Path, directory: str, base: str
) -> None:
    repo = tmp_path / directory
    repo.mkdir()

    assert _base(derive_project_name(repo)) == base


def test_the_digest_still_names_the_resolved_path(tmp_path: Path) -> None:
    repo = tmp_path / "мой-repo"
    repo.mkdir()
    digest = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[
        : cs.PROJECT_NAME_DIGEST_LEN
    ]

    assert derive_project_name(repo) == f"repo{cs.PROJECT_NAME_DIGEST_MARKER}{digest}"
