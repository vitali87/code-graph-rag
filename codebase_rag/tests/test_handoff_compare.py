"""`scripts/handoff.py compare`: two engine selections, run against each other.

docs/reports/PHASE3_GO.md, sections 6 and 10 (step 2). Each pinned
repository whose language the two `CGR_INDEX_ENGINE` selections put on
different engines is indexed once under each, in a Python process of its
own, with the Go type checker on, and the two runs' file plans, project
indexes and graphs must be the same. Until the engine holds a language's
analysis both runs are Python's, so the comparison finds Python equal to
itself; a planted difference shows it would see one. These tests start the
real child processes, on a small checkout that stands in for a pinned
repository.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

from codebase_rag import constants as cs
from codebase_rag.canonical_graph import read_graph, write_graph
from codebase_rag.parsers.go_frontend.frontend import _build_tool
from codebase_rag.types_defs import ReferenceRepo
from scripts import handoff, reference_graphs
from scripts.reference_graphs import load_manifest

L = cs.SupportedLanguage
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
SEED = cs.REFERENCE_SEED_ENV
ENGINE = cs.INDEX_ENGINE_ENV
FIRST, SECOND = "python", "rust"


class Child(NamedTuple):
    """A process the script started: what it ran, with what environment."""

    command: list[str]
    env: dict[str, str]


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
        env: dict[str, str],
        **options: bool | int | str | Path,
    ) -> subprocess.CompletedProcess[str]:
        started.append(Child(command, env))
        return real(command, env=env, **options)

    monkeypatch.setattr(subprocess, "run", spy)
    return started


def _plant(monkeypatch: pytest.MonkeyPatch, edit) -> None:
    """`edit(out)` applied to the second side's files, as its run left them."""
    original = handoff.index_run

    def planted(
        repo: ReferenceRepo,
        source: Path,
        seed: int | None,
        out: Path,
        options: handoff.IndexOptions,
    ) -> None:
        original(repo, source, seed, out, options)
        if options.index_engine == SECOND:
            edit(out)

    monkeypatch.setattr(handoff, "index_run", planted)


def _drop_helper(out: Path) -> None:
    # A definition missing from the index, and the edge into it from the
    # graph.
    index = out / cs.HandoffFile.INDEX
    lines = index.read_bytes().split(b"\n")
    index.write_bytes(b"\n".join(line for line in lines if line != HELPER.encode()))
    graph = out / handoff.GRAPH_FILE
    write_graph(graph, [line for line in read_graph(graph) if not _into_helper(line)])


def _into_helper(line: str) -> bool:
    return "\tCALLS\t" in line and '\t"demo.main.helper"\t{' in line


def _swap_files(out: Path) -> None:
    plan = out / cs.HandoffFile.FILES
    header, first, second, *rest = plan.read_bytes().split(b"\n")
    plan.write_bytes(b"\n".join([header, second, first, *rest]))


