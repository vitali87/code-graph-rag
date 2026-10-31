import csv
import shutil
import subprocess
from pathlib import Path

import pytest
import typer

from codebase_rag import constants as cs
from evals import constants as ec
from evals.retrieval import (
    cgr_call_edges,
    colgrep_call_edges,
    colgrep_index,
    first_party_property_names,
    first_party_symbols,
    grep_call_edges,
    main,
    oracle_call_edges,
    parse_py_trees,
    sample_symbols,
    score_retrieval,
)
from evals.types_defs import NameEdge, NodeKey

_CALLS = cs.RelationshipType.CALLS.value
_MODULE = cs.NodeLabel.MODULE.value

_RG = shutil.which(ec.RG_BIN)
needs_rg = pytest.mark.skipif(_RG is None, reason="ripgrep not installed")

# core.py genuinely CALLS helper(), instantiates Widget(), and calls w.run();
# build() is defined but never called, so it is a caller, never a callee.
_CORE = """\
def helper():
    return 1


class Widget:
    def run(self):
        return helper()


def build():
    helper()
    w = Widget()
    w.run()
    return w
"""

# uses.py only imports and aliases helper/Widget; it never calls them, so a
# name-based grep over-includes it while the call oracle does not.
_USES = """\
from pkg.core import Widget, helper

ALIAS = helper
VALUE = 2
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(_CORE, encoding="utf-8")
    (pkg / "uses.py").write_text(_USES, encoding="utf-8")
    return tmp_path


def _edge(file: str, name: str) -> NameEdge:
    return NameEdge(_CALLS, NodeKey(_MODULE, file, ec.MODULE_START_LINE), name)


def test_oracle_captures_first_party_calls(repo: Path) -> None:
    trees, _files = parse_py_trees(repo)
    fp = first_party_symbols(trees)
    oracle = oracle_call_edges(trees, fp)

    assert _edge("pkg/core.py", "helper") in oracle
    assert _edge("pkg/core.py", "Widget") in oracle
    assert _edge("pkg/core.py", "run") in oracle
    # build is defined but never called -> never a callee edge.
    assert _edge("pkg/core.py", "build") not in oracle
    # uses.py references symbols but calls none of them.
    assert not any(e.source.file == "pkg/uses.py" for e in oracle)


# A property is invoked by a bare attribute read (getter, Load) or write
# (setter, Store) with no parens; both are descriptor-method calls cgr emits a
# CALLS edge for, so the oracle counts both. A `del` (deleter) is not a
# retrieval call, and a bare method reference (no parens) is not a call.
_PROPS = """\
class Model:
    @property
    def related_model(self):
        return 1

    @cached_property
    def output_field(self):
        return 2

    @property
    def slot(self):
        return 4

    @slot.setter
    def slot(self, v):
        self._slot = v

    @property
    def gone(self):
        return 5

    def plain(self):
        return 3

    def user(self):
        a = self.related_model
        b = self.output_field
        cb = self.plain
        return a, b, cb

    def writer(self, x):
        self.slot = x
        del self.gone
