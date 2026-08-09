# A merged ambient namespace member (`declare namespace N { export function
# helper(): number }`) is where TS resolves a `helper.call(this)` receiver
# inside a value block of N; the binding index must see that introduction so
# the site has TWO relevant bindings and suppresses, instead of falling
# through to a same-named file-level function and emitting a wrong edge.
# Issue #996, surfaced by the #995 review.
from __future__ import annotations

from pathlib import Path

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"


class _Capture:
    def __init__(self) -> None:
        self.rels: list[tuple[PropertyValue, str, PropertyValue]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append((from_spec[2], str(rel_type), to_spec[2]))

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _run(tmp_path: Path, src: str, filename: str = "a.ts") -> _Capture:
    (tmp_path / filename).write_text(src)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return cap


def test_merged_ambient_member_suppresses_file_level_fallthrough(
    tmp_path: Path,
) -> None:
    cap = _run(
        tmp_path,
        """
function helper (): number { return 1 }
declare namespace N { export function helper (): number }
namespace N { export function use (): number { return helper.call(this) } }
""",
    )
    wrong = [
        r
        for r in cap.rels
        if r[1] == "CALLS"
        and str(r[0]).endswith(".use")
        and str(r[2]) == f"{PROJECT}.a.helper"
    ]
    assert not wrong, wrong


def test_overload_signatures_beside_their_implementation_still_resolve(
    tmp_path: Path,
) -> None:
    cap = _run(
        tmp_path,
        """
namespace N {
  export function pick (x: string): string
  export function pick (x: number): number
  export function pick (x: unknown): unknown { return x }
  export function use (): unknown { return pick.call(this, 1) }
}
""",
    )
    edges = [
        r
        for r in cap.rels
        if r[1] == "CALLS"
        and str(r[0]).endswith(".use")
        and str(r[2]).startswith(f"{PROJECT}.a.N.pick")
    ]
    assert edges, [r for r in cap.rels if r[1] == "CALLS"]


def test_without_ambient_merge_the_file_level_binding_still_resolves(
    tmp_path: Path,
) -> None:
    cap = _run(
        tmp_path,
        """
function helper (): number { return 1 }
namespace N { export function use (): number { return helper.call(this) } }
""",
    )
    edges = [
        r
        for r in cap.rels
        if r[1] == "CALLS"
        and str(r[0]).endswith(".use")
        and str(r[2]) == f"{PROJECT}.a.helper"
    ]
    assert edges, [r for r in cap.rels if r[1] == "CALLS"]
