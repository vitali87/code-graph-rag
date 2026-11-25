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
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePath
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import engine_frame
from codebase_rag.config import load_ignore_patterns
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.structure_processor import StructureProcessor
from codebase_rag.tests.conftest import _MockIngestor, create_and_run_updater
from codebase_rag.tests.engine_modes import STRUCTURE_SHADOW_DIR_ENV
from codebase_rag.tests.test_symlink_policy import _link, _make_undecodable, _write
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
    def test_folders_come_in_python_s_path_order_part_by_part(
        self, tmp_path: Path, pairs: list[Pair], checked: _Recorded
    ) -> None:
        repo = _tree(
            tmp_path / "repo",
            [
                "a-b/x/",
                "a/b/",
                "a.b/",
                "B/",
                "a_b/",
                "é/",
                "é/",
                "z/",
                "中文/",
                "🎉/",
                "a/b/c/",
                "Ä/",
                "ä/",
            ],
        )

        found = _same_structure(pairs, checked, repo)

        # `a/b` before `a-b`, though `a-b` sorts first as a string; upper
        # case before lower; then by code point, a composed `é` after the
        # decomposed one; the root first, as `.`.
        assert _keys(found) == [
            ".",
            "B",
            "a",
            "a/b",
            "a/b/c",
            "a-b",
            "a-b/x",
            "a.b",
            "a_b",
            "é",
            "z",
            "Ä",
            "ä",
            "é",
            "中文",
            "🎉",
        ]

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
        names = ["app.vcxproj", "[ab]?.vcxproj", "x*.vcxproj"]
        if sys.platform != "win32":
            names.append("*.vcxproj")
        repo = _tree(tmp_path / "repo", [f"{i}/{name}" for i, name in enumerate(names)])

        found = _same_structure(pairs, checked, repo)

        literal = {} if sys.platform == "win32" else {"3": f"{PROJECT}.3"}
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

        with pytest.raises(ValueError):
            engine_frame.frame_structure(repo, PROJECT, [], frozenset({"[z-a]"}))


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
    assert total["structures"] > 300, total
    assert total["rows"] > 1000, total
