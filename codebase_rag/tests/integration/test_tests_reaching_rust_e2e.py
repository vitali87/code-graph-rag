# Real-Memgraph check for the bot review on PR #2913: a top-level Rust
# `#[test]` fn reaches `tests_reaching` through its dangling call. Neither
# its path (src/lib.rs) nor a test module names it a test; only its
# `decorators` do, and the caller lookup must return them. The eval double
# answers every delta query with one shared field list, so only a real
# database shows what the production query returns.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater, ReingestReport
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import observe
from codebase_rag.types_defs import PropertyParams, ResultRow

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

PROJECT = "rtreach"
UTIL = "pub fn target() -> i32 {\n    1\n}\n"
FILES = {
    "Cargo.toml": '[package]\nname = "rtreach"\nversion = "0.1.0"\n',
    "src/util.rs": UTIL,
    "src/lib.rs": (
        "mod util;\n\n#[test]\nfn uses_target() {\n    assert_eq!(util::target(), 1);\n}\n"
    ),
}


def test_a_top_level_rust_test_is_reached_through_its_dangling_call(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    root = tmp_path / PROJECT
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    memgraph_ingestor.ensure_constraints()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=memgraph_ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    memgraph_ingestor.flush_all()
    (root / "src/util.rs").write_text(UTIL.replace("fn target(", "fn target_v2("))

    def reingest() -> ReingestReport:
        report = updater.reingest(["src/util.rs"])
        memgraph_ingestor.flush_all()
        return report

    def fetch(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return memgraph_ingestor.fetch_all(
            query, dict(params) if params is not None else None
        )

    delta = observe(fetch, PROJECT, ["src/util.rs"], reingest, repo_root=root)

    assert delta["dangling_callers"]
    reached = {r["qualified_name"] for r in delta["tests_reaching"]}
    assert f"{PROJECT}.src.lib.uses_target" in reached
