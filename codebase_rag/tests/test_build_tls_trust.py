"""The build and release tooling must not trust the distrusted eMudhra roots.

The private keys of eight eMudhra intermediates were compromised. They chain
to emSign ECC Root CA - G3 and, through Sectigo cross-signs, to AAA
Certificate Services, so a client that still trusts either can be handed a
forged certificate for PyPI, GitHub or anything it fetches. Mozilla has since
dropped emSign ECC Root CA - C3 and emSign Root CA - C1 as well. The first
fixed releases are uv 0.13.4, pip 26.2.2 and pypa/gh-action-pypi-publish
v1.14.3 (certifi 2026.10.19, sigstore 4.5.1). A pinned base image is a snapshot of its ca-certificates package, and the LLM
clients reach the system store through httpx2's truststore context, so every
stage that installs packages upgrades ca-certificates and then deselects the
distrusted roots with scripts/distrust_ca_roots.sh: Debian and Ubuntu still
ship some of them, and Ubuntu 20.04 ships all four. pip trusts the system
store on top of its own certifi, so wherever pip fetches, the script runs
first.

Each check is a function run against the repository and against text it must
reject, so a pattern that matches nothing cannot pass for compliance.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_IMAGE = REPO_ROOT / "Dockerfile"
FUZZ_IMAGE = REPO_ROOT / ".clusterfuzzlite" / "Dockerfile"
SCANNER_IMAGE = REPO_ROOT / ".oss-scanner" / "Dockerfile"
DOCKERFILES = [PRODUCTION_IMAGE, FUZZ_IMAGE, SCANNER_IMAGE]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS_DIR / "ci.yml"
UNLOCKED_JOB = "wheel-fresh-resolution"
UNLOCKED_STEP = "Install the wheel with unlocked resolution"

DISTRUST_SCRIPT = REPO_ROOT / "scripts" / "distrust_ca_roots.sh"
PIP_REQUIREMENTS = REPO_ROOT / "scripts" / "pip-requirements.txt"

UV_FLOOR = Version("0.13.4")
PIP_FLOOR = Version("26.2.2")
PYPI_PUBLISH_FLOOR = Version("1.14.3")
# By the names Debian and Ubuntu file them under in /etc/ca-certificates.conf.
DISTRUSTED_ROOTS = frozenset(
    {
        "emSign_ECC_Root_CA_-_G3",
        "emSign_ECC_Root_CA_-_C3",
        "emSign_Root_CA_-_C1",
        "Comodo_AAA_Services_root",
    }
)

DIGEST = "sha256:" + "a" * 64
GOOD_PIP_BOOTSTRAP = (
    "python -m pip install --require-hashes -r scripts/pip-requirements.txt"
)
GOOD_PIP_REQUIREMENTS = (
    "# pip alone, checked by hash.\n"
    f"pip==26.2.2 \\\n    --hash=sha256:{'b' * 64} \\\n    --hash=sha256:{'d' * 64}\n"
)
SHA = "c" * 40
COPY_SCRIPT = "COPY scripts/distrust_ca_roots.sh /usr/local/sbin/"
RUN_SCRIPT = "sudo bash scripts/distrust_ca_roots.sh"
GOOD_SCRIPT = (
    f'ROOTS="{" ".join(sorted(DISTRUSTED_ROOTS))}"\n'
    "for root in $ROOTS; do\n"
    '    sed -i "s|^mozilla/$root\\.crt$|!&|" /etc/ca-certificates.conf\n'
    "done\n"
    "update-ca-certificates --fresh > /dev/null\n"
)


def logical_lines(text: str) -> list[str]:
    """Dockerfile lines with backslash continuations joined."""
    return re.sub(r"\\\n", " ", text).splitlines()


_UV_PIN = re.compile(
    r"ghcr\.io/astral-sh/uv:(?P<image>[^@\s]+)@sha256:"
    r"|^ARG UV_VERSION=(?P<arg>\S+)"
    r"|\buv==(?P<pip>[^\s\"']+)",
    re.MULTILINE,
)
_PIP_INSTALL = re.compile(r"\bpip install\b")
_REQUIREMENTS_FILE = re.compile(r"(?<!\S)(?:-r|--requirement)[\s=](?P<path>\S+)")
_HASHED_PIP = re.compile(r"pip==(?P<version>\S+)(?:\s+--hash=sha256:[0-9a-f]{64})+")
_CA_CERTIFICATES = re.compile(r"(?<![\w-])ca-certificates(?![\w-])")
# An `apt-get install`'s arguments up to the next shell operator, so a path
# such as /etc/ca-certificates.conf later in the same RUN is not an install.
_APT_INSTALL_ARGS = re.compile(r"apt-get install\b([^&;|\n]*)")
_PYPI_PUBLISH = re.compile(
    r"uses:\s*pypa/gh-action-pypi-publish@(?P<ref>\S+)(?:\s+#\s*v(?P<version>\S+))?"
)


def uv_pins(text: str) -> list[str]:
    return [next(v for v in m.groups() if v) for m in _UV_PIN.finditer(text)]


def fails_floor(pinned: str, floor: Version) -> bool:
    """True below the floor, and for anything but an exact X.Y.Z release,
    whose patch level (and so its trust store) cannot be read from the pin."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", pinned):
        return True
    return Version(pinned) < floor


