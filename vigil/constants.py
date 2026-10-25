from enum import StrEnum
from pathlib import Path
from typing import NamedTuple


class Verdict(StrEnum):
    # Report order: what changed your position first, what is ready to
    # merge next, then what was tried and dropped, then what is pending.
    ACT = "ACT"
    READY = "READY"
    DISCARDED = "DISCARDED"
    WATCHING = "WATCHING"


VERDICT_ORDER: tuple[Verdict, ...] = (
    Verdict.ACT,
    Verdict.READY,
    Verdict.DISCARDED,
    Verdict.WATCHING,
)


class Scope(StrEnum):
    RUNTIME = "runtime"
    EXTRA = "extra"
    GROUP = "group"


# A package declared in several places is tested once, under the scope that
# reaches the most installs: what every `pip install code-graph-rag` gets
# outranks an opt-in extra, which outranks a contributor-only group.
SCOPE_RANK: dict[Scope, int] = {Scope.RUNTIME: 0, Scope.EXTRA: 1, Scope.GROUP: 2}


class Speed(StrEnum):
    FASTER = "faster"
    SLOWER = "slower"
    WITHIN_NOISE = "within noise"


class Experiment(StrEnum):
    UPGRADE = "upgrade"
    ADOPT = "adopt"


class Corpus(NamedTuple):
    name: str
    url: str
    ref: str
    commit: str


# Pinned to exact commits so a digest is comparable across candidates. The
# set spans the grammars a dependency bump can change underneath cgr: the
# Python core, and JavaScript and Rust from the treesitter-full extra.
CORPORA: tuple[Corpus, ...] = (
    Corpus(
        "requests",
        "https://github.com/psf/requests.git",
        "v2.32.3",
        "0e322af87745eff34caffe4df68456ebc20d9068",
    ),
    Corpus(
        "express",
        "https://github.com/expressjs/express.git",
        "4.21.2",
        "1faf228935aa0a13111f92c28ee795be64ce3f0f",
    ),
    Corpus(
        "hexyl",
        "https://github.com/sharkdp/hexyl.git",
        "v0.16.0",
        "abc20a380c8c2d9d76c1976222725d3211cef809",
    ),
)
BENCH_CORPUS = "requests"

# Interleaved A/B pairs per timing comparison. With nine pairs an exact
# two-sided sign test can reach p < 0.05 (8 of 9 agreeing); seven cannot
# without a clean sweep, which makes one noisy pair veto a real change.
BENCH_PAIRS = 9
SIGN_TEST_ALPHA = 0.05
# A change counts only beyond both floors: the absolute one is the build's
# observed run-to-run jitter, the relative one keeps a large corpus from
# turning a few milliseconds of noise into a "significant" win.
NOISE_FLOOR_MS = 50.0
NOISE_FLOOR_FRACTION = 0.02
RSS_GUARD_FRACTION = 0.10

# Every candidate venv carries the grammars the corpora need and the test
# extra the suite imports; an item's own extra is synced on top.
VENV_EXTRAS: tuple[str, ...] = ("treesitter-full", "test")
# Extras that pull torch or a vector server: too heavy to install per
# candidate on a shared machine, so their packages are reported, not run.
UNTESTABLE_EXTRAS = frozenset({"semantic", "milvus"})

# Contributor tools with a gate of their own: the command each runs on the
# repo, compared between baseline and candidate by its diagnostic count.
TY_EXCLUDES: tuple[str, ...] = (
    "codebase_rag/tests/",
    "benchmarks/",
    "optimize/",
    "codec/",
    "grammars/",
    "query_modules/",
    "fuzz/",
)
TOOL_GATES: dict[str, tuple[tuple[str, ...], ...]] = {
    "ruff": (("ruff", "check", "."), ("ruff", "format", "--check", ".")),
    "ty": (("ty", "check", *(f"--exclude={p}" for p in TY_EXCLUDES)),),
}

# Paths a candidate may not change: the scorer, its state, the tests that
# grade it, and CI. An attempt that edits its own grader is discarded.
FROZEN_PREFIXES: tuple[str, ...] = (
    "vigil/",
    ".vigil/",
    "evals/",
    "benchmarks/",
    "codebase_rag/tests/",
    ".github/",
)
TESTS_PREFIX = "codebase_rag/tests/"
VIGIL_PREFIX = "vigil/"
# Candidates are cut from what ships, so a READY branch is one commit on
# main and never carries vigil's own history.
DEFAULT_BASE_REF = "origin/main"
PY_SUFFIX = ".py"
TEST_WORKERS = "4"
TEST_TIMEOUT_S = 3600
MEASURE_TIMEOUT_S = 900
SYNC_TIMEOUT_S = 1800
# Failing test ids kept in an outcome's evidence; the full list is in the
# night's log.
FAILURE_SAMPLE = 5
DELTA_EXAMPLES = 3

STATE_DIR = Path(".vigil")
SEEN_FILE = "seen.json"
RESULTS_FILE = "results.tsv"
JOURNAL_FILE = "journal.md"
NIGHTS_DIR = "nights"
# A night is named for the morning its report is read, in the owner's zone.
NIGHT_TZ = "Europe/London"
WORK_DIR_ENV = "VIGIL_WORK"
DEFAULT_WORK_DIR = Path.home() / ".cache" / "vigil"
BRANCH_PREFIX = "vigil/"
RESULTS_HEADER: tuple[str, ...] = (
    "night",
    "item",
    "experiment",
    "verdict",
    "headline",
    "branch",
    "minutes",
)

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
PYPI_PROJECT_URL = "https://pypi.org/project/{name}/{version}/"
HTTP_TIMEOUT_S = 30.0
ITEM_SOURCE_PYPI = "pypi"
ITEM_ID_SEPARATOR = ":"
PYPROJECT = "pyproject.toml"
UV_LOCK = "uv.lock"
ENCODING = "utf-8"
