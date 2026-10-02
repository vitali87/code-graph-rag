"""Issue #2635: a `*`/`**` unpacking at a call site is not a positional argument.

`call_site_properties` counted every argument that was not `name=value` as
a positional one, the `**opts` and `*rest` unpackings included, and the
structural delta's arity check read `arg_count - len(kwarg_names)` as the
positionals passed. `send(req, **opts)` against `def send(request, **kwargs)`
then passed "two" positionals to a one-parameter callee: `too_many`, the
verdict that trips `cgr check --fail-on-found`, on correct code that runs.

`**opts` supplies keywords only. `*rest` supplies an unknown number of
positionals, so the ones written beside it are a lower bound: enough to
prove too many, never enough to prove a fit.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tree_sitter import Node
from typer.testing import CliRunner, Result

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.call_processor import call_site_properties
from codebase_rag.services.graph_diff import _SITE_PROPS
from codebase_rag.structural_check import run_check
from codebase_rag.structural_delta import StructuralDelta, has_findings, observe
from codebase_rag.types_defs import PropertyDict
from evals.cgr_graph import _StatefulIngestor

PROJECT = "unpack"

LIB = (
    "def send(request, **kwargs):\n"
    "    return request\n"
    "\n"
    "\n"
    "def dispatch_hook(key, hooks, hook_data, **kwargs):\n"
    "    return hook_data\n"
    "\n"
    "\n"
    "def one(a):\n"
    "    return a\n"
    "\n"
    "\n"
    "def nothing():\n"
    "    return None\n"
    "\n"
    "\n"
    "def spread(a, *more):\n"
    "    return a\n"
)

# `{call}` sits in a function, `{method_call}` in a method reaching its own
# class's `post` through `self`, the two shapes the issue reports.
APP = (
    "from lib import dispatch_hook, nothing, one, send, spread\n"
    "\n"
    "\n"
    "def forward(req, opts, rest, hooks, r):\n"
    "    return {call}\n"
    "\n"
    "\n"
    "class Client:\n"
    "    def post(self, request, **kwargs):\n"
    "        return request\n"
    "\n"
    "    def go(self, opts, rest):\n"
    "        return {method_call}\n"
)

NEUTRAL_CALL = "one(req)"
NEUTRAL_METHOD_CALL = "self.post(1)"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _app(call: str = NEUTRAL_CALL, method_call: str = NEUTRAL_METHOD_CALL) -> str:
    return APP.format(call=call, method_call=method_call)


def _index(
    root: Path, call: str = NEUTRAL_CALL
) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    """Commit `lib.py` and `app.py` calling `call`, and index the commit."""
    root.mkdir()
    (root / "lib.py").write_text(LIB)
    (root / "app.py").write_text(_app(call=call))
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return root, store, updater


@pytest.fixture
def indexed(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    return _index(temp_repo / PROJECT)


def _observe(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater], changed: list[str]
) -> StructuralDelta:
    root, store, updater = indexed
    return observe(
        store.fetch_all,
        PROJECT,
        changed,
        lambda: updater.reingest(changed, deleted=[]),
        repo_root=root,
    )


def _runs(source: str) -> bool:
    """CPython's own answer: does the call go through with empty splats?

    `*rest` is `[]` and `**opts` is `{}`, the emptiest each can be, so a
    call that still raises passes too many whatever they hold, and one that
    runs is a call the graph cannot call wrong.
    """
    namespace: dict[str, object] = {}
    exec(LIB, namespace)
    exec(source, namespace)
    probe = namespace["probe"]
    assert callable(probe)
    try:
        probe()
    except TypeError:
        return False
    return True


def _function_runs(call: str) -> bool:
    return _runs(
        "def probe():\n"
        "    req, opts, rest, hooks, r = 1, {}, [], None, 2\n"
        f"    return {call}\n"
    )


def _method_runs(call: str) -> bool:
    return _runs(
        "class Client:\n"
        "    def post(self, request, **kwargs):\n"
        "        return request\n"
        "\n"
        "    def go(self, opts, rest):\n"
        f"        return {call}\n"
        "\n"
        "\n"
        "def probe():\n"
        "    return Client().go({}, [])\n"
    )


# --- red: the reported false positives ----------------------------------------


FUNCTION_CALLS_THAT_FIT = [
    pytest.param("send(req, **opts)", id="kwargs-pass-through"),
    # psf/requests sessions.py:791, one of the four rows in the issue.
    pytest.param('dispatch_hook("response", hooks, r, **opts)', id="requests-hook"),
    pytest.param("send(req, stream=True, **opts)", id="keyword-and-kwargs"),
    pytest.param("one(req, *rest)", id="star-args-beside-a-fit"),
    pytest.param("nothing(*rest)", id="star-args-alone"),
    pytest.param("send(req, *rest, **opts)", id="both-unpackings"),
]


@pytest.mark.parametrize("call", FUNCTION_CALLS_THAT_FIT)
def test_an_unpacking_is_not_counted_as_a_positional(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater], call: str
) -> None:
    root, _store, _updater = indexed
    assert _function_runs(call)
    (root / "app.py").write_text(_app(call=call))

    delta = _observe(indexed, ["app.py"])

    assert delta["arity_findings"] == []
    assert not has_findings(delta)


def test_a_kwargs_pass_through_on_self_is_not_too_many(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, _store, _updater = indexed
    call = "self.post(1, **opts)"
    assert _method_runs(call)
    (root / "app.py").write_text(_app(method_call=call))

    delta = _observe(indexed, ["app.py"])

    assert delta["arity_findings"] == []
    assert not has_findings(delta)


def _check(root: Path, store: _StatefulIngestor) -> Result:
    cli_store = MagicMock(wraps=store)
    cli_store.list_projects = MagicMock(return_value=[PROJECT])
    context = MagicMock()
    context.__enter__.return_value = cli_store
    context.__exit__.return_value = False
    with patch("codebase_rag.cli.connect_memgraph", return_value=context):
        return CliRunner().invoke(
            app,
            [
                "check",
                "--repo-path",
                str(root),
                "--project",
                PROJECT,
                "--fail-on-found",
            ],
        )


def test_the_issue_repro_passes_the_gate(temp_repo: Path) -> None:
    """The issue's steps: index, add a comment, `cgr check --fail-on-found`."""
    root, store, _updater = _index(temp_repo / PROJECT, "send(req, **opts)")
    with (root / "app.py").open("a") as handle:
        handle.write("# comment\n")

    result = _check(root, store)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["arity_findings"] == []


