# virtualenv <= 21.7.12 is behind four advisories (GHSA-94p9-xgh2-xp45,
# GHSA-9h9j-4vrj-gf7g, GHSA-x78j-v8h9-3j2q, GHSA-p58f-9548-mpm2): unverified
# seed wheels, pyvenv.cfg injection through --prompt, and command injection
# from activation scripts. pre-commit pulls it into the dev group and uses it
# to build every Python hook environment.
from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import virtualenv
from packaging.version import Version

_INJECTED_KEY = "injected-key"


def test_virtualenv_dependency_is_patched() -> None:
    # 21.7.13 is the first release outside every advisory range above.
    assert Version(version("virtualenv")) >= Version("21.7.13"), version("virtualenv")


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
