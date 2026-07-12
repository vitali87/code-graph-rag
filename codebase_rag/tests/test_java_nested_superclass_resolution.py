# (H) When several files each declare a same-named nested class (gson has
# (H) CollectionTypeAdapterFactory.Adapter, MapTypeAdapterFactory.Adapter,
# (H) ReflectiveTypeAdapterFactory.Adapter), a subclass `class Sub extends Adapter`
# (H) must inherit from the Adapter nested in ITS OWN file, not a same-named one in
# (H) another file that merely sorts first. A wrong INHERITS sends the method-override
# (H) edges to the wrong base, so the subclass overrides look dead.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _edges(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {(c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel)}


def test_nested_superclass_prefers_same_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jsup"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    # (H) Two files each declare a nested `Adapter`. The subclass in Reflective.java
    # (H) must inherit from Reflective's Adapter, not Collection's (which sorts first).
    (pkg / "Collection.java").write_text(
        "package com.example;\n"
        "public class Collection {\n"
        "  abstract static class Adapter { abstract int make(); }\n"
        "}\n",
        encoding="utf-8",
    )
    (pkg / "Reflective.java").write_text(
        "package com.example;\n"
        "public class Reflective {\n"
        "  abstract static class Adapter { abstract int make(); }\n"
        "  static final class Sub extends Adapter {\n"
        "    @Override int make() { return 1; }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    inherits = _edges(mock_ingestor, "INHERITS")
    assert any(
        f.endswith(".Reflective.Sub") and t.endswith(".Reflective.Adapter")
        for f, t in inherits
    ), inherits
    assert not any(
        f.endswith(".Reflective.Sub") and t.endswith(".Collection.Adapter")
        for f, t in inherits
    ), "Sub wrongly inherited the other file's Adapter"
