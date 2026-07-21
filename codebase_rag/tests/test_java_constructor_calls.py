# Java collected only `method_invocation` as call nodes, never
# `object_creation_expression`, so `new X(...)` produced no INSTANTIATES edge to the
# class and no CALLS edge to the constructor. Every constructor reached only via
# `new` therefore looked dead (45 of gson's 114 false positives). A `new X(...)` must
# instantiate the class and call its constructor, mirroring Python/JS class calls.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _project(temp_repo: Path, body: str) -> Path:
    p = temp_repo / "jctor"
    (p / "com" / "example").mkdir(parents=True)
    (p / "com" / "example" / "M.java").write_text(
        f"package com.example;\n{body}\n", encoding="utf-8"
    )
    return p


def _edges(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {(c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel)}


def test_new_expression_calls_constructor_and_instantiates(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _project(
        temp_repo,
        "class Widget {\n"
        "  Widget(int x) { }\n"
        "}\n"
        "class Factory {\n"
        "  Widget make() { return new Widget(5); }\n"
        "}\n",
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="java")
    calls = _edges(mock_ingestor, "CALLS")
    insts = _edges(mock_ingestor, "INSTANTIATES")
    assert any(
        f.endswith(".Factory.make()") and t.endswith(".Widget.Widget(int)")
        for f, t in calls
    ), calls
    assert any(
        f.endswith(".Factory.make()") and t.endswith(".Widget") for f, t in insts
    ), insts


def test_new_expression_reaches_overloaded_constructors(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Overload resolution by argument type is not attempted; for reachability a
    # `new X(...)` reaches every declared constructor of X so none is reported dead.
    _project(
        temp_repo,
        "class Box {\n"
        "  Box() { }\n"
        "  Box(int x) { }\n"
        "}\n"
        "class User {\n"
        "  Box build() { return new Box(1); }\n"
        "}\n",
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="java")
    calls = _edges(mock_ingestor, "CALLS")
    assert any(
        f.endswith(".User.build()") and t.endswith(".Box.Box()") for f, t in calls
    )
    assert any(
        f.endswith(".User.build()") and t.endswith(".Box.Box(int)") for f, t in calls
    )


def test_generic_new_expression_reaches_constructor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `new MyList<String>()`: the type field is a `generic_type`, not a plain
    # `type_identifier`, so the creation query must still capture it (generic args
    # are stripped to the base name) or the constructor looks dead.
    _project(
        temp_repo,
        "class MyList<T> {\n"
        "  MyList() { }\n"
        "}\n"
        "class User {\n"
        "  MyList<String> build() { return new MyList<String>(); }\n"
        "}\n",
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="java")
    calls = _edges(mock_ingestor, "CALLS")
    insts = _edges(mock_ingestor, "INSTANTIATES")
    assert any(
        f.endswith(".User.build()") and t.endswith(".MyList.MyList()") for f, t in calls
    ), calls
    assert any(f.endswith(".User.build()") and t.endswith(".MyList") for f, t in insts)


def test_scoped_new_expression_reaches_nested_constructor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `new Outer.Inner()`: the type field is a `scoped_type_identifier`, which the
    # creation query must capture so the nested class's constructor is reached.
    _project(
        temp_repo,
        "class Outer {\n"
        "  static class Inner {\n"
        "    Inner() { }\n"
        "  }\n"
        "}\n"
        "class User {\n"
        "  Outer.Inner build() { return new Outer.Inner(); }\n"
        "}\n",
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="java")
    insts = _edges(mock_ingestor, "INSTANTIATES")
    assert any(
        f.endswith(".User.build()") and t.endswith(".Outer.Inner") for f, t in insts
    ), insts
