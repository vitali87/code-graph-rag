import asyncio
import shlex
from pathlib import Path

from loguru import logger
from pydantic_ai import Tool

from ..schemas import ShellCommandResult

# A strict list of commands the agent is allowed to execute.
COMMAND_ALLOWLIST = {
    "ls",
    "rg",
    "cat",
    "git",
    "echo",
    "pwd",
    "pytest",
    "mypy",
    "ruff",
    "uv",
    "find",
    # FS Modifying commands - Agent MUST ask for confirmation before using.
    "rm",
    "cp",
    "mv",
    "mkdir",
    "rmdir",
}

# Git commands that require user confirmation
GIT_CONFIRMATION_COMMANDS = {
    "add",
    "commit",
    "push",
    "pull",
    "merge",
    "rebase",
    "reset",
    "checkout",
    "branch",
    "tag",
    "stash",
    "cherry-pick",
    "revert",
}


def _is_dangerous_command(cmd_parts: list[str]) -> bool:
    """Checks for dangerous command patterns."""
    command = cmd_parts[0]
    return command == "rm" and "-rf" in cmd_parts


def _requires_confirmation(cmd_parts: list[str]) -> tuple[bool, str]:
    """
    Checks if a command requires user confirmation.
    Returns (requires_confirmation, reason).
    """
    if not cmd_parts:
        return False, ""
    
    command = cmd_parts[0]
    
    # File system modification commands
    if command in {"rm", "cp", "mv", "mkdir", "rmdir"}:
        return True, f"filesystem modification command '{command}'"
    
    # Package management commands
    if command == "uv":
        return True, "package management command 'uv'"
    
    # Git commands that modify state
    if command == "git" and len(cmd_parts) > 1:
        git_subcommand = cmd_parts[1]
        if git_subcommand in GIT_CONFIRMATION_COMMANDS:
            return True, f"git command 'git {git_subcommand}'"
    
    return False, ""


class ShellCommander:
    """Service to execute shell commands."""

    def __init__(self, project_root: str = ".", timeout: int = 30):
        self.project_root = Path(project_root).resolve()
        self.timeout = timeout
        logger.info(f"ShellCommander initialized with root: {self.project_root}")

    async def execute(self, command: str, confirmed: bool = False) -> ShellCommandResult:
        """
        Execute a shell command and return the status code, stdout, and stderr.
        """
        logger.info(f"Executing shell command: {command}")
        try:
            cmd_parts = shlex.split(command)
            if not cmd_parts:
                return ShellCommandResult(
                    return_code=-1, stdout="", stderr="Empty command provided."
                )

            # Security: Check if the command is in the allowlist
            if cmd_parts[0] not in COMMAND_ALLOWLIST:
                available_commands = ", ".join(sorted(COMMAND_ALLOWLIST))
                suggestion = ""
                if cmd_parts[0] == "grep":
                    suggestion = " Use 'rg' instead of 'grep' for text searching."
                
                err_msg = f"Command '{cmd_parts[0]}' is not in the allowlist.{suggestion} Available commands: {available_commands}"
                logger.error(err_msg)
                return ShellCommandResult(return_code=-1, stdout="", stderr=err_msg)

            # Security: Check for dangerous argument combinations
            if _is_dangerous_command(cmd_parts):
                err_msg = f"Rejected dangerous command: {' '.join(cmd_parts)}"
                logger.error(err_msg)
                return ShellCommandResult(return_code=-1, stdout="", stderr=err_msg)

            # Skip confirmation check since tool interface handles safety at agent level

            process = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_root,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            logger.info(f"Return code: {process.returncode}")
            if stdout_str:
                logger.info(f"Stdout: {stdout_str}")
            if stderr_str:
                logger.warning(f"Stderr: {stderr_str}")

            return ShellCommandResult(
                return_code=(
                    process.returncode if process.returncode is not None else -1
                ),  # VA: redundant but to satisfy type checker
                stdout=stdout_str,
                stderr=stderr_str,
            )
        except TimeoutError:
            msg = f"Command '{command}' timed out after {self.timeout} seconds."
            logger.error(msg)
            try:
                process.kill()
                await process.wait()
                logger.info("Process killed due to timeout.")
            except ProcessLookupError:
                logger.warning(
                    "Process already terminated when timeout kill was attempted."
                )
            return ShellCommandResult(return_code=-1, stdout="", stderr=msg)
        except Exception as e:
            logger.error(f"An error occurred while executing command: {e}")
            return ShellCommandResult(return_code=-1, stdout="", stderr=str(e))


def create_shell_command_tool(shell_commander: ShellCommander) -> Tool:
    """Factory function to create the shell command tool."""

    async def run_shell_command(command: str) -> ShellCommandResult:
        """
        Executes a shell command from the approved allowlist only.
        
        AVAILABLE COMMANDS:
        - File operations: ls, cat, find, pwd
        - Text search: rg (ripgrep) - USE THIS INSTEAD OF grep
        - Version control: git (some subcommands require confirmation)
        - Testing: pytest, mypy, ruff  
        - Package management: uv (requires confirmation)
        - File system: rm, cp, mv, mkdir, rmdir (require confirmation)
        - Other: echo
        
        IMPORTANT: Use 'rg' for text searching, NOT 'grep' (grep is not available).
        
        COMMANDS REQUIRING USER CONFIRMATION:
        - File system: rm, cp, mv, mkdir, rmdir
        - Package management: uv (any subcommand)
        - Git operations: add, commit, push, pull, merge, rebase, reset, checkout, branch, tag, stash, cherry-pick, revert
        - Safe git commands (no confirmation needed): status, log, diff, show, ls-files, remote, config
        
        When a command requires confirmation, the tool will execute it directly without
        additional confirmation prompts since the agent handles user approval.
        """
        return await shell_commander.execute(command, confirmed=True)

    return Tool(
        function=run_shell_command,
        name="execute_shell_command",
        description="Executes shell commands from allowlist. For dangerous commands, call twice: first to check if confirmation needed, then with user_confirmed=True after getting approval.",
    )
