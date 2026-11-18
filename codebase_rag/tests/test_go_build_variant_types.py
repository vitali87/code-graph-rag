"""A Go type each build declares again keeps its methods, the same way every run (#3148).

System-level Go declares one type per platform (`ztypes_linux.go`,
`ztypes_darwin.go`, ...) with its methods in matching `syscall_<os>.go`
files. The methods attached to whichever copy a set yielded first, so the
graph changed between runs; copies of a method on one line of different files
merged into one node; and `var v Iovec; v.SetLen()` bound to `Cmsghdr.SetLen`
when the copy the receiver resolved to declared no `SetLen`. Every case
indexes the issue's layout through the real parsers on the tree-sitter path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyValue
from evals.cgr_graph import _StatefulIngestor

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT = "bt"
_GOOS = ("linux", "darwin", "freebsd")
_CALLER = f"{_PROJECT}.sys.readv.appendBytes"
# Hash seeds the cross-process check indexes under. Set and dict order follows
# the seed, which is what moved a method between copies from run to run.
_HASH_SEEDS = ("0", "1", "2", "3")
# A deadline for one indexing subprocess, not a performance assertion.
_SUBPROCESS_TIMEOUT_SECONDS = 300

_TYPES = """package sys

type Iovec struct{ Len uint64 }

type Cmsghdr struct{ Len uint64 }

type Msghdr struct{ Iovlen uint64 }
"""

_METHODS = """package sys

func (iov *Iovec) SetLen(length int) { iov.Len = uint64(length) }

func (cmsg *Cmsghdr) SetLen(length int) { cmsg.Len = uint64(length) }

func (msghdr *Msghdr) SetIovlen(length int) { msghdr.Iovlen = uint64(length) }
"""

_READV = """package sys

func appendBytes(bs [][]byte) []Iovec {
\tvar vecs []Iovec
\tfor _, b := range bs {
\t\tvar v Iovec
\t\tv.SetLen(len(b))
\t\tvecs = append(vecs, v)
\t}
\treturn vecs
}
"""

# Indexes each tree named on the command line under this process's hash seed
# and writes every node and edge, with its properties, to the first argument.
# The project's sync time is the clock's, the one property no seed decides.
# No embedding pass: this process has none of pytest's vector-store isolation.
_DUMP = """
import json
import sys
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

out, roots = Path(sys.argv[1]), sys.argv[2:]
parsers, queries = load_parsers()
graphs = []
for root in roots:
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=Path(root),
        parsers=parsers,
        queries=queries,
        project_name="bt",
        skip_embeddings=True,
    ).run(force=True)
    store.flush_all()
    graphs.append(
        {
            "nodes": sorted(
                json.dumps(
                    [
                        label,
                        uid,
                        {k: v for k, v in props.items() if k != cs.KEY_LAST_SYNCED_AT},
                    ],
                    sort_keys=True,
                    default=str,
                )
                for (label, uid), props in store.nodes.items()
            ),
            "edges": sorted(
                json.dumps([*edge, store.props_for(edge)], sort_keys=True, default=str)
                for edge in store.keyed_edges
            ),
        }
    )
