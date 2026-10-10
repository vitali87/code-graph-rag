"""A caller that dropped its call along with the callee is not dangling.

Replacing a `reduce` callback with a loop, a named function expression with
an arrow, inlining a nested Python helper, or a Rust closure with a loop
removes the inner function and the caller's call to it in one edit. The
delta still reported the enclosing function as a dangling caller of what
it no longer calls, so `cgr check --fail-on-found` failed a correct change
(issue #3246). A re-parsed caller is now judged by its new body.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import run_check
from codebase_rag.structural_delta import StructuralDelta, has_findings, observe
from evals.cgr_graph import _StatefulIngestor

PROJECT = "cl"

CARGO = '[package]\nname = "cl"\nversion = "0.1.0"\nedition = "2021"\n'
NESTED = (
    "def nested(xs):\n    def double(x):\n        return x * 2\n\n"
    "    return [double(x) for x in xs]\n"
)
SIBLING = "\n\ndef twice(x):\n    double = x * 2\n    return double\n"


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


def _edit(root: Path, files: dict[str, str], edits: dict[str, str]) -> StructuralDelta:
    store, updater = _index(root, files)
    for rel, text in edits.items():
        (root / rel).write_text(text, encoding="utf-8")
    paths = sorted(edits)
    return observe(
        store.fetch_all,
        PROJECT,
        paths,
        lambda: updater.reingest(paths),
        repo_root=root,
    )


def _dangling(delta: StructuralDelta) -> list[tuple[str, str]]:
    return [
        (d["caller"].rsplit(".", 1)[-1], d["target"].rsplit(".", 1)[-1])
        for d in delta["dangling_callers"]
    ]


@pytest.mark.parametrize(
    ("rel", "before", "after", "removed"),
    [
        pytest.param(
            "tot.js",
            "export function total(xs) { return xs.reduce((a, b) => a + b, 0); }\n",
            "export function total(xs) { let t = 0; for (const x of xs) t += x; return t; }\n",
            "total.anonymous_0_45",
            id="js-callback-to-loop",
        ),
        pytest.param(
            "n.js",
            "export function names(us) {\n  return us.map(function pick(u) {\n"
            "    return u.name;\n  });\n}\n",
            "export function names(us) {\n  return us.map((u) => u.name);\n}\n",
            "names.pick",
            id="js-named-function-to-arrow",
        ),
        pytest.param(
            "tot.py",
            NESTED + SIBLING,
            # `doubled` holds the name only as part of another identifier, and
            # the `double` below sits outside `nested`'s body.
            "def nested(xs):\n    doubled = [x * 2 for x in xs]\n    return doubled\n"
            + SIBLING,
            "nested.double",
            id="python-nested-def-inlined",
        ),
        pytest.param(
            "src/main.rs",
            "fn doubled(xs: &[i32]) -> Vec<i32> {\n    xs.iter().map(|x| x * 2).collect()\n}\n",
            "fn doubled(xs: &[i32]) -> Vec<i32> {\n    let mut out = Vec::new();\n"
            "    for x in xs {\n        out.push(x * 2);\n    }\n    out\n}\n",
            "anonymous_",
            id="rust-closure-to-loop",
        ),
    ],
)
def test_a_dropped_inner_function_leaves_no_dangling_caller(
    temp_repo: Path, rel: str, before: str, after: str, removed: str
) -> None:
    delta = _edit(temp_repo, {"Cargo.toml": CARGO, rel: before}, {rel: after})

    # Still listed as information; only no longer a finding.
    assert any(removed in qn for qn in delta["symbols"]["removed"]), delta["symbols"]
    assert delta["dangling_callers"] == []
    assert not has_findings(delta)


def test_a_nested_def_removed_while_its_call_stays_is_dangling(
    temp_repo: Path,
) -> None:
    # Negative: the body still calls `double`, which no longer exists.
    broken = NESTED.replace("    def double(x):\n        return x * 2\n\n", "")

    delta = _edit(temp_repo, {"tot.py": NESTED}, {"tot.py": broken})

    assert _dangling(delta) == [("nested", "double")]
    result = subprocess.run(
        [sys.executable, "-B", "-c", "import tot; tot.nested([1])"],
        cwd=temp_repo,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    assert "NameError: name 'double' is not defined" in result.stderr


def test_a_helper_removed_with_its_only_call_is_not_dangling(temp_repo: Path) -> None:
    source = (
        "def helper(x):\n    return x + 1\n\n\n"
        "def run(xs):\n    return [helper(x) for x in xs]\n"
    )

    delta = _edit(
        temp_repo,
        {"m.py": source},
        {"m.py": "def run(xs):\n    return [x + 1 for x in xs]\n"},
    )

    assert delta["symbols"]["removed"] == [f"{PROJECT}.m.helper"]
    assert delta["dangling_callers"] == []


@pytest.mark.parametrize(
    ("edits", "dangling"),
    [
        # Negative: the edited caller still calls the helper it removed.
        pytest.param(
            {"m.py": "def run(xs):\n    return [helper(x) for x in xs]\n"},
            [("run", "helper")],
            id="call-left-in-the-same-file",
        ),
        # Negative: a caller the edit never touched is read from the base.
        pytest.param(
            {"lib.py": "def other():\n    return 0\n"},
            [("use", "helper")],
            id="caller-in-an-untouched-file",
        ),
    ],
)
def test_a_caller_that_still_names_the_removed_symbol_is_dangling(
    temp_repo: Path, edits: dict[str, str], dangling: list[tuple[str, str]]
) -> None:
    files = {
        "lib.py": "def helper(x):\n    return x + 1\n",
        "use.py": "from lib import helper\n\n\ndef use():\n    return helper(1)\n",
        "m.py": "from lib import helper\n\n\ndef run(xs):\n    return [helper(x) for x in xs]\n",
    }

    delta = _edit(temp_repo, files, {"lib.py": "def other():\n    return 0\n", **edits})

    assert ("use", "helper") in _dangling(delta)
    assert set(dangling) <= set(_dangling(delta))


def test_cgr_check_passes_a_callback_replaced_by_a_loop(temp_repo: Path) -> None:
    root = temp_repo
    before = "export function total(xs) { return xs.reduce((a, b) => a + b, 0); }\n"
    store, _updater = _index(root, {"tot.js": before})
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "tot.js").write_text(
        "export function total(xs) { let t = 0; for (const x of xs) t += x; return t; }\n",
        encoding="utf-8",
    )
    parsers, queries = load_parsers()

    delta = run_check(root, "HEAD", PROJECT, store, parsers, queries)

    assert delta["dangling_callers"] == []
    assert not has_findings(delta)