"""


def test_oracle_captures_property_access_as_calls(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "m.py").write_text(_PROPS, encoding="utf-8")
    trees, _files = parse_py_trees(tmp_path)
    fp = first_party_symbols(trees)
    props = first_party_property_names(trees)

    assert props == {"related_model", "output_field", "slot", "gone"}
    oracle = oracle_call_edges(trees, fp, props)
    # getter read (Load) and setter write (Store) both count.
    assert _edge("pkg/m.py", "related_model") in oracle
    assert _edge("pkg/m.py", "output_field") in oracle
    assert _edge("pkg/m.py", "slot") in oracle
    # `cb = self.plain` is a bound-method reference, not a call.
    assert _edge("pkg/m.py", "plain") not in oracle
    # `del self.gone` (Del) is the only access of gone and is not a call.
    assert _edge("pkg/m.py", "gone") not in oracle


@needs_rg
def test_grep_name_overincludes_vs_oracle(repo: Path) -> None:
    trees, files = parse_py_trees(repo)
    fp = first_party_symbols(trees)
    oracle = oracle_call_edges(trees, fp)
    grep_name = grep_call_edges(repo, fp, files, ec.GrepMode.NAME)

    # bare import/alias of helper in uses.py is a grep false positive.
    assert _edge("pkg/uses.py", "helper") in grep_name
    assert _edge("pkg/uses.py", "helper") not in oracle
    # build's definition site mentions its name though it is never called.
    assert _edge("pkg/core.py", "build") in grep_name
    assert _edge("pkg/core.py", "build") not in oracle


@needs_rg
def test_grep_call_excludes_bare_reference_but_flags_def_site(repo: Path) -> None:
    trees, files = parse_py_trees(repo)
    fp = first_party_symbols(trees)
    grep_call = grep_call_edges(repo, fp, files, ec.GrepMode.CALL)

    # `def build():` matches NAME( -> grep cannot tell a def from a call.
    assert _edge("pkg/core.py", "build") in grep_call
    # `ALIAS = helper` is not followed by ( -> the call-pattern excludes it.
    assert _edge("pkg/uses.py", "helper") not in grep_call


def test_score_retrieval_computes_prf() -> None:
    e1, e2, e3 = _edge("a.py", "f"), _edge("a.py", "g"), _edge("b.py", "h")
    oracle = {e1, e2, e3}
    retrieved = {e1, e2, _edge("c.py", "x")}  # tp=2, fp=1, fn=1
    result = score_retrieval([(ec.RetrievalCondition.GRAPH.value, retrieved)], oracle)
    row = next(
        r for r in result.rows if r["label"] == ec.RetrievalCondition.GRAPH.value
    )
    assert (row["tp"], row["fp"], row["fn"]) == (2, 1, 1)
    assert row["precision"] == round(2 / 3, ec.ROUND_DIGITS)
    assert row["recall"] == round(2 / 3, ec.ROUND_DIGITS)


@needs_rg
def test_grep_preserves_colon_in_path(repo: Path) -> None:
    # a .py file whose name contains a colon must keep its full path; the
    # ripgrep output separator must not be confused with a path colon.
    (repo / "pkg" / "od:d.py").write_text(
        "from pkg.core import helper\n\nhelper()\n", encoding="utf-8"
    )
    trees, files = parse_py_trees(repo)
    fp = first_party_symbols(trees)
    grep_name = grep_call_edges(repo, fp, files, ec.GrepMode.NAME)

    assert _edge("pkg/od:d.py", "helper") in grep_name


def test_cgr_call_edges_smoke(repo: Path) -> None:
    trees, _files = parse_py_trees(repo)
    fp = first_party_symbols(trees)
    cgr = cgr_call_edges(repo, repo.name, fp)

    assert isinstance(cgr, set)
    # cgr resolves the intra-module first-party call helper() in core.py.
    assert _edge("pkg/core.py", "helper") in cgr


# ColGrep is a Rust binary that downloads a model on first use, so these tests
# stand in for it at the subprocess boundary: they pin the command the arm
# sends and how it reads the answer, not ColGrep's ranking. Any other command
# (ripgrep, git) still runs for real.
_REAL_RUN = subprocess.run
_KwargValue = str | Path | bool | None


class _FakeColGrep:
    def __init__(
        self,
        answers: dict[tuple[str, bool], list[str]],
        fail: frozenset[str] = frozenset(),
        init_error: str | None = None,
    ) -> None:
        self.answers = answers
        self.fail = fail
        self.init_error = init_error
        self.calls: list[list[str]] = []
        self.data_dirs: list[str] = []

    def __call__(
        self, argv: list[str], **kwargs: _KwargValue
    ) -> subprocess.CompletedProcess[str]:
        if not isinstance(argv, list) or argv[0] != ec.COLGREP_BIN:
            return _REAL_RUN(argv, **kwargs)
        self.calls.append(list(argv))
        env = kwargs["env"]
        assert isinstance(env, dict)
        self.data_dirs.append(env[ec.COLGREP_DATA_DIR_ENV])
        if argv[1] == ec.COLGREP_INIT:
            code = 0 if self.init_error is None else 2
            return subprocess.CompletedProcess(argv, code, "", self.init_error or "")
        name = argv[argv.index(ec.COLGREP_END_OF_OPTIONS) + 1]
        if name in self.fail:
            return subprocess.CompletedProcess(argv, 2, "", "boom")
        lines = self.answers.get((name, ec.COLGREP_PATTERN_FLAG in argv), [])
        return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")


def _follows(argv: list[str], flag: str, value: str) -> bool:
    return argv[argv.index(flag) + 1] == value


def test_colgrep_search_command(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeColGrep({})
    monkeypatch.setattr(subprocess, "run", fake)
    data_dir = repo / "cg"

    colgrep_call_edges(repo, {"init"}, set(), ec.GrepMode.NAME, data_dir, top_k=7)
    colgrep_call_edges(repo, {"init"}, set(), ec.GrepMode.CALL, data_dir, top_k=7)
    name_argv, call_argv = fake.calls
    # ColGrep reads its saved settings from beside this directory, so every
    # call must carry it, or the user's own settings would steer the run.
    assert fake.data_dirs == [str(data_dir)] * 2

    for argv in (name_argv, call_argv):
        # The explicit `search` subcommand plus `--` keep a symbol named like
        # a colgrep subcommand (`init` here) a query rather than a command.
        assert argv[:2] == [ec.COLGREP_BIN, ec.COLGREP_SEARCH]
        assert argv[-3:] == [ec.COLGREP_END_OF_OPTIONS, "init", ec.RG_SEARCH_PATH]
        assert _follows(argv, ec.COLGREP_TOP_K_FLAG, "7")
        assert _follows(argv, ec.COLGREP_INCLUDE_FLAG, ec.RG_PY_GLOB)
        assert _follows(argv, ec.COLGREP_COLOR_FLAG, ec.COLGREP_COLOR_NEVER)
        assert ec.COLGREP_FILES_ONLY in argv
        assert ec.COLGREP_NO_UPDATE in argv
        assert ec.COLGREP_FORCE_CPU in argv

    # NAME is the ranked search alone; CALL narrows with grep_call's own
    # pattern, matched case-sensitively as ripgrep does.
    assert ec.COLGREP_PATTERN_FLAG not in name_argv
    assert _follows(call_argv, ec.COLGREP_PATTERN_FLAG, r"\binit\s*\(")
    assert ec.COLGREP_CASE_SENSITIVE in call_argv


def test_colgrep_edges_keep_only_graded_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repo.resolve()
    fake = _FakeColGrep(
        {
            ("helper", False): [
                str(root / "pkg" / "core.py"),
                "pkg/uses.py",
                "README.md",
                str(root.parent / "elsewhere.py"),
            ],
            ("helper", True): ["pkg/core.py"],
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)
    _trees, files = parse_py_trees(repo)

    # Absolute and cwd-relative paths both map onto the graded file set; a
    # file outside it or outside the target is dropped.
    data_dir = repo / "cg"
    assert colgrep_call_edges(repo, {"helper"}, files, ec.GrepMode.NAME, data_dir) == {
        _edge("pkg/core.py", "helper"),
        _edge("pkg/uses.py", "helper"),
    }
    assert colgrep_call_edges(repo, {"helper"}, files, ec.GrepMode.CALL, data_dir) == {
        _edge("pkg/core.py", "helper")
    }


def test_colgrep_failed_search_is_skipped(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeColGrep(
        {("helper", False): ["pkg/core.py"], ("build", False): ["pkg/core.py"]},
        fail=frozenset({"build"}),
    )
    monkeypatch.setattr(subprocess, "run", fake)
    _trees, files = parse_py_trees(repo)

    edges = colgrep_call_edges(
        repo, {"helper", "build"}, files, ec.GrepMode.NAME, repo / "cg"
    )

    assert edges == {_edge("pkg/core.py", "helper")}


def test_colgrep_index_reports_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", _FakeColGrep({}))
    assert colgrep_index(repo, repo / "cg") is None

    failing = _FakeColGrep({}, init_error="model download failed")
    monkeypatch.setattr(subprocess, "run", failing)
    assert colgrep_index(repo, repo / "cg") == "model download failed"
    (argv,) = failing.calls
    assert argv[:2] == [ec.COLGREP_BIN, ec.COLGREP_INIT]
    assert ec.COLGREP_YES in argv


def test_sample_symbols_is_seeded_subset() -> None:
    symbols = {f"s{i}" for i in range(50)}

    picked = sample_symbols(symbols, 10, seed=3)

    assert len(picked) == 10
    assert picked <= symbols
    assert sample_symbols(symbols, 10, seed=3) == picked
    assert sample_symbols(symbols, 0, seed=3) == symbols
    assert sample_symbols(symbols, 99, seed=3) == symbols


@needs_rg
def test_main_scores_colgrep_arms_on_the_sample(
    repo: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    every = ["pkg/core.py", "pkg/uses.py"]
    fake = _FakeColGrep(
        {
            (name, call): every
            for name in ("helper", "Widget", "run", "build")
            for call in (False, True)
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)
    real_which = shutil.which

    def which(cmd: str, mode: int = 1, path: str | None = None) -> str | None:
        return cmd if cmd == ec.COLGREP_BIN else real_which(cmd, mode, path)

    monkeypatch.setattr(shutil, "which", which)
    out_dir = tmp_path_factory.mktemp("out")

    main(
        target=repo,
        project_name="",
        out_dir=out_dir,
        sample=3,
        seed=0,
        colgrep=True,
        colgrep_k=2,
    )

    with (out_dir / ec.RETRIEVAL_SCORES_FILENAME).open(encoding="utf-8") as handle:
        rows = {row["label"]: row for row in csv.DictReader(handle)}
    assert {
        ec.RetrievalCondition.COLGREP_NAME.value,
        ec.RetrievalCondition.COLGREP_CALL.value,
    } <= rows.keys()
    # Every arm is graded against one oracle, so tp + fn agrees across rows.
    assert len({int(r["tp"]) + int(r["fn"]) for r in rows.values()}) == 1
    # One index build, then one search per sampled symbol and mode.
    searched = [argv for argv in fake.calls if argv[1] == ec.COLGREP_SEARCH]
    assert len(fake.calls) - len(searched) == 1
    assert len(searched) == 2 * 3
    assert all(_follows(argv, ec.COLGREP_TOP_K_FLAG, "2") for argv in searched)
    # One throwaway settings-and-index directory for the whole run, removed
    # afterwards, never ColGrep's default location.
    (data_dir,) = set(fake.data_dirs)
    assert not Path(data_dir).exists()


@needs_rg
def test_main_stops_when_nothing_is_called(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    (tmp_path / "lonely.py").write_text("def lonely():\n    return 1\n", "utf-8")

    with pytest.raises(typer.Exit):
        main(
            target=tmp_path,
            project_name="",
            out_dir=tmp_path_factory.mktemp("out"),
            sample=0,
            seed=0,
            colgrep=False,
            colgrep_k=ec.COLGREP_DEFAULT_TOP_K,
        )
