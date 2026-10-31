import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

from codebase_rag import constants as cs

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "version-bump.yml"
)


class Step(TypedDict):
    name: str
    run: str
    env: dict[str, str]


def _bump_step() -> Step:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["bump-version"]["steps"]
    return next(s for s in steps if s["name"] == "Bump version")


def _bump(current: str, bump_type: str = "patch") -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("the Ubuntu workflow requires bash")
    script = _bump_step()["run"]
    env = {
        **os.environ,
        "GITHUB_OUTPUT": os.devnull,
        "CURRENT": current,
        "BUMP_TYPE": bump_type,
    }
    return subprocess.run(
        [bash, "-e", "-c", script],
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        env=env,
        check=False,
    )


def _new_version(current: str, bump_type: str = "patch") -> str:
    result = _bump(current, bump_type)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout.split(" to ")[1].split(" ")[0]


def test_no_component_is_capped() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "VERSION_COMPONENT_CAP" not in workflow["env"]


# 0.0.1006 once rolled over into 0.1.0, announcing a breaking change that
# nothing made; past 999 a component now just keeps counting.
@pytest.mark.parametrize(
    ("current", "bump_type", "expected"),
    [
        ("0.0.5", "patch", "0.0.6"),
        ("0.0.999", "patch", "0.0.1000"),
        ("0.0.1006", "patch", "0.0.1007"),
        ("0.999.999", "patch", "0.999.1000"),
        ("0.999.7", "minor", "0.1000.0"),
        ("0.999.7", "major", "1.0.0"),
    ],
)
def test_components_never_roll_over(
    current: str, bump_type: str, expected: str
) -> None:
    assert _new_version(current, bump_type) == expected


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("0.1.89", "0.2.0"),
        ("0.0.1006", "0.1.0"),
        ("1.4.2", "2.0.0"),
        ("2.0.1500", "3.0.0"),
    ],
)
def test_a_breaking_bump_is_minor_below_one_and_major_after(
    current: str, expected: str
) -> None:
    assert _new_version(current, "breaking") == expected


def test_a_version_set_by_hand_is_kept_as_it_stands() -> None:
    assert _new_version("0.3.0", "none") == "0.3.0"


def test_a_malformed_version_set_by_hand_is_refused() -> None:
    result = _bump("0.3.0a", "none")
    assert result.returncode != 0
    assert "refusing malformed version" in result.stdout


def test_step_script_interpolates_no_expressions() -> None:
    step = _bump_step()
    assert "${{" not in step["run"]
    assert step["env"] == {
        "CURRENT": "${{ steps.get_version.outputs.current }}",
        "BUMP_TYPE": "${{ steps.bump_type.outputs.type }}",
    }


@pytest.mark.parametrize("current", ["0.0", "0.0.1a", "0.0.1\n0.0.2"])
def test_malformed_current_version_is_refused(current: str) -> None:
    result = _bump(current)
    assert result.returncode != 0
    assert "refusing malformed version" in result.stdout


@pytest.mark.parametrize(
    "template",
    [
        "0.0.$(touch {marker})",
        "0.0.`touch {marker}`",
        "a[$(touch {marker})].0.1",
        '0.0.1"; touch {marker}; "',
    ],
)
def test_injected_version_runs_no_command(tmp_path: Path, template: str) -> None:
    marker = tmp_path / "pwned"
    result = _bump(template.format(marker=marker))
    assert result.returncode != 0
    assert "refusing malformed version" in result.stdout
    assert not marker.exists()


def test_unknown_bump_type_is_refused() -> None:
    result = _bump("0.0.1", "patch; touch /tmp/x")
    assert result.returncode != 0
    assert "unknown bump type" in result.stdout


def test_no_step_script_interpolates_expressions() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    offenders = [
        step["name"]
        for step in workflow["jobs"]["bump-version"]["steps"]
        if "${{" in step.get("run", "")
    ]
    assert offenders == []
