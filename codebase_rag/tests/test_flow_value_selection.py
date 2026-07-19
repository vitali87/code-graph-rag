# (H) Taint through value-selection expressions: a ternary / conditional
# (H) expression MAY yield either branch, and a short-circuit default
# (H) (`env || 'fallback'`, `x or 'y'`) MAY yield either operand, so the
# (H) bound name unions the branches' taints instead of dropping them.
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
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }


_ENV_TO_STDOUT = ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>")


def test_python_ternary_bind_carries_taint(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import os\n\n"
            "def work(cond):\n"
            "    s = os.getenv('SECRET') if cond else 'clean'\n"
            "    print(s)\n"
        )
    }
    assert _ENV_TO_STDOUT in _run_flow(tmp_path, files)


def test_python_or_default_bind_carries_taint(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import os\n\n"
            "def work():\n"
            "    s = os.getenv('SECRET') or 'clean'\n"
            "    print(s)\n"
        )
    }
    assert _ENV_TO_STDOUT in _run_flow(tmp_path, files)


def test_python_clean_ternary_still_kills(tmp_path: Path) -> None:
    # (H) A ternary over two clean values REPLACES the old taint.
    files = {
        "m.py": (
            "import os\n\n"
            "def work(cond):\n"
            "    s = os.getenv('SECRET')\n"
            "    s = 'a' if cond else 'b'\n"
            "    print(s)\n"
        )
    }
    assert _ENV_TO_STDOUT not in _run_flow(tmp_path, files)


def test_js_ternary_bind_carries_taint(tmp_path: Path) -> None:
    files = {
        "m.js": (
            "export function work(c) {\n"
            "  const s = c ? process.env.SECRET : 'clean'\n"
            "  console.log(s)\n"
            "}\n"
        )
    }
    assert _ENV_TO_STDOUT in _run_flow(tmp_path, files)


def test_js_logical_or_default_carries_taint(tmp_path: Path) -> None:
    files = {
        "m.js": (
            "export function work() {\n"
            "  const s = process.env.SECRET || 'clean'\n"
            "  console.log(s)\n"
            "}\n"
        )
    }
    assert _ENV_TO_STDOUT in _run_flow(tmp_path, files)


def test_js_nullish_default_carries_taint(tmp_path: Path) -> None:
    files = {
        "m.js": (
            "export function work() {\n"
            "  const s = process.env.SECRET ?? 'clean'\n"
            "  console.log(s)\n"
            "}\n"
        )
    }
    assert _ENV_TO_STDOUT in _run_flow(tmp_path, files)


def test_js_logical_and_carries_right_taint(tmp_path: Path) -> None:
    files = {
        "m.js": (
            "export function work(c) {\n"
            "  const s = c && process.env.SECRET\n"
            "  console.log(s)\n"
            "}\n"
        )
    }
    assert _ENV_TO_STDOUT in _run_flow(tmp_path, files)


def test_js_arithmetic_binary_does_not_carry_taint(tmp_path: Path) -> None:
    # (H) Only short-circuit operators select a whole operand as the value;
    # (H) `s = n - 1` over clean operands must stay clean even while a
    # (H) tainted local exists.
    files = {
        "m.js": (
            "export function work(n) {\n"
            "  const t = process.env.OTHER\n"
            "  const s = n - 1\n"
            "  console.log(s)\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::OTHER", "resource::STDOUT::<dynamic>") not in flows


def test_java_ternary_bind_carries_taint(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "class A {\n"
            "  void work(boolean c) {\n"
            '    String s = c ? System.getenv("SECRET") : "clean";\n'
            "    System.out.println(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_TO_STDOUT in _run_flow(tmp_path, files)


def test_cpp_ternary_bind_carries_taint(tmp_path: Path) -> None:
    files = {
        "main.cpp": (
            "#include <cstdlib>\n"
            "#include <iostream>\n"
            "void work(bool c) {\n"
            '    const char* s = c ? getenv("SECRET") : "clean";\n'
            "    std::cout << s;\n"
            "}\n"
        )
    }
    assert _ENV_TO_STDOUT in _run_flow(tmp_path, files)
