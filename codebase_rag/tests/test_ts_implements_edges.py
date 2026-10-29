# TypeScript class `implements` was dropped: cgr captured `extends`
# (-> INHERITS) via class_heritage but never the `implements_clause`, so a
# class implementing interfaces produced no IMPLEMENTS edges.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import RelationshipType
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_TS = """\
export interface Shape {}
export interface Drawable {}
export class Base {}
export class Circle extends Base implements Shape, Drawable {}
"""


def _pairs(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {
        (call[0][0][2], call[0][2][2]) for call in get_relationships(mock_ingestor, rel)
    }


def test_typescript_class_implements_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "ts_impl"
    project.mkdir()
    (project / "lib.ts").write_text(_TS, encoding="utf-8")
    create_and_run_updater(project, mock_ingestor, skip_if_missing="typescript")

    inherits = _pairs(mock_ingestor, RelationshipType.INHERITS.value)
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "ts_impl.lib"

    # extends still works.
    assert (f"{base}.Circle", f"{base}.Base") in inherits, inherits
    # implements must now produce IMPLEMENTS to each interface.
    assert (f"{base}.Circle", f"{base}.Shape") in implements, implements
    assert (f"{base}.Circle", f"{base}.Drawable") in implements, implements
