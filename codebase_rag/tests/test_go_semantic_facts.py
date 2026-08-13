# Go go/types frontend consumer (issue #1179, PR2): the resolver wiring that
# turns the producer's facts into graph edges -- exact first-party call binding
# (embedded promotion, scope shadowing) and external-site suppression. The
# synthetic-facts tests drive run_updater with a monkeypatched runner and need
# no go toolchain; the end-to-end test runs the real bundled tool and skips
# when `go` is absent. The join is location-keyed and degrades to the Go
# heuristics on any key miss, so the frontend-off graph is unchanged.
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parsers.go_frontend import GoCallSite, GoSemanticFacts
from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "go"


def _byte_loc(source: str, needle: str) -> tuple[int, int]:
    # (1-based line, 0-based BYTE column) of the first occurrence of `needle`.
    for line_no, line in enumerate(source.splitlines(), 1):
        idx = line.find(needle)
        if idx >= 0:
            return line_no, len(line[:idx].encode("utf-8"))
    raise AssertionError(needle)


def _pairs(mock_ingestor: MagicMock, rel_type: str) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel_type)
    }


def _has(pairs: set[tuple[str, str]], caller_suffix: str, callee_suffix: str) -> bool:
    return any(
        ca.endswith(caller_suffix) and ce.endswith(callee_suffix) for ca, ce in pairs
    )


def _gotypes(monkeypatch: pytest.MonkeyPatch, facts: GoSemanticFacts) -> None:
    # The Go frontend runs behind the LanguageFrontend registry, so patch the
    # toolchain gate and the runner where the ADAPTER binds them.
    from codebase_rag.parsers.frontends import go as go_fe

    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    monkeypatch.setattr(go_fe, "go_frontend_available", lambda: True)
    monkeypatch.setattr(go_fe, "run_go_frontend", lambda repo_path: facts)


_AMBIGUOUS_SRC = """package sample

type A struct{}

func (a A) Handle() string { return "a" }

type B struct{}

func (b B) Handle() string { return "b" }

type Handler interface{ Handle() string }

func Run(h Handler) {
\t_ = h.Handle()
}
"""


