import asyncio
import shlex
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path
from typing import Any, cast

from loguru import logger
from pydantic_ai import ApprovalRequired, RunContext, Tool

from ..schemas import ShellCommandResult

COMMAND_ALLOWLIST = frozenset(
    {
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
        "pre-commit",
        "rm",
        "cp",
        "mv",
        "mkdir",
        "rmdir",
    }
)

READ_ONLY_COMMANDS = frozenset({"ls", "cat", "find", "pwd", "rg", "echo"})

SAFE_GIT_SUBCOMMANDS = frozenset(
    {"status", "log", "diff", "show", "ls-files", "remote", "config", "branch"}
)


def _is_dangerous_command(cmd_parts: list[str]) -> bool:
    command = cmd_parts[0]
    return command == "rm" and "-rf" in cmd_parts


def _requires_approval(command: str) -> bool:
    try:
        cmd_parts = shlex.split(command)
    except ValueError:
        return True

    if not cmd_parts:
        return True

    base_cmd = cmd_parts[0]

    if base_cmd in READ_ONLY_COMMANDS:
        return False

    if base_cmd == "git" and len(cmd_parts) > 1:
        return cmd_parts[1] not in SAFE_GIT_SUBCOMMANDS

    return True


def timing_decorator(
    func: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """
    A decorator that logs the execution time of the decorated asynchronous function.
    """

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start_time = time.perf_counter()
        result = await func(*args, **kwargs)
        end_time = time.perf_counter()
        execution_time = (end_time - start_time) * 1000
        func_name = getattr(func, "__qualname__", getattr(func, "__name__", repr(func)))
        logger.info(f"'{func_name}' executed in {execution_time:.2f}ms")
        return result

    return wrapper


class ShellCommander:
    """Service to execute shell commands."""

    def __init__(self, project_root: str = ".", timeout: int = 30):
        self.project_root = Path(project_root).resolve()
        self.timeout = timeout
        logger.info(f"ShellCommander initialized with root: {self.project_root}")

    @timing_decorator
    async def execute(self, command: str) -> ShellCommandResult:
        logger.info(f"Executing shell command: {command}")
        try:
            cmd_parts = shlex.split(command)
            if not cmd_parts:
                return ShellCommandResult(
                    return_code=-1, stdout="", stderr="Empty command provided."
                )

            if cmd_parts[0] not in COMMAND_ALLOWLIST:
                available_commands = ", ".join(sorted(COMMAND_ALLOWLIST))
                suggestion = ""
                if cmd_parts[0] == "grep":
                    suggestion = " Use 'rg' instead of 'grep' for text searching."

                err_msg = f"Command '{cmd_parts[0]}' is not in the allowlist.{suggestion} Available commands: {available_commands}"
                logger.error(err_msg)
                return ShellCommandResult(return_code=-1, stdout="", stderr=err_msg)

            if _is_dangerous_command(cmd_parts):
                err_msg = f"Rejected dangerous command: {' '.join(cmd_parts)}"
                logger.error(err_msg)
                return ShellCommandResult(return_code=-1, stdout="", stderr=err_msg)

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
                ),
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
    async def run_shell_command(
        ctx: RunContext[None], command: str
    ) -> ShellCommandResult:
        """
        Executes a shell command from the approved allowlist only.

        Args:
            command: The shell command to execute

        AVAILABLE COMMANDS (no approval needed):
        - Read-only: ls, cat, find, pwd, rg, echo
        - Safe git: status, log, diff, show, ls-files, remote, config, branch

        COMMANDS REQUIRING APPROVAL:
        - File system modifications: rm, cp, mv, mkdir, rmdir
        - Package management: uv
        - Git write operations: add, commit, push, pull, merge, rebase, etc.
        - Testing: pytest, mypy, ruff, pre-commit

        IMPORTANT: Use 'rg' for text searching, NOT 'grep' (grep is not available).
        """
        if _requires_approval(command) and not ctx.tool_call_approved:
            raise ApprovalRequired(metadata={"command": command})

        return cast(ShellCommandResult, await shell_commander.execute(command))

    return Tool(
        function=run_shell_command,
        name="execute_shell_command",
        description="Executes shell commands from allowlist. Read-only commands run without approval; write operations require user confirmation.",
    )
