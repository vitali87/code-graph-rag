"""A bare C/C++ call never binds another translation unit's `static`.

A `static` function has internal linkage: only its own file, or one that
`#include`s it, can call it. The bare-call fallback ranked same-named
candidates by import distance and then by name, so `now()` in main.c bound
a.c's private `static now()` rather than the extern `now()` z.c defines
(redis: every cross-file `mstime()` landed on quicklist.c's static copy,
issue #3154).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import logs as ls
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    force_mtime_after_cache,
    get_relationships,
)
from codebase_rag.types_defs import PropertyDict, ResultRow
from evals.cgr_graph import _StatefulIngestor

_CSTATIC = {
    "a.c": (
        "/* a.c: a private helper with the same name */\n"
        "static long long now(void) { return 1; }\n\n"
        "long long a_tick(void) { return now(); }\n"
    ),
    "z.h": "#ifndef Z_H\n#define Z_H\nlong long now(void);\n#endif\n",
    "z.c": '#include "z.h"\n\nlong long now(void) { return 42; }\n',
    "main.c": '#include "z.h"\n\nint main(void) { return (int) now(); }\n',
}
_REDIS = {
    "src/sds.h": (
        "#ifndef SDS_H\n#define SDS_H\n"
        "static inline int sdslen(const char *s) { return s ? 1 : 0; }\n#endif\n"
    ),
    "src/server.h": (
        '#ifndef SERVER_H\n#define SERVER_H\n#include "sds.h"\n'
        "long long mstime(void);\n#endif\n"
    ),
    "src/server.c": '#include "server.h"\nlong long mstime(void) { return 1; }\n',
    "src/aof.c": (
        '#include "server.h"\n'
        "int aof(const char *s) { return sdslen(s) + (int) mstime(); }\n"
    ),
    "src/quicklist.c": (
        '#include "server.h"\n'
        "static long long mstime(void) { return 2; }\n"
        "int ql(void) { return (int) mstime(); }\n"
    ),
    "src/ae_epoll.c": "static int api_create(void) { return 1; }\n",
    "src/ae.c": (
        '#include "ae_epoll.c"\n\nint ae_create(void) { return api_create(); }\n'
    ),
}
_CPP = {
    "k.cpp": (
        "static int hidden() { return 1; }\n"
        "namespace ns { static int scoped() { return 2; } }\n"
        "struct S { static int sm() { return 3; } };\n"
        "int vis() { return hidden() + ns::scoped() + S::sm(); }\n"
    ),
    "l.cpp": "int hidden();\nint other() { return hidden(); }\n",
    "s.h": "#pragma once\nstruct T { static int tm(); };\n",
    "s.cpp": '#include "s.h"\nint T::tm() { return 4; }\n',
    "m.cpp": '#include "s.h"\nint user() { return T::tm(); }\n',
}

# Internal linkage the definition itself does not spell (#3154 review).
_INHERITED = {
    "c-static-prototype": (
        {
            "a.c": (
                "static long long now(void);\n"
                "long long now(void) { return 1; }\n"
                "long long a_tick(void) { return now(); }\n"
            ),
            "z.c": "long long now(void) { return 42; }\n",
            "main.c": "long long now(void);\nint main(void) { return (int) now(); }\n",
        },
        "c",
    ),
    "cpp-anonymous-namespace": (
        {
            "k.cpp": "namespace { int now() { return 1; } }\nint k() { return now(); }\n",
            "z.cpp": "int now() { return 42; }\n",
            "main.cpp": "int now();\nint main() { return now(); }\n",
        },
        "cpp",
    ),
    "cpp-static-template": (
        {
            "k.cpp": (
                "template <typename T> static int now(T) { return 1; }\n"
                "int k() { return now(1); }\n"
            ),
            "z.cpp": "int now(int) { return 42; }\n",
            "main.cpp": "int now(int);\nint main() { return now(1); }\n",
        },
        "cpp",
    ),
    # A named namespace's `static` declaration, even in an earlier block of
    # the namespace, is inherited by its definition there.
    "cpp-namespace-static-prototype": (
        {
            "k.cpp": (
                "namespace N { static int now(); }\n"
                "namespace N { int now() { return 1; } int k() { return now(); } }\n"
            ),
            "z.cpp": "namespace N { int now() { return 42; } }\n",
            "main.cpp": "namespace N { int now(); }\nint main() { return N::now(); }\n",
        },
        "cpp",
    ),
}
# C++ overloads are two functions: `static` on `f(int)` leaves `f(double)`
# extern, `n::k(int)` is not the file's `static k(int)`, nor the file's
# `m(int)` n's `static m(int)` (#3154 review). With the extern `p(double)`
# written first, the call binds plain `a.p`, whose overloads the edge then
# fans out to: the `static p(int)` among them is not b.cpp's to call.
_CPP_OVERLOADS = {
    "a.cpp": (
        "static void f(int);\n"
        "void f(double) {}\n"
        "static void h(int) {}\n"
        "void h(double) {}\n"
        "static void k(int);\n"
        "namespace n { void k(int) {} }\n"
        "namespace n { static void m(int); }\n"
        "void m(int) {}\n"
        "void p(double) {}\n"
        "static void p(int) {}\n"
    ),
    "b.cpp": (
        "void f(double);\nvoid h(double);\nnamespace n { void k(int); }\n"
        "void m(int);\nvoid p(double);\n"
        "void g() { f(1.0); h(1.0); n::k(1); m(1); p(1.0); }\n"
    ),
}
# A static's file is matched exactly, not as a qn prefix: main.h's module
# `main.h` and a/b.c's `a.b` both start with another file's (`main`, `a`).
_PREFIX = {
    "main.h": "static inline int helper(void) { return 1; }\n",
    "main.c": "int helper(void);\nint main(void) { return helper(); }\n",
    "other.c": "int helper(void) { return 7; }\n",
    "a.c": "int now(void);\nint caller(void) { return now(); }\n",
    "a/b.c": "static int now(void) { return 1; }\nint bb(void) { return now(); }\n",
    "z.c": "int now(void) { return 42; }\n",
}
# Two same-named headers: the include map keeps one binding per local name.
_SAME_STEM = {
    "a/util.h": "static inline int first(void) { return 1; }\n",
    "b/util.h": "static inline int second(void) { return 2; }\n",
    "main.c": (
        '#include "a/util.h"\n#include "b/util.h"\n'
        "int main(void) { return first() + second(); }\n"
    ),
}
# A header is compiled inside the file that includes it: util.h's `wrap()`
# reaches main.c's `static helper()` and a.h's `static inline early()`, which
# main.c defines and includes before util.h. lone.h is compiled only into
# user.c, which has neither.
_INCLUDER = {
    "a.h": "static inline int early(void) { return 2; }\n",
    "util.h": "static inline int wrap(void) { return helper() + early(); }\n",
    "main.c": (
        "static int helper(void) { return 1; }\n"
        '#include "a.h"\n#include "util.h"\n'
        "int main(void) { return wrap(); }\n"
    ),
    "lone.h": "static inline int solo(void) { return helper() + early(); }\n",
    "user.c": '#include "lone.h"\nint use(void) { return solo(); }\n',
}

_Calls = dict[tuple[str, str], str]


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _short(qn: object) -> str:
    return str(qn).split(".", 1)[1]


def _calls(root: Path, files: dict[str, str], grammar: str) -> _Calls:
    _write(root, files)
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing=grammar)
    return {
        (_short(c.args[0][2]), _short(c.args[2][2])): str(
            (c.kwargs.get("properties") or {}).get(cs.KEY_RESOLUTION)
        )
        for c in get_relationships(mock, cs.RelationshipType.CALLS)
    }


@pytest.fixture(scope="module")
def cstatic(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("c3154") / "cstatic", _CSTATIC, "c")


@pytest.fixture(scope="module")
def redis(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("r3154") / "redis", _REDIS, "c")


@pytest.fixture(scope="module")
def cpp(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("x3154") / "cxx", _CPP, "cpp")


def test_main_calls_the_extern_now_it_links_against(cstatic: _Calls) -> None:
    callees = {to for (src, to) in cstatic if src == "main.main"}
    assert callees == {"z.now"}, cstatic


def test_another_files_static_gets_no_cross_file_callers(
    cstatic: _Calls, redis: _Calls
) -> None:
    assert [src for (src, to) in cstatic if to == "a.now"] == ["a.a_tick"], cstatic
    assert redis.get(("src.aof.aof", "src.server.mstime")) == "heuristic", redis
    assert ("src.aof.aof", "src.quicklist.mstime") not in redis, redis


def test_a_cpp_file_scope_static_stays_in_its_file(cpp: _Calls) -> None:
    assert ("l.other", "k.hidden") not in cpp, cpp


@pytest.mark.parametrize(
    ("calls", "caller", "callee"),
    [
        ("cstatic", "a.a_tick", "a.now"),
        ("redis", "src.quicklist.ql", "src.quicklist.mstime"),
        ("redis", "src.ae.ae_create", "src.ae_epoll.api_create"),
        ("redis", "src.aof.aof", "src.sds.sdslen"),
        ("cpp", "k.vis", "k.hidden"),
        ("cpp", "k.vis", "k.ns.scoped"),
        ("cpp", "m.user", "s.h.T.tm"),
    ],
    ids=[
        "own-file",
        "own-file-redis",
        "included-c-file",
        "header-static-inline-via-another-header",
        "cpp-own-file",
        "cpp-namespace-static",
        "cpp-static-member",
    ],
)
def test_a_static_its_file_or_an_includer_can_reach_still_binds(
    request: pytest.FixtureRequest, calls: str, caller: str, callee: str
) -> None:
    # Negatives: the static's own file, a file that `#include`s the `.c` that
    # defines it (redis's ae.c / ae_epoll.c), a `static inline` in a header
    # reached through another header, and a C++ class's `static` member
    # (unrelated to linkage) all keep their edges.
    edges: _Calls = request.getfixturevalue(calls)
    assert (caller, callee) in edges, edges


@pytest.mark.parametrize(
    ("case", "own_caller", "private", "extern"),
    [
        ("c-static-prototype", "a.a_tick", "a.now", "z.now"),
        ("cpp-anonymous-namespace", "k.k", "k.now", "z.now"),
        ("cpp-static-template", "k.k", "k.now", "z.now"),
        # `k.N.now` is the static prototype, `k.N.now@2` its definition.
        ("cpp-namespace-static-prototype", "k.N.k", "k.N.now@2", "z.N.now"),
    ],
)
def test_a_static_the_definition_inherits_stays_in_its_file(
    tmp_path: Path, case: str, own_caller: str, private: str, extern: str
) -> None:
    # `static` on an earlier declaration or on the wrapped template, or an
    # anonymous namespace, makes the definition just as private.
    files, grammar = _INHERITED[case]
    calls = _calls(tmp_path / case, files, grammar)
    assert ("main.main", extern) in calls, calls
    assert ("main.main", private) not in calls, calls
    assert (own_caller, private) in calls, calls


def test_a_cpp_static_overload_leaves_the_extern_one_linkable(
    tmp_path: Path,
) -> None:
    # `a.f` is the `static f(int)` prototype, `a.f@2` the extern `f(double)`;
    # `a.p` is the extern `p(double)`, `a.p@10` the `static p(int)`.
    calls = _calls(tmp_path / "overloads", _CPP_OVERLOADS, "cpp")
    callees = {to for (src, to) in calls if src == "b.g"}
    assert callees == {"a.f@2", "a.h@4", "a.n.k", "a.m", "a.p"}, calls


def test_a_static_is_matched_to_its_file_not_a_qn_prefix(tmp_path: Path) -> None:
    calls = _calls(tmp_path / "prefix", _PREFIX, "c")
    assert ("main.main", "other.helper") in calls, calls
    assert ("main.main", "main.h.helper") not in calls, calls
    assert ("a.caller", "z.now") in calls, calls
    assert ("a.caller", "a.b.now") not in calls, calls
    assert ("a.b.bb", "a.b.now") in calls, calls


def test_a_header_a_same_named_include_displaced_still_reaches(
    tmp_path: Path,
) -> None:
    calls = _calls(tmp_path / "stem", _SAME_STEM, "c")
    assert ("main.main", "a.util.first") in calls, calls
    assert ("main.main", "b.util.second") in calls, calls


def _updater(root: Path, store: _StatefulIngestor) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store, repo_path=root, parsers=parsers, queries=queries
    )


def _sync(root: Path, store: _StatefulIngestor, force: bool) -> set[tuple[str, str]]:
    _updater(root, store).run(force=force)
    return {
        (_short(src), _short(dst))
        for _, src, rel, _, dst in store.edges
        if rel == cs.RelationshipType.CALLS.value
    }


@pytest.mark.parametrize(
    ("files", "edited", "private", "grammar"),
    [
        (_CSTATIC, "main.c", "a.now", "c"),
        (_CPP, "l.cpp", "k.hidden", "cpp"),
        (_INHERITED["c-static-prototype"][0], "main.c", "a.now", "c"),
        (_INHERITED["cpp-anonymous-namespace"][0], "main.cpp", "k.now", "cpp"),
        (
            _INHERITED["cpp-namespace-static-prototype"][0],
            "main.cpp",
            "k.N.now@2",
            "cpp",
        ),
        (_CPP_OVERLOADS, "b.cpp", "a.f", "cpp"),
        # An unchanged header's includes still lead to sds.h's static inline.
        (_REDIS, "src/aof.c", "src.quicklist.mstime", "c"),
    ],
    ids=[
        "c",
        "cpp",
        "c-static-prototype",
        "cpp-anonymous-namespace",
        "cpp-namespace-static-prototype",
        "cpp-overloads",
        "redis",
    ],
)
def test_an_incremental_sync_keeps_an_unchanged_files_static_private(
    tmp_path: Path, files: dict[str, str], edited: str, private: str, grammar: str
) -> None:
    # Only the caller's file is re-parsed: the static's linkage comes back
    # from the graph, so the edit binds what a fresh index does.
    if grammar not in load_parsers()[0]:
        pytest.skip(f"{grammar} parser not available")
    root = tmp_path / "repo"
    _write(root, files)
    store = _StatefulIngestor()
    fresh = _sync(root, store, force=True)

    _write(root, {edited: files[edited] + "\n"})
    after = _sync(root, store, force=False)

    caller = f"{edited.split('.', 1)[0].replace('/', '.')}."
    assert {e for e in after if e[0].startswith(caller)} == {
        e for e in fresh if e[0].startswith(caller)
    }, after
    assert not [e for e in after if e[0].startswith(caller) and e[1] == private]


def test_a_header_reaches_the_statics_of_the_file_that_includes_it(
    tmp_path: Path,
) -> None:
    calls = _calls(tmp_path / "includer", _INCLUDER, "c")
    assert ("util.wrap", "main.helper") in calls, calls
    assert ("util.wrap", "a.early") in calls, calls
    # Negative: no file compiling lone.h defines or includes either static.
    assert ("lone.solo", "main.helper") not in calls, calls
    assert ("lone.solo", "a.early") not in calls, calls


@pytest.mark.parametrize("edited", ["util.h", "lone.h"])
def test_an_incremental_sync_of_a_header_keeps_its_includers_statics(
    tmp_path: Path, edited: str
) -> None:
    # Only the header is re-parsed; the files that include it come back
    # from the graph and still decide what its calls may reach.
    if "c" not in load_parsers()[0]:
        pytest.skip("c parser not available")
    root = tmp_path / "repo"
    _write(root, _INCLUDER)
    store = _StatefulIngestor()
    fresh = _sync(root, store, force=True)
    _write(root, {edited: _INCLUDER[edited] + "\n"})
    after = _sync(root, store, force=False)
    assert after == fresh, (after, fresh)


# util.h is compiled into main.c two includes up, through mid.h, so its
# `wrap()` reaches main.c's `static helper()` and the `static inline early()`
# main.c includes first. Nothing calls `wrap()`: a sync of util.h re-parses
# mid.h, which includes it, and leaves main.c to the graph (issue #3802).
# lone.c includes neither, and a sync of util.h has no reason to read it.
_TWO_UP = {
    "early.h": "static inline int early(void) { return 2; }\n",
    "main.c": (
        "static int helper(void) { return 1; }\n"
        '#include "early.h"\n#include "mid.h"\n'
        "int main(void) { return 0; }\n"
    ),
    "mid.h": '#include "util.h"\n',
    "util.h": "static inline int wrap(void) { return helper() + early(); }\n",
    "lone.c": '#include "lone.h"\nint lone(void) { return 0; }\n',
    "lone.h": "int lone(void);\n",
}
_THREE_UP = {
    "main.c": (
        "static int helper(void) { return 1; }\n"
        '#include "top.h"\nint main(void) { return 0; }\n'
    ),
    "top.h": '#include "mid.h"\n',
    "mid.h": '#include "util.h"\n',
    "util.h": "static inline int wrap(void) { return helper(); }\n",
}
# b/early.h takes the local name `early` from a/early.h, so main.c's map
# names b/early.h alone; a/early.h is still compiled into main.c, and with it
# the `static inline leaf()` it includes, which util.h's `wrap()` calls.
_DISPLACED_UP = {
    "main.c": (
        '#include "a/early.h"\n#include "b/early.h"\n#include "mid.h"\n'
        "int main(void) { return 0; }\n"
    ),
    "a/early.h": '#include "leaf.h"\n',
    "a/leaf.h": "static inline int leaf(void) { return 1; }\n",
    "b/early.h": "static inline int other(void) { return 2; }\n",
    "mid.h": '#include "util.h"\n',
    "util.h": "static inline int wrap(void) { return leaf(); }\n",
}
# Two translation units compile util.h, each with a `static helper()` of its
# own: a fresh index binds the call to a.c's, and a sync that found b.c alone
# would bind it to b.c's.
_TWO_UNITS = {
    "a.c": (
        "static int helper(void) { return 1; }\n"
        '#include "mid.h"\nint a(void) { return 0; }\n'
    ),
    "b.c": (
        "static int helper(void) { return 2; }\n"
        '#include "mid.h"\nint b(void) { return 0; }\n'
    ),
    "mid.h": '#include "util.h"\n',
    "util.h": "static inline int wrap(void) { return helper(); }\n",
}
_ADDED = "int added(void) { return 3; }\n"

_Graph = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]


def _graph(store: _StatefulIngestor) -> _Graph:
    # Every edge at its site, with its resolution: a call that kept its
    # target but lost its certainty differs too.
    nodes = frozenset((label, str(uid)) for label, uid in store.nodes)
    edges = frozenset(
        (
            *(str(part) for part in edge),
            str(store.props_for(edge).get(cs.KEY_RESOLUTION)),
        )
        for edge in store.keyed_edges
    )
    return nodes, edges


def _sync_util_h(
    root: Path, files: dict[str, str], updater: str
) -> tuple[GraphUpdater, _StatefulIngestor]:
    """Index `files`, add a function to util.h and sync it on `updater`,
    whose graph must then be a fresh index's."""
    _write(root, files)
    store = _StatefulIngestor()
    first = _updater(root, store)
    first.run(force=True)
    _write(root, {"util.h": files["util.h"] + _ADDED})
    force_mtime_after_cache(root, root / "util.h")
    match updater:
        case "reused":
            synced = first
            synced.run()
        case "new":
            synced = _updater(root, store)
            synced.run()
        case _:
            synced = _updater(root, store)
            synced.reingest([root / "util.h"])
    clean = _StatefulIngestor()
    _updater(root, clean).run(force=True)
    assert _graph(store) == _graph(clean)
    return synced, store


