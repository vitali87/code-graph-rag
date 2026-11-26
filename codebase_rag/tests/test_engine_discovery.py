"""The frame's walk and file plan against Python's walk.

Phase 2's step 5 (docs/reports/PHASE2_FRAME.md, sections 4, 5 and 13) moves
the repository walk to Rust. The frame's file plan must be the one
`walk_eligible_files` gives: the files in walk order, each with the md5 the
sync records, the folders listed, and the entries reported as links, special
files and names that are not UTF-8. Each corner of the walk's order and of
its rules is checked here against Python's own walk, as are its reading of
the ignore files and the run's fallback to Python's plan on a difference.
The existing walk and ignore tests run once more with every walk and every
reading of the ignore files compared (`discovery_shadow.py`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import engine_frame
from codebase_rag.checkout_state import state_file
from codebase_rag.config import load_ignore_patterns
from codebase_rag.tests.conftest import _MockIngestor, create_and_run_updater
from codebase_rag.tests.shadows import child_env
from codebase_rag.tests.test_symlink_policy import (
    _link,
    _make_undecodable,
    _write,
    needs_fifo,
)
from codebase_rag.utils.path_utils import readable_name

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent
SHADOW_PLUGIN = "codebase_rag.tests.discovery_shadow"
# The tests whose walks and ignore files are compared once more.
SHADOWED_TEST_FILES = (
    "test_symlink_policy.py",
    "test_ignored_file_suffixes.py",
    "test_qn_walk_order_parity.py",
    "test_realtime_walk_parity.py",
    "test_interactive_setup_persisted.py",
    "test_gitignore_patterns.py",
    "test_gitignore_negation.py",
    "test_gitignore_ignore_semantics.py",
    "test_cgrignore.py",
    "test_exclude_patterns.py",
    "test_tracked_source_dirs.py",
)
needs_permissions = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="POSIX permissions, which root reads past",
)


def _frame(
    repo: Path,
    exclude: frozenset[str] | None = None,
    unignore: frozenset[str] | None = None,
) -> engine_frame.FilePlan:
    plan = engine_frame.frame_file_plan(repo, exclude, unignore)
    # The checkout builds its own crate, so the walk is always there.
    assert plan is not None, "the extension lacks the walk; rebuild the crate"
    return plan


def _same_plan(
    repo: Path,
    exclude: frozenset[str] | None = None,
    unignore: frozenset[str] | None = None,
) -> engine_frame.FilePlan:
    """The frame's plan, once it is Python's: the files, folders and hashes
    in the same order, and the same entries reported."""
    python = engine_frame.hashed(engine_frame.python_file_plan(repo, exclude, unignore))
    frame = _frame(repo, exclude, unignore)
    assert engine_frame.plan_difference(python, frame) == []
    assert (frame.files, frame.dirs, frame.hashes) == (
        python.files,
        python.dirs,
        python.hashes,
    )
    return frame


def _rel_paths(plan: engine_frame.FilePlan) -> list[str]:
    return [file.rel_path for file in plan.files]


def _keys(plan: engine_frame.FilePlan) -> list[str]:
    return [visited.key for visited in plan.dirs]


def _tree(root: Path, rel_paths: Sequence[str]) -> Path:
    for rel in rel_paths:
        _write(root / rel, f"# {rel}\n")
    return root


class TestOrder:
    def test_each_folder_gives_its_files_then_its_subfolders_by_code_point(
        self, tmp_path: Path
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            [
                "a-b/x.py",
                "a/b.py",
                "a.b",
                "a_b",
                "B.py",
                "a.py",
                "é.py",
                "z.py",
                "中文/文件.py",
                "🎉.py",
                "Z/y.py",
                "a/c/d/e/f/g.py",
                "a/c/d/e/f/h/i.py",
            ],
        )

        plan = _same_plan(repo)

        # The root's files first; `a/b.py` before `a-b/x.py`, though sorted
        # paths put it after; upper case before lower; then by code point.
        assert _rel_paths(plan) == [
            "B.py",
            "a.b",
            "a.py",
            "a_b",
            "z.py",
            "é.py",
            "🎉.py",
            "Z/y.py",
            "a/b.py",
            "a/c/d/e/f/g.py",
            "a/c/d/e/f/h/i.py",
            "a-b/x.py",
            "中文/文件.py",
        ]
        assert _keys(plan)[:3] == [cs.ROOT_DIR_KEY, "Z", "a"]
        assert plan.dirs[0].dirpath == str(repo)

    def test_a_deep_tree_is_walked_to_its_end(self, tmp_path: Path) -> None:
        deep = "/".join(f"d{i}" for i in range(60))
        repo = _tree(tmp_path / "repo", [f"{deep}/leaf.py", "top.py"])

        plan = _same_plan(repo)

        assert _rel_paths(plan) == ["top.py", f"{deep}/leaf.py"]
        assert len(plan.dirs) == 61

    @pytest.mark.parametrize("threads", [1, 2, 0])
    def test_any_number_of_threads_gives_the_same_plan(
        self, threads: int, tmp_path: Path
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            [f"p{i:02}/q{j}/m{(i * j) % 5}.py" for i in range(30) for j in range(4)],
        )
        made = engine_frame.new_engine(threads)
        assert made is not None

        files, dirs, *_reports, hashes = made.file_plan(repo, None, None)

        frame = _same_plan(repo)
        assert [engine_frame.WalkedFile(*file) for file in files] == list(frame.files)
        assert [engine_frame.VisitedDir(*visited) for visited in dirs] == list(
            frame.dirs
        )
        assert tuple(hashes) == frame.hashes


class TestRules:
    def test_cgr_s_state_files_are_dropped_at_any_depth(self, tmp_path: Path) -> None:
        names = sorted(cs.CGR_STATE_FILENAMES)
        repo = _tree(
            tmp_path / "repo",
            [*names, *(f"pkg/deeper/{name}" for name in names), "pkg/kept.py"],
        )

        assert _rel_paths(_same_plan(repo)) == ["pkg/kept.py"]

    @pytest.mark.parametrize(
        "unignore",
        [
            None,
            frozenset({"sub/.git/config", "/.hg/hgrc"}),
            frozenset({".svn/"}),
            frozenset({"*.[Cc]ache/"}),
        ],
    )
    def test_version_control_folders_are_kept_only_for_a_line_through_them(
        self, tmp_path: Path, unignore: frozenset[str] | None
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            [
                "a.py",
                ".git/HEAD",
                ".hg/hgrc",
                ".svn/entries",
                "sub/.git/config",
                "sub/.git/objects/x",
                "sub/b.py",
            ],
        )

        plan = _same_plan(repo, unignore=unignore)

        if unignore is None:
            assert _rel_paths(plan) == ["a.py", "sub/b.py"]

    @pytest.mark.parametrize(
        "unignore",
        [
            None,
            frozenset({"vendor/keep/**"}),
            frozenset({"/target/generated/**"}),
            frozenset({"*.go"}),
            frozenset({"node_modules/pkg/index.js"}),
        ],
    )
    def test_built_in_folders_are_kept_for_src_bin_or_an_unignore_inside(
        self, tmp_path: Path, unignore: frozenset[str] | None
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            [
                "main.py",
                "src/bin/tool.rs",
                "bin/run.sh",
                "node_modules/pkg/index.js",
                "vendor/keep/a.go",
                "vendor/drop/b.go",
                "target/generated/G.java",
                "build/out.js",
                "__pycache__/m.cpython-313.pyc",
            ],
        )

        plan = _same_plan(repo, unignore=unignore)

        if unignore is None:
            assert _rel_paths(plan) == ["main.py", "src/bin/tool.rs"]

    @pytest.mark.parametrize(
        "unignore",
        [None, frozenset({"lib/jquery.min.js"}), frozenset({"lib/"})],
    )
    def test_generated_endings_are_skipped_unless_a_line_names_the_file(
        self, tmp_path: Path, unignore: frozenset[str] | None
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            [
                "lib/jquery.min.js",
                "lib/site.min.css",
                "lib/app.js",
                "mod.pyc",
                "mod.pyo",
                "native.o",
                "native.so",
                "lib.a",
                "App.class",
                "notes.py~",
                "scratch.tmp",
            ],
        )

        plan = _same_plan(repo, unignore=unignore)

        expected = ["lib/app.js"]
        if unignore == frozenset({"lib/jquery.min.js"}):
            expected = ["lib/app.js", "lib/jquery.min.js"]
        assert _rel_paths(plan) == expected

    def test_exclude_values_and_both_ignore_files_combine_as_a_sync_combines_them(
        self, tmp_path: Path
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            [
                "app.py",
                "docs/guide.md",
                "gen/a.py",
                "gen/keep.py",
                "logs/x.log",
                "scripts/tool.py",
                "src/gen/b.py",
                "tmp/scratch.py",
                "out/main.go",
            ],
        )
        (repo / ".cgrignore").write_text("docs/\n!tmp/\n", encoding="utf-8")
        (repo / ".gitignore").write_text(
            "﻿*.log\ngen/*\n!gen/keep.py\n", encoding="utf-8"
        )
        ignore = load_ignore_patterns(repo)
        exclude = frozenset({"scripts"}) | ignore.exclude

        plan = _same_plan(repo, exclude, ignore.unignore or None)

        assert _rel_paths(plan) == [
            ".cgrignore",
            ".gitignore",
            "app.py",
            "gen/keep.py",
            "src/gen/b.py",
            "tmp/scratch.py",
        ]
        read = engine_frame.frame_ignore_patterns(repo)
        assert read is not None
        assert (read.exclude, read.unignore) == (ignore.exclude, ignore.unignore)

    @pytest.mark.parametrize("pattern", ["[z-a]", "bad\\", "!"])
    def test_a_pattern_python_refuses_is_refused(
        self, tmp_path: Path, pattern: str
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py"])

        patterns = frozenset({pattern})

        # pathspec refuses some patterns itself and leaves others to `re`.
        with pytest.raises((ValueError, re.error)):
            engine_frame.python_file_plan(repo, patterns)
        with pytest.raises(ValueError):
            _frame(repo, patterns)


class TestLinks:
    def test_links_are_reported_and_never_followed(self, tmp_path: Path) -> None:
        repo = _tree(tmp_path / "repo", ["real/a.py", "b.py", "pkg/c.py"])
        _tree(tmp_path / "outside", ["secret.py"])
        _link(repo / "file_link.py", "b.py")
        _link(repo / "dir_link", "real", directory=True)
        _link(repo / "pkg" / "out.py", "../../outside/secret.py")
        _link(repo / "loop", "loop")
        _link(repo / "dangling.py", "absent.py")
        # Judged as the folder it names, and pruned before it is reported.
        _link(repo / "node_modules", "real", directory=True)

        plan = _same_plan(repo)

        assert _rel_paths(plan) == ["b.py", "pkg/c.py", "real/a.py"]
        assert sorted(plan.symlinks) == [
            "dangling.py",
            "dir_link",
            "file_link.py",
            "loop",
            "pkg/out.py",
        ]

    def test_a_link_the_rules_leave_out_is_not_reported(self, tmp_path: Path) -> None:
        repo = _tree(tmp_path / "repo", ["a.py"])
        _link(repo / "skip.py", "a.py")

        plan = _same_plan(repo, exclude=frozenset({"skip.py"}))

        assert plan.symlinks == ()

    def test_a_root_that_is_a_link_is_followed(self, tmp_path: Path) -> None:
        _tree(tmp_path / "repo", ["a.py", "pkg/b.py"])
        root = tmp_path / "root_link"
        _link(root, "repo", directory=True)

        plan = _same_plan(root)

        assert _rel_paths(plan) == ["a.py", "pkg/b.py"]
        assert plan.dirs[1].dirpath == f"{root}{os.sep}pkg"


@needs_fifo
class TestSpecialFiles:
    def test_a_pipe_is_reported_unless_the_rules_skip_it(self, tmp_path: Path) -> None:
        repo = _tree(tmp_path / "repo", ["a.py"])
        os.mkfifo(repo / "pipe.py")
        os.mkfifo(repo / "skipped.log")
        (repo / "node_modules").mkdir()
        os.mkfifo(repo / "node_modules" / "pruned")

        plan = _same_plan(repo, exclude=frozenset({"*.log"}))

        assert _rel_paths(plan) == ["a.py"]
        assert plan.special == ("pipe.py",)


class TestUndecodableNames:
    def test_a_name_the_rules_skip_is_dropped_silently_and_others_reported(
        self, tmp_path: Path
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py"])
        _make_undecodable(repo, b"bad\xff.py", "x = 1\n")
        _make_undecodable(repo, b"bad\xff.log", "log\n")
        _make_undecodable(repo, b"named\xfe.py", "x = 2\n")
        folder = _make_undecodable(repo, b"dir\xff")
        _write(folder / "c.py", "x = 3\n")
        pruned = _make_undecodable(repo, b"gen\xfe")
        _write(pruned / "d.py", "x = 4\n")
        named = _make_undecodable(repo, b"drop\xfd")
        _write(named / "e.py", "x = 5\n")

        # `?` takes the byte as one character, and a pattern may hold the
        # byte's surrogate itself, as Python reads it.
        plan = _same_plan(
            repo,
            exclude=frozenset({"*.log", "gen?/", "named\udcfe.py", "drop\udcfd/"}),
        )

        assert _rel_paths(plan) == ["a.py"]
        assert sorted(readable_name(rel) for rel in plan.undecodable) == [
            "bad\\xff.py",
            "dir\\xff",
        ]
        assert sorted(plan.undecodable) == sorted(
            os.fsdecode(name) for name in (b"bad\xff.py", b"dir\xff")
        )


class TestUnreadableFolders:
    @needs_permissions
    def test_a_folder_that_cannot_be_listed_is_skipped_whole(
        self, tmp_path: Path
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py", "locked/b.py", "open/c.py"])
        locked = repo / "locked"
        locked.chmod(0)
        try:
            plan = _same_plan(repo)
        finally:
            locked.chmod(0o755)

        assert _rel_paths(plan) == ["a.py", "open/c.py"]
        assert _keys(plan) == [cs.ROOT_DIR_KEY, "open"]

    @needs_permissions
    def test_a_file_that_cannot_be_read_is_walked_without_a_hash(
        self, tmp_path: Path
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py", "secret.py"])
        secret = repo / "secret.py"
        secret.chmod(0)
        try:
            plan = _same_plan(repo)
        finally:
            secret.chmod(0o644)

        assert _rel_paths(plan) == ["a.py", "secret.py"]
        assert plan.hashes[1] is None


def test_the_plan_is_python_s_on_cgr_s_own_checkout() -> None:
    ignore = load_ignore_patterns(REPO_ROOT)

    plan = _same_plan(REPO_ROOT, ignore.exclude or None, ignore.unignore or None)

    assert len(plan.files) > 1000
    read = engine_frame.frame_ignore_patterns(REPO_ROOT)
    assert read is not None
    assert (read.exclude, read.unignore) == (ignore.exclude, ignore.unignore)


def test_the_hashes_are_the_ones_the_sync_records(tmp_path: Path) -> None:
    repo = _tree(tmp_path / "repo", ["pkg/__init__.py", "pkg/mod.py", "README.md"])
    _write(repo / "crlf.py", "x = 1\r\ny = 2\r\n")
    create_and_run_updater(repo, _MockIngestor())
    recorded = json.loads(
        state_file(repo, cs.HASH_CACHE_FILENAME).read_text(encoding=cs.ENCODING_UTF8)
    )

    plan = _same_plan(repo)

    assert dict(zip(_rel_paths(plan), plan.hashes, strict=True)) == recorded


def test_the_walk_holds_cgr_s_names() -> None:
    made = engine_frame.engine()
    assert made is not None
    engine_type = type(made)
    assert frozenset(getattr(engine_type, "STATE_FILENAMES")) == cs.CGR_STATE_FILENAMES
    assert getattr(engine_type, "ROOT_DIR_KEY") == cs.ROOT_DIR_KEY
    assert (
        frozenset(getattr(engine_type, "TRACKED_SOURCE_DIR_NAMES"))
        == cs.TRACKED_SOURCE_DIR_NAMES
    )
    assert getattr(engine_type, "GIT_LS_FILES_TIMEOUT_S") == cs.GIT_LS_FILES_TIMEOUT_S
    pattern_chars = {
        chr(code)
        for code in range(0x80)
        if cs.TRACKED_PATH_PATTERN_CHARS.search(chr(code))
    }
    assert set(getattr(engine_type, "TRACKED_PATH_PATTERN_CHARS")) == pattern_chars


class TestIgnoreFiles:
    def test_the_frame_reads_what_python_reads_and_warns_of(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".cgrignore").write_bytes(b"ok.py\nbad\\\n[z-a]\n!\n!!\n!\tkeep\n")
        (repo / ".gitignore").write_bytes(b"\xef\xbb\xbfa\r\nb/\r!b/x\n")
        warned: list[str] = []
        sink = logger.add(warned.append, level="WARNING")
        try:
            python = load_ignore_patterns(repo)
        finally:
            logger.remove(sink)

        read = engine_frame.frame_ignore_patterns(repo)

        assert read is not None
        assert (read.exclude, read.unignore) == (python.exclude, python.unignore)
        assert read.refused_lines == tuple(
            (".cgrignore", number) for number in (2, 3, 4, 5)
        )
        lines = [int(m[1]) for w in warned if (m := re.search(r"line (\d+) of", w))]
        assert lines == [2, 3, 4, 5]
        assert read.unreadable == ()

    def test_a_file_that_is_not_utf8_is_skipped_whole(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".gitignore").write_bytes(b"a.py\n\xff\n")

        read = engine_frame.frame_ignore_patterns(repo)

        assert read is not None
        assert read.unreadable == (".gitignore",)
        assert read.exclude == load_ignore_patterns(repo).exclude == frozenset()

    def test_tracked_source_is_rescued_as_python_rescues_it(
        self, tmp_path: Path
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            ["bin/main.dart", "tools/out/gen.go", "vendor/bin/x.js", "src/a.py"],
        )
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
        subprocess.run([*git, "add", "-A"], cwd=repo, check=True)

        read = engine_frame.frame_ignore_patterns(repo)

        assert read is not None
        assert read.unignore == load_ignore_patterns(repo).unignore
        assert read.unignore == {"/bin/main.dart", "/tools/out/gen.go"}
        assert read.tracked_dirs == ("bin", "tools/out")
        plan = _same_plan(repo, unignore=read.unignore)
        assert _rel_paths(plan) == ["bin/main.dart", "src/a.py", "tools/out/gen.go"]


def _plan(
    *files: str, hashes: tuple[str | None, ...] | None = None
) -> engine_frame.FilePlan:
    walked = tuple(
        engine_frame.WalkedFile("/r", rel.rsplit("/", 1)[-1], rel) for rel in files
    )
    return engine_frame.FilePlan(
        files=walked,
        dirs=(engine_frame.VisitedDir(cs.ROOT_DIR_KEY, "/r"),),
        symlinks=(),
        special=(),
        undecodable=(),
        hashes=hashes if hashes is not None else tuple("0" * 32 for _ in files),
    )


class TestComparison:
    def test_equal_plans_have_no_difference(self) -> None:
        assert engine_frame.plan_difference(_plan("a.py"), _plan("a.py")) == []

    def test_a_file_only_one_side_holds_is_named(self) -> None:
        difference = engine_frame.plan_difference(_plan("a.py", "b.py"), _plan("a.py"))

        assert difference == [f"- file\tb.py\t{'0' * 32}\t/r\tb.py"]

    def test_the_order_is_compared(self) -> None:
        difference = engine_frame.plan_difference(
            _plan("a.py", "b.py"), _plan("b.py", "a.py")
        )

        assert difference
        assert all(line.startswith(("-", "+")) for line in difference)

    def test_a_hash_is_compared_and_unreadable_is_spelled_as_the_cache_does(
        self,
    ) -> None:
        difference = engine_frame.plan_difference(
            _plan("a.py"), _plan("a.py", hashes=(None,))
        )

        assert difference == [
            f"- file\ta.py\t{'0' * 32}\t/r\ta.py",
            f"+ file\ta.py\t{cs.HASH_CACHE_UNREADABLE}\t/r\ta.py",
        ]

    def test_reports_are_compared_as_sets(self) -> None:
        one = _plan("a.py")._replace(symlinks=("x", "y"))
        other = _plan("a.py")._replace(symlinks=("y", "x"))

        assert engine_frame.plan_difference(one, other) == []
        assert engine_frame.plan_difference(one, _plan("a.py")) == [
            "- symlink\tx",
            "- symlink\ty",
        ]

    # Plans alike are told without spelling their lines; each field a plan
    # holds still differs when it alone does.
    def test_a_file_s_folder_alone_is_compared(self) -> None:
        moved = _plan("a.py")._replace(
            files=(engine_frame.WalkedFile("/s", "a.py", "a.py"),)
        )

        assert engine_frame.plan_difference(_plan("a.py"), moved) == [
            f"- file\ta.py\t{'0' * 32}\t/r\ta.py",
            f"+ file\ta.py\t{'0' * 32}\t/s\ta.py",
        ]

    def test_a_folder_alone_is_compared(self) -> None:
        listed = _plan("a.py")._replace(
            dirs=(
                engine_frame.VisitedDir(cs.ROOT_DIR_KEY, "/r"),
                engine_frame.VisitedDir("sub", "/r/sub"),
            )
        )

        assert engine_frame.plan_difference(_plan("a.py"), listed) == [
            "+ dir\tsub\t/r/sub"
        ]

    @pytest.mark.parametrize("report", ["special", "undecodable"])
    def test_each_report_alone_is_compared(self, report: str) -> None:
        reported = _plan("a.py")._replace(**{report: ("x",)})

        assert engine_frame.plan_difference(reported, _plan("a.py")) == [
            f"- {report}\tx"
        ]

    def test_a_plan_with_a_file_and_no_hash_is_refused_as_before(self) -> None:
        unhashed = _plan("a.py", hashes=())

        with pytest.raises(ValueError):
            engine_frame.plan_difference(unhashed, unhashed)


class _Recorded:
    """A `PlanCheck` that keeps what it is told."""

    def __init__(self, compares: bool = True) -> None:
        self._compares = compares
        self.differences: list[tuple[Path, list[str]]] = []

    def compares(self) -> bool:
        return self._compares

    def differs(self, repo_path: Path, difference: Sequence[str]) -> None:
        self.differences.append((repo_path, list(difference)))


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frame on, as it will be in production, with no check in place:
    the `rust` and `both` cells put one in for their whole session."""
    monkeypatch.setattr(engine_frame, "ENABLED", True)
    monkeypatch.setattr(engine_frame, "CHECK", None)


