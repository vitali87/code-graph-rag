"""cgr-graph-rs ships as wheels that code-graph-rag requires (issue #386).

The Rust half of the embedded graph is published to PyPI and installed with
code-graph-rag on the platforms it has wheels for. These pin the pieces that
must agree for that to hold: the requirement and the crate's version, the
platforms the requirement names and the wheels the workflow builds, and the
licences and bundling that go with a compiled dependency.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.markers import default_environment
from packaging.requirements import Requirement

from codebase_rag.constants.build import PYINSTALLER_PACKAGES
from scripts import check_cgr_graph_rs_on_pypi as release_check

REPO_ROOT = Path(__file__).resolve().parents[2]
CRATE_DIR = REPO_ROOT / "rust" / "cgr_graph_rs"
WHEELS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "rust-wheels.yml"
DIST_NAME = "cgr-graph-rs"
MODULE_NAME = "cgr_graph_rs"
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release.sh"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish.yml"
# What maturin names each wheel rust-wheels.yml builds, plus the sdist a
# release could also carry.
PUBLISHED_FILES = [
    "cgr_graph_rs-0.1.4-cp312-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
    "cgr_graph_rs-0.1.4-cp312-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
    "cgr_graph_rs-0.1.4-cp312-abi3-musllinux_1_2_x86_64.whl",
    "cgr_graph_rs-0.1.4-cp312-abi3-musllinux_1_2_aarch64.whl",
    "cgr_graph_rs-0.1.4-cp312-abi3-macosx_10_13_x86_64.macosx_11_0_arm64.macosx_10_13_universal2.whl",
    "cgr_graph_rs-0.1.4-cp312-abi3-win_amd64.whl",
    "cgr_graph_rs-0.1.4.tar.gz",
]

# What a Python on each wheel's platform reports to environment markers.
TARGET_ENVIRONMENTS = {
    "x86_64-unknown-linux-gnu": [("linux", "x86_64")],
    "aarch64-unknown-linux-gnu": [("linux", "aarch64")],
    "x86_64-unknown-linux-musl": [("linux", "x86_64")],
    "aarch64-unknown-linux-musl": [("linux", "aarch64")],
    "universal2-apple-darwin": [("darwin", "arm64"), ("darwin", "x86_64")],
    "x86_64-pc-windows-msvc": [("win32", "AMD64")],
}
# Platforms with no wheel, where installing must skip the extension rather
# than try to compile it.
WITHOUT_WHEELS = [
    ("linux", "armv7l", "CPython"),
    ("linux", "s390x", "CPython"),
    ("linux", "ppc64le", "CPython"),
    ("win32", "ARM64", "CPython"),
    ("win32", "x86", "CPython"),
    ("linux", "x86_64", "PyPy"),
    ("darwin", "arm64", "PyPy"),
]


def _toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _requirement() -> Requirement:
    deps = _toml(REPO_ROOT / "pyproject.toml")["project"]["dependencies"]
    (spec,) = [d for d in deps if Requirement(d).name == DIST_NAME]
    return Requirement(spec)


def _crate_version() -> str:
    return _toml(CRATE_DIR / "pyproject.toml")["project"]["version"]


def _environment(platform: str, machine: str, implementation: str) -> dict[str, str]:
    env = dict(default_environment())
    env.update(
        sys_platform=platform,
        platform_machine=machine,
        platform_python_implementation=implementation,
    )
    return env


def _workflow_targets() -> set[str]:
    jobs = yaml.safe_load(WHEELS_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    linux = {
        entry["target"] for entry in jobs["linux"]["strategy"]["matrix"]["include"]
    }
    names = {jobs[job]["name"].removeprefix("Wheel ") for job in ("macos", "windows")}
    return linux | names


def test_the_requirement_floor_is_the_crate_version() -> None:
    """Python code written against a crate release requires at least it, so
    an install never pairs it with an older library from PyPI."""
    requirement = _requirement()
    version = _crate_version()
    assert f">={version}" in {str(s) for s in requirement.specifier}
    assert requirement.specifier.contains(version)


def test_the_crate_has_one_version() -> None:
    cargo = _toml(CRATE_DIR / "Cargo.toml")["package"]["version"]
    assert cargo == _crate_version()


def test_the_workflow_builds_the_wheels_this_test_knows() -> None:
    """A wheel target added or dropped must be reflected in the requirement's
    platforms, which the tests below check against this map."""
    assert _workflow_targets() == set(TARGET_ENVIRONMENTS)


@pytest.mark.parametrize(
    ("target", "platform", "machine"),
    [
        (target, platform, machine)
        for target, envs in TARGET_ENVIRONMENTS.items()
        for platform, machine in envs
    ],
)
def test_every_platform_with_a_wheel_installs_it(
    target: str, platform: str, machine: str
) -> None:
    marker = _requirement().marker
    assert marker is not None
    assert marker.evaluate(_environment(platform, machine, "CPython")), target


@pytest.mark.parametrize(("platform", "machine", "implementation"), WITHOUT_WHEELS)
def test_a_platform_without_a_wheel_skips_it(
    platform: str, machine: str, implementation: str
) -> None:
    """No sdist is published, so requiring it where no wheel exists would make
    code-graph-rag uninstallable there instead of running the Python twin."""
    marker = _requirement().marker
    assert marker is not None
    assert not marker.evaluate(_environment(platform, machine, implementation))


def test_a_checkout_builds_the_crate_it_carries() -> None:
    sources = _toml(REPO_ROOT / "pyproject.toml")["tool"]["uv"]["sources"]
    source = REPO_ROOT / sources[DIST_NAME]["path"]
    assert source.resolve() == CRATE_DIR.resolve()
    assert _toml(source / "pyproject.toml")["project"]["name"] == DIST_NAME


def test_the_wheel_carries_the_crates_licences() -> None:
    """The library has every crate it uses compiled in, so the wheel owes
    their notices; the binaries' notice generator reads them from it."""
    project = _toml(CRATE_DIR / "pyproject.toml")["project"]
    assert project["license-files"] == ["LICENSE", "THIRD_PARTY_LICENSES.md"]
    for name in project["license-files"]:
        assert (CRATE_DIR / name).is_file(), name


