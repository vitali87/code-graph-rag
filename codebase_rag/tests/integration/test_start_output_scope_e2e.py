"""Issue #2440: `cgr start --update-graph -o` writes the synced repo's graph.

Indexes another project into a real Memgraph first, then runs the real
`cgr start --repo-path alpha --update-graph -o` against it and reads the file
back: it must hold alpha and none of the other project, while `cgr export`
without a scope still writes both.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.utils.path_utils import derive_project_name

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

ALPHA = """import json


def hello():
    return json.dumps("alpha")


class Service:
    def run(self):
        return hello()
"""
BETA = """import json


def greet():
    return json.dumps("beta")


class Handler:
    def handle(self, value):
        return greet()
"""


def _repo(root: Path, name: str, source: str) -> Path:
    repo = root / name
    repo.mkdir()
    (repo / f"{name}.py").write_text(source, encoding="utf-8")
    return repo


def _index(ingestor: MemgraphIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor, repo_path=repo, parsers=parsers, queries=queries
    ).run()
    ingestor.flush_all()


def _projects(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(node["properties"]["name"])
        for node in data["nodes"]
        if "Project" in node["labels"]
    }


@pytest.fixture
def two_projects(
    memgraph_ingestor: MemgraphIngestor,
    memgraph_container: dict[str, str | int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    # `cgr` connects through the settings, so point them at the test server.
    monkeypatch.setattr(settings, "MEMGRAPH_HOST", str(memgraph_container["host"]))
    monkeypatch.setattr(settings, "MEMGRAPH_PORT", int(memgraph_container["port"]))
    _index(memgraph_ingestor, _repo(tmp_path, "beta", BETA))
    return _repo(tmp_path, "alpha", ALPHA)


def _start(repo: Path, out: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "start",
            "--repo-path",
            str(repo),
            "--update-graph",
            "--no-start-stack",
            "--no-embeddings",
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output


def test_the_start_export_holds_the_synced_project_alone(
    two_projects: Path, tmp_path: Path
) -> None:
    out = tmp_path / "graph.json"

    _start(two_projects, out)

    alpha = derive_project_name(two_projects)
    assert _projects(out) == {alpha}
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["metadata"]["projects"] == [alpha]
    names = {
        str(node["properties"].get("qualified_name", "")) for node in data["nodes"]
    }
    assert not {name for name in names if name.startswith("beta")}


def test_every_relationship_of_the_start_export_has_both_ends_in_it(
    two_projects: Path, tmp_path: Path
) -> None:
    out = tmp_path / "graph.json"

    _start(two_projects, out)

    data = json.loads(out.read_text(encoding="utf-8"))
    ids = {node["node_id"] for node in data["nodes"]}
    ends = {rel["from_id"] for rel in data["relationships"]} | {
        rel["to_id"] for rel in data["relationships"]
    }
    assert data["relationships"]
    assert ends <= ids


def test_an_unscoped_export_still_holds_every_project(
    two_projects: Path, tmp_path: Path
) -> None:
    _start(two_projects, tmp_path / "graph.json")
    whole = tmp_path / "whole.json"

    result = CliRunner().invoke(app, ["export", "-o", str(whole)])

    assert result.exit_code == 0, result.output
    assert _projects(whole) == {derive_project_name(two_projects), "beta"}
