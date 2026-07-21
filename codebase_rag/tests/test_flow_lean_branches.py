# Path-sensitivity completion for the lean non-Python flow walk (issue #714):
# loops and try/catch branch-and-merge for the non-hoisted languages (Go,
# Java, C++), and Rust if/match expressions. MAY semantics: taint surviving
# on ANY path survives the join, a kill counts only when it happens on EVERY
# path, and a loop body's later statements can taint its earlier ones.
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
    # Build the graph for `files` and return (from_qn, to_qn) for every
    # FLOWS_TO edge.
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
    # The read happens AFTER the write in source order, but a later loop
    # iteration carries it back to the sink: needs the second body pass.
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
    # The loop may run zero times, so a kill inside the body must not erase
    # taint on the skip path.
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
    # The try body may throw BEFORE the kill, so the catch handler must be
    # seeded with union(pre, body_exit): the ENV taint still reaches the
    # sink inside catch.
    files = {
        "A.java": (
            "class A {\n"
            "  void work() {\n"
            '    String s = System.getenv("SECRET");\n'
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
            '    String s = System.getenv("SECRET");\n'
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
    # Rust ifs are if_expression, not if_statement: the branch-merge must
    # route them too, so a kill in one branch keeps the other path's taint.
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
    # A kill inside one match arm must not erase the other arms' taint.
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
            '    const char* s = "";\n'
            "    for (int i = 0; i < 2; i++) {\n"
            "        printf(s);\n"
            '        s = getenv("SECRET");\n'
            "    }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_go_for_post_statement_kill_does_not_erase_skip_path(tmp_path: Path) -> None:
    # The for-clause post statement runs only AFTER a completed body
    # iteration, never on the zero-iteration path: a kill there must not
    # erase the pre-loop taint.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func work(n int) {\n"
            '\ts := os.Getenv("SECRET")\n'
            '\tfor i := 0; i < n; s = "safe" {\n'
            "\t}\n"
            '\tos.WriteFile("out.txt", []byte(s), 0644)\n'
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") in flows


def test_java_for_update_kill_runs_after_body_pass(tmp_path: Path) -> None:
    # The update clause runs after each body iteration, so the FIRST body
    # pass still sees the pre-loop taint: a kill in the update must not hide
    # the sink inside the body.
    files = {
        "A.java": (
            "class A {\n"
            "  void work(int n) {\n"
            '    String s = System.getenv("SECRET");\n'
            '    for (int i = 0; i < n; s = "safe") {\n'
            "      System.out.println(s);\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_cpp_for_update_kill_does_not_erase_skip_path(tmp_path: Path) -> None:
    files = {
        "main.cpp": (
            "#include <cstdio>\n"
            "#include <cstdlib>\n"
            "void work(int n) {\n"
            '    const char* s = getenv("SECRET");\n'
            '    for (int i = 0; i < n; s = "") {\n'
            "    }\n"
            "    printf(s);\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_java_try_with_resources_taint_flows_to_body_sink(tmp_path: Path) -> None:
    # The resource declarations of a try-with-resources run before the body
    # on every path: a taint bound there must reach a sink in the body.
    files = {
        "A.java": (
            "class A {\n"
            "  void work() throws Exception {\n"
            '    try (String s = System.getenv("SECRET")) {\n'
            "      System.out.println(s);\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_java_do_while_kill_has_no_skip_path(tmp_path: Path) -> None:
    # A do-while body ALWAYS runs at least once, so a kill there is a kill
    # on every path: no false skip-path flow may survive the loop.
    files = {
        "A.java": (
            "class A {\n"
            "  void work(boolean cond) {\n"
            '    String s = System.getenv("SECRET");\n'
            "    do {\n"
            '      s = "safe";\n'
            "    } while (cond);\n"
            "    System.out.println(s);\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") not in flows


def test_cpp_do_while_condition_sees_body_taint(tmp_path: Path) -> None:
    # The do-while condition runs AFTER each body iteration, so a sink in
    # the condition sees the body's taint.
    files = {
        "main.cpp": (
            "#include <cstdio>\n"
            "#include <cstdlib>\n"
            "void work() {\n"
            '    const char* s = "";\n'
            "    do {\n"
            '        s = getenv("SECRET");\n'
            "    } while (printf(s) > 0);\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_rust_loop_kill_has_no_skip_path(tmp_path: Path) -> None:
    # A Rust `loop` always enters its body before it can break, so a kill
    # on the straight-line body path must not be undone by a skip-path merge.
    files = {
        "main.rs": (
            "fn work() {\n"
            '    let mut s = std::env::var("SECRET").unwrap();\n'
            "    loop {\n"
            "        s = String::new();\n"
            "        break;\n"
            "    }\n"
            '    std::fs::write("out.txt", &s).unwrap();\n'
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") not in flows


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
