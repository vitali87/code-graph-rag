from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger
from pydantic_ai import ApprovalRequired, RunContext, Tool

from .. import constants as cs
from .. import logs as ls
from .. import tool_errors as te
from ..config import settings
from ..decorators import async_timing_decorator
from ..schemas import ShellCommandResult
from ..taint import ReadContentRecord
from . import tool_descriptions as td

PIPELINE_PATTERNS_COMPILED = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in cs.SHELL_DANGEROUS_PATTERNS_PIPELINE
)
SEGMENT_PATTERNS_COMPILED = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in cs.SHELL_DANGEROUS_PATTERNS_SEGMENT
)


def _is_outside_single_quotes(command: str, pos: int) -> bool:
    in_single = False
    i = 0
    while i < pos:
        char = command[i]
        if char == "\\" and not in_single and i + 1 < len(command):
            i += 2
            continue
        if char == "'":
            in_single = not in_single
        i += 1
    return not in_single


def _has_subshell(command: str) -> str | None:
    for pattern in cs.SHELL_SUBSHELL_PATTERNS:
        start = 0
        while True:
            pos = command.find(pattern, start)
            if pos == -1:
                break
            if _is_outside_single_quotes(command, pos):
                return pattern
            start = pos + 1
    return None


class CommandGroup:
    __slots__ = ("commands", "operator")

    def __init__(self, commands: list[str], operator: str | None = None):
        self.commands = commands
        self.operator = operator


def _parse_command(command: str) -> list[CommandGroup]:
    groups: list[CommandGroup] = []
    current_pipeline: list[str] = []
    current_segment: list[str] = []
    in_single = False
    in_double = False
    pending_operator: str | None = None
    i = 0

    def finalize_segment() -> None:
        seg = "".join(current_segment).strip()
        if seg:
            current_pipeline.append(seg)
        current_segment.clear()

    def finalize_group(new_operator: str) -> None:
        nonlocal pending_operator
        finalize_segment()
        if current_pipeline:
            groups.append(CommandGroup(list(current_pipeline), pending_operator))
        current_pipeline.clear()
        pending_operator = new_operator

    while i < len(command):
        char = command[i]
        if char == "\\" and i + 1 < len(command):
            current_segment.append(char)
            current_segment.append(command[i + 1])
            i += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            current_segment.append(char)
        elif char == '"' and not in_single:
            in_double = not in_double
            current_segment.append(char)
        elif char == "|" and not in_single and not in_double:
            if i + 1 < len(command) and command[i + 1] == "|":
                finalize_group("||")
                i += 2
                continue
            finalize_segment()
        elif char == "&" and not in_single and not in_double:
            if i + 1 < len(command) and command[i + 1] == "&":
                finalize_group("&&")
                i += 2
                continue
            current_segment.append(char)
        elif char == ";" and not in_single and not in_double:
            finalize_group(";")
        else:
            current_segment.append(char)
        i += 1

    finalize_segment()
    if current_pipeline:
        groups.append(CommandGroup(list(current_pipeline), pending_operator))

    return groups


def _is_blocked_command(cmd: str) -> bool:
    return cmd in cs.SHELL_DANGEROUS_COMMANDS


def _is_dangerous_rm(cmd_parts: list[str]) -> bool:
    if not cmd_parts or cmd_parts[0] != cs.SHELL_CMD_RM:
        return False
    flags = "".join(part for part in cmd_parts[1:] if part.startswith("-"))
    return "r" in flags and "f" in flags


def _is_dangerous_rm_path(cmd_parts: list[str], project_root: Path) -> tuple[bool, str]:
    if not cmd_parts or cmd_parts[0] != cs.SHELL_CMD_RM:
        return False, ""
    path_args = [p for p in cmd_parts[1:] if not p.startswith("-")]
    for path_arg in path_args:
        if path_arg in ("*", ".", ".."):
            return True, f"rm targeting dangerous path: {path_arg}"
        try:
            if path_arg.startswith("/"):
                resolved = Path(path_arg).resolve()
            else:
                resolved = (project_root / path_arg).resolve()
        except (OSError, ValueError):
            return True, f"rm with invalid path: {path_arg}"
        resolved_str = str(resolved)
        if resolved == resolved.parent:
            return True, "rm targeting root directory"
        try:
            resolved.relative_to(project_root)
        except ValueError:
            parts = resolved.parts
            if len(parts) >= 2 and parts[1] in cs.SHELL_SYSTEM_DIRECTORIES:
                return True, f"rm targeting system directory: {resolved_str}"
            return True, f"rm targeting path outside project: {resolved_str}"
    return False, ""