def test_call_fact_overrides_heuristic_to_exact_implementation(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `h.Handle()` on an interface receiver: the name-trie heuristic guesses the
    # FIRST same-named method (A.Handle). Point the go/types fact at B.Handle
    # (keyed on the callee NAME token, joined via the Pass-2 name alias) and the
    # edge must bind B.Handle instead -- proving the compiler fact overrides the
    # heuristic, not that both happen to agree.
    root = temp_repo / "goexact"
    root.mkdir()
    (root / "go.mod").write_text("module example.com/exact\n\ngo 1.23\n")
    (root / "sample.go").write_text(_AMBIGUOUS_SRC, encoding="utf-8")

    # The CALL site is the unique `h.Handle()` line.
    hcall_line, _ = _byte_loc(_AMBIGUOUS_SRC, "h.Handle()")
    hcall_text = _AMBIGUOUS_SRC.splitlines()[hcall_line - 1]
    hcall_col = len(hcall_text[: hcall_text.index("Handle")].encode("utf-8"))
    target_line, target_col = _byte_loc(_AMBIGUOUS_SRC, 'Handle() string { return "b"')
    facts = GoSemanticFacts(
        call_sites={
            ("sample.go", hcall_line, hcall_col, "Handle"): GoCallSite(
                "Handle", "sample.go", target_line, target_col
            )
        },
        external_sites=set(),
    )
    _gotypes(monkeypatch, facts)

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    calls = _pairs(ingestor, "CALLS")
    assert _has(calls, "Run", "B.Handle"), calls
    assert not _has(calls, "Run", "A.Handle"), calls


_EXTERNAL_SRC = """package sample

type Helper struct{}

func (h Helper) Close() {}

func Ping() {}

func Run(c interface{ Close() }) {
\tPing()
\tc.Close()
}
"""


def test_external_site_fact_suppresses_name_trie_fallback(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `c.Close()` on an interface receiver: the compiler proved the callee
    # leaves the module, so its external-site fact must suppress a fabricated
    # edge onto the unrelated first-party Helper.Close, while the sibling
    # first-party Ping() call in the same function still resolves.
    root = temp_repo / "goexternal"
    root.mkdir()
    (root / "go.mod").write_text("module example.com/external\n\ngo 1.23\n")
    (root / "sample.go").write_text(_EXTERNAL_SRC, encoding="utf-8")

    close_line, _ = _byte_loc(_EXTERNAL_SRC, "c.Close()")
    close_text = _EXTERNAL_SRC.splitlines()[close_line - 1]
    close_col = len(close_text[: close_text.index("Close")].encode("utf-8"))
    facts = GoSemanticFacts(
        call_sites={},
        external_sites={("sample.go", close_line, close_col, "Close")},
    )
    _gotypes(monkeypatch, facts)

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    calls = _pairs(ingestor, "CALLS")
    assert not _has(calls, "Run", "Helper.Close"), calls
    assert _has(calls, "Run", "Ping"), calls


def test_frontend_off_clears_stale_go_facts(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Watch-mode reset must cover the Go fact stores: a later run with the
    # frontend off may not keep binding stale targets.
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.frontends.protocol import ResolvedCallSite

    parsers, queries = load_parsers()
    updater = gu.GraphUpdater(
        ingestor=MagicMock(), repo_path=temp_repo, parsers=parsers, queries=queries
    )
    dp = updater.factory.definition_processor
    dp.go_call_sites[("stale.go", 1, 0, "Old")] = ResolvedCallSite(
        "Old", "stale.go", 2, 0
    )
    dp.go_external_sites.add(("stale.go", 3, 0, "Ext"))
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)

    updater._run_go_frontend()

    assert dp.go_call_sites == {}
    assert dp.go_external_sites == set()


def test_auto_without_toolchain_matches_frontend_off(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The core invariant: with GO_FRONTEND=AUTO and no go toolchain, the CALLS
    # graph must be byte-identical to a run with the frontend fully off, so a
    # missing compiler degrades to today's tree-sitter graph, never worse.
    # SEPARATE dirs: a second updater on the same path would no-op on the
    # incremental hash cache and produce an empty (falsely-matching) graph.
    auto_root = temp_repo / "goinvariant_auto"
    off_root = temp_repo / "goinvariant_off"
    for r in (auto_root, off_root):
        r.mkdir()
        (r / "go.mod").write_text("module example.com/inv\n\ngo 1.23\n")
        (r / "sample.go").write_text(_AMBIGUOUS_SRC, encoding="utf-8")

    from codebase_rag.parsers.frontends import go as go_fe

    monkeypatch.setattr(go_fe, "go_frontend_available", lambda: False)
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.AUTO)
    ingestor_auto = MagicMock()
    run_updater(auto_root, ingestor_auto, skip_if_missing=SKIP)

    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    ingestor_off = MagicMock()
    run_updater(off_root, ingestor_off, skip_if_missing=SKIP)

    def _local(pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
        # Strip the differing project-root prefix so the two graphs compare.
        return {(a.split(".", 1)[-1], b.split(".", 1)[-1]) for a, b in pairs}

    assert _local(_pairs(ingestor_auto, "CALLS")) == _local(
        _pairs(ingestor_off, "CALLS")
    )


_E2E_SRC = """package sample

import "fmt"

type Base struct{}

func (b Base) Do() string { return "base" }

type Outer struct {
\tBase
}

func Run() {
\to := Outer{}
\t_ = o.Do()
\tfmt.Println("x")
}
"""


def test_gotypes_end_to_end_binds_promoted_call_through_real_facts(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end with REAL facts (no synthetic maps): proves the tool's emitted
    # line/col keys match tree-sitter node positions AND the Pass-2 name-alias
    # registry. `o.Do()` is a promoted embedded-method call; the fact binds it to
    # Base.Do, and `fmt.Println` is an external site (no first-party edge).
    from codebase_rag.parsers.go_frontend import (
        go_frontend_available,
        run_go_frontend,
    )

    go = shutil.which("go")
    if go is None or not go_frontend_available():
        pytest.skip("go toolchain not available")
    root = temp_repo / "goe2e"
    root.mkdir()
    (root / "go.mod").write_text("module example.com/e2e\n\ngo 1.23\n")
    (root / "sample.go").write_text(_E2E_SRC, encoding="utf-8")

    facts = run_go_frontend(root)
    if not facts.call_sites:
        pytest.skip("gotypes frontend could not build in this environment")

    # Guard the linchpin: the emitted target col is the NAME-node position, not
    # the func-keyword span start (they differ for Go).
    do_key = next(k for k in facts.call_sites if k[3] == "Do")
    do_target = facts.call_sites[do_key]
    name_line, name_col = _byte_loc(_E2E_SRC, "Do() string")
    assert (do_target.target_line, do_target.target_col) == (name_line, name_col)

    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    calls = _pairs(ingestor, "CALLS")
    assert _has(calls, "Run", "Base.Do"), calls
    assert not any(ce.endswith("Println") for _, ce in calls), calls
