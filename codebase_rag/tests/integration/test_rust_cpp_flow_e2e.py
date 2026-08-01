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


def _build(
    ingestor: MemgraphIngestor, tmp_path: Path, filename: str, code: str
) -> None:
    project = tmp_path / "flow_rs_cpp"
    project.mkdir()
    (project / filename).write_text(code, encoding="utf-8")
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
        "RETURN a.qualified_name AS frm, b.qualified_name AS to, r.kind AS kind"
    )
    return [{k: None if v is None else str(v) for k, v in row.items()} for row in rows]


def _resource_flow(flows: list[dict[str, str | None]], frm: str, to: str) -> bool:
    return any(
        (f["frm"] or "").endswith(frm)
        and (f["to"] or "").endswith(to)
        and f["kind"] == FlowKind.RESOURCE.value
        for f in flows
    )


def _any_resource(flows: list[dict[str, str | None]]) -> bool:
    return any(f["kind"] == FlowKind.RESOURCE.value for f in flows)


# Rust


def test_rust_env_var_to_println(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A `let` carries the env source into a println! macro sink: ENV -> STDOUT.
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.rs",
        "fn boot() {\n"
        '    let secret = std::env::var("SECRET").unwrap();\n'
        '    println!("{}", secret);\n'
        "}\n",
    )
    assert _resource_flow(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_rust_inlined_env_in_println(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # The env read is inlined directly into the macro args: ENV -> STDOUT.
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.rs",
        'fn boot() {\n    println!("{}", std::env::var("SECRET").unwrap());\n}\n',
    )
    assert _resource_flow(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_rust_tainted_path_name_no_over_taint(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A local named `env` shadows nothing in the fully-qualified `std::env::var`
    # path, and only CLEAN is ever printed. The bare-identifier scan must not
    # treat the `env` path segment as the tainted local (over-taint P1).
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.rs",
        "fn boot() {\n"
        '    let env = std::env::var("SECRET").unwrap();\n'
        '    println!("{}", std::env::var("CLEAN").unwrap());\n'
        "}\n",
    )
    flows = _flows(memgraph_ingestor)
    assert _resource_flow(flows, "resource::ENV::CLEAN", "resource::STDOUT::<dynamic>")
    assert not _resource_flow(
        flows, "resource::ENV::SECRET", "resource::STDOUT::<dynamic>"
    )


def test_rust_inline_format_capture_to_println(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Rust inline format capture: `println!("{secret}")` reads the local through
    # the format string with no separate identifier token. ENV -> STDOUT.
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.rs",
        "fn boot() {\n"
        '    let secret = std::env::var("SECRET").unwrap();\n'
        '    println!("{secret}");\n'
        "}\n",
    )
    assert _resource_flow(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_rust_untainted_println_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A println! of a clean literal produces no resource flow.
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.rs",
        'fn boot() {\n    println!("hello");\n}\n',
    )
    assert not _any_resource(_flows(memgraph_ingestor))


# C++


def test_cpp_getenv_to_cout(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A local carries getenv into a cout stream sink: ENV -> STDOUT.
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.cpp",
        "#include <iostream>\n"
        "#include <cstdlib>\n"
        "void boot() {\n"
        '    const char* secret = getenv("SECRET");\n'
        "    std::cout << secret;\n"
        "}\n",
    )
    assert _resource_flow(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_cpp_inlined_getenv_in_cout(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # getenv inlined directly into the stream insertion: ENV -> STDOUT.
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.cpp",
        "#include <iostream>\n"
        "#include <cstdlib>\n"
        "void boot() {\n"
        '    std::cout << getenv("SECRET");\n'
        "}\n",
    )
    assert _resource_flow(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_cpp_untainted_cout_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A cout of a clean literal produces no resource flow.
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.cpp",
        '#include <iostream>\nvoid boot() {\n    std::cout << "hello";\n}\n',
    )
    assert not _any_resource(_flows(memgraph_ingestor))