def test_a_positional_passed_in_place_of_the_unpacking_still_fails_the_gate(
    temp_repo: Path,
) -> None:
    """`run_check` is what `cgr check` runs, with a fresh updater as there.

    Called directly: the CLI tests hand the store over in a MagicMock, which
    `GraphUpdater` does not accept as a graph it can read, so a re-ingest
    under the CLI leaves the indexed `**opts` edge in place.
    """
    root, store, _updater = _index(temp_repo / PROJECT, "send(req, **opts)")
    assert not _function_runs("send(req, opts)")
    (root / "app.py").write_text(_app(call="send(req, opts)"))
    parsers, queries = load_parsers()

    delta = run_check(root, "HEAD", PROJECT, store, parsers, queries)

    (finding,) = delta["arity_findings"]
    assert finding["verdict"] == cs.DELTA_ARITY_TOO_MANY
    assert finding["arg_count"] == 2
    assert has_findings(delta)


# --- negative: what must still be too_many -----------------------------------


FUNCTION_CALLS_THAT_DO_NOT_FIT = [
    # The issue's own negative: two positionals for one parameter.
    pytest.param("one(req, r)", id="plain-surplus"),
    # The written positionals are a floor `*rest` only adds to: CPython
    # rejects `one(1, 2, *[])`.
    pytest.param("one(req, r, *rest)", id="surplus-beside-star-args"),
    pytest.param("nothing(req, *rest)", id="star-args-after-a-surplus"),
    # `**opts` adds no positional, and takes none away either.
    pytest.param("one(req, r, **opts)", id="surplus-beside-kwargs"),
    pytest.param("send(req, r, **opts)", id="surplus-to-a-kwargs-callee"),
]


