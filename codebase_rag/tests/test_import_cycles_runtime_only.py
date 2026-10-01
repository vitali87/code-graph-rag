"""Issue #2685: only imports that run at module import time make a cycle.

A function-local import runs when its function is called, and one under
`if TYPE_CHECKING:` never runs; they are the standard fixes for a circular
import. `cgr check` built its cycle graph from every Module-IMPORTS->Module
edge, so adding either reported a "new import cycle" and failed
`--fail-on-found`, although both modules import fine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.contract import measure
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import snapshot
from codebase_rag.types_defs import PropertyParams, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "cycleproj"
DB = "from app.models import User\n\n\ndef save(u):\n    return User(u)\n"
MODELS = "class User:\n    def __init__(self, name):\n        self.name = name\n"

LAZY = (
    MODELS
    + "\n    def persist(self):\n        from app.db import save\n        return save(self)\n"
)
TYPE_CHECKING_IMPORT = (
    "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from app.db import save\n\n"
    + MODELS
)
TYPING_TYPE_CHECKING = (
    "import typing\n\nif typing.TYPE_CHECKING:\n    from app.db import save\n\n"
    + MODELS
)
TOP_LEVEL = "from app.db import save\n\n" + MODELS
ELSE_BRANCH = (
    "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    pass\nelse:\n"
    "    from app.db import save\n\n" + MODELS
)
CLASS_BODY = "class User:\n    from app.db import save\n\n    def __init__(self, name):\n        self.name = name\n"


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        for rel, text in {
            "app/__init__.py": "",
            "app/db.py": DB,
            "app/models.py": MODELS,
        }.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        parsers, queries = load_parsers()
        self.store = _StatefulIngestor()
        self.updater = GraphUpdater(
            ingestor=self.store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=PROJECT,
        )
        self.updater.run(force=True)

    def fetch_all(self, query: str, params: PropertyParams | None) -> list[ResultRow]:
        return self.store.fetch_all(query, None if params is None else dict(params))

    def edit_models(self, text: str) -> list[list[str]]:
        (self.root / "app/models.py").write_text(text, encoding="utf-8")
        delta = measure(
            self.fetch_all,
            PROJECT,
            self.root,
            ["app/models.py"],
            self.updater.reingest,
        )
        return delta["new_import_cycles"]

    def import_scopes(self) -> set[str | None]:
        return {
            None
            if (scope := self.store.props_for(edge).get(cs.KEY_IMPORT_SCOPE)) is None
            else str(scope)
            for edge in self.store.keyed_edges
            if edge[1] == f"{PROJECT}.app.models"
            and edge[2] == cs.RelationshipType.IMPORTS.value
            and edge[4] == f"{PROJECT}.app.db"
        }


CYCLE = [[f"{PROJECT}.app.db", f"{PROJECT}.app.models"]]


@pytest.fixture
def repo(tmp_path: Path) -> _Repo:
    return _Repo(tmp_path / PROJECT)


@pytest.mark.parametrize(
    ("text", "scope"),
    [
        pytest.param(LAZY, cs.ImportScope.FUNCTION, id="lazy"),
        pytest.param(
            TYPE_CHECKING_IMPORT, cs.ImportScope.TYPE_CHECKING_BLOCK, id="type-checking"
        ),
        pytest.param(
            TYPING_TYPE_CHECKING,
            cs.ImportScope.TYPE_CHECKING_BLOCK,
            id="typing.type-checking",
        ),
    ],
)
def test_an_import_that_does_not_run_at_import_time_makes_no_cycle(
    repo: _Repo, text: str, scope: cs.ImportScope
) -> None:
    assert repo.edit_models(text) == []
    assert repo.import_scopes() == {scope.value}


# Negative: what must not change.


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(TOP_LEVEL, id="top-level"),
        pytest.param(ELSE_BRANCH, id="else-of-type-checking"),
        pytest.param(CLASS_BODY, id="class-body"),
    ],
)
def test_an_import_that_runs_at_import_time_still_makes_a_cycle(
    repo: _Repo, text: str
) -> None:
    assert repo.edit_models(text) == CYCLE
    assert repo.import_scopes() == {None}


def test_a_lazy_import_is_still_an_import_edge(repo: _Repo) -> None:
    repo.edit_models(LAZY)

    after = snapshot(repo.fetch_all, PROJECT, ["app/models.py"])

    assert f"{PROJECT}.app.db" in after.imports[f"{PROJECT}.app.models"]
