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
    # (H) System.getenv reads ENV::SECRET (literal arg); System.out/err.print* write
    # (H) STDOUT (arg is an identifier -> <dynamic>); Files.writeString writes a FILE
    # (H) (its arg is a Path, so the path identity is <dynamic>). First Java increment
    # (H) of issue #714 -- direct, non-handle sinks only.
    _build(memgraph_ingestor, tmp_path, _JAVA_CODE)
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::ENV::SECRET") in edges
    assert (_WRITES, "resource::STDOUT::<dynamic>") in edges
    assert (_WRITES, "resource::FILE::<dynamic>") in edges


def test_java_files_read(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> None:
    # (H) Files.readString / readAllLines are direct FILE reads.
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
    # (H) A parameter named `System` shadows the java.lang.System global, so
    # (H) System.getenv here is not the stdlib sink -- no ENV read may be emitted.
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
    # (H) A local variable named `System` also shadows the global within its scope.
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
    # (H) Java locals are declare-at-point: a call BEFORE a same-named local is the
    # (H) real global, so it must still emit. A later `Object System` shadows only the
    # (H) calls that follow it, not this earlier System.getenv (source-order shadowing).
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
    # (H) `import java.nio.file.Files` maps Files -> java.nio.file.Files, so the call
    # (H) normalises to java.nio.file.Files.readString; the FQN sink key must match so
    # (H) the FILE read still emits (the common, imported case).
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
    # (H) A fully-qualified call `java.nio.file.Files.write(...)` (no import) also hits
    # (H) the FQN sink key.
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
    # (H) In `Object System = make(), x = System.getenv(...)` the first declarator binds
    # (H) `System`, which is in scope for the second declarator's initializer (JLS 6.3),
    # (H) so System.getenv there is the local, not the global -- no ENV read.
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
    # (H) In `Object x = System.getenv("A"), System = make()` the sink is in the FIRST
    # (H) declarator's initializer, which runs BEFORE the second declarator binds
    # (H) `System` (JLS 6.3: a name is in scope only from its own declarator on), so
    # (H) System.getenv there is the global and must emit.
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
    # (H) The for-each loop variable is NOT in scope in the iterable expression, so a
    # (H) sink there is the real global and must emit even when the loop var name
    # (H) collides with the sink head.
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
    # (H) The for-each loop variable IS in scope in the loop body, so a call on it
    # (H) there is shadowed (not the global).
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
    # (H) A varargs parameter `Object... System` is a `spread_parameter` (no `name`
    # (H) field; the bound name is its plain identifier child). It shadows the global
    # (H) just like a plain parameter, so no ENV read may be emitted.
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
