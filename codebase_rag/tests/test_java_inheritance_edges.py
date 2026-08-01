# Java inheritance edges. cgr captured a class's `extends`/`implements` but
# missed two cases: an interface's `extends` superinterfaces (-> INHERITS)
# and an enum's `implements` interfaces (-> IMPLEMENTS). Both clauses carry a
# type_list of interface names that were never extracted.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import RelationshipType
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_JAVA = """\
package demo;

public interface A {}
public interface B {}
public interface Big extends A, B {}

abstract class Base {}
enum Color implements A { RED }

class Circle extends Base implements A, B {}

class Box<T> {}
interface Comparable<T> {}

class Holder extends Box<String> implements Comparable<Holder> {}
"""


def _pairs(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    # (source_qn, target_qn) for the given relationship.
    return {
        (call[0][0][2], call[0][2][2]) for call in get_relationships(mock_ingestor, rel)
    }


def test_java_inheritance_and_implements_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "java_inh"
    project.mkdir()
    (project / "Demo.java").write_text(_JAVA, encoding="utf-8")
    create_and_run_updater(project, mock_ingestor, skip_if_missing="java")

    inherits = _pairs(mock_ingestor, RelationshipType.INHERITS.value)
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "java_inh.Demo"

    # Interface extends -> INHERITS to each superinterface.
    assert (f"{base}.Big", f"{base}.A") in inherits, inherits
    assert (f"{base}.Big", f"{base}.B") in inherits, inherits
    # Enum implements -> IMPLEMENTS.
    assert (f"{base}.Color", f"{base}.A") in implements, implements
    # Class extends/implements (already worked) stay intact.
    assert (f"{base}.Circle", f"{base}.Base") in inherits, inherits
    assert (f"{base}.Circle", f"{base}.A") in implements, implements
    assert (f"{base}.Circle", f"{base}.B") in implements, implements
    # Generic (parameterized) bases must be captured by their base type.
    assert (f"{base}.Holder", f"{base}.Box") in inherits, inherits
    assert (f"{base}.Holder", f"{base}.Comparable") in implements, implements
