"""The frame's structure pass and File rows against Python's.

Phase 2's step 7 (docs/reports/PHASE2_FRAME.md, sections 7 and 13) moves
the structure pass to Rust: the Package and Folder rows, the
`structural_elements` map that the rest of the index reads a folder's
package from, and each indexed file's File row. Python's structure
processor still writes them, at the point it always has. The frame must
build Python's rows and map exactly: the folders in Python's path order,
packages by the language specs' markers taken literally, no Folder for the
root, and every property in Python's order. Each corner is held here to
Python's own, as is the run's fallback to Python's rows on a difference.
The existing structure tests run once more with every structure and every
File row compared (`structure_shadow.py`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from unittest.mock import MagicMock

import pytest
from loguru import logger
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag import engine_frame
from codebase_rag.config import load_ignore_patterns
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.structure_processor import StructureProcessor
from codebase_rag.tests.conftest import _MockIngestor, create_and_run_updater
from codebase_rag.tests.engine_modes import STRUCTURE_SHADOW_DIR_ENV
from codebase_rag.tests.shadows import child_env
from codebase_rag.tests.test_symlink_policy import (
    _junction,
    _link,
    _make_undecodable,
    _write,
    needs_junctions,
)
from codebase_rag.utils.path_utils import cached_resolve_posix

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent
SHADOW_PLUGIN = "codebase_rag.tests.structure_shadow"
# The tests that rerun under the shadow: every test file that reads the
# structure map, calls the structure pass or asserts on its rows.
SHADOW_MARKERS = re.compile(
    r"structural_elements|identify_structure|process_generic_file"
    r"|StructureProcessor|CONTAINS_FOLDER|CONTAINS_PACKAGE|CONTAINS_FILE"
    r"|NodeLabel\.(FOLDER|PACKAGE)\b"
)
PROJECT = "proj"
# Folders whose order Python's paths decide in corners: `/` against
# punctuation, case, an accent composed and decomposed, other scripts.
ORDER_CORNERS = (
    "a-b/x",
    "a/b/c",
    "a.b",
    "B",
    "a_b",
    "e\u0301",
    "\u00e9",
    "z",
    "\u4e2d\u6587",
    "\U0001f389",
    "\u00c4",
    "\u00e4",
)
# Their order, with every folder on their paths, where names compare exactly.
EXACT_ORDER = [
    ".",
    "B",
    "a",
    "a/b",
    "a/b/c",
    "a-b",
    "a-b/x",
    "a.b",
    "a_b",
    "e\u0301",
    "z",
    "\u00c4",
    "\u00e4",
    "\u00e9",
    "\u4e2d\u6587",
    "\U0001f389",
]
needs_links = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")


def _tree(root: Path, rel_paths: Sequence[str]) -> Path:
    """Each path a file, or a folder when it ends in `/`."""
    root.mkdir(parents=True, exist_ok=True)
    for rel in rel_paths:
        if rel.endswith("/"):
            (root / rel).mkdir(parents=True, exist_ok=True)
        else:
            _write(root / rel, f"# {rel}\n")
    return root


def _processor(
    repo: Path,
    exclude: frozenset[str] | None = None,
    unignore: frozenset[str] | None = None,
) -> StructureProcessor:
    return StructureProcessor(
        ingestor=_MockIngestor(),
        repo_path=repo,
        project_name=PROJECT,
        queries={},
        exclude_paths=exclude,
        unignore_paths=unignore,
    )


class _Recorded:
    """A `PlanCheck` that compares every structure and keeps what it is told."""

    def __init__(self) -> None:
        self.compared = 0
        self.structures: list[list[str]] = []

    def compares(self) -> bool:
        return True

    def differs(self, repo_path: Path, difference: Sequence[str]) -> None:
        raise AssertionError(list(difference))

    def compares_changes(self) -> bool:
        return False

    def changes_differ(self, repo_path: Path, difference: Sequence[str]) -> None:
        raise AssertionError(list(difference))

    def compares_structure(self) -> bool:
        self.compared += 1
        return True

    def structure_differs(self, repo_path: Path, difference: Sequence[str]) -> None:
        self.structures.append(list(difference))

    def compares_modules(self) -> bool:
        return False

    def modules_differ(self, repo_path: Path, difference: Sequence[str]) -> None:
        raise AssertionError(list(difference))


@pytest.fixture
def checked(monkeypatch: pytest.MonkeyPatch) -> _Recorded:
    """Every structure compared, the frame off, any difference kept."""
    check = _Recorded()
    monkeypatch.setattr(engine_frame, "ENABLED", False)
    monkeypatch.setattr(engine_frame, "CHECK", check)
    return check


type Pair = tuple[engine_frame.Structure, engine_frame.Structure | None]


@pytest.fixture
def pairs(checked: _Recorded, monkeypatch: pytest.MonkeyPatch) -> list[Pair]:
    """Each whole derivation's structures, Python's and the frame's."""
    found: list[Pair] = []
    original = engine_frame.structure_for_run

    def recording(
        repo_path: Path,
        project_name: str,
        markers: set[str],
        exclude: frozenset[str] | None,
        unignore: frozenset[str] | None,
        python: engine_frame.Structure,
    ) -> tuple[engine_frame.Structure, engine_frame.Structure | None]:
        used, frame = original(
            repo_path, project_name, markers, exclude, unignore, python
        )
        found.append((python, frame))
        return used, frame

    monkeypatch.setattr(engine_frame, "structure_for_run", recording)
    return found


