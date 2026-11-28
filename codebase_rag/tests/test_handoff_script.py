"""`scripts/handoff.py`: the pinned repositories' hand-offs, run against run.

docs/reports/PHASE1_SEAMS.md, sections 3 and 5 (step 6). Each run indexes in
a Python process of its own, under its own hash seed, the runs after the
first on a copy at another path, and every run must write the first run's
`files.txt` and `index.txt` byte for byte. These tests start the real child
processes, on a small checkout that stands in for a pinned repository.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.types_defs import ReferenceRepo
from scripts import handoff, reference_graphs
from scripts.reference_graphs import load_manifest

# In Python, whose grammar every install has, the base one too.
REPO = ReferenceRepo(
    name="demo",
    language="python",
    repository="example/demo",
    commit="0" * 40,
)
FILES = {
    "main.py": (
        "from pkg.util import upper\n\n\n"
        "def helper():\n    return upper('x')\n\n\n"
        "def main():\n    return helper()\n"
    ),
    "pkg/__init__.py": "",
    "pkg/util.py": "def upper(text):\n    return text.upper()\n",
}
HELPER = 'definition\tdemo.main.helper\t{"label":"Function"}'
WORKFLOW = handoff.PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
SEED = cs.REFERENCE_SEED_ENV
ENGINE = cs.INDEX_ENGINE_ENV


class Child(NamedTuple):
    """A process the script started: what it ran, where, with what
    environment, and how it ended."""

    command: list[str]
    cwd: Path
    env: dict[str, str]
    result: subprocess.CompletedProcess[str]


@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A local checkout the script takes for the pinned one, with the caller's
    environment holding no hash seed and no engine selection of its own."""
    checkout = tmp_path / "cache" / REPO["commit"] / REPO["name"]
    for rel, text in FILES.items():
        path = checkout / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    monkeypatch.setattr(handoff, "fetch", lambda repo, cache: checkout)
    for name in (SEED, ENGINE):
        monkeypatch.delitem(reference_graphs._CALLER_ENV, name, raising=False)
    return checkout


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> list[Child]:
    """Every process the script starts, each still run."""
    started: list[Child] = []
    real = subprocess.run

    def spy(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        **options: bool | int | str,
    ) -> subprocess.CompletedProcess[str]:
        result = real(command, cwd=cwd, env=env, **options)
        started.append(Child(command, cwd, env, result))
        return result

    monkeypatch.setattr(subprocess, "run", spy)
    return started


def _source(child: Child) -> Path:
    """The checkout the child indexed."""
    return Path(child.command[child.command.index(handoff.INDEX_COMMAND) + 1])