def pip_pin_problems(requirements: str) -> list[str]:
    """The bootstrap's requirements file pins pip and nothing else, to an
    exact release at the floor, with the SHA-256s --require-hashes checks."""
    entries = [
        entry
        for line in logical_lines(requirements)
        if (entry := re.sub(r"(?:^|\s)#.*", "", line).strip())
    ]
    if len(entries) != 1:
        return [f"expected pip alone, found {len(entries)} requirements"]
    pin = _HASHED_PIP.fullmatch(entries[0])
    if pin is None:
        return [f"pip is not pinned by hash: {entries[0]}"]
    if fails_floor(pin["version"], PIP_FLOOR):
        return [f"bootstrap pip {pin['version']} is below {PIP_FLOOR}"]
    return []


def pip_bootstrap_problems(commands: list[str], requirements: str) -> list[str]:
    """Until pip has replaced itself, the interpreter's bundled pip, which
    still trusts the distrusted root, is what talks to the index, so its
    first install must be pip alone, from scripts/pip-requirements.txt read
    with --require-hashes, so that pip checks the SHA-256s pinned there.
    `requirements` is that file's text."""
    installs = [c for c in commands if _PIP_INSTALL.search(c)]
    if not installs:
        return ["no pip install found"]
    requirements_file = _REQUIREMENTS_FILE.search(installs[0])
    if (
        "--require-hashes" not in installs[0].split()
        or requirements_file is None
        or Path(requirements_file["path"]).name != PIP_REQUIREMENTS.name
    ):
        return [f"first pip install is not the hash-pinned pip: {installs[0]}"]
    return pip_pin_problems(requirements)


def ca_store_problems(text: str) -> list[str]:
    stages = re.split(r"(?m)^FROM ", "\n".join(logical_lines(text)))[1:]
    return [
        f"stage {index} (FROM {stage.splitlines()[0]}) installs packages "
        "without upgrading ca-certificates"
        for index, stage in enumerate(stages)
        if (installs := _APT_INSTALL_ARGS.findall(stage))
        and not any(_CA_CERTIFICATES.search(args) for args in installs)
    ]


def pypi_publish_problems(text: str) -> list[str]:
    problems = []
    for use in _PYPI_PUBLISH.finditer(text):
        if not re.fullmatch(r"[0-9a-f]{40}", use["ref"]):
            problems.append(f"not pinned to a commit SHA: {use['ref']}")
        elif use["version"] is None or fails_floor(use["version"], PYPI_PUBLISH_FLOOR):
            problems.append(f"below {PYPI_PUBLISH_FLOOR} or unversioned: {use[0]}")
    return problems


_SCRIPT_ROOTS = re.compile(r'^ROOTS="(?P<roots>[^"]*)"', re.MULTILINE)
_SCRIPT_CALL = re.compile(r"(?:^|[\s/])distrust_ca_roots\.sh(?=$|[\s;&|])")


