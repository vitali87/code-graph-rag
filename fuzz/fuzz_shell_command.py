"""Fuzz the EXECUTE_SHELL gates against the production entry points.

Three gates stand between a model-authored command string and a subprocess,
and their failure modes are security failures rather than crashes:

* `ShellCommander.refusal` -- the whole pre-spawn screen `execute` runs, in
  both the default and the YOLO mode;
* `_requires_approval` -- whether the default mode asks a human first;
* `_noninteractive_denial` -- the confinement gate for operator-less runs,
  where no human is asked at all.

The harness calls those functions, not a mirror of them: an earlier version
re-implemented the screen and asserted properties of its own copy, so a
drift between the copy and `execute` was invisible.

Properties, each checked on every input:

1. Totality. No gate raises; an exception reaches the tool path as an
   unhandled error rather than a refusal.
2. Compositionality. When a command is allowed, every segment it would spawn
   is allowed ON ITS OWN, parses back to the same single segment and has a
   program to run. This is the laundering property: an operator, a quote or
   an escape must not let a segment through that would be refused alone.
3. Suffix laundering. Property 2 again on `<input> <op> <suffix>`, where the
   suffix is refused alone by a structural check (not a raw-string pattern),
   so the fuzzer explores prefixes that might swallow or split it.
4. Approval. A command the default mode runs without asking spawns only the
   approval-free programs, with no redirect token.
5. xargs monotonicity. When a command is refused, `xargs <command>` is too,
   in both modes. (`find -exec` and launcher wrapping are NOT monotonic in
   the default mode by design: those still need approval.)
6. Respelling. Re-quoting the tokens of a single-segment command (same
   `shlex.split`, same segmentation) never changes a structural verdict.
7. Confinement. When the non-interactive gate allows a command and the
   screen would run it, every program spawned is a read-only one and every
   operand, `--opt=value` and file-option value stays inside a real project
   root that contains symlinks out of it.

Run locally (see CONTRIBUTING.md for building atheris on macOS):

    uv run --extra fuzz python fuzz/fuzz_shell_command.py -max_total_time=60
"""

import atexit
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

# `shell_command` imports pydantic_ai, which reaches logfire's pydantic plugin.
# That plugin patches pydantic at import time via `inspect.getsource`, which
# raises OSError inside a PyInstaller bundle where there is no source to read,
# killing the target before it fuzzes a single input. Set before the import.
os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "1")

import atheris

with atheris.instrument_imports():
    from loguru import logger

    from codebase_rag.constants import security as cs
    from codebase_rag.tools.shell_command import (
        _CONFINED_READ_OPTION_KINDS,
        ShellCommander,
        _check_pipeline_patterns,
        _check_segment_patterns,
        _has_subshell,
        _noninteractive_denial,
        _parse_command,
        _parse_find_options,
        _parse_getopt_options,
        _requires_approval,
    )

MAX_COMMAND_CHARS = 4096

# Hard-coded rather than read from settings, so widening the approval-free or
# non-interactive sets is a deliberate change here too. The drift tests in
# `test_fuzz_harnesses.py` fail when the defaults and these disagree.
APPROVAL_FREE_PROGRAMS = frozenset({"echo", "pwd", "tr"})
NONINTERACTIVE_PROGRAMS = APPROVAL_FREE_PROGRAMS | frozenset(
    {"cat", "cut", "find", "head", "ls", "rg", "sort", "tail", "uniq", "wc"}
)


