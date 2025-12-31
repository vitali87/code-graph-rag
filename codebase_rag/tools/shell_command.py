from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path

from loguru import logger
from pydantic_ai import ApprovalRequired, RunContext, Tool

from .. import constants as cs
from .. import logs as ls
from .. import tool_errors as te
from ..config import settings
from ..decorators import async_timing_decorator
from ..schemas import ShellCommandResult
from . import tool_descriptions as td

PIPE_SPLIT_PATTERN = re.compile(r"\s*(?:\||\|\||&&|;)\s*")


def _extract_commands(command: str) -> list[str]:
    segments = PIPE_SPLIT_PATTERN.split(command)
    commands = []
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        try:
            parts = shlex.split(segment)
            if parts:
                commands.append(parts[0])
        except ValueError:
            continue
    return commands


def _has_subshell(command: str) -> str | None:
    for pattern in cs.SHELL_SUBSHELL_PATTERNS:
        if pattern in command:
            return pattern
    return None


def _is_dangerous_command(cmd_parts: list[str]) -> bool:
    command = cmd_parts[0]
    return command == cs.SHELL_CMD_RM and cs.SHELL_RM_RF_FLAG in cmd_parts


def _requires_approval(command: str) -> bool:
    segments = PIPE_SPLIT_PATTERN.split(command)
    has_commands = False
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        try:
            parts = shlex.split(segment)
        except ValueError:
            return True

        if not parts:
            continue

        has_commands = True
        base_cmd = parts[0]
        if base_cmd in settings.SHELL_READ_ONLY_COMMANDS:
            continue

        if base_cmd == cs.SHELL_CMD_GIT and len(parts) > 1:
            if parts[1] in settings.SHELL_SAFE_GIT_SUBCOMMANDS:
                continue

        return True

    return not has_commands


class ShellCommander:
    def __init__(self, project_root: str = ".", timeout: int = 30):
        self.project_root = Path(project_root).resolve()
        self.timeout = timeout
        logger.info(ls.SHELL_COMMANDER_INIT.format(root=self.project_root))

    @async_timing_decorator
    async def execute(self, command: str) -> ShellCommandResult:
        logger.info(ls.TOOL_SHELL_EXEC.format(cmd=command))
        try:
            if subshell_pattern := _has_subshell(command):
                err_msg = te.COMMAND_SUBSHELL_NOT_ALLOWED.format(
                    pattern=subshell_pattern
                )
                logger.error(err_msg)
                return ShellCommandResult(
                    return_code=cs.SHELL_RETURN_CODE_ERROR, stdout="", stderr=err_msg
                )

            commands = _extract_commands(command)
            if not commands:
                return ShellCommandResult(
                    return_code=cs.SHELL_RETURN_CODE_ERROR,
                    stdout="",
                    stderr=te.COMMAND_EMPTY,
                )

            available_commands = ", ".join(sorted(settings.SHELL_COMMAND_ALLOWLIST))
            for cmd in commands:
                if cmd not in settings.SHELL_COMMAND_ALLOWLIST:
                    suggestion = cs.GREP_SUGGESTION if cmd == cs.SHELL_CMD_GREP else ""
                    err_msg = te.COMMAND_NOT_ALLOWED.format(
                        cmd=cmd,
                        suggestion=suggestion,
                        available=available_commands,
                    )
                    logger.error(err_msg)
                    return ShellCommandResult(
                        return_code=cs.SHELL_RETURN_CODE_ERROR,
                        stdout="",
                        stderr=err_msg,
                    )

            cmd_parts = shlex.split(command)
            if _is_dangerous_command(cmd_parts):
                err_msg = te.COMMAND_DANGEROUS.format(cmd=" ".join(cmd_parts))
                logger.error(err_msg)
                return ShellCommandResult(
                    return_code=cs.SHELL_RETURN_CODE_ERROR, stdout="", stderr=err_msg
                )

            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_root,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )

            stdout_str = stdout.decode(cs.ENCODING_UTF8, errors="replace").strip()
            stderr_str = stderr.decode(cs.ENCODING_UTF8, errors="replace").strip()

            logger.info(ls.TOOL_SHELL_RETURN.format(code=process.returncode))
            if stdout_str:
                logger.info(ls.TOOL_SHELL_STDOUT.format(stdout=stdout_str))
            if stderr_str:
                logger.warning(ls.TOOL_SHELL_STDERR.format(stderr=stderr_str))

            return ShellCommandResult(
                return_code=(
                    process.returncode
                    if process.returncode is not None
                    else cs.SHELL_RETURN_CODE_ERROR
                ),
                stdout=stdout_str,
                stderr=stderr_str,
            )
        except TimeoutError:
            msg = te.COMMAND_TIMEOUT.format(cmd=command, timeout=self.timeout)
            logger.error(msg)
            try:
                process.kill()
                await process.wait()
                logger.info(ls.TOOL_SHELL_KILLED)
            except ProcessLookupError:
                logger.warning(ls.TOOL_SHELL_ALREADY_TERMINATED)
            return ShellCommandResult(
                return_code=cs.SHELL_RETURN_CODE_ERROR, stdout="", stderr=msg
            )
        except Exception as e:
            logger.error(ls.TOOL_SHELL_ERROR.format(error=e))
            return ShellCommandResult(
                return_code=cs.SHELL_RETURN_CODE_ERROR, stdout="", stderr=str(e)
            )


def create_shell_command_tool(shell_commander: ShellCommander) -> Tool:
    async def run_shell_command(
        ctx: RunContext[None], command: str
    ) -> ShellCommandResult:
        if _requires_approval(command) and not ctx.tool_call_approved:
            raise ApprovalRequired(metadata={"command": command})

        return await shell_commander.execute(command)

    return Tool(
        function=run_shell_command,
        name=td.AgenticToolName.EXECUTE_SHELL,
        description=td.SHELL_COMMAND,
    )