def _check_pipeline_patterns(full_command: str) -> str | None:
    for pattern, reason in PIPELINE_PATTERNS_COMPILED:
        if pattern.search(full_command):
            return reason
    return None


def _check_segment_patterns(segment: str) -> str | None:
    for pattern, reason in SEGMENT_PATTERNS_COMPILED:
        if pattern.search(segment):
            return reason
    return None


def _flag_name(arg: str) -> str:
    # `--type=bool` and `--type bool` should compare equal on the flag name.
    return arg.split("=", 1)[0]


def _is_git_config_exec_key(key: str) -> bool:
    key = key.lower()
    if key in cs.SHELL_GIT_CONFIG_EXEC_KEYS:
        return True
    return any(
        key.startswith(prefix)
        and key.endswith(suffix)
        and len(key) > len(prefix) + len(suffix)
        for prefix, suffix in cs.SHELL_GIT_CONFIG_EXEC_KEY_PATTERNS
    )


def _git_dash_c_exec_key(cmd_parts: list[str]) -> str | None:
    """Return the shell-executing config key set inline via `git -c key=value`.

    `git -c` applies a config key for one command without writing a file, so it
    reaches the same executable keys `_git_config_exec_key` guards on the write
    path (GHSA-wvxg-744g-6pcg).
    """
    if not cmd_parts or cmd_parts[0] != cs.SHELL_CMD_GIT:
        return None

    index = 1
    while index < len(cmd_parts):
        arg = cmd_parts[index]

        # git's own options end at the subcommand; `-c` after it belongs to the
        # subcommand and means something else entirely (`git commit -c <ref>`
        # reuses a commit message and executes nothing). Reaching this on a
        # value that belongs to a preceding flag would stop the scan early, so
        # value-taking global flags consume their argument below.
        if not arg.startswith("-"):
            return None

        key: str | None = None
        separated = arg in cs.SHELL_GIT_INLINE_CONFIG_FLAGS and index + 1 < len(
            cmd_parts
        )
        if separated:
            key = cmd_parts[index + 1]
        elif arg.startswith("-c") and len(arg) > 2:
            key = arg[2:]
        elif arg.startswith("--config-env="):
            key = arg[len("--config-env=") :]

        if key and _is_git_config_exec_key(key.split("=", 1)[0]):
            return key.split("=", 1)[0]

        if (
            arg in cs.SHELL_GIT_GLOBAL_VALUE_FLAGS
            and arg not in cs.SHELL_GIT_OPTIONAL_ARG_FLAGS
        ):
            index += 2
        else:
            index += 1

    return None


def _git_config_exec_key(cmd_parts: list[str]) -> str | None:
    """Return the shell-executing git config key this command would write.

    `git config` writes to keys like `core.sshCommand` plant a value git later
    hands to a shell, so the write itself is RCE on the next git operation
    (GHSA-2rr7-8xrw-gmhr). Reading a key is fine, and clearing one (`--unset`)
    is how a victim recovers, so those return None.
    """
    if len(cmd_parts) < 3 or cmd_parts[0] != cs.SHELL_CMD_GIT:
        return None
    if cmd_parts[1] != cs.SHELL_GIT_SUBCMD_CONFIG:
        return None

    args = cmd_parts[2:]
    flags = [_flag_name(arg) for arg in args if arg.startswith("-")]
    if any(flag in cs.SHELL_GIT_CONFIG_READ_ACTIONS for flag in flags):
        return None
    if any(flag in cs.SHELL_GIT_CONFIG_UNSET_FLAGS for flag in flags):
        return None

    return next(
        (
            arg
            for arg in args
            if not arg.startswith("-") and _is_git_config_exec_key(arg)
        ),
        None,
    )


