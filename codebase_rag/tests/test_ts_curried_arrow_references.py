# (H) A curried arrow's inner function (`const persistImpl = (config) =>
# (H) (set, get, api) => {...}`, zustand's middleware shape) is the outer's implicit
# (H) return value, but the returned-function reference pass only walked
# (H) `return_statement`s -- an expression-bodied arrow has none, so the inner arrow
# (H) node got a DEFINES edge and nothing else, orphaning it (false dead) even when
# (H) the outer is reachable. The expression body IS the return: reference it.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _refs(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "REFERENCES")
    }


def test_curried_inner_arrow_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "zcurry"
    root.mkdir(parents=True)
    (root / "mw.ts").write_text(
        "const persistImpl = (config: any) => (set: any, get: any, api: any) => {\n"
        "  return config\n"
        "}\n"
        "export const persist = persistImpl as unknown\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(
        f.endswith(".persistImpl") and ".persistImpl.anonymous_" in t for f, t in refs
    ), sorted(t for f, t in refs if "persistImpl" in t)


def test_double_curried_inner_arrows_are_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Three-deep currying `(a) => (b) => (c) => {...}`: the middle arrow is
    # (H) anonymous (no caller pass of its own), so the innermost must be referenced
    # (H) too -- the walk bubbles both to the nearest named scope.
    root = temp_repo / "zcurry2"
    root.mkdir(parents=True)
    (root / "mw.ts").write_text(
        "export const chain = (a: any) => (b: any) => (c: any) => {\n"
        "  return a + b + c\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    inner_refs = {t for f, t in refs if f.endswith(".chain") and ".anonymous_" in t}
    assert len(inner_refs) >= 2, sorted(refs)
