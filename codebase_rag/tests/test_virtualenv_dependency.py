# virtualenv <= 21.7.12 is behind four advisories (GHSA-94p9-xgh2-xp45,
# GHSA-9h9j-4vrj-gf7g, GHSA-x78j-v8h9-3j2q, GHSA-p58f-9548-mpm2): unverified
# seed wheels, pyvenv.cfg injection through --prompt, and command injection
# from activation scripts. pre-commit pulls it into the dev group and uses it
# to build every Python hook environment. Every release before 21.14.9 is also
# inside at least one of five later advisories, which OSV-Scanner fails main
# on: GHSA-5vjq-rrrf-7h2q (fixed in 21.14.4), GHSA-vxj4-q4g9-crj7 (21.14.7),
# and GHSA-43mp-35q3-x3fq, GHSA-c978-59m8-c77v, GHSA-cw65-gx24-rc82 (21.14.9).
# 21.14.9 itself regressed into GHSA-cx6f-vjhg-g3cc (CVE-2026-115586, fixed in
# 21.14.13): when promptvars was off at activation, the bash bin/activate stored
# the --prompt value in PS1 with $ and backticks unescaped, so turning
# promptvars back on, or a child bash inheriting PS1, ran any command
# substitution in it.
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
import virtualenv
from packaging.version import Version

from codebase_rag import constants as cs

_INJECTED_KEY = "injected-key"
_INJECTED_OUTPUT = "pwned"
# Activate with promptvars off, the state the vulnerable branch keyed on, then
# turn it back on and expand PS1 the way bash draws it. The first line is a
# control: a bare command substitution, which must run.
_EXPAND_PROMPT_AFTER_ACTIVATION = f"""\
PS1=
shopt -u promptvars
source "$1"
shopt -s promptvars
control='$(echo {_INJECTED_OUTPUT})'
printf '%s\\n' "${{control@P}}" "${{PS1@P}}"
"""
_SHELL_STATE_VARIABLES = ("BASH_ENV", "POSIXLY_CORRECT", "VIRTUAL_ENV_DISABLE_PROMPT")


def test_virtualenv_dependency_is_patched() -> None:
    # 21.14.13 is the first release outside every advisory range above.
    assert Version(version("virtualenv")) >= Version("21.14.13"), version("virtualenv")


def test_prompt_newline_cannot_inject_a_pyvenv_cfg_key(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    virtualenv.cli_run(
        [str(env_dir), "--no-seed", "--prompt", f"x\n{_INJECTED_KEY} = true"],
        setup_logging=False,
    )
    keys = [
        line.partition("=")[0].strip()
        for line in (env_dir / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    ]
    # Known positive: the prompt is recorded, so the absence below is not an
    # empty or unread file.
    assert "prompt" in keys, keys
    assert _INJECTED_KEY not in keys, keys


@pytest.mark.skipif(
    sys.platform == cs.PLATFORM_WINDOWS, reason="bin/activate is the POSIX layout"
)
@pytest.mark.parametrize(
    "substitution", [f"$(echo {_INJECTED_OUTPUT})", f"`echo {_INJECTED_OUTPUT}`"]
)
def test_prompt_command_substitution_stays_inert_in_bash(
    tmp_path: Path, substitution: str
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        # pytest.skip is not annotated NoReturn, so the checker cannot narrow
        # `bash` to str across it on its own.
        pytest.skip("the bash activator needs bash")
    assert bash is not None
    probe = subprocess.run(
        [bash, "-c", 'p=x; : "${p@P}"'], capture_output=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip("${PS1@P} needs bash 4.4+; macOS ships 3.2 as /bin/bash")
    env_dir = tmp_path / "env"
    virtualenv.cli_run(
        [str(env_dir), "--no-seed", "--prompt", f"x{substitution}y"],
        setup_logging=False,
    )
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _SHELL_STATE_VARIABLES
    }
    result = subprocess.run(
        [
            bash,
            "--noprofile",
            "--norc",
            "-c",
            _EXPAND_PROMPT_AFTER_ACTIVATION,
            "bash",
            str(env_dir / "bin" / "activate"),
        ],
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        env=env,
        check=True,
    )
    control, prompt = result.stdout.splitlines()
    # Known positive: this bash runs command substitutions found in a prompt,
    # so the inert prompt below is the escaping, not an expansion that never ran.
    assert control == _INJECTED_OUTPUT, result.stdout
    # The prompt is still drawn, with the substitution shown as text.
    assert f"echo {_INJECTED_OUTPUT}" in prompt, prompt
    assert f"x{_INJECTED_OUTPUT}y" not in prompt, prompt