def _xargs_short_cluster(arg: str) -> int | None:
    """Tokens consumed by a bundled short-flag cluster, or None if unknown.

    `xargs -0pt cat` and `xargs -n1 cat` are ordinary spellings, so treating a
    cluster as unrecognisable would block routine use and collapse the
    fail-closed rule into blocking everything. Returns 1 when the cluster is
    self-contained and 2 when its final flag takes the following token.
    """
    for position, letter in enumerate(arg[1:], start=1):
        short = f"-{letter}"
        if short in cs.SHELL_XARGS_BOOLEAN_FLAGS:
            continue
        if short in cs.SHELL_XARGS_OPTIONAL_ARG_FLAGS:
            # Attached-only: the cluster remainder is its value, if any.
            return 1
        if short in cs.SHELL_XARGS_VALUE_FLAGS:
            # A trailing value flag takes the next token; anything after it in
            # the cluster is its value and is consumed with it.
            return 2 if position == len(arg) - 1 else 1
        return None
    return 1


def _xargs_launched_index(cmd_parts: list[str]) -> int | None:
    """Index of the program `xargs` would launch, or None if it launches none.

    Returns the position rather than the token so the caller can vet the whole
    launched command -- program and arguments -- instead of only its name.
    A sentinel value means the scan could not determine the program at all.
    """
    if not cmd_parts or cmd_parts[0] != cs.SHELL_CMD_XARGS:
        return None

    args = cmd_parts[1:]
    index = 0
    while index < len(args):
        arg = args[index]
        if not arg.startswith("-"):
            return index + 1

        if arg == "--":
            return index + 2 if index + 1 < len(args) else None

        flag = _flag_name(arg)
        if flag in cs.SHELL_XARGS_OPTIONAL_ARG_FLAGS:
            index += 1
            continue
        if flag in cs.SHELL_XARGS_VALUE_FLAGS:
            if "=" in arg:
                index += 1
            else:
                bundled = len(arg) > 2 and not arg.startswith("--")
                index += 1 if bundled else 2
            continue
        if flag in cs.SHELL_XARGS_BOOLEAN_FLAGS:
            index += 1
            continue

        if len(arg) > 2 and not arg.startswith("--"):
            consumed = _xargs_short_cluster(arg)
            if consumed is not None:
                index += consumed
                continue

        return -1

    return None


def _xargs_launched_command(cmd_parts: list[str]) -> str | None:
    """Return the program `xargs` would launch, or None if it launches none.

    `_validate_segment` only ever checks a segment's own base command, so an
    allowlisted launcher reaches interpreters the allowlist deliberately omits
    (GHSA-wvxg-744g-6pcg). Bare `xargs` defaults to `echo` and launches nothing
    of its own.
    """
    index = _xargs_launched_index(cmd_parts)
    if index is None:
        return None
    if index < 0:
        return cs.SHELL_XARGS_UNKNOWN_LAUNCH
    return cmd_parts[index]


def _awk_exec_construct(cmd_parts: list[str]) -> str | None:
    """Return the construct through which this awk program reaches a command.

    awk can only launch a subprocess or write a file through a fixed set of
    constructs, so they are detected in the program text itself. Matching the
    surrounding spelling was defeated by every indirection tried -- a variable
    holding the command name, a -v assignment, string concatenation, a program
    supplied with -f, a newline in the program -- because the command name
    need never appear literally. The constructs cannot be avoided.
    """
    if not cmd_parts or cmd_parts[0] != cs.SHELL_CMD_AWK:
        return None

    # A program given with -f lives in a file this validator cannot read, so
    # its contents are unknowable and it is refused outright.
    if any(_flag_name(a) == "-f" for a in cmd_parts[1:] if a.startswith("-")):
        return "a program file this validator cannot inspect"

    index = 1
    while index < len(cmd_parts):
        arg = cmd_parts[index]
        if arg.startswith("-"):
            # A value flag consumes the following token, which is its value
            # and not the program; the attached spelling (-vc=id) carries it.
            if arg in cs.SHELL_AWK_VALUE_FLAGS:
                index += 2
            else:
                index += 1
            continue
        for pattern, reason in cs.SHELL_AWK_EXEC_TOKENS:
            if re.search(pattern, arg):
                return reason
        # Only the first non-flag argument is the program; the rest are files.
        break

    return None


