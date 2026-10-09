"""One very deep function costs no other function its calls (issue #3173).

The Rust, Go and C++ local-type passes walked a function body with Python
recursion, one frame per tree level. A generated `else if` chain of ~1,000
branches, or one condition of ~1,000 `||` terms, raised RecursionError, and
the call pass caught it for the WHOLE file: every call in it was dropped,
neighbours included, while the sync still reported "done".
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from tree_sitter import Node
from typer.testing import CliRunner, Result

from codebase_rag import cli
from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.call_processor import CallProcessor
from codebase_rag.parsers.type_inference import TypeInferenceEngine
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import ASTNode

# Past Python's default recursion limit of 1,000 frames in all three grammars.
BRANCHES = 1200
TERMS = 1000

_RS_MANIFEST = '[package]\nname = "deep"\nversion = "0.1.0"\nedition = "2021"\n'
_GO_MANIFEST = "module deep\n\ngo 1.21\n"

# Two types share `run`, so only a typed local picks the right one.
_RS_TYPES = (
    "pub struct Worker {}\npub struct Idle {}\n"
    "impl Worker { pub fn run(&self) -> u32 { 1 } }\n"
    "impl Idle { pub fn run(&self) -> u32 { 0 } }\n"
)
_GO_TYPES = (
    "type Worker struct{}\ntype Idle struct{}\n\n"
    "func (w Worker) Run() int { return 1 }\nfunc (i Idle) Run() int { return 0 }\n\n"
)
_CPP_TYPES = (
    "class Worker { public: int run() { return 1; } };\n"
    "class Idle { public: int run() { return 0; } };\n"
)


def _else_if(head: str, mid: str, body: str, count: int) -> str:
    return "\n".join(
        (head if i == 0 else mid).format(i=i) + "\n        " + body.format(i=i)
        for i in range(count)
    )


def _rust(dispatch_body: str) -> dict[str, str]:
    return {
        "Cargo.toml": _RS_MANIFEST,
        "src/lib.rs": _RS_TYPES
        + "pub fn helper() -> u32 { 1 }\npub fn other() -> u32 { 2 }\n"
        + "pub fn dispatch(x: u32) -> u32 {\n    let w = Worker {};\n"
        + dispatch_body
        + "\n}\npub fn simple() -> u32 { other() }\n",
    }


def _go(dispatch_body: str) -> dict[str, str]:
    return {
        "go.mod": _GO_MANIFEST,
        "a.go": "package deep\n\n"
        + _GO_TYPES
        + "func helper() int { return 1 }\nfunc other() int { return 2 }\n\n"
        + "func dispatch(x int) int {\n    w := Worker{}\n"
        + dispatch_body
        + "\n}\n\nfunc simple() int { return other() }\n",
    }


def _cpp(dispatch_body: str) -> dict[str, str]:
    return {
        "a.cpp": _CPP_TYPES
        + "int helper() { return 1; }\nint other() { return 2; }\n"
        + "int dispatch(int x) {\n    Worker w;\n"
        + dispatch_body
        + "\n}\nint simple() { return other(); }\n",
    }


def _or_terms(count: int) -> str:
    return " || ".join(f"x == {i}" for i in range(count))


_SOURCES: dict[tuple[str, str], Callable[[], dict[str, str]]] = {
    ("rust", "else-if"): lambda: _rust(
        _else_if("    if x == {i} {{", "    }} else if x == {i} {{", "{i}", BRANCHES)
        + "\n    } else {\n        helper() + w.run()\n    }"
    ),
    ("rust", "or-chain"): lambda: _rust(
        f"    if {_or_terms(TERMS)} {{ helper() + w.run() }} else {{ 0 }}"
    ),
    ("go", "else-if"): lambda: _go(
        _else_if(
            "    if x == {i} {{", "    }} else if x == {i} {{", "return {i}", BRANCHES
        )
        + "\n    } else {\n        return helper() + w.Run()\n    }"
    ),
    ("go", "or-chain"): lambda: _go(
        f"    if {_or_terms(TERMS)} {{\n        return helper() + w.Run()\n    }}\n"
        "    return 0"
    ),
    ("cpp", "else-if"): lambda: _cpp(
        _else_if(
            "    if (x == {i}) {{",
            "    }} else if (x == {i}) {{",
            "return {i};",
            BRANCHES,
        )
        + "\n    } else {\n        return helper() + w.run();\n    }"
    ),
    ("cpp", "or-chain"): lambda: _cpp(
        f"    if ({_or_terms(TERMS)}) {{ return helper() + w.run(); }}\n    return 0;"
    ),
}

_SHALLOW: dict[str, Callable[[], dict[str, str]]] = {
    "rust": lambda: _rust("    if x == 0 { helper() + w.run() } else { 0 }"),
    "go": lambda: _go(
        "    if x == 0 {\n        return helper() + w.Run()\n    }\n    return 0"
    ),
    "cpp": lambda: _cpp(
        "    if (x == 0) { return helper() + w.run(); }\n    return 0;"
    ),
}

_WORKER_RUN = {
    "rust": "src.lib.Worker.run",
    "go": "a.Worker.Run",
    "cpp": "a.Worker.run",
}
_MODULE = {"rust": "src.lib", "go": "a", "cpp": "a"}
_FILE = {"rust": "src/lib.rs", "go": "a.go", "cpp": "a.cpp"}


def _index(repo: Path, files: dict[str, str], mock: MagicMock) -> GraphUpdater:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return create_and_run_updater(repo, mock)


def _calls(repo: Path, mock: MagicMock) -> set[tuple[str, str]]:
    prefix = f"{repo.name}."
    return {
        (str(c.args[0][2]).removeprefix(prefix), str(c.args[2][2]).removeprefix(prefix))
        for c in mock.ensure_relationship_batch.call_args_list
        if c.args[1] == cs.RelationshipType.CALLS.value
    }


@pytest.mark.parametrize(("lang", "shape"), list(_SOURCES), ids=str)
def test_a_deep_function_keeps_every_call_in_its_file(
    temp_repo: Path, mock_ingestor: MagicMock, lang: str, shape: str
) -> None:
    # The autouse pass-failure gate fails this test on any swallowed error.
    updater = _index(temp_repo, _SOURCES[(lang, shape)](), mock_ingestor)

    mod = _MODULE[lang]
    calls = _calls(temp_repo, mock_ingestor)
    assert (f"{mod}.simple", f"{mod}.other") in calls, calls
    assert (f"{mod}.dispatch", f"{mod}.helper") in calls, calls
    # The deep function's own locals are typed, not skipped: `w.run()` binds
    # the Worker method alone, as it does in a shallow function.
    worker_calls = {to for frm, to in calls if frm == f"{mod}.dispatch"} - {
        f"{mod}.helper"
    }
    assert worker_calls == {f"{mod}.{_WORKER_RUN[lang].removeprefix(mod + '.')}"}, calls
    assert updater.call_pass_failures == []


@pytest.mark.parametrize("lang", list(_SHALLOW))
def test_a_shallow_function_types_its_locals_alike(
    temp_repo: Path, mock_ingestor: MagicMock, lang: str
) -> None:
    # Negative control: the deep cases above must type `w` exactly as here.
    _index(temp_repo, _SHALLOW[lang](), mock_ingestor)
    mod = _MODULE[lang]
    calls = _calls(temp_repo, mock_ingestor)
    assert {to for frm, to in calls if frm == f"{mod}.dispatch"} == {
        f"{mod}.helper",
        f"{mod}.{_WORKER_RUN[lang].removeprefix(mod + '.')}",
    }, calls


def test_failed_local_types_cost_only_that_functions_typing(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    _fail_on_swallowed_pass_errors: list[str],
) -> None:
    # Whatever walker still overflows on a deeper shape is contained to its
    # function: the function's calls resolve untyped, its neighbours' stay.
    original = TypeInferenceEngine.build_local_variable_type_map

    def overflow(
        self: TypeInferenceEngine,
        caller_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        class_context: str | None = None,
    ) -> dict[str, str]:
        name = caller_node.child_by_field_name(cs.FIELD_NAME)
        if name is not None and name.text == b"dispatch":
            raise RecursionError("maximum recursion depth exceeded")
        return original(self, caller_node, module_qn, language, class_context)

    with patch.object(TypeInferenceEngine, "build_local_variable_type_map", overflow):
        updater = _index(temp_repo, _SHALLOW["rust"](), mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert ("src.lib.simple", "src.lib.other") in calls, calls
    assert ("src.lib.dispatch", "src.lib.helper") in calls, calls
    logged = _fail_on_swallowed_pass_errors
    assert len(logged) == 1, logged
    assert logged[0].startswith("Failed to infer local types in "), logged
    assert f"{temp_repo.name}.src.lib.dispatch (src/lib.rs)" in logged[0], logged
    assert updater.call_pass_failures == [temp_repo / "src/lib.rs"]
    logged.clear()


def test_a_failed_caller_costs_only_its_own_calls(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    _fail_on_swallowed_pass_errors: list[str],
) -> None:
    original = CallProcessor._ingest_function_calls

    def explode(
        self: CallProcessor, caller_node: Node, caller_qn: str, *a: Any, **kw: Any
    ) -> None:
        if caller_qn.endswith(".dispatch"):
            raise RuntimeError("caller pass exploded")
        original(self, caller_node, caller_qn, *a, **kw)

    with patch.object(CallProcessor, "_ingest_function_calls", explode):
        updater = _index(temp_repo, _SHALLOW["go"](), mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert ("a.simple", "a.other") in calls, calls
    assert not {to for frm, to in calls if frm == "a.dispatch"}, calls
    logged = _fail_on_swallowed_pass_errors
    assert len(logged) == 1, logged
    # Still the prefix the suite's pass-failure gate watches (issue #1070).
    assert logged[0].startswith("Failed to process calls in "), logged
    assert f"{temp_repo.name}.a.dispatch (a.go)" in logged[0], logged
    assert updater.call_pass_failures == [temp_repo / "a.go"]
    logged.clear()


# --- the sync summary ---------------------------------------------------------


@pytest.fixture(autouse=False)
def _wide(monkeypatch: pytest.MonkeyPatch) -> None:
    # One line per message whatever the tmp path's length.
    monkeypatch.setattr(cli.app_context, "console", Console(width=1000))


def _start(repo: Path, failed: list[Path]) -> Result:
    def build(**kwargs: str) -> MagicMock:
        updater = MagicMock()
        updater.project_name = kwargs["project_name"]
        updater.skipped_because_in_sync = False
        updater.call_pass_failures = failed
        return updater

    connection = MagicMock()
    connection.__enter__.return_value = MagicMock()
    connection.__exit__.return_value = False
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=connection),
        patch("codebase_rag.graph_updater.GraphUpdater", side_effect=build),
        patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        return CliRunner().invoke(
            app,
            [
                "start",
                "--no-start-stack",
                "--repo-path",
                str(repo),
                "--project-name",
                "deep",
                "--update-graph",
            ],
        )


@pytest.mark.usefixtures("_wide")
def test_the_sync_summary_counts_files_whose_call_pass_failed(tmp_path: Path) -> None:
    result = _start(tmp_path, [tmp_path / "src/lib.rs", tmp_path / "go/a.go"])

    assert result.exit_code == 0, result.output
    assert cs.CLI_MSG_SYNC_DONE.split("{")[0] in result.output, result.output
    warning = cs.CLI_MSG_CALL_PASS_FAILURES.format(count=2, files="go/a.go, src/lib.rs")
    assert warning in result.output, result.output


@pytest.mark.usefixtures("_wide")
def test_a_clean_sync_prints_no_call_pass_warning(tmp_path: Path) -> None:
    result = _start(tmp_path, [])

    assert result.exit_code == 0, result.output
    assert cs.CLI_MSG_SYNC_DONE.split("{")[0] in result.output, result.output
    assert "Call analysis failed" not in result.output, result.output


@pytest.mark.usefixtures("_wide")
def test_a_long_failure_list_is_capped(tmp_path: Path) -> None:
    failed = [tmp_path / f"gen/f{i:02}.rs" for i in range(13)]
    result = _start(tmp_path, failed)

    assert result.exit_code == 0, result.output
    shown = ", ".join(f"gen/f{i:02}.rs" for i in range(cs.CLI_CALL_PASS_FAILURES_SHOWN))
    files = f"{shown}, " + cs.CLI_MSG_AND_MORE.format(count=3)
    assert cs.CLI_MSG_CALL_PASS_FAILURES.format(count=13, files=files) in (
        result.output
    ), result.output


def test_a_rerun_does_not_report_the_previous_runs_failures(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    _fail_on_swallowed_pass_errors: list[str],
) -> None:
    # Negative: the list is per run. A reused updater (watch mode) whose next
    # run finds the repo in sync walks no calls and has nothing to report.
    original = TypeInferenceEngine.build_local_variable_type_map

    def overflow(self: TypeInferenceEngine, *args: Any, **kw: Any) -> dict[str, str]:
        raise RecursionError("maximum recursion depth exceeded")

    with patch.object(TypeInferenceEngine, "build_local_variable_type_map", overflow):
        updater = _index(temp_repo, _SHALLOW["cpp"](), mock_ingestor)
    assert updater.call_pass_failures == [temp_repo / "a.cpp"]
    _fail_on_swallowed_pass_errors.clear()

    with patch.object(TypeInferenceEngine, "build_local_variable_type_map", original):
        updater.run()
    assert updater.skipped_because_in_sync
    assert updater.call_pass_failures == []