@pytest.mark.parametrize("updater", ["new", "reused", "reingest"])
@pytest.mark.parametrize(
    ("files", "edges"),
    [
        (_TWO_UP, {("util.wrap", "main.helper"), ("util.wrap", "early.early")}),
        (_THREE_UP, {("util.wrap", "main.helper")}),
        (_DISPLACED_UP, {("util.wrap", "a.leaf.leaf")}),
        (_TWO_UNITS, {("util.wrap", "a.helper")}),
    ],
    ids=["two-up", "three-up", "displaced-up", "two-units"],
)
def test_a_sync_of_a_header_finds_every_unit_that_compiles_it(
    tmp_path: Path, files: dict[str, str], edges: set[tuple[str, str]], updater: str
) -> None:
    # The includers above the re-parsed headers are unchanged, so a new
    # updater holds none of their includes, yet they decide what util.h's
    # calls may reach: its graph must be a fresh index's.
    if "c" not in load_parsers()[0]:
        pytest.skip("c parser not available")
    _, store = _sync_util_h(tmp_path / "repo", files, updater)
    calls = {
        (_short(src), _short(dst))
        for _, src, rel, _, dst in store.edges
        if rel == cs.RelationshipType.CALLS.value
    }
    assert edges <= calls, calls


@pytest.mark.parametrize(
    ("files", "read"),
    [
        (_TWO_UP, {"util", "mid", "main", "early"}),
        (_DISPLACED_UP, {"util", "mid", "main", "a.early", "a.leaf", "b.early"}),
        (_TWO_UNITS, {"util", "mid", "a", "b"}),
    ],
    ids=["two-up", "displaced-up", "two-units"],
)
def test_a_sync_of_a_header_reads_the_units_that_compile_it_and_no_other(
    tmp_path: Path, files: dict[str, str], read: set[str]
) -> None:
    # util.h and mid.h are re-parsed; the files that compile them, and what
    # those include, are read back. lone.c and lone.h lead to neither, so a
    # new updater never opens them.
    if "c" not in load_parsers()[0]:
        pytest.skip("c parser not available")
    synced, _ = _sync_util_h(tmp_path / "repo", files, "new")
    held = {_short(qn) for qn in synced.factory.import_processor.import_mapping}
    assert held == read, held


