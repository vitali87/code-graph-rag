"""Issue #2359: confined shell reads run without a prompt; refusals never prompt.

Every `ls`, `rg`, `head` or `wc` the agent ran in interactive mode asked for
approval, although the same reads are allowed unattended in non-interactive
runs and the file reader tool reads any project file without asking. And a
command the allowlist rejects (`grep`) was put to the user first, then
refused whatever they answered.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic_ai import ApprovalRequired

from codebase_rag import constants as cs
from codebase_rag import prompts
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.tools.shell_command import (
    ShellCommander,
    _is_windows_namesake,
    _pipeline_env,
    create_shell_command_tool,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("def run():\n    return 1\n")
    (root / "README.md").write_text("# demo\nline two\n")
    return root


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The pipelines that reached the process layer, which is stubbed out.

    These tests guard the approval decision, not which programs the host
    has: the CI runners carry no ripgrep, and on Windows `find` can be
    System32's find.exe. Everything above the spawn (the prompt, the
    refusals, the executor's own safety checks) runs for real.
    """
    pipelines: list[list[str]] = []

    async def record(
        _commander: ShellCommander, segments: list[str]
    ) -> tuple[int, bytes, bytes]:
        pipelines.append(segments)
        return 0, b"", b""

    monkeypatch.setattr(ShellCommander, "_execute_pipeline", record)
    return pipelines


@pytest.fixture
def no_git_for_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # On a Windows runner the executor would put the real Git usr\bin first
    # on PATH, and its tools would answer before the stand-ins below.
    monkeypatch.setattr(cs, "SHELL_WINDOWS_GIT_USR_BIN", str(tmp_path / "no-git"))


def _tool(project: Path):
    return create_shell_command_tool(ShellCommander(str(project), timeout=10))


def _unapproved() -> MagicMock:
    ctx = MagicMock()
    ctx.tool_call_approved = False
    return ctx


def _stand_in_program(directory: Path, name: str) -> None:
    # Found by shutil.which on every host: an executable bare name for POSIX,
    # a PATHEXT extension for Windows. Never run.
    directory.mkdir(parents=True, exist_ok=True)
    for filename in (name, f"{name}.exe"):
        program = directory / filename
        program.write_text("")
        program.chmod(0o755)


@pytest.mark.parametrize(
    ("command", "pipeline"),
    [
        ("ls pkg", ["ls pkg"]),
        ("rg -n run pkg", ["rg -n run pkg"]),
        ("head -1 README.md", ["head -1 README.md"]),
        ("wc -l README.md", ["wc -l README.md"]),
        ("cat pkg/mod.py | wc -l", ["cat pkg/mod.py", "wc -l"]),
        ("find pkg -name '*.py'", ["find pkg -name '*.py'"]),
    ],
)
async def test_a_confined_read_runs_without_a_prompt(
    project: Path, spawned: list[list[str]], command: str, pipeline: list[str]
) -> None:
    # An unapproved context: reaching execution at all means no prompt.
    result = await _tool(project).function(_unapproved(), command)

    assert result.return_code == 0, result.stderr
    assert spawned == [pipeline]


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/hostname",
        "ls ..",
        "rg -L run .",
        "sort -o out.txt README.md",
        "cat README.md > copy.md",
        "find . -delete",
        "rm README.md",
        "git log -1",
    ],
)
async def test_anything_else_still_asks(
    project: Path, spawned: list[list[str]], command: str
) -> None:
    # Negative: absolute paths, traversal, symlink following, write forms,
    # redirects, mutating find, writes and git keep the prompt, and nothing
    # runs before it is answered.
    tool = _tool(project)
    ctx = _unapproved()
    with pytest.raises(ApprovalRequired):
        await tool.function(ctx, command)

    assert spawned == []


@pytest.fixture
def outside_patterns(tmp_path: Path) -> str:
    # Beside the project root, not in it. POSIX spelling, because shlex would
    # eat a Windows path's backslashes.
    patterns = tmp_path / "outside-patterns.txt"
    patterns.write_text("OUTSIDE_SECRET\n")
    return patterns.as_posix()