def _is_dangerous_command(
    cmd_parts: list[str], full_segment: str, bypass_allowlist: bool = False
) -> tuple[bool, str]:
    if not cmd_parts:
        return False, ""

    base_cmd = cmd_parts[0]

    if _is_blocked_command(base_cmd):
        return True, f"blocked command: {base_cmd}"

    if _is_dangerous_rm(cmd_parts):
        return True, "rm with dangerous flags"

    if bypass_allowlist and base_cmd in cs.SHELL_LAUNCHER_COMMANDS:
        # Two launchers state what they will run and can be vetted even here;
        # the rest take no inspectable argument and are blocked outright.
        # `find` launches a program only via a mutating action, so read-only
        # find stays usable under yolo -- the point of yolo is unattended work.
        if base_cmd == cs.SHELL_CMD_XARGS:
            index = _xargs_launched_index(cmd_parts)
            if index is None:
                # Bare xargs defaults to echo and launches nothing of its own.
                confined = True
            elif index < 0:
                confined = False
            else:
                # Vet the launched command as a segment in its own right.
                # Allowlist membership alone is not safety: every launcher is
                # itself allowlisted, so `xargs uv run python -c ...` would
                # otherwise pass the check that blocks `uv run python -c ...`.
                launched_parts = cmd_parts[index:]
                nested_dangerous, _ = _is_dangerous_command(
                    launched_parts,
                    " ".join(launched_parts),
                    bypass_allowlist,
                )
                confined = (
                    launched_parts[0] in settings.SHELL_COMMAND_ALLOWLIST
                    and not nested_dangerous
                )
        elif base_cmd == cs.SHELL_CMD_FIND:
            confined = not _find_requires_approval(cmd_parts)
        else:
            confined = False

        if not confined:
            return True, (
                f"{base_cmd} launches arbitrary programs; blocked when the "
                "allowlist is bypassed"
            )

    if construct := _awk_exec_construct(cmd_parts):
        return True, f"awk can run a command via {construct}"

    if key := _git_dash_c_exec_key(cmd_parts):
        return True, f"git -c sets '{key}', a key whose value git executes"

    if key := _git_config_exec_key(cmd_parts):
        return True, f"git config write to '{key}' plants a command git will run"

    if reason := _check_segment_patterns(full_segment):
        return True, reason

    return False, ""


def _validate_segment(
    segment: str,
    available_commands: str,
    bypass_allowlist: bool = False,
    _depth: int = 0,
) -> str | None:
    try:
        cmd_parts = shlex.split(segment)
    except ValueError:
        return te.COMMAND_INVALID_SYNTAX.format(segment=segment)

    if not cmd_parts:
        return None

    if _depth > cs.SHELL_MAX_LAUNCHER_NESTING:
        # Each level drops at least one token, so this is unreachable for any
        # plausible command; the cap exists so a pathological one fails with a
        # validator reason rather than a bare RecursionError from the runtime.
        return te.COMMAND_DANGEROUS_BLOCKED.format(
            cmd=cmd_parts[0],
            reason="launcher nesting is too deep to check",
        )

    base_cmd = cmd_parts[0]

    if not bypass_allowlist and base_cmd not in settings.SHELL_COMMAND_ALLOWLIST:
        suggestion = cs.GREP_SUGGESTION if base_cmd == cs.SHELL_CMD_GREP else ""
        return te.COMMAND_NOT_ALLOWED.format(
            cmd=base_cmd, suggestion=suggestion, available=available_commands
        )

    launched_index = _xargs_launched_index(cmd_parts)
    if launched_index is not None:
        if launched_index < 0:
            return te.COMMAND_DANGEROUS_BLOCKED.format(
                cmd=base_cmd,
                reason=(
                    "xargs carries a flag this validator cannot interpret, so "
                    "the program it would launch cannot be checked"
                ),
            )
        # Validate the launched command as a segment in its own right, in BOTH
        # modes. Checking only its name lets a launcher through, since every
        # launcher is itself allowlisted -- and nesting hides `git -c`, the
        # unknown-flag sentinel, and a further xargs from every check below,
        # because those all inspect cmd_parts[0] only (GHSA rounds 4 and 5).
        # shlex.join, not " ".join: a bare join drops the quoting shlex.split
        # removed, so a token containing whitespace is re-split into two by the
        # nested parse. `git -c 'a b' -c core.pager=x log` then presents `b` as
        # the first non-flag token, which stops _git_dash_c_exec_key's scan
        # before the real -c behind it -- nesting weakening the decision, the
        # very thing this recursion exists to prevent.
        if nested := _validate_segment(
            shlex.join(cmd_parts[launched_index:]),
            available_commands,
            bypass_allowlist,
            _depth + 1,
        ):
            return nested

    is_dangerous, reason = _is_dangerous_command(cmd_parts, segment, bypass_allowlist)
    if is_dangerous:
        return te.COMMAND_DANGEROUS_BLOCKED.format(cmd=base_cmd, reason=reason)

    return None


