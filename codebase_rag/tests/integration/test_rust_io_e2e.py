from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_READS = cs.RelationshipType.READS_FROM.value
_WRITES = cs.RelationshipType.WRITES_TO.value


def _build(ingestor: MemgraphIngestor, tmp_path: Path, code: str) -> None:
    project = tmp_path / "rust_project"
    project.mkdir()
    (project / "main.rs").write_text(code, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()


def _io_edges(ingestor: MemgraphIngestor) -> set[tuple[str, str]]:
    rows = ingestor.fetch_all(
        f"MATCH ()-[r:{_READS}|{_WRITES}]->(res:{cs.NodeLabel.RESOURCE.value}) "
        "RETURN type(r) AS rel, res.qualified_name AS qn"
    )
    return {(str(row["rel"]), str(row["qn"])) for row in rows}


_RUST_CODE = """\
fn leak(name: String) {
    let s = std::env::var("SECRET").unwrap();
    println!("{}", s);
    print!("{}", s);
    eprintln!("err {}", name);
    eprint!("err {}", name);
    std::fs::write("out.txt", s);
    std::fs::create_dir("d");
    let c = std::fs::read_to_string("in.txt");
}
"""


def test_rust_direct_io_sinks(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # std::env::var reads ENV::SECRET; println!/print!/eprintln!/eprint! macros
    # write STDOUT; std::fs::write / create_dir / read_to_string are direct FILE
    # write/read. First Rust increment of issue #714 -- direct calls + print
    # macros, no handles.
    _build(memgraph_ingestor, tmp_path, _RUST_CODE)
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::ENV::SECRET") in edges
    assert (_WRITES, "resource::STDOUT::<dynamic>") in edges
    assert (_WRITES, "resource::FILE::out.txt") in edges
    assert (_WRITES, "resource::FILE::d") in edges
    assert (_READS, "resource::FILE::in.txt") in edges


def test_rust_call_inside_print_macro_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A sink call written INLINE in macro args -- `println!("{}", env::var("X"))`
    # -- must still emit its READS_FROM. tree-sitter flattens macro bodies into a
    # token_tree of raw tokens (no call_expression node), so the walk reconstructs
    # scoped calls from the token stream. The canonical "log a secret" taint case.
    _build(
        memgraph_ingestor,
        tmp_path,
        "fn f() {\n"
        '    println!("{}", std::env::var("SECRET"));\n'
        '    eprintln!("{}", std::fs::read_to_string("in.txt").unwrap());\n'
        "}\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::ENV::SECRET") in edges
    assert (_READS, "resource::FILE::in.txt") in edges


def test_rust_use_imported_short_path(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # `use std::fs;` then `fs::write(..)` is the idiomatic short form; the sink is
    # keyed on both the full `std::fs::write` and the `fs::write` short path.
    _build(
        memgraph_ingestor,
        tmp_path,
        "use std::fs;\nuse std::env;\n"
        "fn f() {\n"
        '    let k = env::var("TOKEN");\n'
        '    fs::write("o.txt", k);\n'
        "}\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::ENV::TOKEN") in edges
    assert (_WRITES, "resource::FILE::o.txt") in edges


def test_rust_local_module_does_not_match_std_sink(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A local `mod fs` with its own write() must NOT be mistaken for std::fs::write.
    # A bare short path (`fs::write`) resolves to std only when `fs` is imported
    # (`use std::fs;`); with a local module and no import, no FILE edge is emitted.
    _build(
        memgraph_ingestor,
        tmp_path,
        "mod fs {\n"
        "    pub fn write(_p: &str, _d: &str) {}\n"
        "}\n"
        "fn f() {\n"
        '    fs::write("out.txt", "x");\n'
        "}\n",
    )
    assert (_WRITES, "resource::FILE::out.txt") not in _io_edges(memgraph_ingestor)


def test_rust_nested_closure_not_credited(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A macro sink inside a nested closure is not the enclosing fn's I/O; the walk
    # prunes nested scopes, and the closure is not a registered caller here.
    _build(
        memgraph_ingestor,
        tmp_path,
        'fn f() {\n    let g = || { std::fs::write("secret.txt", "x"); };\n}\n',
    )
    assert (_WRITES, "resource::FILE::secret.txt") not in _io_edges(memgraph_ingestor)


def test_rust_file_handle_methods(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # issue #714 handle walk: File::open (through `?`) and File::create
    # (through .unwrap()) bind FILE handles; read/write methods carry the I/O.
    _build(
        memgraph_ingestor,
        tmp_path,
        "use std::fs::File;\n"
        "use std::io::{Read, Write};\n"
        "fn work() -> std::io::Result<()> {\n"
        '    let mut f = File::open("in.txt")?;\n'
        "    let mut s = String::new();\n"
        "    f.read_to_string(&mut s)?;\n"
        '    let mut out = File::create("out.txt").unwrap();\n'
        "    out.write_all(s.as_bytes())?;\n"
        "    Ok(())\n"
        "}\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::FILE::in.txt") in edges
    assert (_WRITES, "resource::FILE::out.txt") in edges
