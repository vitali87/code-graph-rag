# Go semantic frontend (issue #1179): a bundled go/packages tool that emits
# exact first-party call targets (embedded-struct method promotion and scope
# shadowing resolved by the real type rules) and external-site facts for callees
# that leave the module. The parse/registry/adapter tests drive the wiring with
# synthetic facts and need no go toolchain; the end-to-end test runs the real
# bundled tool and skips when `go` is absent.
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.frontends import FRONTENDS, SemanticFacts
from codebase_rag.parsers.frontends.go import GoFrontend, _adapt_go_semantic_facts
from codebase_rag.parsers.frontends.protocol import (
    ImplementsPair,
    LanguageFrontend,
    ResolvedCallSite,
)
from codebase_rag.parsers.go_frontend import (
    GoCallSite,
    GoImplements,
    GoSemanticFacts,
    find_go_module,
    run_go_frontend,
)
from codebase_rag.parsers.go_frontend.frontend import _build_tool, _parse_payload

_FIXTURE = """package main

import "fmt"

type Base struct{}

func (b Base) Hello() string { return "hi" }

type Outer struct {
\tBase
}

func run(fn func() string) string { return fn() }

func main() {
\to := Outer{}
\ts := "café"; _ = o.Hello() + s
\t_ = run(greet)
\tfmt.Println("external")
}

func greet() string { return "greet" }
"""


def _byte_loc(source: str, needle: str) -> tuple[int, int]:
    # (1-based line, 0-based BYTE column) of the first occurrence of `needle`,
    # matching tree-sitter's start_point and the gotypes tool's emitted columns.
    for line_no, line in enumerate(source.splitlines(), 1):
        idx = line.find(needle)
        if idx >= 0:
            return line_no, len(line[:idx].encode("utf-8"))
    raise AssertionError(needle)


def test_parse_payload_reads_call_and_external_sections() -> None:
    payload = json.dumps(
        {
            "calls": [
                {
                    "file": "a.go",
                    "line": 5,
                    "col": 8,
                    "name": "Handle",
                    "tfile": "b.go",
                    "tline": 3,
                    "tcol": 4,
                }
            ],
            "externals": [{"file": "a.go", "line": 11, "col": 8, "name": "Println"}],
            "implements": [
                {
                    "file": "a.go",
                    "line": 9,
                    "col": 5,
                    "name": "Loud",
                    "ifile": "b.go",
                    "iline": 5,
                    "icol": 5,
                    "iname": "Greeter",
                }
            ],
        }
    )
    facts = _parse_payload(payload)
    assert facts.call_sites[("a.go", 5, 8, "Handle")] == GoCallSite(
        "Handle", "b.go", 3, 4
    )
    assert facts.external_sites == {("a.go", 11, 8, "Println")}
    assert facts.implements == [GoImplements("a.go", 9, 5, "b.go", 5, 5)]


def test_parse_payload_without_sections_yields_empty_facts() -> None:
    # An older tool build (stale cached binary) may emit no section; all three
    # families must default to empty instead of raising.
    facts = _parse_payload(json.dumps({}))
    assert facts.call_sites == {}
    assert facts.external_sites == set()
    assert facts.implements == []


def test_parse_payload_non_json_signals_failure() -> None:
    # None, not empty facts: empty is the answer for a module the tool
    # analysed and found nothing in, so reusing it for a contract violation
    # made the two indistinguishable and the degradation invisible (#1462).
    # The behaviour this originally guarded -- degrade rather than crash --
    # is unchanged; only the signal is now distinguishable.
    assert _parse_payload("panic: boom\n") is None


def test_go_frontend_is_registered() -> None:
    frontend = FRONTENDS.get(cs.SupportedLanguage.GO)
    assert frontend is not None
    assert frontend.language == cs.SupportedLanguage.GO
    assert isinstance(frontend, LanguageFrontend)
    assert isinstance(frontend.available(), bool)


def test_find_go_module_discovers_gomod(tmp_path: Path) -> None:
    assert find_go_module(tmp_path) is None
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.23\n")
    found = find_go_module(tmp_path)
    assert found is not None
    assert found.name == "go.mod"


def test_applies_requires_a_go_module(tmp_path: Path) -> None:
    frontend = GoFrontend()
    assert frontend.applies(tmp_path) is False
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.23\n")
    assert frontend.applies(tmp_path) is True


