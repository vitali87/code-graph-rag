from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

HELPER = (
    "package com.foo.util;\n\n"
    "public class Helper {\n"
    "    public static int help() { return 1; }\n"
    "    public int inst() { return 2; }\n"
    "}\n"
)
MAIN = (
    "package com.foo.app;\n\n"
    "import com.foo.util.Helper;\n\n"
    "public class Main {\n"
    "    public int run() { return Helper.help(); }\n"
    "    public int runInst() { Helper h = new Helper(); return h.inst(); }\n"
    "}\n"
)


def _run_calls(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    # Build the graph for `files` (repo-relative paths) and return CALLS edges.
    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("java parser not available")
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
        project_name="repo",
    ).run()
    out: set[tuple[str, str]] = set()
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] == "CALLS":
            out.add((c.args[0][2], c.args[2][2]))
    return out


def _has(calls: set[tuple[str, str]], caller_suffix: str, callee_suffix: str) -> bool:
    return any(
        a.endswith(caller_suffix) and b.endswith(callee_suffix) for a, b in calls
    )


def test_flat_layout_static_call(tmp_path: Path) -> None:
    # Package dirs at the repo root: the import is judged local and resolves to
    # the MODULE qn (…util.Helper) instead of the class qn (…util.Helper.Helper),
    # so the method search misses and the cross-package call is dropped.
    calls = _run_calls(
        tmp_path,
        {
            "com/foo/util/Helper.java": HELPER,
            "com/foo/app/Main.java": MAIN,
        },
    )
    assert _has(calls, "app.Main.Main.run()", "util.Helper.Helper.help()")


def test_flat_layout_instance_call(tmp_path: Path) -> None:
    # Same flat-layout failure through the declared-variable type path.
    calls = _run_calls(
        tmp_path,
        {
            "com/foo/util/Helper.java": HELPER,
            "com/foo/app/Main.java": MAIN,
        },
    )
    assert _has(calls, "app.Main.Main.runInst()", "util.Helper.Helper.inst()")


def test_maven_layout_calls(tmp_path: Path) -> None:
    # Regression: the standard src/main/java layout already resolves.
    calls = _run_calls(
        tmp_path,
        {
            "src/main/java/com/foo/util/Helper.java": HELPER,
            "src/main/java/com/foo/app/Main.java": MAIN,
        },
    )
    assert _has(calls, "app.Main.Main.run()", "util.Helper.Helper.help()")
    assert _has(calls, "app.Main.Main.runInst()", "util.Helper.Helper.inst()")


def test_nested_unmarked_root_calls(tmp_path: Path) -> None:
    # Regression: a package root nested under an unmarked directory (lib/).
    calls = _run_calls(
        tmp_path,
        {
            "lib/com/foo/util/Helper.java": HELPER,
            "lib/com/foo/app/Main.java": MAIN,
        },
    )
    assert _has(calls, "app.Main.Main.run()", "util.Helper.Helper.help()")


def test_gradle_multimodule_calls(tmp_path: Path) -> None:
    # Regression: cross-module call between two Gradle-style modules.
    calls = _run_calls(
        tmp_path,
        {
            "svc-a/src/main/java/com/foo/util/Helper.java": HELPER,
            "svc-b/src/main/java/com/foo/app/Main.java": MAIN,
        },
    )
    assert _has(calls, "app.Main.Main.run()", "util.Helper.Helper.help()")
    assert _has(calls, "app.Main.Main.runInst()", "util.Helper.Helper.inst()")


ENUM_SRC = (
    "package com.foo.util;\n\n"
    "public enum Mode {\n"
    "    FAST, SLOW;\n"
    "    public static Mode parse() { return FAST; }\n"
    "}\n"
)
ENUM_MAIN = (
    "package com.foo.app;\n\n"
    "import com.foo.util.Mode;\n\n"
    "public class EnumMain {\n"
    "    public Mode pick() { return Mode.parse(); }\n"
    "}\n"
)


def test_flat_layout_enum_static_call(tmp_path: Path) -> None:
    # An imported enum's static method: enums register as NodeType.ENUM, so the
    # imported-class normalization must accept them, or the flat-layout call is
    # still dropped.
    calls = _run_calls(
        tmp_path,
        {
            "com/foo/util/Mode.java": ENUM_SRC,
            "com/foo/app/EnumMain.java": ENUM_MAIN,
        },
    )
    assert _has(calls, "app.EnumMain.EnumMain.pick()", "util.Mode.Mode.parse()")