def _build_sandbox() -> Path:
    """A real project root with symlinks pointing out of it.

    Confinement is about what a path RESOLVES to, so it needs a filesystem:
    `linked_file` reads like a repo file and is the host's secret.
    """
    base = Path(tempfile.mkdtemp(prefix="cgr-fuzz-shell-")).resolve()
    atexit.register(shutil.rmtree, base, ignore_errors=True)
    outside = base / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    root = base / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "data.txt").write_text("data\n", encoding="utf-8")
    (root / "sub" / "inner.txt").write_text("inner\n", encoding="utf-8")
    links = {
        "linked_dir": outside,
        "linked_file": outside / "secret.txt",
        "-linked": outside / "secret.txt",
        "inner_link": root / "data.txt",
        "loop": Path("loop"),
    }
    for name, target in links.items():
        try:
            (root / name).symlink_to(target)
        except OSError:
            # No symlink privilege (some Windows hosts): the remaining
            # properties still hold, they just see fewer escape routes.
            continue
    return root


SANDBOX_ROOT = _build_sandbox()
# One real root for every gate, so the `rm` and `git` guards meet the same
# symlinks and loop the non-interactive gate does. A temp dir rather than one
# derived from `__file__`, which under PyInstaller is the per-run `_MEIPASS`.
STRICT = ShellCommander(str(SANDBOX_ROOT))
YOLO = ShellCommander(str(SANDBOX_ROOT), is_yolo=lambda: True)
MODES = (("default", STRICT), ("yolo", YOLO))


# Each is refused ALONE in both modes by a structural check rather than a
# raw-string pattern, so property 3 cannot pass merely because the pattern
# still matches the concatenation. Verified at import below.
STRUCTURAL_SUFFIXES = (
    "rm ../outside_project",
    "git -c core.pager=id log",
    "git -C ../other log",
    "git --git-dir=/tmp/evil/.git log",
    "rg --pre=id x",
    "sed s/x/y/e f",
    "git config core.pager id",
    "xargs -n1 sed s/x/y/e f",
    "xargs git -c core.pager=id log",
)
OPERATORS = ("&&", "||", ";", "|")
SUFFIX_CHOICES = len(STRUCTURAL_SUFFIXES) * len(OPERATORS)
RESPELL_SEEDS = 0xFFFF
RESPELL_STYLES = 5

for _suffix in STRUCTURAL_SUFFIXES:
    for _mode, _commander in MODES:
        if _commander.refusal(_suffix) is None:
            raise SystemExit(f"suffix {_suffix!r} is allowed alone in {_mode} mode")
        if _check_segment_patterns(_suffix) or _check_pipeline_patterns(_suffix):
            raise SystemExit(f"suffix {_suffix!r} is refused by a raw pattern")


def _total(label: str, func, *args):  # type: ignore[no-untyped-def]
    try:
        return func(*args)
    except RecursionError:
        raise AssertionError(
            f"{label} hit the recursion limit on {args[0]!r}"
        ) from None
    except Exception as exc:  # noqa: BLE001 - the point of the harness
        raise AssertionError(
            f"{label} raised {type(exc).__name__}: {exc} on {args[0]!r}"
        ) from exc


def _spawned(commander: ShellCommander, command: str) -> list[str] | None:
    """The segments `execute` would spawn, or None when it refuses."""
    err, groups = commander._screen(command)
    if err is not None:
        return None
    return [segment for group in groups for segment in group.commands]


def _check_composition(mode: str, commander: ShellCommander, command: str) -> None:
    segments = _total(f"{mode} screen", _spawned, commander, command)
    if segments is None:
        return
    for segment in segments:
        argv = shlex.split(segment)
        if not argv:
            raise AssertionError(
                f"{mode} allowed {command!r}, which spawns an empty argv "
                f"from segment {segment!r}"
            )
        alone = _parse_command(segment)
        if (
            len(alone) != 1
            or len(alone[0].commands) != 1
            or shlex.split(alone[0].commands[0]) != argv
        ):
            raise AssertionError(
                f"{mode} allowed {command!r}, whose segment {segment!r} "
                "parses differently on its own"
            )
        if (reason := _total(f"{mode} screen", commander.refusal, segment)) is not None:
            raise AssertionError(
                f"{mode} allowed {command!r}, which spawns segment "
                f"{segment!r} that is refused on its own: {reason}"
            )


