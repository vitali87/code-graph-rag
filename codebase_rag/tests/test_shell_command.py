from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic_ai import ApprovalRequired, Tool

from codebase_rag.tools.shell_command import (
    COMMAND_ALLOWLIST,
    READ_ONLY_COMMANDS,
    SAFE_GIT_SUBCOMMANDS,
    ShellCommander,
    _is_dangerous_command,
    _requires_approval,
    create_shell_command_tool,
)

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def shell_commander(temp_project_root: Path) -> ShellCommander:
    return ShellCommander(str(temp_project_root), timeout=5)


class TestShellCommanderInit:
    def test_init_resolves_project_root(self, temp_project_root: Path) -> None:
        commander = ShellCommander(str(temp_project_root))
        assert commander.project_root == temp_project_root.resolve()

    def test_init_default_timeout(self, temp_project_root: Path) -> None:
        commander = ShellCommander(str(temp_project_root))
        assert commander.timeout == 30

    def test_init_custom_timeout(self, temp_project_root: Path) -> None:
        commander = ShellCommander(str(temp_project_root), timeout=60)
        assert commander.timeout == 60


class TestIsDangerousCommand:
    def test_rm_rf_is_dangerous(self) -> None:
        assert _is_dangerous_command(["rm", "-rf", "/"]) is True
        assert _is_dangerous_command(["rm", "-rf", "."]) is True

    def test_rm_without_rf_is_not_dangerous(self) -> None:
        assert _is_dangerous_command(["rm", "file.txt"]) is False
        assert _is_dangerous_command(["rm", "-r", "dir"]) is False

    def test_other_commands_not_dangerous(self) -> None:
        assert _is_dangerous_command(["ls", "-la"]) is False
        assert _is_dangerous_command(["cat", "file.txt"]) is False
        assert _is_dangerous_command(["git", "status"]) is False


class TestRequiresApproval:
    def test_read_only_commands_no_approval(self) -> None:
        for cmd in READ_ONLY_COMMANDS:
            assert _requires_approval(cmd) is False

    def test_read_only_with_args_no_approval(self) -> None:
        assert _requires_approval("ls -la") is False
        assert _requires_approval("cat file.txt") is False
        assert _requires_approval("find . -name '*.py'") is False

    def test_safe_git_subcommands_no_approval(self) -> None:
        for subcmd in SAFE_GIT_SUBCOMMANDS:
            assert _requires_approval(f"git {subcmd}") is False

    def test_unsafe_git_subcommands_require_approval(self) -> None:
        assert _requires_approval("git push") is True
        assert _requires_approval("git commit -m 'msg'") is True
        assert _requires_approval("git reset --hard") is True

    def test_write_commands_require_approval(self) -> None:
        assert _requires_approval("rm file.txt") is True
        assert _requires_approval("cp file1 file2") is True
        assert _requires_approval("mv file1 file2") is True
        assert _requires_approval("mkdir new_dir") is True

    def test_invalid_command_requires_approval(self) -> None:
        assert _requires_approval("") is True
        assert _requires_approval("'unclosed quote") is True


class TestCommandAllowlist:
    def test_common_commands_in_allowlist(self) -> None:
        expected_commands = {
            "ls",
            "cat",
            "git",
            "echo",
            "pwd",
            "find",
            "rm",
            "cp",
            "mv",
            "mkdir",
        }
        for cmd in expected_commands:
            assert cmd in COMMAND_ALLOWLIST


class TestShellCommanderExecute:
    async def test_execute_ls_command(
        self, shell_commander: ShellCommander, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "test.txt"
        test_file.write_text("content", encoding="utf-8")
        result = await shell_commander.execute("ls")
        assert result.return_code == 0
        assert "test.txt" in result.stdout

    async def test_execute_pwd_command(
        self, shell_commander: ShellCommander, temp_project_root: Path
    ) -> None:
        result = await shell_commander.execute("pwd")
        assert result.return_code == 0
        assert str(temp_project_root) in result.stdout

    async def test_execute_echo_command(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("echo 'Hello World'")
        assert result.return_code == 0
        assert "Hello World" in result.stdout

    async def test_execute_command_not_in_allowlist(
        self, shell_commander: ShellCommander
    ) -> None:
        result = await shell_commander.execute("curl http://example.com")
        assert result.return_code == -1
        assert "not in the allowlist" in result.stderr

    async def test_execute_empty_command(self, shell_commander: ShellCommander) -> None:
        result = await shell_commander.execute("")
        assert result.return_code == -1
        assert "empty" in result.stderr.lower()

    async def test_execute_dangerous_command_rejected(
        self, shell_commander: ShellCommander
    ) -> None:
        result = await shell_commander.execute("rm -rf /")
        assert result.return_code == -1
        assert "dangerous" in result.stderr.lower()

    async def test_execute_grep_suggests_rg(
        self, shell_commander: ShellCommander
    ) -> None:
        result = await shell_commander.execute("grep pattern file.txt")
        assert result.return_code == -1
        assert "rg" in result.stderr

    async def test_execute_command_with_stderr(
        self, shell_commander: ShellCommander
    ) -> None:
        result = await shell_commander.execute("ls nonexistent_file_12345")
        assert result.return_code != 0

    async def test_execute_cat_command(
        self, shell_commander: ShellCommander, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "cat_test.txt"
        test_file.write_text("File content here", encoding="utf-8")
        result = await shell_commander.execute("cat cat_test.txt")
        assert result.return_code == 0
        assert "File content here" in result.stdout


class TestCreateShellCommandTool:
    def test_creates_tool_instance(self, shell_commander: ShellCommander) -> None:
        tool = create_shell_command_tool(shell_commander)
        assert isinstance(tool, Tool)

    def test_tool_has_correct_name(self, shell_commander: ShellCommander) -> None:
        tool = create_shell_command_tool(shell_commander)
        assert tool.name == "execute_shell_command"

    def test_tool_has_description(self, shell_commander: ShellCommander) -> None:
        tool = create_shell_command_tool(shell_commander)
        assert tool.description is not None
        assert "shell" in tool.description.lower()


class TestToolApprovalBehavior:
    async def test_read_only_command_no_approval_needed(
        self, shell_commander: ShellCommander
    ) -> None:
        tool = create_shell_command_tool(shell_commander)
        mock_ctx = MagicMock()
        mock_ctx.tool_call_approved = False
        result = await tool.function(mock_ctx, "ls")
        assert result.return_code == 0

    async def test_write_command_requires_approval(
        self, shell_commander: ShellCommander
    ) -> None:
        tool = create_shell_command_tool(shell_commander)
        mock_ctx = MagicMock()
        mock_ctx.tool_call_approved = False
        with pytest.raises(ApprovalRequired):
            await tool.function(mock_ctx, "rm test.txt")

    async def test_write_command_with_approval(
        self, shell_commander: ShellCommander, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "to_delete.txt"
        test_file.write_text("delete me", encoding="utf-8")
        tool = create_shell_command_tool(shell_commander)
        mock_ctx = MagicMock()
        mock_ctx.tool_call_approved = True
        result = await tool.function(mock_ctx, "rm to_delete.txt")
        assert result.return_code == 0
        assert not test_file.exists()