def _has_redirect_operators(parts: list[str]) -> bool:
    return any(p in cs.SHELL_REDIRECT_OPERATORS for p in parts)


def _find_requires_approval(parts: list[str]) -> bool:
    return any(part in cs.SHELL_FIND_MUTATING_ACTIONS for part in parts[1:])


def _requires_approval(command: str) -> bool:
    if not command.strip():
        return True

    try:
        groups = _parse_command(command)
    except (ValueError, IndexError):
        return True

    has_commands = False
    for group in groups:
        for segment in group.commands:
            segment = segment.strip()
            if not segment:
                continue
            try:
                parts = shlex.split(segment)
            except ValueError:
                return True

            if not parts:
                continue

            if _has_redirect_operators(parts):
                return True

            has_commands = True
            base_cmd = parts[0]
            if base_cmd == "find" and _find_requires_approval(parts):
                return True
            if base_cmd in settings.SHELL_READ_ONLY_COMMANDS:
                continue

            if base_cmd == cs.SHELL_CMD_GIT and len(parts) > 1:
                if parts[1] in settings.SHELL_SAFE_GIT_SUBCOMMANDS:
                    continue

            return True

    return not has_commands


class ShellCommander:
    __slots__ = ("project_root", "timeout", "is_yolo")

    def __init__(
        self,
        project_root: str = ".",
        timeout: int = 30,
        is_yolo: Callable[[], bool] | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.timeout = timeout
        self.is_yolo = is_yolo or (lambda: False)
        logger.info(ls.SHELL_COMMANDER_INIT.format(root=self.project_root))

    async def _execute_pipeline(self, segments: list[str]) -> tuple[int, bytes, bytes]:
        start_time = time.monotonic()
        input_data: bytes | None = None
        all_stderr: list[bytes] = []
        last_return_code = 0

        env = os.environ.copy()
        if sys.platform == "win32":
            git_bin = r"C:\Program Files\Git\usr\bin"
            if os.path.isdir(git_bin) and git_bin not in env["PATH"]:
                env["PATH"] = f"{git_bin};{env['PATH']}"

        for segment in segments:
            elapsed = time.monotonic() - start_time
            remaining_timeout = self.timeout - elapsed
            if remaining_timeout <= 0:
                raise TimeoutError

            cmd_parts = shlex.split(segment)
            executable = shutil.which(cmd_parts[0], path=env["PATH"])
            if not executable:
                executable = cmd_parts[0]

            try:
                proc = await asyncio.create_subprocess_exec(
                    executable,
                    *cmd_parts[1:],
                    stdin=asyncio.subprocess.PIPE if input_data is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.project_root,
                    env=env,
                )
            except OSError as e:
                # A bare str(OSError) hides WHICH segment failed to spawn, so
                # an intermittent runner failure surfaces as an opaque -1
                # (issue #902). Name the segment and the resolved executable.
                raise RuntimeError(
                    te.COMMAND_SPAWN_FAILED.format(
                        segment=segment, executable=executable, error=e
                    )
                ) from e
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=input_data), timeout=remaining_timeout
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise

            last_return_code = (
                proc.returncode
                if proc.returncode is not None
                else cs.SHELL_RETURN_CODE_ERROR
            )
            input_data = stdout

            if stderr:
                all_stderr.append(stderr)

        return last_return_code, input_data or b"", b"".join(all_stderr)

    @async_timing_decorator
    async def execute(self, command: str) -> ShellCommandResult:
        """Run a command after the safety checks, capturing both streams."""
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

            if pattern_reason := _check_pipeline_patterns(command):
                err_msg = te.COMMAND_DANGEROUS_PATTERN.format(reason=pattern_reason)
                logger.error(err_msg)
                return ShellCommandResult(
                    return_code=cs.SHELL_RETURN_CODE_ERROR,
                    stdout="",
                    stderr=err_msg,
                )

            groups = _parse_command(command)
            if not groups:
                return ShellCommandResult(
                    return_code=cs.SHELL_RETURN_CODE_ERROR,
                    stdout="",
                    stderr=te.COMMAND_EMPTY,
                )

            available_commands = ", ".join(sorted(settings.SHELL_COMMAND_ALLOWLIST))
            bypass_allowlist = self.is_yolo()
            for group in groups:
                for segment in group.commands:
                    if err_msg := _validate_segment(
                        segment, available_commands, bypass_allowlist=bypass_allowlist
                    ):
                        logger.error(err_msg)
                        return ShellCommandResult(
                            return_code=cs.SHELL_RETURN_CODE_ERROR,
                            stdout="",
                            stderr=err_msg,
                        )
                    try:
                        cmd_parts = shlex.split(segment)
                    except ValueError:
                        continue
                    is_dangerous, reason = _is_dangerous_rm_path(
                        cmd_parts, self.project_root
                    )
                    if is_dangerous:
                        err_msg = te.COMMAND_DANGEROUS_BLOCKED.format(
                            cmd=cmd_parts[0], reason=reason
                        )
                        logger.error(err_msg)
                        return ShellCommandResult(
                            return_code=cs.SHELL_RETURN_CODE_ERROR,
                            stdout="",
                            stderr=err_msg,
                        )

            all_stdout: list[str] = []
            all_stderr: list[str] = []
            last_return_code = 0

            for group in groups:
                should_run = True
                if group.operator == "&&":
                    should_run = last_return_code == 0
                elif group.operator == "||":
                    should_run = last_return_code != 0

                if not should_run:
                    continue

                return_code, stdout, stderr = await self._execute_pipeline(
                    group.commands
                )
                last_return_code = return_code

                stdout_str = stdout.decode(cs.ENCODING_UTF8, errors="replace").strip()
                stderr_str = stderr.decode(cs.ENCODING_UTF8, errors="replace").strip()

                if stdout_str:
                    all_stdout.append(stdout_str)
                if stderr_str:
                    all_stderr.append(stderr_str)

            final_stdout = "\n".join(all_stdout)
            final_stderr = "\n".join(all_stderr)

            logger.info(ls.TOOL_SHELL_RETURN.format(code=last_return_code))
            if final_stdout:
                logger.info(ls.TOOL_SHELL_STDOUT.format(stdout=final_stdout))
            if final_stderr:
                logger.warning(ls.TOOL_SHELL_STDERR.format(stderr=final_stderr))

            return ShellCommandResult(
                return_code=last_return_code,
                stdout=final_stdout,
                stderr=final_stderr,
            )
        except TimeoutError:
            msg = te.COMMAND_TIMEOUT.format(cmd=command, timeout=self.timeout)
            logger.error(msg)
            return ShellCommandResult(
                return_code=cs.SHELL_RETURN_CODE_ERROR, stdout="", stderr=msg
            )
        except Exception as e:
            logger.error(ls.TOOL_SHELL_ERROR.format(error=e))
            return ShellCommandResult(
                return_code=cs.SHELL_RETURN_CODE_ERROR, stdout="", stderr=str(e)
            )


