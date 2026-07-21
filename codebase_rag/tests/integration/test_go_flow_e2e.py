from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.flow_access import FlowKind

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_FLOWS = cs.RelationshipType.FLOWS_TO.value


def _build(ingestor: MemgraphIngestor, tmp_path: Path, code: str) -> None:
    project = tmp_path / "flow_go"
    project.mkdir()
    (project / "main.go").write_text(code, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()


def _flows(ingestor: MemgraphIngestor) -> list[dict[str, str | None]]:
    rows = ingestor.fetch_all(
        f"MATCH (a)-[r:{_FLOWS}]->(b) "
        "RETURN a.qualified_name AS frm, b.qualified_name AS to, "
        "r.kind AS kind, r.via AS via"
    )
    return [{k: None if v is None else str(v) for k, v in row.items()} for row in rows]


def _has(flows: list[dict[str, str | None]], frm: str, to: str, **props: str) -> bool:
    return any(
        (f["frm"] or "").endswith(frm)
        and (f["to"] or "").endswith(to)
        and all(f.get(k) == v for k, v in props.items())
        for f in flows
    )


def test_go_env_flows_to_stdout(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A `:=` local carries the env source to a later Println: ENV -> STDOUT.
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func boot() {\n"
        '\tsecret := os.Getenv("SECRET")\n'
        "\tfmt.Println(secret)\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_go_direct_env_argument_flows_to_stdout(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        'func boot() {\n\tfmt.Println(os.Getenv("TOKEN"))\n}\n',
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::TOKEN",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_go_network_read_flows_to_file(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import (\n\t"net/http"\n\t"os"\n)\n\n'
        "func sync() {\n"
        '\tdata := http.Get("https://api.example.com/x")\n'
        '\tos.WriteFile("out.txt", data, 0644)\n'
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::NETWORK::https://api.example.com/x",
        "resource::FILE::out.txt",
        kind=FlowKind.RESOURCE.value,
    )


def test_go_tainted_value_arg_edge_to_callee(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import "os"\n\n'
        "func sink_fn(x string) {}\n\n"
        "func boot() {\n"
        '\tsecret := os.Getenv("SECRET")\n'
        "\tsink_fn(secret)\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "boot",
        "sink_fn",
        kind=FlowKind.ARG.value,
    )


def test_go_untainted_value_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import "fmt"\n\n'
        'func boot() {\n\ts := "safe"\n\tfmt.Println(s)\n}\n',
    )
    flows = _flows(memgraph_ingestor)
    assert not any(f["kind"] == FlowKind.RESOURCE.value for f in flows)


def test_go_return_taint_flows_across_call(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # getSecret() returns os.Getenv(...); the caller assigns and logs it, so the
    # return edge and the ENV -> STDOUT flow both appear (shared fixpoint).
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        'func getSecret() string {\n\treturn os.Getenv("SECRET")\n}\n\n'
        "func boot() {\n"
        "\ts := getSecret()\n"
        "\tfmt.Println(s)\n"
        "}\n",
    )
    flows = _flows(memgraph_ingestor)
    assert _has(flows, "getSecret", "boot", kind=FlowKind.RETURN.value)
    assert _has(
        flows,
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_go_two_value_assignment_flows(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # `resp, err := http.Get(url)` is the idiomatic form: the single call feeds
    # both LHS names, so the network source still reaches the file sink via resp.
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import (\n\t"net/http"\n\t"os"\n)\n\n'
        "func sync() {\n"
        '\tresp, err := http.Get("https://api.example.com/x")\n'
        "\t_ = err\n"
        '\tos.WriteFile("out.txt", resp, 0644)\n'
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::NETWORK::https://api.example.com/x",
        "resource::FILE::out.txt",
        kind=FlowKind.RESOURCE.value,
    )


def test_go_local_shadows_source_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A local `os` shadows the imported package, so os.Getenv here is not the env
    # source and nothing must flow to the Println sink.
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func boot() {\n"
        "\tos := load()\n"
        '\tsecret := os.Getenv("SECRET")\n'
        "\tfmt.Println(secret)\n"
        "}\n",
    )
    flows = _flows(memgraph_ingestor)
    assert not any(f["kind"] == FlowKind.RESOURCE.value for f in flows)


def test_go_later_shadow_does_not_suppress_earlier_source(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Go has no hoisting: a `os := load()` AFTER the read must not retroactively
    # shadow the earlier real os.Getenv, so the direct ENV -> STDOUT flow stands.
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func boot() {\n"
        '\tfmt.Println(os.Getenv("SECRET"))\n'
        "\tos := load()\n"
        "\t_ = os\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_go_later_shadow_keeps_prior_variable_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A read bound to `secret`, THEN a later `os := load()` shadow, then a log of
    # secret: the import was in scope at the read, so ENV -> STDOUT must survive.
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func boot() {\n"
        '\tsecret := os.Getenv("SECRET")\n'
        "\tos := load()\n"
        "\t_ = os\n"
        "\tfmt.Println(secret)\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_go_parallel_assignment_preserves_rhs_taint(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Go evaluates every RHS before any LHS update; `_, copy := "safe", secret`
    # must read `secret` as still tainted, so logging copy keeps ENV -> STDOUT.
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func boot() {\n"
        '\tsecret := os.Getenv("SECRET")\n'
        '\tsecret, copy := "safe", secret\n'
        "\t_ = secret\n"
        "\tfmt.Println(copy)\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_go_reassignment_kills_taint(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Re-binding the tainted var to a constant kills its taint before the sink.
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func boot() {\n"
        '\tsecret := os.Getenv("SECRET")\n'
        '\tsecret = "safe"\n'
        "\tfmt.Println(secret)\n"
        "}\n",
    )
    flows = _flows(memgraph_ingestor)
    assert not any(f["kind"] == FlowKind.RESOURCE.value for f in flows)
