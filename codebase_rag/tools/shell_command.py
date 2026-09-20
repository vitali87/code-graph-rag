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
    flags = "".join(_rm_options(cmd_parts[1:]))
    return "r" in flags and "f" in flags


def _rm_options(args: list[str]) -> list[str]:
    """The tokens any supported rm might parse as options.

    Deliberately the mirror image of `_rm_operands`. GNU rm permutes, so it
    honours a flag written after an operand: `rm target -rf` deletes
    recursively and without prompting. Stopping at the first operand, as the
    POSIX operand rule does, would miss that and let the flag check pass.

    Only `--` ends option parsing here, because after it a dash-prefixed token
    is a filename on every rm. That keeps `rm -r -- -in-root-file` off the
    dangerous-flag path, which is the case that made reading whole operands as
    flags wrong in the first place.

    Each helper errs towards catching more: operands POSIX-maximal so more
    paths face the containment check, options GNU-maximal so more spellings
    face the flag check.
    """
    end = args.index("--") if "--" in args else len(args)
    return [part for part in args[:end] if part.startswith("-") and part != "-"]


def _rm_operands(args: list[str]) -> list[str]:
    """The tokens rm would treat as paths.

    POSIX option parsing ends at the first operand or at `--`, whichever comes
    first, and everything from there on is a path even when it starts with a
    dash. BSD rm (macOS) follows that literally, so `rm a -x/../../outside/x`
    deletes the second token; GNU rm instead permutes and rejects `-x/` as a
    bad option. Taking the POSIX reading treats more tokens as paths than GNU
    would, so the containment check only ever errs towards refusing.

    Dropping every dash-prefixed token, as this used to, let both
    `rm -r -- -x/../../outside/victim` and `rm a -x/../../outside/victim`
    skip the containment check entirely.
    """
    for index, token in enumerate(args):
        if token == "--":
            return args[index + 1 :]
        if token == "-" or not token.startswith("-"):
            return args[index:]
    return []


def _is_dangerous_rm_path(cmd_parts: list[str], project_root: Path) -> tuple[bool, str]:
    if not cmd_parts or cmd_parts[0] != cs.SHELL_CMD_RM:
        return False, ""
    for path_arg in _rm_operands(cmd_parts[1:]):
        if path_arg in ("*", ".", ".."):
            return True, f"rm targeting dangerous path: {path_arg}"
        # Joining onto the root is the platform's own absoluteness test: an
        # absolute target replaces the root outright, a relative one lands
        # under it. A `startswith("/")` check is POSIX-only: on Windows a
        # rooted, drive-less target such as `/x` would resolve on the Python
        # process's drive rather than the root's, the drive rm runs on.
        try:
            resolved = (project_root / path_arg).resolve()
        except (OSError, ValueError):
            return True, f"rm with invalid path: {path_arg}"
        if resolved == project_root:
            return True, "rm targeting the project root"
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


