"""Issue #2639: `cgr check` does not fail on arity against a guessed callee.

A `heuristic` edge is a name-only guess, yet every CALLS edge out of an
edited file was judged: `options.pop("verbose", None)` on a dict, bound by
its last name segment to a no-parameter `tests/helpers.py::pop()`, read
`too_many` and a comment-only edit exited 1 (pallets/click: 7 of 29
findings). A finding also named no callee, so it could not be diagnosed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.graph_query import QueryFn
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import StructuralDelta, has_findings, observe
from codebase_rag.types_defs import PropertyParams, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "heur"

FILES = {
    "tests/__init__.py": "",
    "tests/helpers.py": "def pop():\n    return None\n",
    "lib.py": "def one(a):\n    return a\n",
    "app.py": (
        "from lib import one\n\n\n"
        'def take(options):\n    return options.pop("verbose", None)\n\n\n'
        "def wrong():\n    return one(1, 2)\n"
    ),
}

Indexed = tuple[Path, _StatefulIngestor, GraphUpdater]


def _fetch(store: _StatefulIngestor) -> QueryFn:
    def fetch(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return store.fetch_all(query, dict(params) if params is not None else None)

    return fetch


@pytest.fixture
def indexed(temp_repo: Path) -> Indexed:
    root = temp_repo / PROJECT
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
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


def _edit(indexed: Indexed, rel: str, old: str, new: str) -> StructuralDelta:
    root, store, updater = indexed
    path = root / rel
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))
    return observe(
        _fetch(store),
        PROJECT,
        [rel],
        lambda: updater.reingest([rel]),
        repo_root=root,
    )


def test_a_comment_only_edit_finds_nothing_against_a_guessed_callee(
    indexed: Indexed,
) -> None:
    delta = _edit(indexed, "app.py", "def take(options):", "# note\ndef take(options):")

    assert [f["line"] for f in delta["arity_findings"]] == [10]
    (finding,) = delta["arity_findings"]
    assert finding["callee"] == f"{PROJECT}.lib.one"


def test_a_finding_names_its_callee_and_how_it_was_bound(indexed: Indexed) -> None:
    delta = _edit(indexed, "app.py", "def take(options):", "# note\ndef take(options):")

    (finding,) = delta["arity_findings"]
    assert (finding["callee"], finding["resolution"]) == (
        f"{PROJECT}.lib.one",
        cs.EdgeResolution.EXACT.value,
    )


def test_a_guessed_site_of_a_changed_signature_is_unknown(indexed: Indexed) -> None:
    delta = _edit(indexed, "tests/helpers.py", "def pop():", "def pop(key):")

    (change,) = delta["signature_changes"]
    (site,) = change["sites"]
    assert site["resolution"] == cs.EdgeResolution.HEURISTIC.value
    assert site["verdict"] == cs.DELTA_ARITY_UNKNOWN
    assert not has_findings(delta)


# Negative: what must not change.


def test_an_exact_too_many_still_fails(indexed: Indexed) -> None:
    delta = _edit(indexed, "app.py", "one(1, 2)", "one(1, 2, 3)")

    verdicts = {f["line"]: f["verdict"] for f in delta["arity_findings"]}
    assert verdicts[9] == cs.DELTA_ARITY_TOO_MANY
    assert has_findings(delta)


def test_an_exact_site_of_a_changed_signature_keeps_its_verdict(
    indexed: Indexed,
) -> None:
    delta = _edit(indexed, "lib.py", "def one(a):", "def one():")

    (change,) = delta["signature_changes"]
    assert [s["verdict"] for s in change["sites"]] == [cs.DELTA_ARITY_TOO_MANY]
    assert has_findings(delta)


def test_the_site_query_names_each_column_once() -> None:
    # Memgraph refuses a RETURN that names two columns alike, so a second
    # `r.resolution AS resolution` failed every delta snapshot: `cgr rename`
    # committed its edit yet never re-ingested it, and the graph kept the old
    # name (test_edits_undo_graph_e2e). The in-memory store hid it.
    returned = cq.CYPHER_DELTA_SITES.split("RETURN", 1)[1]
    aliases = re.findall(r"\bAS\s+(\w+)", returned)

    assert cs.KEY_RESOLUTION in aliases
    assert len(aliases) == len(set(aliases))
