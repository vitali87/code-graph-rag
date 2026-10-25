import fnmatch
from pathlib import Path

from defusedxml import ElementTree
from loguru import logger

from . import bench
from . import constants as vc
from . import logs as ls
from .types_defs import Cone, GraphDelta, SuiteRun
from .workspace import changed_paths, run, work_dir

_GRAMMAR_PREFIX = "tree-sitter-"
# Grammar package suffix -> the token cgr's per-language test files carry.
_GRAMMAR_TEST_TOKENS = {"c-sharp": "csharp", "markdown": "markdown", "cpp": "cpp"}


def grammar_language(package: str) -> str | None:
    if (
        not package.startswith(_GRAMMAR_PREFIX)
        or package == _GRAMMAR_PREFIX + "language-pack"
    ):
        return None
    return package.removeprefix(_GRAMMAR_PREFIX)


def select_tests(repo: Path, package: str, cone: Cone) -> list[str]:
    """Tests to run for a package bump.

    A grammar is loaded by name at runtime, so no import edge leads to it;
    its reach is the language's own test files instead, which cgr names
    after the language (test_scala_*.py, test_lua_l1_eval.py).
    """
    tests = set(cone.test_files)
    language = grammar_language(package)
    if language is not None:
        token = _GRAMMAR_TEST_TOKENS.get(language, language.replace("-", "_"))
        tests |= {
            str(p.relative_to(repo))
            for p in (repo / vc.TESTS_PREFIX).glob("test_*.py")
            if fnmatch.fnmatch(p.name, f"*{token}*")
        }
    return sorted(tests)


def frozen_touched(worktree: Path, base_ref: str) -> list[str]:
    return [
        p for p in changed_paths(worktree, base_ref) if p.startswith(vc.FROZEN_PREFIXES)
    ]


def _failed_ids(junit: Path) -> tuple[int, list[str]]:
    root = ElementTree.parse(junit).getroot()
    total = 0
    failed: list[str] = []
    skipped = 0
    for case in root.iter("testcase"):
        total += 1
        kinds = {child.tag for child in case}
        if kinds & {"failure", "error"}:
            failed.append(f"{case.get('classname', '')}::{case.get('name', '')}")
        elif "skipped" in kinds:
            skipped += 1
    return total - len(failed) - skipped, sorted(failed)


def run_tests(worktree: Path, tests: list[str], label: str) -> SuiteRun:
    """Run `tests` in `worktree`'s venv; a failed collection counts as a
    failure of every selected file, since none of them was exercised."""
    if not tests:
        return SuiteRun(selected=0, passed=0, failed=[])
    junit = work_dir() / "junit" / f"{label}.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    junit.unlink(missing_ok=True)
    cmd = [
        str(bench.venv_python(worktree)),
        "-m",
        "pytest",
        "-q",
        "-n",
        vc.TEST_WORKERS,
        "-p",
        "no:cacheprovider",
        "-m",
        "not integration",
        f"--junitxml={junit}",
        *tests,
    ]
    logger.info(ls.RUNNING_TESTS.format(count=len(tests), where=worktree.name))
    done = run(cmd, worktree, timeout=vc.TEST_TIMEOUT_S)
    if not junit.exists():
        return SuiteRun(
            selected=len(tests), passed=0, failed=[f"{t}::<collection>" for t in tests]
        )
    passed, failed = _failed_ids(junit)
    if done.returncode not in (0, 1) and not failed:
        failed = [f"<pytest exit {done.returncode}>"]
    return SuiteRun(selected=len(tests), passed=passed, failed=failed)


def new_failures(baseline: SuiteRun, candidate: SuiteRun) -> list[str]:
    return sorted(set(candidate.failed) - set(baseline.failed))


def tool_diagnostics(worktree: Path, tool: str) -> tuple[int, int]:
    """(failing commands, output lines) for a contributor tool's gate."""
    bin_dir = bench.venv_python(worktree).parent
    failing = 0
    lines = 0
    for cmd in vc.TOOL_GATES[tool]:
        done = run(
            [str(bin_dir / cmd[0]), *cmd[1:]], worktree, timeout=vc.TEST_TIMEOUT_S
        )
        failing += done.returncode != 0
        lines += len(
            [ln for ln in (done.stdout + done.stderr).splitlines() if ln.strip()]
        )
    return failing, lines


def digest_gate(
    base: Path, cand: Path, corpora: dict[str, Path], label: str
) -> list[GraphDelta]:
    """Build every corpus with both sides; one GraphDelta per corpus whose
    graph differs. Identity is byte-for-byte on the canonical lines, so a
    single dropped CALLS edge is a difference."""
    deltas: list[GraphDelta] = []
    dumps = work_dir() / "dumps"
    dumps.mkdir(parents=True, exist_ok=True)
    for name, target in corpora.items():
        base_dump = dumps / f"{label}-{name}-base.txt"
        cand_dump = dumps / f"{label}-{name}-cand.txt"
        b = bench.measure(base, name, target, base_dump)
        c = bench.measure(cand, name, target, cand_dump)
        logger.info(
            ls.DIGESTS.format(corpus=name, base=b.digest[:12], cand=c.digest[:12])
        )
        if b.digest != c.digest:
            deltas.append(
                bench.graph_delta(
                    name,
                    base_dump.read_text(encoding=vc.ENCODING).splitlines(),
                    cand_dump.read_text(encoding=vc.ENCODING).splitlines(),
                )
            )
    return deltas