def _git_escapes_project(cmd_parts: list[str], project_root: Path) -> tuple[bool, str]:
    """Whether git is pointed at a repository outside the project root.

    Git reads the target's config and EXECUTES what it finds -- an
    `alias.x = !cmd`, `core.fsmonitor`, `core.pager` -- so any writable
    directory becomes arbitrary execution. Verified: a planted
    `alias.pwn = "!touch FILE"` ran with rc=0 through
    `git --git-dir=SCRATCH/.git pwn`.

    Keyed on the resolved TARGET rather than the flag, because pointing git
    inside the project is ordinary use: `git -C . diff` and
    `git --git-dir x log` stay allowed.
    """
    if not cmd_parts or cmd_parts[0] != cs.SHELL_CMD_GIT:
        return False, ""

    index = 1
    while index < len(cmd_parts):
        arg = cmd_parts[index]
        target = None
        if arg in cs.SHELL_GIT_REPO_LOCATION_FLAGS:
            if index + 1 < len(cmd_parts):
                target = cmd_parts[index + 1]
            index += 2
        elif "=" in arg and _flag_name(arg) in cs.SHELL_GIT_REPO_LOCATION_FLAGS:
            target = arg.split("=", 1)[1]
            index += 1
        else:
            index += 1
            continue

        if not target:
            continue
        # Joining onto the root is the platform's own absoluteness test: an
        # absolute target replaces the root outright, a relative one lands
        # under it. A `startswith("/")` check is POSIX-only: on Windows a
        # rooted, drive-less target such as `/x` would resolve on the Python
        # process's drive rather than the root's, which is the drive git
        # itself runs on (its cwd is the root).
        try:
            resolved = (project_root / target).resolve()
        except (OSError, ValueError):
            return True, f"git pointed at an unresolvable path: {target}"

        try:
            resolved.relative_to(project_root)
        except ValueError:
            return True, (
                f"git pointed outside the project at {resolved}, whose config "
                "git would execute"
            )
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
    if any(
        key.startswith(prefix)
        and key.endswith(suffix)
        and len(key) > len(prefix) + len(suffix)
        for prefix, suffix in cs.SHELL_GIT_CONFIG_EXEC_KEY_PATTERNS
    ):
        return True

    # A suffix rule rather than another name: git names program-valued keys
    # by convention, and a list of names cannot cover a key nobody enumerated.
    return "." in key and key.endswith(cs.SHELL_GIT_CONFIG_EXEC_KEY_SUFFIXES)


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


def _git_subcommand_index(cmd_parts: list[str]) -> int | None:
    """Index of git's subcommand, stepping over global options.

    git's own options precede the subcommand and several consume the token
    after them, so the subcommand is neither at a fixed position nor simply
    the first non-dash token: `git -C dir config ...` puts `dir` there.
    Locating it wrongly has caused three separate bypasses, so every caller
    uses this one implementation rather than repeating the rule.
    """
    index = 1
    while index < len(cmd_parts):
        arg = cmd_parts[index]
        if not arg.startswith("-"):
            return index
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
    index = _git_subcommand_index(cmd_parts)
    if index is None or cmd_parts[index] != cs.SHELL_GIT_SUBCMD_CONFIG:
        return None

    args = cmd_parts[index + 1 :]
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


def _awk_program_skeleton(program: str) -> str:
    """The awk program with string literals blanked out.

    A `>` inside a printed string is text, not a redirect, and a filename or
    message can contain one. sed's scanner already blanks its user-supplied
    spans for the same reason; awk's did not, so `print "a>b"` was refused
    while `printf("x") > "f"` slipped past a parenthesis exclusion.
    """
    out: list[str] = []
    index = 0
    while index < len(program):
        char = program[index]
        if char == '"':
            end = index + 1
            while end < len(program) and program[end] != '"':
                if program[end] == "\\":
                    end += 1
                end += 1
            if end >= len(program):
                return cs.SHELL_AWK_UNPARSEABLE
            out.append('""')
            index = end + 1
            continue
        if char == "/" and _awk_regex_starts_here(program, index):
            if "/" not in program[index + 1 :]:
                # Unterminated: blanking would swallow the rest of the
                # program and could hide a construct behind it, so signal
                # that the program cannot be scanned.
                return cs.SHELL_AWK_UNPARSEABLE
            # A /regex/ literal is user text too: `/a|b/` is alternation, not
            # a pipe to a command, and gsub(/x|y/,...) is everyday awk.
            end = index + 1
            while end < len(program) and program[end] != "/":
                if program[end] == "\\":
                    end += 1
                end += 1
            out.append("//")
            index = min(end + 1, len(program))
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _awk_regex_starts_here(program: str, index: int) -> bool:
    """Whether the `/` at `index` opens a regex literal rather than division.

    awk allows both; a regex can only appear where a value is expected, so it
    follows an operator, a delimiter, or the start of the program -- never a
    name, number or closing bracket, which is where division follows.
    """
    for char in reversed(program[:index]):
        if char.isspace():
            continue
        return not (char.isalnum() or char in "_)]$")
    return True


