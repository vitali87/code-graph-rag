"""The Python engine writes its file plan and project index on request.

Phase 1, step 5 of the Rust rewrite (docs/reports/PHASE1_SEAMS.md, sections 3
and 5). Given `handoff_dir`, `GraphUpdater.run` writes `files.txt` once
discovery and change detection are done and `index.txt` once the deferred
definitions are resolved: the two points of today's pipeline the Rust engine
replaces first. Both must read the same at another path under another hash
seed, and asking for them must not change the graph.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.canonical_graph import canonical_json
from codebase_rag.checkout_state import state_dir as checkout_state_dir
from codebase_rag.config import settings
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.document_tier import _load_parser as _load_markdown
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.tests.conftest import _MockIngestor, force_mtime_after_cache
from codebase_rag.tests.test_graph_updater_index_engine import (
    LANGUAGE_FIXTURES,
    _calls,
    _write,
)
from codebase_rag.tests.test_symlink_policy import _make_undecodable

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT = "demo"
_FILES = "files.txt"
_INDEX = "index.txt"
# A deadline for the indexing subprocess, not a performance assertion.
_SUBPROCESS_TIMEOUT_SECONDS = 300

# A Go module whose walk order is not its sorted order: the walk takes a
# directory's files by name, then its subdirectories, so `zeta.go` comes
# before `geo/`. Beside the Go files, a manifest and a document take the
# other routes.
GO_FIXTURE = {
    "README.md": "# Shapes\n\nA demo.\n",
    "go.mod": "module example.com/shapes\n\ngo 1.22\n",
    "main.go": (
        'package main\n\nimport "example.com/shapes/geo"\n\n'
        "func main() {\n\tdescribe(geo.Square{Side: 2})\n}\n\n"
        "func describe(s geo.Shape) float64 {\n\treturn s.Area()\n}\n"
    ),
    "zeta.go": "package main\n\nfunc zeta() int {\n\treturn 1\n}\n",
    "geo/shape.go": (
        "package geo\n\ntype Shape interface {\n\tArea() float64\n}\n\n"
        "type Named interface {\n\tShape\n\tName() string\n}\n"
    ),
    "geo/square.go": (
        "package geo\n\ntype Base struct {\n\tID int\n}\n\n"
        "type Square struct {\n\tBase\n\tSide float64\n}\n\n"
        "func (s Square) Area() float64 {\n\treturn s.Side * s.Side\n}\n\n"
        'func (s Square) Name() string {\n\treturn "square"\n}\n'
    ),
}
_SQUARE_EDITED = (
    GO_FIXTURE["geo/square.go"]
    + "\nfunc (s Square) Double() float64 {\n\treturn 2 * s.Area()\n}\n"
)
_CIRCLE = (
    "package geo\n\ntype Circle struct {\n\tR float64\n}\n\n"
    "func (c Circle) Area() float64 {\n\treturn 3 * c.R * c.R\n}\n"
)
_DOCUMENT_ROUTE = (
    cs.FileTier.DOCUMENT if _load_markdown() is not None else cs.FileTier.NONE
)
# An unchanged caller whose edge into an edited file survives the edit: a
# change to the body alone keeps `helper`'s interface, so `main.py` is not
# re-parsed and its CALLS edge is captured and restored around the re-parse.
CALLER_FIXTURE = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(name):\n    return name.upper()\n",
    "main.py": "from pkg.util import helper\n\n\ndef run():\n    return helper('y')\n",
}
_UTIL_EDITED = "def helper(name):\n    return name.lower()\n"
_CALLS = (
    "MATCH (a:Function)-[:CALLS]->(b:Function) "
    "RETURN a.qualified_name AS caller, b.qualified_name AS callee"
)
_FUNCTIONS = "MATCH (f:Function) RETURN f.qualified_name AS qn"
_FILE_PATHS = "MATCH (f:File) RETURN f.path AS path"
_FOLDER_PATHS = "MATCH (f:Folder) RETURN f.path AS path"
_PACKAGE_PATHS = "MATCH (p:Package) RETURN p.path AS path"
_NODE_NAMES = "MATCH (n) RETURN n.qualified_name AS qn, n.name AS name, n.path AS path"
# How `os.walk` spells the byte 0xff of a name that is not valid UTF-8.
_UNDECODED_BYTE = "\udcff"
# Per language: the indexed files at the root, one importing a package that a
# folder named `bad\xff` holds, and that package's files (issue #3722).
_IMPORTED_FROM_A_BAD_NAME: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "dart": (
        {
            "pubspec.yaml": "name: app\n",
            "test/app_test.dart": "import 'package:bad/x.dart';\n\n"
            "void main() {\n  f();\n}\n",
        },
        {"pubspec.yaml": "name: bad\n", "lib/x.dart": "void f() {}\n"},
    ),
    "go": (
        {
            "go.mod": "module example.com/m\n",
            "main.go": 'package main\n\nimport "example.com/bad"\n\n'
            "func main() {\n\tbad.F()\n}\n",
        },
        {
            "go.mod": "module example.com/bad\n",
            "lib.go": "package bad\n\nfunc F() {}\n",
        },
    ),
    # Generated sources beside a build file, which the walk then rescues.
    "java": (
        {
            "src/main/java/com/app/App.java": "package com.app;\n\n"
            "import com.gen.Gen;\n\n"
            "public class App {\n  void f() {\n    Gen.g();\n  }\n}\n",
        },
        {
            "pom.xml": "<project/>\n",
            "target/generated-sources/annotations/com/gen/Gen.java": (
                "package com.gen;\n\n"
                "public class Gen {\n  public static void g() {}\n}\n"
            ),
        },
    ),
    "js": (
        {
            "app.js": "import { f } from '@acme/bad';\n\n"
            "export function g() {\n  return f();\n}\n",
        },
        {
            "package.json": '{"name": "@acme/bad", "main": "index.js"}\n',
            "index.js": "export function f() {\n  return 1;\n}\n",
        },
    ),
    "julia": (
        {"src/App.jl": "module App\nusing .Foo\nend\n"},
        {"Foo.jl": "module Foo\nf() = 1\nend\n"},
    ),
    "python": (
        {"app.py": "from pkg.mod import f\n\n\ndef g():\n    return f()\n"},
        {"pkg/__init__.py": "", "pkg/mod.py": "def f():\n    return 1\n"},
    ),
    # A `paths` alias from the tsconfig in that folder.
    "ts": (
        {
            "app.ts": "import { f } from '@/x';\n\n"
            "export function g() {\n  return f();\n}\n",
        },
        {
            "tsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["src/*"]}}}\n',
            "src/x.ts": "export function f() {\n  return 1;\n}\n",
        },
    ),
}
# A README and other Markdown beside a Python and a Go file, and a shell
# script, which the ast-grep tier takes where its extra is installed. Neither
# of those two tiers registers a module or a definition in the project index,
# though the graph holds their Module nodes and the script's Function
# (issue #3790).
DOCUMENTS_FIXTURE = {
    "README.md": "# Demo\n\nSee [the guide](docs/guide.md).\n",
    "CHANGELOG.md": "# Changes\n",
    "docs/guide.md": "# Guide\n\n## Usage\n\nRun it.\n",
    "app.py": (
        "class Base:\n    pass\n\n\n"
        "class App(Base):\n    def run(self):\n        return 1\n"
    ),
    "go.mod": "module example.com/demo\n\ngo 1.22\n",
    "main.go": "package main\n\nfunc main() {\n\tgreet()\n}\n\nfunc greet() {}\n",
    "tool.sh": "greet() {\n  echo hi\n}\n",
}
_DOCUMENTS_EDITED = {
    "app.py": DOCUMENTS_FIXTURE["app.py"] + "\n\ndef helper():\n    return 2\n",
    "main.go": DOCUMENTS_FIXTURE["main.go"] + "\nfunc wave() {}\n",
    "docs/guide.md": DOCUMENTS_FIXTURE["docs/guide.md"] + "\n## More\n",
}
# Names and paths outside ASCII, which the files keep as UTF-8.
UNICODE_FIXTURE = {
    "grüße.py": "def grüße():\n    return 'é'\n\n\ndef call():\n    return grüße()\n",
}

# What the reference graphs index with: the frontends that need no compiler,
# so a Go toolchain, a JDK or libclang on PATH changes neither side of a
# comparison. The child process gets the same as environment variables.
_FRONTENDS = {
    "CPP_FRONTEND": cs.CppFrontend.TREESITTER,
    "CSHARP_FRONTEND": cs.CSharpFrontend.TREESITTER,
    "GO_FRONTEND": cs.GoFrontend.TREESITTER,
    "JAVA_FRONTEND": cs.JavaFrontend.HEURISTIC,
    "PYTHON_FRONTEND": cs.PythonFrontend.HEURISTIC,
}

# A second index in a child process: the hash seed is fixed when an
# interpreter starts, so one process cannot index under two. It writes the
# hash of the probe it is given, which shows the seed it ran under.
_CHILD = """
import sys
from pathlib import Path