def _check_approval(command: str) -> None:
    if _total("approval gate", _requires_approval, command):
        return
    segments = _total("default screen", _spawned, STRICT, command)
    if segments is None:
        return
    for segment in segments:
        argv = shlex.split(segment)
        if argv[0] not in APPROVAL_FREE_PROGRAMS or any(
            token in cs.SHELL_REDIRECT_OPERATORS for token in argv
        ):
            raise AssertionError(
                f"{command!r} runs without approval but spawns {argv!r}"
            )


def _single_argv(command: str) -> list[str] | None:
    groups = _parse_command(command)
    if len(groups) != 1 or len(groups[0].commands) != 1:
        return None
    try:
        # Split what the parser keeps, not the raw text: `rm;` spawns `rm`.
        argv = shlex.split(groups[0].commands[0])
    except ValueError:
        return None
    return argv or None


def _check_xargs(mode: str, commander: ShellCommander, command: str) -> None:
    argv = _single_argv(command)
    if argv is None or argv[0].startswith("-"):
        return
    if _total(f"{mode} screen", commander.refusal, command) is None:
        return
    for wrapper in ("xargs", "xargs -n1"):
        wrapped = f"{wrapper} {command}"
        if _total(f"{mode} screen", commander.refusal, wrapped) is None:
            raise AssertionError(
                f"{mode} refuses {command!r} but allows the wrapping {wrapped!r}"
            )


def _respell_token(token: str, style: int) -> str:
    if not token:
        return "''"
    if style == 1:
        return "'" + token.replace("'", "'\"'\"'") + "'"
    if style == 2:
        return "".join("\\" + char for char in token)
    if style == 3 and len(token) > 1:
        return shlex.quote(token[:1]) + shlex.quote(token[1:])
    if style == 4:
        return '"' + token.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return shlex.quote(token)


def _respell(argv: list[str], seed: int) -> str:
    # The seed's base-5 digits pick each token's style, cycling once the
    # 16-bit seed runs out of digits, so every input respells reproducibly.
    digits = []
    while seed:
        seed, digit = divmod(seed, RESPELL_STYLES)
        digits.append(digit)
    digits = digits or [0]
    return " ".join(
        _respell_token(token, digits[i % len(digits)]) for i, token in enumerate(argv)
    )


def _pattern_decided(command: str) -> bool:
    # Segment patterns run on the whole command AND on each parsed segment,
    # where a separator the parser dropped (`rm -rf /;`) no longer hides them.
    segments = [
        segment for group in _parse_command(command) for segment in group.commands
    ]
    return bool(
        _has_subshell(command)
        or _check_pipeline_patterns(command)
        or any(_check_segment_patterns(text) for text in (command, *segments))
    )


def _check_respelling(command: str, seed: int) -> None:
    argv = _single_argv(command)
    if argv is None:
        return
    respelled = _respell(argv, seed)
    if respelled == command or _single_argv(respelled) != argv:
        return
    # Raw-string patterns see the spelling by design, and without a shell
    # their verdict on a re-quoted token carries no execution meaning.
    if _pattern_decided(command) or _pattern_decided(respelled):
        return
    verdicts = (
        ("default screen", STRICT.refusal),
        ("yolo screen", YOLO.refusal),
        ("approval gate", lambda c: not _requires_approval(c)),
        ("non-interactive gate", lambda c: _noninteractive_denial(c, SANDBOX_ROOT)),
    )
    for label, verdict in verdicts:
        before = _total(label, verdict, command) is None
        after = _total(label, verdict, respelled) is None
        if before != after:
            raise AssertionError(
                f"respelling {command!r} as {respelled!r} flipped the {label} "
                f"from {'allow' if before else 'refuse'} to "
                f"{'allow' if after else 'refuse'}"
            )


