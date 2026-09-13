"""Scoped re-ingest keeps an UNCHANGED file's RETURNS/ACCEPTS facts when it
shares a module qn with a re-parsed file (issue #1892).

`foo.py` and `foo/__init__.py` both derive `proj.foo`. The stale-fact filter
in `_reingest_delete` used to key on that module qn, so re-ingesting `foo.py`
together with the file defining the annotated type discarded the rehydrated
facts of `foo/__init__.py` too. Measured on the issue: the edge was NOT lost
on main, because the inbound-edge restore (#1527) rebuilt it; the fact path
was simply dead. The filter now keys on the fact's own file, the way the
Parameter mirror (#1891) does, so the edge has two independent paths back.
The regression therefore disables the restore: with it on, an edge-level
assertion passes on both filters and proves nothing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PendingTypeFact
from evals.cgr_graph import _StatefulIngestor

GADGET = "proj.models.Gadget"


def _edges(store: _StatefulIngestor, rel: str) -> set[tuple[str, str]]:
    return {(str(src), str(tgt)) for _sl, src, r, _tl, tgt in store.edges if r == rel}


def _accepts_of(store: _StatefulIngestor, owner_suffix: str) -> set[str]:
    return {
        tgt
        for src, tgt in _edges(store, cs.RelationshipType.ACCEPTS.value)
        if src.endswith(owner_suffix)
    }


def _repo(tmp_path: Path, colliding_source: str) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    (repo / "models.py").write_text("class Gadget:\n    pass\n", encoding="utf-8")
    (repo / "foo.py").write_text(
        "def use(gadget: Gadget) -> int:\n    return 0\n", encoding="utf-8"
    )
    (repo / "foo").mkdir()
    (repo / "foo" / "__init__.py").write_text(colliding_source, encoding="utf-8")
    return repo


def _updater(repo: Path, store: _StatefulIngestor) -> GraphUpdater:
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    return GraphUpdater(
        ingestor=store, repo_path=repo, parsers=parsers, queries=queries
    )


def _touch_and_reingest(repo: Path, store: _StatefulIngestor) -> None:
    # The type's file changes (so Gadget is deleted and recreated and every
    # ACCEPTS into it is detached) together with the colliding SIBLING of the
    # unchanged file; the unchanged file itself is not named.
    (repo / "models.py").write_text(
        "class Gadget:\n    pass\n# touched\n", encoding="utf-8"
    )
    (repo / "foo.py").write_text(
        "def use(gadget: Gadget) -> int:\n    return 2\n", encoding="utf-8"
    )
    _updater(repo, store).reingest([repo / "models.py", repo / "foo.py"])


@pytest.mark.parametrize(
    ("colliding_source", "owner_suffix"),
    [
        ("def other(gadget: Gadget) -> int:\n    return 1\n", ".other"),
        (
            "class Holder:\n    def other(self, gadget: Gadget) -> int:\n        return 1\n",
            ".Holder.other",
        ),
    ],
    ids=["function", "method"],
)
def test_the_unchanged_colliding_files_fact_survives_the_stale_filter(
    tmp_path: Path, colliding_source: str, owner_suffix: str
) -> None:
    repo = _repo(tmp_path, colliding_source)
    store = _StatefulIngestor()
    _updater(repo, store).run(force=True)
    # Fixture guard: the edge exists before the reingest.
    assert _accepts_of(store, owner_suffix) == {GADGET}

    survivors: list[list[str]] = []
    original_delete = GraphUpdater._reingest_delete

    def spy_delete(self: GraphUpdater, reparse, gone, hashes) -> None:  # type: ignore[no-untyped-def]
        original_delete(self, reparse, gone, hashes)
        survivors.append(
            [
                f.qualified_name
                for f in self.factory.definition_processor.pending_type_facts
            ]
        )

    # The restore is switched OFF so the fact path is the only way back for
    # the edge: with it on, both filters end with the edge present.
    with (
        patch.object(GraphUpdater, "_reingest_delete", spy_delete),
        patch.object(
            GraphUpdater, "_restore_inbound_edges", lambda self, captured: None
        ),
    ):
        _touch_and_reingest(repo, store)

    assert survivors, "the scoped reingest must run the stale filter"
    kept = survivors[-1]
    assert any(qn.endswith(owner_suffix) for qn in kept), (
        "the unchanged colliding file's fact was discarded with the re-parsed one's"
    )
    assert not any(qn.endswith(".use") for qn in kept), (
        "the re-parsed file's fact is stale"
    )
    assert _accepts_of(store, owner_suffix) == {GADGET}, (
        "the unchanged colliding file's ACCEPTS was not rebuilt from its fact"
    )
    assert _accepts_of(store, ".use") == {GADGET}


def test_a_fact_without_a_file_still_falls_back_to_the_module_key(
    tmp_path: Path,
) -> None:
    # A fact ingested with no path (no file identity) cannot be matched by
    # file; it keeps the old module-qn behaviour rather than surviving every
    # filter and emitting a stale edge.
    repo = _repo(tmp_path, "def other(gadget: Gadget) -> int:\n    return 1\n")
    store = _StatefulIngestor()
    updater = _updater(repo, store)
    updater.run(force=True)
    pending = updater.factory.definition_processor.pending_type_facts
    pending[:] = [
        PendingTypeFact("Function", "proj.foo.ghost", "proj.foo", "Gadget", None, None),
        PendingTypeFact(
            "Function", "proj.other.kept", "proj.other", "Gadget", None, None
        ),
    ]
    updater._reingest_delete({"foo.py": repo / "foo.py"}, {}, {})
    assert [fact.qualified_name for fact in pending] == ["proj.other.kept"]


def test_every_producer_records_the_owning_file(tmp_path: Path) -> None:
    # Both parser sites (function, method) and the rehydration from the graph
    # must fill the path, or the file-keyed filter silently degrades to the
    # module-qn key for that producer. The queue empties when the edges are
    # emitted, so the parse-time facts are captured at the emitter.
    from codebase_rag.parsers import type_facts as tf

    repo = _repo(
        tmp_path,
        "class Holder:\n    def other(self, gadget: Gadget) -> int:\n        return 1\n",
    )
    store = _StatefulIngestor()
    updater = _updater(repo, store)
    captured: list[PendingTypeFact] = []
    original = tf.emit_type_edges

    def spy(pending, resolver, ingestor):  # type: ignore[no-untyped-def]
        captured.extend(pending)
        return original(pending, resolver, ingestor)

    with patch.object(tf, "emit_type_edges", side_effect=spy):
        updater.run(force=True)
    # Same-stem siblings get distinct module qns in the graph (#1569), so the
    # owners are matched by name suffix rather than a spelled-out module.
    assert _path_of(captured, ".use") == "foo.py"
    assert _path_of(captured, ".Holder.other") == "foo/__init__.py"
    # Rehydration: a fresh updater re-reads the facts from the graph.
    fresh = _updater(repo, store)
    fresh._rehydrate_registry_from_graph()
    rehydrated = fresh.factory.definition_processor.pending_type_facts
    assert _path_of(rehydrated, ".use") == "foo.py"
    assert _path_of(rehydrated, ".Holder.other") == "foo/__init__.py"


def _path_of(facts: list[PendingTypeFact], owner_suffix: str) -> str | None:
    matches = [fact for fact in facts if fact.qualified_name.endswith(owner_suffix)]
    assert len(matches) == 1, [fact.qualified_name for fact in matches]
    return matches[0].path
