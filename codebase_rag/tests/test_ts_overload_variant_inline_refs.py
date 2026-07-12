# (H) A TS function with OVERLOAD signatures registers its implementation under a
# (H) duplicate-variant qn (`useStore@27`, zustand's react.ts), but its inline
# (H) function arguments register under the NATURAL qn (`useStore.anonymous_31_22`).
# (H) The by-position reference candidates were built from the variant caller qn
# (H) only, so the anons never matched and reported dead. Candidates must include
# (H) the variant-stripped scope.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_overload_impl_inline_arg_arrows_are_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "zovl"
    root.mkdir(parents=True)
    (root / "react.ts").write_text(
        "import React from 'react'\n"
        "export function useStore(api: any): any\n"
        "export function useStore(api: any, selector: any): any\n"
        "export function useStore(api: any, selector?: any) {\n"
        "  return React.useSyncExternalStore(\n"
        "    api.subscribe,\n"
        "    React.useCallback(() => selector(api.getState()), [api, selector]),\n"
        "  )\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "REFERENCES")
    }
    assert any(".useStore.anonymous_6_" in t for _, t in refs), sorted(
        t for _, t in refs if "useStore" in t
    )