def script_problems(text: str) -> list[str]:
    """A `!` before a root in /etc/ca-certificates.conf deselects it, and
    survives a later ca-certificates upgrade; the store only drops it once
    update-ca-certificates rebuilds it."""
    listed = _SCRIPT_ROOTS.search(text)
    roots = set(listed["roots"].split()) if listed else set()
    problems = [
        f"does not deselect {root}" for root in sorted(DISTRUSTED_ROOTS - roots)
    ]
    deselect = text.find("|!&|")
    if deselect < 0:
        problems.append(
            "does not mark the roots deselected in /etc/ca-certificates.conf"
        )
    if text.find("update-ca-certificates --fresh", max(deselect, 0)) < 0:
        problems.append("does not rebuild the store after deselecting the roots")
    return problems


def stage_distrust_problems(text: str) -> list[str]:
    """Each stage that upgrades ca-certificates runs the script after that
    upgrade, in the same RUN, from a copy the stage brought in first."""
    problems = []
    stages = re.split(r"(?m)^FROM ", "\n".join(logical_lines(text)))[1:]
    for index, stage in enumerate(stages):
        lines = stage.splitlines()
        for n, line in enumerate(lines):
            upgrades = [
                m
                for m in _APT_INSTALL_ARGS.finditer(line)
                if _CA_CERTIFICATES.search(m[1])
            ]
            if not upgrades:
                continue
            where = f"stage {index} (FROM {lines[0]})"
            if not _SCRIPT_CALL.search(line, upgrades[-1].end()):
                problems.append(f"{where} upgrades ca-certificates and keeps the roots")
            elif not any(
                c.startswith("COPY") and "scripts/distrust_ca_roots.sh" in c
                for c in lines[:n]
            ):
                problems.append(
                    f"{where} runs distrust_ca_roots.sh without copying it in"
                )
    return problems


# What reaches the network through the system store: pip under any of its
# names, curl, cargo, go fetching modules, and uv building or syncing, which
# runs cargo for the crate.
_FETCH = re.compile(
    r"\bpip(?:3(?:\.\d+)?)? install\b|\bcurl\b|\bcargo (?:build|fetch|install)\b"
    r"|\bgo (?:build|get|install|mod download)\b|\buv (?:build|sync)\b"
)


def distrust_before_fetch_problems(commands: list[str]) -> list[str]:
    commands = [c for c in commands if not c.lstrip().startswith("#")]
    fetches = [i for i, c in enumerate(commands) if _FETCH.search(c)]
    if not fetches:
        return ["nothing fetches"]
    runs = [c for c in commands[: fetches[0]] if not re.match(r"(?:COPY|ADD)\s", c)]
    if not any(_SCRIPT_CALL.search(c) for c in runs):
        return ["distrust_ca_roots.sh does not run before the first fetch"]
    return []


@pytest.mark.parametrize(
    "text",
    [
        f"FROM ghcr.io/astral-sh/uv:0.13.1@{DIGEST} AS uv",
        f"COPY --from=ghcr.io/astral-sh/uv:0.13.3@{DIGEST} /uv /uvx /bin/",
        # A minor tag hides which patch release the digest is.
        f"FROM ghcr.io/astral-sh/uv:0.13@{DIGEST} AS uv",
        "ARG UV_VERSION=0.12.10",
        'RUN pip install --no-cache-dir "uv==0.13.3"',
    ],
)
def test_a_uv_pin_below_the_floor_is_rejected(text: str) -> None:
    assert [fails_floor(p, UV_FLOOR) for p in uv_pins(text)] == [True]


def test_the_first_fixed_uv_passes_the_floor() -> None:
    pins = uv_pins("ARG UV_VERSION=0.13.4")
    assert [fails_floor(p, UV_FLOOR) for p in pins] == [False]


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: str(p.parent.name))
def test_every_build_image_pins_a_fixed_uv(dockerfile: Path) -> None:
    pins = uv_pins(dockerfile.read_text(encoding="utf-8"))
    assert pins, f"no uv pin found in {dockerfile}"
    assert [p for p in pins if fails_floor(p, UV_FLOOR)] == []


