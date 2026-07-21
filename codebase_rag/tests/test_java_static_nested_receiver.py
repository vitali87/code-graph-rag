# A static-field access on a nested class used as a call receiver
# (`AccessChecker.INSTANCE.canAccess(...)`, gson's ReflectionAccessFilterHelper) did
# not resolve: the field-access chain's base (`AccessChecker`, a nested class
# referenced by simple name) was only tried as `module.AccessChecker`, never the
# nested `module.Outer.AccessChecker`, so the whole call dropped and the method
# looked dead.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }


def test_static_nested_field_receiver_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jstat"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "public class M {\n"
        "  public static boolean check(Object o) {\n"
        "    return Checker.INSTANCE.canAccess(o);\n"
        "  }\n"
        "  static class Checker {\n"
        "    static final Checker INSTANCE = new Checker();\n"
        "    boolean canAccess(Object o) { return true; }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = _calls(mock_ingestor)
    assert any(
        f.endswith(".M.check(Object)") and t.endswith(".Checker.canAccess(Object)")
        for f, t in calls
    ), sorted(t for f, t in calls if "canAccess" in t)
