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
    _node("Function", RUN, "app.py", 3, 8, ast_fingerprint="fp-run"),
    _node("Class", STORE, "app.py", 10, 18),
    _node("Method", STORE_GET, "app.py", 11, 13, ast_fingerprint="fp-store-get"),
    _node("Module", f"{P}.util", "util.py", 1, 10),
    _node("Function", UTIL_GET, "util.py", 1, 4),
    _node("Function", VALIDATE, "util.py", 6, 9, ast_fingerprint="fp-validate"),
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
        # When set, every MENTIONS statement fails at the store.
        self.fail_mentions = False

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
        elif query == cq.CYPHER_GLOSS_TARGET:
            n = self.nodes.get(str(p[cs.KEY_QN]))
            if n is not None:
                out.append({cs.KEY_TARGET_HASH: n.get(cs.KEY_AST_FINGERPRINT)})
        elif query == cq.CYPHER_GLOSS_READ:
            key = str(p[cs.KEY_QN])
            if key in self.glosses:
                out.append(self._gloss_row(key))
        elif query == cq.CYPHER_GLOSSES_ANNOTATING:
            out = [self._gloss_row(g) for g, qn in self.annotates if qn == p[cs.KEY_QN]]
        elif query == cq.CYPHER_GLOSSES_MENTIONING:
            out = [self._gloss_row(g) for g, qn in self.mentions if qn == p[cs.KEY_QN]]
        else:
            raise AssertionError(f"unexpected query: {query[:60]}")
        # Deliberately unsorted: the tools must sort.
        return list(reversed(out))

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        p = params or {}
        self.writes.append((query, p))
        key = str(p[cs.KEY_QN])
        target = str(p.get(cs.KEY_TARGET_QN, ""))
        subject_present = target in self.nodes and target.startswith(
            str(p.get(cs.KEY_PROJECT_PREFIX, ""))
        )
        if query == cq.CYPHER_GLOSS_WRITE:
            if self.vanish_on_write or not subject_present:
                return
            # Every property is SET by name and a null unsets it, so a
            # rewrite REPLACES the stored properties rather than merging.
            self.glosses[key] = {
                k: v
                for k, v in p.items()
                if k not in (cs.KEY_QN, cs.KEY_PROJECT_PREFIX) and v is not None
            }
            self.annotates.add((key, target))
        elif query == cq.CYPHER_GLOSS_MENTION:
            if self.fail_mentions:
                raise RuntimeError("store down")
            if key in self.glosses and subject_present:
                self.mentions.add((key, target))
        elif query == cq.CYPHER_GLOSS_DELETE:
            self.glosses.pop(key, None)
            self.annotates = {(g, qn) for g, qn in self.annotates if g != key}
            self.mentions = {(g, qn) for g, qn in self.mentions if g != key}
        else:
            raise AssertionError(f"unexpected write: {query[:60]}")


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
    # No MENTIONS edge was attempted on a node that was never created.
    assert [q for q, _ in graph.writes] == [cq.CYPHER_GLOSS_WRITE]


def test_author_and_commit_are_recorded_when_given() -> None:
    graph = FakeGraph()
    row = _write(graph, RUN, author=" cgr-1 ", commit_sha="abc123")
    assert row["created_by"] == "cgr-1"
    assert row["commit_sha"] == "abc123"
    bare = _write(graph, VALIDATE)
    assert bare["commit_sha"] is None
    # Rewriting the same note without a commit unsets the one it had.
    again = _write(graph, RUN, author="cgr-1")
    assert again["qualified_name"] == row["qualified_name"]
    assert again["commit_sha"] is None


def test_a_failed_mention_write_removes_the_node_this_call_created() -> None:
    graph = FakeGraph()
    graph.fail_mentions = True
    with pytest.raises(RuntimeError, match="store down"):
        _write(graph, RUN, mentions=VALIDATE)
    assert graph.glosses == {}
    assert graph.annotates == set()


def test_a_failed_mention_write_leaves_a_pre_existing_note_alone() -> None:
    graph = FakeGraph()
    first = _write(graph, RUN)
    graph.fail_mentions = True
    with pytest.raises(RuntimeError, match="store down"):
        _write(graph, RUN, mentions=VALIDATE)
    assert set(graph.glosses) == {first["qualified_name"]}


def test_a_target_without_a_fingerprint_stores_no_hash() -> None:
    graph = FakeGraph()
    row = _write(graph, STORE)
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


def test_a_capture_outage_aborts_even_a_full_build(tmp_path: Path) -> None:
    # A full build used to continue on the grounds that every caller is
    # re-parsed; gloss edges have no source to re-derive from, so continuing
    # would orphan every note in the project.
    # A real class, not a MagicMock: the capture is gated on the runtime
    # QueryProtocol check, which a bare mock does not satisfy.
    class _RaisingStore:
        def fetch_all(self, query: str, params: PropertyDict | None = None) -> list:
            raise RuntimeError("store down")

        def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
            return None

    updater = GraphUpdater(
        ingestor=_RaisingStore(),  # type: ignore[arg-type]
        repo_path=tmp_path,
        parsers={},
        queries={},
    )
    updater._is_full_build = True
    with pytest.raises(RuntimeError, match="store down"):
        updater._capture_inbound_edges(["app.py"])


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
