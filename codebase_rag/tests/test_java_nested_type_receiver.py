# A receiver typed as a NESTED class referenced by its simple name (gson's
# `RECORD_HELPER` field, typed by the nested `RecordHelper`) failed to resolve:
# `_resolve_java_type_name("RecordHelper", module)` only tried `module.RecordHelper`
# (module.Type), never `module.Outer.RecordHelper` (module.Enclosing.Nested), so the
# method call dropped and the whole nested hierarchy looked dead.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _project(temp_repo: Path, body: str) -> Path:
    p = temp_repo / "jnest"
    (p / "com" / "example").mkdir(parents=True)
    (p / "com" / "example" / "M.java").write_text(
        f"package com.example;\n{body}\n", encoding="utf-8"
    )
    return p


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }


def test_nested_class_typed_field_receiver_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _project(
        temp_repo,
        "public class M {\n"
        "  private static final Helper HELPER = new SubHelper();\n"
        "  public static boolean ok(int x) { return HELPER.check(x); }\n"
        "  private abstract static class Helper {\n"
        "    abstract boolean check(int x);\n"
        "  }\n"
        "  private static class SubHelper extends Helper {\n"
        "    @Override boolean check(int x) { return x > 0; }\n"
        "  }\n"
        "}\n",
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="java")
    calls = _calls(mock_ingestor)
    # HELPER is typed by the nested Helper, so HELPER.check must resolve to the
    # nested Helper.check -- not drop.
    assert any(
        f.endswith(".M.ok(int)") and t.endswith(".M.Helper.check(int)")
        for f, t in calls
    ), calls
