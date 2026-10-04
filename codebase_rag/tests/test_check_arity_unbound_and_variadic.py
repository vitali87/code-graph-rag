"""Issue #2899: `cgr check` reports no `too_many` on Python that runs.

Two shapes were judged against the wrong parameter count:

1. An explicit call through the class, `Base.__init__(self, a)`: `self` is
   written as an argument, and the verdict added the implicit receiver on
   top of it, so it was counted twice.
2. `*rest` after a default holding `)`, `def defaults(a, b=dict(), *rest)`:
   the header scan stopped at the first `)`, the one in `dict()`, and never
   saw `*rest`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.graph_query import QueryFn
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import StructuralDelta, observe
from codebase_rag.types_defs import PropertyParams, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "arity"

LIB = (
    "def defaults(a, b=dict(), *rest):\n    return a\n\n\n"
    "def tupled(a, b=(1, 2), *rest):\n    return a\n\n\n"
    "def deco(x):\n    return lambda f: f\n\n\n"
    "@deco(1)\n"
    "def decorated(a, *rest):\n    return a\n\n\n"
    "def plain(a):\n    return a\n\n\n"
    "def kwonly(a, *, b=1):\n    return a\n\n\n"
    "class Base:\n"
    "    def __init__(self, a):\n        self.a = a\n\n"
    "    def m(self, x):\n        return x\n\n\n"
    "class Child(Base):\n"
    "    def __init__(self, a, b):\n"
    "        Base.__init__(self, a)\n"
    "        self.b = b\n\n"
    "    def m(self, x):\n        return Base.m(self, x)\n\n"
    "    def too_many_unbound(self, x):\n        return Base.m(self, x, x)\n\n"
    "    def too_many_bound(self, x):\n        return self.helper(x, x)\n\n"
    "    def helper(self, x):\n        return x\n"
)
USE = (
    "from lib import decorated, defaults, kwonly, plain, tupled\n\n\n"
    "def run():\n"
    "    defaults(1, 2, 3, 4)\n"
    "    tupled(1, 2, 3, 4)\n"
    "    decorated(1, 2, 3)\n"
    "    plain(1, 2)\n"
    "    kwonly(1, 2)\n"
)

Indexed = tuple[Path, _StatefulIngestor, GraphUpdater]


def _fetch(store: _StatefulIngestor) -> QueryFn:
    def fetch(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return store.fetch_all(query, dict(params) if params is not None else None)

    return fetch


@pytest.fixture
def delta(temp_repo: Path) -> StructuralDelta:
    root = temp_repo / PROJECT
    root.mkdir()
    (root / "lib.py").write_text(LIB)
    (root / "use.py").write_text(USE)
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    # The issue's edit: a trailing comment, so both files are re-parsed and
    # every site in them is judged.
    for name in ("lib.py", "use.py"):
        path = root / name
        path.write_text(path.read_text() + "\n# touched\n")
    return observe(
        _fetch(store),
        PROJECT,
        ["lib.py", "use.py"],
        lambda: updater.reingest(["lib.py", "use.py"]),
        repo_root=root,
    )


def _too_many(delta: StructuralDelta) -> set[tuple[str, int]]:
    return {(f["path"], f["line"] or 0) for f in delta["arity_findings"]}


@pytest.mark.parametrize(
    ("site", "shape"),
    [
        (("lib.py", 36), "Base.__init__(self, a)"),
        (("lib.py", 40), "Base.m(self, x)"),
        (("use.py", 5), "defaults(1, 2, 3, 4) against b=dict(), *rest"),
        (("use.py", 6), "tupled(1, 2, 3, 4) against b=(1, 2), *rest"),
    ],
)
def test_a_call_that_supplies_what_the_callee_takes_is_not_too_many(
    delta: StructuralDelta, site: tuple[str, int], shape: str
) -> None:
    assert site not in _too_many(delta), shape


# Negative: what must not change.


@pytest.mark.parametrize(
    ("site", "shape"),
    [
        (("use.py", 8), "plain(1, 2) against def plain(a)"),
        (("use.py", 9), "kwonly(1, 2): a bare `*` takes no positionals"),
        (("lib.py", 43), "Base.m(self, x, x): one too many, written unbound"),
        (("lib.py", 46), "self.helper(x, x): one too many, bound"),
    ],
)
def test_a_call_that_passes_too_many_is_still_too_many(
    delta: StructuralDelta, site: tuple[str, int], shape: str
) -> None:
    assert site in _too_many(delta), shape


def test_a_decorated_variadic_callee_is_still_not_too_many(
    delta: StructuralDelta,
) -> None:
    # `decorated(1, 2, 3)` under `@deco(1)`: already right, and the header
    # read must keep it so.
    assert ("use.py", 7) not in _too_many(delta)