@pytest.mark.parametrize(
    ("commands", "requirements"),
    [
        (
            ["python -m pip install --upgrade pip", "python -m pip install pkg"],
            GOOD_PIP_REQUIREMENTS,
        ),
        # pip checks a file's hashes unasked once a line carries one, but only
        # --require-hashes refuses the file once they are gone.
        ([GOOD_PIP_BOOTSTRAP.replace(" --require-hashes", "")], GOOD_PIP_REQUIREMENTS),
        (
            [GOOD_PIP_BOOTSTRAP.replace("pip-requirements", "requirements")],
            GOOD_PIP_REQUIREMENTS,
        ),
        ([GOOD_PIP_BOOTSTRAP], "pip==26.2.2\n"),
        ([GOOD_PIP_BOOTSTRAP], GOOD_PIP_REQUIREMENTS.replace("26.2.2", "26.2.1")),
        ([GOOD_PIP_BOOTSTRAP], GOOD_PIP_REQUIREMENTS.replace("pip==", "pip>=")),
        (
            [GOOD_PIP_BOOTSTRAP],
            GOOD_PIP_REQUIREMENTS + f"pkg==1.0 --hash=sha256:{'e' * 64}\n",
        ),
        (["python -m pip install pkg", GOOD_PIP_BOOTSTRAP], GOOD_PIP_REQUIREMENTS),
    ],
    ids=[
        "unhashed-upgrade",
        "hashes-not-required",
        "other-file",
        "no-hash",
        "below-floor",
        "not-exact",
        "not-pip-alone",
        "not-first",
    ],
)
def test_a_pip_that_is_not_bootstrapped_first_is_rejected(
    commands: list[str], requirements: str
) -> None:
    assert pip_bootstrap_problems(commands, requirements)


def test_a_hashed_pip_bootstrap_at_the_floor_is_accepted() -> None:
    commands = [
        GOOD_PIP_BOOTSTRAP,
        "python -m pip install --upgrade pip",
        "python -m pip install pkg",
    ]
    assert pip_bootstrap_problems(commands, GOOD_PIP_REQUIREMENTS) == []


def test_every_build_image_bootstraps_pip_before_using_it() -> None:
    """The fuzz image's build.sh pip-installs the project after the image is
    built, with whichever pip the Dockerfile left behind."""
    checked = {}
    for dockerfile in DOCKERFILES:
        commands = [
            line
            for line in logical_lines(dockerfile.read_text(encoding="utf-8"))
            if re.search(r"\bpip install\b", line)
        ]
        if commands:
            checked[dockerfile] = pip_bootstrap_problems(
                commands, PIP_REQUIREMENTS.read_text(encoding="utf-8")
            )
    assert FUZZ_IMAGE in checked
    assert {path: problems for path, problems in checked.items() if problems} == {}


def test_the_unlocked_wheel_job_bootstraps_pip() -> None:
    """`python -m venv` seeds the interpreter's bundled pip, which then
    upgrades itself from PyPI unless the first install is hash-pinned."""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    (step,) = [
        s
        for s in workflow["jobs"][UNLOCKED_JOB]["steps"]
        if s.get("name") == UNLOCKED_STEP
    ]
    assert (
        pip_bootstrap_problems(
            step["run"].splitlines(), PIP_REQUIREMENTS.read_text(encoding="utf-8")
        )
        == []
    )


@pytest.mark.parametrize(
    "text",
    [
        "FROM base AS builder\nRUN apt-get install -y ca-certificates git\n"
        "FROM base\nRUN apt-get install -y ripgrep\n",
        "FROM base\nRUN apt-get install -y ca-certificates-java ripgrep\n",
        "FROM base\nRUN apt-get install -y \\\n    ripgrep\n",
        # The package name inside a path in the same RUN is not an install.
        "FROM base\nRUN apt-get install -y git && \\\n"
        "    sed -i 's|^x$|!&|' /etc/ca-certificates.conf && update-ca-certificates\n",
    ],
    ids=["runtime-skips-it", "near-miss-name", "continued-line", "path-only"],
)
def test_a_stage_that_skips_ca_certificates_is_rejected(text: str) -> None:
    assert ca_store_problems(text)


