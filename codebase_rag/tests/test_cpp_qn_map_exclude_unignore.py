"""Regression tests for the C++ module-qn map's walk filters (issue #1099).

``build_module_qn_map`` reproduces ``GraphUpdater._collect_eligible_files`` so
the libclang path synthesises qualified names byte-identical to the tree-sitter
pass. It used to walk without ``--exclude``/``.cgrignore``, so the two saw
different file sets and disagreed on the qn of any file caught by a basename
collision.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.cpp_frontend.qn import build_module_qn_map

PROJECT = "proj"


def _write(repo: Path, rel: str, body: str = "int x;\n") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _indexer_rel_files(
    repo: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> set[str]:
    """The exact file set the tree-sitter pass would index."""
    updater = GraphUpdater.__new__(GraphUpdater)
    updater.repo_path = repo
    updater.exclude_paths = exclude_paths
    updater.unignore_paths = unignore_paths
    updater._single_file = None
    return {key for _path, key in updater._collect_eligible_files()}


class TestExcludedFilesDoNotClaimQns:
    def test_excluded_collision_partner_leaves_the_base_qn_free(
        self, tmp_path: Path
    ) -> None:
        # foo.cpp sorts before foo.h, so it claims proj.foo and pushes foo.h
        # onto proj.foo.h. Excluding foo.cpp must hand proj.foo back to foo.h,
        # which is what the tree-sitter pass keys it as.
        _write(tmp_path, "foo.cpp")
        _write(tmp_path, "foo.h")

        qn_map = build_module_qn_map(
            tmp_path, PROJECT, exclude_paths=frozenset({"foo.cpp"})
        )

        assert "foo.cpp" not in qn_map
        assert qn_map["foo.h"] == f"{PROJECT}.foo"

    def test_unexcluded_collision_still_suffixes(self, tmp_path: Path) -> None:
        _write(tmp_path, "foo.cpp")
        _write(tmp_path, "foo.h")

        qn_map = build_module_qn_map(tmp_path, PROJECT)

        assert qn_map["foo.cpp"] == f"{PROJECT}.foo"
        assert qn_map["foo.h"] == f"{PROJECT}.foo.h"

    def test_excluded_directory_is_pruned(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/keep.cpp")
        _write(tmp_path, "vendor/drop.cpp")

        qn_map = build_module_qn_map(
            tmp_path, PROJECT, exclude_paths=frozenset({"vendor"})
        )

        assert "src/keep.cpp" in qn_map
        assert not any(rel.startswith("vendor/") for rel in qn_map)


class TestUnignoreRescuedFilesGetQns:
    def test_rescued_file_is_visible_to_the_map(self, tmp_path: Path) -> None:
        # node_modules is a built-in ignore; a .cgrignore rescue puts it back
        # in the index, so an #include resolving to it needs a qn.
        _write(tmp_path, "src/main.cpp")
        _write(tmp_path, "node_modules/lib/thing.h")

        qn_map = build_module_qn_map(
            tmp_path, PROJECT, unignore_paths=frozenset({"node_modules"})
        )

        assert qn_map["node_modules/lib/thing.h"] == f"{PROJECT}.node_modules.lib.thing"

    def test_without_the_rescue_the_file_stays_invisible(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/main.cpp")
        _write(tmp_path, "node_modules/lib/thing.h")

        qn_map = build_module_qn_map(tmp_path, PROJECT)

        assert "node_modules/lib/thing.h" not in qn_map


class TestParityWithTheIndexerWalk:
    def test_same_file_set_under_exclude(self, tmp_path: Path) -> None:
        _write(tmp_path, "foo.cpp")
        _write(tmp_path, "foo.h")
        _write(tmp_path, "src/keep.cpp")
        _write(tmp_path, "vendor/drop.cpp")
        excludes = frozenset({"vendor", "foo.cpp"})

        qn_map = build_module_qn_map(tmp_path, PROJECT, exclude_paths=excludes)

        assert set(qn_map) == _indexer_rel_files(tmp_path, exclude_paths=excludes)

    def test_same_file_set_under_unignore(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/main.cpp")
        _write(tmp_path, "node_modules/lib/thing.h")
        rescues = frozenset({"node_modules"})

        qn_map = build_module_qn_map(tmp_path, PROJECT, unignore_paths=rescues)

        assert set(qn_map) == _indexer_rel_files(tmp_path, unignore_paths=rescues)

    def test_same_file_set_with_no_filters(self, tmp_path: Path) -> None:
        _write(tmp_path, "foo.cpp")
        _write(tmp_path, "foo.h")
        _write(tmp_path, "src/deep/nested.hpp")

        qn_map = build_module_qn_map(tmp_path, PROJECT)

        assert set(qn_map) == _indexer_rel_files(tmp_path)

    def test_state_files_are_skipped_like_the_indexer(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/main.cpp")
        _write(tmp_path, ".cgr-hash-cache.json", "{}")

        qn_map = build_module_qn_map(tmp_path, PROJECT)

        assert ".cgr-hash-cache.json" not in qn_map
        assert set(qn_map) == _indexer_rel_files(tmp_path)
