# (H) An inline function passed as a call argument was referenced only when the
# (H) callee resolved to a registered first-party callable (callable-params path).
# (H) An external/unresolvable callee -- a parameter (`create((set) => ({ inc: () =>
# (H) set((state) => ...) }))`, zustand's store shape) or a cast-wrapped callee
# (H) (`;(set as NamedSet<S>)((state) => reducer(state, action), ...)`) -- consumed
# (H) the arrow invisibly, so the registered anonymous node reported dead. Passing a
# (H) function to ANY call hands it over: reference it from the calling scope.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _refs(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "REFERENCES")
    }


def test_inline_arrow_arg_to_param_callee_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "zarg"
    root.mkdir(parents=True)
    (root / "App.jsx").write_text(
        "import { create } from 'zustand'\n"
        "const useStore = create((set) => ({\n"
        "  count: 1,\n"
        "  inc: () => set((state) => ({ count: state.count + 1 })),\n"
        "}))\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(t.endswith(".App.anonymous_3_17") for _, t in refs), sorted(
        t for _, t in refs if "anonymous" in t
    )


def test_inline_arrow_arg_to_cast_wrapped_callee_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "zarg2"
    root.mkdir(parents=True)
    (root / "mw.ts").write_text(
        "type NamedSet = unknown\n"
        "const reducer = (state: any, action: any) => state\n"
        "const reduxImpl = (initial: any) => (set: any, get: any, api: any) => {\n"
        "  ;(api as any).dispatch = (action: any) => {\n"
        "    ;(set as NamedSet)((state: any) => reducer(state, action), false, action)\n"
        "    return action\n"
        "  }\n"
        "  return initial\n"
        "}\n"
        "export const redux = reduxImpl as unknown\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(".reduxImpl.anonymous_4_" in t for _, t in refs), sorted(
        t for _, t in refs if "reduxImpl" in t
    )
