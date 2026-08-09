"""A symlink planted inside an analyzed repository must not let AstGrepService
read or write files outside the configured project root (GHSA-85gg-2gfq-q95m).

The containment check in should_skip_path was lexical: the unresolved path was
compared against the project root, so a symlinked file whose target lives
outside the root passed the check and structural_search returned its content
while structural_replace(dry_run=False) overwrote it in place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.tools.ast_grep_service import AstGrepService
from codebase_rag.utils.path_utils import should_skip_path

SECRET_SOURCE = 'API_TOKEN = "sk-live-not-a-real-secret"\n'


@pytest.fixture
def escape_layout(tmp_path: Path) -> tuple[Path, Path]:
    """A project root containing a symlink to a python file outside it."""
    outside = tmp_path / "outside-secret.py"
    outside.write_text(SECRET_SOURCE, encoding="utf-8")
    root = tmp_path / "safe-project-root"
    root.mkdir()
    (root / "linked_module.py").symlink_to(outside)
    (root / "honest_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root, outside


class TestShouldSkipPathSymlinks:
    def test_symlink_resolving_outside_root_is_skipped(
        self, escape_layout: tuple[Path, Path]
    ) -> None:
        root, _ = escape_layout
        assert should_skip_path(root / "linked_module.py", root, is_file=True)

    def test_symlink_resolving_inside_root_is_kept(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "real.py"
        target.write_text("VALUE = 1\n", encoding="utf-8")
        link = root / "alias.py"
        link.symlink_to(target)
        assert not should_skip_path(link, root, is_file=True)

    def test_regular_file_inside_root_is_kept(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "real.py"
        target.write_text("VALUE = 1\n", encoding="utf-8")
        assert not should_skip_path(target, root, is_file=True)


class TestAstGrepServiceSymlinkEscape:
    def test_search_does_not_follow_symlink_outside_root(
        self, escape_layout: tuple[Path, Path]
    ) -> None:
        root, _ = escape_layout
        service = AstGrepService(project_root=str(root))
        matches = service.search(pattern="API_TOKEN", language="python")
        assert all("linked_module" not in str(m) for m in matches)

    def test_replace_does_not_modify_file_outside_root(
        self, escape_layout: tuple[Path, Path]
    ) -> None:
        root, outside = escape_layout
        service = AstGrepService(project_root=str(root))
        service.replace(
            pattern="API_TOKEN",
            rewrite="PWNED",
            language="python",
            dry_run=False,
        )
        assert outside.read_text(encoding="utf-8") == SECRET_SOURCE

    def test_search_still_finds_in_root_file(
        self, escape_layout: tuple[Path, Path]
    ) -> None:
        root, _ = escape_layout
        (root / "honest_module.py").write_text(
            'API_TOKEN = "inside"\n', encoding="utf-8"
        )
        service = AstGrepService(project_root=str(root))
        matches = service.search(pattern="API_TOKEN", language="python")
        assert any("honest_module" in str(m) for m in matches)
