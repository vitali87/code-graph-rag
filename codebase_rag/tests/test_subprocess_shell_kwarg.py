"""`subprocess_shell` needs a `shell=True` keyword argument of the call itself.

The rule fired when the text `shell=True` appeared anywhere under the call:
in a string argument (`["grep", "shell=True", path]`), in a command string,
in a comment between the arguments, or as another call's keyword. None of
those runs a shell, so safe calls were reported as command injection
(issue #2869).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_ast_grep_analyzer import _fire

pytest.importorskip("ast_grep_py")

SUBPROCESS_SHELL = "subprocess_shell"


def _flagged(tmp_path: Path, body: str) -> list[int]:
    src = f"import subprocess\n\n\ndef f(cmd, build, use_shell):\n{body}"
    return sorted(
        int(p[cs.KEY_START_LINE]) - 4
        for p in _fire(tmp_path, "s.py", src)
        if p[cs.KEY_NAME] == SUBPROCESS_SHELL
    )


def test_the_issue_flags_only_the_shell_call(tmp_path: Path) -> None:
    body = (
        "    subprocess.run(cmd, shell=True)\n"
        "    subprocess.run(cmd, shell=False)\n"
        '    subprocess.run(["grep", "shell=True", "/etc/config"])\n'
        '    subprocess.run("echo configured shell=True now")\n'
    )
    assert _flagged(tmp_path, body) == [1]


def test_text_that_is_not_the_calls_keyword_is_not_a_shell(tmp_path: Path) -> None:
    body = (
        "    subprocess.run(\n"
        "        cmd,  # shell=True would be unsafe here\n"
        "    )\n"
        "    subprocess.run(build(shell=True))\n"
        "    subprocess.check_output(cmd, env={'X': 'shell=True'})\n"
    )
    assert _flagged(tmp_path, body) == []


def test_every_spelling_of_the_keyword_is_still_flagged(tmp_path: Path) -> None:
    # Negatives: a real `shell=True` fires however the call is laid out.
    body = (
        "    subprocess.Popen(cmd, shell = True)\n"
        "    subprocess.check_output(cmd, text=True, shell=True)\n"
        "    subprocess.call(\n"
        "        cmd,\n"
        "        shell=True,\n"
        "    )\n"
    )
    assert _flagged(tmp_path, body) == [1, 2, 3]


def test_a_shell_flag_from_a_variable_is_left_alone(tmp_path: Path) -> None:
    # Negative: as before, only the literal `True` is flagged.
    assert _flagged(tmp_path, "    subprocess.run(cmd, shell=use_shell)\n") == []
