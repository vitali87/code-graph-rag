"""The frame's module-name plan against Python's module map.

Phase 2's step 8 (docs/reports/PHASE2_FRAME.md, sections 10 and 13) builds
the merge: each parsed file's summary, folded into the project's index in
walk order. Its first section is the module names, which file claims each
module's bare name and which keep their extension, and it must name every
module as the definition processor does: from the path, a package's
`__init__` file and a `mod.rs` by their folder, a notebook with its
extension, a same-stem pair of two language families both with theirs, a
`.d.ts` beside its implementation with its own, and otherwise the first
file in walk order with the bare name. Each rule is held here to Python's
own, on a pair of files where walk order and sorted path order part. The
plan is compared, on every run whose map starts empty, with the map, as
`index.txt`'s `module` lines; Python's map is the one used, whatever the
comparison finds. The existing hand-off and naming tests run once more with
every module map compared (`module_shadow.py`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import engine_frame
from codebase_rag.config import load_ignore_patterns
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import _MockIngestor
from codebase_rag.tests.engine_modes import MODULE_SHADOW_DIR_ENV
from codebase_rag.tests.test_jupyter_notebooks import _cell, _notebook
from codebase_rag.tests.test_symlink_policy import _link, _write
from codebase_rag.types_defs import FileRoute
from codebase_rag.utils.path_utils import base_module_qn

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent
SHADOW_PLUGIN = "codebase_rag.tests.module_shadow"
# The tests that rerun under the shadow: the hand-off's, and every test file
# that reads the module map or names a module by one of its rules.
SHADOW_MARKERS = re.compile(
    r"module_qn_to_file_path|_disambiguate_module_qn|base_module_qn"
    r"|own_extension_module_qn|has_other_language_sibling"
    r"|has_implementation_sibling|handoff_dir|same.stem|\.d\.ts|\.ipynb"
    r"|__init__\.pyi"
)
PROJECT = "proj"
needs_links = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")

# A source that parses, by suffix.
SOURCES = {
    ".py": "def f():\n    return 1\n",
    ".pyi": "def f() -> int: ...\n",
    ".js": "function f() {\n  return 1;\n}\n",
    ".ts": "export function f(): number {\n  return 1;\n}\n",
    ".mts": "export function f(): number {\n  return 1;\n}\n",
    ".mjs": "export function f() {\n  return 1;\n}\n",
    ".c": "int f(void) { return 1; }\n",
    ".h": "int f(void);\n",
    ".rs": "pub fn f() -> i32 {\n    1\n}\n",
    ".go": "package m\n\nfunc F() int { return 1 }\n",
    ".md": "# notes\n",
}
PYTHON_NOTEBOOK = _notebook([_cell("code", ["def g():\n", "    return 2\n"])])
R_NOTEBOOK = _notebook(
    [_cell("code", ["g <- function() 2\n"])],
    metadata={"kernelspec": {"language": "R", "name": "ir"}},
)


def _source(rel: str) -> str:
    if rel.endswith(cs.EXT_IPYNB):
        return PYTHON_NOTEBOOK
    return SOURCES[Path(rel).suffix]


def _tree(root: Path, files: Iterable[str] | Mapping[str, str]) -> Path:
    """Each file, with a source that parses unless one is given."""
    root.mkdir(parents=True, exist_ok=True)
    contents = files if isinstance(files, Mapping) else {rel: None for rel in files}
    for rel, text in contents.items():
        _write(root / rel, _source(rel) if text is None else text)
    return root


class _Recorded:
    """A `PlanCheck` that compares every module map and keeps what it is
    told; the rest of the frame is left out."""

    def __init__(self) -> None:
        self.compared = 0
        self.modules: list[list[str]] = []

    def compares(self) -> bool:
        return False

    def differs(self, repo_path: Path, difference: Sequence[str]) -> None:
        raise AssertionError(list(difference))

    def compares_changes(self) -> bool:
        return False

    def changes_differ(self, repo_path: Path, difference: Sequence[str]) -> None:
        raise AssertionError(list(difference))

    def compares_structure(self) -> bool:
        return False

    def structure_differs(self, repo_path: Path, difference: Sequence[str]) -> None:
        raise AssertionError(list(difference))

    def compares_modules(self) -> bool:
        self.compared += 1
        return True

    def modules_differ(self, repo_path: Path, difference: Sequence[str]) -> None:
        self.modules.append(list(difference))


@pytest.fixture
def checked(monkeypatch: pytest.MonkeyPatch) -> _Recorded:
    """Every module map compared, the frame off, any difference kept."""
    check = _Recorded()
    monkeypatch.setattr(engine_frame, "ENABLED", False)
    monkeypatch.setattr(engine_frame, "CHECK", check)
    return check


class Compared:
    """What each compared run held: Python's module map, as path -> qn, and
    the frame's plan for the same claims, the same way."""

    def __init__(self) -> None:
        self.python: list[dict[str, str]] = []
        self.frame: list[dict[str, str] | None] = []
        self.claims: list[list[engine_frame.ModuleClaim]] = []