def _same_structure(
    pairs: list[Pair],
    checked: _Recorded,
    repo: Path,
    exclude: frozenset[str] | None = None,
    unignore: frozenset[str] | None = None,
) -> engine_frame.Structure:
    """The frame's structure of `repo`, once it is Python's to the line."""
    _processor(repo, exclude, unignore).identify_structure()
    ((python, frame),) = pairs[-1:]
    assert frame is not None, "the extension lacks the structure; rebuild the crate"
    assert checked.structures == []
    assert frame == python
    return frame


# The code points a name of one character is tried with: past ASCII, to
# the end of the second plane.
SINGLE_CHARACTER_NAMES = (0x80, 0x20000)
SURROGATES = range(0xD800, 0xE000)
PRIVATE_USE = range(0xE000, 0xF900)


def _nameable(code: int) -> bool:
    return code not in SURROGATES and code not in PRIVATE_USE


def _folders_on_disk(repo: Path) -> list[str]:
    return [
        path.relative_to(repo).as_posix() for path in repo.rglob("*") if path.is_dir()
    ]


def _keys(structure: engine_frame.Structure) -> list[str]:
    return [key for key, _qn in structure.elements]


def _packages(structure: engine_frame.Structure) -> dict[str, str]:
    return {key: qn for key, qn in structure.elements if qn is not None}


def test_the_frame_holds_the_schema_s_names() -> None:
    made = engine_frame.engine()
    assert made is not None
    engine_type = type(made)
    assert getattr(engine_type, "NODE_LABELS") == [
        cs.NodeLabel.PROJECT,
        cs.NodeLabel.PACKAGE,
        cs.NodeLabel.FOLDER,
        cs.NodeLabel.FILE,
    ]
    assert getattr(engine_type, "PROPERTY_KEYS") == [
        cs.KEY_NAME,
        cs.KEY_QUALIFIED_NAME,
        cs.KEY_PATH,
        cs.KEY_ABSOLUTE_PATH,
        cs.KEY_EXTENSION,
    ]
    assert getattr(engine_type, "RELATIONSHIP_TYPES") == [
        cs.RelationshipType.CONTAINS_PACKAGE,
        cs.RelationshipType.CONTAINS_FOLDER,
        cs.RelationshipType.CONTAINS_FILE,
    ]


