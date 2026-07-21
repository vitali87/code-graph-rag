# PHP inheritance was entirely missing: cgr emitted neither INHERITS
# (class/interface `extends`) nor IMPLEMENTS (class `implements`). PHP keeps
# extends in a base_clause and implements in a class_interface_clause, holding
# `name` nodes that the parent extractor never read.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import RelationshipType
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_PHP = """\
<?php
namespace App;

interface Shape {}
interface Drawable {}
interface Big extends Shape, Drawable {}

class Base {}
class Circle extends Base implements Shape, Drawable {}
"""


def _pairs(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {
        (call[0][0][2], call[0][2][2]) for call in get_relationships(mock_ingestor, rel)
    }


def test_php_inheritance_and_implements_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "php_inh"
    project.mkdir()
    (project / "lib.php").write_text(_PHP, encoding="utf-8")
    create_and_run_updater(project, mock_ingestor, skip_if_missing="php")

    inherits = _pairs(mock_ingestor, RelationshipType.INHERITS.value)
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "php_inh.lib"

    # class extends -> INHERITS.
    assert (f"{base}.Circle", f"{base}.Base") in inherits, inherits
    # class implements -> IMPLEMENTS to each interface.
    assert (f"{base}.Circle", f"{base}.Shape") in implements, implements
    assert (f"{base}.Circle", f"{base}.Drawable") in implements, implements
    # interface extends -> INHERITS to each superinterface.
    assert (f"{base}.Big", f"{base}.Shape") in inherits, inherits
    assert (f"{base}.Big", f"{base}.Drawable") in inherits, inherits
