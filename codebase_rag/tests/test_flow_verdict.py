"""Three-verdict flow reachability (issue #1050): FOUND returns the path,
NO_FLOW asserts full coverage, UNKNOWN names the coverage gaps so an absent
path is never read as a verified absence."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.capture import ALL_ENABLED
from codebase_rag.cypher_queries import CYPHER_LIST_PROJECTS
from codebase_rag.flow_verdict import (
    CYPHER_FLOW_COVERAGE_GAPS,
    CYPHER_FLOW_EDGES,
    CYPHER_FLOW_REMOTE_EDGES,
    CYPHER_FLOW_WRITE_EDGES,
    FLOW_VERDICT_FOUND,
    FLOW_VERDICT_NO_FLOW,
    FLOW_VERDICT_UNKNOWN,
    flow_reachability_verdict,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _query_fn(
    edges: list[tuple[str, ...]],
    gaps: list[str] | dict[str, list[str]],
    remote: list[tuple[str, str]] | None = None,
    other_edges: dict[str, list[tuple[str, ...]]] | None = None,
    calls: list[tuple[str, dict | None]] | None = None,
    projects: list[str] | None = None,
    writes: dict[str, list[tuple[str, str]]] | None = None,
):
    """Flow edges are (source, target), or (source, target, scope) for a
    resource-to-resource flow; `remote` rows are (source, target) with the
    handler's project read off its name, as the graph has it; `other_edges`
    holds the flow edges of
    every project other than the asked one, keyed by project name; `gaps`
    is the asked project's list, or one per project; `calls` records every
    (query, params) issued; `writes` holds each project's (function,
    resource) WRITES_TO rows, as its prefix query returns them."""

    def fetch_all(query: str, params=None):
        if calls is not None:
            calls.append((query, params))
        project = params["project_name"] if params else None
        if query == CYPHER_FLOW_EDGES:
            rows = (
                other_edges[project]
                if other_edges and project in other_edges
                else edges
            )
            return [
                {"source": e[0], "target": e[1], "scope": e[2] if len(e) > 2 else None}
                for e in rows
            ]
        if query == CYPHER_FLOW_WRITE_EDGES:
            return [
                {"source": f, "target": r}
                for f, r in (writes or {}).get(project or "", [])
            ]
        if query == CYPHER_FLOW_COVERAGE_GAPS:
            if isinstance(gaps, dict):
                return [{"path": p} for p in gaps.get(project or "", [])]
            return [{"path": p} for p in gaps] if project == "p" else []
        if query == CYPHER_FLOW_REMOTE_EDGES:
            return [{"source": s, "target": t} for s, t in remote or []]
        if query == CYPHER_LIST_PROJECTS and projects is not None:
            return [{"name": name} for name in projects]
        raise AssertionError(query)

    return fetch_all


def test_found_returns_the_path() -> None:
    result = flow_reachability_verdict(
        _query_fn(
            [("p.a.src", "p.a.mid"), ("p.a.mid", "p.a.sink"), ("p.a.mid", "p.a.x")],
            ["uncovered.php"],
        ),
        "p",
        "p.a.src",
        "p.a.sink",
    )
    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == ("p.a.src", "p.a.mid", "p.a.sink")
    assert result.gaps == ()


def test_a_network_resource_continues_into_the_handler_of_another_project() -> None:
    """A client's NETWORK resource resolves to an endpoint another project's
    handler exposes; the walk continues into that handler and along that
    project's own flow edges, and names the hop that crossed the boundary
    (issue #1603). Before, the path ended at the resource: NO_FLOW with
    full coverage, a verified absence that was not one."""
    remote = [("resource::NETWORK::http://q:8000/items", "q.api.handler")]
    result = flow_reachability_verdict(
        _query_fn(
            [("p.a.src", "resource::NETWORK::http://q:8000/items")],
            [],
            remote=remote,
            other_edges={"q": [("q.api.handler", "q.db.sink")]},
        ),
        "p",
        "p.a.src",
        "q.db.sink",
    )
    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (
        "p.a.src",
        "resource::NETWORK::http://q:8000/items",
        "q.api.handler",
        "q.db.sink",
    )
    assert result.remote_hops == (
        ("resource::NETWORK::http://q:8000/items", "q.api.handler"),
    )


def test_an_rpc_resource_continues_into_its_handler_directly() -> None:
    """RPC and dispatch resources are exposed by their handler with no
    RESOLVES_TO in between, so the resource itself is the hop's source; the
    resource node carries no project, so the handler's is read off its name
    and that project's edges are loaded (local review P1)."""
    result = flow_reachability_verdict(
        _query_fn(
            [("p.a.src", "resource::RPC::Greeter.Hello")],
            [],
            remote=[("resource::RPC::Greeter.Hello", "q.server.greet")],
            other_edges={"q": [("q.server.greet", "q.db.sink")]},
        ),
        "p",
        "p.a.src",
        "q.db.sink",
    )
    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (
        "p.a.src",
        "resource::RPC::Greeter.Hello",
        "q.server.greet",
        "q.db.sink",
    )
    assert result.remote_hops == (("resource::RPC::Greeter.Hello", "q.server.greet"),)


def test_a_path_inside_one_service_has_no_remote_hops() -> None:
    """A purely local path reports no hops, and reads no other project: an
    unrelated hop into `q` exists in the graph, and `q`'s edges are loaded
    only when the walk reaches it (bot review on PR #1978)."""
    calls: list[tuple[str, dict | None]] = []
    result = flow_reachability_verdict(
        _query_fn(
            [("p.a.src", "p.a.sink")],
            [],
            remote=[("p.other.net", "q.api.handler")],
            other_edges={"q": []},
            calls=calls,
        ),
        "p",
        "p.a.src",
        "p.a.sink",
    )
    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.remote_hops == ()
    edge_reads = [params for query, params in calls if query == CYPHER_FLOW_EDGES]
    assert [p["project_name"] for p in edge_reads if p] == ["p"]


def test_a_service_calling_itself_is_not_a_boundary() -> None:
    """A NETWORK resource resolving to a handler of the SAME project is
    followed like any hop, but it is not remote: the project before the
    resource and the handler's are one (bot review on PR #1978)."""
    result = flow_reachability_verdict(
        _query_fn(
            [
                ("p.a.src", "resource::NETWORK::http://q:8000/items"),
                ("p.api.handler", "p.db.sink"),
            ],
            [],
            remote=[("resource::NETWORK::http://q:8000/items", "p.api.handler")],
        ),
        "p",
        "p.a.src",
        "p.db.sink",
    )
    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (
        "p.a.src",
        "resource::NETWORK::http://q:8000/items",
        "p.api.handler",
        "p.db.sink",
    )
    assert result.remote_hops == ()


def test_coverage_of_every_project_the_walk_entered_counts() -> None:
    """No path, and the walk entered `q`: `q`'s uncovered module may hold
    the continuation, so its gaps make the verdict UNKNOWN even when the
    asked project is fully covered (bot review on PR #1978, P1). Gaps and
    edges are read for exactly the projects entered, under their own
    names (never `r`, which no reachable hop leads into), and the asked
    project's gaps still count."""
    calls: list[tuple[str, dict | None]] = []
    result = flow_reachability_verdict(
        _query_fn(
            [("p.a.src", "resource::NETWORK::http://q:8000/items")],
            {"q": ["q/uncovered.php"]},
            remote=[
                ("resource::NETWORK::http://q:8000/items", "q.api.handler"),
                ("p.never", "r.api.handler"),
            ],
            other_edges={"q": [("q.api.handler", "q.other")], "r": []},
            calls=calls,
        ),
        "p",
        "p.a.src",
        "q.db.sink",
    )
    assert result.verdict == FLOW_VERDICT_UNKNOWN
    assert result.gaps == ("q/uncovered.php",)
    gap_reads = [
        params for query, params in calls if query == CYPHER_FLOW_COVERAGE_GAPS
    ]
    assert [p["project_name"] for p in gap_reads if p] == ["p", "q"]
    edge_reads = [params for query, params in calls if query == CYPHER_FLOW_EDGES]
    assert [p["project_name"] for p in edge_reads if p] == ["p", "q"]

    covered = flow_reachability_verdict(
        _query_fn(
            [("p.a.src", "resource::NETWORK::http://q:8000/items")],
            {"q": [], "p": ["p/uncovered.php"]},
            remote=[("resource::NETWORK::http://q:8000/items", "q.api.handler")],
            other_edges={"q": [("q.api.handler", "q.other")]},
        ),
        "p",
        "p.a.src",
        "q.db.sink",
    )
    assert covered.verdict == FLOW_VERDICT_UNKNOWN
    assert covered.gaps == ("p/uncovered.php",)


def test_no_flow_requires_full_coverage() -> None:
    result = flow_reachability_verdict(
        _query_fn([("p.a.src", "p.a.other")], []),
        "p",
        "p.a.src",
        "p.a.sink",
    )
    assert result.verdict == FLOW_VERDICT_NO_FLOW
    assert result.path == ()
    assert result.gaps == ()


def test_unknown_names_the_gaps() -> None:
    result = flow_reachability_verdict(
        _query_fn([], ["legacy/script.scala", "legacy/tool.rb"]),
        "p",
        "p.a.src",
        "p.a.sink",
    )
    assert result.verdict == FLOW_VERDICT_UNKNOWN
    assert result.gaps == ("legacy/script.scala", "legacy/tool.rb")


def test_equal_names_without_an_edge_are_not_a_path() -> None:
    """Two identical (even nonexistent) qns must not report FOUND by name
    equality alone; only a genuine cycle through FLOWS_TO edges counts."""
    result = flow_reachability_verdict(
        _query_fn([("p.a", "p.b")], []),
        "p",
        "p.ghost",
        "p.ghost",
    )
    assert result.verdict == FLOW_VERDICT_NO_FLOW


def test_equal_names_with_a_real_cycle_are_found() -> None:
    result = flow_reachability_verdict(
        _query_fn([("p.a.src", "p.a.mid"), ("p.a.mid", "p.a.src")], []),
        "p",
        "p.a.src",
        "p.a.src",
    )
    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == ("p.a.src", "p.a.mid", "p.a.src")


def test_cycles_terminate() -> None:
    result = flow_reachability_verdict(
        _query_fn([("p.a", "p.b"), ("p.b", "p.a")], []),
        "p",
        "p.a",
        "p.z",
    )
    assert result.verdict == FLOW_VERDICT_NO_FLOW


def _module_props(mock_ingestor: MagicMock) -> dict[str, dict]:
    return {
        c.args[1]["qualified_name"]: c.args[1]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == "Module"
    }


def test_indexing_records_flow_coverage_per_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "covered.py").write_text("def f():\n    return 1\n")
    # Lua joined FLOW_REGISTERED_LANGUAGES in #1175, PHP in #1174, Dart in
    # #1173, and Scala (the last gap) in #1256, so every supported language is
    # now flow-registered; an unsupported extension still parses to no Module
    # at all, so there is no honest per-language "uncovered" example left.
    (temp_repo / "lua_covered.lua").write_text("local function f() return 1 end\n")
    (temp_repo / "php_covered.php").write_text("<?php\nfunction f() { return 1; }\n")
    (temp_repo / "dart_covered.dart").write_text("int f() { return 1; }\n")
    (temp_repo / "scala_covered.scala").write_text("object U { def f(): Int = 1 }\n")
    parsers, queries = load_parsers()
    if not {"php", "lua", "dart", "scala"} <= set(parsers):
        pytest.skip("php/lua/dart/scala parser not available")
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    )
    updater.run()
    modules = _module_props(mock_ingestor)
    project = temp_repo.name
    assert modules[f"{project}.covered"]["flow_covered"] is True
    assert modules[f"{project}.lua_covered"]["flow_covered"] is True
    assert modules[f"{project}.php_covered"]["flow_covered"] is True
    assert modules[f"{project}.dart_covered"]["flow_covered"] is True
    assert modules[f"{project}.scala_covered"]["flow_covered"] is True


