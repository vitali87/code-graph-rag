"""Issue #2903: `tests_reaching` includes the tests of a renamed or removed symbol.

The walk started from what the edit added, changed or renamed TO, and from
what the re-parsed files define afterwards. A caller of the old name keeps no
edge in the re-ingested graph (its target is gone), so the tests that reach
the symbol through it, which are exactly the tests that now fail, were left
out, while the same report listed that caller as dangling.
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

PROJECT = "treach"

STORE = (
    'def fetch(key):\n    return {"k": key}\n\n\n'
    "def save(key, value):\n    return True\n"
)
FILES = {
    "pkg/__init__.py": "",
    "pkg/store.py": STORE,
    "pkg/service.py": (
        "from pkg import store\n\n\n"
        "def get(key):\n    return store.fetch(key)\n\n\n"
        "def put(key, value):\n    return store.save(key, value)\n"
    ),
    "tests/__init__.py": "",
    "tests/test_service.py": (
        "from pkg.service import get, put\n\n\n"
        'def test_get():\n    assert get("a") == {"k": "a"}\n\n\n'
        'def test_put():\n    assert put("a", 1)\n'
    ),
    "tests/test_store.py": (
        'from pkg import store\n\n\ndef test_fetch():\n    assert store.fetch("a")\n'
    ),
}

Indexed = tuple[Path, _StatefulIngestor, GraphUpdater]


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def indexed(temp_repo: Path) -> Indexed:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FILES.items():
        _write(root, rel, text)
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
    return root, store, updater


def _fetch(store: _StatefulIngestor) -> QueryFn:
    def fetch(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return store.fetch_all(query, dict(params) if params is not None else None)

    return fetch


def _observe(
    indexed: Indexed, changed: list[str], deleted: list[str] | None = None
) -> StructuralDelta:
    root, store, updater = indexed
    return observe(
        _fetch(store),
        PROJECT,
        [*changed, *(deleted or [])],
        lambda: updater.reingest(changed, deleted=deleted or []),
        repo_root=root,
    )


def _reaching(delta: StructuralDelta) -> dict[str, tuple[int, str]]:
    prefix = f"{PROJECT}."
    return {
        r["qualified_name"].removeprefix(prefix): (
            r["depth"],
            r["through"].removeprefix(prefix),
        )
        for r in delta["tests_reaching"]
    }


def test_a_renamed_symbols_tests_are_reached_through_its_dangling_caller(
    indexed: Indexed,
) -> None:
    root = indexed[0]
    _write(
        root, "pkg/store.py", STORE.replace("def fetch(key):", "def fetch_one(key):")
    )

    delta = _observe(indexed, ["pkg/store.py"])

    assert _reaching(delta)["tests.test_service.test_get"] == (2, "pkg.service.get")


def test_a_removed_symbols_tests_are_reached_through_its_dangling_caller(
    indexed: Indexed,
) -> None:
    root = indexed[0]
    _write(root, "pkg/store.py", "def save(key, value):\n    return True\n")

    delta = _observe(indexed, ["pkg/store.py"])

    assert _reaching(delta)["tests.test_service.test_get"] == (2, "pkg.service.get")


def test_a_test_that_called_the_renamed_symbol_itself_is_reached(
    indexed: Indexed,
) -> None:
    root = indexed[0]
    _write(
        root, "pkg/store.py", STORE.replace("def fetch(key):", "def fetch_one(key):")
    )

    delta = _observe(indexed, ["pkg/store.py"])

    assert _reaching(delta)["tests.test_store.test_fetch"] == (1, "pkg.store.fetch")


def test_a_deleted_files_tests_are_reached_through_its_dangling_callers(
    indexed: Indexed,
) -> None:
    root = indexed[0]
    (root / "pkg/store.py").unlink()

    delta = _observe(indexed, [], deleted=["pkg/store.py"])

    assert {
        "tests.test_service.test_get",
        "tests.test_service.test_put",
        "tests.test_store.test_fetch",
    } <= set(_reaching(delta))


# Negative: what must not change.


def test_a_body_edit_still_reaches_every_test_of_the_file(indexed: Indexed) -> None:
    root = indexed[0]
    _write(root, "pkg/store.py", STORE.replace('"k": key', '"key": key'))

    delta = _observe(indexed, ["pkg/store.py"])

    assert delta["dangling_callers"] == []
    assert _reaching(delta) == {
        "tests.test_store.test_fetch": (1, "pkg.store.fetch"),
        "tests.test_service.test_get": (2, "pkg.service.get"),
        "tests.test_service.test_put": (2, "pkg.service.put"),
    }


def test_a_rename_with_its_callers_updated_leaves_nothing_dangling(
    indexed: Indexed,
) -> None:
    root = indexed[0]
    _write(
        root, "pkg/store.py", STORE.replace("def fetch(key):", "def fetch_one(key):")
    )
    _write(
        root,
        "pkg/service.py",
        FILES["pkg/service.py"].replace("store.fetch(", "store.fetch_one("),
    )
    _write(
        root,
        "tests/test_store.py",
        FILES["tests/test_store.py"].replace("store.fetch(", "store.fetch_one("),
    )

    delta = _observe(indexed, ["pkg/store.py", "pkg/service.py", "tests/test_store.py"])

    assert delta["dangling_callers"] == []
    assert _reaching(delta)["tests.test_service.test_get"] == (1, "pkg.service.get")