def test_adapter_maps_go_facts_to_semantic_facts() -> None:
    facts = GoSemanticFacts(
        call_sites={("a.go", 5, 8, "Handle"): GoCallSite("Handle", "b.go", 3, 4)},
        external_sites={("a.go", 11, 8, "Println")},
        implements=[GoImplements("a.go", 9, 5, "b.go", 5, 5)],
    )
    adapted = _adapt_go_semantic_facts(facts)
    assert isinstance(adapted, SemanticFacts)
    assert adapted.resolved_call_sites[("a.go", 5, 8, "Handle")] == ResolvedCallSite(
        "Handle", "b.go", 3, 4
    )
    assert adapted.external_sites == {("a.go", 11, 8, "Println")}
    # GoImplements maps 1:1 onto the generic implements_pairs family.
    assert adapted.implements_pairs == [ImplementsPair("a.go", 9, 5, "b.go", 5, 5)]
    # The Go frontend leaves the C#-specific families empty.
    assert adapted.base_kinds == {}
    assert adapted.partial_groups == []
    assert adapted.query_calls == []


def test_adapter_of_empty_facts_is_empty() -> None:
    assert _adapt_go_semantic_facts(GoSemanticFacts({}, set(), [])).is_empty()


def test_gotypes_tool_emits_call_and_external_facts(tmp_path: Path) -> None:
    # End-to-end against the real bundled tool: the compiled gotypes frontend
    # must resolve an embedded-struct PROMOTED method call to its exact
    # declaration (a bind the name trie cannot make), report a stdlib call as
    # an external site (never a first-party fact), and emit 0-based BYTE
    # columns even past a multibyte prefix on the call line.
    go = shutil.which("go")
    if go is None:
        pytest.skip("go toolchain not available")
    # Probe the build explicitly: an offline/deps-missing failure is a legitimate
    # environment skip, but once the tool builds the fixture ALWAYS contains
    # Hello and Println, so empty facts below are a real regression, not a skip.
    if _build_tool(go) is None:
        pytest.skip("gotypes tool could not build in this environment")
    (tmp_path / "go.mod").write_text("module example.com/fix\n\ngo 1.23\n")
    (tmp_path / "main.go").write_text(_FIXTURE, encoding="utf-8")

    facts = run_go_frontend(tmp_path)

    # The call site is on the unique multibyte ("café") line; the target is the
    # method declaration ("Hello() string" appears only there).
    call_line = next(
        i for i, line in enumerate(_FIXTURE.splitlines(), 1) if "café" in line
    )
    call_text = _FIXTURE.splitlines()[call_line - 1]
    hello_col = len(call_text[: call_text.index("Hello")].encode("utf-8"))
    target_line, target_col = _byte_loc(_FIXTURE, "Hello() string")
    assert facts.call_sites[("main.go", call_line, hello_col, "Hello")] == GoCallSite(
        "Hello", "main.go", target_line, target_col
    )

    println_line, println_col = _byte_loc(_FIXTURE, "Println")
    assert ("main.go", println_line, println_col, "Println") in facts.external_sites
    assert not any(key[3] == "Println" for key in facts.call_sites)


_IMPLEMENTS_FIXTURE = """package main

type Speaker interface{ Speak() string }

type Dog struct{}

func (d Dog) Speak() string { return "woof" }

type Box[T any] struct{ v T }

func (b Box[T]) Speak() string { return "box" }

func main() {
\t_ = Dog{}
\t_ = Box[int]{}
}
"""


def test_gotypes_tool_emits_implements_and_skips_generics(tmp_path: Path) -> None:
    # End-to-end: the tool must emit the Dog -> Speaker pair proven by
    # types.Implements, keyed on both declaring NAME tokens, AND must SKIP the
    # generic Box[T] even though it structurally satisfies Speaker --
    # types.Implements is not contractually specified for uninstantiated
    # generics, so the frontend degrades to tree-sitter there rather than emit a
    # version-dependent edge (must not crash on the generic, either).
    go = shutil.which("go")
    if go is None:
        pytest.skip("go toolchain not available")
    if _build_tool(go) is None:
        pytest.skip("gotypes tool could not build in this environment")
    (tmp_path / "go.mod").write_text("module example.com/impl\n\ngo 1.23\n")
    (tmp_path / "main.go").write_text(_IMPLEMENTS_FIXTURE, encoding="utf-8")

    facts = run_go_frontend(tmp_path)

    dog_line, dog_col = _byte_loc(_IMPLEMENTS_FIXTURE, "Dog struct")
    iface_line, iface_col = _byte_loc(_IMPLEMENTS_FIXTURE, "Speaker interface")
    # Exactly one pair: Dog -> Speaker. The generic Box[T] structurally
    # satisfies Speaker too, so its absence here proves the generic skip.
    assert facts.implements == [
        GoImplements("main.go", dog_line, dog_col, "main.go", iface_line, iface_col)
    ]


class _FakeToolRun:
    """Captures per-module-root tool invocations and returns canned payloads
    keyed by the invoked directory's repo-relative posix path."""

    def __init__(self, repo: Path, payloads: dict[str, dict]) -> None:
        self.repo = repo
        self.payloads = payloads
        self.invoked: list[str] = []

    def __call__(self, cmd, **kwargs):
        root = Path(cmd[1])
        rel = "" if root == self.repo else root.relative_to(self.repo).as_posix()
        self.invoked.append(rel)
        payload = self.payloads.get(rel, {"calls": [], "externals": []})
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(payload), stderr=""
        )


