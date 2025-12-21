from __future__ import annotations

import asyncio
import shlex
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path

from loguru import logger
from pydantic_ai import ApprovalRequired, RunContext, Tool

from .. import constants as cs
from .. import logs as ls
from .. import tool_errors as te
from ..schemas import ShellCommandResult
from . import tool_descriptions as td

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
    return command == cs.SHELL_CMD_RM and cs.SHELL_RM_RF_FLAG in cmd_parts


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

    if base_cmd == cs.SHELL_CMD_GIT and len(cmd_parts) > 1:
        return cmd_parts[1] not in SAFE_GIT_SUBCOMMANDS

    return True


def timing_decorator[**P, T](
    func: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T]]:
    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        start_time = time.perf_counter()
        result = await func(*args, **kwargs)
        end_time = time.perf_counter()
        execution_time = (end_time - start_time) * 1000
        func_name = getattr(func, "__qualname__", getattr(func, "__name__", repr(func)))
        logger.info(ls.SHELL_TIMING.format(func=func_name, time=execution_time))
        return result

    return wrapper


class ShellCommander:
    def __init__(self, project_root: str = ".", timeout: int = 30):
        self.project_root = Path(project_root).resolve()
        self.timeout = timeout
        logger.info(ls.SHELL_COMMANDER_INIT.format(root=self.project_root))

    @timing_decorator
    async def execute(self, command: str) -> ShellCommandResult:
        logger.info(ls.TOOL_SHELL_EXEC.format(cmd=command))
        try:
            cmd_parts = shlex.split(command)
            if not cmd_parts:
                return ShellCommandResult(
                    return_code=cs.SHELL_RETURN_CODE_ERROR,
                    stdout="",
                    stderr=te.COMMAND_EMPTY,
                )

            if cmd_parts[0] not in COMMAND_ALLOWLIST:
                available_commands = ", ".join(sorted(COMMAND_ALLOWLIST))
                suggestion = (
                    cs.GREP_SUGGESTION if cmd_parts[0] == cs.SHELL_CMD_GREP else ""
                )

                err_msg = te.COMMAND_NOT_ALLOWED.format(
                    cmd=cmd_parts[0],
                    suggestion=suggestion,
                    available=available_commands,
                )
                logger.error(err_msg)
                return ShellCommandResult(
                    return_code=cs.SHELL_RETURN_CODE_ERROR, stdout="", stderr=err_msg
                )

            if _is_dangerous_command(cmd_parts):
                err_msg = te.COMMAND_DANGEROUS.format(cmd=" ".join(cmd_parts))
                logger.error(err_msg)
                return ShellCommandResult(
                    return_code=cs.SHELL_RETURN_CODE_ERROR, stdout="", stderr=err_msg
                )

            process = await asyncio.create_subprocess_exec(
                *cmd_parts,
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
        name="execute_shell_command",
        description=td.SHELL_COMMAND,
    )
