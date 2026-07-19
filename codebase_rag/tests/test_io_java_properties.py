# (H) Java system properties are process-level configuration exactly like
# (H) environment variables (commons-io reads java.io.tmpdir/user.home
# (H) constantly): model them as ENV resources.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

READS_FROM = cs.RelationshipType.READS_FROM.value
WRITES_TO = cs.RelationshipType.WRITES_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) in (READS_FROM, WRITES_TO)
    }


def _has(rels: set[tuple[str, str, str]], caller: str, rel: str, resource: str) -> bool:
    return any(
        a.partition("(")[0].endswith(caller) and r == rel and b == resource
        for a, r, b in rels
    )


def test_java_get_property_reads_env(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "class A {\n"
            "  String tmp() {\n"
            '    return System.getProperty("java.io.tmpdir");\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.tmp", READS_FROM, "resource::ENV::java.io.tmpdir"), rels


def test_java_qualified_clear_property_writes_env(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "class A {\n"
            "  void drop() {\n"
            '    java.lang.System.clearProperty("flag");\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.drop", WRITES_TO, "resource::ENV::flag"), rels


def test_java_set_property_writes_env(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "class A {\n"
            "  void mark() {\n"
            '    System.setProperty("flag", "on");\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.mark", WRITES_TO, "resource::ENV::flag"), rels
