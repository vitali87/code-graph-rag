# Issue #1140 tier 2: annotation-processor output under target/ and build/ is
# real API surface (Lombok, Dagger, MapStruct) that the prune previously hid,
# leaving every call into a generated method dangling. Discovery next to a
# build file carves out the exact generated subtrees, registers them as Java
# import-probe roots, and stamps their modules generated with a processor
# hint. A repo without build files keeps today's prune untouched.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.capture import ALL_ENABLED
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.java_generated import (
    discover_generated_source_roots,
    generated_prefixes_for,
    generator_hint,
)

_CALLER = (
    "package com.app;\n\nimport com.example.gen.Widget;\n\n"
    "public class Caller {\n    public String use() {\n"
    "        Widget w = new Widget();\n        return w.getName();\n    }\n}\n"
)
_WIDGET = (
    "package com.example.gen;\n\npublic class Widget {\n"
    "    private String name;\n\n    public String getName() {\n"
    "        return this.name;\n    }\n}\n"
)


def _run(repo: Path) -> MagicMock:
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    ).run()
    return mock


def _modules(mock: MagicMock) -> dict[str, dict]:
    return {
        c.args[1]["qualified_name"]: c.args[1]
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == "Module"
    }


def _edges(mock: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == rel
    }


def _write_maven_repo(repo: Path) -> None:
    (repo / "src/main/java/com/app").mkdir(parents=True)
    (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    (repo / "src/main/java/com/app/Caller.java").write_text(_CALLER, encoding="utf-8")
    gen = repo / "target/generated-sources/annotations/com/example/gen"
    gen.mkdir(parents=True)
    (gen / "Widget.java").write_text(_WIDGET, encoding="utf-8")


def test_maven_generated_call_resolves_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write_maven_repo(repo)
    mock = _run(repo)
    modules = _modules(mock)
    widget_qn = "proj.target.generated-sources.annotations.com.example.gen.Widget"
    assert modules[widget_qn]["generated"] is True
    assert modules[widget_qn]["generator"] == "annotations"
    assert (
        "proj.src.main.java.com.app.Caller",
        widget_qn,
    ) in _edges(mock, "IMPORTS")
    assert any(
        src.startswith("proj.src.main.java.com.app.Caller")
        and dst.startswith(f"{widget_qn}.Widget.getName")
        for src, dst in _edges(mock, "CALLS")
    )


def test_handwritten_modules_carry_no_generated_flag(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write_maven_repo(repo)
    mock = _run(repo)
    caller = _modules(mock)["proj.src.main.java.com.app.Caller"]
    assert "generated" not in caller
    assert "generator" not in caller


def test_gradle_layout_is_discovered_with_the_fixed_hint(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
    gen = repo / "build/generated/sources/annotationProcessor/java/main/com/example/gen"
    gen.mkdir(parents=True)
    (gen / "Widget.java").write_text(_WIDGET, encoding="utf-8")
    roots = discover_generated_source_roots(repo)
    assert roots == [
        ("build", "generated", "sources", "annotationProcessor", "java", "main")
    ]
    prefixes = generated_prefixes_for(roots)
    assert (
        generator_hint(
            "build/generated/sources/annotationProcessor/java/main/com/example/gen/Widget.java",
            prefixes,
        )
        == "annotationProcessor"
    )
    mock = _run(repo)
    generated = [
        props for props in _modules(mock).values() if props.get("generated") is True
    ]
    assert generated
    assert all(p["generator"] == "annotationProcessor" for p in generated)


def test_repo_without_build_files_keeps_the_prune(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    (repo / "target/generated-sources/annotations/com").mkdir(parents=True)
    (repo / "target/generated-sources/annotations/com/W.java").write_text(
        "package com;\npublic class W {}\n", encoding="utf-8"
    )
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    assert discover_generated_source_roots(repo) == []
    mock = _run(repo)
    assert not any("target" in qn for qn in _modules(mock))


def test_vanished_generated_roots_stop_rescuing(tmp_path: Path) -> None:
    import shutil

    repo = tmp_path / "proj"
    _write_maven_repo(repo)
    parsers, queries = load_parsers()
    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    )
    updater.run()
    assert any("generated-sources" in p for p in (updater.unignore_paths or ()))
    shutil.rmtree(repo / "target")
    updater.run(force=True)
    assert not any("generated-sources" in p for p in (updater.unignore_paths or ()))


def test_generated_provenance_survives_the_protobuf_round_trip(
    tmp_path: Path,
) -> None:
    import codec.schema_pb2 as pb
    from codebase_rag.services.protobuf_service import ProtobufFileIngestor

    repo = tmp_path / "proj"
    _write_maven_repo(repo)
    out = tmp_path / "out"
    parsers, queries = load_parsers()
    ingestor = ProtobufFileIngestor(output_path=str(out), repo_path=str(repo))
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    ).run(force=True)
    ingestor.flush_all()
    index = pb.GraphCodeIndex()
    index.ParseFromString((out / "index.bin").read_bytes())
    generated = [
        n.module
        for n in index.nodes
        if n.WhichOneof("payload") == "module" and n.module.generated
    ]
    assert generated
    assert all(m.generator == "annotations" for m in generated)