def create_shell_command_tool(
    shell_commander: ShellCommander, read_record: ReadContentRecord | None = None
) -> Tool:
    """Build the `execute_shell_command` tool, recording stdout and stderr in
    `read_record` so they feed the egress taint gate (issue #1128)."""

    async def run_shell_command(
        ctx: RunContext[None], command: str
    ) -> ShellCommandResult:
        """Run a shell command, recording both output streams."""
        if (
            not shell_commander.is_yolo()
            and _requires_approval(command)
            and not ctx.tool_call_approved
        ):
            raise ApprovalRequired(metadata={"command": command})

        result = await shell_commander.execute(command)
        if read_record is not None:
            # Shell output is repository content too (`cat`, `grep`); feed
            # the egress taint gate (issue #1128). Both streams reach the
            # model, and stderr carries source just as readily (compiler
            # diagnostics, tracebacks quoting the offending line).
            read_record.record(result.stdout)
            read_record.record(result.stderr)
        return result

    return Tool(
        function=run_shell_command,
        name=td.AgenticToolName.EXECUTE_SHELL,
        description=td.SHELL_COMMAND,
    )


_ESCAPING_PATH_ARG = re.compile(r"(?:^|=)[/~]")


def _long_option_matches(arg: str, canonical: str) -> bool:
    # GNU tools accept any unambiguous abbreviation of a long option, so
    # `--out`, `--outp`, ... all mean `--output` and `--files0` means
    # `--files0-from`. Match the arg's option name (before any `=`) as a
    # non-empty prefix of the canonical name, which covers the full spelling
    # too (Greptile review on PR #1388). Abbreviation ambiguity only widens
    # what GNU rejects, never what it accepts, so treating every prefix as
    # the dangerous option is safe.
    name = arg.split("=", 1)[0]
    return len(name) > 2 and canonical.startswith(name)


