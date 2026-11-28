"""A sync with FLOWS_TO captured resolves calls as a fresh index does (#3818).

With the `io` group on, the sync reads each changed file's resource flows
before deleting it, and building the call processor for that read happens
before the definition pass fills the run's maps. The processor replaced each
map that was still empty with a private one of its own, so the writes that
followed never reached it: Go calls lost their package clauses (`exact`
became `heuristic`, or a `_test` package's call bound as its production
package's), a Python function defined twice lost its suffixed name, and a
C++ method defined out of its class lost the class its definition named.
Each sync here must leave the graph a fresh index of the edited repository
writes, edge for edge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.canonical_graph import canonical, difference
from codebase_rag.capture import resolve_capture
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import _MockIngestor
from codebase_rag.tests.test_handoff import _FRONTENDS, _embedded_graph

L = cs.SupportedLanguage
_PROJECT = "demo"
_IO = resolve_capture([cs.CaptureGroup.IO])
_MODULE = "module example.com/m\n\ngo 1.22\n"

# Each corner: its language, the repository's files, then the edit (None
# deletes a file).
type Corner = tuple[L, dict[str, str], dict[str, str | None]]

_CALLER = (
    'package q\n\nimport "example.com/m/p"\n\n'
    "func Q() int {\n\tt := p.New()\n\treturn t.N() + p.Use(t)\n}\n"
)
_PACKAGE = {
    "go.mod": _MODULE,
    "p/t.go": "package p\n\ntype T struct{ n int }\n\nfunc New() *T { return &T{} }\n",
    "p/m.go": (
        "package p\n\nfunc (t *T) M() int { return t.n }\n\n"
        "func (t *T) N() int { return t.M() }\n"
    ),
    "p/i.go": (
        "package p\n\ntype I interface {\n\tM() int\n}\n\n"
        "func Use(i I) int { return i.M() }\n"
    ),
    "q/q.go": _CALLER,
    "z/z.go": (
        "package z\n\ntype Z struct{}\n\nfunc (Z) M() int { return 3 }\n\n"
        "func NewZ() Z { return Z{} }\n"
    ),
}
_SHAPE = "package p\n\ntype S struct{}\n\nfunc (S) Do() int { return 1 }\n"
_TWICE = (
    "import os\n\n\ndef g():\n    return 1\n\n\ndef h():\n    return 2\n\n\n"
    "if os.name:\n\n    def f():\n        return g()\n\nelse:\n\n"
    "    def f():\n        return h()\n"
)
_NAMESAKES = (
    "namespace n1 {\nclass A {\npublic:\n  int f();\n};\n}\n"
    "namespace n2 {\nclass A {\npublic:\n  int f();\n  int g();\n};\n}\n"
)
_OUT_OF_CLASS = (
    '#include "a.h"\nint n1::A::f() { return 0; }\n'
    "int n2::A::g() { return 1; }\nint n2::A::f() { return g(); }\n"
)

CORNERS: dict[str, Corner] = {
    # The two the issue names, as the engine comparison writes them.
    "test_packages": (
        L.GO,
        {
            "go.mod": _MODULE,
            "p/x.go": _SHAPE,
            "p/x_test.go": (
                'package p\n\nimport "testing"\n\nfunc helper() S { return S{} }\n\n'
                "func TestDo(t *testing.T) { helper().Do() }\n"
            ),
            "p/ext_test.go": (
                'package p_test\n\nimport (\n\t"testing"\n\n\t"example.com/m/p"\n)\n\n'
                "func TestExt(t *testing.T) { p.S{}.Do() }\n"
            ),
        },
        {"p/x.go": _SHAPE + "\nfunc (S) Undo() {}\n"},
    ),
    "moved_file": (
        L.GO,
        {**_PACKAGE, "p/x.go": "package p\n\nfunc Helper() int { return 1 }\n"},
        {
            "p/x.go": None,
            "q/x.go": "package q\n\nfunc Helper() int { return 1 }\n",
            "q/q.go": _CALLER.replace("p.Use(t)", "Helper()"),
        },
    ),
    # The least that shows it: a call to a function in the caller's own
    # package, from the file the sync re-parses.
    "same_package_call": (
        L.GO,
        {
            "go.mod": _MODULE,
            "a/a.go": "package a\n\nfunc A() int { return B() }\n",
            "a/b.go": "package a\n\nfunc B() int { return 1 }\n",
        },
        {"a/a.go": "package a\n\nfunc A() int { return B() + 1 }\n"},
    ),
    # The other maps the processor held a copy of: each function's recorded
    # name, which a second `def f` is suffixed in ...
    "function_defined_twice": (
        L.PYTHON,
        {"m.py": _TWICE},
        {"m.py": _TWICE.replace("return h()", "return h() + 1")},
    ),
    # ... and the class each out-of-class C++ definition names, which the
    # caller's leaf name alone cannot tell from its namesake's.
    "out_of_class_method": (
        L.CPP,
        {"a.h": _NAMESAKES, "a.cpp": _OUT_OF_CLASS},
        {"a.cpp": _OUT_OF_CLASS.replace("return g();", "return g() + 1;")},
    ),
}


@pytest.fixture(autouse=True)
def _frontends_need_no_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _FRONTENDS.items():
        monkeypatch.setattr(settings, name, value)


def _write(repo: Path, files: dict[str, str | None]) -> None:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=cs.ENCODING_UTF8)


def _index(repo: Path, scratch: Path, force: bool) -> list[str]:
    """Run an updater on `repo` with the I/O group on, into the graph and
    state kept under `scratch`, and return the stored graph's lines."""
    parsers, queries = load_parsers()
    with _embedded_graph(scratch / "graph.sqlite") as ingestor:
        GraphUpdater(
            ingestor=ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            project_name=_PROJECT,
            skip_embeddings=True,
            state_dir=scratch / "state",
            capture=_IO,
        ).run(force=force)
        export = json.loads(json.dumps(ingestor.export_graph_to_dict()))
    return canonical(export, repo)