def test_stages_that_all_install_ca_certificates_are_accepted() -> None:
    text = (
        "FROM base AS builder\nRUN apt-get install -y \\\n    ca-certificates git\n"
        "FROM base\nRUN apt-get install -y ripgrep ca-certificates\n"
        "FROM other AS copied\n"
    )
    assert ca_store_problems(text) == []


def test_every_stage_that_installs_packages_upgrades_ca_certificates() -> None:
    texts = [p.read_text(encoding="utf-8") for p in DOCKERFILES]
    assert any("apt-get install" in t for t in texts)
    assert {
        str(p): ca_store_problems(t)
        for p, t in zip(DOCKERFILES, texts)
        if ca_store_problems(t)
    } == {}


# certifi's bundle and grpcio's roots.pem are their own copies of the roots:
# certifi 2026.10.23 also drops emSign ECC Root CA - C3 and emSign Root CA -
# C1, and grpcio 1.84.1 is the first whose roots.pem drops G3.
LOCKFILE = REPO_ROOT / "uv.lock"
LOCK_FLOORS = {"certifi": Version("2026.10.23"), "grpcio": Version("1.84.1")}
GOOD_LOCK = (
    '[[package]]\nname = "certifi"\nversion = "2026.10.23"\n\n'
    '[[package]]\nname = "grpcio"\nversion = "1.84.1"\n'
)
_LOCKED = re.compile(
    r'^\[\[package\]\]\nname = "(?P<name>[^"]+)"\nversion = "(?P<version>[^"]+)"',
    re.MULTILINE,
)


def lock_problems(text: str) -> list[str]:
    locked: dict[str, list[str]] = {}
    for package in _LOCKED.finditer(text):
        locked.setdefault(package["name"], []).append(package["version"])
    return [
        f"{name} {versions or 'unlocked'} below {floor}"
        for name, floor in LOCK_FLOORS.items()
        if not (versions := locked.get(name))
        or any(fails_floor(v, floor) for v in versions)
    ]


@pytest.mark.parametrize(
    "text",
    [
        GOOD_LOCK.replace("2026.10.23", "2026.10.19"),
        GOOD_LOCK.replace("1.84.1", "1.84.0"),
        GOOD_LOCK.split("\n\n")[1],
    ],
    ids=["old-certifi", "old-grpcio", "certifi-unlocked"],
)
def test_a_lock_with_a_stale_root_bundle_is_rejected(text: str) -> None:
    assert text != GOOD_LOCK
    assert lock_problems(text)


def test_a_lock_at_the_floors_is_accepted() -> None:
    assert lock_problems(GOOD_LOCK) == []


def test_the_lock_is_past_every_bundled_root_fix() -> None:
    assert lock_problems(LOCKFILE.read_text(encoding="utf-8")) == []


# A published wheel carries pyproject's ranges, not uv.lock, and pip upgrades
# a dependency only when the installed one no longer satisfies them. So the
# floors are declared too: certifi in the core dependencies, grpcio in the
# extras whose clients pull it in.
PYPROJECT = REPO_ROOT / "pyproject.toml"
LAST_STALE = {"certifi": "2026.10.19", "grpcio": "1.84.0"}
GRPC_EXTRAS = ("semantic", "milvus")
GOOD_PYPROJECT = (
    '[project]\ndependencies = ["loguru>=0.7.3", "certifi>=2026.10.23"]\n'
    "[project.optional-dependencies]\n"
    'semantic = ["qdrant-client>=1.9.0", "grpcio>=1.84.1"]\n'
    'milvus = ["pymilvus[milvus-lite]>=3.0.0", "grpcio>=1.84.1"]\n'
)


def declared_floor_problems(text: str) -> list[str]:
    """Each place must require the package with a range that admits the
    fixed release and refuses the last stale one."""
    project = tomllib.loads(text)["project"]
    extras = project.get("optional-dependencies", {})
    places = [("certifi", "dependencies", project.get("dependencies", []))]
    places += [("grpcio", f"extra {e}", extras.get(e, [])) for e in GRPC_EXTRAS]
    problems = []
    for name, where, requirements in places:
        ranges = [r.specifier for r in map(Requirement, requirements) if r.name == name]
        if not any(
            s.contains(str(LOCK_FLOORS[name])) and not s.contains(LAST_STALE[name])
            for s in ranges
        ):
            problems.append(f"{where} does not require {name}>={LOCK_FLOORS[name]}")
    return problems


