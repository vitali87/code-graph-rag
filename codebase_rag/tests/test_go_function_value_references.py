# A Go function used as a first-class VALUE, returned bare (`return usageFn`)
# or placed in a composite literal (a func map `map[string]any{"rpad": rpad}` or
# a func slice `[]Handler{a}`), is invoked later by whoever receives it, never by
# a call the graph can see. Without a reference edge the function looks dead
# (cobra's defaultUsageFunc / rpad / trimRightSpace). cgr must reference such a
# function from the enclosing scope so it stays reachable.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _edges(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    calls = get_relationships(mock_ingestor, "CALLS")
    refs = get_relationships(mock_ingestor, "REFERENCES")
    return {(c.args[0][2], c.args[2][2]) for c in calls + refs}


def _project(temp_repo: Path, body: str) -> Path:
    p = temp_repo / "goref"
    p.mkdir()
    (p / "go.mod").write_text("module goref\n\ngo 1.22\n", encoding="utf-8")
    (p / "m.go").write_text(f"package m\n\n{body}\n", encoding="utf-8")
    return p


def test_returned_function_value_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _project(
        temp_repo,
        "func usageFn(x int) int { return x }\n"
        "func getUsage() func(int) int {\n\treturn usageFn\n}\n",
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="go")
    edges = _edges(mock_ingestor)
    assert any(
        f.endswith(".goref.m.getUsage") and t.endswith(".goref.m.usageFn")
        for f, t in edges
    ), sorted(e for e in edges if "usageFn" in e[1])


def test_func_map_literal_value_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _project(
        temp_repo,
        "func rpad(s string) string { return s }\n"
        'var funcMap = map[string]interface{}{\n\t"rpad": rpad,\n}\n',
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="go")
    edges = _edges(mock_ingestor)
    assert any(t.endswith(".goref.m.rpad") for _f, t in edges), sorted(
        e for e in edges if "rpad" in e[1]
    )


def test_func_slice_literal_value_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _project(
        temp_repo,
        "func handlerA() {}\nfunc wire() []func() {\n\treturn []func(){handlerA}\n}\n",
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="go")
    edges = _edges(mock_ingestor)
    assert any(t.endswith(".goref.m.handlerA") for _f, t in edges), sorted(
        e for e in edges if "handlerA" in e[1]
    )


def test_module_var_assigned_function_value_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # cobra's `var preExecHookFn = preExecHook`: a package-level var bound to a
    # bare function value, invoked later through the var. The assignment
    # references the function even in a file with no calls of its own.
    _project(
        temp_repo,
        "func preExecHook() {}\nvar preExecHookFn = preExecHook\n",
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="go")
    edges = _edges(mock_ingestor)
    assert any(t.endswith(".goref.m.preExecHook") for _f, t in edges), sorted(
        e for e in edges if "preExecHook" in e[1]
    )


def test_local_short_var_assigned_function_value_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `hook := preExecHook` inside a function binds the function to a local,
    # then hands it onward; the bind references it (mirrors the module var).
    _project(
        temp_repo,
        "func preExecHook() {}\nfunc use() interface{} {\n\thook := preExecHook\n\treturn hook\n}\n",
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="go")
    edges = _edges(mock_ingestor)
    assert any(t.endswith(".goref.m.preExecHook") for _f, t in edges), sorted(
        e for e in edges if "preExecHook" in e[1]
    )
