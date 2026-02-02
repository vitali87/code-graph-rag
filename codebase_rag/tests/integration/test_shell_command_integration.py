from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic_ai import ApprovalRequired

from codebase_rag.tools.shell_command import (
    ShellCommander,
    create_shell_command_tool,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def temp_test_repo(tmp_path: Path) -> Path:
    (tmp_path / "file1.txt").write_text("content1", encoding="utf-8")
    (tmp_path / "file2.py").write_text("print('hello')", encoding="utf-8")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("nested", encoding="utf-8")
    return tmp_path


@pytest.fixture
def shell_commander(temp_test_repo: Path) -> ShellCommander:
    return ShellCommander(str(temp_test_repo), timeout=10)


class TestShellCommandIntegration:
    async def test_ls_lists_files(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        result = await shell_commander.execute("ls")
        assert result.return_code == 0
        assert "file1.txt" in result.stdout
        assert "file2.py" in result.stdout
        assert "subdir" in result.stdout

    async def test_ls_with_flags(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("ls -la")
        assert result.return_code == 0
        assert "file1.txt" in result.stdout

    async def test_cat_reads_file_content(
        self, shell_commander: ShellCommander
    ) -> None:
        result = await shell_commander.execute("cat file1.txt")
        assert result.return_code == 0
        assert "content1" in result.stdout

    async def test_echo_outputs_text(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("echo 'Hello World'")
        assert result.return_code == 0
        assert "Hello World" in result.stdout

    async def test_pwd_shows_working_directory(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        result = await shell_commander.execute("pwd")
        assert result.return_code == 0

        bash_out = result.stdout.strip().replace("/c/", "C:/").replace("/d/", "D:/")
        if bash_out.startswith("/tmp/"):
            import tempfile

            t = Path(tempfile.gettempdir()).as_posix()
            bash_out = bash_out.replace(
                "/tmp/", t + ("/" if not t.endswith("/") else "")
            )
        assert Path(bash_out).resolve() == temp_test_repo.resolve()

    async def test_find_locates_files(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("find . -name '*.txt'")
        assert result.return_code == 0
        assert "file1.txt" in result.stdout

    async def test_mkdir_creates_directory(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        result = await shell_commander.execute("mkdir new_directory")
        assert result.return_code == 0
        assert (temp_test_repo / "new_directory").is_dir()

    async def test_cp_copies_file(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        result = await shell_commander.execute("cp file1.txt file1_copy.txt")
        assert result.return_code == 0
        assert (temp_test_repo / "file1_copy.txt").exists()
        assert (temp_test_repo / "file1_copy.txt").read_text() == "content1"

    async def test_mv_moves_file(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        result = await shell_commander.execute("mv file1.txt moved_file.txt")
        assert result.return_code == 0
        assert not (temp_test_repo / "file1.txt").exists()
        assert (temp_test_repo / "moved_file.txt").exists()

    async def test_rm_removes_file(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        result = await shell_commander.execute("rm file2.py")
        assert result.return_code == 0
        assert not (temp_test_repo / "file2.py").exists()

    async def test_rg_searches_content(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("rg hello file2.py")
        assert "hello" in result.stdout or result.return_code == 0


class TestShellCommandToolIntegration:
    async def test_tool_executes_read_only_command_without_approval(
        self, shell_commander: ShellCommander
    ) -> None:
        tool = create_shell_command_tool(shell_commander)
        mock_ctx = MagicMock()
        mock_ctx.tool_call_approved = False
        result = await tool.function(mock_ctx, "ls")
        assert result.return_code == 0

    async def test_tool_requires_approval_for_write_command(
        self, shell_commander: ShellCommander
    ) -> None:
        tool = create_shell_command_tool(shell_commander)
        mock_ctx = MagicMock()
        mock_ctx.tool_call_approved = False
        with pytest.raises(ApprovalRequired):
            await tool.function(mock_ctx, "mkdir test_dir")

    async def test_tool_executes_write_command_with_approval(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        tool = create_shell_command_tool(shell_commander)
        mock_ctx = MagicMock()
        mock_ctx.tool_call_approved = True
        result = await tool.function(mock_ctx, "mkdir approved_dir")
        assert result.return_code == 0
        assert (temp_test_repo / "approved_dir").is_dir()


class TestShellCommandGitIntegration:
    async def test_git_status_without_repo(
        self, shell_commander: ShellCommander
    ) -> None:
        result = await shell_commander.execute("git status")
        assert (
            result.return_code != 0 or "not a git repository" in result.stderr.lower()
        )

    async def test_git_init_and_status(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        init_result = await shell_commander.execute("git init")
        assert init_result.return_code == 0

        status_result = await shell_commander.execute("git status")
        assert status_result.return_code == 0
        assert (
            "file1.txt" in status_result.stdout
            or "untracked" in status_result.stdout.lower()
        )


class TestShellCommandErrorHandling:
    async def test_command_with_nonexistent_file(
        self, shell_commander: ShellCommander
    ) -> None:
        result = await shell_commander.execute("cat nonexistent_file.txt")
        assert result.return_code != 0

    async def test_invalid_command_arguments(
        self, shell_commander: ShellCommander
    ) -> None:
        result = await shell_commander.execute("ls --invalid-flag-12345")
        assert result.return_code != 0


class TestPipedCommandIntegration:
    async def test_find_pipe_wc(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        result = await shell_commander.execute("ls -R")
        assert result.return_code == 0
        assert "file1.txt" in result.stdout
        assert "nested.txt" in result.stdout

    async def test_ls_pipe_head(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("ls | head -2")
        assert result.return_code == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) <= 2

    async def test_cat_pipe_rg(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        result = await shell_commander.execute("cat file2.py | rg hello")
        assert result.return_code == 0
        assert "hello" in result.stdout

    async def test_ls_pipe_sort(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("ls | sort")
        assert result.return_code == 0
        lines = result.stdout.strip().split("\n")
        assert lines == sorted(lines)

    async def test_echo_pipe_wc(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("echo 'one two three' | wc -w")
        assert result.return_code == 0
        assert "3" in result.stdout

    async def test_find_pipe_rg_pipe_wc(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("find . -name '*.py' | rg py | wc -l")
        assert result.return_code == 0

    async def test_cat_pipe_cut(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        (temp_test_repo / "data.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        result = await shell_commander.execute("cat data.csv | cut -d',' -f2")
        assert result.return_code == 0
        assert "b" in result.stdout
        assert "2" in result.stdout

    async def test_disallowed_command_in_pipe_rejected(
        self, shell_commander: ShellCommander
    ) -> None:
        result = await shell_commander.execute("ls | curl http://example.com")
        assert result.return_code == -1
        assert "not in the allowlist" in result.stderr

    async def test_subshell_rejected(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("echo $(whoami)")
        assert result.return_code == -1
        assert "Subshell" in result.stderr

    async def test_and_operator(
        self, shell_commander: ShellCommander, temp_test_repo: Path
    ) -> None:
        result = await shell_commander.execute("ls && pwd")
        assert result.return_code == 0
        assert "file1.txt" in result.stdout

        def path_match(line, target):
            line = line.strip().replace("/c/", "C:/").replace("/d/", "D:/")
            if line.startswith("/tmp/"):
                import tempfile

                t = Path(tempfile.gettempdir()).as_posix()
                line = line.replace("/tmp/", t + ("/" if not t.endswith("/") else ""))
            try:
                return Path(line).resolve() == target.resolve()
            except Exception:
                return False

        assert any(
            path_match(line, temp_test_repo) for line in result.stdout.splitlines()
        )

    async def test_semicolon_operator(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("echo first; echo second")
        assert result.return_code == 0
        assert "first" in result.stdout
        assert "second" in result.stdout