@pytest.fixture
def compared(checked: _Recorded, monkeypatch: pytest.MonkeyPatch) -> Compared:
    found = Compared()
    original = engine_frame.modules_for_run

    def recording(
        repo_path: Path,
        project_name: str,
        claims: Sequence[engine_frame.ModuleClaim],
        exclude: frozenset[str] | None,
        unignore: frozenset[str] | None,
        python: Mapping[str, Path],
    ) -> None:
        found.claims.append(list(claims))
        found.python.append(
            {path: qn for qn, path, _ in engine_frame.python_modules(repo_path, python)}
        )
        frame = engine_frame.frame_module_plan(
            repo_path, project_name, claims, exclude, unignore
        )
        found.frame.append(
            None if frame is None else {path: qn for qn, path, _ in frame}
        )
        original(repo_path, project_name, claims, exclude, unignore, python)

    monkeypatch.setattr(engine_frame, "modules_for_run", recording)
    return found


def _updater(target: Path, **kwargs: frozenset[str] | None) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        _MockIngestor(),
        target,
        parsers,
        queries,
        project_name=PROJECT,
        project_named=True,
        skip_embeddings=True,
        **kwargs,
    )


def _names(
    compared: Compared,
    checked: _Recorded,
    repo: Path,
    exclude: frozenset[str] | None = None,
) -> dict[str, str]:
    """Each module's name, by its file's path, once a fresh index of `repo`
    has named every module as the frame does."""
    _updater(repo, exclude_paths=exclude).run()
    (python,) = compared.python[-1:]
    (frame,) = compared.frame[-1:]
    assert frame is not None, "the extension lacks the merge; rebuild the crate"
    assert checked.modules == []
    assert frame == python
    return python


def test_the_frame_reads_python_s_naming_tables() -> None:
    assert engine_frame.naming_rules() is not None
    made = engine_frame.engine()
    assert made is not None
    assert hasattr(made, cs.EG_RUST_MODULE_PLAN)