class TestTheComparison:
    def test_python_is_equal_to_itself(
        self, pinned: Path, children: list[Child], tmp_path: Path, capsys
    ) -> None:
        status = handoff.compare([REPO], tmp_path / "cache", (FIRST, SECOND))
        out = capsys.readouterr().out

        assert status == 0, out
        assert out.splitlines() == [
            "demo (python): python against rust",
            "  files.txt: same",
            "  index.txt: same",
            "  graph.txt: same",
        ]
        first, second = children
        # One run per selection, both on the checkout itself, under the
        # interpreter's own seed, with the type checker on and the graph
        # exported beside the hand-offs.
        assert [child.env[ENGINE] for child in children] == [FIRST, SECOND]
        for child in children:
            assert SEED not in child.env
            assert child.env[cs.SETTING_GO_FRONTEND] == cs.GoFrontend.GOTYPES
            assert handoff.GRAPH_FLAG in child.command
            source = child.command[child.command.index(handoff.INDEX_COMMAND) + 1]
            assert Path(source) == pinned.resolve()

    def test_a_planted_difference_is_reported_as_exactly_its_lines(
        self, pinned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        _plant(monkeypatch, _drop_helper)

        status = handoff.compare([REPO], tmp_path / "cache", (FIRST, SECOND))
        out = capsys.readouterr().out.splitlines()

        assert status == cs.REFERENCE_EXIT_DIFFERENT
        heading, files, index, removed, graph, *edges = out
        assert heading == "demo (python): python against rust"
        assert files == "  files.txt: same"
        assert index == (
            "  index.txt: 1 lines only under python, 0 only under rust"
            "  <- first difference"
        )
        assert removed == f"    - {HELPER}"
        assert graph == "  graph.txt: 1 lines only under python, 0 only under rust"
        assert len(edges) == 1
        assert edges[0].startswith('    - E\tCALLS\tFunction\t"demo.main.main"\t')

    def test_the_file_plan_is_compared_in_order(
        self, pinned: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        _plant(monkeypatch, _swap_files)

        status = handoff.compare([REPO], tmp_path / "cache", (FIRST, SECOND))
        out = capsys.readouterr().out.splitlines()

        assert status == cs.REFERENCE_EXIT_DIFFERENT
        assert out[1].startswith("  files.txt: 1 lines only under python, 1 only")
        assert out[1].endswith("<- first difference")
        assert out[-2:] == ["  index.txt: same", "  graph.txt: same"]

    def test_the_preview_reaches_every_run(
        self, pinned: Path, children: list[Child], tmp_path: Path, capsys
    ) -> None:
        status = handoff.compare(
            [REPO], tmp_path / "cache", (FIRST, "python=rust"), preview="python"
        )

        assert status == 0, capsys.readouterr().out
        assert [
            child.command[child.command.index(handoff.PREVIEW_FLAG) + 1]
            for child in children
        ] == ["python", "python"]

    def test_a_go_repository_needs_go_on_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        uuid = next(repo for repo in load_manifest() if repo["language"] == L.GO)
        monkeypatch.setattr(handoff.shutil, "which", lambda _program: None)

        def never(*_args: str) -> Path:
            raise AssertionError("fetched a repository it could not compare")

        monkeypatch.setattr(handoff, "fetch", never)

        with pytest.raises(SystemExit) as exited:
            handoff.compare([uuid], tmp_path / "cache", ("python", "go=rust"))

        assert str(exited.value) == (
            f"{uuid['name']}: comparing a Go repository runs the Go type checker, "
            "and there is no `go` on PATH"
        )


GO_REPO = ReferenceRepo(
    name="demo-go",
    language="go",
    repository="example/demo-go",
    commit="1" * 40,
)
GO_FILES = {
    "go.mod": "module example.com/demo\n\ngo 1.22\n",
    "main.go": (
        "package main\n\ntype Shape interface {\n\tArea() int\n}\n\n"
        "type Square struct{}\n\nfunc (Square) Area() int {\n\treturn 1\n}\n\n"
        "func main() {\n\tvar s Shape = Square{}\n\t_ = s.Area()\n}\n"
    ),
}
UNAVAILABLE = (
    "Go frontend enabled but the go toolchain is unavailable; using tree-sitter"
)


@pytest.fixture
def pinned_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    checkout = tmp_path / "cache" / GO_REPO["commit"] / GO_REPO["name"]
    for rel, text in GO_FILES.items():
        path = checkout / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    monkeypatch.setattr(handoff, "fetch", lambda repo, cache: checkout)
    for name in (SEED, ENGINE):
        monkeypatch.delitem(reference_graphs._CALLER_ENV, name, raising=False)
    return checkout


def _go_types_runs(monkeypatch: pytest.MonkeyPatch) -> list[handoff.GoTypesRun]:
    """What each run wrote of the type checker, as it left it."""
    written: list[handoff.GoTypesRun] = []
    original = handoff.index_run

    def kept(
        repo: ReferenceRepo,
        source: Path,
        seed: int | None,
        out: Path,
        options: handoff.IndexOptions,
    ) -> None:
        original(repo, source, seed, out, options)
        path = out / handoff.GO_TYPES_FILE
        written.append(json.loads(path.read_text(encoding=cs.ENCODING_UTF8)))

    monkeypatch.setattr(handoff, "index_run", kept)
    return written


def _record(tmp_path: Path, record: handoff.GoTypesRun) -> Path:
    (tmp_path / handoff.GO_TYPES_FILE).write_text(json.dumps(record), encoding="utf-8")
    return tmp_path


class TestTheGoTypeChecker:
    """Without its answers on both sides, `compare` would hold two
    tree-sitter indexes to each other and pass: a Go too old, a helper that
    does not build, or a run that crashes each fall back alike."""

    @pytest.mark.usefixtures("fingerprint_inputs_once_per_session")
    def test_a_run_whose_type_checker_did_not_run_fails_the_comparison(
        self,
        pinned_go: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Go on this PATH, so the comparison starts, but on none the runs
        # see: each falls back to tree-sitter, as a failed build would.
        monkeypatch.setattr(handoff.shutil, "which", lambda _program: "go")
        monkeypatch.setitem(
            reference_graphs._CALLER_ENV, "PATH", str(Path(sys.executable).parent)
        )

        with pytest.raises(SystemExit) as exited:
            handoff.compare([GO_REPO], tmp_path / "cache", ("python", "go=rust"))

        heading, *warned = str(exited.value).splitlines()
        assert heading == (
            "demo-go: the Go type checker did not analyse . in the run under "
            "python, which fell back to tree-sitter; the run warned:"
        )
        assert f"    {UNAVAILABLE}" in warned

    @pytest.mark.usefixtures("fingerprint_inputs_once_per_session")
    def test_both_runs_of_a_go_repository_have_its_answers(
        self,
        pinned_go: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys,
    ) -> None:
        go = shutil.which("go")
        if go is None:
            pytest.skip("go toolchain not available")
        if _build_tool(go) is None:
            pytest.skip("gotypes tool could not build in this environment")
        written = _go_types_runs(monkeypatch)

        status = handoff.compare([GO_REPO], tmp_path / "cache", ("python", "go=rust"))

        assert status == 0, capsys.readouterr().out
        assert [(run["modules"], run["analysed"]) for run in written] == [
            (["."], ["."]),
            (["."], ["."]),
        ]

    @pytest.mark.parametrize(
        ("record", "refusal"),
        [
            (
                handoff.GoTypesRun(
                    modules=[".", "tools"],
                    analysed=["."],
                    warnings=["Go frontend tool failed for module anchor tools"],
                ),
                "demo-go: the Go type checker did not analyse tools in the run "
                "under go=rust, which fell back to tree-sitter; the run warned:\n"
                "    Go frontend tool failed for module anchor tools",
            ),
            (
                handoff.GoTypesRun(modules=[], analysed=[], warnings=[]),
                "demo-go: no go.mod for the Go type checker to analyse in the run "
                "under go=rust, so the comparison would run without it",
            ),
        ],
    )
    def test_a_module_left_out_or_none_to_analyse_is_refused(
        self, tmp_path: Path, record: handoff.GoTypesRun, refusal: str
    ) -> None:
        with pytest.raises(SystemExit) as exited:
            handoff._type_checked(GO_REPO, "go=rust", _record(tmp_path, record))

        assert str(exited.value) == refusal

    def test_a_run_that_wrote_nothing_of_it_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exited:
            handoff._type_checked(GO_REPO, "python", tmp_path)

        assert (
            str(exited.value) == "demo-go: the run under python wrote no gotypes.json"
        )

    def test_every_module_analysed_passes(self, tmp_path: Path) -> None:
        record = handoff.GoTypesRun(
            modules=[".", "tools"], analysed=[".", "tools"], warnings=[]
        )

        handoff._type_checked(GO_REPO, "python", _record(tmp_path, record))


class TestWhatIsCompared:
    @pytest.mark.parametrize(
        ("first", "second", "languages"),
        [
            ("python", "go=rust", {L.GO}),
            ("go=python", "go=rust", {L.GO}),
            ("python", "rust", set(L)),
            ("auto", "python", set()),
            ("python", "python;go=python", set()),
        ],
    )
    def test_the_languages_the_selections_part_on(
        self, first: str, second: str, languages: set[cs.SupportedLanguage]
    ) -> None:
        # As asked, whether or not this build runs either there.
        assert handoff.parted_languages(first, second) == languages

    def test_the_pinned_go_repositories_for_go_on_rust(self) -> None:
        compared = handoff.compared_repos(load_manifest(), "python", "go=rust")

        assert [repo["language"] for repo in compared] == [L.GO] * len(compared)
        assert "uuid" in [repo["name"] for repo in compared]


class TestTheCommandLine:
    def test_main_hands_the_chosen_repositories_to_the_comparison(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[list[str], tuple[str, str], str | None]] = []

        def recorded(
            repos: list[ReferenceRepo],
            cache: Path,
            engines: tuple[str, str],
            preview: str | None = None,
        ) -> int:
            calls.append(([repo["name"] for repo in repos], engines, preview))
            return 0

        monkeypatch.setattr(handoff, "compare", recorded)
        go_repos = [
            repo["name"] for repo in load_manifest() if repo["language"] == L.GO
        ]

        assert handoff.main(["compare", "--engines", "python,go=rust"]) == 0
        assert (
            handoff.main(
                ["compare", "uuid", "--engines", "go=python,go=rust", "--preview", "go"]
            )
            == 0
        )
        assert calls == [
            (go_repos, ("python", "go=rust"), None),
            (["uuid"], ("go=python", "go=rust"), "go"),
        ]

    @pytest.mark.parametrize(
        ("argv", "refusal"),
        [
            (["compare"], "--engines"),
            (["compare", "--engines", "python"], "give two CGR_INDEX_ENGINE values"),
            (
                ["compare", "--engines", "python,go=rust,rust"],
                "give two CGR_INDEX_ENGINE values",
            ),
            (["compare", "--engines", "python,"], "give two CGR_INDEX_ENGINE values"),
            (
                ["compare", "--engines", "python,go=fortran"],
                "'fortran' is not an engine; use one of auto, python, rust.",
            ),
            (
                ["compare", "--engines", "python,go=rust", "--preview", "fortran"],
                "'fortran' is not a language",
            ),
            (
                ["compare", "--engines", "auto,python"],
                "no repository chosen holds a language that auto and python put "
                "on different engines",
            ),
            (
                ["compare", "itsdangerous", "--engines", "python,go=rust"],
                "no repository chosen holds a language that python and go=rust "
                "put on different engines",
            ),
            (["compare", "no-such-repo", "--engines", "python,rust"], "unknown"),
        ],
    )
    def test_main_refuses_what_it_cannot_compare(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
        argv: list[str],
        refusal: str,
    ) -> None:
        def never(*_args: str, **_kwargs: str) -> int:
            raise AssertionError("compared with options it should have refused")

        monkeypatch.setattr(handoff, "compare", never)

        with pytest.raises(SystemExit) as exited:
            handoff.main(argv)

        assert exited.value.code == 2
        assert refusal in capsys.readouterr().err
