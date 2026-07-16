# (H) Path-sensitivity completion for the lean non-Python flow walk (issue #714):
# (H) loops and try/catch branch-and-merge for the non-hoisted languages (Go,
# (H) Java, C++), and Rust if/match expressions. MAY semantics: taint surviving
# (H) on ANY path survives the join, a kill counts only when it happens on EVERY
# (H) path, and a loop body's later statements can taint its earlier ones.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run_flow(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    # (H) Build the graph for `files` and return (from_qn, to_qn) for every
    # (H) FLOWS_TO edge.
    parsers, queries = load_parsers()
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }


def test_go_loop_carried_taint_reaches_earlier_sink(tmp_path: Path) -> None:
    # (H) The read happens AFTER the write in source order, but a later loop
    # (H) iteration carries it back to the sink: needs the second body pass.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func work(s string) {\n"
            "\tfor i := 0; i < 2; i++ {\n"
            '\t\tos.WriteFile("out.txt", []byte(s), 0644)\n'
            '\t\ts = os.Getenv("SECRET")\n'
            "\t}\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") in flows


def test_go_kill_inside_loop_body_does_not_erase_skip_path(tmp_path: Path) -> None:
    # (H) The loop may run zero times, so a kill inside the body must not erase
    # (H) taint on the skip path.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func work(items []string) {\n"
            '\ts := os.Getenv("SECRET")\n'
            "\tfor range items {\n"
            '\t\ts = "safe"\n'
            "\t}\n"
            '\tos.WriteFile("out.txt", []byte(s), 0644)\n'
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") in flows


def test_java_catch_sees_pre_kill_taint(tmp_path: Path) -> None:
    # (H) The try body may throw BEFORE the kill, so the catch handler must be
    # (H) seeded with union(pre, body_exit): the ENV taint still reaches the
    # (H) sink inside catch.
    files = {
        "A.java": (
            "class A {\n"
            "  void work() {\n"
            "    String s = System.getenv(\"SECRET\");\n"
            "    try {\n"
            '      s = "safe";\n'
            "      risky();\n"
            "    } catch (Exception e) {\n"
            "      System.out.println(s);\n"
            "    }\n"
            "  }\n"
            "  void risky() {}\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_java_while_kill_does_not_erase_skip_path(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "class A {\n"
            "  void work(boolean cond) {\n"
            "    String s = System.getenv(\"SECRET\");\n"
            "    while (cond) {\n"
            '      s = "safe";\n'
            "    }\n"
            "    System.out.println(s);\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_cpp_catch_sees_pre_kill_taint(tmp_path: Path) -> None:
    files = {
        "main.cpp": (
            "#include <cstdlib>\n"
            "#include <iostream>\n"
            "void work() {\n"
            '    const char* s = getenv("SECRET");\n'
            "    try {\n"
            '        s = "safe";\n'
            "        risky();\n"
            "    } catch (...) {\n"
            "        std::cout << s;\n"
            "    }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_rust_if_kill_does_not_erase_else_path(tmp_path: Path) -> None:
    # (H) Rust ifs are if_expression, not if_statement: the branch-merge must
    # (H) route them too, so a kill in one branch keeps the other path's taint.
    files = {
        "main.rs": (
            "fn work(cond: bool) {\n"
            '    let mut s = std::env::var("SECRET").unwrap();\n'
            "    if cond {\n"
            "        s = String::new();\n"
            "    }\n"
            '    std::fs::write("out.txt", &s).unwrap();\n'
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") in flows


def test_rust_match_arm_kill_does_not_leak(tmp_path: Path) -> None:
    # (H) A kill inside one match arm must not erase the other arms' taint.
    files = {
        "main.rs": (
            "fn work(n: u8) {\n"
            '    let mut s = std::env::var("SECRET").unwrap();\n'
            "    match n {\n"
            "        1 => {\n"
            "            s = String::new();\n"
            "        }\n"
            "        _ => {}\n"
            "    }\n"
            '    std::fs::write("out.txt", &s).unwrap();\n'
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") in flows


def test_cpp_for_loop_carried_taint(tmp_path: Path) -> None:
    files = {
        "main.cpp": (
            "#include <cstdio>\n"
            "#include <cstdlib>\n"
            "void work() {\n"
            "    const char* s = \"\";\n"
            "    for (int i = 0; i < 2; i++) {\n"
            "        printf(s);\n"
            '        s = getenv("SECRET");\n'
            "    }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