def test_the_binaries_bundle_the_extension() -> None:
    """It is imported by name at run time, which PyInstaller cannot see."""
    (package,) = [p for p in PYINSTALLER_PACKAGES if p.name == MODULE_NAME]
    assert package.collect_all


def test_the_release_check_knows_every_wheel() -> None:
    assert set(release_check.WHEEL_TAGS) == _workflow_targets()


def test_the_release_check_reads_the_crate_version() -> None:
    assert release_check.crate_version() == _crate_version()


def test_a_release_with_every_wheel_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_check, "released_filenames", lambda _: PUBLISHED_FILES)
    assert release_check.main() == 0


def test_a_missing_crate_release_stops_a_local_release(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Uploading code-graph-rag before its crate would publish a version no
    supported platform can install."""
    monkeypatch.setattr(release_check, "released_filenames", lambda _: None)
    assert release_check.main() == release_check.INCOMPLETE
    assert DIST_NAME in capsys.readouterr().err


@pytest.mark.parametrize("target", sorted(TARGET_ENVIRONMENTS))
def test_a_missing_wheel_stops_a_local_release(
    target: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tag = release_check.WHEEL_TAGS[target]
    files = [name for name in PUBLISHED_FILES if tag not in name]
    monkeypatch.setattr(release_check, "released_filenames", lambda _: files)
    assert release_check.main() == release_check.INCOMPLETE
    assert target in capsys.readouterr().err


def test_an_sdist_is_not_a_wheel() -> None:
    sdist_only = [name for name in PUBLISHED_FILES if name.endswith(".tar.gz")]
    assert release_check.missing_targets(sdist_only) == list(release_check.WHEEL_TAGS)


def test_an_unreachable_pypi_stops_a_local_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(_: str) -> list[str]:
        raise release_check.urllib.error.URLError("offline")

    monkeypatch.setattr(release_check, "released_filenames", unreachable)
    assert release_check.main() == 1


def test_a_malformed_crate_version_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The version goes into PyPI's URL."""
    monkeypatch.setattr(release_check, "crate_version", lambda: "1.0/../x")
    assert release_check.main() == 1


def _publish_steps(job: str) -> list[dict[str, str]]:
    jobs = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    return jobs[job]["steps"]


def test_a_partial_crate_release_is_built_and_uploaded_again() -> None:
    """A run that stopped after uploading some wheels must not leave the
    rest missing: only a complete release skips the crate's jobs."""
    (check,) = [
        step for step in _publish_steps("rust-version") if step.get("id") == "check"
    ]
    assert "python3 scripts/check_cgr_graph_rs_on_pypi.py" in check["run"]
    assert '0) echo "publish=false"' in check["run"]
    assert f'{release_check.INCOMPLETE}) echo "publish=true"' in check["run"]


def test_code_graph_rag_waits_for_every_crate_wheel() -> None:
    """The crate has no sdist: a platform without its wheel cannot install
    the code-graph-rag that requires it."""
    steps = _publish_steps("publish")
    runs = [step.get("run", "") for step in steps]
    uses = [step.get("uses", "") for step in steps]
    check = next(
        i
        for i, run in enumerate(runs)
        if "scripts/check_cgr_graph_rs_on_pypi.py" in run
    )
    upload = next(i for i, use in enumerate(uses) if "gh-action-pypi-publish" in use)
    assert check < upload


def test_the_local_release_checks_before_it_commits_or_uploads() -> None:
    script = RELEASE_SCRIPT.read_text(encoding="utf-8")
    check = script.index("scripts/check_cgr_graph_rs_on_pypi.py")
    assert check < script.index("git commit")
    assert check < script.index("twine upload")


def test_the_local_release_runs_the_check_on_a_supported_python() -> None:
    """A workstation's own python3 may predate tomllib (3.11); uv picks an
    interpreter code-graph-rag supports, as the build and upload steps do,
    without syncing the project, which would compile the crate."""
    script = RELEASE_SCRIPT.read_text(encoding="utf-8")
    (line,) = [
        line
        for line in script.splitlines()
        if "scripts/check_cgr_graph_rs_on_pypi.py" in line
    ]
    requires = _toml(REPO_ROOT / "pyproject.toml")["project"]["requires-python"]
    assert line.startswith("uv run --no-project ")
    assert f'--python "{requires}"' in line


def test_the_fuzz_build_installs_the_crate_it_carries() -> None:
    """ClusterFuzzLite pip-installs the project, so without the crate in the
    same call pip looks for cgr-graph-rs on PyPI and the build fails before a
    target is compiled."""
    build = (REPO_ROOT / ".clusterfuzzlite" / "build.sh").read_text(encoding="utf-8")
    (install,) = [
        line
        for line in build.splitlines()
        if "pip install" in line and "[treesitter-full]" in line
    ]
    assert "./rust/cgr_graph_rs" in install
    assert install.index("./rust/cgr_graph_rs") < install.index("[treesitter-full]")
    # OSS-Fuzz's sanitizer flags are for fuzz targets, not a dependency.
    assert "-u RUSTFLAGS" in build


def test_the_fuzz_image_has_a_checked_rust_toolchain() -> None:
    """base-builder-python's cargo is a wrapper around /rust/bin/cargo, which
    that image does not have."""
    image = (REPO_ROOT / ".clusterfuzzlite" / "Dockerfile").read_text(encoding="utf-8")
    assert "CARGO_HOME=/rust" in image
    assert "RUSTUP_SHA256=" in image
    assert "sha256sum -c -" in image.split("rustup-init", 1)[1]
