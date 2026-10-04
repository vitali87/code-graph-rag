"""Issues #2853 and #2845: `cgr check` sees a call the new signature rejects.

The signature capture is the positional names alone, so two breaking edits
read as hints that never fail `--fail-on-found`:

- #2853: a keyword a caller passes that the new signature does not accept
  (`host=` after `host` was renamed or removed, no `**kwargs`) is a certain
  `TypeError`, yet it read `ok` or `possibly_missing`.
- #2845: a parameter added WITHOUT a default leaves every caller short, a
  certain `TypeError`, yet it read `possibly_missing`, exactly as an
  optional one does.

The callee's header is read back from the syntax tree, which says which
parameters have defaults, which take keywords and whether `**kwargs` exists.
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

PROJECT = "checkkw"

LIB = (
    "def connect(host, port):\n    return (host, port)\n\n\n"
    "def send(msg):\n    return msg\n\n\n"
    "def opts(a, **kw):\n    return a\n\n\n"
    "def kwo(a, *, flag):\n    return a\n\n\n"
    "def posonly(a, b, /):\n    return a\n\n\n"
    "def pos_kw(a, /, **kw):\n    return a\n\n\n"
    "def wrap(f):\n    return f\n\n\n"
    "class Svc:\n"
    "    def run(self, job):\n        return job\n\n"
    "    @staticmethod\n"
    "    def util(x):\n        return x\n\n"
    "    @classmethod\n"
    "    def make(cls, x):\n        return x\n"
)
APP = (
    "from lib import Svc, connect, kwo, opts, pos_kw, posonly, send\n\n\n"
    'def call_connect():\n    return connect(host="a", port=80)\n\n\n'
    'def call_send():\n    return send("hi")\n\n\n'
    "def call_send_splat(params):\n    return send(**params)\n\n\n"
    "def call_opts():\n    return opts(1, anything=2)\n\n\n"
    "def call_kwo():\n    return kwo(1, flag=True)\n\n\n"
    "def call_posonly():\n    return posonly(1, 2)\n\n\n"
    "def call_run():\n    return Svc().run(1)\n\n\n"
    "def call_util():\n    return Svc.util(1)\n\n\n"
    "def call_make():\n    return Svc.make(1)\n\n\n"
    "def call_pos_kw():\n    return pos_kw(1, b=2)\n\n\n"
    "def call_run_by_keyword(svc):\n    return Svc.run(self=svc, job=1)\n"
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


# The caller in app.py whose site each test reads.
CALLER = {
    "connect": "call_connect",
    "send": "call_send",
    "opts": "call_opts",
    "kwo": "call_kwo",
    "posonly": "call_posonly",
    "Svc.run": "call_run",
    "Svc.util": "call_util",
    "Svc.make": "call_make",
    "pos_kw": "call_pos_kw",
}


def _edit_app(indexed: Indexed, old: str, new: str) -> StructuralDelta:
    # A caller edited to call wrongly: its sites are judged as
    # `arity_findings`, whatever the callee's history.
    root, store, updater = indexed
    path = root / "app.py"
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))
    return observe(
        _fetch(store),
        PROJECT,
        ["app.py"],
        lambda: updater.reingest(["app.py"]),
        repo_root=root,
    )


def _findings(delta: StructuralDelta) -> dict[int, str]:
    return {f["line"] or 0: f["verdict"] for f in delta["arity_findings"]}


def _verdict(delta: StructuralDelta, callee: str) -> str:
    (change,) = [
        c
        for c in delta["signature_changes"]
        if c["qualified_name"] == f"{PROJECT}.lib.{callee}"
    ]
    (site,) = [
        s for s in change["sites"] if s["caller"] == f"{PROJECT}.app.{CALLER[callee]}"
    ]
    return site["verdict"]


@pytest.mark.parametrize(
    ("old", "new", "callee", "verdict"),
    [
        (
            "def connect(host, port):\n    return (host, port)",
            "def connect(hostname, port):\n    return (hostname, port)",
            "connect",
            cs.DELTA_ARITY_UNEXPECTED_KEYWORD,
        ),
        (
            "def connect(host, port):\n    return (host, port)",
            "def connect(port):\n    return port",
            "connect",
            cs.DELTA_ARITY_UNEXPECTED_KEYWORD,
        ),
        ("def send(msg):", "def send(msg, channel):", "send", cs.DELTA_ARITY_TOO_FEW),
        (
            "def run(self, job):",
            "def run(self, job, prio):",
            "Svc.run",
            cs.DELTA_ARITY_TOO_FEW,
        ),
    ],
    ids=[
        "renamed-keyword",
        "removed-keyword",
        "required-positional-added",
        "required-added-to-a-method",
    ],
)
def test_a_call_the_new_signature_rejects_is_a_definite_finding(
    indexed: Indexed, old: str, new: str, callee: str, verdict: str
) -> None:
    delta = _edit(indexed, old, new)

    assert _verdict(delta, callee) == verdict
    assert has_findings(delta)


@pytest.mark.parametrize(
    ("old", "new", "line", "verdict"),
    [
        (
            'connect(host="a", port=80)',
            'connect(hst="a", port=80)',
            5,
            cs.DELTA_ARITY_UNEXPECTED_KEYWORD,
        ),
        ("kwo(1, flag=True)", "kwo(1)", 21, cs.DELTA_ARITY_TOO_FEW),
        ("posonly(1, 2)", "posonly(1, b=2)", 25, cs.DELTA_ARITY_UNEXPECTED_KEYWORD),
    ],
    ids=["misspelled-keyword", "missing-keyword-only", "positional-only-by-keyword"],
)
def test_an_edited_call_the_signature_rejects_is_an_arity_finding(
    indexed: Indexed, old: str, new: str, line: int, verdict: str
) -> None:
    delta = _edit_app(indexed, old, new)

    assert _findings(delta).get(line) == verdict
    assert has_findings(delta)


def test_a_keyword_naming_a_positional_only_parameter_does_not_fill_it(
    indexed: Indexed,
) -> None:
    # `pos_kw(1, b=2)`: once `b` is positional-only, the keyword lands in
    # `**kw` and `b` is missing (Greptile, PR #2947).
    delta = _edit(indexed, "def pos_kw(a, /, **kw):", "def pos_kw(a, b, /, **kw):")

    assert _verdict(delta, "pos_kw") == cs.DELTA_ARITY_TOO_FEW
    assert has_findings(delta)


# Negative: what must not change.


def test_an_edited_caller_that_calls_correctly_has_no_finding(
    indexed: Indexed,
) -> None:
    delta = _edit_app(indexed, "opts(1, anything=2)", "opts(1, anything=3)")

    assert delta["arity_findings"] == []


@pytest.mark.parametrize(
    ("old", "new", "callee"),
    [
        ("def send(msg):", "def send(msg, channel=None):", "send"),
        ("def opts(a, **kw):", "def opts(a, b=1, **kw):", "opts"),
        (
            "def connect(host, port):\n    return (host, port)",
            "def connect(port, *, host):\n    return (host, port)",
            "connect",
        ),
        ("def util(x):", "def util(x, y=0):", "Svc.util"),
        ("def make(cls, x):", "def make(cls, x, y=0):", "Svc.make"),
        ("def send(msg):", "@wrap\ndef send(msg, channel):", "send"),
    ],
    ids=[
        "optional-added",
        "kwargs-takes-any-keyword",
        "keyword-only-keeps-the-name",
        "staticmethod-has-no-receiver",
        "classmethod-binds-cls",
        "unknown-decorator-is-not-judged",
    ],
)
def test_a_call_the_new_signature_accepts_is_no_finding(
    indexed: Indexed, old: str, new: str, callee: str
) -> None:
    delta = _edit(indexed, old, new)

    assert _verdict(delta, callee) not in (
        cs.DELTA_ARITY_UNEXPECTED_KEYWORD,
        cs.DELTA_ARITY_TOO_FEW,
        cs.DELTA_ARITY_TOO_MANY,
    )
    assert not has_findings(delta)


def test_a_keyword_splat_may_supply_the_new_required_parameter(
    indexed: Indexed,
) -> None:
    delta = _edit(indexed, "def send(msg):", "def send(msg, channel):")

    (change,) = delta["signature_changes"]
    verdicts = {site["line"]: site["verdict"] for site in change["sites"]}
    # `send(**params)` on line 13 may carry `channel`; only the plain
    # `send("hi")` on line 9 is certainly short.
    assert verdicts[13] != cs.DELTA_ARITY_TOO_FEW
    assert verdicts[9] == cs.DELTA_ARITY_TOO_FEW


def test_a_receiver_passed_by_keyword_is_an_unbound_call(indexed: Indexed) -> None:
    # `Svc.run(self=svc, job=1)` supplies the receiver itself; an optional
    # parameter added to `run` breaks nothing (Greptile, PR #2947).
    delta = _edit(indexed, "def run(self, job):", "def run(self, job, prio=0):")

    (change,) = delta["signature_changes"]
    verdicts = {site["caller"]: site["verdict"] for site in change["sites"]}
    assert verdicts[f"{PROJECT}.app.call_run_by_keyword"] not in (
        cs.DELTA_ARITY_UNEXPECTED_KEYWORD,
        cs.DELTA_ARITY_TOO_FEW,
        cs.DELTA_ARITY_TOO_MANY,
    )
    assert not has_findings(delta)
