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
