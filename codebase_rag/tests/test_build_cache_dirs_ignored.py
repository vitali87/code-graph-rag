# Android's NDK build cache (.cxx) and Dart's tool cache (.dart_tool) are
# generated build output, but neither was in IGNORE_PATTERNS: a Flutter app
# that committed its .cxx directory got 12 CMake compiler-probe `main`
# functions indexed as project code and reported by dead-code.
# The two caches are asserted in separate tests so each one depends only on
# its own parser: a .cxx regression must still fail where the optional Dart
# parser is absent.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_node_names


def test_android_ndk_build_cache_is_not_indexed(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "app"
    (root / ".cxx" / "CMakeFiles").mkdir(parents=True)
    (root / ".cxx" / "CMakeFiles" / "CMakeCCompilerId.c").write_text(
        "int main(void) { return 0; }\n", encoding="utf-8"
    )
    (root / "src").mkdir()
    (root / "src" / "native.c").write_text(
        "int add(int a, int b) { return a + b; }\n", encoding="utf-8"
    )

    create_and_run_updater(root, mock_ingestor, skip_if_missing="c")

    modules = get_node_names(mock_ingestor, "Module")
    assert any(".src.native" in m for m in modules), modules
    assert not any(".cxx" in m for m in modules), modules


def test_dart_tool_cache_is_not_indexed(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "app"
    (root / ".dart_tool").mkdir(parents=True)
    (root / ".dart_tool" / "generated.dart").write_text(
        "void tick() {}\n", encoding="utf-8"
    )
    (root / "lib").mkdir()
    (root / "lib" / "main.dart").write_text("void main() {}\n", encoding="utf-8")

    create_and_run_updater(root, mock_ingestor, skip_if_missing="dart")

    modules = get_node_names(mock_ingestor, "Module")
    assert any(".lib.main" in m for m in modules), modules
    assert not any("dart_tool" in m for m in modules), modules