@pytest.mark.parametrize(
    "text",
    [
        GOOD_PYPROJECT.replace(', "certifi>=2026.10.23"', ""),
        GOOD_PYPROJECT.replace("certifi>=2026.10.23", "certifi>=2026.10.19"),
        GOOD_PYPROJECT.replace(', "grpcio>=1.84.1"]\n', "]\n", 1),
        GOOD_PYPROJECT.replace("grpcio>=1.84.1", "grpcio>=1.84.0"),
        GOOD_PYPROJECT.replace("certifi>=2026.10.23", "certifi"),
    ],
    ids=["no-certifi", "stale-certifi", "extra-without-grpcio", "stale-grpcio", "bare"],
)
def test_a_declared_range_that_admits_a_stale_bundle_is_rejected(text: str) -> None:
    assert text != GOOD_PYPROJECT
    assert declared_floor_problems(text)


def test_declared_floors_are_accepted() -> None:
    assert declared_floor_problems(GOOD_PYPROJECT) == []


def test_pyproject_declares_every_bundled_root_floor() -> None:
    assert declared_floor_problems(PYPROJECT.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "text",
    [
        f"      uses: pypa/gh-action-pypi-publish@{SHA} # v1.14.2",
        f"      uses: pypa/gh-action-pypi-publish@{SHA}",
        "      uses: pypa/gh-action-pypi-publish@release/v1 # v1.14.3",
    ],
    ids=["below-floor", "no-version-comment", "not-a-sha"],
)
def test_a_vulnerable_or_unverifiable_pypi_publish_is_rejected(text: str) -> None:
    assert pypi_publish_problems(text)


def test_the_fixed_pypi_publish_is_accepted() -> None:
    text = f"      uses: pypa/gh-action-pypi-publish@{SHA} # v1.14.3"
    assert pypi_publish_problems(text) == []


def test_every_pypi_upload_uses_a_fixed_action() -> None:
    """The version comment is what Dependabot rewrites alongside the SHA; a
    hand edit that moves one without the other is not caught here."""
    texts = {p.name: p.read_text(encoding="utf-8") for p in WORKFLOWS_DIR.glob("*.yml")}
    assert any("pypa/gh-action-pypi-publish@" in t for t in texts.values())
    assert {
        n: pypi_publish_problems(t)
        for n, t in texts.items()
        if pypi_publish_problems(t)
    } == {}


@pytest.mark.parametrize(
    "text",
    [
        GOOD_SCRIPT.replace("Comodo_AAA_Services_root", ""),
        GOOD_SCRIPT.replace("emSign_ECC_Root_CA_-_G3", "emSign_ECC_Root_CA_-_G2"),
        GOOD_SCRIPT.replace("update-ca-certificates --fresh > /dev/null\n", ""),
        "update-ca-certificates --fresh\n"
        + GOOD_SCRIPT.split("update-", maxsplit=1)[0],
        GOOD_SCRIPT.replace("|!&|", "|&|"),
    ],
    ids=["misses-aaa", "near-miss-root", "no-rebuild", "rebuild-first", "no-deselect"],
)
def test_a_script_that_leaves_a_root_trusted_is_rejected(text: str) -> None:
    assert text != GOOD_SCRIPT
    assert script_problems(text)


def test_a_script_that_deselects_every_root_is_accepted() -> None:
    assert script_problems(GOOD_SCRIPT) == []


