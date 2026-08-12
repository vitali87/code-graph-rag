# Go semantic frontend (issue #1179): a bundled go/packages tool that emits
# exact first-party call targets (embedded-struct method promotion and scope
# shadowing resolved by the real type rules) and external-site facts for callees
# that leave the module. The parse/registry/adapter tests drive the wiring with
# synthetic facts and need no go toolchain; the end-to-end test runs the real
# bundled tool and skips when `go` is absent.
import json
import shutil
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.frontends import FRONTENDS, SemanticFacts
from codebase_rag.parsers.frontends.go import GoFrontend, _adapt_go_semantic_facts
from codebase_rag.parsers.frontends.protocol import LanguageFrontend, ResolvedCallSite
from codebase_rag.parsers.go_frontend import (
    GoCallSite,
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
        }
    )
    facts = _parse_payload(payload)
    assert facts.call_sites[("a.go", 5, 8, "Handle")] == GoCallSite(
        "Handle", "b.go", 3, 4
    )
    assert facts.external_sites == {("a.go", 11, 8, "Println")}


def test_parse_payload_without_sections_yields_empty_facts() -> None:
    # An older tool build (stale cached binary) may emit neither section; both
    # must default to empty instead of raising.
    facts = _parse_payload(json.dumps({}))
    assert facts.call_sites == {}
    assert facts.external_sites == set()


def test_parse_payload_non_json_yields_empty_facts() -> None:
    facts = _parse_payload("panic: boom\n")
    assert facts.call_sites == {}
    assert facts.external_sites == set()


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
    assert found is not None and found.name == "go.mod"


def test_applies_requires_a_go_module(tmp_path: Path) -> None:
    frontend = GoFrontend()
    assert frontend.applies(tmp_path) is False
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.23\n")
    assert frontend.applies(tmp_path) is True


def test_adapter_maps_go_facts_to_semantic_facts() -> None:
    facts = GoSemanticFacts(
        call_sites={("a.go", 5, 8, "Handle"): GoCallSite("Handle", "b.go", 3, 4)},
        external_sites={("a.go", 11, 8, "Println")},
    )
    adapted = _adapt_go_semantic_facts(facts)
    assert isinstance(adapted, SemanticFacts)
    assert adapted.resolved_call_sites[("a.go", 5, 8, "Handle")] == ResolvedCallSite(
        "Handle", "b.go", 3, 4
    )
    assert adapted.external_sites == {("a.go", 11, 8, "Println")}
    # The Go frontend fills only the two call families; the rest stay empty.
    assert adapted.base_kinds == {}
    assert adapted.partial_groups == []
    assert adapted.query_calls == []


def test_adapter_of_empty_facts_is_empty() -> None:
    assert _adapt_go_semantic_facts(GoSemanticFacts({}, set())).is_empty()


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
