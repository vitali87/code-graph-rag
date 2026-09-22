"""`cgr check --isolated` against a real Memgraph (issue #1718).

The unit tier proves the restore on the eval emulator, which models the
capture queries by value; this replays the same edit on the real store so
the Cypher itself is exercised: the variable-length subtree walk on both
kinds of scope node, `startNode` for the edge direction, `properties()` of
both ends, and the label-union finding delete. A gloss on a re-parsed
definition covers the far-end path: the re-parse re-grades the note's
anchor, and the restore must put the grade back.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import run_check

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

PROJECT = "isorepo"

FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(a):\n    return a + 1\n",
    # Two sites on one caller/callee pair: per-site edges (issue #1522)
    # share their endpoints and differ only in their properties, which the
    # real store keys on and the unit tier's double cannot (issue #1921).
    "pkg/app.py": (
        "from pkg.util import helper\n\n\ndef run():\n    return helper(0) + helper(1)\n"
    ),
    "main.py": "from pkg.app import run\n\n\ndef main():\n    run()\n",
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "from pkg.app import run\n\n\ndef test_run():\n    assert run() == 2\n"
    ),
}

_TIMINGS = ("reingest_ms", "delta_ms")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _index(ingestor: MemgraphIngestor, root: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)


def _edit(root: Path) -> None:
    """The edit the unit tier replays: a rename whose callers dangle, a
    deleted module, a new module in a new directory with a new external
    import, a flipped package indicator, and a changed body under a gloss."""
    _write(root, "pkg/util.py", FIXTURE["pkg/util.py"].replace("helper", "assist"))
    _write(root, "pkg/app.py", FIXTURE["pkg/app.py"].replace("helper(1)", "helper(2)"))
    (root / "main.py").unlink()
    _write(root, "lib/tool.py", "import os\n\n\ndef tool():\n    return os.sep\n")
    (root / "tests" / "__init__.py").unlink()


def _annotate(ingestor: MemgraphIngestor, qn: str) -> None:
    """A gloss on `qn` graded EXACT against its current anchor hash."""
    rows = ingestor.fetch_all(
        "MATCH (f:Function {qualified_name: $qn}) RETURN f.anchor_hash AS h",
        {"qn": qn},
    )
    assert rows, "the function node carries no anchor hash to annotate"
    assert rows[0]["h"], rows
    ingestor.execute_write(
        "MATCH (f:Function {qualified_name: $qn}) "
        "CREATE (g:Gloss {qualified_name: 'gloss-1', kind: 'note', body: 'x', "
        "target_qn: $qn, target_hash: $h, anchor_state: 'EXACT'}) "
        "CREATE (g)-[:ANNOTATES]->(f)",
        {"qn": qn, "h": rows[0]["h"]},
    )


def _dump(ingestor: MemgraphIngestor) -> tuple[list[str], list[str]]:
    nodes = ingestor.fetch_all(
        "MATCH (n) RETURN labels(n)[0] AS label, properties(n) AS props"
    )
    edges = ingestor.fetch_all(
        "MATCH (a)-[r]->(b) RETURN labels(a)[0] AS al, properties(a) AS ap, "
        "type(r) AS rel, properties(r) AS rp, labels(b)[0] AS bl, properties(b) AS bp"
    )
    return (
        sorted(json.dumps(row, sort_keys=True, default=str) for row in nodes),
        sorted(json.dumps(row, sort_keys=True, default=str) for row in edges),
    )


def _check(ingestor: MemgraphIngestor, root: Path, *, isolated: bool) -> dict:
    parsers, queries = load_parsers()
    delta = run_check(
        root, "HEAD", PROJECT, ingestor, parsers, queries, isolated=isolated
    )
    return {key: value for key, value in delta.items() if key not in _TIMINGS}


class TestIsolatedCheck:
    def test_the_store_reads_as_it_did(
        self, memgraph_ingestor: MemgraphIngestor, repo: Path
    ) -> None:
        _index(memgraph_ingestor, repo)
        _annotate(memgraph_ingestor, f"{PROJECT}.pkg.app.run")
        _edit(repo)
        before = _dump(memgraph_ingestor)
        assert before[0], "the graph holds no nodes to restore"
        assert before[1], "the graph holds no edges to restore"

        delta = _check(memgraph_ingestor, repo, isolated=True)

        assert _dump(memgraph_ingestor) == before
        assert delta["dangling_callers"][0]["target"] == f"{PROJECT}.pkg.util.helper"
        assert f"{PROJECT}.main.main" in delta["symbols"]["removed"]

    def test_the_applied_check_changes_the_store(
        self, memgraph_ingestor: MemgraphIngestor, repo: Path
    ) -> None:
        """The control: the same edit without isolation lands, including the
        re-graded gloss the isolated run has to put back."""
        _index(memgraph_ingestor, repo)
        _annotate(memgraph_ingestor, f"{PROJECT}.pkg.app.run")
        _edit(repo)
        before = _dump(memgraph_ingestor)

        _check(memgraph_ingestor, repo, isolated=False)

        assert _dump(memgraph_ingestor) != before
        rows = memgraph_ingestor.fetch_all(
            "MATCH (g:Gloss {qualified_name: 'gloss-1'}) RETURN g.anchor_state AS s"
        )
        assert rows[0]["s"] == "STALE"

    def test_the_finding_cleanup_spares_another_projects_findings(
        self, memgraph_ingestor: MemgraphIngestor, repo: Path
    ) -> None:
        """Findings key on a repo-relative path, so a sibling project in the
        shared graph can hold the same one. This runs the real Cypher; the
        unit tier can only check the double's modelling of it, because that
        store dispatches on query identity (greptile-local, #1718)."""
        _index(memgraph_ingestor, repo)
        memgraph_ingestor.execute_write(
            "CREATE (n:CodeSmell {qualified_name: $qn, path: $path})",
            {"qn": "otherproj.pkg.util.3.0.bare_except", "path": "pkg/util.py"},
        )
        _edit(repo)

        _check(memgraph_ingestor, repo, isolated=True)

        rows = memgraph_ingestor.fetch_all(
            "MATCH (n:CodeSmell {qualified_name: $qn}) RETURN count(n) AS c",
            {"qn": "otherproj.pkg.util.3.0.bare_except"},
        )
        assert int(rows[0]["c"]) == 1, "the sibling project's finding was deleted"

    def test_the_same_edit_measures_the_same_way_twice(
        self, memgraph_ingestor: MemgraphIngestor, repo: Path
    ) -> None:
        _index(memgraph_ingestor, repo)
        _edit(repo)

        first = _check(memgraph_ingestor, repo, isolated=True)
        second = _check(memgraph_ingestor, repo, isolated=True)

        assert first["dangling_callers"]
        assert second == first