def test_nested_modules_each_get_a_tool_run_with_reanchored_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #1227: `./...` never descends into a nested go.mod, so the tool
    # must run once per module root; each run emits module-relative paths
    # that re-anchor to the repo root before merging.
    repo = tmp_path / "repo"
    (repo / "sub" / "tool").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/root\n\ngo 1.22\n")
    (repo / "sub" / "tool" / "go.mod").write_text(
        "module example.com/tool\n\ngo 1.22\n"
    )
    import codebase_rag.parsers.go_frontend.frontend as fe

    monkeypatch.setattr(fe.shutil, "which", lambda _name: "/usr/bin/go")
    monkeypatch.setattr(fe, "_build_tool", lambda _go: Path("/fake/gotypes"))
    fake = _FakeToolRun(
        repo,
        {
            "": {
                "calls": [
                    {
                        "file": "main.go",
                        "line": 5,
                        "col": 2,
                        "name": "Run",
                        "tfile": "lib.go",
                        "tline": 3,
                        "tcol": 5,
                    }
                ],
                "externals": [],
            },
            "sub/tool": {
                "calls": [
                    {
                        "file": "cmd.go",
                        "line": 8,
                        "col": 1,
                        "name": "Go",
                        "tfile": "cmd.go",
                        "tline": 2,
                        "tcol": 5,
                    }
                ],
                "externals": [
                    {"file": "cmd.go", "line": 9, "col": 1, "name": "Printf"}
                ],
                "implements": [
                    {
                        "file": "cmd.go",
                        "line": 12,
                        "col": 5,
                        "ifile": "iface.go",
                        "iline": 4,
                        "icol": 5,
                    }
                ],
            },
        },
    )
    monkeypatch.setattr(fe.subprocess, "run", fake)

    facts = run_go_frontend(repo)

    assert sorted(fake.invoked) == ["", "sub/tool"]
    assert facts.call_sites[("main.go", 5, 2, "Run")] == GoCallSite(
        "Run", "lib.go", 3, 5
    )
    assert facts.call_sites[("sub/tool/cmd.go", 8, 1, "Go")] == GoCallSite(
        "Go", "sub/tool/cmd.go", 2, 5
    )
    assert facts.external_sites == {("sub/tool/cmd.go", 9, 1, "Printf")}
    assert facts.implements == [
        GoImplements("sub/tool/cmd.go", 12, 5, "sub/tool/iface.go", 4, 5)
    ]


def test_ignored_directories_never_get_a_tool_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "vendor" / "dep").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/root\n\ngo 1.22\n")
    (repo / "vendor" / "dep" / "go.mod").write_text(
        "module example.com/dep\n\ngo 1.22\n"
    )
    import codebase_rag.parsers.go_frontend.frontend as fe

    monkeypatch.setattr(fe.shutil, "which", lambda _name: "/usr/bin/go")
    monkeypatch.setattr(fe, "_build_tool", lambda _go: Path("/fake/gotypes"))
    fake = _FakeToolRun(repo, {})
    monkeypatch.setattr(fe.subprocess, "run", fake)

    run_go_frontend(repo)

    assert fake.invoked == [""]


def test_one_failing_module_degrades_only_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A per-module tool failure (timeout, crash) must drop only that
    # module's facts to tree-sitter; the sibling module's facts survive.
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/root\n\ngo 1.22\n")
    (repo / "sub" / "go.mod").write_text("module example.com/sub\n\ngo 1.22\n")
    import codebase_rag.parsers.go_frontend.frontend as fe

    monkeypatch.setattr(fe.shutil, "which", lambda _name: "/usr/bin/go")
    monkeypatch.setattr(fe, "_build_tool", lambda _go: Path("/fake/gotypes"))

    invoked: list[Path] = []

    def _run(cmd, **kwargs):
        root = Path(cmd[1])
        invoked.append(root)
        if root == repo:
            raise subprocess.TimeoutExpired(cmd, 1)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "calls": [
                        {
                            "file": "s.go",
                            "line": 3,
                            "col": 1,
                            "name": "Do",
                            "tfile": "s.go",
                            "tline": 1,
                            "tcol": 5,
                        }
                    ],
                    "externals": [],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(fe.subprocess, "run", _run)

    facts = run_go_frontend(repo)

    assert facts.call_sites == {
        ("sub/s.go", 3, 1, "Do"): GoCallSite("Do", "sub/s.go", 1, 5)
    }
    assert facts.external_sites == set()
    assert facts.implements == []
    assert invoked == [repo, repo / "sub"]
