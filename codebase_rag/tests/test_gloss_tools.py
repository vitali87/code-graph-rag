"""Writing and reading Gloss nodes through the MCP tools (issue #1808, stage two).

The fake store below answers exactly the fixed queries the gloss code issues
and raises on anything else, so a query the tests do not model cannot pass
by returning an empty list. Its `execute_write` mirrors the real statements'
one property that matters here: a MATCH on a subject that is not there
writes nothing and raises nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import gloss
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.types_defs import NodeType, PropertyDict, ResultRow

P = "proj"
RUN = f"{P}.app.run"
STORE = f"{P}.app.Store"
STORE_GET = f"{P}.app.Store.get"
UTIL_GET = f"{P}.util.get"
VALIDATE = f"{P}.util.validate"
BODY = "safe because validate() runs first"


def _node(
    label: str, qn: str, path: str, start: int, end: int, **extra: Any
) -> ResultRow:
    row: ResultRow = {
        cs.KEY_LABEL: label,
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_NAME: qn.rsplit(".", 1)[-1],
        cs.KEY_PATH: path,
        cs.KEY_START_LINE: start,
        cs.KEY_END_LINE: end,
    }
    row.update(extra)
    return row


NODES: list[ResultRow] = [
    _node("Module", f"{P}.app", "app.py", 1, 20),
    _node("Function", RUN, "app.py", 3, 8, anchor_hash="fp-run"),
    _node("Class", STORE, "app.py", 10, 18, anchor_hash="fp-store"),
    _node("Method", STORE_GET, "app.py", 11, 13, anchor_hash="fp-store-get"),
    # A property trio: the registry names the setter and deleter `x@<line>`
    # (issue #1808); the getter keeps the natural name.
    _node("Method", f"{STORE}.x", "app.py", 14, 15, decorators=["@property"]),
    _node("Method", f"{STORE}.x@16", "app.py", 16, 17, decorators=["@x.setter"]),
    _node("Method", f"{STORE}.x@18", "app.py", 18, 19, decorators=["@x.deleter"]),
    _node("Module", f"{P}.util", "util.py", 1, 10),
    _node("Function", UTIL_GET, "util.py", 1, 4),
    _node("Function", VALIDATE, "util.py", 6, 9, anchor_hash="fp-validate"),
]


class FakeGraph:
    """The fixture graph plus the glosses written into it."""

    def __init__(self, root: str = "/elsewhere/foreign-checkout") -> None:
        self.nodes = {str(n[cs.KEY_QUALIFIED_NAME]): n for n in NODES}
        self.glosses: dict[str, PropertyDict] = {}
        self.annotates: set[tuple[str, str]] = set()
        self.mentions: set[tuple[str, str]] = set()
        self.writes: list[tuple[str, PropertyDict]] = []
        self.root = root
        # When set, the subject is gone by the time the write statement runs.
        self.vanish_on_write = False

    def _gloss_row(self, key: str) -> ResultRow:
        row: ResultRow = dict(self.glosses[key])
        row[cs.KEY_QUALIFIED_NAME] = key
        row[cs.KEY_MENTIONS] = [qn for g, qn in self.mentions if g == key]
        return row

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        p = params or {}
        out: list[ResultRow] = []
        if query == cq.CYPHER_PROJECT_ROOT_PATH:
            return [{cs.KEY_ROOT_PATH: self.root}]
        if query in (cq.CYPHER_GRAPH_RESOLVE_NAME, cq.CYPHER_GRAPH_RESOLVE_LOCATION):
            assert p[cs.KEY_PROJECT_PREFIX] == f"{P}.", "every read is project-scoped"
        if query == cq.CYPHER_GRAPH_RESOLVE_NAME:
            for n in self.nodes.values():
                q = str(n[cs.KEY_QUALIFIED_NAME])
                if (
                    q == p[cs.KEY_QN]
                    or q.endswith(str(p[cs.KEY_SUFFIX]))
                    or n[cs.KEY_NAME] == p[cs.KEY_NAME]
                ):
                    out.append(n)
        elif query == cq.CYPHER_GRAPH_RESOLVE_LOCATION:
            line = int(p[cs.KEY_LINE])  # type: ignore[arg-type]
            for n in self.nodes.values():
                if n[cs.KEY_PATH] == p[cs.KEY_PATH] and int(
                    n[cs.KEY_START_LINE]  # type: ignore[arg-type]
                ) <= line <= int(n[cs.KEY_END_LINE]):  # type: ignore[arg-type]
                    out.append(n)
        elif query == cq.CYPHER_GLOSS_VARIANTS:
            prefix = str(p[cs.KEY_VARIANT_PREFIX])
            for n in self.nodes.values():
                q = str(n[cs.KEY_QUALIFIED_NAME])
                if q == p[cs.KEY_QN] or q.startswith(prefix):
                    out.append({**n, cs.KEY_DECORATORS: n.get(cs.KEY_DECORATORS)})
        elif query == cq.CYPHER_GLOSS_TARGET:
            n = self.nodes.get(str(p[cs.KEY_QN]))
            if n is not None:
                out.append(
                    {
                        cs.KEY_TARGET_HASH: n.get(cs.KEY_ANCHOR_HASH),
                        cs.KEY_NAME: n.get(cs.KEY_NAME),
                        cs.KEY_PATH: n.get(cs.KEY_PATH),
                        cs.KEY_START_LINE: n.get(cs.KEY_START_LINE),
                        cs.KEY_END_LINE: n.get(cs.KEY_END_LINE),
                    }
                )
        elif query == cq.CYPHER_GLOSS_READ:
            key = str(p[cs.KEY_QN])
            if key in self.glosses:
                out.append(self._gloss_row(key))
        elif query == cq.CYPHER_GLOSSES_ANNOTATING:
            out = [self._gloss_row(g) for g, qn in self.annotates if qn == p[cs.KEY_QN]]
        elif query == cq.CYPHER_GLOSSES_MENTIONING:
            out = [self._gloss_row(g) for g, qn in self.mentions if qn == p[cs.KEY_QN]]
        elif query in (cq.CYPHER_GLOSSES_ORPHANED_ON, cq.CYPHER_GLOSSES_ORPHANED_UNDER):
            attached = {g for g, _qn in self.annotates}
            under = query == cq.CYPHER_GLOSSES_ORPHANED_UNDER

            def in_project(props: PropertyDict) -> bool:
                recorded = props.get(cs.KEY_PROJECT)
                if recorded is not None:
                    return recorded == p[cs.KEY_PROJECT_NAME]
                return str(props.get(cs.KEY_TARGET_QN, "")).startswith(
                    str(p[cs.KEY_PROJECT_PREFIX])
                )

            def names_target(props: PropertyDict) -> bool:
                qn = str(props.get(cs.KEY_TARGET_QN, ""))
                if under:
                    return qn == p[cs.KEY_QN] or qn.startswith(
                        str(p[cs.KEY_VARIANT_PREFIX])
                    )
                return qn == p[cs.KEY_QN] or qn.endswith(str(p[cs.KEY_SUFFIX]))

            out = [
                self._gloss_row(g)
                for g, props in self.glosses.items()
                if in_project(props) and names_target(props) and g not in attached
            ]
        else:
            raise AssertionError(f"unexpected query: {query[:60]}")
        # Deliberately unsorted: the tools must sort.
        return list(reversed(out))

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        """The single write statement, all or nothing.

        Mirrors the real query's gate: the subject and EVERY mentioned
        definition must be present, or the row is dropped before any MERGE
        and nothing changes. `ON CREATE SET` keeps the first author and
        creation time; every other property is replaced (a null unsets it).
        """
        p = params or {}
        self.writes.append((query, p))
        if query != cq.CYPHER_GLOSS_WRITE:
            raise AssertionError(f"unexpected write: {query[:60]}")
        key = str(p[cs.KEY_QN])
        prefix = str(p[cs.KEY_PROJECT_PREFIX])
        target = str(p[cs.KEY_TARGET_QN])
        wanted = [str(m) for m in p[cs.KEY_MENTION_QNS]]  # type: ignore[union-attr]
        present = [
            qn for qn in [target, *wanted] if qn in self.nodes and qn.startswith(prefix)
        ]
        if self.vanish_on_write or len(present) != 1 + len(wanted):
            return
        created = {
            k: p[k]
            for k in (cs.KEY_CREATED_BY, cs.KEY_CREATED_AT)
            if key not in self.glosses
        }
        kept = {
            k: v
            for k, v in self.glosses.get(key, {}).items()
            if k in (cs.KEY_CREATED_BY, cs.KEY_CREATED_AT)
        }
        skip = {
            cs.KEY_QN,
            cs.KEY_PROJECT_PREFIX,
            cs.KEY_PROJECT_NAME,
            cs.KEY_CREATED_BY,
            cs.KEY_CREATED_AT,
        }
        self.glosses[key] = {
            **kept,
            **created,
            **{k: v for k, v in p.items() if k not in skip and v is not None},
            # `g.project = $project_name`: the parameter lands on the node
            # under the property name.
            cs.KEY_PROJECT: p[cs.KEY_PROJECT_NAME],
        }
        # A fresh write against a name is EXACT at that name: any earlier
        # subject edge (a note that had MOVED) is dropped and the repair
        # state cleared, mirroring the statement.
        self.annotates = {(g, qn) for g, qn in self.annotates if g != key}
        self.annotates.add((key, target))
        self.glosses[key].pop(cs.KEY_MOVED_FROM, None)
        self.glosses[key].pop(cs.KEY_CANDIDATE_QNS, None)
        # A repeat write replaces the note's mentions.
        self.mentions = {(g, qn) for g, qn in self.mentions if g != key}
        for qn in wanted:
            self.mentions.add((key, qn))


def _write(graph: FakeGraph, target: str, **kw: Any) -> Any:
    args = {"body": BODY, "kind": cs.GlossKind.SAFETY_PRECONDITION.value}
    args.update(kw)
    return gloss.write_gloss(graph.fetch_all, graph.execute_write, P, target, **args)


def _is_refusal(result: Any) -> bool:
    return isinstance(result, dict) and cs.DICT_KEY_ERROR in result


# --- writing -----------------------------------------------------------------


def test_a_note_on_an_exact_qualified_name_is_stored_with_its_edges() -> None:
    graph = FakeGraph()
    row = _write(graph, RUN, mentions=VALIDATE)
    assert not _is_refusal(row)
    key = row["qualified_name"]
    assert key.startswith(cs.GLOSS_ID_PREFIX)
    assert row["target_qn"] == RUN
    assert row["anchor_state"] == cs.GlossAnchorState.EXACT.value
    assert row["target_hash"] == "fp-run"
    assert row["kind"] == cs.GlossKind.SAFETY_PRECONDITION.value
    assert row["status"] == cs.GLOSS_STATUS_ACCEPTED
    assert row["created_by"] == cs.GLOSS_DEFAULT_AUTHOR
    assert row["body"] == BODY
    assert row["mentions"] == [VALIDATE]
    assert graph.annotates == {(key, RUN)}
    assert graph.mentions == {(key, VALIDATE)}
    # The note carries its own record of what it is attached to, which the
    # post-sync re-anchor pass rebuilds the edges from.
    assert graph.glosses[key][cs.KEY_TARGET_QN] == RUN
    assert graph.glosses[key][cs.KEY_MENTION_QNS] == [VALIDATE]


def test_the_same_note_twice_is_one_node() -> None:
    graph = FakeGraph()
    first = _write(graph, RUN)
    second = _write(graph, RUN)
    assert first["qualified_name"] == second["qualified_name"]
    assert len(graph.glosses) == 1


def test_a_different_body_is_a_different_gloss() -> None:
    graph = FakeGraph()
    _write(graph, RUN)
    _write(graph, RUN, body="mirrors Store.get")
    assert len(graph.glosses) == 2


def test_a_bare_name_matching_several_definitions_is_refused_with_candidates() -> None:
    graph = FakeGraph()
    result = _write(graph, "get")
    assert _is_refusal(result)
    assert "2 definitions" in result[cs.DICT_KEY_ERROR]
    assert [c["qualified_name"] for c in result["candidates"]] == [
        STORE_GET,
        UTIL_GET,
    ]
    assert graph.writes == []


def test_a_dotted_suffix_matching_one_definition_is_accepted() -> None:
    # `resolve` also returns every `get` by bare name; the documented
    # `Class.method` target form must still land on the one it suffixes.
    graph = FakeGraph()
    row = _write(graph, "Store.get")
    assert row["target_qn"] == STORE_GET


def test_a_bare_name_matching_one_definition_is_accepted() -> None:
    graph = FakeGraph()
    row = _write(graph, "validate")
    assert row["target_qn"] == VALIDATE


def test_an_unknown_target_is_refused_and_writes_nothing() -> None:
    graph = FakeGraph()
    result = _write(graph, "nothing_here")
    assert _is_refusal(result)
    assert "nothing_here" in result[cs.DICT_KEY_ERROR]
    assert "candidates" not in result
    assert graph.writes == []


def test_an_unknown_kind_is_refused_naming_the_kinds() -> None:
    graph = FakeGraph()
    result = _write(graph, RUN, kind="todo")
    assert _is_refusal(result)
    for kind in cs.GlossKind:
        assert kind.value in result[cs.DICT_KEY_ERROR]
    assert graph.writes == []


def test_a_blank_body_is_refused() -> None:
    graph = FakeGraph()
    result = _write(graph, RUN, body="   \n")
    assert _is_refusal(result)
    assert graph.writes == []


def test_an_unresolvable_mention_refuses_the_whole_write() -> None:
    graph = FakeGraph()
    result = _write(graph, RUN, mentions=f"{VALIDATE}, get")
    assert _is_refusal(result)
    assert "'get'" in result[cs.DICT_KEY_ERROR]
    assert graph.writes == [], "resolution happens before any write"


def test_a_location_anchors_to_the_innermost_definition() -> None:
    graph = FakeGraph()
    row = _write(graph, "app.py:12")
    # Line 12 is inside the module (1-20), the class (10-18) and the method
    # (11-13); the method is what the line "is in".
    assert row["target_qn"] == STORE_GET
    assert row["target_hash"] == "fp-store-get"


def test_a_subject_that_vanished_before_the_write_is_reported_not_written() -> None:
    graph = FakeGraph()
    graph.vanish_on_write = True
    result = _write(graph, RUN, mentions=VALIDATE)
    assert _is_refusal(result)
    assert "not written" in result[cs.DICT_KEY_ERROR]
    assert graph.glosses == {}
    assert graph.annotates == set()
    assert graph.mentions == set()


def test_author_and_commit_are_recorded_when_given() -> None:
    graph = FakeGraph()
    row = _write(graph, RUN, author=" cgr-1 ", commit_sha="abc123")
    assert row["created_by"] == "cgr-1"
    assert row["commit_sha"] == "abc123"
    bare = _write(graph, VALIDATE)
    assert bare["commit_sha"] is None
    # Rewriting the same note without a commit unsets the one it had.
    again = _write(graph, RUN)
    assert again["qualified_name"] == row["qualified_name"]
    assert again["commit_sha"] is None


def test_a_mention_that_vanished_before_the_write_writes_nothing() -> None:
    # The write is one statement gated on every mentioned definition being
    # present, so a peer re-index removing one between resolving and writing
    # leaves no note at all, never a note with half its edges.
    graph = FakeGraph()
    _write(graph, VALIDATE, body="pure; no I/O")  # an unrelated note survives
    original_fetch = graph.fetch_all

    def fetch_then_drop(
        query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        rows = original_fetch(query, params)
        if query == cq.CYPHER_GLOSS_TARGET:
            # The last read before the write: the mention resolved, then left.
            graph.nodes.pop(UTIL_GET, None)
        return rows

    result = gloss.write_gloss(
        fetch_then_drop,
        graph.execute_write,
        P,
        RUN,
        body=BODY,
        kind=cs.GlossKind.MIRRORS.value,
        mentions=UTIL_GET,
    )
    assert _is_refusal(result)
    assert "not written" in result[cs.DICT_KEY_ERROR]
    assert len(graph.glosses) == 1, "only the unrelated note remains"
    assert not any(qn == RUN for _, qn in graph.annotates)


def test_a_repeat_write_replaces_the_mentions() -> None:
    graph = FakeGraph()
    first = _write(graph, RUN, mentions=VALIDATE)
    key = first["qualified_name"]
    assert graph.mentions == {(key, VALIDATE)}
    changed = _write(graph, RUN, mentions=UTIL_GET)
    assert changed["mentions"] == [UTIL_GET]
    assert graph.mentions == {(key, UTIL_GET)}
    cleared = _write(graph, RUN)
    assert cleared["mentions"] == []
    assert graph.mentions == set()


def test_a_rejected_repeat_write_is_not_reported_as_the_old_note() -> None:
    # The node already exists, so its presence after a gated-out statement
    # proves nothing; the per-call nonce is what tells this write ran.
    graph = FakeGraph()
    first = _write(graph, RUN, author="first-agent")
    original_fetch = graph.fetch_all

    def fetch_then_drop(
        query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        rows = original_fetch(query, params)
        if query == cq.CYPHER_GLOSS_TARGET:
            graph.nodes.pop(UTIL_GET, None)
        return rows

    result = gloss.write_gloss(
        fetch_then_drop,
        graph.execute_write,
        P,
        RUN,
        body=BODY,
        kind=cs.GlossKind.SAFETY_PRECONDITION.value,
        mentions=UTIL_GET,
        author="retry-agent",
        commit_sha="c2",
    )
    assert _is_refusal(result)
    assert "not written" in result[cs.DICT_KEY_ERROR]
    stored = graph.glosses[first["qualified_name"]]
    assert stored[cs.KEY_CREATED_BY] == "first-agent"
    assert cs.KEY_COMMIT_SHA not in stored, "the old note is exactly as it was"
    assert graph.mentions == set()


def test_a_repeat_write_keeps_the_original_author_and_time() -> None:
    graph = FakeGraph()
    first = _write(graph, RUN, author="first-agent")
    graph.glosses[first["qualified_name"]][cs.KEY_CREATED_AT] = (
        "2026-01-01T00:00:00+00:00"
    )
    again = _write(graph, RUN, author="retry-agent", commit_sha="c2", mentions=VALIDATE)
    assert again["qualified_name"] == first["qualified_name"]
    assert again["created_by"] == "first-agent"
    assert again["created_at"] == "2026-01-01T00:00:00+00:00"
    # Everything that is not creation metadata follows the latest write.
    assert again["commit_sha"] == "c2"
    assert again["mentions"] == [VALIDATE]


def test_a_target_without_a_fingerprint_stores_no_hash() -> None:
    # `util.get` is the fixture definition indexed without a hash.
    graph = FakeGraph()
    row = _write(graph, UTIL_GET)
    assert row["target_hash"] is None
    assert cs.KEY_TARGET_HASH not in graph.glosses[row["qualified_name"]]


def test_the_id_is_a_pure_function_of_subject_kind_and_body() -> None:
    a = gloss.gloss_id(RUN, "invariant", "x")
    assert a == gloss.gloss_id(RUN, "invariant", "x")
    assert a != gloss.gloss_id(RUN, "mirrors", "x")
    assert a != gloss.gloss_id(VALIDATE, "invariant", "x")
    assert len(a) == len(cs.GLOSS_ID_PREFIX) + cs.GLOSS_ID_HEX_LENGTH


# --- reading -----------------------------------------------------------------


def test_read_separates_notes_on_a_symbol_from_notes_that_mention_it() -> None:
    graph = FakeGraph()
    on_run = _write(graph, RUN, mentions=VALIDATE)
    on_validate = _write(graph, VALIDATE, body="pure; no I/O")
    about_validate = gloss.glosses_for(graph.fetch_all, P, VALIDATE)
    assert about_validate["target"]["qualified_name"] == VALIDATE
    assert [g["qualified_name"] for g in about_validate["annotating"]] == [
        on_validate["qualified_name"]
    ]
    assert [g["qualified_name"] for g in about_validate["mentioning"]] == [
        on_run["qualified_name"]
    ]
    about_run = gloss.glosses_for(graph.fetch_all, P, RUN)
    assert [g["qualified_name"] for g in about_run["annotating"]] == [
        on_run["qualified_name"]
    ]
    assert about_run["mentioning"] == []


def test_read_orders_oldest_first_then_by_key_whatever_the_store_order() -> None:
    graph = FakeGraph()
    for key, created in (
        ("gloss:c", "2026-09-13T10:00:00+00:00"),
        ("gloss:a", "2026-09-13T11:00:00+00:00"),
        ("gloss:b", "2026-09-13T10:00:00+00:00"),
    ):
        graph.glosses[key] = {cs.KEY_CREATED_AT: created, cs.KEY_BODY: key}
        graph.annotates.add((key, RUN))
    result = gloss.glosses_for(graph.fetch_all, P, RUN)
    assert [g["qualified_name"] for g in result["annotating"]] == [
        "gloss:b",
        "gloss:c",
        "gloss:a",
    ]


def test_read_on_a_symbol_with_no_notes_is_empty_not_an_error() -> None:
    graph = FakeGraph()
    result = gloss.glosses_for(graph.fetch_all, P, RUN)
    assert result["annotating"] == []
    assert result["mentioning"] == []


def test_read_refuses_an_ambiguous_name_the_same_way_as_a_write() -> None:
    graph = FakeGraph()
    result = gloss.glosses_for(graph.fetch_all, P, "get")
    assert _is_refusal(result)
    assert len(result["candidates"]) == 2


def test_read_on_a_gone_definition_returns_its_orphaned_notes_with_the_error() -> None:
    # Stage four of #1808: a note whose definition's name is gone is LOST or
    # AMBIGUOUS and attached to nothing, so the read that used to find it
    # attached would report "no definition" and nothing else. The refusal
    # stands (the name does not resolve, and nothing is re-bound), and the
    # notes written against that exact name come back with it, state and
    # candidates included, so the orphaning is visible rather than silent.
    graph = FakeGraph()
    gone = f"{P}.app.removed"
    graph.glosses["gloss:lost"] = {
        cs.KEY_TARGET_QN: gone,
        cs.KEY_BODY: "b1",
        cs.KEY_CREATED_AT: "2026-09-13T10:00:00+00:00",
        cs.KEY_ANCHOR_STATE: cs.GlossAnchorState.LOST.value,
    }
    graph.glosses["gloss:amb"] = {
        cs.KEY_TARGET_QN: gone,
        cs.KEY_BODY: "b2",
        cs.KEY_CREATED_AT: "2026-09-13T09:00:00+00:00",
        cs.KEY_ANCHOR_STATE: cs.GlossAnchorState.AMBIGUOUS.value,
        cs.KEY_CANDIDATE_QNS: [UTIL_GET, STORE_GET],
    }
    # A note on the same old name that has since MOVED is attached, so it is
    # not orphaned and must not appear here.
    graph.glosses["gloss:moved"] = {
        cs.KEY_TARGET_QN: RUN,
        cs.KEY_MOVED_FROM: gone,
        cs.KEY_BODY: "b3",
        cs.KEY_CREATED_AT: "2026-09-13T08:00:00+00:00",
        cs.KEY_ANCHOR_STATE: cs.GlossAnchorState.MOVED.value,
    }
    graph.annotates.add(("gloss:moved", RUN))
    result = gloss.glosses_for(graph.fetch_all, P, gone)
    assert _is_refusal(result)
    assert cs.KEY_CANDIDATES not in result
    orphaned = result[cs.KEY_ORPHANED]
    assert [g["qualified_name"] for g in orphaned] == ["gloss:amb", "gloss:lost"]
    assert orphaned[0]["anchor_state"] == cs.GlossAnchorState.AMBIGUOUS.value
    assert orphaned[0]["candidate_qns"] == [STORE_GET, UTIL_GET], "sorted"
    assert orphaned[1]["anchor_state"] == cs.GlossAnchorState.LOST.value
    assert orphaned[1]["candidate_qns"] == []


def test_a_repeat_write_of_a_moved_note_replaces_its_subject_edge() -> None:
    # The key is deterministic in (subject, kind, body), so writing the same
    # note against its ORIGINAL name after it has MOVED finds the same node,
    # still attached to the definition it followed. A name-keyed MERGE added a
    # second ANNOTATES edge (bot review: 1 -> 2 edges reproduced). The write
    # drops any other subject edge first and clears the repair state.
    graph = FakeGraph()
    row = _write(graph, RUN)
    key = row["qualified_name"]
    # The note follows its hash to another definition.
    graph.annotates = {(g, qn) for g, qn in graph.annotates if g != key}
    graph.annotates.add((key, VALIDATE))
    graph.glosses[key][cs.KEY_TARGET_QN] = VALIDATE
    graph.glosses[key][cs.KEY_MOVED_FROM] = RUN
    graph.glosses[key][cs.KEY_ANCHOR_STATE] = cs.GlossAnchorState.MOVED.value
    again = _write(graph, RUN)
    assert again["qualified_name"] == key
    assert {qn for g, qn in graph.annotates if g == key} == {RUN}, "one subject"
    assert again["moved_from"] is None
    assert again["anchor_state"] == cs.GlossAnchorState.EXACT.value
    q = cq.CYPHER_GLOSS_WRITE
    # The stale-subject deletion precedes the new subject MERGE.
    stale = q.index(
        f"OPTIONAL MATCH (g)-[old:{cs.RelationshipType.ANNOTATES.value}]->(prev)"
    )
    assert "WHERE prev <> t" in q
    assert "FOREACH (edge IN stale_subjects | DELETE edge)" in q
    merge = q.index(f"MERGE (g)-[:{cs.RelationshipType.ANNOTATES.value}]->(t)")
    assert stale < merge
    assert "g.moved_from = null, g.candidate_qns = null" in q


def test_a_written_note_records_its_project() -> None:
    # Recorded, not derived: a project name may contain dots, and the repair
    # pass and the orphan read scope on this field (local review).
    graph = FakeGraph()
    row = _write(graph, RUN)
    assert graph.glosses[row["qualified_name"]][cs.KEY_PROJECT] == P
    assert "g.project = $project_name" in cq.CYPHER_GLOSS_WRITE


def test_read_on_a_gone_definition_accepts_the_dotted_suffix_form() -> None:
    # The tool text promises resolve-style names, so `removed` and
    # `app.removed` must find the notes written against `proj.app.removed`.
    graph = FakeGraph()
    gone = f"{P}.app.removed"
    graph.glosses["gloss:lost"] = {
        cs.KEY_TARGET_QN: gone,
        cs.KEY_PROJECT: P,
        cs.KEY_BODY: "b",
        cs.KEY_CREATED_AT: "2026-09-13T10:00:00+00:00",
        cs.KEY_ANCHOR_STATE: cs.GlossAnchorState.LOST.value,
    }
    for name in ("removed", "app.removed", gone):
        result = gloss.glosses_for(graph.fetch_all, P, name)
        assert _is_refusal(result), name
        assert [g["qualified_name"] for g in result[cs.KEY_ORPHANED]] == [
            "gloss:lost"
        ], name
    # A suffix that is not dot-aligned is not the name: `moved` is not
    # `.removed`.
    assert cs.KEY_ORPHANED not in gloss.glosses_for(graph.fetch_all, P, "moved")


def test_read_on_a_gone_definition_never_shows_another_projects_notes() -> None:
    # The full qualified name of another project's gone definition resolves
    # to nothing here, and its notes are that project's to show, not ours.
    graph = FakeGraph()
    graph.glosses["gloss:theirs"] = {
        cs.KEY_TARGET_QN: "other.app.removed",
        cs.KEY_PROJECT: "other",
        cs.KEY_BODY: "b",
        cs.KEY_CREATED_AT: "2026-09-13T10:00:00+00:00",
        cs.KEY_ANCHOR_STATE: cs.GlossAnchorState.LOST.value,
    }
    for name in ("other.app.removed", "removed"):
        assert cs.KEY_ORPHANED not in gloss.glosses_for(graph.fetch_all, P, name)


def test_read_on_a_gone_definition_with_no_notes_is_a_plain_refusal() -> None:
    graph = FakeGraph()
    result = gloss.glosses_for(graph.fetch_all, P, f"{P}.app.never")
    assert _is_refusal(result)
    assert cs.KEY_ORPHANED not in result, "an empty list would read as a finding"


def test_read_on_an_ambiguous_name_does_not_look_for_orphans() -> None:
    # An ambiguous name is not a gone name; the refusal carries candidates
    # and nothing else, and no orphan read is issued for it.
    graph = FakeGraph()
    seen: list[str] = []

    def spy(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        seen.append(query)
        return graph.fetch_all(query, params)

    result = gloss.glosses_for(spy, P, "get")
    assert _is_refusal(result)
    assert cq.CYPHER_GLOSSES_ORPHANED_ON not in seen


def test_a_moved_note_reads_back_with_where_it_came_from() -> None:
    graph = FakeGraph()
    graph.glosses["gloss:moved"] = {
        cs.KEY_TARGET_QN: RUN,
        cs.KEY_MOVED_FROM: f"{P}.app.old_run",
        cs.KEY_BODY: "b",
        cs.KEY_CREATED_AT: "2026-09-13T08:00:00+00:00",
        cs.KEY_ANCHOR_STATE: cs.GlossAnchorState.MOVED.value,
    }
    graph.annotates.add(("gloss:moved", RUN))
    result = gloss.glosses_for(graph.fetch_all, P, RUN)
    [row] = result["annotating"]
    assert row["anchor_state"] == cs.GlossAnchorState.MOVED.value
    assert row["moved_from"] == f"{P}.app.old_run"
    assert row["candidate_qns"] == []


# --- the MCP surface ----------------------------------------------------------


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _registry(graph: FakeGraph, tmp_path: Path) -> MCPToolsRegistry:
    ingestor = MagicMock()
    ingestor.fetch_all.side_effect = graph.fetch_all
    ingestor.execute_write.side_effect = graph.execute_write
    ingestor.list_projects.return_value = [P, "other"]
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        return MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=ingestor, cypher_gen=MagicMock()
        )


def test_both_tools_are_advertised_with_the_shared_descriptions(tmp_path: Path) -> None:
    registry = _registry(FakeGraph(), tmp_path)
    advertised = {s.name: s for s in registry.get_tool_schemas()}
    for name in (cs.MCPToolName.ANNOTATE, cs.MCPToolName.GLOSSES):
        assert name in advertised, "a handler nothing advertises has no caller"
        assert advertised[name].description == td.MCP_TOOLS[name]
        meta = registry._tools[name]
        assert meta.returns_json is True
        assert cs.MCPParamName.PROJECT in meta.input_schema["properties"]
    annotate = registry._tools[cs.MCPToolName.ANNOTATE].input_schema
    assert set(annotate["required"]) == {
        cs.MCPParamName.TARGET,
        cs.MCPParamName.BODY,
        cs.MCPParamName.KIND,
    }
    assert cs.MCPParamName.MENTIONS in annotate["properties"]
    assert cs.MCPParamName.AUTHOR in annotate["properties"]
    assert registry._tools[cs.MCPToolName.GLOSSES].input_schema["required"] == [
        cs.MCPParamName.TARGET
    ]


@pytest.mark.anyio
async def test_mcp_annotate_writes_and_glosses_reads_it_back(tmp_path: Path) -> None:
    graph = FakeGraph()
    registry = _registry(graph, tmp_path)
    row = await registry.annotate(
        target=RUN,
        body=BODY,
        kind=cs.GlossKind.INVARIANT.value,
        mentions=VALIDATE,
        project=P,
    )
    assert not _is_refusal(row)
    assert graph.annotates == {(row["qualified_name"], RUN)}
    read = await registry.glosses(target=VALIDATE, project=P)
    assert [g["qualified_name"] for g in read["mentioning"]] == [row["qualified_name"]]


@pytest.mark.anyio
async def test_mcp_stamps_the_commit_only_for_the_servers_own_checkout(
    tmp_path: Path,
) -> None:
    own = FakeGraph(root=str(tmp_path))
    with patch("codebase_rag.mcp.tools.head_commit", return_value="deadbeef") as head:
        row = await _registry(own, tmp_path).annotate(
            target=RUN, body=BODY, kind=cs.GlossKind.INVARIANT.value, project=P
        )
    assert row["commit_sha"] == "deadbeef"
    head.assert_called_once_with(tmp_path.resolve())

    foreign = FakeGraph()
    with patch("codebase_rag.mcp.tools.head_commit", return_value="deadbeef") as head:
        row = await _registry(foreign, tmp_path).annotate(
            target=RUN, body=BODY, kind=cs.GlossKind.INVARIANT.value, project=P
        )
    assert row["commit_sha"] is None
    head.assert_not_called()


@pytest.mark.anyio
async def test_mcp_unknown_project_is_refused_before_any_write(tmp_path: Path) -> None:
    graph = FakeGraph()
    registry = _registry(graph, tmp_path)
    result = await registry.annotate(
        target=RUN, body=BODY, kind=cs.GlossKind.INVARIANT.value, project="typo"
    )
    assert _is_refusal(result)
    assert "typo" in result[cs.DICT_KEY_ERROR]
    assert graph.writes == []
    registry.ingestor.fetch_all.assert_not_called()


@pytest.mark.anyio
async def test_mcp_refuses_both_tools_while_the_graph_is_partial(
    tmp_path: Path,
) -> None:
    """A partial graph makes an ambiguous name look unique, so a note written
    against it lands on the wrong definition; both tools refuse like every
    other graph reader, and the write never reaches the store."""
    graph = FakeGraph()
    registry = _registry(graph, tmp_path)
    registry._graph_incomplete = True
    registry._incomplete_project = P
    written = await registry.annotate(
        target=RUN, body=BODY, kind=cs.GlossKind.INVARIANT.value, project=P
    )
    read = await registry.glosses(target=RUN, project=P)
    assert _is_refusal(written)
    assert _is_refusal(read)
    assert graph.writes == []
    # The control: the same registry with the flag clear writes.
    registry._graph_incomplete = False
    registry._incomplete_project = None
    ok = await registry.annotate(
        target=RUN, body=BODY, kind=cs.GlossKind.INVARIANT.value, project=P
    )
    assert not _is_refusal(ok)
    assert len(graph.glosses) == 1


@pytest.mark.anyio
async def test_mcp_store_errors_are_reported_not_raised(tmp_path: Path) -> None:
    registry = _registry(FakeGraph(), tmp_path)
    registry.ingestor.execute_write.side_effect = RuntimeError("down")
    result = await registry.annotate(
        target=RUN, body=BODY, kind=cs.GlossKind.INVARIANT.value, project=P
    )
    assert _is_refusal(result)
    assert "down" in result[cs.DICT_KEY_ERROR]


# --- surviving an incremental re-parse -----------------------------------------


def _inbound_rels(query: str) -> set[str]:
    match = re.search(r"\[r:([A-Z_|]+)\]", query)
    assert match, "the capture query must name its relation list"
    return set(match.group(1).split("|"))


def test_the_inbound_capture_names_both_gloss_edges() -> None:
    # A gloss lives only in the graph: when its subject's file is re-parsed
    # the subtree is deleted and recreated, and an edge the capture does not
    # name is not restored. That would orphan every note on the next update.
    rels = _inbound_rels(cs.CYPHER_INBOUND_EDGES)
    assert cs.RelationshipType.ANNOTATES.value in rels
    assert cs.RelationshipType.MENTIONS.value in rels


def test_the_eval_emulator_restores_the_same_relations_as_production() -> None:
    # The emulator matches the capture query by VALUE and reimplements it in
    # Python over its own set (see the comment above CYPHER_INBOUND_EDGES), so
    # this is the one axis of that query a unit test can hold in step.
    from evals.cgr_graph import _INBOUND_DEPENDENT_RELS

    assert set(_INBOUND_DEPENDENT_RELS) == _inbound_rels(cs.CYPHER_INBOUND_EDGES)


def _captured(caller_label: str, caller_qn: str, rel: str, target_qn: str) -> ResultRow:
    return {
        cs.KEY_CALLER_LABEL: caller_label,
        cs.KEY_CALLER_QN: caller_qn,
        cs.KEY_REL: rel,
        cs.KEY_TARGET_LABEL: "Function",
        cs.KEY_TARGET_QN: target_qn,
        cs.KEY_CALLER_PATH: None,
        cs.KEY_PROPS: {},
    }


def test_restoring_a_gloss_edge_bypasses_the_capture_filter(tmp_path: Path) -> None:
    """With the GLOSSES group off (the default), the filtering sink drops an
    ANNOTATES emission. A restore is not an emission: the edge already
    existed, so it must reach the store regardless of the selection."""
    ingestor = MagicMock()
    updater = GraphUpdater(
        ingestor=ingestor, repo_path=tmp_path, parsers={}, queries={}
    )
    assert not updater.capture.rel_enabled(cs.RelationshipType.ANNOTATES)
    updater.function_registry[RUN] = NodeType.FUNCTION
    updater._restore_inbound_edges(
        [
            _captured("Gloss", "gloss:abc", "ANNOTATES", RUN),
            _captured("Gloss", "gloss:abc", "MENTIONS", RUN),
            _captured("Function", f"{P}.other.caller", "CALLS", RUN),
        ]
    )
    restored = {
        (call.args[0][0], call.args[1], call.args[2][2])
        for call in ingestor.ensure_relationship_batch.call_args_list
    }
    assert restored == {
        ("Gloss", "ANNOTATES", RUN),
        ("Gloss", "MENTIONS", RUN),
        ("Function", "CALLS", RUN),
    }


class _RaisingStore:
    # A real class, not a MagicMock: the capture is gated on the runtime
    # QueryProtocol check, which a bare mock does not satisfy.
    def fetch_all(self, query: str, params: PropertyDict | None = None) -> list:
        raise RuntimeError("store down")

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def test_a_capture_outage_aborts_an_incremental_run(tmp_path: Path) -> None:
    updater = GraphUpdater(
        ingestor=_RaisingStore(),  # type: ignore[arg-type]
        repo_path=tmp_path,
        parsers={},
        queries={},
    )
    updater._is_full_build = False
    with pytest.raises(RuntimeError, match="store down"):
        updater._capture_inbound_edges(["app.py"])


def test_a_capture_outage_on_a_full_build_names_how_notes_come_back() -> None:
    # A full build continues over an unreadable graph (an existing contract);
    # the warning must say how the gloss edges, which have no source to be
    # re-derived from, are recovered: by the re-anchor pass, not by re-parsing.
    from codebase_rag import logs as lg

    assert "Gloss" in lg.INBOUND_CAPTURE_FAILED
    assert "re-attached" in lg.INBOUND_CAPTURE_FAILED


def test_a_capture_outage_on_a_full_build_warns_and_continues(tmp_path: Path) -> None:
    from codebase_rag import logs as lg

    updater = GraphUpdater(
        ingestor=_RaisingStore(),  # type: ignore[arg-type]
        repo_path=tmp_path,
        parsers={},
        queries={},
    )
    updater._is_full_build = True
    with patch("codebase_rag.graph_updater.logger") as log:
        assert updater._capture_inbound_edges(["app.py"]) == []
    log.warning.assert_called_once_with(lg.INBOUND_CAPTURE_FAILED)


class _RecordingStore:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.fail = False
        # When set, the store reports one note the name tier left unattached
        # (no comparable hash), so the repair tier has something to mark.
        self.unanchored: list[ResultRow] = []

    def fetch_all(self, query: str, params: PropertyDict | None = None) -> list:
        if query == cq.CYPHER_UNANCHORED_GLOSSES:
            return list(self.unanchored)
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        if self.fail:
            raise RuntimeError("store down")
        self.writes.append(query)

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(self, *args: object, **kwargs: object) -> None:
        return None

    def flush_all(self) -> None:
        return None


def test_reanchoring_rebuilds_edges_from_the_notes_own_record(tmp_path: Path) -> None:
    # The statements read the note's recorded names, not a captured edge set,
    # so a rebuild that recreated the definitions (or a capture that could
    # not be read) still ends with every note attached.
    store = _RecordingStore()
    store.unanchored = [
        {
            cs.KEY_QUALIFIED_NAME: "gloss:orphan",
            cs.KEY_TARGET_QN: f"{P}.gone.f",
            cs.KEY_TARGET_HASH: None,
            cs.KEY_ANCHOR_STATE: cs.GlossAnchorState.EXACT.value,
        }
    ]
    updater = GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=tmp_path,
        parsers={},
        queries={},
    )
    updater._reanchor_glosses()
    # Name tier first; then the repair tier places or marks what the name
    # tier left unattached (here: one note with no comparable hash, marked
    # LOST); then the mentions restore, so a note that has just MOVED gets
    # its MENTIONS edges back in the same run; then grading.
    assert store.writes == [
        cq.CYPHER_REANCHOR_GLOSSES,
        cq.CYPHER_GLOSS_MARK,
        cq.CYPHER_REANCHOR_GLOSS_MENTIONS,
        cq.CYPHER_GRADE_GLOSS_ANCHORS,
    ]
    assert "g.target_qn" in cq.CYPHER_REANCHOR_GLOSSES
    assert "g.mention_qns" in cq.CYPHER_REANCHOR_GLOSS_MENTIONS
    # Only an unattached note is re-anchored; an attached one is left alone.
    assert "WHERE subjects = 0" in cq.CYPHER_REANCHOR_GLOSSES


def test_reanchoring_by_name_resets_the_state_it_left_behind() -> None:
    # A note the previous run graded LOST or AMBIGUOUS whose name has come
    # back is attached again and reads EXACT (grading may demote it to STALE
    # straight after); a MOVED note stays MOVED, since `target_qn` is by then
    # the name it moved to. The candidate list belongs to AMBIGUOUS alone.
    q = cq.CYPHER_REANCHOR_GLOSSES
    assert "SET g.anchor_state = CASE WHEN g.moved_from IS NULL" in q
    assert (
        f"THEN '{cs.GlossAnchorState.EXACT.value}' "
        f"ELSE '{cs.GlossAnchorState.MOVED.value}' END" in q
    )
    assert "g.candidate_qns = null" in q
    assert "DELETE" not in q


def test_grading_compares_the_recorded_hash_with_the_subjects_current_one() -> None:
    # After re-anchoring, a note whose recorded hash no longer matches the
    # subject's `anchor_hash` reads STALE; a match reads EXACT again; a
    # subject or note without a hash is left as it is, never guessed at.
    q = cq.CYPHER_GRADE_GLOSS_ANCHORS
    assert "g.target_hash = t.anchor_hash" in q
    assert f"ELSE '{cs.GlossAnchorState.STALE.value}' END" in q
    # A note that followed its definition to a new name stays MOVED while the
    # hashes agree: `moved_from` is what keeps the move visible.
    assert (
        f"CASE WHEN g.moved_from IS NULL\n          THEN '{cs.GlossAnchorState.EXACT.value}' "
        f"ELSE '{cs.GlossAnchorState.MOVED.value}' END" in q
    )
    assert "g.target_hash IS NOT NULL AND t.anchor_hash IS NOT NULL" in q
    # A note that recorded a pre-format hash (stage two wrote the clone
    # skeleton) is not comparable and is left alone, not read as STALE.
    assert f"g.target_hash STARTS WITH '{cs.ANCHOR_HASH_VERSION}'" in q
    assert "SET g.anchor_state" in q
    assert "DELETE" not in q


def test_a_note_records_the_subjects_anchor_hash_not_the_clone_skeleton() -> None:
    graph = FakeGraph()
    row = _write(graph, RUN)
    assert row["target_hash"] == "fp-run"
    assert "n.anchor_hash AS target_hash" in cq.CYPHER_GLOSS_TARGET
    assert "ast_fingerprint" not in cq.CYPHER_GLOSS_TARGET


def test_reanchoring_failure_is_logged_not_raised(tmp_path: Path) -> None:
    from codebase_rag import logs as lg

    store = _RecordingStore()
    store.fail = True
    updater = GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=tmp_path,
        parsers={},
        queries={},
    )
    with patch("codebase_rag.graph_updater.logger") as log:
        updater._reanchor_glosses()
    log.warning.assert_called_once_with(
        lg.GLOSS_REANCHOR_FAILED.format(error="store down")
    )


def test_a_full_rebuild_ends_by_reanchoring_the_notes(tmp_path: Path) -> None:
    # The finding this pins: a forced rebuild whose inbound-edge capture failed
    # deleted and recreated the definitions with nothing to restore, leaving
    # every note unattached. The re-anchor pass runs at the end of the run.
    from codebase_rag.parser_loader import load_parsers

    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    parsers, queries = load_parsers()
    store = _RecordingStore()
    store.fetch_all = lambda query, params=None: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("graph down")
    )
    updater = GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=P,
    )
    with patch.object(GraphUpdater, "_reanchor_glosses", autospec=True) as reanchor:
        updater.run(force=True)
    reanchor.assert_called_once()


def test_an_unchanged_run_still_reanchors_the_notes(tmp_path: Path) -> None:
    # A run whose re-anchor failed has already published its cache, so the
    # next unchanged run takes the in-sync fast path; the re-anchor must run
    # there or "re-attached by the next run" is false for that run.
    store = _RecordingStore()
    updater = GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=tmp_path,
        parsers={},
        queries={},
    )
    with (
        patch.object(GraphUpdater, "_is_already_in_sync", return_value=True),
        patch.object(GraphUpdater, "_reanchor_glosses", autospec=True) as reanchor,
    ):
        updater.run()
    assert updater.skipped_because_in_sync
    reanchor.assert_called_once()


def test_a_gloss_whose_subject_did_not_survive_is_not_re_attached(
    tmp_path: Path,
) -> None:
    # Same guard every restored edge gets: a subject that the re-parse did not
    # recreate is left without the edge, which is what marks the note as
    # orphaned for the sweep rather than re-binding it to nothing.
    ingestor = MagicMock()
    updater = GraphUpdater(
        ingestor=ingestor, repo_path=tmp_path, parsers={}, queries={}
    )
    updater._restore_inbound_edges([_captured("Gloss", "gloss:abc", "ANNOTATES", RUN)])
    ingestor.ensure_relationship_batch.assert_not_called()


# --- the text-quote anchor at write time (stage five) ------------------------

APP_PY = (
    "import os\n\n\ndef run(v):\n    y = helper(v)\n    return y * 2\n\n\n" + "\n" * 12
)


def _reader(files: dict[str, str]) -> tuple[list[tuple[str, str]], Any]:
    from codebase_rag.gloss_anchor import ParsedSource, parse_source
    from codebase_rag.parser_loader import load_parsers

    parsers, _ = load_parsers()
    calls: list[tuple[str, str]] = []

    def read(project: str, path: str) -> ParsedSource | None:
        calls.append((project, path))
        text = files.get(path)
        if text is None:
            return None
        return ParsedSource(text, parse_source(parsers, Path(path), text))

    return calls, read


def test_a_note_records_its_subjects_text_quote_when_the_source_is_readable() -> None:
    from codebase_rag.gloss_anchor import text_anchor

    graph = FakeGraph()
    calls, read = _reader({"app.py": APP_PY})
    row = _write(graph, RUN, read_source=read)
    assert not _is_refusal(row)
    stored = graph.glosses[row["qualified_name"]]
    # Expected from a parse of its own, so the reader's call list stays
    # what the write alone produced.
    _other_calls, parse_only = _reader({"app.py": APP_PY})
    parsed = parse_only(P, "app.py")
    assert parsed is not None
    assert parsed.tree is not None
    expected = text_anchor(parsed, "run", 3, 8)
    assert expected is not None
    assert stored[cs.KEY_ANCHOR_QUOTE] == expected.quote
    assert stored[cs.KEY_ANCHOR_PREFIX] == expected.prefix
    assert stored[cs.KEY_ANCHOR_SUFFIX] == expected.suffix
    assert calls == [(P, "app.py")]


def test_a_note_without_a_reader_or_without_the_file_carries_no_quote() -> None:
    graph = FakeGraph()
    row = _write(graph, RUN)
    assert not _is_refusal(row)
    assert cs.KEY_ANCHOR_QUOTE not in graph.glosses[row["qualified_name"]]
    _calls, read = _reader({})
    row = _write(graph, STORE_GET, read_source=read)
    assert not _is_refusal(row)
    assert cs.KEY_ANCHOR_QUOTE not in graph.glosses[row["qualified_name"]]


def test_the_write_statement_records_the_three_anchor_fields() -> None:
    q = cq.CYPHER_GLOSS_WRITE
    for field in ("anchor_quote", "anchor_prefix", "anchor_suffix"):
        assert f"g.{field} = ${field}" in q
    for field in ("name", "path", "start_line", "end_line"):
        assert f"AS {field}" in cq.CYPHER_GLOSS_TARGET


def test_mcp_reads_the_subjects_source_only_from_its_own_checkout(
    tmp_path: Path,
) -> None:
    # The checkout is one level down and a file is planted ABOVE it, so the
    # escaping path exists on disk: a None for it comes from the root guard,
    # not from a missing file (local review P2).
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "app.py").write_text("SECRET\n")
    (repo / "app.py").write_text("x = 1\n")
    foreign = _registry(FakeGraph(), repo)
    assert foreign._source_reader_for(P) is None
    own = _registry(FakeGraph(root=str(repo)), repo)
    read = own._source_reader_for(P)
    assert read is not None
    parsed = read(P, "app.py")
    assert parsed is not None
    assert parsed.text == "x = 1\n"
    # The registry fixture loads no grammars, so the parse is absent here;
    # the updater test below covers a real parse.
    assert parsed.tree is None
    # Another project, a path that escapes the root, a missing file: None.
    assert read("other", "app.py") is None
    assert read(P, "../app.py") is None
    assert read(P, "missing.py") is None


@pytest.mark.anyio
async def test_mcp_annotate_hands_the_reader_to_the_write(tmp_path: Path) -> None:
    registry = _registry(FakeGraph(root=str(tmp_path)), tmp_path)
    with patch("codebase_rag.mcp.tools.gloss.write_gloss") as write:
        write.return_value = {"qualified_name": "k"}
        await registry.annotate(
            target=RUN, body=BODY, kind=cs.GlossKind.INVARIANT.value, project=P
        )
    assert callable(write.call_args.args[-1])


def test_the_updater_reads_only_its_own_projects_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "app.py").write_text("SECRET\n")
    (repo / "app.py").write_text("x = 1\n")
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=_RecordingStore(),  # type: ignore[arg-type]
        repo_path=repo,
        parsers=parsers,
        queries=queries,
    )
    (repo / "notes.txt").write_text("plain\n")
    parsed = updater._read_project_source(updater.project_name, "app.py")
    assert parsed is not None
    assert parsed.text == "x = 1\n"
    assert parsed.tree is not None
    plain = updater._read_project_source(updater.project_name, "notes.txt")
    assert plain is not None
    assert plain.tree is None
    assert updater._read_project_source("other", "app.py") is None
    # The planted file above the root exists: None here is the guard.
    assert updater._read_project_source(updater.project_name, "../app.py") is None
    assert updater._read_project_source(updater.project_name, "missing.py") is None


def test_the_reanchor_pass_hands_the_updater_reader_to_the_repair(
    tmp_path: Path,
) -> None:
    store = _RecordingStore()
    updater = GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=tmp_path,
        parsers={},
        queries={},
    )
    with patch("codebase_rag.graph_updater.repair_unanchored") as repair:
        updater._reanchor_glosses()
    repair.assert_called_once_with(
        store.fetch_all, store.execute_write, updater._read_project_source
    )


def test_a_class_note_records_the_class_hash() -> None:
    """Containers carry `anchor_hash` now (issue #1808), so a note on a class
    is graded and repaired like one on a function."""
    graph = FakeGraph()
    row = _write(graph, STORE)
    assert not _is_refusal(row)
    assert graph.glosses[row["qualified_name"]][cs.KEY_TARGET_HASH] == "fp-store"


class TestPropertyDescriptors:
    """A property's getter, setter and deleter share one qualified name; the
    registry names the later members `x@<line>`, which shifts with the
    file. `#getter`, `#setter` and `#deleter` name them stably (issue
    #1808)."""

    def test_each_descriptor_names_its_member(self) -> None:
        graph = FakeGraph()
        for descriptor, expected in (
            ("getter", f"{STORE}.x"),
            ("setter", f"{STORE}.x@16"),
            ("deleter", f"{STORE}.x@18"),
        ):
            found = gloss.resolve_one(graph.fetch_all, P, f"{STORE}.x#{descriptor}")
            assert not gloss._is_refusal(found), found
            assert found["qualified_name"] == expected  # type: ignore[index]
        # The dotted-suffix form resolves the base the same way.
        found = gloss.resolve_one(graph.fetch_all, P, "Store.x#setter")
        assert found["qualified_name"] == f"{STORE}.x@16"  # type: ignore[index]

    def test_an_unknown_descriptor_and_a_missing_member_are_refused(self) -> None:
        graph = FakeGraph()
        unknown = gloss.resolve_one(graph.fetch_all, P, f"{STORE}.x#owner")
        assert gloss._is_refusal(unknown)
        assert "owner" in unknown[cs.DICT_KEY_ERROR]  # type: ignore[index]
        plain = gloss.resolve_one(graph.fetch_all, P, f"{STORE_GET}#setter")
        assert gloss._is_refusal(plain)
        assert ".setter" in plain[cs.DICT_KEY_ERROR]  # type: ignore[index]

    def test_a_hash_opening_a_name_is_part_of_the_name(self) -> None:
        """A JS/TS private member keeps its `#` in the qualified name, so
        `Bank.#validatePin` is a name, not a descriptor target (bot
        review)."""
        private = f"{STORE}.#secret"
        assert gloss._split_descriptor(private) == (private, None)
        assert gloss._split_descriptor("#secret") == ("#secret", None)
        assert gloss._split_descriptor(f"{STORE}.x#Setter ") == (
            f"{STORE}.x",
            "setter",
        )
        graph = FakeGraph()
        graph.nodes[private] = _node("Method", private, "app.py", 30, 31)
        found = gloss.resolve_one(graph.fetch_all, P, private)
        assert not gloss._is_refusal(found), found
        assert found["qualified_name"] == private  # type: ignore[index]
        # The typo form still gets the descriptor refusal, not "not found".
        typo = gloss.resolve_one(graph.fetch_all, P, f"{STORE}.x#owner")
        assert "owner" in typo[cs.DICT_KEY_ERROR]  # type: ignore[index]

    def test_the_base_is_resolved_once(self) -> None:
        graph = FakeGraph()
        queries: list[str] = []

        def counting(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
            queries.append(query)
            return graph.fetch_all(query, params)

        found = gloss.resolve_one(counting, P, f"{STORE}.x#setter")
        assert found["qualified_name"] == f"{STORE}.x@16"  # type: ignore[index]
        assert queries.count(cq.CYPHER_GRAPH_RESOLVE_NAME) == 1

    def test_a_note_on_a_gone_member_is_reported_orphaned(self) -> None:
        """The note filed through `x#setter` sits on `x@16`; once the setter
        is gone the descriptor target no longer resolves, and the read
        must still surface the orphaned note (bot review)."""
        graph = FakeGraph()
        stored = gloss.write_gloss(
            graph.fetch_all,
            graph.execute_write,
            P,
            f"{STORE}.x#setter",
            "validates before storing",
            "invariant",
        )
        assert not gloss._is_refusal(stored), stored
        gone = f"{STORE}.x@16"
        del graph.nodes[gone]
        graph.annotates = {(g, qn) for g, qn in graph.annotates if qn != gone}
        result = gloss.glosses_for(graph.fetch_all, P, f"{STORE}.x#setter")
        assert gloss._is_refusal(result)
        assert ".setter" in result[cs.DICT_KEY_ERROR]  # type: ignore[index]
        orphaned = result[cs.KEY_ORPHANED]  # type: ignore[typeddict-item]
        assert [g["target_qn"] for g in orphaned] == [gone]
        # The deleter's note is still attached, so it is not orphaned.
        graph.annotates.add(("gloss:kept", f"{STORE}.x@18"))
        graph.glosses["gloss:kept"] = {
            cs.KEY_TARGET_QN: f"{STORE}.x@18",
            cs.KEY_BODY: "b",
        }
        again = gloss.glosses_for(graph.fetch_all, P, f"{STORE}.x#setter")
        assert [g["target_qn"] for g in again[cs.KEY_ORPHANED]] == [gone]  # type: ignore[typeddict-item]

    def test_a_note_written_on_the_setter_is_filed_on_it(self) -> None:
        graph = FakeGraph()
        stored = gloss.write_gloss(
            graph.fetch_all,
            graph.execute_write,
            P,
            f"{STORE}.x#setter",
            "validates before storing",
            "invariant",
        )
        assert not gloss._is_refusal(stored), stored
        assert stored["target_qn"] == f"{STORE}.x@16"  # type: ignore[index]