@pytest.mark.parametrize("call", FUNCTION_CALLS_THAT_DO_NOT_FIT)
def test_too_many_written_positionals_are_still_too_many(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater], call: str
) -> None:
    root, _store, _updater = indexed
    assert not _function_runs(call)
    (root / "app.py").write_text(_app(call=call))

    delta = _observe(indexed, ["app.py"])

    assert [f["verdict"] for f in delta["arity_findings"]] == [cs.DELTA_ARITY_TOO_MANY]
    assert has_findings(delta)


def test_a_surplus_on_self_beside_kwargs_is_still_too_many(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, _store, _updater = indexed
    call = "self.post(1, 2, **opts)"
    assert not _method_runs(call)
    (root / "app.py").write_text(_app(method_call=call))

    delta = _observe(indexed, ["app.py"])

    assert [f["verdict"] for f in delta["arity_findings"]] == [cs.DELTA_ARITY_TOO_MANY]


@pytest.mark.parametrize(
    "call",
    [
        pytest.param("spread(req, r, *rest)", id="variadic-callee"),
        pytest.param("send(req, timeout=1)", id="named-keyword-only"),
    ],
)
def test_calls_that_already_fit_stay_clean(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater], call: str
) -> None:
    root, _store, _updater = indexed
    assert _function_runs(call)
    (root / "app.py").write_text(_app(call=call))

    delta = _observe(indexed, ["app.py"])

    assert delta["arity_findings"] == []


