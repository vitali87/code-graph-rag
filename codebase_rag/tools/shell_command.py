import asyncio
import shlex
from pathlib import Path

from loguru import logger
from pydantic_ai import Tool, RunContext

from ..schemas import ShellCommandResult


class ShellCommander:
    """Service to execute shell commands."""

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        logger.info(f"ShellCommander initialized with root: {self.project_root}")

    async def execute(self, command: str) -> ShellCommandResult:
        """
        Execute a shell command and return the status code, stdout, and stderr.
        """
        logger.info(f"Executing shell command: {command}")
        try:
            # Use shlex.split to safely parse the command and avoid shell injection
            cmd_parts = shlex.split(command)
            if not cmd_parts:
                return ShellCommandResult(
                    return_code=-1, stdout="", stderr="Empty command provided."
                )

            process = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_root,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

            stdout_str = stdout.decode().strip()
            stderr_str = stderr.decode().strip()

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
            msg = f"Command '{command}' timed out."
            logger.error(msg)
            return ShellCommandResult(return_code=-1, stdout="", stderr=msg)
        except Exception as e:
            logger.error(f"An error occurred while executing command: {e}")
            return ShellCommandResult(return_code=-1, stdout="", stderr=str(e))


def create_shell_command_tool(shell_commander: ShellCommander) -> Tool:
    """Factory function to create the shell command tool."""

    async def run_shell_command(
        ctx: RunContext, command: str
    ) -> ShellCommandResult:
        """
        Executes a shell command.
        For security, this tool cannot run commands with sudo or modify system-level
        files. Use it for tasks like running scripts, listing files, or checking
        versions.
        """
        return await shell_commander.execute(command)

    return Tool(
        function=run_shell_command,
        name="execute_shell_command",
        description="Executes a shell command in the project's environment.",
    ) 