class TestRules:
    def test_names_come_from_the_path_less_the_module_extension(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            ["top.py", "a/b/c.py", "lib/a.b.js", "types/api.d.ts", "src/main.rs"],
        )

        assert _names(compared, checked, repo) == {
            "a/b/c.py": "proj.a.b.c",
            "lib/a.b.js": "proj.lib.a.b",
            "src/main.rs": "proj.src.main",
            "top.py": "proj.top",
            "types/api.d.ts": "proj.types.api",
        }

    def test_a_package_s_init_and_a_mod_rs_take_their_folder_s_name(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            ["__init__.py", "pkg/__init__.py", "stubs/__init__.pyi", "src/net/mod.rs"],
        )

        assert _names(compared, checked, repo) == {
            "__init__.py": "proj",
            "pkg/__init__.py": "proj.pkg",
            "src/net/mod.rs": "proj.src.net",
            "stubs/__init__.pyi": "proj.stubs",
        }

    def test_a_module_and_a_package_of_one_name_in_one_folder(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        # `lib/pkg.py` is walked before `lib/pkg/__init__.py`, a folder's
        # files before its subfolders, so it keeps the bare name, though
        # `lib/pkg/__init__.py` sorts first part by part.
        repo = _tree(tmp_path / "repo", ["lib/pkg.py", "lib/pkg/__init__.py"])

        assert _names(compared, checked, repo) == {
            "lib/pkg.py": "proj.lib.pkg",
            "lib/pkg/__init__.py": "proj.lib.pkg.py",
        }

    def test_a_notebook_keeps_its_extension_and_one_not_in_python_claims_none(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            {
                "analysis.ipynb": PYTHON_NOTEBOOK,
                "analysis.py": SOURCES[".py"],
                "stats.ipynb": R_NOTEBOOK,
                "stats/ipynb.py": SOURCES[".py"],
            },
        )

        # An R notebook is no module, so `stats/ipynb.py` keeps the name it
        # would have shared with a Python one.
        assert _names(compared, checked, repo) == {
            "analysis.ipynb": "proj.analysis.ipynb",
            "analysis.py": "proj.analysis",
            "stats/ipynb.py": "proj.stats.ipynb",
        }

    def test_a_same_stem_file_of_another_family_makes_both_keep_their_extensions(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            [
                "util.py",
                "util.js",
                "types.py",
                "types.d.ts",
                "pkg/__init__.pyi",
                "pkg/__init__.js",
                "mod.py",
                "mod.rs",
                "lone.go",
                "lone.md",
            ],
        )

        assert _names(compared, checked, repo) == {
            "lone.go": "proj.lone",
            "mod.py": "proj.mod",
            "mod.rs": "proj",
            "pkg/__init__.js": "proj.pkg.__init__",
            "pkg/__init__.pyi": "proj.pkg",
            "types.d.ts": "proj.types.d.ts",
            "types.py": "proj.types.py",
            "util.js": "proj.util.js",
            "util.py": "proj.util.py",
        }

    def test_a_same_stem_file_of_one_family_is_named_by_walk_order(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", ["x.c", "x.h", "y.js", "y.ts"])

        assert _names(compared, checked, repo) == {
            "x.c": "proj.x",
            "x.h": "proj.x.h",
            "y.js": "proj.y",
            "y.ts": "proj.y.ts",
        }

    def test_a_declaration_with_and_without_its_implementation(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            ["foo.d.ts", "foo.ts", "bar.d.ts", "baz.d.ts", "baz.ts", "qux.d.mts"],
        )
        _write(tmp_path / "repo" / "qux.mjs", SOURCES[".mjs"])

        # `foo.d.ts` walks first but yields the name to `foo.ts`; with no
        # implementation, or an excluded one, a declaration keeps it.
        assert _names(compared, checked, repo, frozenset({"baz.ts"})) == {
            "bar.d.ts": "proj.bar",
            "baz.d.ts": "proj.baz",
            "foo.d.ts": "proj.foo.d.ts",
            "foo.ts": "proj.foo",
            "qux.d.mts": "proj.qux.d.mts",
            "qux.mjs": "proj.qux",
        }

    @needs_links
    def test_a_linked_sibling_is_no_sibling(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", ["util.py", "real.js", "foo.d.ts", "real.ts"])
        _link(repo / "util.js", "real.js")
        _link(repo / "foo.ts", "real.ts")

        names = _names(compared, checked, repo)

        assert names["util.py"] == "proj.util"
        assert names["foo.d.ts"] == "proj.foo"

    def test_the_first_file_in_walk_order_claims_where_sorted_order_parts(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        # `pkg/__init__.py` sorts before `pkg.py` part by part, and
        # `a.b/__init__.py` before `a/b.py` as a string; the walk takes the
        # other of each first.
        repo = _tree(
            tmp_path / "repo",
            ["pkg.py", "pkg/__init__.py", "a/b.py", "a.b/__init__.py"],
        )

        assert _names(compared, checked, repo) == {
            "a.b/__init__.py": "proj.a.b.py",
            "a/b.py": "proj.a.b",
            "pkg.py": "proj.pkg",
            "pkg/__init__.py": "proj.pkg.py",
        }
        (claims,) = compared.claims
        assert [claim.rel_path for claim in claims] == [
            "pkg.py",
            "a/b.py",
            "a.b/__init__.py",
            "pkg/__init__.py",
        ]

    def test_a_later_claim_takes_its_last_suffix_over_what_holds_that_name(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        # `foo.d.ts` loses `proj.foo` to `foo.d.mts` and takes `.ts`, its
        # last suffix. `d/x.h` loses `proj.d.x` to `d/x.c` and takes
        # `proj.d.x.h` from `d.x.h.c`, which the map then no longer holds.
        repo = _tree(
            tmp_path / "repo", ["foo.d.mts", "foo.d.ts", "d.x.h.c", "d/x.c", "d/x.h"]
        )

        assert _names(compared, checked, repo) == {
            "d/x.c": "proj.d.x",
            "d/x.h": "proj.d.x.h",
            "foo.d.mts": "proj.foo",
            "foo.d.ts": "proj.foo.ts",
        }


class TestRuns:
    def test_a_single_file_run_compares_its_one_module(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", ["util.py", "util.js", "other.py"])
        _updater(repo).run()
        _write(repo / "util.py", "def f():\n    return 2\n")

        _updater(repo / "util.py").run()

        # Its sibling is on disk, though its plan holds the one file.
        assert compared.python[-1] == {"util.py": "proj.util.py"}
        assert compared.frame[-1] == compared.python[-1]
        assert checked.modules == []

    def test_a_run_whose_map_holds_another_run_s_names_is_not_compared(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py", "b.py"])
        updater = _updater(repo)
        updater.run()

        updater.run(force=True)

        assert checked.compared == 1
        assert len(compared.python) == 1

    def test_a_run_compares_only_the_files_parsed_into_the_map(
        self, tmp_path: Path, compared: Compared, checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py", "notes.md", "b.pyi", "b.py"])

        names = _names(compared, checked, repo)

        # A stub beside its implementation is not parsed (#2445), nor is a
        # document.
        assert names == {"a.py": "proj.a", "b.py": "proj.b"}
        assert [claim.language for claim in compared.claims[0]] == [
            cs.SupportedLanguage.PYTHON,
            cs.SupportedLanguage.PYTHON,
        ]


def test_the_plan_is_python_s_on_cgr_s_own_checkout() -> None:
    # Every file of cgr's own checkout that a grammar reads, registered in
    # walk order as `process_file` registers it, without the parse.
    ignore = load_ignore_patterns(REPO_ROOT)
    exclude, unignore = ignore.exclude or None, ignore.unignore or None
    updater = _updater(REPO_ROOT, exclude_paths=exclude, unignore_paths=unignore)
    definitions = updater.factory.definition_processor
    plan = engine_frame.python_file_plan(REPO_ROOT, exclude, unignore)
    claims = []
    for index, file in enumerate(plan.files):
        path = engine_frame.file_path(file)
        match updater._route(path):
            case FileRoute(tier=cs.FileTier.TREE_SITTER, language=language) if (
                language is not None
            ):
                qn = definitions._disambiguate_module_qn(
                    base_module_qn(Path(file.rel_path), PROJECT), path
                )
                definitions.module_qn_to_file_path[qn] = path
                claims.append(engine_frame.ModuleClaim(index, file.rel_path, language))

    frame = engine_frame.frame_module_plan(
        REPO_ROOT, PROJECT, claims, exclude, unignore
    )

    assert frame is not None
    python = definitions.module_qn_to_file_path
    assert engine_frame.modules_difference(REPO_ROOT, python, frame) == []
    assert len(frame) > 1000
    assert f"{PROJECT}.codebase_rag" in {qn for qn, _path, _language in frame}


# --- choosing the answer --------------------------------------------------------


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frame on, as it will be in production, with no check in place."""
    monkeypatch.setattr(engine_frame, "ENABLED", True)
    monkeypatch.setattr(engine_frame, "CHECK", None)


def _plant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frame's plan, planted with a difference: each name in capitals."""
    original = engine_frame.frame_module_plan

    def planted(
        repo_path: Path,
        project_name: str,
        claims: Sequence[engine_frame.ModuleClaim],
        exclude: frozenset[str] | None = None,
        unignore: frozenset[str] | None = None,
    ) -> tuple[engine_frame.PlannedModule, ...] | None:
        found = original(repo_path, project_name, claims, exclude, unignore)
        assert found is not None
        return tuple((qn.upper(), path, language) for qn, path, language in found)

    monkeypatch.setattr(engine_frame, "frame_module_plan", planted)


def _warnings(run: Callable[[], object]) -> list[str]:
    warned: list[str] = []
    sink = logger.add(lambda message: warned.append(str(message)), level="WARNING")
    try:
        run()
    finally:
        logger.remove(sink)
    return warned


def _module_qns(updater: GraphUpdater) -> list[str]:
    return sorted(updater.factory.definition_processor.module_qn_to_file_path)


class TestChoosing:
    def test_off_and_unchecked_the_frame_is_never_asked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(engine_frame, "ENABLED", False)
        monkeypatch.setattr(engine_frame, "CHECK", None)

        def refusing(*_args: object) -> None:
            raise AssertionError("the frame was asked")

        monkeypatch.setattr(engine_frame, "frame_module_plan", refusing)
        monkeypatch.setattr(engine_frame, "modules_for_run", refusing)
        updater = _updater(_tree(tmp_path / "repo", ["a.py"]))

        updater.run()

        assert _module_qns(updater) == ["proj.a"]

    def test_on_a_difference_one_warning_names_it_and_python_s_map_is_kept(
        self, tmp_path: Path, production: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py", "b.py"])
        _plant(monkeypatch)
        updater = _updater(repo)

        warned = _warnings(updater.run)

        differs = [line for line in warned if "module names" in line]
        assert len(differs) == 1
        assert str(repo) in differs[0]
        assert '- module\tproj.a\t{"language":"python","path":"a.py"}' in differs[0]
        assert _module_qns(updater) == ["proj.a", "proj.b"]

    def test_a_frame_that_raises_is_a_difference(
        self, tmp_path: Path, production: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raising(*_args: object) -> None:
            raise ValueError("refused")

        monkeypatch.setattr(engine_frame, "frame_module_plan", raising)
        updater = _updater(_tree(tmp_path / "repo", ["a.py"]))

        warned = _warnings(updater.run)

        assert sum("raised\trefused" in line for line in warned) == 1
        assert _module_qns(updater) == ["proj.a"]

    @pytest.mark.parametrize("enabled", [False, True])
    def test_a_check_is_told_of_the_difference_and_python_s_map_is_kept(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        checked: _Recorded,
        enabled: bool,
    ) -> None:
        monkeypatch.setattr(engine_frame, "ENABLED", enabled)
        _plant(monkeypatch)
        updater = _updater(_tree(tmp_path / "repo", ["a.py", "b.py"]))

        updater.run()

        (difference,) = checked.modules
        assert [line[:2] for line in difference] == ["- ", "- ", "+ ", "+ "]
        assert difference[2] == (
            '+ module\tPROJ.A\t{"language":"python","path":"a.py"}'
        )
        # Even with the frame on, the module names stay Python's in phase 2.
        assert _module_qns(updater) == ["proj.a", "proj.b"]

    def test_a_check_that_cannot_compare_leaves_the_frame_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checked: _Recorded
    ) -> None:
        monkeypatch.setattr(checked, "compares_modules", lambda: False)
        _plant(monkeypatch)

        _updater(_tree(tmp_path / "repo", ["a.py"])).run()

        assert checked.modules == []


def _shadowed_test_files() -> list[str]:
    return sorted(
        path.name
        for path in TESTS.glob("test_*.py")
        if path.name != Path(__file__).name
        and SHADOW_MARKERS.search(path.read_text(encoding=cs.ENCODING_UTF8))
    )


def test_every_existing_hand_off_and_naming_test_agrees_with_the_frame(
    tmp_path: Path,
) -> None:
    # The existing tests that write the hand-offs, read the module map or
    # name a module by one of its rules, with every module map compared
    # with the frame's plan; any difference fails the test that made it. On
    # two workers of their own: each writes what it compared, added up here.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_XDIST", "PYTEST_CURRENT_TEST"))
    }
    env[MODULE_SHADOW_DIR_ENV] = str(tmp_path)
    files = _shadowed_test_files()
    assert len(files) > 30, files
    assert "test_handoff.py" in files
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-n",
            "2",
            "-p",
            SHADOW_PLUGIN,
            *(str(TESTS / name) for name in files),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=3600,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-8000:]
    reports = [
        json.loads(path.read_text(encoding=cs.ENCODING_UTF8))
        for path in tmp_path.glob("*.json")
    ]
    assert len(reports) == 3, output[-4000:]
    differences = [line for report in reports for line in report["differences"]]
    assert differences == [], differences[:20]
    total = {key: sum(report[key] for report in reports) for key in ("runs", "modules")}
    assert total["runs"] > 300, total
    assert total["modules"] > 1000, total
