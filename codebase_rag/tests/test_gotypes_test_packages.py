"""Issue #2571, gap 2: the go/types helper loads packages with their tests.

`packages.Load` without `Tests` never type-checks a `_test.go` file, so no
test file received a compiler fact: in go-chi/chi, 907 of the 1,024
heuristic CALLS came from test files. With tests loaded, each tested package
comes back as several variants (`p`, `p [p.test]`, `p_test [p.test]`, the
generated `p.test` main), and every file must still be answered once.

These run the real bundled helper and skip, like the other end-to-end
frontend tests, where it cannot be built (no `go`, or one older than the
helper's `go` directive).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag import logs as ls
from codebase_rag.parsers.go_frontend import GoCallSite, run_go_frontend
from codebase_rag.parsers.go_frontend import frontend as go_frontend
from codebase_rag.tests.conftest import get_relationships, run_updater

GO_MOD = "module example.com/proj\n\ngo 1.24\n"
ROUTER = (
    "package gotest\n"
    "\n"
    "type Mux struct{ routes []string }\n"
    "\n"
    "func NewRouter() *Mux { return &Mux{} }\n"
    "\n"
    "func (m *Mux) Get(p string) { m.routes = append(m.routes, p) }\n"
    "\n"
    "type Base struct{}\n"
    "\n"
    'func (Base) Do() string { return "do" }\n'
    "\n"
    "type Outer struct{ Base }\n"
)
APP = (
    "package gotest\n"
    "\n"
    "func Build() *Mux {\n"
    "\tr := NewRouter()\n"
    '\tr.Get("/a")\n'
    "\treturn r\n"
    "}\n"
)
# `o.Do()` is promoted from the embedded Base: only the compiler can bind it,
# so its edge is proof that a fact reached the test file.
ROUTER_TEST = (
    "package gotest\n"
    "\n"
    'import "testing"\n'
    "\n"
    "func TestRouter(t *testing.T) {\n"
    "\tr := NewRouter()\n"
    '\tr.Get("/b")\n'
    "\to := Outer{}\n"
    "\t_ = o.Do()\n"
    "\tif len(r.routes) != 1 {\n"
    '\t\tt.Fatal("route not added")\n'
    "\t}\n"
    "}\n"
)
EXTERNAL_TEST = (
    "package gotest_test\n"
    "\n"
    "import (\n"
    '\t"testing"\n'
    "\n"
    '\t"example.com/proj/gotest"\n'
    ")\n"
    "\n"
    "func TestExternal(t *testing.T) {\n"
    "\tr := gotest.NewRouter()\n"
    '\tr.Get("/c")\n'
    "\t_ = t\n"
    "}\n"
)
TEST_FILES = {
    "gotest/router_test.go": ROUTER_TEST,
    "gotest/external_test.go": EXTERNAL_TEST,
}

IMPL = (
    "package impl\n"
    "\n"
    "type Speaker interface{ Speak() string }\n"
    "\n"
    "type Dog struct{}\n"
    "\n"
    'func (d Dog) Speak() string { return "woof" }\n'
    "\n"
    "func Talk(s Speaker) string { return s.Speak() }\n"
)
IMPL_TEST = (
    "package impl\n"
    "\n"
    'import "testing"\n'
    "\n"
    "type loud struct{}\n"
    "\n"
    'func (l loud) Speak() string { return "LOUD" }\n'
    "\n"
    "func TestTalk(t *testing.T) {\n"
    '\tif Talk(Dog{}) != "woof" || Talk(loud{}) != "LOUD" {\n'
    '\t\tt.Fatal("talk")\n'
    "\t}\n"
    "}\n"
)


def _helper() -> Path:
    # Probe the build explicitly, as test_go_frontend does: an old or missing
    # toolchain is an environment skip, never an empty-facts pass.
    go = shutil.which("go")
    if go is None:
        pytest.skip("go toolchain not available")
    binary = go_frontend._build_tool(go)
    if binary is None:
        pytest.skip("gotypes tool could not build in this environment")
    return binary


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _issue_repo(parent: Path, with_tests: bool = True) -> Path:
    # The package sits below the module root, so the external test imports
    # `example.com/proj/gotest` (a module-root import leaves an orphan
    # ExternalModule node the graph audit rejects, unrelated to this issue).
    files = {"go.mod": GO_MOD, "gotest/router.go": ROUTER, "gotest/app.go": APP}
    if with_tests:
        files.update(TEST_FILES)
    return _write(parent / "proj", files)


def _raw_payload(binary: Path, root: Path) -> dict[str, list[dict[str, str | int]]]:
    # The helper's own output, before the Python side keys it into maps that
    # would silently collapse a duplicated fact.
    proc = subprocess.run(
        [str(binary), str(root)],
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=True,
        timeout=600,
        env={**os.environ, **go_frontend._GO_ENV},
    )
    return json.loads(proc.stdout.splitlines()[-1])


def _loc(source: str, needle: str) -> tuple[int, int]:
    # (1-based line, 0-based byte column) of the first `needle`.
    for line_no, line in enumerate(source.splitlines(), 1):
        if (idx := line.find(needle)) >= 0:
            return line_no, len(line[:idx].encode("utf-8"))
    raise AssertionError(needle)


def _site(file: str, source: str, needle: str, name: str) -> tuple[str, int, int, str]:
    line, col = _loc(source, needle)
    return (file, line, col + needle.index(name), name)


def _target(file: str, source: str, needle: str, name: str) -> GoCallSite:
    line, col = _loc(source, needle)
    return GoCallSite(name, file, line, col + needle.index(name))


# --- red ------------------------------------------------------------------------


def test_test_files_get_compiler_facts(tmp_path: Path) -> None:
    _helper()
    root = _issue_repo(tmp_path)

    facts = run_go_frontend(root)

    new_router = _target("gotest/router.go", ROUTER, "NewRouter()", "NewRouter")
    get = _target("gotest/router.go", ROUTER, "Get(p string)", "Get")
    do = _target("gotest/router.go", ROUTER, "Do() string", "Do")
    sites = facts.call_sites
    assert sites[
        _site("gotest/router_test.go", ROUTER_TEST, "NewRouter()", "NewRouter")
    ] == (new_router)
    assert sites[_site("gotest/router_test.go", ROUTER_TEST, "r.Get(", "Get")] == get
    assert sites[_site("gotest/router_test.go", ROUTER_TEST, "o.Do()", "Do")] == do
    assert (
        sites[
            _site(
                "gotest/external_test.go",
                EXTERNAL_TEST,
                "gotest.NewRouter()",
                "NewRouter",
            )
        ]
        == new_router
    )
    assert (
        sites[_site("gotest/external_test.go", EXTERNAL_TEST, "r.Get(", "Get")] == get
    )
    assert _site("gotest/router_test.go", ROUTER_TEST, "t.Fatal(", "Fatal") in (
        facts.external_sites
    )


def test_the_facts_log_counts_the_test_files(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _helper()
    root = _issue_repo(temp_repo)
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="INFO")
    try:
        run_updater(root, MagicMock())
    finally:
        logger.remove(sink)

    # app.go 2 + router_test.go 3 + external_test.go 2; t.Fatal is external.
    assert ls.GO_FRONTEND_FACTS.format(calls=7, externals=1) in messages


def test_test_file_calls_bind_through_the_facts(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _helper()
    root = _issue_repo(temp_repo)
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    ingestor = MagicMock()
    run_updater(root, ingestor)

    calls: dict[tuple[str, str], set[str]] = {}
    for c in get_relationships(ingestor, cs.RelationshipType.CALLS):
        props = c.kwargs.get("properties") or {}
        key = (str(c.args[0][2]), str(c.args[2][2]))
        calls.setdefault(key, set()).add(str(props.get(cs.KEY_RESOLUTION)))
    exact = {cs.EdgeResolution.EXACT}
    assert (
        calls[("proj.gotest.router_test.TestRouter", "proj.gotest.router.NewRouter")]
        == exact
    )
    assert calls[
        ("proj.gotest.external_test.TestExternal", "proj.gotest.router.Mux.Get")
    ] == (exact)
    # Promotion through the embedded Base: the name trie cannot make this bind.
    assert (
        calls[("proj.gotest.router_test.TestRouter", "proj.gotest.router.Base.Do")]
        == exact
    )


# --- negative -------------------------------------------------------------------


def test_each_file_is_answered_once(tmp_path: Path) -> None:
    # `p`, `p [p.test]` and `p_test [p.test]` all hold router.go or app.go
    # through their imports, and the variants re-declare every type: nothing
    # may come out twice.
    binary = _helper()
    for root in (
        _issue_repo(tmp_path),
        _write(
            tmp_path / "impl",
            {
                "go.mod": "module example.com/impl\n\ngo 1.24\n",
                "speak.go": IMPL,
                "speak_test.go": IMPL_TEST,
            },
        ),
    ):
        payload = _raw_payload(binary, root)

        for section in ("calls", "externals"):
            keys = [
                (fact["file"], fact["line"], fact["col"], fact["name"])
                for fact in payload[section]
            ]
            assert len(keys) == len(set(keys)), (section, keys)
        pairs = [
            (p["file"], p["line"], p["col"], p["ifile"], p["iline"], p["icol"])
            for p in payload["implements"]
        ]
        assert len(pairs) == len(set(pairs)), pairs


def test_non_test_files_answer_as_they_did_without_tests(tmp_path: Path) -> None:
    binary = _helper()
    with_tests = _raw_payload(binary, _issue_repo(tmp_path / "with"))
    without = _raw_payload(binary, _issue_repo(tmp_path / "without", with_tests=False))

    def production(section: str) -> list[dict[str, str | int]]:
        return [
            fact
            for fact in with_tests[section]
            if not str(fact["file"]).endswith("_test.go")
        ]

    assert production("calls") == without["calls"]
    assert production("externals") == without["externals"]
    assert with_tests["implements"] == without["implements"]


def test_a_package_without_tests_is_unchanged(tmp_path: Path) -> None:
    binary = _helper()
    root = _write(
        tmp_path / "impl",
        {"go.mod": "module example.com/impl\n\ngo 1.24\n", "speak.go": IMPL},
    )

    payload = _raw_payload(binary, root)

    speak_call_line, speak_call_col = _loc(IMPL, "s.Speak()")
    speak_line, speak_col = _loc(IMPL, "Speak() string }")
    dog_line, dog_col = _loc(IMPL, "Dog struct")
    iface_line, iface_col = _loc(IMPL, "Speaker interface")
    assert payload == {
        "calls": [
            {
                "file": "speak.go",
                "line": speak_call_line,
                "col": speak_call_col + 2,
                "name": "Speak",
                "tfile": "speak.go",
                "tline": speak_line,
                "tcol": speak_col,
            }
        ],
        "externals": [],
        "implements": [
            {
                "file": "speak.go",
                "line": dog_line,
                "col": dog_col,
                "name": "Dog",
                "ifile": "speak.go",
                "iline": iface_line,
                "icol": iface_col,
                "iname": "Speaker",
            }
        ],
    }


def test_a_test_type_implements_once_and_production_pairs_stay(
    tmp_path: Path,
) -> None:
    binary = _helper()
    root = _write(
        tmp_path / "impl",
        {
            "go.mod": "module example.com/impl\n\ngo 1.24\n",
            "speak.go": IMPL,
            "speak_test.go": IMPL_TEST,
        },
    )

    payload = _raw_payload(binary, root)

    assert sorted((p["name"], p["iname"]) for p in payload["implements"]) == [
        ("Dog", "Speaker"),
        ("loud", "Speaker"),
    ]


def test_a_broken_test_file_costs_only_the_test_files(tmp_path: Path) -> None:
    # A test variant that does not type-check is skipped whole, as any
    # ill-typed package is; the package's own files still answer.
    binary = _helper()
    root = _issue_repo(tmp_path, with_tests=False)
    (root / "gotest" / "broken_test.go").write_text(
        "package gotest\n\nfunc TestBroken() { NewRouter().Missing() }\n",
        encoding="utf-8",
    )

    payload = _raw_payload(binary, root)

    files = {fact["file"] for fact in payload["calls"]}
    assert files == {"gotest/app.go"}


# A method signature naming a type of the package itself (`Msg`): each test
# variant re-declares that type, so a test-only implementer and the
# production interface it satisfies sit in different type universes, and
# types.Implements across them is false (#2616 review).
MSG_IMPL = (
    "package impl\n"
    "\n"
    "type Msg struct{ text string }\n"
    "\n"
    "type Speaker interface{ Speak(m Msg) string }\n"
    "\n"
    "type Dog struct{}\n"
    "\n"
    "func (d Dog) Speak(m Msg) string { return m.text }\n"
)
MSG_IMPL_TEST = (
    "package impl\n"
    "\n"
    'import "testing"\n'
    "\n"
    "type loud struct{}\n"
    "\n"
    'func (l loud) Speak(m Msg) string { return m.text + "!" }\n'
    "\n"
    "type echoer interface{ Speak(m Msg) string }\n"
    "\n"
    "func TestTalk(t *testing.T) {\n"
    "\tvar s Speaker = loud{}\n"
    "\tvar e echoer = Dog{}\n"
    "\t_, _ = s, e\n"
    "}\n"
)
MSG_EXTERNAL_TEST = (
    "package impl_test\n"
    "\n"
    "import (\n"
    '\t"testing"\n'
    "\n"
    '\t"example.com/impl"\n'
    ")\n"
    "\n"
    "type quiet struct{}\n"
    "\n"
    'func (quiet) Speak(m impl.Msg) string { return "" }\n'
    "\n"
    "func TestQuiet(t *testing.T) {\n"
    "\tvar s impl.Speaker = quiet{}\n"
    "\t_ = s\n"
    "}\n"
)


def _msg_repo(parent: Path) -> Path:
    return _write(
        parent / "impl",
        {
            "go.mod": "module example.com/impl\n\ngo 1.24\n",
            "speak.go": MSG_IMPL,
            "speak_test.go": MSG_IMPL_TEST,
            "quiet_test.go": MSG_EXTERNAL_TEST,
        },
    )


def test_a_test_type_implements_an_interface_over_a_package_type(
    tmp_path: Path,
) -> None:
    binary = _helper()

    payload = _raw_payload(binary, _msg_repo(tmp_path))

    pairs = sorted((p["name"], p["iname"]) for p in payload["implements"])
    assert ("loud", "Speaker") in pairs
    assert ("quiet", "Speaker") in pairs


def test_a_production_type_implements_a_test_interface_over_a_package_type(
    tmp_path: Path,
) -> None:
    binary = _helper()

    payload = _raw_payload(binary, _msg_repo(tmp_path))

    pairs = sorted((p["name"], p["iname"]) for p in payload["implements"])
    assert ("Dog", "echoer") in pairs
    assert ("loud", "echoer") in pairs


def test_cross_variant_pairs_come_out_once_and_production_pairs_stay(
    tmp_path: Path,
) -> None:
    # Negative: the production pair is still there, every pair once, and the
    # interfaces are never paired with themselves.
    binary = _helper()

    payload = _raw_payload(binary, _msg_repo(tmp_path))

    pairs = [
        (p["file"], p["line"], p["col"], p["ifile"], p["iline"], p["icol"])
        for p in payload["implements"]
    ]
    assert len(pairs) == len(set(pairs)), pairs
    assert sorted((p["name"], p["iname"]) for p in payload["implements"]) == [
        ("Dog", "Speaker"),
        ("Dog", "echoer"),
        ("loud", "Speaker"),
        ("loud", "echoer"),
        ("quiet", "Speaker"),
        ("quiet", "echoer"),
    ]


def test_a_test_type_implements_edge_reaches_the_graph(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The package sits below the module root, as in `_issue_repo`.
    _helper()
    root = _write(
        temp_repo / "proj",
        {
            "go.mod": GO_MOD,
            "impl/speak.go": MSG_IMPL,
            "impl/speak_test.go": MSG_IMPL_TEST,
            "impl/quiet_test.go": MSG_EXTERNAL_TEST.replace(
                '"example.com/impl"', '"example.com/proj/impl"'
            ),
        },
    )
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    ingestor = MagicMock()
    run_updater(root, ingestor)

    edges = {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in get_relationships(ingestor, cs.RelationshipType.IMPLEMENTS)
    }
    speaker = "proj.impl.speak.Speaker"
    assert ("proj.impl.speak.Dog", speaker) in edges
    assert ("proj.impl.speak_test.loud", speaker) in edges
    assert ("proj.impl.quiet_test.quiet", speaker) in edges
