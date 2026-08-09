"""Three-verdict flow reachability (issue #1050): FOUND returns the path,
NO_FLOW asserts full coverage, UNKNOWN names the coverage gaps so an absent
path is never read as a verified absence."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.capture import ALL_ENABLED
from codebase_rag.flow_verdict import (
    CYPHER_FLOW_COVERAGE_GAPS,
    CYPHER_FLOW_EDGES,
    FLOW_VERDICT_FOUND,
    FLOW_VERDICT_NO_FLOW,
    FLOW_VERDICT_UNKNOWN,
    flow_reachability_verdict,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _query_fn(edges: list[tuple[str, str]], gaps: list[str]):
    def fetch_all(query: str, params=None):
        if query == CYPHER_FLOW_EDGES:
            return [{"source": s, "target": t} for s, t in edges]
        if query == CYPHER_FLOW_COVERAGE_GAPS:
            return [{"path": p} for p in gaps]
        raise AssertionError(query)

    return fetch_all


def test_found_returns_the_path() -> None:
    result = flow_reachability_verdict(
        _query_fn(
            [("p.a.src", "p.a.mid"), ("p.a.mid", "p.a.sink"), ("p.a.mid", "p.a.x")],
            ["uncovered.lua"],
        ),
        "p",
        "p.a.src",
        "p.a.sink",
    )
    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == ("p.a.src", "p.a.mid", "p.a.sink")
    assert result.gaps == ()


def test_no_flow_requires_full_coverage() -> None:
    result = flow_reachability_verdict(
        _query_fn([("p.a.src", "p.a.other")], []),
        "p",
        "p.a.src",
        "p.a.sink",
    )
    assert result.verdict == FLOW_VERDICT_NO_FLOW
    assert result.path == ()
    assert result.gaps == ()


def test_unknown_names_the_gaps() -> None:
    result = flow_reachability_verdict(
        _query_fn([], ["legacy/script.lua", "legacy/tool.php"]),
        "p",
        "p.a.src",
        "p.a.sink",
    )
    assert result.verdict == FLOW_VERDICT_UNKNOWN
    assert result.gaps == ("legacy/script.lua", "legacy/tool.php")


def test_equal_names_without_an_edge_are_not_a_path() -> None:
    """Two identical (even nonexistent) qns must not report FOUND by name
    equality alone; only a genuine cycle through FLOWS_TO edges counts."""
    result = flow_reachability_verdict(
        _query_fn([("p.a", "p.b")], []),
        "p",
        "p.ghost",
        "p.ghost",
    )
    assert result.verdict == FLOW_VERDICT_NO_FLOW


def test_equal_names_with_a_real_cycle_are_found() -> None:
    result = flow_reachability_verdict(
        _query_fn([("p.a.src", "p.a.mid"), ("p.a.mid", "p.a.src")], []),
        "p",
        "p.a.src",
        "p.a.src",
    )
    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == ("p.a.src", "p.a.mid", "p.a.src")


def test_cycles_terminate() -> None:
    result = flow_reachability_verdict(
        _query_fn([("p.a", "p.b"), ("p.b", "p.a")], []),
        "p",
        "p.a",
        "p.z",
    )
    assert result.verdict == FLOW_VERDICT_NO_FLOW


def _module_props(mock_ingestor: MagicMock) -> dict[str, dict]:
    return {
        c.args[1]["qualified_name"]: c.args[1]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == "Module"
    }


def test_indexing_records_flow_coverage_per_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "covered.py").write_text("def f():\n    return 1\n")
    (temp_repo / "uncovered.lua").write_text("local function f() return 1 end\n")
    parsers, queries = load_parsers()
    if "lua" not in parsers:
        pytest.skip("lua parser not available")
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    )
    updater.run()
    modules = _module_props(mock_ingestor)
    project = temp_repo.name
    assert modules[f"{project}.covered"]["flow_covered"] is True
    assert modules[f"{project}.uncovered"]["flow_covered"] is False


def test_default_capture_reports_modules_uncovered(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Flow capture is opt-in: with the default selection, even a registered
    language's module is honestly uncovered."""
    (temp_repo / "covered.py").write_text("def f():\n    return 1\n")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )
    updater.run()
    modules = _module_props(mock_ingestor)
    assert modules[f"{temp_repo.name}.covered"]["flow_covered"] is False


def test_protobuf_export_preserves_flow_coverage(tmp_path: Path) -> None:
    """Full round trip: serialize to disk, reload, and read the property."""
    import codec.schema_pb2 as pb
    from codebase_rag import constants as cs
    from codebase_rag.services.protobuf_service import ProtobufFileIngestor

    ingestor = ProtobufFileIngestor(str(tmp_path))
    ingestor.ensure_node_batch(
        "Module",
        {
            "qualified_name": "p.covered",
            "name": "covered.py",
            "path": "covered.py",
            "flow_covered": True,
        },
    )
    ingestor.flush_all()
    raw = (tmp_path / cs.PROTOBUF_INDEX_FILE).read_bytes()
    index = pb.GraphCodeIndex.FromString(raw)
    modules = [n.module for n in index.nodes if n.WhichOneof("payload") == "module"]
    assert modules and modules[0].flow_covered is True


def test_inline_modules_are_never_spurious_coverage_gaps(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Every full Module emission either carries flow_covered (the bodied
    inline-mod producer stamps it, matching its file's coverage) or uses the
    synthetic inline path the gaps query excludes; either way an inline mod
    can never surface as a spurious coverage gap."""
    (temp_repo / "src").mkdir()
    (temp_repo / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n')
    (temp_repo / "src" / "lib.rs").write_text(
        "pub mod inner {\n    pub fn f() -> u32 { 1 }\n}\n"
    )
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    )
    updater.run()
    from codebase_rag import constants as cs

    for call in mock_ingestor.ensure_node_batch.call_args_list:
        if str(call.args[0]) != "Module":
            continue
        props = call.args[1]
        if "path" not in props:
            # Partial MERGE updates onto an existing node carry no path and
            # must not erase the property either.
            continue
        assert props.get("flow_covered") is True or str(props["path"]).startswith(
            cs.INLINE_MODULE_PATH_PREFIX
        ), props
