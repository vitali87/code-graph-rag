import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "version-bump.yml"
)


def _bump(current: str, bump_type: str = "patch") -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("the Ubuntu workflow requires bash")
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["bump-version"]["steps"]
    step = next(s for s in steps if s["name"] == "Bump version")
    script = (
        step["run"]
        .replace("${{ steps.get_version.outputs.current }}", current)
        .replace("${{ steps.bump_type.outputs.type }}", bump_type)
    )
    env = {
        **os.environ,
        "GITHUB_OUTPUT": os.devnull,
        "VERSION_COMPONENT_CAP": str(workflow["env"]["VERSION_COMPONENT_CAP"]),
    }
    return subprocess.run(
        [bash, "-e", "-c", script], capture_output=True, text=True, env=env, check=False
    )


def _new_version(current: str, bump_type: str = "patch") -> str:
    result = _bump(current, bump_type)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout.split(" to ")[1].split(" ")[0]


def test_cap_is_one_thousand() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["env"]["VERSION_COMPONENT_CAP"] == 1000


@pytest.mark.parametrize(
    ("current", "bump_type", "expected"),
    [
        ("0.0.5", "patch", "0.0.6"),
        ("0.0.998", "patch", "0.0.999"),
        ("0.0.999", "patch", "0.1.0"),
        ("0.0.1006", "patch", "0.1.0"),
        ("0.1.0", "patch", "0.1.1"),
        ("0.999.999", "patch", "1.0.0"),
        ("0.1500.3", "patch", "1.0.0"),
        ("0.998.7", "minor", "0.999.0"),
        ("0.999.7", "minor", "1.0.0"),
        ("0.999.7", "major", "1.0.0"),
        ("0.0.1006", "minor", "0.1.0"),
    ],
)
def test_components_roll_over_at_the_cap(
    current: str, bump_type: str, expected: str
) -> None:
    assert _new_version(current, bump_type) == expected


@pytest.mark.parametrize(
    "current", ["0.0", "0.0.1a", "a[$(touch /tmp/pwn)].0.1", "0.0.1\n0.0.2"]
)
def test_malformed_current_version_is_refused(current: str) -> None:
    result = _bump(current)
    assert result.returncode != 0
    assert "refusing malformed version" in result.stdout
