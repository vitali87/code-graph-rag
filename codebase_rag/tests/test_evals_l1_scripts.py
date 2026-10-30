"""The per-language L1 structure evals (`evals/l1/<language>.py`): each one
is the shared driver wired to its own language's oracle, extractor, scored
node kinds, messages and output files. A script copied from another and left
half-edited grades one language against another's oracle, or writes over
another language's results, so the wiring is what these pin."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from codebase_rag import constants as cs
from evals import cgr_graph, oracles
from evals import constants as ec
from evals import logs as ls
from evals.l1 import csharp as csharp_l1
from evals.l1 import go as go_l1
from evals.l1 import java as java_l1
from evals.l1 import js as js_l1
from evals.l1 import lua as lua_l1
from evals.l1 import php as php_l1
from evals.l1 import rust as rust_l1
from evals.l1 import ts as ts_l1


@dataclass(frozen=True)
class _Wiring:
    script: ModuleType
    available: Callable[[], bool]
    skip_reason: Callable[[], str | None] | None
    oracle_missing: str
    extract_cgr: Callable[..., object]
    run_oracle: Callable[..., object]
    oracle_binary: str
    scored_node_kinds: tuple[cs.NodeLabel, ...]
    scores_filename: str
    diff_filename: str
    language_name: str


_WIRINGS = {
    "csharp": _Wiring(
        csharp_l1,
        oracles.csharp_oracle_available,
        oracles.csharp_oracle_skip_reason,
        ls.CSHARP_ORACLE_MISSING,
        cgr_graph.extract_cgr_csharp_graph,
        oracles.run_csharp_oracle,
        ec.DOTNET_BIN,
        ec.CSHARP_SCORED_NODE_KINDS,
        ec.CSHARP_SCORES_FILENAME,
        ec.CSHARP_DIFF_FILENAME,
        "C#",
    ),
    "go": _Wiring(
        go_l1,
        oracles.go_oracle_available,
        None,
        ls.GO_ORACLE_MISSING,
        cgr_graph.extract_cgr_go_graph,
        oracles.run_go_oracle,
        ec.GO_BIN,
        ec.GO_SCORED_NODE_KINDS,
        ec.GO_SCORES_FILENAME,
        ec.GO_DIFF_FILENAME,
        "Go",
    ),
    "java": _Wiring(
        java_l1,
        oracles.java_oracle_available,
        None,
        ls.JAVA_ORACLE_MISSING,
        cgr_graph.extract_cgr_java_graph,
        oracles.run_java_oracle,
        ec.JAVA_BIN,
        ec.JAVA_SCORED_NODE_KINDS,
        ec.JAVA_SCORES_FILENAME,
        ec.JAVA_DIFF_FILENAME,
        "Java",
    ),
    # JavaScript and TypeScript share the tsc oracle's availability check and
    # message, and nothing else.
    "js": _Wiring(
        js_l1,
        oracles.typescript_oracle_available,
        oracles.typescript_oracle_skip_reason,
        ls.TS_ORACLE_MISSING,
        cgr_graph.extract_cgr_js_graph,
        oracles.run_javascript_oracle,
        ec.NODE_BIN,
        ec.JS_SCORED_NODE_KINDS,
        ec.JS_SCORES_FILENAME,
        ec.JS_DIFF_FILENAME,
        "JavaScript",
    ),
    "ts": _Wiring(
        ts_l1,
        oracles.typescript_oracle_available,
        oracles.typescript_oracle_skip_reason,
        ls.TS_ORACLE_MISSING,
        cgr_graph.extract_cgr_ts_graph,
        oracles.run_typescript_oracle,
        ec.NODE_BIN,
        ec.TS_SCORED_NODE_KINDS,
        ec.TS_SCORES_FILENAME,
        ec.TS_DIFF_FILENAME,
        "TypeScript",
    ),
    "lua": _Wiring(
        lua_l1,
        oracles.lua_oracle_available,
        oracles.lua_oracle_skip_reason,
        ls.LUA_ORACLE_MISSING,
        cgr_graph.extract_cgr_lua_graph,
        oracles.run_lua_oracle,
        ec.NODE_BIN,
        ec.LUA_SCORED_NODE_KINDS,
        ec.LUA_SCORES_FILENAME,
        ec.LUA_DIFF_FILENAME,
        "Lua",
    ),
    "php": _Wiring(
        php_l1,
        oracles.php_oracle_available,
        oracles.php_oracle_skip_reason,
        ls.PHP_ORACLE_MISSING,
        cgr_graph.extract_cgr_php_graph,
        oracles.run_php_oracle,
        ec.NODE_BIN,
        ec.PHP_SCORED_NODE_KINDS,
        ec.PHP_SCORES_FILENAME,
        ec.PHP_DIFF_FILENAME,
        "PHP",
    ),
    "rust": _Wiring(
        rust_l1,
        oracles.rust_oracle_available,
        None,
        ls.RS_ORACLE_MISSING,
        cgr_graph.extract_cgr_rust_graph,
        oracles.run_rust_oracle,
        ec.CARGO_BIN,
        ec.RS_SCORED_NODE_KINDS,
        ec.RS_SCORES_FILENAME,
        ec.RS_DIFF_FILENAME,
        "Rust",
    ),
}


class _Driver:
    """Stands in for the shared driver, recording the run it was asked for."""

    def __init__(self) -> None:
        self.args: tuple[object, ...] = ()
        self.kwargs: dict[str, object] = {}

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.args, self.kwargs = args, kwargs


@pytest.mark.parametrize("language", sorted(_WIRINGS))
def test_each_script_runs_the_driver_with_its_own_languages_wiring(
    language: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wiring = _WIRINGS[language]
    driver = _Driver()
    monkeypatch.setattr(wiring.script, "run_l1_eval", driver)

    wiring.script.main(target=tmp_path, project_name="demo", out_dir=tmp_path / "o")

    assert driver.args == (tmp_path, "demo", tmp_path / "o")
    kwargs = driver.kwargs
    assert kwargs["available"] is wiring.available
    assert kwargs.get("skip_reason") is wiring.skip_reason
    assert kwargs["oracle_missing"] == wiring.oracle_missing
    assert kwargs["extract_cgr"] is wiring.extract_cgr
    assert kwargs["run_oracle"] is wiring.run_oracle
    assert kwargs["oracle_binary"] == wiring.oracle_binary
    assert kwargs["scored_node_kinds"] == wiring.scored_node_kinds
    assert kwargs["scores_filename"] == wiring.scores_filename
    assert kwargs["diff_filename"] == wiring.diff_filename
    assert f"({wiring.language_name} vs " in str(kwargs["title"])


def test_no_two_scripts_write_the_same_result_files() -> None:
    outputs = [
        name
        for wiring in _WIRINGS.values()
        for name in (wiring.scores_filename, wiring.diff_filename)
    ]
    assert len(outputs) == len(set(outputs))


def test_no_two_scripts_grade_against_the_same_oracle_or_extractor() -> None:
    assert len({id(w.run_oracle) for w in _WIRINGS.values()}) == len(_WIRINGS)
    assert len({id(w.extract_cgr) for w in _WIRINGS.values()}) == len(_WIRINGS)
