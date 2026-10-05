"""A Python frame whose name the file does not define is unresolved.

The resolver fell back to the innermost span containing the frame's line
when no node matched the frame's name. A trace recorded before a function
was deleted then bound the old frame to whatever function now sat on its
line: `driver -> alpha` became a `dynamic` edge `driver -> beta`, counted
as a static miss with `unresolved: 0` (issue #2843). The Python tracer
records `co_qualname`, which every definition the index holds answers to
by name, so a name the file lacks says the trace and the code disagree.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.trace.records import FramePoint
from codebase_rag.trace.resolution import (
    CallableNode,
    FrameResolver,
    ResolutionStats,
)

_PROJECT = "tracedemo"


def _resolver(repo: Path) -> FrameResolver:
    # Commit B of the issue: alpha is gone and beta slid onto lines 1-2.
    def node(name: str, start: int, end: int) -> CallableNode:
        return CallableNode(
            label=cs.NodeLabel.FUNCTION,
            qualified_name=f"{_PROJECT}.m.{name}",
            path="m.py",
            start_line=start,
            end_line=end,
        )

    module = CallableNode(
        label=cs.NodeLabel.MODULE,
        qualified_name=f"{_PROJECT}.m",
        path="m.py",
        start_line=None,
        end_line=None,
    )
    return FrameResolver(repo, [module, node("beta", 1, 2), node("driver", 4, 5)])


def _frame(repo: Path, qualname: str, line: int) -> FramePoint:
    return FramePoint(path=str(repo / "m.py"), qualname=qualname, line=line)


def test_a_deleted_function_does_not_rebind_to_its_lines(tmp_path: Path) -> None:
    stats = ResolutionStats()
    assert _resolver(tmp_path).resolve(_frame(tmp_path, "alpha", 1), stats) is None
    assert stats.unresolved == {cs.TraceUnresolvedReason.NO_MATCH.value: 1}


def test_a_frame_inside_another_functions_body_is_not_that_function(
    tmp_path: Path,
) -> None:
    stats = ResolutionStats()
    frame = _frame(tmp_path, "outer.<locals>.gone", 5)
    assert _resolver(tmp_path).resolve(frame, stats) is None
    assert stats.total == 1


def test_a_current_frame_still_resolves(tmp_path: Path) -> None:
    # Negatives: names the file defines resolve as before, and synthetic
    # bodies are still reported as such.
    stats = ResolutionStats()
    resolver = _resolver(tmp_path)
    beta = resolver.resolve(_frame(tmp_path, "beta", 1), stats)
    driver = resolver.resolve(_frame(tmp_path, "driver", 4), stats)
    lam = resolver.resolve(_frame(tmp_path, "driver.<locals>.<lambda>", 5), stats)
    assert beta is not None and beta.qualified_name == f"{_PROJECT}.m.beta"
    assert driver is not None and driver.qualified_name == f"{_PROJECT}.m.driver"
    assert lam is None
    assert stats.unresolved == {cs.TraceUnresolvedReason.SYNTHETIC.value: 1}
