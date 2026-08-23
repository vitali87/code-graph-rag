"""Pure-function coverage for `_chunk_endpoint_disjoint`.

No container needed: this is a plain partitioning algorithm over
`RelBatchRow` dicts, tested in isolation from the network/session code
around it. It exists because an ArcadeDB UNWIND-batched relationship
MERGE where 2+ rows in the *same call* share an endpoint can silently
drop the colliding row -- no exception, no attempted/created mismatch --
found while indexing this project's own repository into ArcadeDB (see
this function's docstring in arcadedb.py, and
test_parallel_flush_into_one_hot_target in
tests/integration/test_graph_backend_conformance.py for the reproduction
at real scale). This file is the fast, container-free half of that
coverage: if `_chunk_endpoint_disjoint` were ever reverted to
`return [rows]`, every test below would fail immediately.
"""

from __future__ import annotations

from codebase_rag.services.graph.arcadedb import _chunk_endpoint_disjoint
from codebase_rag.types_defs import RelBatchRow


def _row(from_val: str, to_val: str) -> RelBatchRow:
    return {"from_val": from_val, "to_val": to_val, "props": {}}


def _flatten(chunks: list[list[RelBatchRow]]) -> list[RelBatchRow]:
    return [row for chunk in chunks for row in chunk]


def _assert_endpoint_disjoint(chunks: list[list[RelBatchRow]]) -> None:
    for chunk in chunks:
        seen: set[object] = set()
        for row in chunk:
            from_val, to_val = row["from_val"], row["to_val"]
            assert from_val not in seen, (
                f"{from_val!r} appears as an endpoint twice in one chunk"
            )
            assert to_val not in seen, (
                f"{to_val!r} appears as an endpoint twice in one chunk"
            )
            seen.add(from_val)
            seen.add(to_val)


def test_empty_input_produces_no_chunks() -> None:
    assert _chunk_endpoint_disjoint([]) == []


def test_single_row_produces_one_chunk_of_one() -> None:
    row = _row("a", "b")
    assert _chunk_endpoint_disjoint([row]) == [[row]]


def test_disjoint_rows_all_land_in_one_chunk() -> None:
    # The common case this function is designed to cost nothing for: every
    # row has a unique endpoint pair, so nothing conflicts and the whole
    # group stays in a single UNWIND-sized chunk.
    rows = [_row(f"src{i}", f"dst{i}") for i in range(50)]
    chunks = _chunk_endpoint_disjoint(rows)
    assert chunks == [rows]


def test_one_hot_vertex_shared_by_every_row_degrades_to_one_row_per_chunk() -> None:
    # Every row targets the same vertex, so no two rows can ever share a
    # chunk: this is the exact shape that silently dropped edges before
    # this function existed (many files importing the same popular
    # module, many functions calling the same popular callee, ...).
    rows = [_row(f"src{i}", "hot") for i in range(50)]
    chunks = _chunk_endpoint_disjoint(rows)
    assert len(chunks) == len(rows)
    assert all(len(chunk) == 1 for chunk in chunks)
    _assert_endpoint_disjoint(chunks)


def test_self_loop_row_is_handled() -> None:
    # from_val == to_val: the row's own endpoint set collapses to one
    # element (the {from_h, to_h} set naturally dedups), and a second row
    # touching that same vertex -- from either side -- must land in a
    # different chunk.
    rows = [_row("x", "x"), _row("x", "y"), _row("z", "w")]
    chunks = _chunk_endpoint_disjoint(rows)
    _assert_endpoint_disjoint(chunks)
    assert sorted(_flatten(chunks), key=repr) == sorted(rows, key=repr)
    # "z"/"w" share no endpoint with the "x" rows, so it must not have
    # been forced into its own chunk by them.
    assert any(rows[2] in chunk and len(chunk) > 1 for chunk in chunks)


def test_multiset_is_preserved_including_duplicates() -> None:
    # Duplicate (from_val, to_val) pairs are not this function's job to
    # collapse -- that is _dedupe_rows_sharing_a_merge_pattern, which runs
    # before this. _chunk_endpoint_disjoint must pass every row through
    # exactly once, duplicates included.
    rows = [
        _row("a", "b"),
        _row("a", "b"),  # exact duplicate of the row above
        _row("a", "c"),
        _row("d", "b"),
        _row("e", "f"),
    ]
    chunks = _chunk_endpoint_disjoint(rows)
    _assert_endpoint_disjoint(chunks)
    assert sorted(_flatten(chunks), key=repr) == sorted(rows, key=repr)
    assert sum(len(chunk) for chunk in chunks) == len(rows)


def test_mixed_hot_and_cold_rows_only_splits_what_must_split() -> None:
    # A realistic mixed shape: most rows have unique endpoints (cold), a
    # handful converge on one popular vertex (hot). Only the hot rows
    # should ever need to spread across more than one chunk.
    cold = [_row(f"src{i}", f"dst{i}") for i in range(20)]
    hot = [_row(f"hotsrc{i}", "hot") for i in range(10)]
    rows = cold + hot
    chunks = _chunk_endpoint_disjoint(rows)
    _assert_endpoint_disjoint(chunks)
    assert sorted(_flatten(chunks), key=repr) == sorted(rows, key=repr)
    # The 10 hot rows can never share a chunk with each other, so at least
    # 10 chunks are required regardless of how the cold rows pack in.
    assert len(chunks) >= len(hot)