class TestOrder:
    def test_folders_come_in_this_python_s_path_order(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", [f"{name}/" for name in ORDER_CORNERS])
        # Windows and macOS keep one folder of names that differ only by
        # case or by the form of an accent.
        on_disk = _folders_on_disk(repo)

        found = _same_structure(pairs, checked, repo)

        assert _keys(found) == [".", *sorted(on_disk, key=lambda rel: repo / rel)]

    @pytest.mark.skipif(
        not isinstance(PurePath(), PurePosixPath),
        reason="Windows paths compare without case",
    )
    def test_where_names_compare_exactly_the_order_is_part_by_part_by_code_point(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", [f"{name}/" for name in ORDER_CORNERS])
        if sorted(_folders_on_disk(repo)) != sorted(EXACT_ORDER[1:]):
            pytest.skip("this file system folds names that differ by case or accent")

        found = _same_structure(pairs, checked, repo)

        # `a/b` before `a-b`, though `a-b` sorts first as a string; upper
        # case before lower; then by code point, a composed `é` after the
        # decomposed one; the root first, as `.`.
        assert _keys(found) == EXACT_ORDER

    @pytest.mark.parametrize("root", ["/repo", "."])
    @pytest.mark.parametrize("flavour", [PurePosixPath, PureWindowsPath])
    def test_folders_sort_as_each_flavour_of_python_path_sorts_them(
        self, flavour: type[PurePath], root: str
    ) -> None:
        # A name of each character past ASCII in the first two planes,
        # private use aside, and each `str.lower` changes inside a longer
        # one: Rust's Unicode is newer than Python's, and lowers letters
        # Python's leaves alone (Rust's own lowering misplaces about 40,000
        # of these names), and a sigma lowers by its place in a word.
        table = engine_frame.python_case_table()
        letters = [char for pair in table for char in pair]
        keys = [
            ".",
            *ORDER_CORNERS,
            *(chr(code) for code in range(*SINGLE_CHARACTER_NAMES) if _nameable(code)),
            *(f"a{char}b" for char in letters),
            *(f"x/{char}" for char in letters[::7]),
            "\u039f\u0394\u039f\u03a3",
            "\u039f\u0394\u039f\u03a3.d",
            "\u03bf\u03b4\u03bf\u03c2",
            "\u03bf\u03b4\u03bf\u03c3",
            "\u03a3\u0391",
            "-x",
        ]
        windows = flavour is PureWindowsPath

        order = engine_frame.frame_folder_order(
            Path(root), keys, table if windows else None
        )

        assert order == sorted(keys, key=lambda key: flavour(root) / key)

    def test_the_root_gets_no_folder_row_unless_it_is_a_package(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        plain = _tree(tmp_path / "plain", ["a.txt", "sub/"])
        package = _tree(tmp_path / "package", ["__init__.py", "sub/"])

        rows = [_same_structure(pairs, checked, repo).rows for repo in (plain, package)]

        assert [row.node.label for row in rows[0]] == [cs.NodeLabel.FOLDER]
        assert [row.node.label for row in rows[1]] == [
            cs.NodeLabel.PACKAGE,
            cs.NodeLabel.FOLDER,
        ]
        root = dict(rows[1][0].node.properties)
        assert (root[cs.KEY_QUALIFIED_NAME], root[cs.KEY_PATH]) == (PROJECT, ".")
        assert rows[1][0].relationship.start == (
            cs.NodeLabel.PROJECT,
            cs.KEY_NAME,
            PROJECT,
        )

    def test_folders_with_no_file_to_index_are_folders(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        repo = _tree(
            tmp_path / "repo", ["empty/", "compiled/a.pyc", "deep/er/est/", "a.py"]
        )

        found = _same_structure(pairs, checked, repo)

        assert _keys(found) == [
            ".",
            "compiled",
            "deep",
            "deep/er",
            "deep/er/est",
            "empty",
        ]

    @pytest.mark.parametrize("root", [".", ""])
    def test_a_root_given_as_the_current_folder_sorts_as_its_name_does(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pairs: list[Pair],
        checked: _Recorded,
        root: str,
    ) -> None:
        repo = _tree(tmp_path / "repo", ["#t/", "-x/", "b/", "(old)/a.py"])
        monkeypatch.chdir(repo)
        # Python caches each folder's resolved path by the path as given,
        # which for a relative one is the last test's folder; the CLI hands
        # the updater resolved paths, so only a test meets this.
        cached_resolve_posix.cache_clear()

        found = _same_structure(pairs, checked, Path(root))

        # `Path(".")` compares as `.`, and its folders without it, so a
        # name below `.` comes first.
        assert _keys(found) == ["#t", "(old)", "-x", ".", "b"]

    def test_a_single_folder_repository(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        found = _same_structure(pairs, checked, _tree(tmp_path / "repo", ["a.py"]))

        assert found.elements == ((".", None),)
        assert found.rows == ()


class TestPackages:
    @pytest.mark.parametrize(
        "marker",
        sorted(
            {
                marker
                for spec in LANGUAGE_SPECS.values()
                for marker in spec.package_indicators
            }
        ),
    )
    def test_nested_packages_by_each_language_s_marker(
        self,
        tmp_path: Path,
        pairs: list[Pair],
        checked: _Recorded,
        marker: str,
    ) -> None:
        if sys.platform == "win32" and "*" in marker:
            pytest.skip("Windows refuses `*` in a name")
        repo = _tree(
            tmp_path / "repo",
            [f"outer/{marker}", f"outer/plain/inner/{marker}", "outer/plain/x.txt"],
        )

        found = _same_structure(pairs, checked, repo)

        assert _packages(found) == {
            "outer": f"{PROJECT}.outer",
            "outer/plain/inner": f"{PROJECT}.outer.plain.inner",
        }
        parents = [row.relationship.start for row in found.rows]
        assert parents == [
            (cs.NodeLabel.PROJECT, cs.KEY_NAME, PROJECT),
            (cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, f"{PROJECT}.outer"),
            (
                cs.NodeLabel.FOLDER,
                cs.KEY_ABSOLUTE_PATH,
                (repo / "outer" / "plain").resolve().as_posix(),
            ),
        ]

    def test_a_vcxproj_marker_is_a_literal_name_never_a_pattern(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        # Windows refuses `*` and `?` in a name, so only the names it allows
        # are made there; the literal marker is a name only elsewhere.
        names = {"app": "app.vcxproj", "bracketed": "[ab]c.vcxproj"}
        if sys.platform != "win32":
            names |= {
                "globbed": "[ab]?.vcxproj",
                "starred": "x*.vcxproj",
                "literal": "*.vcxproj",
            }
        repo = _tree(
            tmp_path / "repo", [f"{key}/{name}" for key, name in names.items()]
        )

        found = _same_structure(pairs, checked, repo)

        literal = {} if sys.platform == "win32" else {"literal": f"{PROJECT}.literal"}
        assert _packages(found) == literal

    def test_a_marker_that_is_a_folder_or_a_link_counts(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", ["folder/__init__.py/", "linked/"])
        if sys.platform != "win32":
            _link(repo / "linked" / "Cargo.toml", "../folder")

        found = _same_structure(pairs, checked, repo)

        assert "folder" in _packages(found)


class TestRules:
    def test_built_in_excluded_rescued_and_version_control_folders(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            [
                "node_modules/dep/__init__.py",
                "node_modules/kept/index.js",
                "node_modules/other/x.js",
                "build/out.py",
                "src/bin/tool.rs",
                ".git/objects/12/",
                "sub/.git/HEAD",
                "excluded/inner/x.py",
                "kept/x.py",
            ],
        )

        found = _same_structure(
            pairs,
            checked,
            repo,
            exclude=frozenset({"excluded/"}),
            unignore=frozenset({"node_modules/kept/index.js", "sub/.git"}),
        )

        # The rescue keeps the folders a `!` line spells a path through, as
        # the structure pass's own rules decide, not the walk's.
        assert _keys(found) == [
            ".",
            "kept",
            "node_modules",
            "node_modules/kept",
            "src",
            "src/bin",
            "sub",
            "sub/.git",
        ]

    @needs_links
    def test_a_linked_folder_is_left_out_and_never_entered(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", ["real/inner/__init__.py"])
        _link(repo / "alias", "real", directory=True)
        _link(repo / "real" / "loop", "..", directory=True)

        found = _same_structure(pairs, checked, repo)

        assert _keys(found) == [".", "real", "real/inner"]

    @needs_junctions
    def test_a_junction_is_a_folder_only_when_it_names_one_inside_the_root(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", ["real/inner/__init__.py"])
        outside = _tree(tmp_path / "outside", ["pkg/__init__.py"])
        gone = _tree(tmp_path / "gone", [])
        # Entered when it names a folder inside the root, and left out as a
        # link otherwise, judged by where it points (issue #3740).
        _junction(repo / "alias", repo / "real")
        # Not "out": that name is skipped as build output wherever it is.
        _junction(repo / "elsewhere", outside)
        _junction(repo / "dangling", gone)
        gone.rmdir()

        found = _same_structure(pairs, checked, repo)

        assert _keys(found) == [".", "alias", "alias/inner", "real", "real/inner"]

    @needs_junctions
    def test_a_directory_symlink_beside_a_junction_is_still_left_out(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", ["real/inner/__init__.py"])
        _junction(repo / "alias", repo / "real")
        _link(repo / "dir_link", "real", directory=True)

        found = _same_structure(pairs, checked, repo)

        assert _keys(found) == [".", "alias", "alias/inner", "real", "real/inner"]

    def test_a_folder_whose_name_is_not_utf8_is_left_out(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", ["a/"])
        bad = _make_undecodable(repo, b"bad\xff")
        _write(bad / "inner" / "__init__.py", "")

        found = _same_structure(pairs, checked, repo)

        assert _keys(found) == [".", "a"]

    @needs_links
    def test_a_root_reached_through_a_link_is_resolved(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        real = _tree(tmp_path / "real", ["pkg/__init__.py", "plain/"])
        _link(tmp_path / "alias", "real", directory=True)

        found = _same_structure(pairs, checked, tmp_path / "alias")

        absolute = {
            dict(row.node.properties)[cs.KEY_ABSOLUTE_PATH] for row in found.rows
        }
        assert absolute == {
            (real / "pkg").resolve().as_posix(),
            (real / "plain").resolve().as_posix(),
        }

    def test_a_pattern_python_refuses_is_refused(self, tmp_path: Path) -> None:
        repo = _tree(tmp_path / "repo", ["a/"])
        refused = frozenset({"[z-a]"})

        with pytest.raises(ValueError):
            engine_frame.frame_structure(repo, PROJECT, [], refused)


def _python_rows(
    processor: StructureProcessor, paths: Sequence[Path]
) -> tuple[engine_frame.ContainedRow, ...]:
    return tuple(processor.file_row(path, path.name) for path in paths)


def _frame_rows(
    processor: StructureProcessor, paths: Sequence[Path]
) -> tuple[engine_frame.ContainedRow | None, ...]:
    elements = [
        (key.as_posix(), qn) for key, qn in processor.structural_elements.items()
    ]
    rows = engine_frame.frame_file_rows(
        processor.repo_path, processor.project_name, elements, paths
    )
    assert rows is not None, "the extension lacks the File rows; rebuild the crate"
    return rows


class TestFileRows:
    def test_each_file_names_its_folder_s_package_or_folder_or_the_project(
        self, tmp_path: Path
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            ["top.yaml", "pkg/__init__.py", "pkg/data.json", "plain/deep/readme"],
        )
        processor = _processor(repo)
        processor.identify_structure()
        paths = [repo / "top.yaml", repo / "pkg" / "data.json"]
        paths.append(repo / "plain" / "deep" / "readme")

        rows = _frame_rows(processor, paths)

        assert rows == _python_rows(processor, paths)
        assert [row.relationship.start[0] for row in rows if row] == [
            cs.NodeLabel.PROJECT,
            cs.NodeLabel.PACKAGE,
            cs.NodeLabel.FOLDER,
        ]

    @pytest.mark.parametrize(
        "name",
        [
            "a.py",
            "a.tar.gz",
            "Makefile",
            ".bashrc",
            ".a.b",
            "..a",
            "a.",
            "a..",
            "a. b",
            "é.ü",
            "a b.c d",
        ],
    )
    def test_the_extension_is_this_python_s_suffix(
        self, tmp_path: Path, name: str
    ) -> None:
        if sys.platform == "win32" and name.endswith((".", " ")):
            pytest.skip("Windows trims a trailing dot or space")
        repo = _tree(tmp_path / "repo", [f"d/{name}"])
        processor = _processor(repo)
        processor.identify_structure()
        path = repo / "d" / name

        (row,) = _frame_rows(processor, [path])

        assert row == _python_rows(processor, [path])[0]
        assert row is not None
        assert dict(row.node.properties)[cs.KEY_EXTENSION] == PurePath(name).suffix

    @needs_links
    def test_a_linked_file_is_named_by_the_link_not_its_target(
        self, tmp_path: Path
    ) -> None:
        repo = _tree(tmp_path / "repo", ["real.md", "sub/"])
        _link(repo / "sub" / "alias.md", "../real.md")
        processor = _processor(repo)
        processor.identify_structure()
        path = repo / "sub" / "alias.md"

        rows = _frame_rows(processor, [path])

        assert rows == _python_rows(processor, [path])
        assert rows[0] is not None
        assert (
            rows[0].relationship.end[2]
            == (repo / "sub").resolve().joinpath("alias.md").as_posix()
        )

    def test_a_relative_root_and_a_path_outside_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tree(tmp_path / "repo", ["pkg/__init__.py", "pkg/a.py", "b.py"])
        _write(tmp_path / "elsewhere.py", "")
        monkeypatch.chdir(tmp_path / "repo")
        processor = _processor(Path("."))
        processor.identify_structure()
        paths = [Path("pkg/a.py"), Path("b.py")]

        rows = _frame_rows(processor, [*paths, tmp_path / "elsewhere.py"])

        assert rows == (*_python_rows(processor, paths), None)


def test_the_structure_and_rows_are_python_s_on_cgr_s_own_checkout(
    pairs: list[Pair], checked: _Recorded
) -> None:
    ignore = load_ignore_patterns(REPO_ROOT)
    exclude, unignore = ignore.exclude or None, ignore.unignore or None
    processor = _processor(REPO_ROOT, exclude, unignore)

    processor.identify_structure()
    plan = engine_frame.python_file_plan(REPO_ROOT, exclude, unignore)
    paths = [engine_frame.file_path(file) for file in plan.files]
    rows = _frame_rows(processor, paths)

    ((python, frame),) = pairs
    assert frame == python
    assert len(python.elements) > 100
    assert _packages(python)["codebase_rag"] == f"{PROJECT}.codebase_rag"
    assert len(paths) > 1000
    assert rows == _python_rows(processor, paths)


# --- a run ----------------------------------------------------------------------


def _updater(target: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(_MockIngestor(), target, parsers, queries, skip_embeddings=True)


_STRUCTURE_LABELS = (cs.NodeLabel.PACKAGE, cs.NodeLabel.FOLDER, cs.NodeLabel.FILE)
_STRUCTURE_EDGES = (
    cs.RelationshipType.CONTAINS_PACKAGE,
    cs.RelationshipType.CONTAINS_FOLDER,
    cs.RelationshipType.CONTAINS_FILE,
)
# Two repositories of one name in two folders: the same project, whose
# folders' resolved paths differ only by the folder above.
LAYOUT = ["pkg/__init__.py", "pkg/sub/mod.py", "plain/notes.md", "top.json"]


def _structure_writes(repo: Path) -> list[str]:
    """What a run of `repo` writes of the structure, in order, each call as
    its repr, which names a label's and an edge type's own type, with the
    repository's resolved path spelled `<repo>`."""
    ingestor = _MockIngestor()
    calls = MagicMock()
    calls.attach_mock(ingestor.ensure_node_batch, "ensure_node_batch")
    calls.attach_mock(ingestor.ensure_relationship_batch, "ensure_relationship_batch")
    create_and_run_updater(repo, ingestor)
    root = repo.resolve().as_posix()
    return [
        repr(call).replace(root, "<repo>")
        for call in calls.mock_calls
        if (call.args[0] if call[0] == "ensure_node_batch" else call.args[1])
        in (*_STRUCTURE_LABELS, *_STRUCTURE_EDGES)
    ]


type Choice = tuple[
    str,
    engine_frame.Structure | tuple[engine_frame.ContainedRow | None, ...],
    engine_frame.Structure | tuple[engine_frame.ContainedRow | None, ...],
]


def _recording(monkeypatch: pytest.MonkeyPatch) -> list[Choice]:
    """Each structure and File-row choice a run makes: what it was, Python's
    answer and the one used."""
    found: list[Choice] = []
    structure_for_run = engine_frame.structure_for_run
    file_rows_for_run = engine_frame.file_rows_for_run

    def structure(
        repo_path: Path,
        project_name: str,
        markers: Iterable[str],
        exclude: frozenset[str] | None,
        unignore: frozenset[str] | None,
        python: engine_frame.Structure,
    ) -> tuple[engine_frame.Structure, engine_frame.Structure | None]:
        used, frame = structure_for_run(
            repo_path, project_name, markers, exclude, unignore, python
        )
        found.append(("structure", python, used))
        return used, frame

    def file_rows(
        repo_path: Path,
        project_name: str,
        elements: Sequence[tuple[str, str | None]],
        paths: Sequence[Path],
        python: tuple[engine_frame.ContainedRow | None, ...],
    ) -> tuple[engine_frame.ContainedRow | None, ...]:
        used = file_rows_for_run(repo_path, project_name, elements, paths, python)
        found.append(("file rows", python, used))
        return used

    monkeypatch.setattr(engine_frame, "structure_for_run", structure)
    monkeypatch.setattr(engine_frame, "file_rows_for_run", file_rows)
    return found


class TestRuns:
    def test_a_run_compares_its_structure_and_its_file_rows(
        self, tmp_path: Path, checked: _Recorded
    ) -> None:
        repo = _tree(tmp_path / "repo", LAYOUT)

        create_and_run_updater(repo, _MockIngestor())

        assert checked.structures == []
        # The structure, then the run's File rows.
        assert checked.compared == 2

    def test_a_single_file_run_compares_its_structure_and_its_one_row(
        self, tmp_path: Path, checked: _Recorded, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _tree(tmp_path / "repo", LAYOUT)
        create_and_run_updater(repo, _MockIngestor())
        target = repo / "pkg" / "sub" / "mod.py"
        target.write_text("X = 2\n", encoding=cs.ENCODING_UTF8)
        found = _recording(monkeypatch)

        _updater(target).run()

        assert checked.structures == []
        ((_, python_structure, _), (_, python_rows, _)) = found
        assert isinstance(python_structure, engine_frame.Structure)
        assert len(python_structure.elements) == 4
        assert isinstance(python_rows, tuple)
        ((row),) = python_rows
        assert dict(row.node.properties)[cs.KEY_PATH] == "pkg/sub/mod.py"

    @needs_links
    def test_a_folder_that_turns_into_a_loop_of_links_mid_run_ends_no_settling(
        self,
        tmp_path: Path,
        checked: _Recorded,
        monkeypatch: pytest.MonkeyPatch,
        _fail_on_swallowed_pass_errors: list[str],
    ) -> None:
        # After the files are read and before their rows are settled: the
        # loop's file fails in its own handler, as before the frame (where
        # resolving it raises, before 3.13), and every other file is still
        # rebuilt; the frame places it as Python does.
        repo = _tree(tmp_path / "repo", ["a.py", "loop/b.py", "z/c.py"])
        pre_parse = GraphUpdater._pre_parse_changed_files

        def looping(
            updater: GraphUpdater,
            changed_entries: list[tuple[Path, str, bool, bytes]],
        ) -> dict[Path, tuple[Node, dict[str, list] | None]]:
            parsed = pre_parse(updater, changed_entries)
            shutil.rmtree(repo / "loop")
            _link(repo / "loop", "loop", directory=True)
            return parsed

        monkeypatch.setattr(GraphUpdater, "_pre_parse_changed_files", looping)
        ingestor = _MockIngestor()
        parsers, queries = load_parsers()
        updater = GraphUpdater(ingestor, repo, parsers, queries, skip_embeddings=True)
        loop_raises = sys.version_info < cs.FRAME_RESOLVE_LOOP_RAISES_BEFORE

        if loop_raises:
            with pytest.raises(RuntimeError):
                updater.run()
        else:
            updater.run()

        assert checked.structures == []
        written = _names(ingestor, cs.NodeLabel.FILE, cs.KEY_PATH)
        assert written == (
            ["a.py", "z/c.py"] if loop_raises else ["a.py", "loop/b.py", "z/c.py"]
        )
        # Where resolving raises, the loop's file failed its parse too, as
        # it would have before the frame.
        assert all("loop/b.py" in line for line in _fail_on_swallowed_pass_errors)
        assert bool(_fail_on_swallowed_pass_errors) is loop_raises
        _fail_on_swallowed_pass_errors.clear()

    @pytest.mark.parametrize("enabled", [False, True])
    def test_a_run_writes_today_s_rows_in_today_s_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
    ) -> None:
        monkeypatch.setattr(engine_frame, "CHECK", None)
        monkeypatch.setattr(engine_frame, "ENABLED", False)
        today = _structure_writes(_tree(tmp_path / "a" / "repo", LAYOUT))
        monkeypatch.setattr(engine_frame, "ENABLED", enabled)
        found = _recording(monkeypatch)

        written = _structure_writes(_tree(tmp_path / "b" / "repo", LAYOUT))

        assert written == today
        # Three folders and four files, each a node and its edge.
        assert len(today) == 2 * 7
        assert "(<NodeLabel.PACKAGE: 'Package'>, {" in today[0]
        assert "<RelationshipType.CONTAINS_PACKAGE: " in today[1]
        # With the frame on, what was written was the frame's.
        assert [(kind, used is not python) for kind, python, used in found] == (
            [("structure", True), ("file rows", True)]
            if enabled
            else [("structure", False)]
        )


# --- choosing the answer --------------------------------------------------------


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frame on, as it will be in production, with no check in place."""
    monkeypatch.setattr(engine_frame, "ENABLED", True)
    monkeypatch.setattr(engine_frame, "CHECK", None)


def _shouting(row: engine_frame.ContainedRow, key: str) -> engine_frame.ContainedRow:
    """`row` with its property `key` spelled in capitals."""
    properties = tuple(
        (name, value.upper() if name == key else value)
        for name, value in row.node.properties
    )
    return row._replace(node=row.node._replace(properties=properties))


def _plant_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frame's structure, planted with a difference: its last row's
    name in capitals."""
    original = engine_frame.frame_structure

    def planted(
        repo_path: Path,
        project_name: str,
        markers: Iterable[str],
        exclude: frozenset[str] | None = None,
        unignore: frozenset[str] | None = None,
    ) -> engine_frame.Structure | None:
        found = original(repo_path, project_name, markers, exclude, unignore)
        assert found is not None
        *kept, last = found.rows
        return found._replace(rows=(*kept, _shouting(last, cs.KEY_NAME)))

    monkeypatch.setattr(engine_frame, "frame_structure", planted)


def _plant_file_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frame's File rows, planted with a difference: each extension in
    capitals."""
    original = engine_frame.frame_file_rows

    def planted(
        repo_path: Path,
        project_name: str,
        elements: Sequence[tuple[str, str | None]],
        paths: Sequence[Path],
    ) -> tuple[engine_frame.ContainedRow | None, ...]:
        rows = original(repo_path, project_name, elements, paths)
        assert rows is not None
        return tuple(
            None if row is None else _shouting(row, cs.KEY_EXTENSION) for row in rows
        )

    monkeypatch.setattr(engine_frame, "frame_file_rows", planted)


def _names(ingestor: _MockIngestor, label: str, key: str) -> list[str]:
    return sorted(row[key] for row in _labelled(ingestor, label))


def _warnings(run: Callable[[], object]) -> list[str]:
    warned: list[str] = []
    sink = logger.add(lambda message: warned.append(str(message)), level="WARNING")
    try:
        run()
    finally:
        logger.remove(sink)
    return warned


def _labelled(ingestor: _MockIngestor, label: str) -> list[dict[str, str]]:
    return [
        call.args[1]
        for call in ingestor.ensure_node_batch.call_args_list
        if call.args[0] == label
    ]


def _folder_row(name: str) -> engine_frame.ContainedRow:
    return engine_frame.ContainedRow(
        node=engine_frame.NodeRow(
            label=cs.NodeLabel.FOLDER,
            properties=((cs.KEY_PATH, name), (cs.KEY_NAME, name)),
        ),
        relationship=engine_frame.RelationshipRow(
            start=(cs.NodeLabel.PROJECT, cs.KEY_NAME, PROJECT),
            rel_type=cs.RelationshipType.CONTAINS_FOLDER,
            end=(cs.NodeLabel.FOLDER, cs.KEY_PATH, name),
        ),
    )


def _structure(*names: str) -> engine_frame.Structure:
    return engine_frame.Structure(
        elements=((".", None), *((name, None) for name in names)),
        rows=tuple(_folder_row(name) for name in names),
    )


class TestComparison:
    # Equal answers are told without spelling their lines; one that differs
    # anywhere is still named line by line.
    def test_equal_structures_have_no_difference(self) -> None:
        assert engine_frame.structure_difference(_structure("a"), _structure("a")) == []

    def test_a_property_alone_is_compared(self) -> None:
        renamed = _structure("a")._replace(rows=(_folder_row("b"),))

        assert engine_frame.structure_difference(_structure("a"), renamed) == [
            "- node\tFolder\tpath=a\tname=a",
            "- edge\tProject(name=proj)\tCONTAINS_FOLDER\tFolder(path=a)",
            "+ node\tFolder\tpath=b\tname=b",
            "+ edge\tProject(name=proj)\tCONTAINS_FOLDER\tFolder(path=b)",
        ]

    def test_an_element_alone_is_compared(self) -> None:
        packaged = _structure("a")._replace(elements=((".", None), ("a", "proj.a")))

        assert engine_frame.structure_difference(_structure("a"), packaged) == [
            "- element\ta",
            "+ element\ta\tproj.a",
        ]

    def test_equal_file_rows_have_no_difference(self) -> None:
        rows = (_folder_row("a"), None)
        paths = [Path("/r/a"), Path("/r/b")]

        assert engine_frame.file_rows_difference(paths, rows, rows) == []

    def test_a_row_only_one_side_has_is_named(self) -> None:
        paths = [Path("/r/a")]

        assert engine_frame.file_rows_difference(
            paths, (_folder_row("a"),), (None,)
        ) == [
            "- node\tFolder\tpath=a\tname=a",
            "- edge\tProject(name=proj)\tCONTAINS_FOLDER\tFolder(path=a)",
            f"+ no row\t{Path('/r/a')}",
        ]

    def test_rows_not_one_to_each_path_are_refused_as_before(self) -> None:
        rows = (_folder_row("a"),)

        with pytest.raises(ValueError):
            engine_frame.file_rows_difference([Path("/r/a"), Path("/r/b")], rows, rows)


class TestChoosing:
    def test_off_and_unchecked_the_frame_is_never_asked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(engine_frame, "ENABLED", False)
        monkeypatch.setattr(engine_frame, "CHECK", None)

        def refusing(*_args: object) -> None:
            raise AssertionError("the frame was asked")

        monkeypatch.setattr(engine_frame, "frame_structure", refusing)
        monkeypatch.setattr(engine_frame, "frame_file_rows", refusing)
        ingestor = _MockIngestor()

        create_and_run_updater(_tree(tmp_path / "repo", LAYOUT), ingestor)

        assert len(_labelled(ingestor, cs.NodeLabel.FILE)) == 4

    def test_on_a_difference_one_warning_names_it_and_python_s_rows_are_written(
        self, tmp_path: Path, production: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _tree(tmp_path / "repo", LAYOUT)
        _plant_structure(monkeypatch)
        ingestor = _MockIngestor()

        warned = _warnings(lambda: create_and_run_updater(repo, ingestor))

        differs = [line for line in warned if "folder structure" in line]
        assert len(differs) == 1
        assert str(repo) in differs[0]
        assert "- node\tFolder\tpath=plain\tname=plain\t" in differs[0]
        assert _names(ingestor, cs.NodeLabel.FOLDER, cs.KEY_NAME) == ["plain", "sub"]

    def test_file_rows_that_differ_fall_back_too(
        self, tmp_path: Path, production: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _tree(tmp_path / "repo", LAYOUT)
        _plant_file_rows(monkeypatch)
        ingestor = _MockIngestor()

        warned = _warnings(lambda: create_and_run_updater(repo, ingestor))

        differs = [line for line in warned if "File rows" in line]
        assert len(differs) == 1
        assert (
            "- node\tFile\tpath=top.json\tname=top.json\textension=.json\t"
            in differs[0]
        )
        assert _names(ingestor, cs.NodeLabel.FILE, cs.KEY_EXTENSION) == [
            ".json",
            ".md",
            ".py",
            ".py",
        ]

    def test_a_frame_that_raises_falls_back(
        self, tmp_path: Path, production: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raising(*_args: object) -> None:
            raise ValueError("refused")

        monkeypatch.setattr(engine_frame, "frame_structure", raising)
        ingestor = _MockIngestor()

        warned = _warnings(
            lambda: create_and_run_updater(_tree(tmp_path / "repo", LAYOUT), ingestor)
        )

        assert sum("raised\trefused" in line for line in warned) == 1
        assert len(_labelled(ingestor, cs.NodeLabel.FOLDER)) == 2
        # No structure of the frame's, so no File rows of its either.
        assert len(_labelled(ingestor, cs.NodeLabel.FILE)) == 4

    @pytest.mark.parametrize("enabled", [False, True])
    def test_a_check_is_told_of_the_difference_instead(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        checked: _Recorded,
        enabled: bool,
    ) -> None:
        monkeypatch.setattr(engine_frame, "ENABLED", enabled)
        _plant_structure(monkeypatch)
        _plant_file_rows(monkeypatch)
        ingestor = _MockIngestor()

        create_and_run_updater(_tree(tmp_path / "repo", LAYOUT), ingestor)

        structure, file_rows = checked.structures
        assert [line[:2] for line in structure] == ["- ", "+ "]
        assert structure[1].startswith("+ node\tFolder\tpath=plain\tname=PLAIN\t")
        assert len(file_rows) == 2 * 4
        # Strict: with the frame on, the run goes on with the frame's rows.
        folders = _names(ingestor, cs.NodeLabel.FOLDER, cs.KEY_NAME)
        extensions = _names(ingestor, cs.NodeLabel.FILE, cs.KEY_EXTENSION)
        assert folders == (["PLAIN", "sub"] if enabled else ["plain", "sub"])
        assert (".PY" in extensions) is enabled

    def test_a_check_that_cannot_compare_leaves_the_frame_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checked: _Recorded
    ) -> None:
        monkeypatch.setattr(engine_frame, "ENABLED", True)
        monkeypatch.setattr(checked, "compares_structure", lambda: False)
        _plant_structure(monkeypatch)
        ingestor = _MockIngestor()

        create_and_run_updater(_tree(tmp_path / "repo", LAYOUT), ingestor)

        assert checked.structures == []
        assert _names(ingestor, cs.NodeLabel.FOLDER, cs.KEY_NAME) == ["plain", "sub"]


# Where a run with the frame off and no check in place was reported asked
# for its structure (#3754): after every change-detection test, in one
# process, and in that order. Their own shadow driver, which reruns other
# files in a process of its own, is left out. The walk's choosing tests go
# between them: one failed under the discovery shadow the same way.
OFF_AND_UNCHECKED = "test_engine_structure.py::TestChoosing::test_off_and_unchecked_the_frame_is_never_asked"
SHADOW_PLUGINS = (
    "codebase_rag.tests.ignore_rules_shadow",
    "codebase_rag.tests.discovery_shadow",
    "codebase_rag.tests.change_shadow",
    SHADOW_PLUGIN,
)
CHANGE_DETECTION_TESTS = "test_engine_change_detection.py"
PLAN_CHOOSING_TESTS = "test_engine_discovery.py::TestPlanForRun"
CHANGE_DETECTION_DRIVER = (
    "test_engine_change_detection.py"
    "::test_every_existing_incremental_sync_test_agrees_with_the_frame"
)


@pytest.mark.parametrize("shadow", [None, *SHADOW_PLUGINS])
def test_a_run_with_the_frame_off_is_unasked_after_the_change_detection_tests(
    shadow: str | None,
) -> None:
    # Also under each shadow plugin, which compares the frame with Python
    # wherever a test asks Python: the structure shadow compared each File
    # row whatever the test had set, so this test failed under it with "the
    # frame was asked", and a run planted with a difference failed under the
    # discovery shadow. Each now compares only while its own check is in
    # place.
    plugins = () if shadow is None else ("-p", shadow)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-n",
            "0",
            *plugins,
            *(
                (TESTS / name).relative_to(REPO_ROOT).as_posix()
                for name in (
                    CHANGE_DETECTION_TESTS,
                    PLAN_CHOOSING_TESTS,
                    OFF_AND_UNCHECKED,
                )
            ),
            # By its node id, which is the path from the root.
            "--deselect",
            (TESTS / CHANGE_DETECTION_DRIVER).relative_to(REPO_ROOT).as_posix(),
        ],
        cwd=REPO_ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=1800,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-8000:]
    assert re.search(r"\b\d{2,} passed", output), output[-4000:]


def _shadowed_test_files() -> list[str]:
    return sorted(
        path.name
        for path in TESTS.glob("test_*.py")
        if path.name != Path(__file__).name
        and SHADOW_MARKERS.search(path.read_text(encoding=cs.ENCODING_UTF8))
    )


def test_every_existing_structure_test_agrees_with_the_frame(tmp_path: Path) -> None:
    # The existing tests that read the structure map, call the structure
    # pass or assert on its rows, with every structure and every File row
    # compared with the frame's; any difference fails the test that made
    # it. On two workers of their own: each writes what it compared, added
    # up here.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_XDIST", "PYTEST_CURRENT_TEST"))
    }
    env[STRUCTURE_SHADOW_DIR_ENV] = str(tmp_path)
    files = _shadowed_test_files()
    assert len(files) > 30, files
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
    total = {
        key: sum(report[key] for report in reports) for key in ("structures", "rows")
    }
    # Hundreds of each even on a base install, which skips the tests of
    # every grammar it lacks (710 structures and 924 rows there; about
    # 1,450 and 1,970 with them all).
    assert total["structures"] > 300, total
    assert total["rows"] > 300, total
