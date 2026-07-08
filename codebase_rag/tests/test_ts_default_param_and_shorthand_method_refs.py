# (H) Two zustand residuals with the same root pattern -- a function consumed by
# (H) syntax the call graph didn't scan:
# (H) 1. a DEFAULT PARAMETER value (`useStore(api, selector = identity as any)`)
# (H)    references the default function; nothing scanned parameter defaults.
# (H) 2. an object-literal SHORTHAND METHOD (`return { then(x) {...}, catch(x)
# (H)    {...} }`, persist's thenable) -- the dispatch-table scan only walked
# (H)    `pair` values, so shorthand methods of a returned/stored object were
# (H)    never referenced and reported dead unless explicitly called in-repo.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _refs(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "REFERENCES")
    }


def test_default_param_function_value_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "zdef"
    root.mkdir(parents=True)
    (root / "react.ts").write_text(
        "const identity = (arg: any) => arg\n"
        "export function useStore(api: any, selector: any = identity as any) {\n"
        "  return selector(api.getState())\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(f.endswith(".useStore") and t.endswith(".identity") for f, t in refs), (
        sorted(t for _, t in refs if "identity" in t)
    )


def test_js_default_param_function_value_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Plain-JS grammar wraps a defaulted parameter in an assignment_pattern
    # (H) whose default sits under `right`, unlike TS's required_parameter `value`;
    # (H) both forms must be scanned.
    root = temp_repo / "zdefjs"
    root.mkdir(parents=True)
    (root / "react.js").write_text(
        "const identity = (arg) => arg\n"
        "export function useStore(api, selector = identity) {\n"
        "  return selector(api.getState())\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(f.endswith(".useStore") and t.endswith(".identity") for f, t in refs), (
        sorted(t for _, t in refs if "identity" in t)
    )


def test_object_literal_shorthand_method_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "zshort"
    root.mkdir(parents=True)
    (root / "thenable.ts").write_text(
        "export const toThenable = (fn: any) => (input: any) => {\n"
        "  const result = fn(input)\n"
        "  return {\n"
        "    then(onFulfilled: any) {\n"
        "      return toThenable(onFulfilled)(result)\n"
        "    },\n"
        "    catch(_onRejected: any) {\n"
        "      return this\n"
        "    },\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(t.endswith(".catch") for _, t in refs), sorted(
        t for _, t in refs if "catch" in t or "then" in t
    )
