# A TS arrow bound through a CAST wrapper (`export const createStore = ((s) =>
# s ? createStoreImpl(s) : createStoreImpl) as CreateStore`, zustand's public-API
# shape) lost ALL its body's call/reference edges: `_js_ts_arrow_binding_name`
# required the arrow to be the declarator's DIRECT value, so the cast/paren
# wrapper made it "anonymous", its calls bubbled to module scope, and dropped.
# The def pass already unwraps the cast (registers `createStore`); the call pass
# must agree, so the impl and its inner functions stay reachable.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }


def test_cast_wrapped_arrow_body_calls_attribute_to_binding(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "zcast"
    root.mkdir(parents=True)
    (root / "vanilla.ts").write_text(
        "type CreateStore = unknown\n"
        "const createStoreImpl = (createState: any) => {\n"
        "  const setState = () => {}\n"
        "  return { setState }\n"
        "}\n"
        "export const createStore = ((createState: any) =>\n"
        "  createState ? createStoreImpl(createState) : createStoreImpl) as CreateStore\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    calls = _calls(mock_ingestor)
    assert any(
        f.endswith(".createStore") and t.endswith(".createStoreImpl") for f, t in calls
    ), sorted(t for f, t in calls if "Impl" in t)


def test_double_cast_wrapped_function_expression_binding(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Nested casts (`as unknown as T`) and a function expression instead of an
    # arrow: the unwrap must loop through every wrapper layer.
    root = temp_repo / "zcast2"
    root.mkdir(parents=True)
    (root / "mw.ts").write_text(
        "type Api = unknown\n"
        "const impl = (x: any) => x\n"
        "export const api = (function (x: any) {\n"
        "  return impl(x)\n"
        "}) as unknown as Api\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    calls = _calls(mock_ingestor)
    assert any(f.endswith(".api") and t.endswith(".impl") for f, t in calls), sorted(
        calls
    )
