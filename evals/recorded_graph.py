"""A recording ingestor and the graph replayed from what it recorded.

`RecordingIngestor` stands in for the graph sink while an index runs and
keeps every write as a mock call. `RecordedGraph` answers the fixed queries
graph edits issue (rename, its contract checks) from those calls, so an
edit can be exercised against a real index without a database.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.types_defs import PropertyDict, ResultRow


class RecordingIngestor:
    _TRACKED = (
        "fetch_all",
        "execute_write",
        "ensure_node_batch",
        "ensure_relationship_batch",
        "flush_all",
    )

    def __init__(self) -> None:
        self.fetch_all = MagicMock()
        self.execute_write = MagicMock()
        self.ensure_node_batch = MagicMock()
        self.ensure_relationship_batch = MagicMock()
        self.flush_all = MagicMock()
        self._fallback = MagicMock()

    def reset_mock(self) -> None:
        for name in (*self._TRACKED, "_fallback"):
            getattr(self, name).reset_mock()

    @property
    def method_calls(self) -> list:
        result = []
        for name in self._TRACKED:
            mock_attr = self.__dict__[name]
            for c in mock_attr.call_args_list:
                result.append(getattr(call, name)(*c.args, **c.kwargs))
        result.extend(self._fallback.method_calls)
        return result

    def __getattr__(self, name: str) -> MagicMock:
        return getattr(self._fallback, name)


class RecordedGraph:
    """The fixed queries answered from what the indexer emitted."""

    def __init__(self, mock: RecordingIngestor | MagicMock, project: str) -> None:
        self.nodes: dict[str, dict] = {}
        # Every label written under a qn: a TS type and a same-named value
        # are two nodes sharing it (#2520), merged in `nodes`.
        self.labels: dict[str, set[str]] = {}
        self.edges: list[tuple[str, str, str, dict]] = []
        for c in mock.ensure_node_batch.call_args_list:
            props = dict(c.args[1])
            props[cs.KEY_LABEL] = str(c.args[0])
            node_qn = str(props.get(cs.KEY_QUALIFIED_NAME, ""))
            self.labels.setdefault(node_qn, set()).add(props[cs.KEY_LABEL])
            self.nodes.setdefault(node_qn, {}).update(props)
        for c in mock.ensure_relationship_batch.call_args_list:
            props = (
                c.kwargs.get("properties")
                or (c.args[3] if len(c.args) > 3 else {})
                or {}
            )
            self.edges.append(
                (str(c.args[0][2]), str(c.args[1]), str(c.args[2][2]), dict(props))
            )
        self.project = project
        self.root_path: str | None = None

    def _node_row(self, qn: str) -> ResultRow:
        n = self.nodes[qn]
        return {
            cs.KEY_LABEL: n[cs.KEY_LABEL],
            cs.KEY_QUALIFIED_NAME: qn,
            cs.KEY_NAME: n.get(cs.KEY_NAME),
            cs.KEY_PATH: n.get(cs.KEY_PATH),
            cs.KEY_START_LINE: n.get(cs.KEY_START_LINE),
            cs.KEY_END_LINE: n.get(cs.KEY_END_LINE),
            cs.KEY_DOCSTRING: n.get(cs.KEY_DOCSTRING),
            cs.KEY_NAME_START_LINE: n.get(cs.KEY_NAME_START_LINE),
            cs.KEY_NAME_START_COL: n.get(cs.KEY_NAME_START_COL),
            cs.KEY_DECORATED_START_LINE: n.get(cs.KEY_DECORATED_START_LINE),
            cs.KEY_DECORATORS: n.get(cs.KEY_DECORATORS),
        }

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        p = params or {}
        qn = str(p.get(cs.KEY_QN, ""))
        if query == cq.CYPHER_PROJECT_ROOT_PATH:
            return [{cs.KEY_ROOT_PATH: self.root_path}] if self.root_path else []
        if query == cq.CYPHER_PROJECT_IS_INCOMPLETE:
            # No marker: this fixture's graph was indexed by a run that
            # finished. An unanswered query would raise, and
            # `_persisted_incomplete` reads a raise as "cannot tell, refuse",
            # so leaving it out makes every reingest here refuse.
            return []
        if query == cq.CYPHER_GRAPH_DEFINITION:
            return [self._node_row(qn)] if qn in self.nodes else []
        if query == cq.CYPHER_GRAPH_RESOLVE_NAME:
            # The rename's cross-check asks for the symbol's namesakes, whose
            # sites are theirs and not unplanned occurrences (issue #2564).
            prefix = str(p.get(cs.KEY_PROJECT_PREFIX, ""))
            suffix = str(p.get(cs.KEY_SUFFIX, ""))
            labels = {label.value for label in cs.DEFINITION_NODE_LABELS}
            return [
                self._node_row(name)
                for name, node in self.nodes.items()
                if name.startswith(prefix)
                and node[cs.KEY_LABEL] in labels
                and (
                    name == qn
                    or name.endswith(suffix)
                    or node.get(cs.KEY_NAME) == p.get(cs.KEY_NAME)
                )
            ]
        if query in (
            cq.CYPHER_GRAPH_CALLERS,
            cq.CYPHER_GRAPH_REFERENCES,
            cq.CYPHER_GRAPH_TYPE_EDGES,
        ):
            rels = {
                cq.CYPHER_GRAPH_CALLERS: {"CALLS"},
                cq.CYPHER_GRAPH_REFERENCES: {"REFERENCES", "INSTANTIATES"},
                cq.CYPHER_GRAPH_TYPE_EDGES: {"INHERITS", "ACCEPTS", "RETURNS"},
            }[query]
            # `$label IN labels(target)`: the twin-aware reads scope to one.
            label = p.get(cs.KEY_LABEL)
            out: list[ResultRow] = []
            for src, rel, dst, props in self.edges:
                if dst != qn or rel not in rels or src not in self.nodes:
                    continue
                if label is not None and label not in self.labels.get(dst, set()):
                    continue
                out.append(
                    {
                        cs.KEY_LABEL: self.nodes[src][cs.KEY_LABEL],
                        cs.KEY_QUALIFIED_NAME: src,
                        cs.KEY_PATH: self.nodes[src].get(cs.KEY_PATH),
                        cs.KEY_REL_TYPE: rel,
                        **{
                            k: props.get(k)
                            for k in (
                                cs.KEY_LINE,
                                cs.KEY_COL,
                                cs.KEY_END_LINE,
                                cs.KEY_END_COL,
                                cs.KEY_ARG_COUNT,
                                cs.KEY_KWARG_NAMES,
                                cs.KEY_RESOLUTION,
                            )
                        },
                    }
                )
            return out
        if query == cq.CYPHER_GRAPH_IMPLEMENTORS:
            return [
                {
                    cs.KEY_LABEL: self.nodes[src][cs.KEY_LABEL],
                    cs.KEY_QUALIFIED_NAME: src,
                    cs.KEY_PATH: self.nodes[src].get(cs.KEY_PATH),
                    cs.KEY_REL_TYPE: rel,
                }
                for src, rel, dst, _props in self.edges
                if dst == qn and rel in ("INHERITS", "IMPLEMENTS") and src in self.nodes
            ]
        if query == cq.CYPHER_GRAPH_OVERRIDES:
            out = []
            for src, rel, dst, props in self.edges:
                if rel != "OVERRIDES":
                    continue
                for a, b in ((src, dst), (dst, src)):
                    if b == qn and a in self.nodes:
                        out.append(
                            {
                                cs.KEY_LABEL: self.nodes[a][cs.KEY_LABEL],
                                cs.KEY_QUALIFIED_NAME: a,
                                cs.KEY_PATH: self.nodes[a].get(cs.KEY_PATH),
                                cs.KEY_REL_TYPE: rel,
                                cs.KEY_RESOLUTION: props.get(cs.KEY_RESOLUTION),
                            }
                        )
            return out
        if query == cq.CYPHER_GRAPH_IMPORTERS:
            out = []
            for src, rel, dst, props in self.edges:
                if rel != "IMPORTS" or dst != qn or src not in self.nodes:
                    continue
                out.append(
                    {
                        cs.KEY_QUALIFIED_NAME: src,
                        cs.KEY_PATH: self.nodes[src].get(cs.KEY_PATH),
                        **{
                            k: props.get(k)
                            for k in (
                                cs.KEY_LINE,
                                cs.KEY_COL,
                                cs.KEY_END_LINE,
                                cs.KEY_END_COL,
                                cs.KEY_ALIAS,
                                cs.KEY_IMPORTED_NAME,
                            )
                        },
                    }
                )
            return out
        if query == cq.CYPHER_GRAPH_GO_IMPLEMENTERS_UNKNOWN:
            prefix = str(p.get(cs.KEY_PROJECT_PREFIX, ""))
            marked = sorted(
                {
                    src
                    for src, rel, dst, _props in self.edges
                    if rel == "DEFINES_METHOD"
                    and self.nodes.get(dst, {}).get(cs.KEY_NAME) == p.get(cs.KEY_NAME)
                    and src.startswith(prefix)
                    and "Interface" in self.labels.get(src, set())
                    and self.nodes[src].get(cs.KEY_IMPLEMENTERS_UNKNOWN)
                }
            )
            return [
                {
                    cs.KEY_QUALIFIED_NAME: src,
                    cs.KEY_PATH: self.nodes[src].get(cs.KEY_PATH),
                    cs.KEY_IMPLEMENTERS_UNKNOWN: self.nodes[src].get(
                        cs.KEY_IMPLEMENTERS_UNKNOWN
                    ),
                }
                for src in marked
            ]
        if query == cq.CYPHER_GRAPH_CALLBACK_SITES:
            return [
                {
                    cs.KEY_QUALIFIED_NAME: src,
                    cs.KEY_LINE: props.get(cs.KEY_LINE),
                    cs.KEY_COL: props.get(cs.KEY_COL),
                    cs.KEY_VIA_PARAM: props[cs.KEY_VIA_PARAM],
                }
                for src, rel, dst, props in self.edges
                if rel == "CALLS" and dst == qn and props.get(cs.KEY_VIA_PARAM)
            ]
        raise AssertionError(f"unexpected query: {query[:50]}")
