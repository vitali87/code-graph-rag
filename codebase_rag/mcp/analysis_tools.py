"""The MCP tools that analyse a run of the code rather than walk one
symbol: the flow-reachability verdict between a source and a sink, and the
traceback explanation and root-cause ranking (issue #227).
"""

from pathlib import Path
from typing import TYPE_CHECKING

from codebase_rag import constants as cs

from . import constants as mcs
from .handler_base import ToolHandlerBase, run_in_thread

if TYPE_CHECKING:
    from codebase_rag.crash_correlation import (
        FrameResolutionRate,
        RootCauseReport,
        TracebackReport,
    )


def _resolution_section(resolution: "FrameResolutionRate") -> dict:
    # `rate` is a property, so `_asdict()` omits it; mapped explicitly
    # because the ratio is the number the caller acts on (issue #227).
    return {
        "total": resolution.total,
        "resolved": resolution.resolved,
        "rate": resolution.rate,
    }


def _explained_section(report: "TracebackReport") -> dict:
    """One exception of an explained traceback: the one that propagated, or
    a member of an ExceptionGroup, with members of its own (issue #3235)."""
    return {
        "exception_type": report.exception_type,
        "exception_message": report.exception_message,
        "frames": [frame._asdict() for frame in report.frames],
        "resolution": _resolution_section(report.resolution),
        "note": report.note,
        "members": [_explained_section(member) for member in report.members],
        "omitted_members": report.omitted_members,
    }


def _ranked_section(report: "RootCauseReport") -> dict:
    """One exception's ranking, as `_explained_section` lays it out."""
    return {
        "exception_type": report.exception_type,
        "exception_message": report.exception_message,
        "failing": report.failing,
        "anchor_is_crash_site": report.anchor_is_crash_site,
        "candidates": [candidate._asdict() for candidate in report.candidates],
        "resolution": _resolution_section(report.resolution),
        "note": report.note,
        "members": [_ranked_section(member) for member in report.members],
        "omitted_members": report.omitted_members,
    }


class AnalysisTools(ToolHandlerBase):
    """Flow verdicts and traceback analysis over this server's project."""

    async def flow_verdict(
        self, source_qualified_name: str, sink_qualified_name: str
    ) -> dict:
        from codebase_rag.flow_verdict import flow_reachability_verdict

        project, workspace_refusal = self._fixed_root_project()
        if project is None:
            return {cs.DICT_KEY_ERROR: workspace_refusal}
        # The edge scan and coverage read must see one consistent graph:
        # index/update handlers hold this lock while they delete and
        # rebuild, and an interleaved read would mix generations. The guard
        # is inside the lock for the same reason: a rebuild landing between
        # the check and the read leaves an approved read running against the
        # partial graph (PR #1547).
        async with self._ingestor_lock:
            if refusal := await run_in_thread(self._unindexed_refusal, project):
                return {cs.DICT_KEY_ERROR: refusal}
            if refusal := await run_in_thread(
                self.marker.partial_graph_refusal, project, cs.MCPToolName.FLOW_VERDICT
            ):
                return {cs.DICT_KEY_ERROR: refusal}
            if refusal := await run_in_thread(
                self._flow_endpoint_refusal,
                project,
                source_qualified_name,
                sink_qualified_name,
            ):
                return {cs.DICT_KEY_ERROR: refusal}
            result = await run_in_thread(
                flow_reachability_verdict,
                self.ingestor.fetch_all,
                project,
                source_qualified_name,
                sink_qualified_name,
            )
        return {
            "verdict": result.verdict,
            "path": list(result.path),
            "gaps": list(result.gaps),
            # Where the path crosses a service boundary (issue #1603).
            "remote_hops": [list(hop) for hop in result.remote_hops],
        }

    async def explain_traceback(
        self, traceback_text: str, path_prefix_map: dict[str, str] | None = None
    ) -> dict:
        from codebase_rag.crash_correlation import explain_traceback

        project, workspace_refusal = self._fixed_root_project()
        if project is None:
            return {cs.DICT_KEY_ERROR: workspace_refusal}
        async with self._ingestor_lock:
            if refusal := await run_in_thread(self._unindexed_refusal, project):
                return {cs.DICT_KEY_ERROR: refusal}
            if refusal := await run_in_thread(
                self.marker.partial_graph_refusal,
                project,
                cs.MCPToolName.EXPLAIN_TRACEBACK,
            ):
                return {cs.DICT_KEY_ERROR: refusal}
            report = await run_in_thread(
                explain_traceback,
                self.ingestor.fetch_all,
                project,
                Path(self.project_root),
                traceback_text,
                path_prefix_map,
            )
        return {
            **_explained_section(report),
            "flow_gaps": list(report.flow_gaps),
            "inferred_checkout_root": report.inferred_root,
        }

    async def rank_root_causes(
        self, traceback_text: str, path_prefix_map: dict[str, str] | None = None
    ) -> dict:
        from codebase_rag.crash_correlation import rank_root_causes

        project, workspace_refusal = self._fixed_root_project()
        if project is None:
            return {cs.DICT_KEY_ERROR: workspace_refusal}
        async with self._ingestor_lock:
            if refusal := await run_in_thread(self._unindexed_refusal, project):
                return {cs.DICT_KEY_ERROR: refusal}
            if refusal := await run_in_thread(
                self.marker.partial_graph_refusal,
                project,
                cs.MCPToolName.RANK_ROOT_CAUSES,
            ):
                return {cs.DICT_KEY_ERROR: refusal}
            report = await run_in_thread(
                rank_root_causes,
                self.ingestor.fetch_all,
                project,
                Path(self.project_root),
                traceback_text,
                path_prefix_map,
            )
        return {
            **_ranked_section(report),
            "flow_used": report.flow_used,
            "flow_gaps": list(report.flow_gaps),
            "inferred_checkout_root": report.inferred_root,
        }

    def _flow_endpoint_refusal(
        self, project_name: str, source: str, sink: str
    ) -> str | None:
        """Why `flow_verdict` cannot answer for these names (issue #2939).

        A name that is no node has no flow edges, so the walk found no path
        and, on a fully covered project, answered NO_FLOW: the verified
        absence a taint question reads as safe, for a typo. The source must
        also be this project's own, or a resource no project owns: only this
        project's flow edges are walked from it. A sink may sit across a
        service boundary, in another project.
        """
        problems = [
            mcs.MCP_FLOW_ENDPOINT.format(side=side, problem=problem)
            for side, qualified_name in (
                (mcs.MCP_FLOW_SOURCE, source),
                (mcs.MCP_FLOW_SINK, sink),
            )
            if (problem := self._unknown_target_error(project_name, qualified_name))
        ]
        if not problems and (owner := self._other_owner(project_name, source)):
            problems.append(
                mcs.MCP_FLOW_SOURCE_ELSEWHERE.format(
                    qualified_name=source, owner=owner, project=project_name
                )
            )
        return " ".join(problems) or None

    def _other_owner(self, project_name: str, qualified_name: str) -> str | None:
        # The longest registered project the name sits under, when it is not
        # this one: project names may contain dots, so a name under this
        # project's prefix can still belong to `<project>.v2`.
        owner = self._name_owner(
            qualified_name, [*self.ingestor.list_projects(), project_name]
        )
        return owner if owner != project_name else None