@pytest.mark.parametrize(
    "template",
    [
        # The reproduction: a value attached to a short option is a path
        # too, and ripgrep reads it and quotes its patterns in stderr.
        "rg -f{outside} README.md",
        "rg -nf{outside} README.md",
        "rg -f {outside} README.md",
        "rg --file={outside} README.md",
        "rg --file {outside} README.md",
        "rg --ignore-file {outside} run pkg",
        "rg --ignore-file={outside} run pkg",
        "rg -f../outside-patterns.txt README.md",
        "rg -nf../outside-patterns.txt README.md",
    ],
)
async def test_an_option_naming_an_outside_file_still_asks(
    project: Path, spawned: list[list[str]], outside_patterns: str, template: str
) -> None:
    tool = _tool(project)
    with pytest.raises(ApprovalRequired):
        await tool.function(_unapproved(), template.format(outside=outside_patterns))

    assert spawned == []


async def test_an_attached_option_value_through_an_outward_symlink_still_asks(
    project: Path, spawned: list[list[str]], outside_patterns: str
) -> None:
    (project / "linked_patterns").symlink_to(outside_patterns)

    with pytest.raises(ApprovalRequired):
        await _tool(project).function(_unapproved(), "rg -flinked_patterns pkg")

    assert spawned == []


@pytest.mark.parametrize(
    "command",
    [
        # An option the confinement rules do not know may read a file, so
        # it asks rather than being assumed harmless.
        "rg --some-future-option run pkg",
        "rg -Y run pkg",
        "cat --unknown README.md",
        "head -Q README.md",
        "find pkg -newerish x",
        # GNU abbreviations are not resolved; the full spelling is required.
        "sort --rev README.md",
    ],
)
async def test_an_option_the_rules_do_not_know_asks(
    project: Path, spawned: list[list[str]], command: str
) -> None:
    with pytest.raises(ApprovalRequired):
        await _tool(project).function(_unapproved(), command)

    assert spawned == []


@pytest.mark.parametrize(
    "command",
    [
        # Negative: a patterns file inside the project, in every spelling,
        # and the everyday searches and reads stay prompt-free.
        "rg -f patterns.txt README.md",
        "rg -fpatterns.txt README.md",
        "rg -nfpatterns.txt README.md",
        "rg --file=patterns.txt README.md",
        "rg --ignore-file patterns.txt run pkg",
        "rg foo pkg/",
        "rg -n -i --type py -g '*.py' run pkg",
        "rg -nC2 -e run --no-heading pkg",
        "rg -l --hidden --max-count=3 run",
        "head -n 5 README.md",
        "tail -20 README.md",
        "cut -d/ -f2 README.md",
        "sort -k2,2 -t: README.md",
        "uniq -c README.md",
        "wc -lw README.md",
        "ls -la pkg",
        "cat -n README.md",
        "find pkg -type f -name '*.py' -maxdepth 2 -print",
        "find pkg -size -10k -newer README.md",
    ],
)
async def test_a_known_option_inside_the_project_runs_without_a_prompt(
    project: Path, spawned: list[list[str]], command: str
) -> None:
    (project / "patterns.txt").write_text("run\n")

    result = await _tool(project).function(_unapproved(), command)

    assert result.return_code == 0, result.stderr
    assert spawned == [[command]]


async def test_a_command_the_allowlist_rejects_is_refused_without_a_prompt(
    project: Path, spawned: list[list[str]]
) -> None:
    result = await _tool(project).function(_unapproved(), "grep -rn run pkg")

    assert result.return_code != 0
    assert "rg" in result.stderr
    assert spawned == []


async def test_a_refused_command_is_refused_even_when_approved(
    project: Path, spawned: list[list[str]]
) -> None:
    # Negative: approval never widens the allowlist.
    ctx = MagicMock()
    ctx.tool_call_approved = True

    result = await _tool(project).function(ctx, "curl http://example.com")

    assert result.return_code != 0
    assert "allowlist" in result.stderr
    assert spawned == []