out.write_text(json.dumps(graphs), encoding="utf-8")
"""


@pytest.fixture(autouse=True)
def _pin_treesitter(monkeypatch: pytest.MonkeyPatch) -> None:
    # go/types, where the local Go can build its helper, binds the call by
    # itself; the tree-sitter path is the one these facts are about.
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)


def _write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir()
    (root / "go.mod").write_text(
        "module example.com/bt\n\ngo 1.22\n", encoding=cs.ENCODING_UTF8
    )
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=cs.ENCODING_UTF8)
    return root


def _for_build(goos: str, source: str) -> str:
    # Puts `SetLen` on line 5, where the issue's copies collided.
    return f"//go:build {goos}\n\n{source}"


def _per_build_layout(root: Path, method_goos: tuple[str, ...] = _GOOS) -> Path:
    # The issue's tree: every GOOS declares the types, `method_goos` declare
    # their methods, and `readv.go`, in every build, calls one.
    files = {f"sys/ztypes_{goos}.go": _for_build(goos, _TYPES) for goos in _GOOS}
    files |= {
        f"sys/syscall_{goos}.go": _for_build(goos, _METHODS) for goos in method_goos
    }
    files["sys/readv.go"] = _READV
    return _write(root, files)


def _shared_type_layout(root: Path) -> Path:
    # One declaration of each type, and every GOOS declaring its methods at
    # the same line and column of its own file.
    files = {"sys/types.go": _TYPES, "sys/readv.go": _READV}
    files |= {f"sys/syscall_{goos}.go": _for_build(goos, _METHODS) for goos in _GOOS}
    return _write(root, files)


def _index(root: Path) -> _StatefulIngestor:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.GO not in parsers:
        pytest.skip("go parser not available")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=_PROJECT,
        skip_embeddings=True,
    ).run(force=True)
    store.flush_all()
    return store


def _methods(store: _StatefulIngestor) -> dict[str, tuple[str, PropertyValue]]:
    # Method qn -> the file and line declaring it.
    return {
        str(uid): (str(props[cs.KEY_PATH]), props[cs.KEY_START_LINE])
        for (label, uid), props in store.nodes.items()
        if label == cs.NodeLabel.METHOD
    }


def _edges(store: _StatefulIngestor, rel: cs.RelationshipType) -> set[tuple[str, str]]:
    return {
        (str(edge[1]), str(edge[4])) for edge in store.keyed_edges if edge[2] == rel
    }


def _callees(store: _StatefulIngestor) -> set[str]:
    return {
        callee
        for caller, callee in _edges(store, cs.RelationshipType.CALLS)
        if caller == _CALLER
    }


def _receiver_type(method_qn: str) -> str:
    # `bt.sys.ztypes_linux.Iovec.SetLen@5` -> `Iovec`.
    return method_qn.rsplit(cs.SEPARATOR_DOT, 2)[-2]


def test_every_build_copy_keeps_its_methods(tmp_path: Path) -> None:
    store = _index(_per_build_layout(tmp_path / "bt"))
    methods = _methods(store)
    # Nine declarations, nine nodes, each at its own file and line: a node
    # two declarations merged into would leave eight distinct places.
    assert len(methods) == 9, methods
    assert len(set(methods.values())) == 9, methods
    owners = {
        method: owner
        for owner, method in _edges(store, cs.RelationshipType.DEFINES_METHOD)
        if method in methods
    }
    for method, (path, _line) in methods.items():
        # `syscall_linux.go`'s methods sit on `ztypes_linux.go`'s copy, the
        # one the linux build compiles them with, whatever the hash order.
        goos = Path(path).stem.rpartition("_")[2]
        assert owners[method].startswith(f"{_PROJECT}.sys.ztypes_{goos}."), (
            method,
            owners[method],
        )
        assert method.startswith(f"{owners[method]}{cs.SEPARATOR_DOT}"), method


def test_a_typed_receiver_binds_its_own_types_method(tmp_path: Path) -> None:
    store = _index(_per_build_layout(tmp_path / "bt"))
    callees = _callees(store)
    assert callees, "v.SetLen bound nothing"
    assert {_receiver_type(qn) for qn in callees} == {"Iovec"}, callees


def test_a_copy_without_the_method_reaches_the_copy_that_has_it(
    tmp_path: Path,
) -> None:
    # Only the linux build declares the methods. `Iovec` has three copies and
    # the receiver resolves to one of them; when that copy holds no `SetLen`,
    # the call still names Iovec's, never `Cmsghdr.SetLen`, the same-named
    # method of another type that the name-only fallback picked.
    store = _index(_per_build_layout(tmp_path / "bt", ("linux",)))
    assert _callees(store) == {f"{_PROJECT}.sys.ztypes_linux.Iovec.SetLen"}


def test_another_copys_method_counts_only_where_the_callers_build_reaches_it(
    tmp_path: Path,
) -> None:
    # FreeBSD and Linux both declare the methods; the caller is Linux-only.
    # FreeBSD's copy sorts first, but no build compiles it with the caller,
    # so the call takes Linux's.
    root = _per_build_layout(tmp_path / "bt", ("freebsd", "linux"))
    (root / "sys" / "use_linux.go").write_text(
        "package sys\n\nfunc useLinux() {\n\tvar v Iovec\n\tv.SetLen(1)\n}\n",
        encoding=cs.ENCODING_UTF8,
    )
    store = _index(root)
    callees = {
        callee
        for caller, callee in _edges(store, cs.RelationshipType.CALLS)
        if caller == f"{_PROJECT}.sys.use_linux.useLinux"
    }
    assert callees == {f"{_PROJECT}.sys.ztypes_linux.Iovec.SetLen"}, callees


def test_a_python_receiver_never_reaches_a_go_types_copies(tmp_path: Path) -> None:
    # The copies of a Go type are a Go caller's business. A Python local
    # typed by the same bare name binds no Go method, as before.
    root = _per_build_layout(tmp_path / "bt", ("linux",))
    (root / "use.py").write_text(
        "def annotated(v: Iovec):\n    v.SetLen(1)\n\n\n"
        "def built():\n    v = Iovec()\n    v.SetLen(1)\n",
        encoding=cs.ENCODING_UTF8,
    )
    store = _index(root)
    reached = {
        callee
        for caller, callee in _edges(store, cs.RelationshipType.CALLS)
        if caller.startswith(f"{_PROJECT}.use.")
    }
    assert not any(callee.startswith(f"{_PROJECT}.sys.") for callee in reached), reached


def test_one_types_methods_on_one_line_of_several_files_stay_apart(
    tmp_path: Path,
) -> None:
    store = _index(_shared_type_layout(tmp_path / "bt"))
    methods = _methods(store)
    assert len(methods) == 9, methods
    assert sorted(path for path, _line in methods.values()) == sorted(
        f"sys/syscall_{goos}.go" for goos in _GOOS for _ in range(3)
    ), methods
    callees = _callees(store)
    assert callees, "v.SetLen bound nothing"
    assert {_receiver_type(qn) for qn in callees} == {"Iovec"}, callees


class _DumpedGraph(TypedDict):
    # One tree's graph as `_DUMP` writes it: each node and edge a JSON row.
    nodes: list[str]
    edges: list[str]


def _graphs_under_seed(seed: str, out: Path, roots: list[Path]) -> list[_DumpedGraph]:
    result = subprocess.run(
        [sys.executable, "-c", _DUMP, str(out), *map(str, roots)],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "PYTHONHASHSEED": seed,
            "GO_FRONTEND": cs.GoFrontend.TREESITTER.value,
        },
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(out.read_text(encoding=cs.ENCODING_UTF8))


def test_the_graph_is_the_same_under_every_hash_seed(tmp_path: Path) -> None:
    # One process shares one hash seed, so indexing twice in it cannot see a
    # choice that follows set order (`test_graph_determinism.py` says as
    # much). Each seed gets its own process.
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")
    # Each tree with the Method nodes its declarations make.
    trees = {
        _per_build_layout(tmp_path / "every_build"): 9,
        _per_build_layout(tmp_path / "linux_only", ("linux",)): 3,
        _shared_type_layout(tmp_path / "shared_type"): 9,
    }
    graphs = {
        seed: _graphs_under_seed(seed, tmp_path / f"seed{seed}.json", list(trees))
        for seed in _HASH_SEEDS
    }
    first, *rest = _HASH_SEEDS
    for seed in rest:
        assert graphs[seed] == graphs[first], f"seed {seed} differs from {first}"
    # And the graph they agree on is the right one: every method has a node,
    # and no call reaches another type's `SetLen`.
    for graph, method_count in zip(graphs[first], trees.values(), strict=True):
        nodes = [json.loads(row) for row in graph["nodes"]]
        edges = [json.loads(row) for row in graph["edges"]]
        methods = [row for row in nodes if row[0] == cs.NodeLabel.METHOD]
        calls = [
            row
            for row in edges
            if row[2] == cs.RelationshipType.CALLS and row[1] == _CALLER
        ]
        assert len(methods) == method_count, methods
        assert calls, edges
        assert {_receiver_type(str(row[4])) for row in calls} == {"Iovec"}, calls
