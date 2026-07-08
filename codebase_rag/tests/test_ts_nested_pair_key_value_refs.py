# (H) An object-literal function value nested under ANOTHER pair-arrow
# (H) (`configure({ onCreated: (state) => { state.setEvents({ compute: (e, s) =>
# (H) {...} }) } })`, zustand's demo Scene) registers under the pair-key PATH
# (H) (Canvas.onCreated.compute), but the dispatch-table scan built its named
# (H) candidate from the caller scope alone (Canvas.compute) -- a miss, so the
# (H) nested handler reported dead. The candidate must include the ancestor pair
# (H) keys between the value and the scanning caller.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_pair_value_nested_in_pair_arrow_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "znest"
    root.mkdir(parents=True)
    (root / "Scene.jsx").write_text(
        "function Canvas(props) {\n"
        "  createRoot(null).configure({\n"
        "    onCreated: (state) => {\n"
        "      state.setEvents({\n"
        "        compute: (event, s) => {\n"
        "          return s.pointer\n"
        "        },\n"
        "      })\n"
        "    },\n"
        "  })\n"
        "  return null\n"
        "}\n"
        "export default Canvas\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "REFERENCES")
    }
    assert any(t.endswith(".onCreated.compute") for _, t in refs), sorted(
        t for _, t in refs if "compute" in t or "onCreated" in t
    )
