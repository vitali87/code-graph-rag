# A function declared inside an anonymous callback must be DEFINEd by that
# callback (its lexical parent), not the nearest named ancestor. The child's qn
# omits anonymous scopes, so trimming it to derive the DEFINES parent skipped the
# callback; the parent is now recomputed from the enclosing function node.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import RelationshipType
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_TS = """\
export function driver(client) {
    test("x", function (assert) {
        function inner(fn) {
            return 1;
        }
        return inner;
    });
}
"""


def test_function_in_anonymous_callback_defined_by_callback(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "ts_closure"
    project.mkdir()
    (project / "m.ts").write_text(_TS, encoding="utf-8")
    create_and_run_updater(project, mock_ingestor, skip_if_missing="typescript")

    # (parent_qn, child_qn) for DEFINES edges into `inner`.
    parents = {
        call[0][0][2]
        for call in get_relationships(mock_ingestor, RelationshipType.DEFINES.value)
        if str(call[0][2][2]).endswith(".inner")
    }
    assert parents, "no DEFINES edge into inner"
    # The parent must be the anonymous callback, not the named driver.
    assert all("anonymous" in p for p in parents), parents
    assert "ts_closure.m.driver" not in parents, parents