def _awk_redirects_output(program: str) -> bool:
    """Whether an awk program redirects print/printf output to a file.

    A redirect's `>` sits at the top level of the output list; a comparison
    sits inside parentheses. Tracking depth is awk's actual grammar, where
    every spelling-based attempt failed: excluding parentheses hid
    `printf("x") > "f"`, and excluding a `$` target hid `print $2 > $1`,
    which was verified writing a file.
    """
    skeleton = _awk_program_skeleton(program)
    for match in re.finditer(r"\b(?:print|printf)\b", skeleton):
        depth = 0
        index = match.end()
        while index < len(skeleton) and skeleton[index] not in ";}":
            char = skeleton[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == ">" and depth <= 0:
                return True
            index += 1
    return False


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
    # `-f x`, `-fx` (attached) and `--file=x` all name a program file. Matching
    # only the exact token `-f` let the attached spellings through.
    if any(
        a == "-f"
        or (a.startswith("-f") and len(a) > 2)
        or _flag_name(a) in cs.SHELL_AWK_PROGRAM_FILE_FLAGS
        for a in cmd_parts[1:]
        if a.startswith("-")
    ):
        return "a program file this validator cannot inspect"

    index = 1
    while index < len(cmd_parts):
        arg = cmd_parts[index]
        if arg.startswith("-"):
            # A value flag consumes the following token, which is its value
            # and not the program; the attached spelling (-vc=id) carries it.
            # An OPTIONAL-argument flag takes its value attached only, so the
            # next token is still the program and must not be stepped over.
            if (
                arg in cs.SHELL_AWK_VALUE_FLAGS
                and arg not in cs.SHELL_AWK_OPTIONAL_ARG_FLAGS
            ):
                # The operand is the flag's value under gawk, but a program
                # under any implementation lacking the flag, so scan BOTH
                # readings rather than pick one -- the same rule sed's -i and
                # -l need. Picking gawk's alone stepped over the program.
                candidate = cmd_parts[index + 1 : index + 2]
                if candidate and not candidate[0].startswith("-"):
                    for pattern, reason in cs.SHELL_AWK_EXEC_TOKENS:
                        if re.search(pattern, candidate[0]):
                            return reason
                index += 2
            else:
                index += 1
            continue
        skeleton = _awk_program_skeleton(arg)
        if skeleton == cs.SHELL_AWK_UNPARSEABLE:
            return "a program with an unterminated string or regex"
        if _awk_redirects_output(arg):
            return "redirect to a file"
        for pattern, reason in cs.SHELL_AWK_EXEC_TOKENS:
            if re.search(pattern, skeleton):
                return reason
        # Only the first non-flag argument is the program; the rest are files.
        break

    return None


def _git_exec_subcommand(cmd_parts: list[str]) -> str | None:
    """Return the git subcommand form that would run a caller-supplied command.

    `git` is allowlisted and only its inline-config exec keys were guarded, so
    `filter-branch --tree-filter`, `bisect run` and `submodule foreach` reached
    a subprocess without meeting the allowlist at all.
    """
    if len(cmd_parts) < 3 or cmd_parts[0] != cs.SHELL_CMD_GIT:
        return None

    subcommand_index = _git_subcommand_index(cmd_parts)
    subcommand = cmd_parts[subcommand_index] if subcommand_index is not None else None
    if subcommand not in cs.SHELL_GIT_EXEC_SUBCOMMANDS:
        return None

    for arg in cmd_parts[1:]:
        if _flag_name(arg) in cs.SHELL_GIT_EXEC_SUBCOMMAND_ARGS:
            return f"{subcommand} {_flag_name(arg)}"

    return None


def _git_exec_flag(cmd_parts: list[str]) -> str | None:
    """Return a git flag that names a program git will run.

    These are independent of the subcommand: `rebase --exec`, `difftool
    --extcmd` and `mergetool --tool` run locally (the first two verified in a
    scratch repo), while `--upload-pack`/`--receive-pack`/`--smtp-server`/
    `--gpg-sign` name a program run at the other end of a connection.
    """
    if not cmd_parts or cmd_parts[0] != cs.SHELL_CMD_GIT:
        return None

    # `--exec-path=DIR` moves the directory git resolves subcommands in, so
    # any non-builtin name execs `DIR/git-<name>`. Verified: a planted
    # `git-pwn` ran with rc=0 through `git --exec-path=DIR pwn`. Only the
    # VALUED form redirects; bare `--exec-path` prints the path and exits,
    # which is why it is filed as optional-arg for arity and must keep its
    # pass.
    for arg in cmd_parts[1:]:
        if arg.startswith(f"{cs.SHELL_GIT_EXEC_PATH_FLAG}="):
            return cs.SHELL_GIT_EXEC_PATH_FLAG

    return _program_naming_flag(cmd_parts, cs.SHELL_GIT_EXEC_FLAGS)


def _sed_awaits_operand(script: str) -> bool:
    """Whether a sed script ends in a file command still missing its operand.

    `sed -ew /tmp/p` splits as ["-ew", "/tmp/p"], so the `w` and its target
    live in different argv entries. Only that shape needs the next token
    joined; joining always would scan input filenames as script text.
    """
    return bool(re.search(r"[wWrR]\s*$", script))


def _sed_script_skeleton(script: str) -> str:
    """The script with s/// and y/// bodies blanked out.

    A regex body or replacement can contain any text, including a letter that
    the command-anchor patterns read as a command (`s/x/Iw file/` looked like
    a `w` write). Blanking those spans leaves the command structure intact
    while removing text the user chose, so the anchor sees only positions
    where a command can actually appear.
    """
    out = []
    index = 0
    while index < len(script):
        char = script[index]
        if char in "sy" and index + 1 < len(script):
            delim = script[index + 1]
            if not delim.isalnum() and delim not in " \t\n;{}":
                end = index + 2
                fields = 0
                while end < len(script) and fields < 2:
                    if script[end] == "\\":
                        end += 2
                        continue
                    if script[end] == delim:
                        fields += 1
                    end += 1
                out.append(char)
                out.append(" " * (end - index - 1))
                index = end
                continue
        if char == "/":
            # A /regex/ address body is user text too, and a letter inside it
            # sits immediately before the command position. Blank it, keeping
            # the delimiters so the address still anchors the command.
            end = index + 1
            while end < len(script) and script[end] != "/":
                if script[end] == "\\":
                    end += 1
                end += 1
            if end < len(script):
                out.append("/")
                out.append(" " * (end - index - 1))
                out.append("/")
                index = end + 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _sed_cluster_tail(arg: str) -> str | None:
    """The script-bearing flag a bundled short-flag cluster ends in, if any.

    `-ne` is `-n` and `-e`, and the `-e` claims the NEXT token as its script.
    Prefix-matching the raw token instead read the cluster's own tail letter
    as the script -- `arg[2:]` of `-ne` is the single character `e` -- so the
    real script was never collected and a following `-e 'w FILE'` was never
    reached. Verified: `sed -ne p -e 'w FILE'` wrote outside the working
    directory with rc=0 while the validator saw only `e`.

    Returns the trailing flag (`-e`, `-f`, or an optional-arg flag such as
    `-i`) when the cluster ends in one and carries no attached value, `""`
    when the cluster is script-free, and None when a letter is unrecognised so
    the caller can fail closed.
    """
    if not arg.startswith("-") or arg.startswith("--") or len(arg) < 3:
        return None

    for position, letter in enumerate(arg[1:], start=1):
        short = f"-{letter}"
        if short in ("-e", "-f"):
            # A trailing -e/-f takes the next token; anything after it in the
            # cluster is its attached value and belongs to it either way.
            return short if position == len(arg) - 1 else ""
        if short in cs.SHELL_SED_OPTIONAL_ARG_FLAGS:
            # In TAIL position this behaves like a standalone `-i`: BSD sed
            # takes the next token as the suffix, which pushes the script into
            # the slot after it. Returning "" here read the cluster as
            # script-free, so nothing was collected and the payload two slots
            # along was never scanned -- `sed -ni bak 'w FILE' in.txt` wrote
            # outside the project root with rc=0 while `sed -n -i bak ...`,
            # the same command unbundled, was correctly refused.
            if position == len(arg) - 1:
                return short
            if short in ("-l", "--line-length"):
                # Mid-cluster `-l` reads two ways and only one of them ends
                # the cluster. GNU takes the remainder as its line length;
                # BSD treats `-l` as a pure boolean (it sits inside the
                # [-EHalnru] cluster in BSD's own synopsis) and keeps
                # parsing, so a trailing `i` is a real `-i` that consumes the
                # next token as its suffix and puts the script two slots on.
                # Stopping here took the GNU half alone and missed the BSD
                # reading entirely: `sed -li bak 'w FILE' in.txt` wrote
                # outside the project root with rc=0. Keep scanning, which is
                # the reading that can still find a script.
                continue
            # Any other optional-arg flag is attached-only mid-cluster, so the
            # remainder is its value.
            return ""
        if short not in cs.SHELL_SED_KNOWN_FLAGS:
            return None
    return ""


def _sed_names_script_file(cmd_parts: list[str]) -> bool:
    """Whether any argument hands sed a script FILE this validator cannot read.

    `-f x`, `-fx`, `--file=x` and a bundled `-nf x` all name one. Matching
    only tokens that START with `-f` missed every cluster: `sed -nf s.sed in`
    ran an arbitrary script file, verified writing its target with rc=0.
    """
    for arg in cmd_parts[1:]:
        if not arg.startswith("-"):
            continue
        if arg == "-f" or (arg.startswith("-f") and len(arg) > 2):
            return True
        if _flag_name(arg) == "--file":
            return True
        if not arg.startswith("--") and len(arg) > 2:
            for letter in arg[1:]:
                short = f"-{letter}"
                if short == "-f":
                    return True
                if short in cs.SHELL_SED_OPTIONAL_ARG_FLAGS or short == "-e":
                    # These consume the cluster remainder, so a later `f` in
                    # it is their value rather than a flag of its own.
                    break
    return False


def _sed_exec_construct(cmd_parts: list[str]) -> str | None:
    """Return the sed construct that would run a command or write a file.

    GNU sed executes through the `s///e` flag and a standalone `e` command,
    and writes through `w FILE` and `s///w FILE`. Only the `s///e` spelling
    was caught. This host's BSD sed rejects `e`, so a local probe says the
    others are harmless -- but CI runs GNU, where they work, and a policy that
    depends on which binary is installed fails open exactly where it matters.
    """
    if not cmd_parts or cmd_parts[0] != cs.SHELL_CMD_SED:
        return None

    if _sed_names_script_file(cmd_parts):
        return "a script file this validator cannot inspect"

    # Script text arrives as the token after `-e`, attached as `-eSCRIPT`, or
    # as `--expression=SCRIPT`, and a command can be SPLIT across two tokens:
    # `sed -ew /tmp/p` gives argv ["-ew", "/tmp/p"] and real sed writes the
    # file, so each token scanned alone never sees the `w` with its target.
    # Joining the remainder keeps them together.
    # Collect every token that could be script text, under either
    # implementation's reading. Rules, once, rather than a patch per case:
    #
    #  * `-e X` / `-eX` / `--expression=X` give script text directly.
    #  * `-i`/`-l` are read differently by GNU and BSD, so BOTH readings are
    #    collected: the operand, and the token after it when the operand
    #    looks like a suffix rather than a script.
    #  * The first bare token is the script; the tokens after it are INPUT
    #    FILES and must not be scanned -- a filename like README.md would
    #    otherwise trip the [wWrR] anchor on "RE".
    #  * The one exception is a script ending in a bare file command, where
    #    the operand is genuinely the next argv entry (`sed -ew /tmp/p`).
    scripts: list[str] = []
    ambiguous_slots: list[int] = []
    # Every candidate is scanned, including the -i operand slots. Those slots
    # are genuinely ambiguous: `sed -i ext wout1 in.txt` treats `wout1` as a
    # SCRIPT under BSD -- verified, it wrote `out1` -- and as a FILENAME under
    # GNU. Four attempts to tell them apart by inspecting the token were each
    # bypassable, because a backup suffix and a file command are the same
    # shape. The cost of scanning is a false positive on the BSD-only
    # `sed -i SUFFIX FILE` spelling when the FILENAME contains r/w/e; GNU
    # users write `sed -i.bak` or `sed -i`, which have no such slot. A rare
    # over-block is the right side of a verified write primitive.

    def add(position: int) -> None:
        if not 0 < position < len(cmd_parts):
            return
        script = cmd_parts[position]
        if _sed_awaits_operand(script) and position + 1 < len(cmd_parts):
            script = f"{script} {cmd_parts[position + 1]}"
        scripts.append(script)

    index = 1
    while index < len(cmd_parts):
        arg = cmd_parts[index]

        if arg == "--":
            # End of options: the next token is the script unless a -e/-f has
            # already given one. A preceding -i contributes only AMBIGUOUS
            # candidates, so it must not suppress this.
            if not scripts:
                add(index + 1)
            break

        if not arg.startswith("-"):
            # Once -e or -f has supplied a script, every bare token is an
            # input FILE. Treating one as a script scanned filenames, and
            # `sed -e 's/a/b/' README.md` tripped the anchor on "RE".
            if not scripts:
                add(index)
            break

        if _flag_name(arg) in cs.SHELL_SED_OPTIONAL_ARG_FLAGS and "=" in arg:
            # `--in-place=.bak`: the attached value is a SUFFIX, not script
            # text, so the script is the next token.
            add(index + 1)
            index += 1
            continue

        if (
            arg in cs.SHELL_SED_OPTIONAL_ARG_FLAGS
            or _sed_cluster_tail(arg) in cs.SHELL_SED_OPTIONAL_ARG_FLAGS
        ):
            # A bundled cluster ending in one of these (`-ni`, `-anl`) takes
            # its operand exactly as the standalone spelling does, so it needs
            # the same both-readings scan. Matching only the standalone form
            # let `sed -ni bak 'w FILE' in.txt` through while refusing the
            # identical `sed -n -i bak 'w FILE' in.txt`.
            # The operand is a suffix under one implementation and the script
            # under the other, so scan BOTH -- unconditionally. Gating the
            # second reading on the operand's SHAPE was a bypass: a suffix of
            # nine characters, or one containing a slash, failed the guess and
            # the real script was never looked at.
            operand = cmd_parts[index + 1 : index + 2]
            if operand and not operand[0].startswith("-"):
                # Only a non-flag token can be the script or a suffix; `--`
                # and a following flag belong to the branches below.
                # GNU: this is the script. BSD: a suffix, and the next
                # token is the script. Scan both; the BSD-suffix reading of
                # the SECOND token is the only slot where GNU would have put
                # an input filename, so that one is position-excluded.
                # Both operands are ambiguous, and in opposite directions:
                # under GNU the first is the script and the second an input
                # file; under BSD the first is a suffix and the second the
                # script.
                #
                # Both slots are scanned. Five attempts to decide which
                # reading applies by inspecting a token all became bypasses:
                # the operand's shape, its whitespace, its position, and
                # classifying it as script-or-suffix from either direction.
                # BSD takes the first operand as a backup suffix whatever it
                # looks like -- a suffix named `1d` left the payload
                # unscanned, verified overwriting a file -- and `README.md`
                # is itself a valid sed `R` command, so the bytes genuinely
                # underdetermine the meaning.
                #
                # Scanning both slots alone would refuse
                # `sed -i 's/a/b/' README.md`, since the filename sits in the
                # second slot (index+2, not index+3). So that slot carries a
                # NARROWER anchor instead: a write leaving the working
                # directory needs a separator or a path form before its
                # target (`w /tmp/x`, `w/tmp/x`, `w../x`, `w~/x`), while an
                # input filename is one unbroken token. That is a property of
                # the write rather than of the token's identity.
                #
                # A bare single-token target with no slash (`wout1`) is
                # still not caught there. The earlier justification -- "it
                # writes only inside the working directory, which tee and cp
                # already permit" -- was WRONG in general: the working
                # directory contains `.git/`, and `w.git/hooks/pre-commit`
                # installs a hook that git then executes. Verified. Any
                # target containing a slash is now caught, which covers that
                # and every dotdir case; what remains is a write to a plain
                # filename in the CWD, with no path component at all.
                add(index + 1)
                ambiguous_slots.append(len(scripts))
                add(index + 2)
                index += 1
            index += 1
            continue

        if any(
            arg.startswith(flag) and len(arg) > len(flag)
            for flag in cs.SHELL_SED_OPTIONAL_ARG_FLAGS
            if not flag.startswith("--")
        ):
            index += 1
            continue

        known = _flag_name(arg) in cs.SHELL_SED_KNOWN_FLAGS
        if not known and not arg.startswith("--") and len(arg) > 2:
            # Short flags bundle (`-an`, `-nE`): known when every letter is.
            known = all(f"-{letter}" in cs.SHELL_SED_KNOWN_FLAGS for letter in arg[1:])
        if not known and "=" not in arg:
            # An unclassifiable option may or may not consume the next token,
            # so the script's position is unknown. Refuse rather than guess.
            return "an option this validator cannot interpret"

        if "=" in arg:
            scripts.append(arg.split("=", 1)[1])
        elif arg in ("-e", "--expression") or _sed_cluster_tail(arg) == "-e":
            # A standalone `-e`, or a bundled cluster ending in a bare `-e`
            # (`-ne`, `-nEe`): the script is the NEXT token in both cases.
            # Reading the cluster's own tail letter as the script -- `arg[2:]`
            # of `-ne` is `e` -- left the following `-e 'w FILE'` uncollected.
            add(index + 1)
            index += 1
        elif arg.startswith("-e") and len(arg) > 2:
            # `-eSCRIPT`, and the bundled `-neSCRIPT` where the `-e` is not
            # the cluster's last letter so the remainder is its script.
            attached = arg[2:]
            if _sed_awaits_operand(attached) and index + 1 < len(cmd_parts):
                attached = f"{attached} {cmd_parts[index + 1]}"
            scripts.append(attached)

        index += 1

    for position, arg in enumerate(scripts):
        skeleton = _sed_script_skeleton(arg)
        for pattern, reason in cs.SHELL_SED_EXEC_TOKENS:
            # The s///e and s///w patterns need the real text; the command
            # anchors run on the skeleton so a letter inside a replacement
            # cannot look like a command.
            # Only the s/// forms need the real text (the skeleton blanks
            # the very bodies they match inside); every other anchor runs on
            # the skeleton so user text cannot look like a command.
            if (
                position in ambiguous_slots
                and reason in cs.SHELL_SED_FILENAME_AMBIGUOUS_REASONS
            ):
                # This slot holds a script only under the BSD reading of
                # `-i SUFFIX FILE`; under GNU it is an input FILENAME. The
                # exec anchors cannot survive that ambiguity: `e` matches
                # after any `/`, so `sed -i 's/a/b/' codebase_rag/embedder.py`
                # -- an ordinary in-place edit, verified confined on GNU sed
                # 4.9 -- was refused "via e command", along with 10.7% of this
                # repo's own slash-bearing paths. An exec command cannot
                # appear in a filename slot without a preceding real command,
                # so only the file anchors are meaningful here.
                continue
            if position in ambiguous_slots and reason == cs.SHELL_SED_FILE_REASON:
                # In this slot the token may be an input filename, so the
                # file command must show a separated or path-like target...
                if re.search(cs.SHELL_SED_FILE_ESCAPING, skeleton):
                    return reason
                # ...but a bare `w<name>` is a write to <name> even with no
                # slash in it. BSD sed reads the token AS a script here, so
                # `sed -i bak wMakefile` truncates `Makefile` and
                # `sed -i bak w.gitignore` truncates `.gitignore` -- both
                # verified emptying the file, rc=1 only AFTER the truncation.
                # GNU reads the operands the other way round and is unharmed,
                # but the policy must deny whichever binary is present.
                if re.match(cs.SHELL_SED_BARE_WRITE_TOKEN, skeleton):
                    return reason
                continue
            target = arg if reason.startswith("s///") else skeleton
            if re.search(pattern, target):
                return reason

    return None


def _program_naming_flag(cmd_parts: list[str], known: frozenset[str]) -> str | None:
    """A flag whose value names a program, by explicit list or by suffix.

    The lists cannot contain an option nobody has enumerated, which is how
    `rg --pre` survived nine review rounds. The suffix check backstops them:
    a flag naming a program is conventionally spelled `--...-cmd`,
    `--...-exec`, `--...-pack` and so on. Flags following no convention
    (`--pre`, `--gpg-sign`) stay in the explicit list.
    """
    for arg in cmd_parts[1:]:
        if not arg.startswith("-"):
            continue
        flag = _flag_name(arg)
        if flag in known:
            return flag
        # A `--no-` flag turns the feature OFF and names no program:
        # `git --no-pager log` ends in "pager" but disables the pager.
        if (
            flag.startswith("--")
            and not flag.startswith("--no-")
            and flag.endswith(cs.SHELL_PROGRAM_NAMING_FLAG_SUFFIXES)
        ):
            return flag
    return None


def _rg_exec_flag(cmd_parts: list[str]) -> str | None:
    """Return the ripgrep flag naming a program rg would run."""
    if not cmd_parts or cmd_parts[0] != cs.SHELL_CMD_RG:
        return None

    return _program_naming_flag(cmd_parts, cs.SHELL_RG_EXEC_FLAGS)


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

    if flag := _git_exec_flag(cmd_parts):
        return True, f"git {flag} names a program git will run"

    if form := _git_exec_subcommand(cmd_parts):
        return True, f"git {form} runs a caller-supplied command"

    if flag := _rg_exec_flag(cmd_parts):
        return True, f"rg {flag} names a program rg will run"

    if construct := _sed_exec_construct(cmd_parts):
        return True, f"sed can run a command or write a file via {construct}"

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
    project_root: Path | None = None,
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

    # Every guard below keys on the program NAME, so a path-qualified spelling
    # would reach none of them: `/usr/bin/xargs -n1 sh -c id` was verified
    # executing under --yolo while bare `xargs` was blocked, and the same held
    # for sed, git and rg. Refuse the qualified form outright rather than
    # reduce it to a basename -- the allowlist holds bare names, so basenaming
    # would also admit `/tmp/evil/sed`, a DIFFERENT binary wearing an
    # allowlisted name. Refusing loses nothing: every allowlisted command is
    # resolved through PATH at execution anyway.
    if "/" in base_cmd or os.sep in base_cmd:
        return te.COMMAND_DANGEROUS_BLOCKED.format(
            cmd=base_cmd,
            reason=(
                "a path-qualified program name bypasses every name-keyed "
                "check; invoke the command by its bare name"
            ),
        )

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
            project_root,
        ):
            return nested

    is_dangerous, reason = _is_dangerous_command(cmd_parts, segment, bypass_allowlist)
    if is_dangerous:
        return te.COMMAND_DANGEROUS_BLOCKED.format(cmd=base_cmd, reason=reason)

    # Inside the recursion, so `xargs git --git-dir=EVIL pwn` is caught too.
    # Running it only from execute() checked the top-level segment alone, and
    # the nested spelling executed the planted alias with rc=0 -- nesting
    # weakening the decision, which is exactly what this recursion exists to
    # prevent. Skipped when no root is supplied, as the direct unit tests of
    # other guards do.
    if project_root is not None:
        is_dangerous, reason = _git_escapes_project(cmd_parts, project_root)
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
                        segment,
                        available_commands,
                        bypass_allowlist=bypass_allowlist,
                        project_root=self.project_root,
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