def _drop_helper_on_later_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run after the first whose index lacks one definition."""
    original = handoff.index_run

    def planted(
        repo: ReferenceRepo,
        source: Path,
        seed: int | None,
        out: Path,
        options: handoff.IndexOptions,
    ) -> None:
        original(repo, source, seed, out, options)
        if seed is not None:
            index = out / cs.HandoffFile.INDEX
            lines = index.read_bytes().split(b"\n")
            kept = [line for line in lines if line != HELPER.encode()]
            assert len(kept) == len(lines) - 1
            index.write_bytes(b"\n".join(kept))

    monkeypatch.setattr(handoff, "index_run", planted)


def test_two_runs_index_in_processes_of_their_own_and_agree(
    pinned: Path, children: list[Child], tmp_path: Path, capsys
) -> None:
    status = handoff.run([REPO], tmp_path / "cache", 2)
    out = capsys.readouterr().out

    assert status == 0, out
    assert out.splitlines() == ["demo (python): 3 files, 10 index lines"]
    first, second = children
    # The first under the interpreter's own seed, the second under another,
    # set; the first on the checkout, the second on a copy elsewhere, named
    # after the repository as the checkout is.
    assert SEED not in first.env
    assert second.env[SEED] == "1"
    assert _source(first) == pinned.resolve()
    assert _source(second) != pinned.resolve()
    assert _source(second).name == REPO["name"]
    # Each from a scratch directory of its own, so no `.env` of the caller's
    # applies, importing this checkout's script all the same.
    assert first.cwd != second.cwd
    assert handoff.PROJECT_ROOT not in (first.cwd, second.cwd)
    for child in children:
        assert child.command[1:3] == ["-m", "scripts.handoff"]
        paths = child.env["PYTHONPATH"].split(os.pathsep)
        assert paths[0] == str(handoff.PROJECT_ROOT)


def test_a_hash_seed_in_the_callers_environment_is_not_the_first_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Inherited, it would be the second run's seed too: PYTHONHASHSEED=1 in
    # the shell gave both runs seed 1 (Greptile review of PR #3717).
    monkeypatch.setitem(reference_graphs._CALLER_ENV, SEED, "1")

    assert SEED not in handoff._child_env(tmp_path, None, None)
    assert handoff._child_env(tmp_path, 1, None)[SEED] == "1"
    assert handoff._child_env(tmp_path, 2, None)[SEED] == "2"


def test_a_run_that_writes_other_files_fails_the_check(
    pinned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _drop_helper_on_later_runs(monkeypatch)

    status = handoff.run([REPO], tmp_path / "cache", 2)
    out = capsys.readouterr().out.splitlines()

    assert status == cs.REFERENCE_EXIT_DIFFERENT
    assert out == [
        "demo (python): 3 files, 10 index lines",
        "  the run with PYTHONHASHSEED=1 differs from the first in index.txt: "
        "1 lines only before, 0 only after",
        f"    - {HELPER}",
    ]


def _frame_files(monkeypatch: pytest.MonkeyPatch, plant: bytes = b"") -> list[bytes]:
    """What each run wrote of how the frame differed from Python, with
    `plant` written in its place on the runs after the first."""
    written: list[bytes] = []
    original = handoff.index_run

    def kept(
        repo: ReferenceRepo,
        source: Path,
        seed: int | None,
        out: Path,
        options: handoff.IndexOptions,
    ) -> None:
        original(repo, source, seed, out, options)
        written.append((out / handoff.FRAME_FILE).read_bytes())
        if seed is not None and plant:
            (out / handoff.FRAME_FILE).write_bytes(plant)

    monkeypatch.setattr(handoff, "index_run", kept)
    return written


def test_each_run_checks_the_frame_s_plan_and_reading_of_the_ignore_files(
    pinned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    (pinned / ".gitignore").write_text("build/\n!build/keep.py\n", encoding="utf-8")
    written = _frame_files(monkeypatch)

    status = handoff.run([REPO], tmp_path / "cache", 2)

    assert status == 0, capsys.readouterr().out
    # Compared, and the same: an empty file, where a run that could not
    # compare says so.
    assert written == [b"", b""]


def test_a_frame_that_differs_from_python_fails_the_check(
    pinned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _frame_files(monkeypatch, plant=b"- file\tmain.py\t0\n+ symlink\tmain.py")

    status = handoff.run([REPO], tmp_path / "cache", 2)
    out = capsys.readouterr().out.splitlines()

    assert status == cs.REFERENCE_EXIT_DIFFERENT
    assert out == [
        "demo (python): 3 files, 10 index lines",
        "  the frame differs from Python in the run with PYTHONHASHSEED=1: "
        "1 lines only before, 1 only after",
        "    - file\tmain.py\t0",
        "    + symlink\tmain.py",
    ]


def test_each_run_checks_the_frame_s_structure_too(tmp_path: Path) -> None:
    check = handoff._FrameCheck()
    difference = ["- node\tFolder\tpath=pkg\tname=pkg", "+ node\tFolder\tpath=pkg"]

    assert check.compares_structure()
    check.structure_differs(tmp_path, difference)

    assert (check.compared, check.lines) == (1, difference)


def test_each_run_checks_the_frame_s_module_names_too(tmp_path: Path) -> None:
    check = handoff._FrameCheck()
    difference = [
        '- module\tdemo.main\t{"language":"python","path":"main.py"}',
        '+ module\tdemo.main.py\t{"language":"python","path":"main.py"}',
    ]

    assert check.compares_modules()
    check.modules_differ(tmp_path, difference)

    assert (check.compared, check.lines) == (1, difference)


def test_the_frame_s_reading_of_the_ignore_files_is_compared(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag import engine_frame
    from codebase_rag.config import load_ignore_patterns

    (pinned / ".cgrignore").write_text("docs/\n!keep.py\n", encoding="utf-8")
    python = load_ignore_patterns(pinned)
    assert handoff._reading_difference(pinned, python) == []
    read = engine_frame.frame_ignore_patterns(pinned)
    assert read is not None
    monkeypatch.setattr(
        engine_frame,
        "frame_ignore_patterns",
        lambda _repo: read._replace(exclude=frozenset({"doc/"})),
    )

    assert handoff._reading_difference(pinned, python) == [
        "- ignore exclude\tdocs/",
        "+ ignore exclude\tdoc/",
    ]


def test_a_recording_writes_the_first_runs_files_once_every_run_agrees(
    pinned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "handoffs"

    status = handoff.run([REPO], tmp_path / "cache", 2, out=out)

    assert status == 0
    written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.txt"))
    assert written == ["demo/files.txt", "demo/index.txt"]
    index = (out / "demo" / cs.HandoffFile.INDEX).read_bytes()
    assert index.startswith(b"# cgr handoff index.txt 2\n")
    assert f"\n{HELPER}\n".encode() in index

    _drop_helper_on_later_runs(monkeypatch)
    (out / "demo" / cs.HandoffFile.INDEX).write_bytes(b"kept\n")

    status = handoff.run([REPO], tmp_path / "cache", 2, out=out)

    assert status == cs.REFERENCE_EXIT_DIFFERENT
    assert (out / "demo" / cs.HandoffFile.INDEX).read_bytes() == b"kept\n"


def test_the_selection_reaches_the_index_environment_and_no_other(
    pinned: Path,
    children: list[Child],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setitem(reference_graphs._CALLER_ENV, ENGINE, "python")
    warned = ls.INDEX_ENGINE_NOT_ON_RUST.format(language="go")

    status = handoff.run([REPO], tmp_path / "cache", 2, index_engine="go=rust")

    assert status == 0
    assert [child.env[ENGINE] for child in children] == ["go=rust", "go=rust"]
    # Each index read it: this build cannot index Go on Rust, and says so.
    assert all(warned in child.result.stderr for child in children)

    children.clear()
    status = handoff.run([REPO], tmp_path / "cache", 2)

    assert status == 0
    assert [ENGINE in child.env for child in children] == [False, False]
    assert not any(warned in child.result.stderr for child in children)


def test_main_hands_its_options_to_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], int, Path | None, str | None, str | None]] = []

    def recorded(
        repos: list[ReferenceRepo],
        cache: Path,
        runs: int,
        *,
        out: Path | None,
        index_engine: str | None,
        preview: str | None,
    ) -> int:
        names = [repo["name"] for repo in repos]
        calls.append((names, runs, out, index_engine, preview))
        return 0

    monkeypatch.setattr(handoff, "run", recorded)

    assert handoff.main(["check", "uuid", "--index-engine", "go=rust"]) == 0
    assert handoff.main(["check", "--runs", "3"]) == 0
    assert handoff.main(["record", "uuid", "--out", str(tmp_path)]) == 0
    assert handoff.main(["check", "uuid", "--preview", "go"]) == 0
    assert calls == [
        (["uuid"], 2, None, "go=rust", None),
        ([repo["name"] for repo in load_manifest()], 3, None, None, None),
        (["uuid"], 2, tmp_path, None, None),
        (["uuid"], 2, None, None, "go"),
    ]


@pytest.mark.parametrize(
    ("argv", "refusal"),
    [
        (
            ["check", "--index-engine", "go=fortran"],
            "'fortran' is not an engine; use one of auto, python, rust.",
        ),
        (["check", "no-such-repo"], "unknown repositories: no-such-repo"),
        (["check", "--runs", "1"], "--runs 2"),
        (["record", "uuid"], "--out"),
        (["check", "--preview", "c"], "this preview names only c."),
    ],
)
def test_main_refuses_what_it_cannot_run(
    monkeypatch: pytest.MonkeyPatch, capsys, argv: list[str], refusal: str
) -> None:
    def never(*_args: str, **_kwargs: str) -> int:
        raise AssertionError("ran with options it should have refused")

    monkeypatch.setattr(handoff, "run", never)

    with pytest.raises(SystemExit) as exited:
        handoff.main(argv)

    assert exited.value.code == 2
    assert refusal in capsys.readouterr().err


def test_the_reference_graphs_job_checks_the_hand_offs_with_its_install() -> None:
    # With every tier's extra, as the graphs are checked, and after a
    # difference in them too, so one push shows every check's. The engine
    # comparison runs the Go type checker, so Go is installed before it.
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding=cs.ENCODING_UTF8))
    steps = workflow["jobs"]["reference-graphs"]["steps"]
    checks = [step for step in steps if "scripts.handoff" in step.get("run", "")]

    assert [step["run"] for step in checks] == [
        "uv run --no-sync python -m scripts.handoff check --runs 2",
        "uv run --no-sync python -m scripts.handoff compare --engines python,go=rust",
    ]
    assert [step["if"] for step in checks] == ["${{ !cancelled() }}"] * 2
    go = [
        number
        for number, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/setup-go@")
    ]
    assert len(go) == 1
    assert go[0] < steps.index(checks[1])
    assert steps[go[0]]["with"]["go-version"] == ">=1.27.2"
