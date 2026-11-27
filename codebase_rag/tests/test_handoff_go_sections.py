"""Go's own sections of the project index hand-off, format version 2.

docs/reports/PHASE3_GO.md, sections 6 and 10 (step 2). The index Python
writes holds, beside the five sections every language fills, the tables Go's
analysis keeps: each module's package clause, each method's signature, each
interface's embeds, each implementation pair with who paired it and the
builds it holds in, each free function's first result and each file's build
constraint. The Rust engine writes the same from its index, so a difference
names the stage at which the two part. These tests read the sections Python
writes for small modules, each line exactly, and hold the constraint's text
to the `//go:build` grammar it reads back through.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.go.builds import (
    ALWAYS,
    NEVER,
    UNKNOWN,
    canonical_text,
    header_constraint,
    name_constraint,
)
from codebase_rag.parsers.go_frontend.frontend import _build_tool
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.tests.test_graph_updater_index_engine import LANGUAGE_FIXTURES, _write

_PROJECT = "demo"
_INDEX = "index.txt"
_GO_KINDS = tuple(f"{kind}\t" for kind in cs.HandoffKind if kind.startswith("go_"))

# One package in two files of a folder, a receiver method beside its type's
# file, and the folder's external test package, whose clause differs.
PACKAGE_ACROSS_FILES = {
    "go.mod": "module example.com/store\n\ngo 1.22\n",
    "store/item.go": (
        "package store\n\ntype Item struct {\n\tKey string\n}\n\n"
        "func New(key string) (*Item, error) {\n\treturn &Item{Key: key}, nil\n}\n"
    ),
    "store/methods.go": (
        "package store\n\nfunc (i *Item) Name() string {\n\treturn i.Key\n}\n\n"
        "func (i Item) Size(scale int, unit string) (int, bool) {\n"
        '\treturn len(i.Key) * scale, unit != ""\n}\n'
    ),
    "store/item_test.go": "package store_test\n\nfunc helper() int {\n\treturn 1\n}\n",
}
# Interfaces embedding a local one and one from the standard library, in the
# order written, and a type whose methods satisfy the two with local embeds.
INTERFACE_WITH_EMBEDS = {
    "go.mod": "module example.com/shapes\n\ngo 1.22\n",
    "shapes/shape.go": (
        'package shapes\n\nimport "io"\n\n'
        "type Shape interface {\n\tArea() float64\n}\n\n"
        "type Solid interface {\n\tShape\n\tio.Reader\n\tVolume() float64\n}\n\n"
        "type Boxed interface {\n\tShape\n\tVolume() float64\n}\n"
    ),
    "shapes/cube.go": (
        "package shapes\n\ntype Cube struct {\n\tSide float64\n}\n\n"
        "func (c Cube) Area() float64 {\n\treturn 6 * c.Side * c.Side\n}\n\n"
        "func (c Cube) Volume() float64 {\n\treturn c.Side * c.Side * c.Side\n}\n"
    ),
}
# A type each of two builds declares, by file name, each implementing one
# interface in its own build, and a file under a `//go:build` line.
BUILD_VARIANTS = {
    "go.mod": "module example.com/clock\n\ngo 1.22\n",
    "sys/clock.go": "package sys\n\ntype Clock interface {\n\tNow() int\n}\n",
    "sys/wall_linux.go": (
        "package sys\n\ntype wall struct{}\n\nfunc (wall) Now() int {\n\treturn 1\n}\n"
    ),
    "sys/wall_windows.go": (
        "package sys\n\ntype wall struct{}\n\nfunc (wall) Now() int {\n\treturn 2\n}\n"
    ),
    "sys/tuned.go": (
        "//go:build !windows && (amd64 || arm64)\n\npackage sys\n\n"
        "func tuned() bool {\n\treturn true\n}\n"
    ),
}
GO_SECTION_FIXTURES = {
    "go_package_across_files": PACKAGE_ACROSS_FILES,
    "go_interface_with_embeds": INTERFACE_WITH_EMBEDS,
    "go_build_variants": BUILD_VARIANTS,
}


@pytest.fixture(autouse=True)
def _without_the_type_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    # What most installs run, and what the reference graphs record.
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)


@contextmanager
def _embedded_graph(path: Path) -> Iterator[MemgraphIngestor]:
    with MemgraphIngestor(
        host="unused",
        port=0,
        dialect=get_dialect(DIALECT_SQLITE),
        sqlite_path=str(path),
    ) as ingestor:
        ingestor.ensure_constraints()
        yield ingestor


def _index(files: dict[str, str], tmp_path: Path) -> list[str]:
    """`files` indexed afresh: the lines of the `index.txt` the run wrote."""
    repo = _write(tmp_path / "repo", files)
    handoffs = tmp_path / "handoffs"
    parsers, queries = load_parsers()
    with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
        GraphUpdater(
            ingestor=ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            project_name=_PROJECT,
            skip_embeddings=True,
            state_dir=tmp_path / "state",
            handoff_dir=handoffs,
        ).run()
    data = (handoffs / _INDEX).read_bytes()
    assert data.endswith(b"\n")
    return data.decode("utf-8").split("\n")[:-1]


def _go_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith(_GO_KINDS)]


class TestTheGoSections:
    def test_a_package_across_two_files(self, tmp_path: Path) -> None:
        lines = _index(PACKAGE_ACROSS_FILES, tmp_path)

        # Each module's clause, the test package's its own. The receiver
        # methods sit under their type, declared in the other file, with
        # their own file and the package both files share; a parameter list
        # gives one type per parameter, the results one per result.
        assert lines[0] == "# cgr handoff index.txt 2"
        assert _go_lines(lines) == [
            'go_method\tdemo.store.item.Item.Name\t{"file":"store/methods.go",'
            '"package":"store:store","params":[],"results":["string"]}',
            'go_method\tdemo.store.item.Item.Size\t{"file":"store/methods.go",'
            '"package":"store:store","params":["int","string"],'
            '"results":["int","bool"]}',
            'go_package\tdemo.store.item\t{"clause":"store"}',
            'go_package\tdemo.store.item_test\t{"clause":"store_test"}',
            'go_package\tdemo.store.methods\t{"clause":"store"}',
            'go_return_type\tdemo.store.item.New\t{"type":"Item"}',
            'go_return_type\tdemo.store.item_test.helper\t{"type":"int"}',
        ]

    def test_an_interface_with_embeds(self, tmp_path: Path) -> None:
        lines = _index(INTERFACE_WITH_EMBEDS, tmp_path)

        # Embeds as written, in order, the one outside the repository too.
        # Cube's methods satisfy Shape and Boxed by name; Solid's method set
        # holds io.Reader's, which no file here declares, so it pairs with
        # nothing. Every build holds both pairs.
        assert _go_lines(lines) == [
            'go_implements\tdemo.shapes.cube.Cube\t{"builds":"<always>",'
            '"interface":"demo.shapes.shape.Boxed","source":"guessed"}',
            'go_implements\tdemo.shapes.cube.Cube\t{"builds":"<always>",'
            '"interface":"demo.shapes.shape.Shape","source":"guessed"}',
            'go_interface\tdemo.shapes.shape.Boxed\t{"embeds":["Shape"],'
            '"file":"shapes/shape.go","package":"shapes:shapes"}',
            'go_interface\tdemo.shapes.shape.Shape\t{"embeds":[],'
            '"file":"shapes/shape.go","package":"shapes:shapes"}',
            'go_interface\tdemo.shapes.shape.Solid\t{"embeds":["Shape","io.Reader"],'
            '"file":"shapes/shape.go","package":"shapes:shapes"}',
            'go_method\tdemo.shapes.cube.Cube.Area\t{"file":"shapes/cube.go",'
            '"package":"shapes:shapes","params":[],"results":["float64"]}',
            'go_method\tdemo.shapes.cube.Cube.Volume\t{"file":"shapes/cube.go",'
            '"package":"shapes:shapes","params":[],"results":["float64"]}',
            'go_method\tdemo.shapes.shape.Boxed.Volume\t{"file":"shapes/shape.go",'
            '"package":"shapes:shapes","params":[],"results":["float64"]}',
            'go_method\tdemo.shapes.shape.Shape.Area\t{"file":"shapes/shape.go",'
            '"package":"shapes:shapes","params":[],"results":["float64"]}',
            'go_method\tdemo.shapes.shape.Solid.Volume\t{"file":"shapes/shape.go",'
            '"package":"shapes:shapes","params":[],"results":["float64"]}',
            'go_package\tdemo.shapes.cube\t{"clause":"shapes"}',
            'go_package\tdemo.shapes.shape\t{"clause":"shapes"}',
        ]

    def test_a_build_variant_pair(self, tmp_path: Path) -> None:
        lines = _index(BUILD_VARIANTS, tmp_path)

        # Each variant's file names its build, and each pair holds in that
        # build alone. A file every build compiles, `clock.go`, states no
        # constraint and has no line.
        assert _go_lines(lines) == [
            'go_build\tsys/tuned.go\t{"constraint":"!windows && (amd64 || arm64)"}',
            'go_build\tsys/wall_linux.go\t{"constraint":"linux"}',
            'go_build\tsys/wall_windows.go\t{"constraint":"windows"}',
            'go_implements\tdemo.sys.wall_linux.wall\t{"builds":"linux",'
            '"interface":"demo.sys.clock.Clock","source":"guessed"}',
            'go_implements\tdemo.sys.wall_windows.wall\t{"builds":"windows",'
            '"interface":"demo.sys.clock.Clock","source":"guessed"}',
            'go_interface\tdemo.sys.clock.Clock\t{"embeds":[],'
            '"file":"sys/clock.go","package":"sys:sys"}',
            'go_method\tdemo.sys.clock.Clock.Now\t{"file":"sys/clock.go",'
            '"package":"sys:sys","params":[],"results":["int"]}',
            'go_method\tdemo.sys.wall_linux.wall.Now\t{"file":"sys/wall_linux.go",'
            '"package":"sys:sys","params":[],"results":["int"]}',
            'go_method\tdemo.sys.wall_windows.wall.Now\t{"file":"sys/wall_windows.go",'
            '"package":"sys:sys","params":[],"results":["int"]}',
            'go_package\tdemo.sys.clock\t{"clause":"sys"}',
            'go_package\tdemo.sys.tuned\t{"clause":"sys"}',
            'go_package\tdemo.sys.wall_linux\t{"clause":"sys"}',
            'go_package\tdemo.sys.wall_windows\t{"clause":"sys"}',
            'go_return_type\tdemo.sys.tuned.tuned\t{"type":"bool"}',
        ]

    @pytest.mark.usefixtures(
        "one_go_helper_per_session", "fingerprint_inputs_once_per_session"
    )
    def test_a_pair_the_type_checker_proved_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        go = shutil.which("go")
        if go is None:
            pytest.skip("go toolchain not available")
        if _build_tool(go) is None:
            pytest.skip("gotypes tool could not build in this environment")
        monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)

        lines = _index(INTERFACE_WITH_EMBEDS, tmp_path)

        # go/types reads io.Reader too, so it proves Cube implements neither
        # Solid nor anything the names would not pair it with.
        assert [line for line in lines if line.startswith("go_implements\t")] == [
            'go_implements\tdemo.shapes.cube.Cube\t{"builds":"<always>",'
            '"interface":"demo.shapes.shape.Boxed","source":"proven"}',
            'go_implements\tdemo.shapes.cube.Cube\t{"builds":"<always>",'
            '"interface":"demo.shapes.shape.Shape","source":"proven"}',
        ]

    @pytest.mark.parametrize(
        "language",
        [
            language
            for language in LANGUAGE_FIXTURES
            if language != cs.SupportedLanguage.GO
        ],
    )
    def test_a_repository_without_go_has_none(
        self, language: cs.SupportedLanguage, tmp_path: Path
    ) -> None:
        lines = _index(LANGUAGE_FIXTURES[language], tmp_path)

        assert lines[0] == "# cgr handoff index.txt 2"
        assert _go_lines(lines) == []


# Constraints as `//go:build` lines write them, each with its canonical text:
# operands in the order written, compound operands of a compound grouped.
_CONSTRAINTS = [
    ("linux", "linux"),
    ("!windows", "!windows"),
    ("linux && amd64", "linux && amd64"),
    ("linux || darwin", "linux || darwin"),
    ("!windows && (amd64 || arm64)", "!windows && (amd64 || arm64)"),
    ("(linux && cgo) || go1.21", "(linux && cgo) || go1.21"),
    ("linux && cgo || go1.21", "(linux && cgo) || go1.21"),
    ("!(linux && amd64)", "!(linux && amd64)"),
    ("((linux))", "linux"),
    ("linux && linux", "linux"),
    ("custom_tag && !other.tag", "custom_tag && !other.tag"),
]


def _go_build(expression: str) -> str:
    return f"//go:build {expression}\n\npackage p\n"


class TestTheConstraintText:
    @pytest.mark.parametrize(("expression", "text"), _CONSTRAINTS)
    def test_it_reads_back_as_the_same_constraint(
        self, expression: str, text: str
    ) -> None:
        constraint = header_constraint(_go_build(expression))

        assert canonical_text(constraint) == text
        assert header_constraint(_go_build(text)) == constraint

    def test_a_file_name_reads_as_its_build(self) -> None:
        assert canonical_text(name_constraint("x/poll_linux_arm64.go")) == (
            "linux && arm64"
        )
        assert canonical_text(name_constraint("x/poll_windows_test.go")) == "windows"

    def test_what_the_grammar_cannot_spell_has_words_no_tag_can_be(self) -> None:
        texts = [canonical_text(constraint) for constraint in (ALWAYS, NEVER, UNKNOWN)]

        assert texts == ["<always>", "<never>", "<unknown>"]
        assert canonical_text(header_constraint(_go_build("linux && !linux"))) == (
            "<never>"
        )
        assert canonical_text(header_constraint(_go_build("linux &&"))) == "<unknown>"
        assert canonical_text(header_constraint("package p\n")) == "<always>"
