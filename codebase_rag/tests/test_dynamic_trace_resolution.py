# Runtime frames carry co_qualname artifacts (<locals>, <module>, <lambda>)
# and the graph carries duplicate-definition markers (qn@line); the resolver
# must bridge both notations and report why a frame cannot be mapped
# (issue #1246).

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.trace.records import FramePoint
from codebase_rag.trace.resolution import (
    CallableNode,
    FrameResolver,
    ResolutionStats,
)

_PROJECT = "proj__deadbeef"


def _node(
    label: str,
    qualified_name: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> CallableNode:
    return CallableNode(
        label=label,
        qualified_name=qualified_name,
        path=path,
        start_line=start_line,
        end_line=end_line,
    )


def _resolver(repo: Path) -> FrameResolver:
    nodes = [
        _node(cs.NodeLabel.MODULE, f"{_PROJECT}.pkg.mod", "pkg/mod.py"),
        _node(
            cs.NodeLabel.FUNCTION,
            f"{_PROJECT}.pkg.mod.outer",
            "pkg/mod.py",
            1,
            10,
        ),
        _node(
            cs.NodeLabel.FUNCTION,
            f"{_PROJECT}.pkg.mod.outer.inner",
            "pkg/mod.py",
            2,
            4,
        ),
        _node(
            cs.NodeLabel.METHOD,
            f"{_PROJECT}.pkg.mod.Klass.method",
            "pkg/mod.py",
            12,
            20,
        ),
        _node(
            cs.NodeLabel.FUNCTION,
            f"{_PROJECT}.pkg.dup.helper@5",
            "pkg/dup.py",
            5,
            7,
        ),
        _node(
            cs.NodeLabel.FUNCTION,
            f"{_PROJECT}.pkg.dup.helper@12",
            "pkg/dup.py",
            12,
            14,
        ),
    ]
    return FrameResolver(repo, nodes)


def _frame(repo: Path, rel: str, qualname: str, line: int) -> FramePoint:
    return FramePoint(path=str(repo / rel), qualname=qualname, line=line)


def test_resolves_nested_function_through_locals(tmp_path):
    resolver = _resolver(tmp_path)
    stats = ResolutionStats()
    resolved = resolver.resolve(
        _frame(tmp_path, "pkg/mod.py", "outer.<locals>.inner", 2), stats
    )
    assert resolved is not None
    assert resolved.qualified_name == f"{_PROJECT}.pkg.mod.outer.inner"
    assert stats.total == 0


def test_resolves_method_and_module_level_code(tmp_path):
    resolver = _resolver(tmp_path)
    stats = ResolutionStats()
    method = resolver.resolve(_frame(tmp_path, "pkg/mod.py", "Klass.method", 12), stats)
    module = resolver.resolve(_frame(tmp_path, "pkg/mod.py", "<module>", 1), stats)
    assert method is not None
    assert method.label == cs.NodeLabel.METHOD
    assert module is not None
    assert module.label == cs.NodeLabel.MODULE
    assert module.qualified_name == f"{_PROJECT}.pkg.mod"


def test_duplicate_variants_disambiguate_by_line(tmp_path):
    resolver = _resolver(tmp_path)
    stats = ResolutionStats()
    first = resolver.resolve(_frame(tmp_path, "pkg/dup.py", "helper", 5), stats)
    second = resolver.resolve(_frame(tmp_path, "pkg/dup.py", "helper", 12), stats)
    assert first is not None
    assert first.qualified_name.endswith("helper@5")
    assert second is not None
    assert second.qualified_name.endswith("helper@12")


def test_unresolved_reasons_are_categorised(tmp_path):
    resolver = _resolver(tmp_path)
    stats = ResolutionStats()

    outside = resolver.resolve(
        FramePoint(path="/somewhere/else/x.py", qualname="f", line=1), stats
    )
    synthetic = resolver.resolve(
        _frame(tmp_path, "pkg/mod.py", "outer.<locals>.<lambda>", 3), stats
    )
    unknown = resolver.resolve(_frame(tmp_path, "pkg/nope.py", "f", 1), stats)
    missing = resolver.resolve(_frame(tmp_path, "pkg/mod.py", "ghost", 99), stats)

    assert outside is None
    assert synthetic is None
    assert unknown is None
    assert missing is None
    assert stats.unresolved == {
        cs.TraceUnresolvedReason.OUTSIDE_REPO.value: 1,
        cs.TraceUnresolvedReason.SYNTHETIC.value: 1,
        cs.TraceUnresolvedReason.UNKNOWN_PATH.value: 1,
        cs.TraceUnresolvedReason.NO_MATCH.value: 1,
    }


def test_wrapper_falls_back_to_line_containment(tmp_path):
    # A decorator's wrapper can run under a name the static pass recorded
    # differently; when the name lookup fails, the innermost span containing
    # the runtime line must win.
    resolver = _resolver(tmp_path)
    stats = ResolutionStats()
    resolved = resolver.resolve(
        _frame(tmp_path, "pkg/mod.py", "renamed_at_runtime", 3), stats
    )
    assert resolved is not None
    assert resolved.qualified_name == f"{_PROJECT}.pkg.mod.outer.inner"
