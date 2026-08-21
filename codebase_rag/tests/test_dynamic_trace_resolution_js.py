# V8 profile frames carry bare function names (never dotted paths), file
# URLs, and function-declaration lines; the JS resolver must join them onto
# the graph's dotted qualified names by path plus line span, with the name
# as a tiebreak (issue #1247).

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.trace.records import FramePoint
from codebase_rag.trace.resolution import (
    CallableNode,
    JsFrameResolver,
    ResolutionStats,
)

_P = "demo"


def _node(label, qualified_name, path, start_line, end_line):
    return CallableNode(
        label=label,
        qualified_name=qualified_name,
        path=path,
        start_line=start_line,
        end_line=end_line,
    )


def _nodes():
    return [
        _node(cs.NodeLabel.MODULE, f"{_P}.src.utils", "src/utils.js", None, None),
        _node(cs.NodeLabel.FUNCTION, f"{_P}.src.utils.greet", "src/utils.js", 3, 5),
        _node(
            cs.NodeLabel.METHOD, f"{_P}.src.utils.Person.greet", "src/utils.js", 10, 12
        ),
        _node(
            cs.NodeLabel.FUNCTION,
            f"{_P}.src.utils.outer.inner",
            "src/utils.js",
            18,
            20,
        ),
        _node(cs.NodeLabel.FUNCTION, f"{_P}.src.utils.outer", "src/utils.js", 16, 24),
        # A truly anonymous callback, named from its 0-based start point.
        _node(
            cs.NodeLabel.FUNCTION,
            f"{_P}.src.utils.outer.anonymous_21_8",
            "src/utils.js",
            22,
            23,
        ),
        # An overload implementation carrying the duplicate marker.
        _node(
            cs.NodeLabel.FUNCTION,
            f"{_P}.src.store.useStore@27",
            "src/store.ts",
            27,
            30,
        ),
    ]


def _resolver(tmp_path):
    return JsFrameResolver(tmp_path, _nodes())


def _frame(tmp_path, rel_path, qualname, line):
    return FramePoint(path=str(tmp_path / rel_path), qualname=qualname, line=line)


def test_resolves_top_level_function_and_method_by_name_and_span(tmp_path):
    resolver = _resolver(tmp_path)
    stats = ResolutionStats()

    top = resolver.resolve(_frame(tmp_path, "src/utils.js", "greet", 3), stats)
    method = resolver.resolve(_frame(tmp_path, "src/utils.js", "greet", 10), stats)

    assert top is not None
    assert top.qualified_name == f"{_P}.src.utils.greet"
    assert method is not None
    assert method.qualified_name == f"{_P}.src.utils.Person.greet"
    assert stats.total == 0


def test_resolves_module_toplevel_frame_to_module_node(tmp_path):
    resolved = _resolver(tmp_path).resolve(
        _frame(tmp_path, "src/utils.js", cs.TRACE_QUALNAME_MODULE, 1),
        ResolutionStats(),
    )

    assert resolved is not None
    assert resolved.label == cs.NodeLabel.MODULE
    assert resolved.qualified_name == f"{_P}.src.utils"


def test_resolves_nested_function_to_innermost_span(tmp_path):
    resolved = _resolver(tmp_path).resolve(
        _frame(tmp_path, "src/utils.js", "inner", 18), ResolutionStats()
    )

    assert resolved is not None
    assert resolved.qualified_name == f"{_P}.src.utils.outer.inner"


def test_resolves_anonymous_frame_by_span(tmp_path):
    resolved = _resolver(tmp_path).resolve(
        _frame(tmp_path, "src/utils.js", cs.TRACE_QUALNAME_ANONYMOUS, 22),
        ResolutionStats(),
    )

    assert resolved is not None
    assert resolved.qualified_name == f"{_P}.src.utils.outer.anonymous_21_8"


def test_resolves_duplicate_marker_variant_by_natural_name(tmp_path):
    resolved = _resolver(tmp_path).resolve(
        _frame(tmp_path, "src/store.ts", "useStore", 28), ResolutionStats()
    )

    assert resolved is not None
    assert resolved.qualified_name == f"{_P}.src.store.useStore@27"


def test_unresolved_reasons_are_categorised(tmp_path):
    resolver = _resolver(tmp_path)
    stats = ResolutionStats()

    outside = resolver.resolve(
        FramePoint(path="/elsewhere/x.js", qualname="f", line=1), stats
    )
    unknown = resolver.resolve(_frame(tmp_path, "src/gone.js", "f", 1), stats)
    missing = resolver.resolve(_frame(tmp_path, "src/utils.js", "ghost", 99), stats)

    assert outside is None
    assert unknown is None
    assert missing is None
    assert stats.unresolved == {
        cs.TraceUnresolvedReason.OUTSIDE_REPO.value: 1,
        cs.TraceUnresolvedReason.UNKNOWN_PATH.value: 1,
        cs.TraceUnresolvedReason.NO_MATCH.value: 1,
    }
