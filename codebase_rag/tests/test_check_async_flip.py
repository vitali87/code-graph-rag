"""Issue #2860: `cgr check` sees a Python `def` <-> `async def` flip.

The shape fingerprint drops the `async` keyword and the positional names do
not change, so `def fetch()` becoming `async def fetch()` (or back) was not
even in `symbols.changed`, and `--fail-on-found` exited 0. Yet it breaks
every caller: a sync call now gets a coroutine whose body never runs, and
`await fetch()` on a now-sync function awaits a non-awaitable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_query import QueryFn
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import StructuralDelta, has_findings, observe
from codebase_rag.types_defs import PropertyParams, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "chkasync"

LIB = (
    "def fetch():\n    return 1\n\n\n"
    "async def afetch():\n    return 1\n\n\n"
    "async def load(key):\n    return key\n\n\n"
    "class Svc:\n"
    "    def run(self):\n        return 1\n"
)
APP = (
    "from lib import Svc, afetch, fetch, load\n\n\n"
    "def use():\n    return fetch() + 1\n\n\n"
    "async def ause():\n    return await afetch()\n\n\n"
    "async def aload():\n    return await load(1)\n\n\n"
    "def use_svc():\n    return Svc().run() + 1\n"
)

Indexed = tuple[Path, _StatefulIngestor, GraphUpdater]


def _fetch(store: _StatefulIngestor) -> QueryFn:
    def fetch(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return store.fetch_all(query, dict(params) if params is not None else None)

    return fetch


@pytest.fixture
def indexed(temp_repo: Path) -> Indexed:
    root = temp_repo / PROJECT
    root.mkdir()
    (root / "lib.py").write_text(LIB)
    (root / "app.py").write_text(APP)
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


def _edit(indexed: Indexed, old: str, new: str) -> StructuralDelta:
    root, store, updater = indexed
    path = root / "lib.py"
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))
    return observe(
        _fetch(store),
        PROJECT,
        ["lib.py"],
        lambda: updater.reingest(["lib.py"]),
        repo_root=root,
    )


def _edit_both(
    indexed: Indexed, lib: tuple[str, str], app: tuple[str, str]
) -> StructuralDelta:
    # The callee and its caller edited together: a caller migrated with it.
    root, store, updater = indexed
    for name, (old, new) in (("lib.py", lib), ("app.py", app)):
        path = root / name
        text = path.read_text()
        assert old in text, old
        path.write_text(text.replace(old, new))
    return observe(
        _fetch(store),
        PROJECT,
        ["lib.py", "app.py"],
        lambda: updater.reingest(["lib.py", "app.py"]),
        repo_root=root,
    )


def _qn(name: str) -> str:
    return f"{PROJECT}.lib.{name}"


@pytest.mark.parametrize(
    ("old", "new", "callee", "caller"),
    [
        ("def fetch():", "async def fetch():", "fetch", "use"),
        ("async def afetch():", "def afetch():", "afetch", "ause"),
        ("    def run(self):", "    async def run(self):", "Svc.run", "use_svc"),
    ],
    ids=["sync-to-async", "async-to-sync", "method-to-async"],
)
def test_an_async_flip_is_a_definite_finding_at_every_call(
    indexed: Indexed, old: str, new: str, callee: str, caller: str
) -> None:
    delta = _edit(indexed, old, new)

    assert _qn(callee) in delta["symbols"]["changed"]
    (change,) = [
        c for c in delta["signature_changes"] if c["qualified_name"] == _qn(callee)
    ]
    assert change["async_change"] == (
        cs.DELTA_ASYNC_ADDED
        if new.lstrip().startswith("async")
        else cs.DELTA_ASYNC_REMOVED
    )
    verdicts = {site["caller"]: site["verdict"] for site in change["sites"]}
    assert verdicts[f"{PROJECT}.app.{caller}"] == cs.DELTA_ARITY_ASYNC_CHANGED
    assert has_findings(delta)


@pytest.mark.parametrize(
    ("lib", "app"),
    [
        (
            ("def fetch():", "async def fetch():"),
            (
                "def use():\n    return fetch() + 1",
                "async def use():\n    return await fetch() + 1",
            ),
        ),
        (
            ("async def afetch():", "def afetch():"),
            ("return await afetch()", "return afetch()"),
        ),
    ],
    ids=["awaited-once-async", "plain-once-sync"],
)
def test_a_caller_migrated_with_the_flip_has_no_finding(
    indexed: Indexed, lib: tuple[str, str], app: tuple[str, str]
) -> None:
    # Greptile, PR #2951: the verdict reads how each call is written now.
    delta = _edit_both(indexed, lib, app)

    # `use` itself turns async in the first case: read the callee's change.
    callee = lib[0].split("def ", 1)[1].split("(", 1)[0]
    (change,) = [
        c for c in delta["signature_changes"] if c["qualified_name"] == _qn(callee)
    ]
    assert cs.DELTA_ARITY_ASYNC_CHANGED not in {s["verdict"] for s in change["sites"]}
    assert not has_findings(delta)


def test_a_coroutine_handed_on_is_a_hint_not_a_finding(indexed: Indexed) -> None:
    # `asyncio.run(fetch())` and `return fetch()` hand the coroutine on;
    # whether it is awaited is up to the code it reaches.
    delta = _edit_both(
        indexed,
        ("def fetch():", "async def fetch():"),
        (
            "from lib import Svc, afetch, fetch, load\n\n\ndef use():\n"
            "    return fetch() + 1",
            "import asyncio\n\nfrom lib import Svc, afetch, fetch, load\n\n\n"
            "def use():\n    return asyncio.run(fetch())",
        ),
    )

    (change,) = delta["signature_changes"]
    assert change["async_change"] == cs.DELTA_ASYNC_ADDED
    assert cs.DELTA_ARITY_ASYNC_CHANGED not in {s["verdict"] for s in change["sites"]}
    assert not has_findings(delta)


# Negative: what must not change.


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("def fetch():\n    return 1", "def fetch():\n    return 2"),
        ("async def afetch():\n    return 1", "async def afetch():\n    return 2"),
    ],
    ids=["sync-body-edit", "async-body-edit"],
)
def test_a_body_edit_keeps_its_async_ness_and_finds_nothing(
    indexed: Indexed, old: str, new: str
) -> None:
    delta = _edit(indexed, old, new)

    assert delta["signature_changes"] == []
    assert not has_findings(delta)


def test_an_optional_parameter_on_an_async_function_is_still_a_hint(
    indexed: Indexed,
) -> None:
    delta = _edit(indexed, "async def load(key):", "async def load(key, ttl=0):")

    (change,) = delta["signature_changes"]
    assert [site["verdict"] for site in change["sites"]] == [
        cs.DELTA_ARITY_POSSIBLY_MISSING
    ]
    assert not has_findings(delta)


def test_a_js_flip_is_listed_but_not_judged(temp_repo: Path) -> None:
    # A JS caller of a now-async function still runs it and gets a Promise;
    # whether that breaks depends on what the caller does with the value.
    root = temp_repo / PROJECT
    root.mkdir()
    (root / "lib.js").write_text("export function load() {\n  return 1;\n}\n")
    (root / "app.js").write_text(
        "import { load } from './lib.js';\n\nexport function use() {\n  load();\n}\n"
    )
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
    (root / "lib.js").write_text("export async function load() {\n  return 1;\n}\n")

    delta = observe(
        _fetch(store),
        PROJECT,
        ["lib.js"],
        lambda: updater.reingest(["lib.js"]),
        repo_root=root,
    )

    (change,) = delta["signature_changes"]
    assert change["async_change"] == cs.DELTA_ASYNC_ADDED
    assert cs.DELTA_ARITY_ASYNC_CHANGED not in {s["verdict"] for s in change["sites"]}
    assert not has_findings(delta)
