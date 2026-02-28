from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.class_ingest.node_type import determine_node_type
from codebase_rag.tests.conftest import (
    create_mock_node,
    get_node_names,
    run_updater,
)
from codebase_rag.types_defs import NodeType


@pytest.mark.parametrize(
    ("ts_node_type", "expected"),
    [
        (cs.TS_RS_ENUM_ITEM, NodeType.ENUM),
        (cs.TS_RS_TRAIT_ITEM, NodeType.INTERFACE),
        (cs.TS_RS_TYPE_ITEM, NodeType.TYPE),
        (cs.TS_RS_UNION_ITEM, NodeType.UNION),
        (cs.TS_RS_STRUCT_ITEM, NodeType.CLASS),
    ],
)
def test_determine_node_type_rust(ts_node_type: str, expected: NodeType) -> None:
    node = create_mock_node(ts_node_type)
    result = determine_node_type(node, "Foo", "crate::Foo", cs.SupportedLanguage.RUST)
    assert result == expected


@pytest.fixture
def rust_node_type_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "rust_node_type_test"
    project_path.mkdir()
    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data='[package]\nname = "rust_node_type_test"\nversion = "0.1.0"\n',
    )
    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(encoding="utf-8", data="")
    (project_path / "types.rs").write_text(
        encoding="utf-8",
        data=(
            "pub enum Color { Red, Green, Blue }\n"
            "pub trait Drawable { fn draw(&self); }\n"
            "pub type Pair = (i32, i32);\n"
            "pub union IntOrFloat { i: i32, f: f32 }\n"
            "pub struct Point { pub x: f64, pub y: f64 }\n"
        ),
    )
    return project_path


def test_rust_enum_label(
    rust_node_type_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(rust_node_type_project, mock_ingestor, skip_if_missing="rust")
    enum_names = get_node_names(mock_ingestor, NodeType.ENUM)
    assert len(enum_names) == 1
    assert enum_names.pop().endswith(".Color")


def test_rust_trait_label(
    rust_node_type_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(rust_node_type_project, mock_ingestor, skip_if_missing="rust")
    interface_names = get_node_names(mock_ingestor, NodeType.INTERFACE)
    assert len(interface_names) == 1
    assert interface_names.pop().endswith(".Drawable")


def test_rust_type_alias_label(
    rust_node_type_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(rust_node_type_project, mock_ingestor, skip_if_missing="rust")
    type_names = get_node_names(mock_ingestor, NodeType.TYPE)
    assert len(type_names) == 1
    assert type_names.pop().endswith(".Pair")


def test_rust_union_label(
    rust_node_type_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(rust_node_type_project, mock_ingestor, skip_if_missing="rust")
    union_names = get_node_names(mock_ingestor, NodeType.UNION)
    assert len(union_names) == 1
    assert union_names.pop().endswith(".IntOrFloat")


def test_rust_struct_label(
    rust_node_type_project: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(rust_node_type_project, mock_ingestor, skip_if_missing="rust")
    class_names = get_node_names(mock_ingestor, NodeType.CLASS)
    assert len(class_names) == 1
    assert class_names.pop().endswith(".Point")