@pytest.mark.parametrize("corner", sorted(CORNERS))
def test_a_sync_with_flows_captured_equals_a_fresh_index(
    corner: str, tmp_path: Path
) -> None:
    language, files, edit = CORNERS[corner]
    if language not in load_parsers()[0]:
        pytest.skip(f"{language} parser not available")
    repo = tmp_path / "repo"
    _write(repo, files)
    _index(repo, tmp_path / "synced", force=True)
    _write(repo, edit)

    synced = _index(repo, tmp_path / "synced", force=False)
    fresh = _index(repo, tmp_path / "fresh", force=True)

    differs = difference(fresh, synced)
    assert not differs, "- a fresh index, + the sync:\n" + "\n".join(differs)


def test_the_call_processor_reads_the_runs_own_maps(tmp_path: Path) -> None:
    # Built before any pass has filled them, as the sync's flow read builds
    # it: every map it and the walks it owns read is the run's own object.
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=_MockIngestor(),
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=_PROJECT,
        skip_embeddings=True,
        state_dir=tmp_path / "state",
        capture=_IO,
    )
    factory = updater.factory
    definitions = factory.definition_processor
    calls = factory.call_processor
    modules = factory.module_qn_to_file_path
    clauses = definitions.go_package_names
    locations = definitions.function_locations
    assert not (modules or clauses or locations)

    held = {
        "module paths": [
            calls.module_qn_to_file_path,
            calls._io_processor._module_paths,
            calls._rpc_exposure._module_paths,
        ],
        "package clauses": [
            calls._go_package_names,
            calls._resolver.go_package_names,
            calls._io_processor._go_package_names,
            calls._rpc_exposure._go_package_names,
        ],
        "function locations": [
            calls.function_locations,
            calls._flow_processor._function_locations,
        ],
        "out-of-class methods": [calls.cpp_out_of_class_methods],
    }
    runs = {
        "module paths": modules,
        "package clauses": clauses,
        "function locations": locations,
        "out-of-class methods": definitions.cpp_out_of_class_methods,
    }
    copies = [
        f"{name} #{position}"
        for name, maps in held.items()
        for position, found in enumerate(maps)
        if found is not runs[name]
    ]
    assert not copies, f"held a copy of the run's map: {copies}"
