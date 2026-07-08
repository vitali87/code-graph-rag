# (H) Java override detection matched the full method signature including generic
# (H) type-variable NAMES, so a subclass that renames a type parameter
# (H) (`Adapter<T,A>` base declares `readField(A,...)`, `FieldReflectionAdapter<T> extends
# (H) Adapter<T,T>` declares `readField(T,...)`) produced no OVERRIDES edge and its
# (H) override looked dead. Overriding is by name + arity regardless of type-var names.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _edges(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {(c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel)}


def test_generic_type_var_rename_still_overrides(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jgen"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "public class M {\n"
        "  abstract static class Base<T, A> {\n"
        "    abstract A readField(A acc, int in);\n"
        "  }\n"
        "  static final class Impl<T> extends Base<T, T> {\n"
        "    @Override T readField(T acc, int in) { return acc; }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    overrides = _edges(mock_ingestor, "OVERRIDES")
    # (H) Impl.readField(T,int) overrides Base.readField(A,int) despite the type-var
    # (H) rename (A -> T); matched by name + arity.
    assert any(
        f.endswith(".Impl.readField(T,int)") and t.endswith(".Base.readField(A,int)")
        for f, t in overrides
    ), overrides
