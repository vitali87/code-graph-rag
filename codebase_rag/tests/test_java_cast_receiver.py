# (H) A method call on a cast receiver `((T) x).m()` (gson's
# (H) `((JsonTreeReader) in).nextJsonElement()`) dropped: the object extractor ignored
# (H) cast/parenthesized receivers, so the call fell to the unqualified path and never
# (H) resolved m on T (cross-file/sibling T). The cast's target type is the receiver.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_cast_receiver_resolves_cross_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jcast"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    # (H) Target Reader.nextX is in a sibling file; a decoy Other.nextX ensures the
    # (H) unqualified fallback can't coincidentally pick the right one.
    (pkg / "Reader.java").write_text(
        "package com.example;\n"
        "public class Reader { public int nextX() { return 1; } }\n",
        encoding="utf-8",
    )
    (pkg / "Other.java").write_text(
        "package com.example;\n"
        "public class Other { public int nextX() { return 2; } }\n",
        encoding="utf-8",
    )
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "public class M {\n"
        "  int use(Object in) { return ((Reader) in).nextX(); }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(
        f.endswith(".M.use(Object)") and t.endswith(".Reader.nextX()") for f, t in calls
    ), sorted(t for f, t in calls if "nextX" in t)
    assert not any(
        f.endswith(".M.use(Object)") and t.endswith(".Other.nextX()") for f, t in calls
    ), "cast receiver wrongly bound to the decoy"


def test_nested_parenthesized_cast_receiver_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A cast wrapped in multiple parentheses `(((Reader) in)).m()` must still yield
    # (H) the cast target type: parenthesized wrappers are unwrapped to the innermost
    # (H) cast, not just one layer.
    root = temp_repo / "jncast"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "Reader.java").write_text(
        "package com.example;\n"
        "public class Reader { public int nextX() { return 1; } }\n",
        encoding="utf-8",
    )
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "public class M {\n"
        "  int use(Object in) { return (((Reader) in)).nextX(); }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(
        f.endswith(".M.use(Object)") and t.endswith(".Reader.nextX()") for f, t in calls
    ), sorted(t for f, t in calls if "nextX" in t)


def test_parenthesized_identifier_receiver_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A parenthesized non-cast receiver `(reader).nextX()` must resolve through the
    # (H) `reader` variable's type (Reader), not fall to the unqualified resolver and bind
    # (H) a same-named decoy method on the enclosing class.
    root = temp_repo / "jpid"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "Reader.java").write_text(
        "package com.example;\n"
        "public class Reader { public int nextX() { return 1; } }\n",
        encoding="utf-8",
    )
    # (H) M declares a decoy nextX(): without a receiver the call mis-binds to it.
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "public class M {\n"
        "  int nextX() { return 9; }\n"
        "  int use(Reader reader) { return (reader).nextX(); }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(
        f.endswith(".M.use(Reader)") and t.endswith(".Reader.nextX()") for f, t in calls
    ), sorted(t for f, t in calls if "nextX" in t)
    assert not any(
        f.endswith(".M.use(Reader)") and t.endswith(".M.nextX()") for f, t in calls
    ), "parenthesized identifier receiver wrongly bound to the enclosing-class decoy"


def test_qualified_cast_receiver_does_not_bind_to_same_package_decoy(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A fully-qualified cast target `((com.other.Reader) in).m()` must NOT resolve to
    # (H) a same-package `Reader` decoy. Stripping the package off the cast type collapses
    # (H) `com.other.Reader` to `Reader` and binds it to the wrong same-package class;
    # (H) keeping the qualified name prevents that wrong edge.
    root = temp_repo / "jqcast"
    (root / "com" / "other").mkdir(parents=True)
    (root / "com" / "example").mkdir(parents=True)
    (root / "com" / "other" / "Reader.java").write_text(
        "package com.other;\n"
        "public class Reader { public int nextX() { return 1; } }\n",
        encoding="utf-8",
    )
    # (H) same-package decoy Reader: without the qualified type the call mis-binds here.
    (root / "com" / "example" / "Reader.java").write_text(
        "package com.example;\n"
        "public class Reader { public int nextX() { return 2; } }\n",
        encoding="utf-8",
    )
    (root / "com" / "example" / "M.java").write_text(
        "package com.example;\n"
        "public class M {\n"
        "  int use(Object in) { return ((com.other.Reader) in).nextX(); }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert not any(
        f.endswith(".M.use(Object)") and t.endswith(".example.Reader.nextX()")
        for f, t in calls
    ), "qualified cast receiver wrongly bound to the same-package decoy"