def _noninteractive_write_form(parts: list[str]) -> bool:
    # Write-capable invocations of otherwise read-only commands: `sort -o` /
    # `--output[=]` writes a file, and uniq's SECOND positional operand is an
    # output file -- counted through `--`, after which every argument is an
    # operand (CodeRabbit and Greptile reviews on PR #1388).
    if parts[0] == "sort":
        for arg in parts[1:]:
            if arg == "--":
                break
            if arg.startswith("--"):
                if _long_option_matches(arg, "--output"):
                    return True
                continue
            if not arg.startswith("-") or len(arg) < 2:
                continue
            # Walk the short-option cluster: `-ro` hides the output option
            # behind other flags, and `-rT` hides the temp-dir option
            # (Greptile review on PR #1388). A value-taking option (-k, -t,
            # -S) consumes the rest of the cluster as its value, so an `o`
            # after one is data, not a flag.
            for ch in arg[1:]:
                if ch in "oT":
                    return True
                if ch in "ktS":
                    break
        return False
    if parts[0] == "uniq":
        operands = 0
        operands_only = False
        for arg in parts[1:]:
            if not operands_only and arg == "--":
                operands_only = True
                continue
            if operands_only or not arg.startswith("-"):
                operands += 1
        return operands > 1
    return False


_FOLLOW_LONG_FLAGS = ("--follow", "--dereference")
_SHORT_CLUSTER = re.compile(r"-[A-Za-z]+")


def _follows_symlinks(parts: list[str]) -> bool:
    # Symlink-following traversal (`find -L .`, `rg -L pat .`, `ls -RL`)
    # reads through outward symlinks the per-operand containment check never
    # sees, because no explicit operand names the escaped target (Greptile
    # review on PR #1388).
    cmd = parts[0]
    if cmd not in ("find", "rg", "ls"):
        return False
    for arg in parts[1:]:
        if arg == "--":
            break
        if cmd == "find":
            if arg in ("-L", "-follow"):
                return True
        elif arg in _FOLLOW_LONG_FLAGS or (
            _SHORT_CLUSTER.fullmatch(arg) and "L" in arg
        ):
            return True
    return False


def _option_carries_file_input(parts: list[str]) -> bool:
    # `sort --files0-from=paths` makes sort read every file a repo-local
    # list names, including /etc/passwd, and `--compress-program`/`--pre`
    # execute a program; the operand containment loop never sees either, so
    # the whole indirect-input mode is denied (Greptile review on PR #1388).
    denied = cs.SHELL_NONINTERACTIVE_DENIED_OPTIONS.get(parts[0])
    if not denied:
        return False
    for arg in parts[1:]:
        if arg == "--":
            break
        name = arg.split("=", 1)[0]
        for opt in denied:
            if len(opt) == 2:
                # A short option: exact, or the attached-value form `-Tdir`.
                if name == opt or arg.startswith(opt):
                    return True
            # A long option matches any unambiguous GNU abbreviation, so
            # `--files0` reaches `--files0-from` (Greptile review on
            # PR #1388).
            elif _long_option_matches(arg, opt):
                return True
    return False