from codebase_rag.tests.test_handoff import _write_hand_offs

root = Path(sys.argv[1])
(root / "probe.txt").write_text(str(hash(sys.argv[2])), encoding="utf-8")
_write_hand_offs(root)
"""
_SEED_PROBE = "cgr handoff seed probe"


@pytest.fixture(autouse=True)
def _frontends_need_no_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _FRONTENDS.items():
        monkeypatch.setattr(settings, name, value)


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


def _updater(
    repo: Path,
    ingestor: MemgraphIngestor | _MockIngestor,
    *,
    state_dir: Path | None,
    handoff_dir: Path | None,
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=_PROJECT,
        skip_embeddings=True,
        state_dir=state_dir,
        handoff_dir=handoff_dir,
    )


def _write_hand_offs(root: Path) -> None:
    """Index every repository under `root/repos` afresh, each writing its
    hand-offs to `root/handoffs/<name>`. Run here and in the child."""
    for repo in sorted((root / "repos").iterdir()):
        scratch = root / "scratch" / repo.name
        scratch.mkdir(parents=True)
        with _embedded_graph(scratch / "graph.sqlite") as ingestor:
            _updater(
                repo,
                ingestor,
                state_dir=scratch / "state",
                handoff_dir=root / "handoffs" / repo.name,
            ).run(force=True)


def _file_line(
    repo: Path,
    rel: str,
    route: cs.FileTier = cs.FileTier.TREE_SITTER,
    language: str | None = "go",
) -> str:
    # Hashed as it is on disk: text mode writes the fixtures with `\r\n` on
    # Windows.
    md5 = hashlib.md5((repo / rel).read_bytes(), usedforsecurity=False).hexdigest()
    language_json = "null" if language is None else f'"{language}"'
    return (
        f'file\t{rel}\t{{"language":{language_json},"md5":"{md5}","route":"{route}"}}'
    )


def _lines(path: Path) -> list[str]:
    data = path.read_bytes()
    assert data.endswith(b"\n"), path
    return data.decode("utf-8").split("\n")[:-1]


def _full_go_plan(repo: Path) -> list[str]:
    # A full build re-parses every file, so it writes no `reparse` lines, and
    # has nothing to delete.
    return [
        "# cgr handoff files.txt 1",
        _file_line(repo, "README.md", _DOCUMENT_ROUTE, None),
        _file_line(repo, "go.mod", cs.FileTier.MANIFEST, None),
        _file_line(repo, "main.go"),
        _file_line(repo, "zeta.go"),
        _file_line(repo, "geo/shape.go"),
        _file_line(repo, "geo/square.go"),
    ]


def _graph_facts(ingestor: MemgraphIngestor) -> tuple[set[str], set[tuple[str, str]]]:
    functions = {str(row["qn"]) for row in ingestor.fetch_all(_FUNCTIONS)}
    calls = {
        (str(row["caller"]), str(row["callee"])) for row in ingestor.fetch_all(_CALLS)
    }
    return functions, calls


def _stamp_after_cache(repo: Path, *rels: str) -> None:
    # An incremental run re-hashes only a file stamped strictly later than the
    # hash cache, so an edit made in the same tick as the cache must be moved.
    for rel in rels:
        force_mtime_after_cache(repo, repo / rel)


class TestTheFilePlan:
    def test_it_lists_every_file_in_walk_order_with_its_route_and_hash(
        self, tmp_path: Path
    ) -> None:
        repo = _write(tmp_path / "repo", GO_FIXTURE)
        handoffs = tmp_path / "handoffs"
        with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
            _updater(
                repo, ingestor, state_dir=tmp_path / "state", handoff_dir=handoffs
            ).run()

        assert _lines(handoffs / _FILES) == _full_go_plan(repo)

    def test_a_failure_while_parsing_leaves_the_file_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Discovery and change detection were done; the plan shows the engines
        # agreed that far even when the definitions stage fails.
        monkeypatch.setattr(
            GraphUpdater,
            "_pre_parse_changed_files",
            MagicMock(side_effect=RuntimeError("a parse failed")),
        )
        repo = _write(tmp_path / "repo", GO_FIXTURE)
        handoffs = tmp_path / "handoffs"
        with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
            updater = _updater(
                repo, ingestor, state_dir=tmp_path / "state", handoff_dir=handoffs
            )
            with pytest.raises(RuntimeError, match="a parse failed"):
                updater.run()

        assert _lines(handoffs / _FILES) == _full_go_plan(repo)
        assert not (handoffs / _INDEX).exists()

    def test_a_plan_that_cannot_be_written_leaves_the_graph_as_it_was(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The plan is written before the run deletes anything. Written after
        # the stale subtrees went, a failed write lost the unchanged caller's
        # edge for good: captured but never handed to the restore.
        repo = _write(tmp_path / "repo", CALLER_FIXTURE)
        handoffs = tmp_path / "handoffs"
        with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
            # The checkout's own state directory, which the stamp reads.
            updater = _updater(repo, ingestor, state_dir=None, handoff_dir=handoffs)
            updater.run()
            before = _graph_facts(ingestor)
            assert ("demo.main.run", "demo.pkg.util.helper") in before[1]
            (repo / "pkg" / "util.py").write_text(_UTIL_EDITED, encoding="utf-8")
            _stamp_after_cache(repo, "pkg/util.py")
            monkeypatch.setattr(
                gu, "write_file_plan", MagicMock(side_effect=OSError("read-only"))
            )

            with pytest.raises(OSError, match="read-only"):
                updater.run()

            assert _graph_facts(ingestor) == before

    def test_an_incremental_run_names_what_it_reparses_and_deletes(
        self, tmp_path: Path
    ) -> None:
        repo = _write(tmp_path / "repo", GO_FIXTURE)
        handoffs = tmp_path / "handoffs"
        with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
            # The checkout's own state directory, which the stamp reads.
            updater = _updater(repo, ingestor, state_dir=None, handoff_dir=handoffs)
            updater.run()
            (repo / "geo" / "square.go").write_text(_SQUARE_EDITED, encoding="utf-8")
            (repo / "geo" / "circle.go").write_text(_CIRCLE, encoding="utf-8")
            (repo / "zeta.go").unlink()
            _stamp_after_cache(repo, "geo/square.go", "geo/circle.go")

            updater.run()

        # Every file still in the walk, in walk order, an unchanged one with
        # the hash the cache already held. Then the re-parse set: the new and
        # the edited file, and the Go package siblings of each changed or
        # deleted file, which re-parse with them; and the deleted file.
        assert _lines(handoffs / _FILES) == [
            "# cgr handoff files.txt 1",
            _file_line(repo, "README.md", _DOCUMENT_ROUTE, None),
            _file_line(repo, "go.mod", cs.FileTier.MANIFEST, None),
            _file_line(repo, "main.go"),
            _file_line(repo, "geo/circle.go"),
            _file_line(repo, "geo/shape.go"),
            _file_line(repo, "geo/square.go"),
            'reparse\tgeo/circle.go\t{"reason":"new"}',
            'reparse\tgeo/shape.go\t{"reason":"dependent"}',
            'reparse\tgeo/square.go\t{"reason":"changed"}',
            'reparse\tmain.go\t{"reason":"dependent"}',
            "deleted\tzeta.go\t{}",
        ]
        index = _lines(handoffs / _INDEX)
        assert "definition\tdemo.geo.square.Square.Double\t" in "\n".join(index)
        assert not [line for line in index if "demo.zeta" in line]

    def test_a_run_already_in_sync_leaves_no_earlier_hand_offs(
        self, tmp_path: Path
    ) -> None:
        # Such a run never reaches either point, and the files left from the
        # run before would describe that run as this one.
        repo = _write(tmp_path / "repo", GO_FIXTURE)
        handoffs = tmp_path / "handoffs"
        with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
            updater = _updater(
                repo, ingestor, state_dir=tmp_path / "state", handoff_dir=handoffs
            )
            updater.run()
            assert {path.name for path in handoffs.iterdir()} == {_FILES, _INDEX}

            updater.run()

        assert updater.skipped_because_in_sync
        assert list(handoffs.iterdir()) == []


class TestANameThatIsNotUtf8:
    """Issue #3722: one such name ended the run where it was written out.

    Into the embedded graph without a hand-off directory, into `files.txt`
    with one. The walk now leaves the entry out, file or directory.
    """

    @pytest.mark.parametrize("with_hand_offs", [False, True])
    def test_the_run_completes_without_it(
        self, tmp_path: Path, with_hand_offs: bool
    ) -> None:
        repo = _write(tmp_path / "repo", CALLER_FIXTURE)
        _make_undecodable(repo, b"notes\xff.txt", "notes\n")
        _make_undecodable(repo / "pkg", b"bad\xff.py", "def g():\n    return 2\n")
        folder = _make_undecodable(repo, b"dir\xff")
        (folder / "c.py").write_text("def c():\n    return 3\n", encoding="utf-8")
        handoffs = tmp_path / "handoffs" if with_hand_offs else None
        with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
            _updater(
                repo, ingestor, state_dir=tmp_path / "state", handoff_dir=handoffs
            ).run()
            files = {row["path"] for row in ingestor.fetch_all(_FILE_PATHS)}
            folders = {row["path"] for row in ingestor.fetch_all(_FOLDER_PATHS)}
            packages = {row["path"] for row in ingestor.fetch_all(_PACKAGE_PATHS)}

        indexed = ["main.py", "pkg/__init__.py", "pkg/util.py"]
        assert sorted(files) == indexed
        assert folders == set()
        assert packages == {"pkg"}
        if handoffs is not None:
            planned = [line.split("\t")[1] for line in _lines(handoffs / _FILES)[1:]]
            assert sorted(planned) == indexed

    @pytest.mark.parametrize("with_hand_offs", [False, True])
    @pytest.mark.parametrize("language", sorted(_IMPORTED_FROM_A_BAD_NAME))
    def test_a_package_under_it_is_never_resolved_into(
        self, tmp_path: Path, language: str, with_hand_offs: bool
    ) -> None:
        # Each language's resolver finds packages, modules or source roots
        # by a listing of its own, so an import of one under such a folder
        # still carried its name into the graph or the project index.
        importer, package = _IMPORTED_FROM_A_BAD_NAME[language]
        repo = _write(tmp_path / "repo", importer)
        _write(_make_undecodable(repo, b"bad\xff"), package)
        handoffs = tmp_path / "handoffs" if with_hand_offs else None
        with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
            _updater(
                repo, ingestor, state_dir=tmp_path / "state", handoff_dir=handoffs
            ).run()
            files = {row["path"] for row in ingestor.fetch_all(_FILE_PATHS)}
            names = ingestor.fetch_all(_NODE_NAMES)

        assert files == set(importer)
        assert not [
            row
            for row in names
            if any(_UNDECODED_BYTE in str(value) for value in row.values())
        ]
        if handoffs is not None:
            for written in sorted(handoffs.iterdir()):
                written.read_bytes().decode("utf-8")


class TestTheProjectIndex:
    def test_it_holds_the_modules_definitions_names_imports_and_parents(
        self, tmp_path: Path
    ) -> None:
        repo = _write(tmp_path / "repo", GO_FIXTURE)
        handoffs = tmp_path / "handoffs"
        with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
            _updater(
                repo, ingestor, state_dir=tmp_path / "state", handoff_dir=handoffs
            ).run()

        # Every line sorted. A module with no imports and a type with no
        # parents hold no fact, so they have no `imports` or `inherits` line;
        # Go's embedded struct is not a parent, its embedded interface is.
        assert _lines(handoffs / _INDEX) == [
            "# cgr handoff index.txt 1",
            'definition\tdemo.geo.shape.Named\t{"label":"Interface"}',
            'definition\tdemo.geo.shape.Named.Name\t{"label":"Method"}',
            'definition\tdemo.geo.shape.Shape\t{"label":"Interface"}',
            'definition\tdemo.geo.shape.Shape.Area\t{"label":"Method"}',
            'definition\tdemo.geo.square.Base\t{"label":"Class"}',
            'definition\tdemo.geo.square.Square\t{"label":"Class"}',
            'definition\tdemo.geo.square.Square.Area\t{"label":"Method"}',
            'definition\tdemo.geo.square.Square.Name\t{"label":"Method"}',
            'definition\tdemo.main.describe\t{"label":"Function"}',
            'definition\tdemo.main.main\t{"label":"Function"}',
            'definition\tdemo.zeta.zeta\t{"label":"Function"}',
            'imports\tdemo.main\t{"geo":"demo.geo"}',
            'inherits\tdemo.geo.shape.Named\t["demo.geo.shape.Shape"]',
            'module\tdemo.geo.shape\t{"language":"go","path":"geo/shape.go"}',
            'module\tdemo.geo.square\t{"language":"go","path":"geo/square.go"}',
            'module\tdemo.main\t{"language":"go","path":"main.go"}',
            'module\tdemo.zeta\t{"language":"go","path":"zeta.go"}',
            'simple_name\tArea\t["demo.geo.shape.Shape.Area","demo.geo.square.Square.Area"]',
            'simple_name\tBase\t["demo.geo.square.Base"]',
            'simple_name\tName\t["demo.geo.shape.Named.Name","demo.geo.square.Square.Name"]',
            'simple_name\tNamed\t["demo.geo.shape.Named"]',
            'simple_name\tShape\t["demo.geo.shape.Shape"]',
            'simple_name\tSquare\t["demo.geo.square.Square"]',
            'simple_name\tdescribe\t["demo.main.describe"]',
            'simple_name\tmain\t["demo.main.main"]',
            'simple_name\tzeta\t["demo.zeta.zeta"]',
        ]

    @pytest.mark.parametrize("reused", [False, True], ids=["new", "reused"])
    @pytest.mark.parametrize("edited", sorted(_DOCUMENTS_EDITED))
    def test_an_incremental_run_holds_what_a_fresh_index_holds(
        self, tmp_path: Path, edited: str, reused: bool
    ) -> None:
        # Issue #3790: the module map was seeded from every Module the graph
        # holds, and the registry from every definition, so a sync listed the
        # Markdown files and the script the run did not re-parse, which a
        # fresh index of the tree never registers. A sync starts a new
        # updater, as `cgr` does, or reuses one, as the watcher does.
        repo = _write(tmp_path / "repo", DOCUMENTS_FIXTURE)
        synced = tmp_path / "synced"
        with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
            # The checkout's own state directory, which the stamp reads.
            updater = _updater(repo, ingestor, state_dir=None, handoff_dir=synced)
            updater.run()
            (repo / edited).write_text(_DOCUMENTS_EDITED[edited], encoding="utf-8")
            _stamp_after_cache(repo, edited)
            if not reused:
                updater = _updater(repo, ingestor, state_dir=None, handoff_dir=synced)
            updater.run()
        fresh = tmp_path / "fresh"
        with _embedded_graph(tmp_path / "fresh.sqlite") as ingestor:
            _updater(
                repo, ingestor, state_dir=tmp_path / "fresh-state", handoff_dir=fresh
            ).run(force=True)

        # The sync was incremental: a full build names no file it re-parses.
        assert f'reparse\t{edited}\t{{"reason":"changed"}}' in _lines(synced / _FILES)
        assert _lines(synced / _INDEX) == _lines(fresh / _INDEX)


class TestTheFormat:
    @pytest.mark.parametrize("name", [_FILES, _INDEX])
    def test_each_line_is_a_kind_a_key_and_canonical_json(
        self, name: str, tmp_path: Path
    ) -> None:
        repo = _write(tmp_path / "repo", GO_FIXTURE | UNICODE_FIXTURE)
        handoffs = tmp_path / "handoffs"
        with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
            _updater(
                repo, ingestor, state_dir=tmp_path / "state", handoff_dir=handoffs
            ).run()

        data = (handoffs / name).read_bytes()
        # UTF-8 with `\n` line ends on every system, names left unescaped.
        assert b"\r" not in data
        assert "grüße".encode() in data
        assert b"\\u" not in data
        header, *lines = _lines(handoffs / name)
        assert header == f"# cgr handoff {name} 1"
        assert lines
        for line in lines:
            _kind, _key, value = line.split("\t")
            assert canonical_json(json.loads(value)) == value, line


def test_the_same_bytes_at_another_path_under_another_seed(tmp_path: Path) -> None:
    fixtures = {str(language): files for language, files in LANGUAGE_FIXTURES.items()}
    fixtures |= {"go_module": GO_FIXTURE, "unicode": UNICODE_FIXTURE}
    first, second = tmp_path / "first", tmp_path / "second" / "elsewhere"
    for root in (first, second):
        for name, files in fixtures.items():
            _write(root / "repos" / name, files)
    # Unset, the parent's seed is random; set, it is that one. Either way the
    # child's differs, which its probe shows.
    seed = "2" if os.environ.get(cs.REFERENCE_SEED_ENV) == "1" else "1"

    _write_hand_offs(first)
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(second), _SEED_PROBE],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            **{name: str(value) for name, value in _FRONTENDS.items()},
            cs.REFERENCE_SEED_ENV: seed,
            cs.REFERENCE_HOME_ENV: str(tmp_path / "child-home"),
        },
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-4000:]
    assert (second / "probe.txt").read_text(encoding="utf-8") != str(hash(_SEED_PROBE))
    for name in fixtures:
        for file in (_FILES, _INDEX):
            here = (first / "handoffs" / name / file).read_bytes()
            there = (second / "handoffs" / name / file).read_bytes()
            assert here == there, (name, file)
        # Not two empty files: every fixture's files become modules.
        assert b"\nmodule\t" in (first / "handoffs" / name / _INDEX).read_bytes()


class TestWithoutADirectory:
    def test_nothing_is_written_or_built(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writers = {
            name: MagicMock()
            for name in ("clear_handoffs", "write_file_plan", "write_project_index")
        }
        for name, writer in writers.items():
            monkeypatch.setattr(gu, name, writer)
        repo = _write(tmp_path / "repo", GO_FIXTURE)
        with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
            updater = _updater(
                repo, ingestor, state_dir=tmp_path / "state", handoff_dir=None
            )
            updater.run()
            (repo / "zeta.go").unlink()
            updater.run()

        for name, writer in writers.items():
            assert not writer.called, name
        assert not [
            path for path in tmp_path.rglob("*") if path.name in {_FILES, _INDEX}
        ]


def _index_sequence(
    repo: Path, make: Callable[[_MockIngestor], GraphUpdater]
) -> list[list]:
    """A full index of the Go fixture, then an incremental one after an edit,
    an addition and a deletion: the ingestor calls of each."""
    shutil.rmtree(repo, ignore_errors=True)
    _write(repo, GO_FIXTURE)
    ingestor = _MockIngestor()
    updater = make(ingestor)
    updater.run()
    full = _calls(ingestor)
    ingestor.reset_mock()
    (repo / "geo" / "square.go").write_text(_SQUARE_EDITED, encoding="utf-8")
    (repo / "geo" / "circle.go").write_text(_CIRCLE, encoding="utf-8")
    (repo / "zeta.go").unlink()
    _stamp_after_cache(repo, "geo/square.go", "geo/circle.go")
    updater.run()
    return [full, _calls(ingestor)]


class TestTheSameGraph:
    @pytest.mark.parametrize("language", list(LANGUAGE_FIXTURES))
    def test_a_full_index_with_and_without_hand_offs(
        self,
        language: cs.SupportedLanguage,
        tmp_path: Path,
        fingerprint_inputs_once_per_session: None,
    ) -> None:
        repo = _write(tmp_path / "repo", LANGUAGE_FIXTURES[language])

        def index(handoff_dir: Path | None) -> list:
            ingestor = _MockIngestor()
            _updater(
                repo,
                ingestor,
                state_dir=tmp_path / f"state-{handoff_dir is None}",
                handoff_dir=handoff_dir,
            ).run()
            return _calls(ingestor)

        without = index(None)

        assert index(tmp_path / "handoffs") == without
        assert (tmp_path / "handoffs" / _INDEX).is_file()

    def test_an_incremental_index_with_and_without_hand_offs(
        self, tmp_path: Path, fingerprint_inputs_once_per_session: None
    ) -> None:
        # The same checkout both times, so its paths in the calls are too;
        # each sequence starts from a state directory of its own.
        repo = tmp_path / "repo"
        handoffs = tmp_path / "handoffs"

        def make(handoff_dir: Path | None) -> Callable[[_MockIngestor], GraphUpdater]:
            def made(ingestor: _MockIngestor) -> GraphUpdater:
                return _updater(
                    repo,
                    ingestor,
                    state_dir=None,
                    handoff_dir=handoff_dir,
                )

            return made

        without = _index_sequence(repo, make(None))
        shutil.rmtree(checkout_state_dir(repo), ignore_errors=True)

        assert _index_sequence(repo, make(handoffs)) == without
        assert "deleted\tzeta.go\t{}" in _lines(handoffs / _FILES)
