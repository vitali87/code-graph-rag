# (H) Switch-family path-sensitivity for the lean non-Python flow walk: Go
# (H) switch/type-switch/select and Rust match arms are EXCLUSIVE (no
# (H) fallthrough), while C-family switches (JS/TS, Java colon groups, C++)
# (H) may fall through, so each case entry unions the previous case's exit.
# (H) MAY semantics throughout: a kill counts only when it happens on every
# (H) path through the statement.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run_flow(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
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


def test_go_switch_case_kill_does_not_erase_other_arms(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func work(x int) {\n"
            '\ts := os.Getenv("SECRET")\n'
            "\tswitch x {\n"
            "\tcase 1:\n"
            '\t\ts = "clean"\n'
            "\tcase 2:\n"
            "\t\t_ = x\n"
            "\t}\n"
            '\tos.WriteFile("out.txt", []byte(s), 0644)\n'
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") in flows


def test_go_switch_kill_on_every_arm_with_default_kills(tmp_path: Path) -> None:
    # (H) With a default present some arm always runs, so a kill on EVERY arm
    # (H) (including default) kills on every path: no edge.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func work(x int) {\n"
            '\ts := os.Getenv("SECRET")\n'
            "\tswitch x {\n"
            "\tcase 1:\n"
            '\t\ts = "clean"\n'
            "\tdefault:\n"
            '\t\ts = "clean"\n'
            "\t}\n"
            '\tos.WriteFile("out.txt", []byte(s), 0644)\n'
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") not in flows


def test_go_switch_arms_are_exclusive(tmp_path: Path) -> None:
    # (H) Go has no implicit fallthrough: taint bound in case 1 must not reach
    # (H) a sink in case 2.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func work(x int) {\n"
            '\ts := "clean"\n'
            "\tswitch x {\n"
            "\tcase 1:\n"
            '\t\ts = os.Getenv("SECRET")\n'
            "\tcase 2:\n"
            '\t\tos.WriteFile("out.txt", []byte(s), 0644)\n'
            "\t}\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") not in flows


def test_go_type_switch_arm_kill_does_not_leak(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func work(x any) {\n"
            '\ts := os.Getenv("SECRET")\n'
            "\tswitch v := x.(type) {\n"
            "\tcase int:\n"
            '\t\ts = "clean"\n'
            "\t\t_ = v\n"
            "\tcase string:\n"
            "\t\t_ = v\n"
            "\t}\n"
            '\tos.WriteFile("out.txt", []byte(s), 0644)\n'
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") in flows


def test_go_select_case_kill_does_not_erase_default_path(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func work(ch chan int) {\n"
            '\ts := os.Getenv("SECRET")\n'
            "\tselect {\n"
            "\tcase <-ch:\n"
            '\t\ts = "clean"\n'
            "\tdefault:\n"
            "\t}\n"
            '\tos.WriteFile("out.txt", []byte(s), 0644)\n'
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") in flows


def test_java_colon_switch_case_kill_does_not_erase_other_paths(
    tmp_path: Path,
) -> None:
    files = {
        "A.java": (
            "class A {\n"
            "  void work(int x) {\n"
            '    String s = System.getenv("SECRET");\n'
            "    switch (x) {\n"
            "      case 1:\n"
            '        s = "safe";\n'
            "        break;\n"
            "      case 2:\n"
            "        break;\n"
            "    }\n"
            "    System.out.println(s);\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_java_arrow_switch_rule_kill_does_not_erase_other_rules(
    tmp_path: Path,
) -> None:
    files = {
        "A.java": (
            "class A {\n"
            "  void work(int x) {\n"
            '    String s = System.getenv("SECRET");\n'
            "    switch (x) {\n"
            '      case 1 -> { s = "safe"; }\n'
            "      default -> { }\n"
            "    }\n"
            "    System.out.println(s);\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_java_stacked_default_label_kills_skip_path(tmp_path: Path) -> None:
    # (H) `case 1: default:` stacks both labels on ONE group: the arm is the
    # (H) default target, so some arm always runs and the kill inside it kills
    # (H) on every path. Only the first label being `case` must not hide the
    # (H) default.
    files = {
        "A.java": (
            "class A {\n"
            "  void work(int x) {\n"
            '    String s = System.getenv("SECRET");\n'
            "    switch (x) {\n"
            "      case 1:\n"
            "      default:\n"
            '        s = "safe";\n'
            "        break;\n"
            "    }\n"
            "    System.out.println(s);\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") not in flows


def test_java_conditional_break_before_kill_keeps_taint(tmp_path: Path) -> None:
    # (H) `if (c) break;` exits the switch BEFORE the kill, so the break path
    # (H) carries the taint out even though every arm ends with a kill: the
    # (H) exit state must be captured AT the break, not at the arm's end.
    files = {
        "A.java": (
            "class A {\n"
            "  void work(int x, boolean c) {\n"
            '    String s = System.getenv("SECRET");\n'
            "    switch (x) {\n"
            "      case 1:\n"
            "        if (c) break;\n"
            '        s = "safe";\n'
            "        break;\n"
            "      default:\n"
            '        s = "safe";\n'
            "        break;\n"
            "    }\n"
            "    System.out.println(s);\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_java_break_in_nested_loop_does_not_exit_switch(tmp_path: Path) -> None:
    # (H) A break inside a loop nested in the arm targets the LOOP: the arm
    # (H) still ends with the kill on every switch-exiting path, so no edge.
    files = {
        "A.java": (
            "class A {\n"
            "  void work(int x) {\n"
            '    String s = System.getenv("SECRET");\n'
            "    switch (x) {\n"
            "      case 1:\n"
            "        while (true) { break; }\n"
            '        s = "safe";\n'
            "        break;\n"
            "      default:\n"
            '        s = "safe";\n'
            "        break;\n"
            "    }\n"
            "    System.out.println(s);\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") not in flows


def test_java_colon_switch_fallthrough_carries_case_taint(tmp_path: Path) -> None:
    # (H) No break between the groups: taint bound in case 1 falls through to
    # (H) the sink in case 2.
    files = {
        "A.java": (
            "class A {\n"
            "  void work(int x) {\n"
            '    String s = "clean";\n'
            "    switch (x) {\n"
            "      case 1:\n"
            '        s = System.getenv("SECRET");\n'
            "      case 2:\n"
            "        System.out.println(s);\n"
            "        break;\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_cpp_switch_case_kill_does_not_erase_other_cases(tmp_path: Path) -> None:
    files = {
        "main.cpp": (
            "#include <cstdlib>\n"
            "#include <iostream>\n"
            "void work(int x) {\n"
            '    const char* s = getenv("SECRET");\n'
            "    switch (x) {\n"
            "        case 1:\n"
            '            s = "safe";\n'
            "            break;\n"
            "        default:\n"
            "            break;\n"
            "    }\n"
            "    std::cout << s;\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_js_switch_case_kill_does_not_erase_other_cases(tmp_path: Path) -> None:
    files = {
        "m.js": (
            "export function work(x) {\n"
            "  let s = process.env.SECRET\n"
            "  switch (x) {\n"
            "    case 1:\n"
            "      s = 'safe'\n"
            "      break\n"
            "    default:\n"
            "      break\n"
            "  }\n"
            "  console.log(s)\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_js_switch_fallthrough_carries_case_taint(tmp_path: Path) -> None:
    files = {
        "m.js": (
            "export function work(x) {\n"
            "  let s = 'clean'\n"
            "  switch (x) {\n"
            "    case 1:\n"
            "      s = process.env.SECRET\n"
            "    case 2:\n"
            "      console.log(s)\n"
            "      break\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_js_do_while_loop_carried_taint(tmp_path: Path) -> None:
    # (H) The sink precedes the bind in source order; a later iteration
    # (H) carries the taint back: needs the mandatory-loop second pass.
    files = {
        "m.js": (
            "export function work(x) {\n"
            "  let s = ''\n"
            "  do {\n"
            "    console.log(s)\n"
            "    s = process.env.SECRET\n"
            "  } while (x)\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows
