import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

from codebase_rag import constants as cs

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
LOCK = ROOT / "uv.lock"
SETUP_UV = "astral-sh/setup-uv@"
KEY_STEP = "Key the uv cache on dependencies, not the project version"
KEY_FILE = "uv-deps.lock"
GLOB = "${{ runner.temp }}/" + KEY_FILE
ROOT_VERSION = re.compile(r'^(name = "code-graph-rag"\r?\nversion = ")[^"]+', re.M)
ROOT_VERSION_LINE = re.compile(r'^(name = "code-graph-rag"\n)version = "[^"]+"\n', re.M)
OTHER_VERSION = re.compile(
    r'^(name = "(?!code-graph-rag")[^"]+"\r?\nversion = ")[^"]+', re.M
)

SetupUvInputs = TypedDict(
    "SetupUvInputs",
    {"enable-cache": bool, "cache-dependency-glob": str},
    total=False,
)
Step = TypedDict(
    "Step",
    {
        "name": str,
        "if": str,
        "shell": str,
        "run": str,
        "uses": str,
        "with": SetupUvInputs,
    },
    total=False,
)


class Job(TypedDict, total=False):
    steps: list[Step]


class Workflow(TypedDict):
    jobs: dict[str, Job]


def _cached_setup_uv_sites() -> list[tuple[str, Step, Step]]:
    sites = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow: Workflow = yaml.safe_load(path.read_text(encoding=cs.ENCODING_UTF8))
        for job_name, job in workflow["jobs"].items():
            steps = job.get("steps", [])
            for index, step in enumerate(steps):
                if not step.get("uses", "").startswith(SETUP_UV):
                    continue
                if not step.get("with", {}).get("enable-cache"):
                    continue
                previous = steps[index - 1] if index else Step()
                sites.append((f"{path.name}:{job_name}", previous, step))
    return sites


def _key_lock(lock: str, tmp_path: Path) -> str:
    # Runs the workflow step itself, the way GitHub runs `shell: bash`, so the
    # assertions below hold for the awk program CI executes, not a copy of it.
    bash = shutil.which("bash")
    if bash is None or shutil.which("awk") is None:
        pytest.skip("the workflow step requires bash and awk")
    (key_step,) = {previous["run"] for _, previous, _ in _cached_setup_uv_sites()}
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "uv.lock").write_bytes(lock.encode(cs.ENCODING_UTF8))
    (work / "step.sh").write_text(key_step, encoding=cs.ENCODING_UTF8, newline="\n")
    subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "step.sh"],
        cwd=work,
        env={**os.environ, "RUNNER_TEMP": "."},
        check=True,
        timeout=30,
    )
    return (work / KEY_FILE).read_bytes().decode(cs.ENCODING_UTF8)


def _with_root_version(lock: str, version: str) -> str:
    keyed, count = ROOT_VERSION.subn(rf"\g<1>{version}", lock)
    assert count == 1
    return keyed


def test_every_cached_setup_uv_is_keyed_on_the_filtered_lock() -> None:
    sites = _cached_setup_uv_sites()
    assert sites
    for site, previous, step in sites:
        assert step["with"].get("cache-dependency-glob") == GLOB, site
        assert previous.get("name") == KEY_STEP, site
        assert previous.get("shell") == "bash", site
        # A step that skips its own setup-uv must skip the key step too, or
        # setup-uv would hash a file no earlier step wrote.
        assert previous.get("if") == step.get("if"), site
    assert len({previous["run"] for _, previous, _ in sites}) == 1


def test_a_version_bump_leaves_the_cache_key_unchanged(tmp_path: Path) -> None:
    lock = _with_root_version(LOCK.read_text(encoding=cs.ENCODING_UTF8), "0.2.1")
    before = _key_lock(lock, tmp_path)
    after = _key_lock(_with_root_version(lock, "0.2.2"), tmp_path)

    assert before == after
    assert before == ROOT_VERSION_LINE.sub(r"\g<1>", lock, count=1)


def test_a_dependency_change_still_changes_the_cache_key(tmp_path: Path) -> None:
    lock = LOCK.read_text(encoding=cs.ENCODING_UTF8)
    changed, count = OTHER_VERSION.subn(r"\g<1>0.0.0", lock, count=1)
    assert count == 1

    assert _key_lock(lock, tmp_path) != _key_lock(changed, tmp_path)


def test_a_crlf_checkout_drops_the_same_line(tmp_path: Path) -> None:
    lock = LOCK.read_text(encoding=cs.ENCODING_UTF8)
    keyed = _key_lock(lock, tmp_path)
    crlf = _key_lock(lock.replace("\n", "\r\n"), tmp_path)

    assert crlf.replace("\r\n", "\n") == keyed