def _noninteractive_denial(command: str, project_root: Path) -> str | None:
    # The denial reason for an operator-less run, or None when every segment
    # is a confined read: a read-only command in a non-writing form, no
    # redirection tokens, no find mutating actions, and no absolute,
    # parent-traversal, or symlink-escaping path arguments (subprocess runs
    # without a shell, so `~` never expands and a redirect token is inert,
    # but both signal intent the harness must not honor; a repo-local
    # symlink, however, WOULD be followed outside the root by the child
    # process — CodeRabbit review on PR #1388).
    try:
        groups = _parse_command(command)
    except (ValueError, IndexError):
        return te.COMMAND_INVALID_SYNTAX.format(segment=command)
    read_only = (
        settings.SHELL_READ_ONLY_COMMANDS | settings.SHELL_NONINTERACTIVE_READ_COMMANDS
    )
    root = project_root.resolve()
    for group in groups:
        for segment in group.commands:
            if not (segment := segment.strip()):
                continue
            try:
                parts = shlex.split(segment)
            except ValueError:
                return te.COMMAND_INVALID_SYNTAX.format(segment=segment)
            if not parts:
                continue
            if parts[0] not in read_only:
                return te.COMMAND_NONINTERACTIVE_DENIED.format(
                    command=segment, reason=te.NONINTERACTIVE_NOT_READ_ONLY
                )
            if _noninteractive_write_form(parts):
                return te.COMMAND_NONINTERACTIVE_DENIED.format(
                    command=segment, reason=te.NONINTERACTIVE_WRITE_FORM
                )
            if _follows_symlinks(parts):
                return te.COMMAND_NONINTERACTIVE_DENIED.format(
                    command=segment, reason=te.NONINTERACTIVE_FOLLOW_SYMLINKS
                )
            if _option_carries_file_input(parts):
                return te.COMMAND_NONINTERACTIVE_DENIED.format(
                    command=segment, reason=te.NONINTERACTIVE_OPTION_CARRIED_INPUT
                )
            if _has_redirect_operators(parts):
                return te.COMMAND_NONINTERACTIVE_DENIED.format(
                    command=segment, reason=te.NONINTERACTIVE_REDIRECT
                )
            if parts[0] == "find" and _find_requires_approval(parts):
                return te.COMMAND_NONINTERACTIVE_DENIED.format(
                    command=segment, reason=te.NONINTERACTIVE_FIND_MUTATES
                )
            operands_only = False
            for arg in parts[1:]:
                if not operands_only and arg == "--":
                    # After `--` every argument is an operand, even one that
                    # starts with `-` (CodeRabbit review on PR #1388).
                    operands_only = True
                    continue
                if _ESCAPING_PATH_ARG.search(arg) or ".." in arg.split("/"):
                    return te.COMMAND_NONINTERACTIVE_DENIED.format(
                        command=segment, reason=te.NONINTERACTIVE_PATH_ESCAPES
                    )
                if not operands_only and arg.startswith("-"):
                    # An `=`-attached option value (`--file=linked_pats`) is a
                    # path the operand check below never sees, so it gets the
                    # same traversal and symlink containment (Greptile review
                    # on PR #1388).
                    value = arg.partition("=")[2]
                    if value and (
                        ".." in value.split("/")
                        or (
                            os.path.lexists(candidate := root / value)
                            and not candidate.resolve().is_relative_to(root)
                        )
                    ):
                        return te.COMMAND_NONINTERACTIVE_DENIED.format(
                            command=segment, reason=te.NONINTERACTIVE_PATH_ESCAPES
                        )
                    continue
                candidate = root / arg
                if os.path.lexists(
                    candidate
                ) and not candidate.resolve().is_relative_to(root):
                    return te.COMMAND_NONINTERACTIVE_DENIED.format(
                        command=segment, reason=te.NONINTERACTIVE_PATH_ESCAPES
                    )
    return None


def create_noninteractive_shell_command_tool(shell_commander: ShellCommander) -> Tool:
    # For operator-less runs (benchmarks, batch jobs): a command that would
    # need interactive approval is DENIED instead of yolo-bypassed, and the
    # allowlist stays enforced, so a model-selected command can never mutate
    # the host or read outside the project root (Greptile security review on
    # PR #1388). The error text tells the model why, so it can retry with a
    # confined read-only command.
    async def run_shell_command(
        ctx: RunContext[None], command: str
    ) -> ShellCommandResult:
        if err_msg := _noninteractive_denial(command, shell_commander.project_root):
            logger.error(err_msg)
            return ShellCommandResult(
                return_code=cs.SHELL_RETURN_CODE_ERROR, stdout="", stderr=err_msg
            )
        return await shell_commander.execute(command)

    return Tool(
        function=run_shell_command,
        name=td.AgenticToolName.EXECUTE_SHELL,
        description=td.SHELL_COMMAND,
    )
