"""No file tool writes inside `.git`.

Git reads its config and hooks from there and runs the commands they name
(`core.fsmonitor`, a hook), and cgr runs `git ls-files` after every write to
refresh the graph. One MCP `write_file` of `.git/config` therefore ran a
command of the caller's choosing on the user's machine, on any transport.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import config as cgr_config
from codebase_rag import sensitive_paths
from codebase_rag.editing.transaction import EditTransaction, TransactionError
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.sensitive_paths import is_git_internal
from codebase_rag.tools.file_editor import FileEditor
from codebase_rag.tools.file_writer import FileWriter

ORIGINAL_CONFIG = "[core]\n\tbare = false\n"
MARKER = "fsmonitor-ran"


def _payload(root: Path) -> str:
    # The command git would run for `ls-files`: it leaves a marker behind.
    return f'[core]\n\tfsmonitor = "touch {root / MARKER}; false"\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / ".git" / "config").write_text(ORIGINAL_CONFIG, encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "relative",
    [
        ".git",
        ".git/config",
        ".git/hooks/pre-commit",
        "vendor/lib/.git/config",
        ".GIT/config",
        ".Git/hooks/post-checkout",
        ".git./config",
        ".git /config",
    ],
)
def test_git_internals_are_recognised(relative: str, tmp_path: Path) -> None:
    assert is_git_internal(tmp_path / relative, tmp_path)


@pytest.mark.parametrize(
    "relative",
    [".gitignore", ".gitattributes", ".github/workflows/ci.yml", "src/git/x.py"],
)
def test_files_beside_git_are_not(relative: str, tmp_path: Path) -> None:
    assert not is_git_internal(tmp_path / relative, tmp_path)


class TestFileWriter:
    @pytest.mark.parametrize("relative", [".git/config", ".git/hooks/pre-commit"])
    def test_refuses_and_writes_nothing(self, relative: str, repo: Path) -> None:
        before = (repo / relative).read_bytes() if (repo / relative).exists() else None

        result = asyncio.run(FileWriter(str(repo)).create_file(relative, "x"))

        assert not result.success
        assert ".git" in (result.error_message or "")
        after = (repo / relative).read_bytes() if (repo / relative).exists() else None
        assert after == before

    def test_still_writes_beside_git(self, repo: Path) -> None:
        result = asyncio.run(FileWriter(str(repo)).create_file(".gitignore", "*.o\n"))

        assert result.success
        assert (repo / ".gitignore").read_text(encoding="utf-8") == "*.o\n"

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_a_link_into_git_is_refused(self, repo: Path) -> None:
        (repo / "notes.txt").symlink_to(repo / ".git" / "config")

        result = asyncio.run(FileWriter(str(repo)).create_file("notes.txt", "x"))

        assert not result.success
        assert (repo / ".git" / "config").read_text(encoding="utf-8") == ORIGINAL_CONFIG


class TestFileEditor:
    def test_surgical_edit_is_refused(self, repo: Path) -> None:
        result = FileEditor(str(repo)).apply_code_block(
            ".git/config", "bare = false", "bare = true"
        )

        assert not result.success
        assert ".git" in (result.error_message or "")
        assert (repo / ".git" / "config").read_text(encoding="utf-8") == ORIGINAL_CONFIG

    def test_whole_file_edit_is_refused(self, repo: Path) -> None:
        result = asyncio.run(FileEditor(str(repo)).edit_file(".git/config", "x"))

        assert not result.success
        assert (repo / ".git" / "config").read_text(encoding="utf-8") == ORIGINAL_CONFIG


def test_an_edit_transaction_will_not_stage_git_internals(repo: Path) -> None:
    transaction = EditTransaction(repo, record=False)

    with pytest.raises(TransactionError, match=r"\.git"):
        transaction.stage(".git/hooks/pre-commit", "#!/bin/sh\n")
    transaction.stage("app.py", "x = 2\n")


class TestMcpWriteToGitConfig:
    """The reported chain, end to end: MCP writes `.git/config`, then the
    server's own `git ls-files` runs what it names.

    `.git/config` also looks like a credential file, which MCP refuses to
    change on its own account; these tests waive that, so they show `.git`
    stays shut for a user who allowed credential writes."""

    @pytest.fixture(autouse=True)
    def _allow_credential_writes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cgr_config.settings, "ALLOW_SENSITIVE_WRITES", True)

    @pytest.fixture
    def registry(self, repo: Path) -> MCPToolsRegistry:
        return MCPToolsRegistry(
            project_root=str(repo), ingestor=MagicMock(), cypher_gen=MagicMock()
        )

    def test_write_file_is_refused(
        self, registry: MCPToolsRegistry, repo: Path
    ) -> None:
        result = asyncio.run(registry.write_file(".git/config", _payload(repo)))

        assert ".git" in result
        assert (repo / ".git" / "config").read_text(encoding="utf-8") == ORIGINAL_CONFIG

    def test_surgical_replace_is_refused(
        self, registry: MCPToolsRegistry, repo: Path
    ) -> None:
        result = asyncio.run(
            registry.surgical_replace_code(
                ".git/config", "bare = false", "fsmonitor = touch x"
            )
        )

        assert ".git" in result
        assert (repo / ".git" / "config").read_text(encoding="utf-8") == ORIGINAL_CONFIG

    @pytest.mark.skipif(
        shutil.which("git") is None or os.name == "nt", reason="needs git and sh"
    )
    def test_the_refresh_runs_no_planted_command(self, tmp_path: Path) -> None:
        repo = tmp_path / "checkout"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S607
        registry = MCPToolsRegistry(
            project_root=str(repo), ingestor=MagicMock(), cypher_gen=MagicMock()
        )

        asyncio.run(registry.write_file(".git/config", _payload(repo)))
        cgr_config.load_ignore_patterns(repo)

        assert not (repo / MARKER).exists()


class TestRelocatedGitDirectory:
    """`git init --separate-git-dir=meta` and `git worktree add` put a
    `.git` file at the root and keep the config and hooks elsewhere, under a
    name the `.git` check never sees."""

    @pytest.fixture
    def separated(self, tmp_path: Path) -> Path:
        (tmp_path / "git-meta" / "hooks").mkdir(parents=True)
        (tmp_path / "git-meta" / "config").write_text(ORIGINAL_CONFIG, encoding="utf-8")
        (tmp_path / ".git").write_text(
            f"gitdir: {tmp_path / 'git-meta'}\n", encoding="utf-8"
        )
        return tmp_path

    @pytest.mark.parametrize(
        "relative", ["git-meta", "git-meta/config", "git-meta/hooks/pre-commit"]
    )
    def test_its_files_are_git_internals(self, relative: str, separated: Path) -> None:
        assert is_git_internal(separated / relative, separated)

    def test_a_relative_gitdir_counts_too(self, separated: Path) -> None:
        (separated / ".git").write_text("gitdir: git-meta\n", encoding="utf-8")

        assert is_git_internal(separated / "git-meta" / "config", separated)

    def test_a_worktrees_shared_git_directory_counts(self, tmp_path: Path) -> None:
        shared = tmp_path / "meta"
        own = shared / "worktrees" / "wt"
        own.mkdir(parents=True)
        (own / "commondir").write_text("../..\n", encoding="utf-8")
        (tmp_path / ".git").write_text(f"gitdir: {own}\n", encoding="utf-8")

        assert is_git_internal(shared / "config", tmp_path)
        assert is_git_internal(own / "HEAD", tmp_path)

    def test_the_rest_of_the_checkout_is_not(self, separated: Path) -> None:
        assert not is_git_internal(separated / "git-metadata.txt", separated)
        assert not is_git_internal(separated / "src" / "app.py", separated)

    def test_mcp_will_not_plant_a_command_there(
        self, separated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cgr_config.settings, "ALLOW_SENSITIVE_WRITES", True)
        registry = MCPToolsRegistry(
            project_root=str(separated), ingestor=MagicMock(), cypher_gen=MagicMock()
        )

        result = asyncio.run(
            registry.write_file("git-meta/config", _payload(separated))
        )

        assert ".git" in result
        assert (separated / "git-meta" / "config").read_text(
            encoding="utf-8"
        ) == ORIGINAL_CONFIG

    @pytest.mark.skipif(
        shutil.which("git") is None or os.name == "nt", reason="needs git and sh"
    )
    def test_with_a_real_separate_git_dir(self, tmp_path: Path) -> None:
        repo = tmp_path / "checkout"
        repo.mkdir()
        # Relative to the working directory, so the git directory lands
        # inside the checkout, where a file tool can reach it.
        subprocess.run(
            ["git", "init", "-q", "--separate-git-dir=git-meta", "."],  # noqa: S607
            check=True,
            cwd=repo,
        )
        assert (repo / "git-meta" / "config").is_file()
        registry = MCPToolsRegistry(
            project_root=str(repo), ingestor=MagicMock(), cypher_gen=MagicMock()
        )

        asyncio.run(registry.write_file("git-meta/config", _payload(repo)))
        cgr_config.load_ignore_patterns(repo)

        assert not (repo / MARKER).exists()


needs_git = pytest.mark.skipif(
    shutil.which("git") is None or os.name == "nt", reason="needs git and sh"
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], check=True, cwd=cwd, capture_output=True)  # noqa: S607


@needs_git
class TestAskingGit:
    """Git itself is asked which git directory it uses for the folder being
    written, so every layout it supports is covered, not only the ones the
    `.git`-file rules know."""

    @pytest.fixture
    def nested(self, tmp_path: Path) -> Path:
        # A project whose vendored dependency is its own repository, with its
        # git directory moved beside it under another name.
        root = tmp_path / "project"
        root.mkdir()
        _git("init", "-q", ".", cwd=root)
        lib = root / "vendor" / "lib"
        lib.mkdir(parents=True)
        _git("init", "-q", "--separate-git-dir=../../meta-lib", ".", cwd=lib)
        assert (root / "meta-lib" / "config").is_file()
        return root

    @pytest.mark.parametrize(
        "relative",
        ["meta-lib/config", "meta-lib/hooks/pre-commit", "meta-lib/info/new/x"],
    )
    def test_a_nested_repositorys_moved_git_directory_is_protected(
        self, relative: str, nested: Path
    ) -> None:
        assert is_git_internal(nested / relative, nested)

    @pytest.mark.parametrize(
        "relative", ["vendor/lib/app.py", "src/app.py", "meta-lib-notes.txt"]
    )
    def test_the_files_around_it_are_not(self, relative: str, nested: Path) -> None:
        assert not is_git_internal(nested / relative, nested)

    def test_mcp_will_not_plant_a_command_there(
        self, nested: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cgr_config.settings, "ALLOW_SENSITIVE_WRITES", True)
        before = (nested / "meta-lib" / "config").read_text(encoding="utf-8")
        registry = MCPToolsRegistry(
            project_root=str(nested), ingestor=MagicMock(), cypher_gen=MagicMock()
        )

        result = asyncio.run(registry.write_file("meta-lib/config", _payload(nested)))

        assert ".git" in result
        assert (nested / "meta-lib" / "config").read_text(encoding="utf-8") == before

    def test_asking_runs_no_command_the_config_names(self, tmp_path: Path) -> None:
        _git("init", "-q", ".", cwd=tmp_path)
        _git(
            "config",
            "core.fsmonitor",
            f"touch {tmp_path / MARKER}; false",
            cwd=tmp_path,
        )

        assert not is_git_internal(tmp_path / "src" / "app.py", tmp_path)
        assert not (tmp_path / MARKER).exists()

    def test_a_repository_above_the_project_is_not_asked_about(
        self, tmp_path: Path
    ) -> None:
        _git("init", "-q", ".", cwd=tmp_path)
        project = tmp_path / "project"
        project.mkdir()

        assert sensitive_paths._git_reported_dirs(project / "app.py", project) == []


def test_without_git_the_root_rules_still_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))
    (tmp_path / "git-meta").mkdir()
    (tmp_path / ".git").write_text("gitdir: git-meta\n", encoding="utf-8")

    assert is_git_internal(tmp_path / ".git" / "config", tmp_path)
    assert is_git_internal(tmp_path / "git-meta" / "config", tmp_path)
    assert not is_git_internal(tmp_path / "src" / "app.py", tmp_path)
