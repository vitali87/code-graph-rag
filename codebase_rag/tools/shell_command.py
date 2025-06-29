import asyncio
import shlex
from pathlib import Path

from loguru import logger
from pydantic_ai import Tool, RunContext

from ..schemas import ShellCommandResult

# A strict list of commands the agent is allowed to execute.
COMMAND_ALLOWLIST = {
    "ls",
    "cat",
    "git",
    "echo",
    "grep",
    "pwd",
    "py",
    "python",
    "sh",
    "bash",
    "pytest",
    "ruff",
    # FS Modifying commands - Agent MUST ask for confirmation before using.
    "rm",
    "cp",
    "mv",
    "mkdir",
    "rmdir",
}


class ShellCommander:
    """Service to execute shell commands."""

    def __init__(self, project_root: str = ".", timeout: int = 30):
        self.project_root = Path(project_root).resolve()
        self.timeout = timeout
        logger.info(f"ShellCommander initialized with root: {self.project_root}")

    async def execute(self, command: str) -> ShellCommandResult:
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
                err_msg = f"Command '{cmd_parts[0]}' is not in the allowlist."
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
                return_code=process.returncode if process.returncode is not None else -1,
                stdout=stdout_str,
                stderr=stderr_str,
            )
        except asyncio.TimeoutError:
            msg = f"Command '{command}' timed out after {self.timeout} seconds."
            logger.error(msg)
            try:
                process.kill()
                await process.wait()
                logger.info("Process killed due to timeout.")
            except ProcessLookupError:
                logger.warning("Process already terminated when timeout kill was attempted.")
            return ShellCommandResult(return_code=-1, stdout="", stderr=msg)
        except Exception as e:
            logger.error(f"An error occurred while executing command: {e}")
            return ShellCommandResult(return_code=-1, stdout="", stderr=str(e))


def create_shell_command_tool(shell_commander: ShellCommander) -> Tool:
    """Factory function to create the shell command tool."""

    async def run_shell_command(ctx: RunContext, command: str) -> ShellCommandResult:
        """
        Executes an allow-listed shell command.

        For commands that modify the filesystem (rm, cp, mv, mkdir, rmdir),
        you MUST ask the user for confirmation before executing.
        For example: "I am about to run `rm -rf /some/path`. Do you want to proceed?"
        Only execute after the user has explicitly confirmed.
        """
        return await shell_commander.execute(command)

    return Tool(
        function=run_shell_command,
        name="execute_shell_command",
        description="Executes a shell command from an approved allowlist.",
    ) 