def _dropping(monkeypatch: pytest.MonkeyPatch, rel_path: str) -> None:
    """The frame's walk, planted with a difference: `rel_path` left out."""
    original = engine_frame.frame_file_plan

    def planted(
        repo_path: Path,
        exclude: frozenset[str] | None = None,
        unignore: frozenset[str] | None = None,
    ) -> engine_frame.FilePlan | None:
        plan = original(repo_path, exclude, unignore)
        assert plan is not None
        kept = [i for i, file in enumerate(plan.files) if file.rel_path != rel_path]
        return plan._replace(
            files=tuple(plan.files[i] for i in kept),
            hashes=tuple(plan.hashes[i] for i in kept),
        )

    monkeypatch.setattr(engine_frame, "frame_file_plan", planted)


def _refusing(*_args: object) -> engine_frame.FilePlan | None:
    raise AssertionError("the frame was asked")


class TestPlanForRun:
    def test_off_and_unchecked_the_frame_is_never_asked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py"])
        monkeypatch.setattr(engine_frame, "ENABLED", False)
        monkeypatch.setattr(engine_frame, "CHECK", None)
        monkeypatch.setattr(engine_frame, "frame_file_plan", _refusing)
        python = engine_frame.python_file_plan(repo)

        assert engine_frame.plan_for_run(repo, None, None, python) is python

    def test_on_and_equal_the_run_uses_the_frame_s_plan(
        self, tmp_path: Path, production: None
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py"])
        python = engine_frame.python_file_plan(repo)

        used = engine_frame.plan_for_run(repo, None, None, python)

        assert used is not python
        assert used == engine_frame.hashed(python)

    def test_on_a_difference_one_warning_names_it_and_python_s_plan_is_used(
        self,
        tmp_path: Path,
        production: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py", "b.py"])
        _dropping(monkeypatch, "b.py")
        python = engine_frame.python_file_plan(repo)
        warned: list[str] = []
        sink = logger.add(warned.append, level="WARNING")
        try:
            used = engine_frame.plan_for_run(repo, None, None, python)
        finally:
            logger.remove(sink)

        assert used is python
        assert len(warned) == 1
        assert str(repo) in warned[0]
        assert "- file\tb.py\t" in warned[0]

    def test_a_frame_that_raises_falls_back_too(
        self,
        tmp_path: Path,
        production: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py"])

        def raising(*_args: object) -> engine_frame.FilePlan:
            raise ValueError("refused")

        monkeypatch.setattr(engine_frame, "frame_file_plan", raising)
        python = engine_frame.python_file_plan(repo)
        warned: list[str] = []
        sink = logger.add(warned.append, level="WARNING")
        try:
            used = engine_frame.plan_for_run(repo, None, None, python)
        finally:
            logger.remove(sink)

        assert used is python
        assert len(warned) == 1
        assert "raised\trefused" in warned[0]

    @pytest.mark.parametrize("enabled", [False, True])
    def test_a_check_is_told_of_the_difference_instead(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        enabled: bool,
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py", "b.py"])
        check = _Recorded()
        monkeypatch.setattr(engine_frame, "ENABLED", enabled)
        monkeypatch.setattr(engine_frame, "CHECK", check)
        _dropping(monkeypatch, "b.py")
        python = engine_frame.python_file_plan(repo)

        used = engine_frame.plan_for_run(repo, None, None, python)

        assert [(path, len(lines)) for path, lines in check.differences] == [(repo, 1)]
        assert check.differences[0][1][0].startswith("- file\tb.py\t")
        # Strict: with the frame on, the run goes on with the frame's plan.
        assert _rel_paths(used) == (["a.py"] if enabled else ["a.py", "b.py"])

    def test_a_check_that_cannot_compare_leaves_python_s_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py"])
        monkeypatch.setattr(engine_frame, "ENABLED", True)
        monkeypatch.setattr(engine_frame, "CHECK", _Recorded(compares=False))
        monkeypatch.setattr(engine_frame, "frame_file_plan", _refusing)
        python = engine_frame.python_file_plan(repo)

        assert engine_frame.plan_for_run(repo, None, None, python) is python

    def test_a_run_falls_back_to_python_s_plan_and_indexes_every_file(
        self,
        tmp_path: Path,
        production: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py", "b.py"])
        _dropping(monkeypatch, "b.py")
        ingestor = _MockIngestor()
        warned: list[str] = []
        sink = logger.add(warned.append, level="WARNING")
        try:
            create_and_run_updater(repo, ingestor)
        finally:
            logger.remove(sink)

        recorded = json.loads(
            state_file(repo, cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
        )
        assert sorted(recorded) == ["a.py", "b.py"]
        differs = [line for line in warned if "file plan" in line]
        assert len(differs) == 1
        assert "b.py" in differs[0]

    def test_a_run_uses_the_frame_s_plan_when_the_two_agree(
        self,
        tmp_path: Path,
        production: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a.py", "pkg/b.py"])
        used: list[engine_frame.FilePlan] = []
        original = engine_frame.plan_for_run

        def recording(
            repo_path: Path,
            exclude: frozenset[str] | None,
            unignore: frozenset[str] | None,
            python: engine_frame.FilePlan,
        ) -> engine_frame.FilePlan:
            plan = original(repo_path, exclude, unignore, python)
            used.append(plan)
            return plan

        monkeypatch.setattr(engine_frame, "plan_for_run", recording)

        create_and_run_updater(repo, _MockIngestor())

        assert len(used) == 1
        assert _rel_paths(used[0]) == ["a.py", "pkg/b.py"]
        assert all(md5 is not None for md5 in used[0].hashes)


def test_every_existing_walk_and_ignore_test_agrees_with_the_frame() -> None:
    # The existing walk and ignore tests, with every walk they make and
    # every reading of the ignore files compared with the frame's; any
    # difference fails the test that made it.
    env = child_env()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            SHADOW_PLUGIN,
            *(str(TESTS / name) for name in SHADOWED_TEST_FILES),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=1800,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-8000:]
    summary = re.search(
        r"discovery shadow: (\d+) walks and (\d+) ignore readings compared, "
        r"(\d+) differed",
        output,
    )
    assert summary is not None, output[-4000:]
    walks, readings, differed = (int(group) for group in summary.groups())
    assert differed == 0, output[-8000:]
    # Well over a hundred walks even on a base install, which skips the
    # tests of every grammar it lacks (182 there, over 300 with them all).
    assert walks > 100, output[-4000:]
    assert readings > 20, output[-4000:]