def _contained(value: str) -> bool:
    """Whether a child running in the root would stay inside it reading `value`.

    Judged by what the path MEANS on this host, not how it is spelt: a
    backslash is a filename character on POSIX, and the gate may refuse more
    spellings than this (a leading `~`, any `..`) without that being a bug.
    """
    if "\x00" in value:
        # exec refuses an argument with a NUL byte, so no child receives it.
        return True
    try:
        resolved = Path(os.path.realpath(SANDBOX_ROOT / value))
    except (OSError, ValueError):
        return True
    return resolved == SANDBOX_ROOT or SANDBOX_ROOT in resolved.parents


def _file_option_values(argv: list[str]) -> list[str]:
    """Values of the options in `argv` that name a file the command opens.

    Read with the gate's own option parser, so every spelling it models
    (`-fF`, `-nfF`, `-f F`, `--file F`) yields the file. ripgrep also reads
    `-f=F` as F, so a value is checked with and without a leading `=`.
    """
    kinds = _CONFINED_READ_OPTION_KINDS.get(argv[0], {})
    scan = (
        _parse_find_options if argv[0] == cs.SHELL_CMD_FIND else _parse_getopt_options
    )
    values: list[str] = []
    for option in scan(argv, kinds):
        if option.kind == cs.ReadOptionKind.PATH:
            values += [option.value, option.value.removeprefix("=")]
    return values


def _path_values(argv: list[str]) -> list[str]:
    values: list[str] = []
    operands_only = False
    for token in argv[1:]:
        if not operands_only and token == "--":
            operands_only = True
        elif operands_only or not token.startswith("-"):
            values.append(token)
        elif "=" in token:
            values.append(token.partition("=")[2])
    return values + _file_option_values(argv)


def _check_confinement(command: str) -> None:
    if _total("non-interactive gate", _noninteractive_denial, command, SANDBOX_ROOT):
        return
    segments = _total("default screen", _spawned, STRICT, command)
    if segments is None:
        return
    for segment in segments:
        argv = shlex.split(segment)
        if argv[0] not in NONINTERACTIVE_PROGRAMS:
            raise AssertionError(
                f"the non-interactive gate allowed {command!r}, which runs the "
                f"non-read-only program {argv[0]!r}"
            )
        for value in _path_values(argv):
            if value and not _contained(value):
                raise AssertionError(
                    f"the non-interactive gate allowed {command!r}, whose path "
                    f"{value!r} escapes the project root"
                )


def check_command(command: str, suffix_choice: int, respell_seed: int) -> None:
    for mode, commander in MODES:
        _check_composition(mode, commander, command)

    suffix = STRUCTURAL_SUFFIXES[suffix_choice % len(STRUCTURAL_SUFFIXES)]
    operator = OPERATORS[suffix_choice // len(STRUCTURAL_SUFFIXES) % len(OPERATORS)]
    combined = f"{command} {operator} {suffix}"
    for mode, commander in MODES:
        try:
            _check_composition(mode, commander, combined)
        except AssertionError as exc:
            raise AssertionError(f"suffix laundering: {exc}") from exc

    _check_approval(command)
    for mode, commander in MODES:
        _check_xargs(mode, commander, command)
    _check_respelling(command, respell_seed)
    _check_confinement(command)


def fuzz_shell_command(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    # Integers come off the back of the buffer, the command off the front, so
    # a seed is `<spec byte><command><respell seed: 2 bytes><suffix: 1 byte>`.
    suffix_choice = fdp.ConsumeIntInRange(0, SUFFIX_CHOICES - 1)
    respell_seed = fdp.ConsumeIntInRange(0, RESPELL_SEEDS)
    command = fdp.ConsumeUnicodeNoSurrogates(MAX_COMMAND_CHARS)
    check_command(command, suffix_choice, respell_seed)


def main() -> None:
    logger.disable("codebase_rag")
    atheris.Setup(sys.argv, atheris.instrument_func(fuzz_shell_command))
    atheris.Fuzz()


if __name__ == "__main__":
    main()