def test_default_capture_reports_modules_uncovered(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Flow capture is opt-in: with the default selection, even a registered
    language's module is honestly uncovered."""
    (temp_repo / "covered.py").write_text("def f():\n    return 1\n")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )
    updater.run()
    modules = _module_props(mock_ingestor)
    assert modules[f"{temp_repo.name}.covered"]["flow_covered"] is False


def test_protobuf_export_preserves_flow_coverage(tmp_path: Path) -> None:
    """Full round trip: serialize to disk, reload, and read the property."""
    import codec.schema_pb2 as pb
    from codebase_rag import constants as cs
    from codebase_rag.services.protobuf_service import ProtobufFileIngestor

    ingestor = ProtobufFileIngestor(str(tmp_path))
    ingestor.ensure_node_batch(
        "Module",
        {
            "qualified_name": "p.covered",
            "name": "covered.py",
            "path": "covered.py",
            "flow_covered": True,
        },
    )
    ingestor.flush_all()
    raw = (tmp_path / cs.PROTOBUF_INDEX_FILE).read_bytes()
    index = pb.GraphCodeIndex.FromString(raw)
    modules = [n.module for n in index.nodes if n.WhichOneof("payload") == "module"]
    assert modules
    assert modules[0].flow_covered is True


def test_inline_modules_are_never_spurious_coverage_gaps(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Every full Module emission either carries flow_covered (the bodied
    inline-mod producer stamps it, matching its file's coverage) or uses the
    synthetic inline path the gaps query excludes; either way an inline mod
    can never surface as a spurious coverage gap."""
    (temp_repo / "src").mkdir()
    (temp_repo / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n')
    (temp_repo / "src" / "lib.rs").write_text(
        "pub mod inner {\n    pub fn f() -> u32 { 1 }\n}\n"
    )
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    )
    updater.run()
    from codebase_rag import constants as cs

    for call in mock_ingestor.ensure_node_batch.call_args_list:
        if str(call.args[0]) != "Module":
            continue
        props = call.args[1]
        if "path" not in props:
            # Partial MERGE updates onto an existing node carry no path and
            # must not erase the property either.
            continue
        assert props.get("flow_covered") is True or str(props["path"]).startswith(
            cs.INLINE_MODULE_PATH_PREFIX
        ), props


def test_a_handler_in_a_dotted_project_loads_that_projects_edges() -> None:
    """Project names may contain dots: `svc.v2.api.handler` belongs to
    `svc.v2`, not to `svc` as its first segment says, so the walk must load
    `svc.v2`'s flow edges to continue (bot review; the ownership rule of
    #1970)."""
    remote = [("svc.client.net", "svc.v2.api.handler")]
    result = flow_reachability_verdict(
        _query_fn(
            [("svc.a.src", "svc.client.net")],
            {},
            remote=remote,
            other_edges={"svc.v2": [("svc.v2.api.handler", "svc.v2.db.sink")]},
            projects=["svc", "svc.v2"],
        ),
        "svc",
        "svc.a.src",
        "svc.v2.db.sink",
    )
    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.remote_hops == (("svc.client.net", "svc.v2.api.handler"),)


TOKEN = "resource::ENV::TOKEN"
STDOUT = "resource::STDOUT::<dynamic>"
NET = "resource::NETWORK::http://q:8000/items"


@pytest.mark.parametrize(
    ("scope", "verdict"),
    [("p.v2.fn", FLOW_VERDICT_NO_FLOW), ("p.fn", FLOW_VERDICT_FOUND)],
    ids=["a-dotted-sibling-project-owns-it", "the-project-owns-it"],
)
def test_a_resource_flow_belongs_to_the_project_its_scope_names(
    scope: str, verdict: str
) -> None:
    """`r.scope STARTS WITH 'p.'` also loads `p.v2`'s resource flows; only
    the project the scope's function belongs to owns one (bot review on PR
    #2762)."""
    result = flow_reachability_verdict(
        _query_fn([(TOKEN, STDOUT, scope)], [], projects=["p", "p.v2"]),
        "p",
        TOKEN,
        STDOUT,
    )
    assert result.verdict == verdict


def test_another_projects_resource_flow_is_not_taken_for_this_ones() -> None:
    """Resource names carry no project: after a hop into `q`, `q`'s own
    `ENV::TOKEN -> STDOUT` is not a flow of `p`'s token (bot review on PR
    #2762). The token did reach `q`'s handler, which may print it, so the
    answer names the handler rather than a verified absence."""
    result = flow_reachability_verdict(
        _query_fn(
            [(TOKEN, NET, "p.client.send")],
            [],
            remote=[(NET, "q.api.handler")],
            other_edges={"q": [(TOKEN, STDOUT, "q.api.other")]},
            projects=["p", "q"],
        ),
        "p",
        TOKEN,
        STDOUT,
    )
    assert result.verdict == FLOW_VERDICT_UNKNOWN
    assert result.path == ()
    assert result.gaps == ("q.api.handler",)


# `notify` reads TOKEN and hands it to `send(v)`, which prints it; `hello`
# prints a literal. As the indexer writes them: the arg flow, the resource
# flow and its link from `notify` (its scope), and each function's writes.
NOTIFY_EDGES = [
    ("p.app.notify", "p.app.send"),
    (TOKEN, STDOUT, "p.app.notify"),
    ("p.app.notify", STDOUT),
]
NOTIFY_WRITES = {"p": [("p.app.send", STDOUT), ("p.app.hello", STDOUT)]}


@pytest.mark.parametrize(
    "function",
    ["p.app.send", "p.app.hello"],
    ids=["a-parameter-it-prints", "a-literal-it-prints"],
)
def test_a_function_reaches_the_sink_its_own_body_writes(function: str) -> None:
    """The resource flow belongs to the function that read the value
    (`notify`), and a literal write makes none, so neither `send` nor
    `hello` owned a link to STDOUT: NO_FLOW with no gaps, a verified
    absence of a write that is in the graph (bot review on PR #2762)."""
    result = flow_reachability_verdict(
        _query_fn(NOTIFY_EDGES, [], writes=NOTIFY_WRITES), "p", function, STDOUT
    )
    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (function, STDOUT)


@pytest.mark.parametrize("source", [TOKEN, "p.client.send"])
@pytest.mark.parametrize(
    ("handler_flows", "handler_writes"),
    [([("q.api.handler", STDOUT)], []), ([], [("q.api.handler", STDOUT)])],
    ids=["a-flow-it-produced", "a-write"],
)
def test_the_sink_a_handler_writes_is_not_a_flow_of_what_it_was_sent(
    source: str,
    handler_flows: list[tuple[str, ...]],
    handler_writes: list[tuple[str, str]],
) -> None:
    """Neither a handler's write nor its own resource flow records which
    value it wrote: a handler that prints "ok" writes STDOUT too. The value
    sent to it reached the handler and no further, so the answer names the
    handler, never FOUND (CodeRabbit review on PR #2762)."""
    result = flow_reachability_verdict(
        _query_fn(
            [(TOKEN, NET, "p.client.send"), ("p.client.send", NET)],
            [],
            remote=[(NET, "q.api.handler")],
            other_edges={"q": handler_flows},
            projects=["p", "q"],
            writes={"q": handler_writes},
        ),
        "p",
        source,
        STDOUT,
    )
    assert result.verdict == FLOW_VERDICT_UNKNOWN
    assert result.path == ()
    assert result.gaps == ("q.api.handler",)


def test_the_writes_of_a_function_the_walk_reached_are_not_followed() -> None:
    """`f` passes a value to `g`, and `g` prints: whether `g` prints that
    value is not recorded (a composed flow from `f` would be), so `f` names
    `g`. `g` itself still reaches what it writes."""
    query = _query_fn([("p.app.f", "p.app.g")], [], writes={"p": [("p.app.g", STDOUT)]})

    reached = flow_reachability_verdict(query, "p", "p.app.f", STDOUT)
    own = flow_reachability_verdict(query, "p", "p.app.g", STDOUT)

    assert reached.verdict == FLOW_VERDICT_UNKNOWN
    assert reached.gaps == ("p.app.g",)
    assert own.verdict == FLOW_VERDICT_FOUND
    assert own.path == ("p.app.g", STDOUT)


def test_a_resource_reaches_the_handler_it_was_sent_to_and_stops() -> None:
    """The handler is a function sink a resource does reach. Past it, the
    handler's own code flows carry other values, so a function beyond it is
    unknown, naming the handler and that function."""
    query = _query_fn(
        [(TOKEN, NET, "p.client.send")],
        [],
        remote=[(NET, "q.api.handler")],
        other_edges={"q": [("q.api.handler", "q.db.save")]},
        projects=["p", "q"],
    )

    handler = flow_reachability_verdict(query, "p", TOKEN, "q.api.handler")
    beyond = flow_reachability_verdict(query, "p", TOKEN, "q.db.save")

    assert handler.verdict == FLOW_VERDICT_FOUND
    assert handler.path == (TOKEN, NET, "q.api.handler")
    assert beyond.verdict == FLOW_VERDICT_UNKNOWN
    assert beyond.gaps == ("q.api.handler", "q.db.save")


def test_a_resource_asked_about_a_function_is_unknown_not_no_flow() -> None:
    """No edge links a resource to the code that reads it, so the walk from
    `TOKEN` can never reach `notify`, which reads it: NO_FLOW with full
    coverage would be a verified absence that is not one (CodeRabbit review
    on PR #2762)."""
    result = flow_reachability_verdict(
        _query_fn(NOTIFY_EDGES, [], writes=NOTIFY_WRITES), "p", TOKEN, "p.app.notify"
    )
    assert result.verdict == FLOW_VERDICT_UNKNOWN
    assert result.path == ()
    assert result.gaps == ("p.app.notify",)


# Negative: what must not change.


def test_a_write_joins_no_resource_to_the_function_that_writes() -> None:
    """A write links its function to the sink, never a resource to the
    function: `ENV::OTHER` read in a function that prints something else
    still has no flow to STDOUT."""
    result = flow_reachability_verdict(
        _query_fn([], [], writes={"p": [("p.app.quiet", STDOUT)]}),
        "p",
        "resource::ENV::OTHER",
        STDOUT,
    )
    assert result.verdict == FLOW_VERDICT_NO_FLOW


def test_a_dotted_sibling_projects_write_is_not_this_projects() -> None:
    """`p.`'s prefix read returns `p.v2`'s writes too; only `p.v2` owns
    them."""
    result = flow_reachability_verdict(
        _query_fn(
            [],
            [],
            projects=["p", "p.v2"],
            writes={"p": [("p.v2.app.send", STDOUT)]},
        ),
        "p",
        "p.v2.app.send",
        STDOUT,
    )
    assert result.verdict == FLOW_VERDICT_NO_FLOW