@pytest.mark.usefixtures("no_git_for_windows")
async def test_a_read_whose_program_is_missing_says_so_plainly(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: a host without ripgrep. The description steers the agent to
    # `rg`, so its absence must come back as a sentence the agent can act on,
    # not a spawn error and not a prompt. The spawn is real; PATH is emptied
    # so no host's rg can answer.
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    result = await _tool(project).function(_unapproved(), "rg -n run pkg")

    assert result.return_code == cs.SHELL_RETURN_CODE_ERROR
    assert "'rg' is not installed" in result.stderr, result.stderr
    assert "rg -n run pkg" in result.stderr, result.stderr


@pytest.mark.usefixtures("no_git_for_windows")
async def test_a_windows_namesake_is_named_not_run(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: on the Windows runner `find` resolved to System32's find.exe,
    # which answered `find pkg -name '*.py'` with `File not found - *.py`. A
    # stand-in system directory reproduces that resolution on any host.
    windows = tmp_path / "Windows"
    _stand_in_program(windows / "System32", "find")
    monkeypatch.setenv(cs.SHELL_WINDOWS_SYSTEM_ROOT_ENV, str(windows))
    monkeypatch.setenv("PATH", str(windows / "System32"))

    result = await _tool(project).function(_unapproved(), "find pkg -name '*.py'")

    assert result.return_code == cs.SHELL_RETURN_CODE_ERROR
    assert "the Windows program" in result.stderr, result.stderr
    assert "POSIX 'find'" in result.stderr, result.stderr


def test_only_a_posix_name_from_the_windows_directory_is_a_namesake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: Git's find, a System32 program with no POSIX namesake, and a
    # host with no Windows directory at all all run as before.
    windows = tmp_path / "Windows"
    system32 = windows / "System32"
    git_find = tmp_path / "Git" / "usr" / "bin" / "find.exe"
    monkeypatch.setenv(cs.SHELL_WINDOWS_SYSTEM_ROOT_ENV, str(windows))

    assert _is_windows_namesake("find", str(system32 / "find.exe"))
    assert _is_windows_namesake("sort", str(system32 / "sort.exe"))
    assert not _is_windows_namesake("find", str(git_find))
    assert not _is_windows_namesake("where", str(system32 / "where.exe"))

    monkeypatch.delenv(cs.SHELL_WINDOWS_SYSTEM_ROOT_ENV)
    assert not _is_windows_namesake("find", str(system32 / "find.exe"))


def test_git_posix_tools_come_first_on_a_windows_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The runner's PATH already held Git's usr\bin, but after System32, so
    # the old "prepend unless present" left find.exe winning.
    system32 = tmp_path / "System32"
    git_usr_bin = tmp_path / "Git" / "usr" / "bin"
    system32.mkdir()
    git_usr_bin.mkdir(parents=True)
    monkeypatch.setattr(cs, "SHELL_WINDOWS_GIT_USR_BIN", str(git_usr_bin))
    monkeypatch.setenv("PATH", os.pathsep.join([str(system32), str(git_usr_bin)]))
    monkeypatch.setattr(sys, "platform", "win32")

    assert _pipeline_env()["PATH"].split(os.pathsep) == [
        str(git_usr_bin),
        str(system32),
    ]


def test_a_path_without_git_for_windows_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: no Git install (or not Windows at all) changes nothing.
    system32 = tmp_path / "System32"
    system32.mkdir()
    monkeypatch.setattr(cs, "SHELL_WINDOWS_GIT_USR_BIN", str(tmp_path / "no-git"))
    monkeypatch.setenv("PATH", str(system32))
    monkeypatch.setattr(sys, "platform", "win32")

    assert _pipeline_env()["PATH"] == str(system32)


def test_the_shell_description_sends_structure_to_the_graph() -> None:
    description = td.SHELL_COMMAND
    assert td.AgenticToolName.QUERY_GRAPH in description
    assert "grep" in description
    assert "rg" in description


def test_the_prompt_routes_structural_questions_to_the_graph() -> None:
    prompt = prompts.build_rag_orchestrator_prompt([])
    assert "fallback" in prompt.lower()
    for kind in ("callers", "inherit", "most-called", "layout", "dependencies"):
        assert kind in prompt.lower(), kind
