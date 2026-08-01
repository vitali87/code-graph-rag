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
    project = tmp_path / "java_project"
    project.mkdir()
    (project / "App.java").write_text(code, encoding="utf-8")
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


_JAVA_CODE = """\
class App {
    void leak() {
        String s = System.getenv("SECRET");
        System.out.println(s);
        System.err.print(s);
        Files.writeString(configPath(), s);
    }
}
"""


def test_java_direct_io_sinks(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # System.getenv reads ENV::SECRET (literal arg); System.out.print* writes
    # STDOUT and System.err.print* writes STDERR (arg is an identifier ->
    # <dynamic>); Files.writeString writes a FILE (its arg is a Path, so the
    # path identity is <dynamic>). First Java increment of issue #714 --
    # direct, non-handle sinks only.
    _build(memgraph_ingestor, tmp_path, _JAVA_CODE)
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::ENV::SECRET") in edges
    assert (_WRITES, "resource::STDOUT::<dynamic>") in edges
    assert (_WRITES, "resource::STDERR::<dynamic>") in edges
    assert (_WRITES, "resource::FILE::<dynamic>") in edges


def test_java_files_read(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> None:
    # Files.readString / readAllLines are direct FILE reads.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void load() {\n"
        "        String cfg = Files.readString(configPath());\n"
        "    }\n"
        "}\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert any(rel == _READS and qn.endswith("FILE::<dynamic>") for rel, qn in edges)


def test_java_local_shadows_system(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A parameter named `System` shadows the java.lang.System global, so
    # System.getenv here is not the stdlib sink; no ENV read may be emitted.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void f(Object System) {\n"
        '        System.getenv("SECRET");\n'
        "    }\n"
        "}\n",
    )
    assert (_READS, "resource::ENV::SECRET") not in _io_edges(memgraph_ingestor)


def test_java_local_var_shadows_system(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A local variable named `System` also shadows the global within its scope.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void f() {\n"
        "        Object System = make();\n"
        '        System.getenv("SECRET");\n'
        "    }\n"
        "}\n",
    )
    assert (_READS, "resource::ENV::SECRET") not in _io_edges(memgraph_ingestor)


def test_java_call_before_decl_still_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Java locals are declare-at-point: a call BEFORE a same-named local is the
    # real global, so it must still emit. A later `Object System` shadows only the
    # calls that follow it, not this earlier System.getenv (source-order shadowing).
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void f() {\n"
        '        System.getenv("A");\n'
        "        Object System = make();\n"
        '        System.getenv("B");\n'
        "    }\n"
        "}\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::ENV::A") in edges
    assert (_READS, "resource::ENV::B") not in edges


def test_java_imported_files_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # `import java.nio.file.Files` maps Files -> java.nio.file.Files, so the call
    # normalises to java.nio.file.Files.readString; the FQN sink key must match so
    # the FILE read still emits (the common, imported case).
    _build(
        memgraph_ingestor,
        tmp_path,
        "import java.nio.file.Files;\n"
        "class App {\n"
        "    void load() {\n"
        "        String cfg = Files.readString(configPath());\n"
        "    }\n"
        "}\n",
    )
    assert any(
        rel == _READS and qn.endswith("FILE::<dynamic>")
        for rel, qn in _io_edges(memgraph_ingestor)
    )


def test_java_fully_qualified_files_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A fully-qualified call `java.nio.file.Files.write(...)` (no import) also hits
    # the FQN sink key.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void save(byte[] b) {\n"
        "        java.nio.file.Files.write(outPath(), b);\n"
        "    }\n"
        "}\n",
    )
    assert any(
        rel == _WRITES and qn.endswith("FILE::<dynamic>")
        for rel, qn in _io_edges(memgraph_ingestor)
    )


def test_java_multi_declarator_second_init_shadowed(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # In `Object System = make(), x = System.getenv(...)` the first declarator binds
    # `System`, which is in scope for the second declarator's initializer (JLS 6.3),
    # so System.getenv there is the local, not the global; no ENV read.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void f() {\n"
        '        Object System = make(), x = System.getenv("SECRET");\n'
        "    }\n"
        "}\n",
    )
    assert (_READS, "resource::ENV::SECRET") not in _io_edges(memgraph_ingestor)


def test_java_earlier_declarator_init_not_shadowed(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # In `Object x = System.getenv("A"), System = make()` the sink is in the FIRST
    # declarator's initializer, which runs BEFORE the second declarator binds
    # `System` (JLS 6.3: a name is in scope only from its own declarator on), so
    # System.getenv there is the global and must emit.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void f() {\n"
        '        Object x = System.getenv("A"), System = make();\n'
        "    }\n"
        "}\n",
    )
    assert (_READS, "resource::ENV::A") in _io_edges(memgraph_ingestor)


def test_java_foreach_iterable_call_not_shadowed(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # The for-each loop variable is NOT in scope in the iterable expression, so a
    # sink there is the real global and must emit even when the loop var name
    # collides with the sink head.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void f() {\n"
        '        for (String System : System.getenv("PATH").split(":")) {\n'
        "            use(System);\n"
        "        }\n"
        "    }\n"
        "}\n",
    )
    assert (_READS, "resource::ENV::PATH") in _io_edges(memgraph_ingestor)


def test_java_foreach_loopvar_shadows_in_body(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # The for-each loop variable IS in scope in the loop body, so a call on it
    # there is shadowed (not the global).
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void f(Object[] items) {\n"
        "        for (Object System : items) {\n"
        '            System.getenv("X");\n'
        "        }\n"
        "    }\n"
        "}\n",
    )
    assert (_READS, "resource::ENV::X") not in _io_edges(memgraph_ingestor)


def test_java_varargs_shadows_system(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A varargs parameter `Object... System` is a `spread_parameter` (no `name`
    # field; the bound name is its plain identifier child). It shadows the global
    # just like a plain parameter, so no ENV read may be emitted.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void f(Object... System) {\n"
        '        System.getenv("SECRET");\n'
        "    }\n"
        "}\n",
    )
    assert (_READS, "resource::ENV::SECRET") not in _io_edges(memgraph_ingestor)


def test_java_handle_constructors_and_wrappers(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # issue #714 handle walk: `new FileWriter` binds a FILE handle; the
    # BufferedReader wrapper takes its identity from the inner FileReader;
    # Files.newBufferedReader unwraps Path.of to the literal.
    _build(
        memgraph_ingestor,
        tmp_path,
        "import java.io.BufferedReader;\n"
        "import java.io.FileReader;\n"
        "import java.io.FileWriter;\n"
        "import java.nio.file.Files;\n"
        "import java.nio.file.Path;\n"
        "class App {\n"
        "  void work(String s) throws Exception {\n"
        '    FileWriter w = new FileWriter("out.txt");\n'
        "    w.write(s);\n"
        '    BufferedReader br = new BufferedReader(new FileReader("in.txt"));\n'
        "    String line = br.readLine();\n"
        '    var r = Files.newBufferedReader(Path.of("cfg.txt"));\n'
        "    r.readLine();\n"
        "  }\n"
        "}\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_WRITES, "resource::FILE::out.txt") in edges
    assert (_READS, "resource::FILE::in.txt") in edges
    assert (_READS, "resource::FILE::cfg.txt") in edges
