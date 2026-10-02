"""Issue #2611 against a real Memgraph: `path:line` targets in every spelling.

Two projects with the same layout are indexed into one database. A location
in `alpha` resolves however its path is spelled, a file `alpha` does not hold
is refused (the containment walk and the Module lookup run for real), and
`beta`'s files and root never answer for `alpha`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import graph_query
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

ALPHA = "alpha"
BETA = "beta"
SESSIONS = """import os


class Session:
    def __init__(self):
        self.cwd = os.getcwd()

    def close(self):
        return None
"""


def _repo(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return repo


def _index(ingestor: MemgraphIngestor, repo: Path, name: str) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=name,
    ).run()
    ingestor.flush_all()


def _qns(rows: object) -> list[str]:
    assert isinstance(rows, list), rows
    return [row["qualified_name"] for row in rows]


def test_location_spellings_resolve_and_unknown_files_are_refused(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    alpha = _repo(
        tmp_path,
        ALPHA,
        {"src/pkg/sessions.py": SESSIONS, "README.md": "# alpha\n\nnotes\n"},
    )
    beta = _repo(
        tmp_path,
        BETA,
        {"src/pkg/sessions.py": SESSIONS, "src/pkg/beta_only.py": "X = 1\n"},
    )
    _index(memgraph_ingestor, alpha, ALPHA)
    _index(memgraph_ingestor, beta, BETA)
    fetch = memgraph_ingestor.fetch_all

    stored = _qns(graph_query.resolve(fetch, ALPHA, "src/pkg/sessions.py:6"))
    assert stored == [
        f"{ALPHA}.src.pkg.sessions.Session.__init__",
        f"{ALPHA}.src.pkg.sessions.Session",
    ]
    for spelling in (
        "./src/pkg/sessions.py:6",
        "src\\pkg\\sessions.py:6",
        "src/pkg/sessions.py:6:9",
        f"{alpha}/src/pkg/sessions.py:6",
        f"{alpha}/src/pkg/sessions.py:6:9",
    ):
        assert _qns(graph_query.resolve_or_refuse(fetch, ALPHA, spelling)) == (
            stored
        ), spelling

    # A held file: no definition at the line, or none at all, stays [].
    assert graph_query.resolve_or_refuse(fetch, ALPHA, "src/pkg/sessions.py:1") == []
    assert graph_query.resolve_or_refuse(fetch, ALPHA, "README.md:2") == []

    # Not alpha's: a missing file, beta's own file, and beta's root.
    for target in (
        "src/pkg/nope.py:3",
        "src/pkg/beta_only.py:1",
        f"{beta}/src/pkg/sessions.py:6",
    ):
        refused = graph_query.resolve_or_refuse(fetch, ALPHA, target)
        assert isinstance(refused, dict), target
        assert repr(ALPHA) in refused["error"]
    # ... while beta answers its own absolute path.
    assert _qns(
        graph_query.resolve_or_refuse(fetch, BETA, f"{beta}/src/pkg/sessions.py:6")
    ) == [
        f"{BETA}.src.pkg.sessions.Session.__init__",
        f"{BETA}.src.pkg.sessions.Session",
    ]