class _ImporterOutage(_StatefulIngestor):
    """Fails the importer query once `down` is set, answers the rest."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if self.down and query == cq.CYPHER_GRAPH_IMPORTERS_OF:
            raise RuntimeError("importer query outage")
        return super().fetch_all(query, params)


@pytest.mark.parametrize("updater", ["new", "reingest"])
def test_a_sync_that_cannot_read_a_headers_includers_aborts(
    tmp_path: Path, updater: str
) -> None:
    # The captured edges restore top.h, but only the importer query finds
    # main.c above it. Read as "no importers", the sync dropped
    # util.wrap -> main.helper and committed its hashes, so the next sync saw
    # the repository in sync and never put the edge back. It fails instead,
    # as a failed inbound capture does: it commits nothing and reports the
    # graph partial, so the caller keeps its unfinished-run marker.
    if "c" not in load_parsers()[0]:
        pytest.skip("c parser not available")
    root = tmp_path / "repo"
    _write(root, _THREE_UP)
    store = _ImporterOutage()
    _updater(root, store).run(force=True)
    _write(root, {"util.h": _THREE_UP["util.h"] + _ADDED})
    force_mtime_after_cache(root, root / "util.h")
    synced = _updater(root, store)
    cache = synced.state_dir / cs.HASH_CACHE_FILENAME
    hashes = cache.read_bytes()
    store.down = True
    with pytest.raises(RuntimeError, match="importer query outage"):
        if updater == "new":
            synced.run()
        else:
            synced.reingest([root / "util.h"])
    assert cache.read_bytes() == hashes
    assert not synced.committed
    assert synced.reingest_mutated is (updater == "reingest")


def test_a_full_build_reads_an_unreadable_importer_query_as_none(
    tmp_path: Path,
) -> None:
    # A full build parses every file, so it holds every include already and
    # goes on, saying what it could not read.
    store = _ImporterOutage()
    store.down = True
    updater = GraphUpdater(ingestor=store, repo_path=tmp_path, parsers={}, queries={})
    updater._is_full_build = True
    with patch("codebase_rag.graph_updater.logger") as log:
        assert updater._graph_importers(["proj.util"]) == set()
    log.warning.assert_called_once_with(ls.C_INCLUDERS_UNREAD)


# a.cpp declares module `foo`, which binds the name `foo` to foo.cpp's module
# without including it: foo.cpp's `helper()` is z.cpp's, never a.cpp's static.
_MODULE_DECLARATION = {
    "foo.cpp": "int helper();\nint use() { return helper(); }\n",
    "z.cpp": "int helper() { return 42; }\n",
    "a.cpp": (
        "export module foo;\n"
        "static int helper() { return 1; }\n"
        "int a() { return helper(); }\n"
    ),
}


def test_a_module_declaration_is_no_include(tmp_path: Path) -> None:
    calls = _calls(tmp_path / "moddecl", _MODULE_DECLARATION, "cpp")
    assert ("foo.use", "z.helper") in calls, calls
    assert ("foo.use", "a.helper") not in calls, calls
    assert ("a.a", "a.helper") in calls, calls


# A global module fragment includes foo.h, then declares module `foo`: both
# bind the qn foo.h takes, and the include is still a real one.
_FRAGMENT_INCLUDE = {
    "foo.h": "static inline int helper() { return 1; }\n",
    "a.cpp": (
        'module;\n#include "foo.h"\nexport module foo;\nint a() { return helper(); }\n'
    ),
}


def test_an_include_a_module_declaration_shares_its_qn_with_still_reaches(
    tmp_path: Path,
) -> None:
    calls = _calls(tmp_path / "fragment", _FRAGMENT_INCLUDE, "cpp")
    assert ("a.a", "foo.helper") in calls, calls
