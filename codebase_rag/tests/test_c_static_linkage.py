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
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
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
# `m(int)` n's `static m(int)` (#3154 review).
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
    ),
    "b.cpp": (
        "void f(double);\nvoid h(double);\nnamespace n { void k(int); }\n"
        "void m(int);\n"
        "void g() { f(1.0); h(1.0); n::k(1); m(1); }\n"
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
    # `a.f` is the `static f(int)` prototype, `a.f@2` the extern `f(double)`.
    calls = _calls(tmp_path / "overloads", _CPP_OVERLOADS, "cpp")
    callees = {to for (src, to) in calls if src == "b.g"}
    assert callees == {"a.f@2", "a.h@4", "a.n.k", "a.m"}, calls


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


def _sync(root: Path, store: _StatefulIngestor, force: bool) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    GraphUpdater(ingestor=store, repo_path=root, parsers=parsers, queries=queries).run(
        force=force
    )
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
