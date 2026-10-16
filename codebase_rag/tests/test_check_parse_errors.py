"""The structural delta reports an edit that leaves a file unparsable.

An edit that breaks the syntax of a file passed `cgr check --fail-on-found`
with exit 0 when the break was in the file's last definition (it read as
`changed`), and otherwise came back as `removed` definitions with
`dangling_callers`, because tree-sitter's recovery swallowed the rest of
the file. Nothing said the file no longer parses (issue #3232).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import run_check
from codebase_rag.structural_delta import StructuralDelta, has_findings, observe
from evals.cgr_graph import _StatefulIngestor

PROJECT = "pe"

A = "def f(x):\n    return x + 1\n\n\ndef g():\n    return f(1)\n"
B = "from a import g\n\n\ndef h():\n    return g()\n"


def _index(root: Path, files: dict[str, str]) -> tuple[_StatefulIngestor, GraphUpdater]:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


def _edit_and_observe(
    root: Path, files: dict[str, str], rel: str, broken: str
) -> StructuralDelta:
    store, updater = _index(root, files)
    (root / rel).write_text(broken, encoding="utf-8")
    return observe(
        store.fetch_all,
        PROJECT,
        [rel],
        lambda: updater.reingest([rel]),
        repo_root=root,
        base_source=lambda path: files[path].encode("utf-8") if path in files else None,
    )


@pytest.mark.parametrize(
    ("broken", "line"),
    [
        pytest.param(A.replace("return f(1)", "return f(1"), 6, id="unclosed-call"),
        pytest.param(A.replace("return f(1)", "retrun f(1)"), 6, id="typo"),
        pytest.param(
            A.replace("    return f(1)", "    if True\n        return f(1)"),
            6,
            id="if-without-colon",
        ),
    ],
)
def test_a_break_in_the_last_definition_is_a_parse_error(
    temp_repo: Path, broken: str, line: int
) -> None:
    delta = _edit_and_observe(temp_repo, {"a.py": A, "b.py": B}, "a.py", broken)

    assert [(e["path"], e["line"]) for e in delta["parse_errors"]] == [("a.py", line)]
    assert delta["parse_errors"][0]["message"].startswith(f"a.py:{line}:")
    assert has_findings(delta)


@pytest.mark.parametrize(
    ("broken", "line"),
    [
        pytest.param(A.replace("return x + 1", "return x + ("), 2, id="break-in-f"),
        pytest.param(A.replace("def g():", "def g()"), 5, id="def-without-colon"),
    ],
)
def test_definitions_the_recovery_lost_are_unreliable_not_removed(
    temp_repo: Path, broken: str, line: int
) -> None:
    # `g` is still in the file; the broken tree lost it. Reporting it as
    # removed, with `h -> g` dangling, sent the author after a function they
    # never touched instead of the line that broke.
    delta = _edit_and_observe(temp_repo, {"a.py": A, "b.py": B}, "a.py", broken)

    (error,) = delta["parse_errors"]
    assert (error["path"], error["line"]) == ("a.py", line), error
    assert error["unreliable"] == [f"{PROJECT}.a.g"]
    assert f"{PROJECT}.a.g" not in delta["symbols"]["removed"]
    assert delta["dangling_callers"] == []
    assert delta["dangling_importers"] == []
    assert has_findings(delta)


@pytest.mark.parametrize(
    ("rel", "source", "broken", "line"),
    [
        pytest.param(
            "web/a.ts",
            "export function f(x: number) { return x + 1; }\n"
            "export function g() {\n  return f(1);\n}\n",
            "export function f(x: number) { return x + 1; }\n"
            "export function g() {\n  return f(1;\n}\n",
            3,
            id="typescript",
        ),
        pytest.param(
            "a.go",
            "package a\n\nfunc f(x int) int { return x + 1 }\n\n"
            "func g() int {\n\treturn f(1)\n}\n",
            "package a\n\nfunc f(x int) int { return x + 1 }\n\n"
            "func g() int {\n\treturn f(1\n}\n",
            6,
            id="go",
        ),
    ],
)
def test_typescript_and_go_breaks_are_parse_errors(
    temp_repo: Path, rel: str, source: str, broken: str, line: int
) -> None:
    delta = _edit_and_observe(temp_repo, {rel: source}, rel, broken)

    assert [(e["path"], e["line"]) for e in delta["parse_errors"]] == [(rel, line)]
    assert "missing" in delta["parse_errors"][0]["message"]
    assert has_findings(delta)


def test_a_file_that_did_not_parse_at_the_base_is_not_a_new_error(
    temp_repo: Path,
) -> None:
    # Negative: a grammar gap or an older break the edit did not cause is
    # no finding of this edit.
    broken = A.replace("return f(1)", "return f(1")
    delta = _edit_and_observe(
        temp_repo,
        {"a.py": broken, "b.py": B},
        "a.py",
        broken.replace("return x + 1", "return x + 2"),
    )

    assert delta["parse_errors"] == []


def test_a_clean_edit_reports_no_parse_error(temp_repo: Path) -> None:
    # Negative: the edit changes `g` and still parses.
    delta = _edit_and_observe(
        temp_repo, {"a.py": A, "b.py": B}, "a.py", A.replace("f(1)", "f(1) + 1")
    )

    assert delta["parse_errors"] == []
    assert delta["symbols"]["changed"] == [f"{PROJECT}.a.g"]
    assert not has_findings(delta)


def test_a_new_file_that_does_not_parse_is_a_parse_error(temp_repo: Path) -> None:
    # A file with no base version is held to parsing like any other.
    store, updater = _index(temp_repo, {"a.py": A, "b.py": B})
    (temp_repo / "c.py").write_text("def k(:\n    pass\n", encoding="utf-8")

    delta = observe(
        store.fetch_all,
        PROJECT,
        ["c.py"],
        lambda: updater.reingest(["c.py"]),
        repo_root=temp_repo,
        base_source=lambda _path: None,
    )

    assert [e["path"] for e in delta["parse_errors"]] == ["c.py"]


def test_cgr_check_reports_the_parse_error_against_its_git_base(
    temp_repo: Path,
) -> None:
    # The check reads the base version from git, so a break is measured
    # against the committed file.
    root = temp_repo
    store, _updater = _index(root, {"a.py": A, "b.py": B})
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "a.py").write_text(A.replace("return f(1)", "return f(1"), encoding="utf-8")
    parsers, queries = load_parsers()

    delta = run_check(root, "HEAD", PROJECT, store, parsers, queries)

    assert [(e["path"], e["line"]) for e in delta["parse_errors"]] == [("a.py", 6)]
    assert has_findings(delta)


async def test_an_mcp_write_that_breaks_the_file_leads_with_the_parse_error(
    temp_repo: Path,
) -> None:
    import json
    from unittest.mock import MagicMock

    from codebase_rag import constants as cs
    from codebase_rag.mcp.tools import MCPToolsRegistry

    store, updater = _index(temp_repo, {"a.py": A, "b.py": B})
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(temp_repo), ingestor=ingestor, cypher_gen=MagicMock()
    )
    registry._live_updater = updater

    result = await registry.surgical_replace_code(
        "a.py", "    return x + 1", "    return x + ("
    )

    lead, _sep, payload = result.partition(cs.MCP_DELTA_HEADER + "\n")
    assert lead.startswith("The file no longer parses: a.py:2:"), lead
    delta = json.loads(payload)
    assert delta["parse_errors"][0]["unreliable"] == [f"{PROJECT}.a.g"]
    assert delta["dangling_callers"] == []

    clean = await registry.write_file("a.py", A)
    # Negative: a write that parses leads with the success line as before.
    assert clean.startswith(cs.MCP_WRITE_SUCCESS.format(path="a.py")), clean
