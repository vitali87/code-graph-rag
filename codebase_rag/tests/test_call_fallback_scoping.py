# The simple-name trie fallback is the last resort for a call nothing else
# resolved, and it matched on the last name segment alone. In a polyglot
# monorepo that let a Python `asyncio.sleep(...)` bind to a `sleep` closure
# inside a generated TypeScript client, and `cls.exists()` to a TS
# `Interceptors.exists` (issue #945): a name that is not reachable from the
# caller at all. Two candidates can never be what a caller meant: one written
# in an unrelated language, and one scoped inside another function's body,
# which no other module can name.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

TS_CLOSURE_HOLDER = (
    "export const createSseClient = () => {\n"
    "  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));\n"
    "  return { sleep };\n"
    "};\n"
)


def _run_rels(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
    for language in ("python", "typescript"):
        if language not in parsers:
            pytest.skip(f"{language} parser not available")
    root = tmp_path / "repo"
    root.mkdir()
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(ingestor=mock, repo_path=root, parsers=parsers, queries=queries).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
    }


def _calls(rels: set[tuple[str, str, str]], caller_suffix: str) -> set[str]:
    return {b for a, r, b in rels if r == "CALLS" and a.endswith(caller_suffix)}


def test_python_call_does_not_bind_to_typescript_closure(tmp_path: Path) -> None:
    files = {
        "svc/worker.py": (
            "import asyncio\n\n\nasync def wait_for_completion():\n"
            "    await asyncio.sleep(1)\n"
        ),
        "web/src/sse.ts": TS_CLOSURE_HOLDER,
    }
    calls = _calls(_run_rels(tmp_path, files), "worker.wait_for_completion")
    assert "repo.web.src.sse.createSseClient.sleep" not in calls, calls


def test_python_call_does_not_bind_to_typescript_method(tmp_path: Path) -> None:
    files = {
        "svc/store.py": (
            "class Row:\n"
            "    @classmethod\n"
            "    def is_ok(cls) -> bool:\n"
            "        return cls.exists()\n"
        ),
        "web/src/utils.ts": (
            "export class Interceptors {\n  exists() {\n    return true;\n  }\n}\n"
        ),
    }
    calls = _calls(_run_rels(tmp_path, files), "store.Row.is_ok")
    assert "repo.web.src.utils.Interceptors.exists" not in calls, calls


def test_call_does_not_bind_into_another_functions_closure(tmp_path: Path) -> None:
    # Same language, but the only same-named candidate lives inside another
    # function's body, where no other module can reach it by name.
    files = {
        "svc/worker.py": ("def run():\n    helper()\n"),
        "svc/other.py": ("def outer():\n    def helper():\n        return 1\n"),
    }
    calls = _calls(_run_rels(tmp_path, files), "worker.run")
    assert "repo.svc.other.outer.helper" not in calls, calls


def test_module_level_candidate_still_binds(tmp_path: Path) -> None:
    # The fallback itself must keep working: a module-level definition in the
    # caller's own language is exactly what it exists to find.
    files = {
        "svc/worker.py": ("def run():\n    helper()\n"),
        "svc/other.py": ("def helper():\n    return 1\n"),
        "web/src/sse.ts": TS_CLOSURE_HOLDER,
    }
    calls = _calls(_run_rels(tmp_path, files), "worker.run")
    assert "repo.svc.other.helper" in calls, calls


def test_same_language_family_still_binds(tmp_path: Path) -> None:
    # JS and TS are one language family; a JS caller reaching a TS definition
    # is an ordinary first-party call, not a cross-language accident.
    files = {
        "web/src/app.js": ("export function run() {\n  helper();\n}\n"),
        "web/src/helpers.ts": ("export function helper() {\n  return 1;\n}\n"),
    }
    calls = _calls(_run_rels(tmp_path, files), "app.run")
    assert "repo.web.src.helpers.helper" in calls, calls


def test_caller_own_nested_function_still_binds(tmp_path: Path) -> None:
    # A caller's OWN nested def is resolved by the enclosing-scope walk, which
    # runs before the fallback and must be untouched by it.
    files = {
        "svc/worker.py": (
            "def run():\n"
            "    def helper():\n        return 1\n\n"
            "    return helper()\n"
        ),
    }
    calls = _calls(_run_rels(tmp_path, files), "worker.run")
    assert "repo.svc.worker.run.helper" in calls, calls