def test_named_keywords_on_self_stay_clean(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    """The case the issue says was already right: `self.send(1, stream=True)`."""
    root, _store, _updater = indexed
    call = "self.post(1, stream=True)"
    assert _method_runs(call)
    (root / "app.py").write_text(_app(method_call=call))

    delta = _observe(indexed, ["app.py"])

    assert delta["arity_findings"] == []


# --- signature changes: every site gets a verdict ----------------------------


def _site_verdicts(delta: StructuralDelta, qn_suffix: str) -> list[str]:
    return [
        site["verdict"]
        for change in delta["signature_changes"]
        if change["qualified_name"].endswith(qn_suffix)
        for site in change["sites"]
    ]


def _signature_change(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
    before: str,
    after: str,
    call: str,
) -> StructuralDelta:
    """`one`'s header goes from `before` to `after`; `call` is its caller.

    The caller and the old header are indexed first, so the header is the
    only edit the delta sees.
    """
    root, _store, updater = indexed
    (root / "lib.py").write_text(LIB.replace("def one(a):", before))
    (root / "app.py").write_text(_app(call=call))
    updater.reingest(["lib.py", "app.py"], deleted=[])
    (root / "lib.py").write_text(LIB.replace("def one(a):", after))
    return _observe(indexed, ["lib.py"])


def test_a_dropped_parameter_a_kwargs_site_never_filled_is_ok(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # `one(req, **opts)` passes one positional, which still fits `one(a)`.
    # Counted as two it was `too_many`, and the signature change failed the
    # gate.
    assert _function_runs("one(req, **opts)")
    delta = _signature_change(
        indexed, "def one(a, b):", "def one(a):", "one(req, **opts)"
    )

    assert _site_verdicts(delta, ".lib.one") == [cs.DELTA_ARITY_OK]
    assert not has_findings(delta)


def test_a_star_args_site_reads_unknown_when_its_floor_fits(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # `one()` called as `one(*rest)` raises only when `rest` is not empty,
    # which the graph cannot see.
    delta = _signature_change(indexed, "def one(a):", "def one():", "one(*rest)")

    assert _site_verdicts(delta, ".lib.one") == [cs.DELTA_ARITY_UNKNOWN]
    assert not has_findings(delta)


def test_a_star_args_site_is_never_ok_by_its_count_alone(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # `one(req, *rest)` fills `one(a)` exactly only while `rest` is empty:
    # the written count fits, and that is not enough to say `ok`.
    delta = _signature_change(
        indexed, "def one(a, b):", "def one(a):", "one(req, *rest)"
    )

    assert _site_verdicts(delta, ".lib.one") == [cs.DELTA_ARITY_UNKNOWN]
    assert not has_findings(delta)


def test_a_dropped_parameter_a_plain_site_passes_is_still_too_many(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    delta = _signature_change(indexed, "def one(a):", "def one():", "one(req)")

    assert _site_verdicts(delta, ".lib.one") == [cs.DELTA_ARITY_TOO_MANY]
    assert has_findings(delta)


def test_a_floor_over_the_new_signature_is_still_too_many(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    delta = _signature_change(indexed, "def one(a):", "def one():", "one(req, *rest)")

    assert _site_verdicts(delta, ".lib.one") == [cs.DELTA_ARITY_TOO_MANY]
    assert has_findings(delta)


# --- the recorded site ---------------------------------------------------------


def _python_calls(source: str) -> dict[str, Node]:
    parsers, _queries = load_parsers()
    tree = parsers[cs.SupportedLanguage.PYTHON].parse(source.encode())
    found: dict[str, Node] = {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PY_CALL and node.text is not None:
            found[node.text.decode()] = node
        stack.extend(node.named_children)
    return found


def _props(call: str) -> PropertyDict:
    return call_site_properties(_python_calls(f"{call}\n")[call])


@pytest.mark.parametrize(
    ("call", "arg_count", "kwarg_names", "star_args", "star_kwargs"),
    [
        pytest.param("send(req, **opts)", 1, [], None, True, id="kwargs"),
        pytest.param("one(*rest)", 0, [], True, None, id="star-args"),
        pytest.param(
            "f(1, *a, b, k=2, **o)", 3, ["k"], True, True, id="every-kind-mixed"
        ),
        pytest.param("f(*a, *b, **c, **d)", 0, [], True, True, id="repeated"),
        # No unpacking: the site is what it always was, with neither flag.
        pytest.param("one(req)", 1, [], None, None, id="plain"),
        pytest.param("send(req, k=1)", 2, ["k"], None, None, id="keyword"),
    ],
)
def test_the_site_records_unpackings_apart_from_the_count(
    call: str,
    arg_count: int,
    kwarg_names: list[str],
    star_args: bool | None,
    star_kwargs: bool | None,
) -> None:
    props = _props(call)

    assert props[cs.KEY_ARG_COUNT] == arg_count
    assert props[cs.KEY_KWARG_NAMES] == kwarg_names
    assert props.get(cs.KEY_STAR_ARGS) is star_args
    assert props.get(cs.KEY_STAR_KWARGS) is star_kwargs


def test_the_delta_reads_the_flag_back_from_the_graph() -> None:
    assert "r.star_args AS star_args" in cq.CYPHER_DELTA_SITES


def test_the_flags_are_location_not_structure() -> None:
    """`cgr diff` must not report a changed relationship when only the
    argument shape at a site moved, as for `arg_count` (#1522)."""
    assert {cs.KEY_STAR_ARGS, cs.KEY_STAR_KWARGS} <= _SITE_PROPS


def test_a_callable_passed_beside_an_unpacking_is_still_referenced(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    """The callback passes read the same argument split; keeping the
    unpacking out of the COUNT must not move `one` out of its slot."""
    root, store, updater = indexed
    (root / "app.py").write_text(_app(call="spread(one, *rest, **opts)"))
    updater.reingest(["app.py"], deleted=[])

    referenced = {
        (str(edge[1]), str(edge[4]))
        for edge in store.edge_props
        if edge[2] in (cs.RelationshipType.REFERENCES, cs.RelationshipType.CALLS)
    }
    assert (f"{PROJECT}.app.forward", f"{PROJECT}.lib.one") in referenced
