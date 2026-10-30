"""`FileStateSweep`: forgetting what one file's last parse registered.

Each test indexes a real temporary repository, then sweeps one file through
a `FileStateSweep` built from that run's registry and asks what is left to
resolve into. Ownership comes from records, never from a qn's shape: a
sibling sharing the file's stem keeps its definitions (#1773), a Go method
keyed under another file's module still goes with its own file (#1659), a
non-source file owns nothing (#2463), and frontend registrations are
attributed per file (#1025).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import SimpleNameLookup
from codebase_rag.updater.file_state import FileStateSweep
from evals.cgr_graph import StatefulIngestor

_SOURCES = {
    "a.py": "def top():\n    return 1\n\n\nclass K:\n    def m(self):\n        pass\n",
    # Its module `proj.a.b` nests under `a.py`'s `proj.a`.
    "a/b.py": (
        "class Nested:\n    def deep(self):\n        return 2\n\n\n"
        "def helper():\n    pass\n"
    ),
    "shapes.py": "def area():\n    return 0\n",
    "shapes.txt": "not source\n",
    "go.mod": "module example.com/proj\n\ngo 1.21\n",
    "pkg/types.go": "package pkg\n\ntype T struct{}\n",
    # Keyed `proj.pkg.types.T.M`, under the module that declares `T`.
    "pkg/methods.go": "package pkg\n\nfunc (t T) M() int { return 1 }\n",
}


@dataclass
class _Indexed:
    repo: Path
    updater: GraphUpdater
    frontend_owned: dict[str, set[str]]
    rehydrated: set[str]

    @property
    def registry(self) -> FunctionRegistryTrie:
        return self.updater.function_registry

    @property
    def names(self) -> SimpleNameLookup:
        return self.updater.simple_name_lookup

    @property
    def module_map(self) -> dict[str, Path]:
        return self.updater.factory.definition_processor.module_qn_to_file_path

    def sweep(self, key: str, frontend_current: bool = False) -> None:
        FileStateSweep(
            factory=self.updater.factory,
            function_registry=self.registry,
            simple_name_lookup=self.names,
            repo_path=self.repo,
            project_name="proj",
            frontend_owned_qns=self.frontend_owned,
            rehydrated_module_qns=self.rehydrated,
        ).sweep(self.repo / key, frontend_current)

    def qns(self) -> set[str]:
        return set(self.registry.keys())


@pytest.fixture
def indexed(temp_repo: Path, monkeypatch: pytest.MonkeyPatch) -> _Indexed:
    # Tree-sitter's Go pass alone: the go/types helper is not what is tested.
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    for key, text in _SOURCES.items():
        path = temp_repo / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=StatefulIngestor(),
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
        skip_embeddings=True,
    )
    updater.run(force=True)
    indexed = _Indexed(temp_repo, updater, {}, set())
    assert indexed.qns() == {
        "proj.a.top",
        "proj.a.K",
        "proj.a.K.m",
        "proj.a.b.Nested",
        "proj.a.b.Nested.deep",
        "proj.a.b.helper",
        "proj.shapes.area",
        "proj.pkg.types.T",
        "proj.pkg.types.T.M",
    }, "fixture guard: the index must register every definition"
    return indexed


def test_a_swept_module_leaves_nothing_to_resolve_into(indexed: _Indexed) -> None:
    indexed.rehydrated.update({"proj.a", "proj.shapes"})

    indexed.sweep("a.py")

    assert not indexed.qns() & {"proj.a.top", "proj.a.K", "proj.a.K.m"}
    for name in ("top", "K", "m"):
        assert not indexed.names[name], name
    assert "proj.a" not in indexed.module_map
    # The read-back qn goes too, or deferred resolution keeps offering it.
    assert indexed.rehydrated == {"proj.shapes"}
    assert "proj.shapes.area" in indexed.qns()


def test_a_directory_sharing_the_files_stem_keeps_its_definitions(
    indexed: _Indexed,
) -> None:
    # `proj.a.b.Nested` records no span: the class owner record saves it.
    indexed.sweep("a.py")

    assert {
        "proj.a.b.Nested",
        "proj.a.b.Nested.deep",
        "proj.a.b.helper",
    } <= indexed.qns()
    assert indexed.names["Nested"] == {"proj.a.b.Nested"}
    assert indexed.module_map["proj.a.b"] == indexed.repo / "a" / "b.py"


@pytest.mark.parametrize("recorded", [True, False], ids=["recorded", "read-back"])
def test_a_non_source_file_beside_a_source_owns_nothing(
    indexed: _Indexed, recorded: bool
) -> None:
    # `shapes.txt`'s path-derived qn is `shapes.py`'s module. A fresh
    # updater's registry is read back from the graph with no module recorded
    # for `shapes.py`, and only the file's language then tells them apart.
    if not recorded:
        del indexed.module_map["proj.shapes"]
    before = indexed.qns()

    indexed.sweep("shapes.txt")

    assert indexed.qns() == before
    assert indexed.names["area"] == {"proj.shapes.area"}
    # The source file itself is swept by the same qn.
    indexed.sweep("shapes.py")
    assert "proj.shapes.area" not in indexed.qns()


def test_a_go_method_goes_with_the_file_that_declares_it(indexed: _Indexed) -> None:
    indexed.sweep("pkg/methods.go")

    assert "proj.pkg.types.T.M" not in indexed.qns()
    assert "proj.pkg.types.T" in indexed.qns()


def test_a_go_method_stays_when_its_receiver_types_file_goes(
    indexed: _Indexed,
) -> None:
    # Its qn sits under `types.go`'s module; its span names `methods.go`.
    indexed.sweep("pkg/types.go")

    assert "proj.pkg.types.T" not in indexed.qns()
    assert "proj.pkg.types.T.M" in indexed.qns()


def test_another_files_frontend_registration_is_spared(indexed: _Indexed) -> None:
    # A header's libclang registration under the source file's prefix.
    indexed.frontend_owned["a.h"] = {"proj.a.top"}

    indexed.sweep("a.py")

    assert "proj.a.top" in indexed.qns()
    assert "proj.a.K" not in indexed.qns()


@pytest.mark.parametrize("frontend_current", [False, True])
def test_the_files_own_frontend_registration_stays_only_when_current(
    indexed: _Indexed, frontend_current: bool
) -> None:
    # A LIBCLANG run has already re-registered the file and skips its parse;
    # a watch event re-runs no frontend, so the stale entry must go.
    indexed.frontend_owned["a.py"] = {"proj.a.top"}

    indexed.sweep("a.py", frontend_current=frontend_current)

    assert ("proj.a.top" in indexed.qns()) is frontend_current


def test_a_never_parsed_source_file_sweeps_its_path_derived_module(
    indexed: _Indexed,
) -> None:
    # A fresh updater's registry is read back from the graph, with no module
    # recorded for the file: the path-derived qn is the only prefix there is.
    indexed.registry["proj.c.f"] = cs.NodeLabel.FUNCTION
    indexed.names["f"].add("proj.c.f")

    indexed.sweep("c.py")

    assert "proj.c.f" not in indexed.qns()
    assert not indexed.names["f"]


def test_a_never_parsed_file_spares_a_qn_another_file_now_holds(
    indexed: _Indexed,
) -> None:
    # A same-stem sibling parsed earlier this run took the bare qn (#2586).
    indexed.registry["proj.c.f"] = cs.NodeLabel.FUNCTION
    indexed.module_map["proj.c"] = indexed.repo / "c.h"

    indexed.sweep("c.py")

    assert "proj.c.f" in indexed.qns()
    assert indexed.module_map["proj.c"] == indexed.repo / "c.h"