def test_the_distrust_script_deselects_every_root() -> None:
    assert DISTRUST_SCRIPT.is_file()
    assert script_problems(DISTRUST_SCRIPT.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "text",
    [
        "FROM base\nRUN apt-get install -y ca-certificates\n",
        f"FROM base\n{COPY_SCRIPT}\n"
        "RUN distrust_ca_roots.sh && apt-get install -y ca-certificates\n",
        "FROM base\nRUN apt-get install -y ca-certificates && distrust_ca_roots.sh\n",
        f"FROM base\n{COPY_SCRIPT}\n"
        "RUN apt-get install -y ca-certificates && distrust_ca_roots.sh.orig\n",
        f"FROM base AS builder\n{COPY_SCRIPT}\n"
        "RUN apt-get install -y ca-certificates && distrust_ca_roots.sh\n"
        "FROM base\nRUN apt-get install -y ca-certificates ripgrep\n",
    ],
    ids=["upgrade-only", "before-upgrade", "not-copied", "near-miss", "second-stage"],
)
def test_a_stage_that_upgrades_without_distrusting_is_rejected(text: str) -> None:
    assert stage_distrust_problems(text)


def test_a_stage_that_distrusts_after_upgrading_is_accepted() -> None:
    text = (
        f"FROM base\n{COPY_SCRIPT}\n"
        "RUN apt-get install -y \\\n    ca-certificates git && \\\n"
        "    distrust_ca_roots.sh\nFROM other AS copied\n"
    )
    assert stage_distrust_problems(text) == []


def test_every_stage_that_upgrades_ca_certificates_distrusts_the_roots() -> None:
    texts = {p: p.read_text(encoding="utf-8") for p in DOCKERFILES}
    upgrading = [
        p
        for p, t in texts.items()
        if any(_CA_CERTIFICATES.search(a) for a in _APT_INSTALL_ARGS.findall(t))
    ]
    assert upgrading == DOCKERFILES
    assert {
        str(p): stage_distrust_problems(t)
        for p, t in texts.items()
        if stage_distrust_problems(t)
    } == {}


@pytest.mark.parametrize(
    "commands",
    [
        ["apt-get install -y ca-certificates", GOOD_PIP_BOOTSTRAP],
        [GOOD_PIP_BOOTSTRAP, RUN_SCRIPT],
        [RUN_SCRIPT.replace("roots.sh", "roots_v2.sh"), GOOD_PIP_BOOTSTRAP],
        # Copying the script into an image does not run it.
        [COPY_SCRIPT, GOOD_PIP_BOOTSTRAP],
        # Building the crate fetches through cargo before pip ever runs.
        ["uv build --wheel rust/cgr_graph_rs", RUN_SCRIPT, GOOD_PIP_BOOTSTRAP],
        # pip fetches under its versioned names too.
        ["pip3 install pkg", RUN_SCRIPT, GOOD_PIP_BOOTSTRAP],
        ["pip3.12 install pkg", RUN_SCRIPT, GOOD_PIP_BOOTSTRAP],
        ["go mod download", RUN_SCRIPT, GOOD_PIP_BOOTSTRAP],
    ],
    ids=[
        "upgrade-only",
        "after-pip",
        "near-miss",
        "copied-not-run",
        "build-first",
        "pip3-first",
        "pip3.12-first",
        "go-first",
    ],
)
def test_a_fetch_from_a_store_that_still_trusts_the_roots_is_rejected(
    commands: list[str],
) -> None:
    assert distrust_before_fetch_problems(commands)


def test_distrusting_before_the_first_fetch_is_accepted() -> None:
    # A comment that names a fetching tool fetches nothing.
    commands = ["# curl fetches through the store", RUN_SCRIPT, "uv build --wheel"]
    assert distrust_before_fetch_problems(commands) == []


def test_every_build_image_distrusts_the_roots_before_fetching() -> None:
    """Ubuntu 20.04, the fuzz image's base, still ships all four roots in its
    newest ca-certificates (20240203~20.04.1), so upgrading cannot drop them."""
    checked = {
        dockerfile: distrust_before_fetch_problems(
            logical_lines(dockerfile.read_text(encoding="utf-8"))
        )
        for dockerfile in DOCKERFILES
    }
    assert {path: problems for path, problems in checked.items() if problems} == {}


def test_the_unlocked_wheel_job_distrusts_the_roots_before_fetching() -> None:
    """The runner image's ca-certificates is whatever it was built with, and
    the crate's wheel build fetches through cargo before pip runs."""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    commands = [
        line
        for step in workflow["jobs"][UNLOCKED_JOB]["steps"]
        for line in step.get("run", "").splitlines()
    ]
    assert distrust_before_fetch_problems(commands) == []
