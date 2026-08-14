# PHP qualified names are path-derived (the namespace declaration is
# ignored), so Xdebug frames resolve primarily by file plus line span; the
# runtime name's class/method tail is the fallback when a leaf callee's
# defining file could not be recovered (issue #1253).

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.trace.records import FramePoint
from codebase_rag.trace.resolution import (
    CallableNode,
    PhpFrameResolver,
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


_NODES = [
    _node(cs.NodeLabel.MODULE, f"{_P}.main", "main.php", None, None),
    _node(cs.NodeLabel.FUNCTION, f"{_P}.main.describeAnimal", "main.php", 11, 14),
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.src.Registry.Registry.handle",
        "src/Registry.php",
        14,
        17,
    ),
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.src.Registry.Registry.greet",
        "src/Registry.php",
        19,
        22,
    ),
    _node(cs.NodeLabel.METHOD, f"{_P}.src.Animal.Dog.sound", "src/Animal.php", 12, 15),
    _node(
        cs.NodeLabel.FUNCTION,
        f"{_P}.src.Shapes.outer.anonymous_9_10",
        "src/Shapes.php",
        10,
        12,
    ),
    _node(cs.NodeLabel.FUNCTION, f"{_P}.src.Shapes.outer", "src/Shapes.php", 5, 20),
]


def _resolver(tmp_path):
    return PhpFrameResolver(tmp_path, _NODES)


def _frame(tmp_path, rel_path, qualname, line):
    path = str(tmp_path / rel_path) if rel_path else ""
    return FramePoint(path=path, qualname=qualname, line=line)


def test_resolves_caller_frames_by_call_site_span(tmp_path):
    resolved = _resolver(tmp_path).resolve(
        _frame(tmp_path, "main.php", "describeAnimal", 13), ResolutionStats()
    )

    assert resolved is not None
    assert resolved.qualified_name == f"{_P}.main.describeAnimal"


def test_resolves_main_frame_to_module(tmp_path):
    resolved = _resolver(tmp_path).resolve(
        _frame(tmp_path, "main.php", cs.TRACE_XDEBUG_MAIN, 20), ResolutionStats()
    )

    assert resolved is not None
    assert resolved.label == cs.NodeLabel.MODULE
    assert resolved.qualified_name == f"{_P}.main"


def test_resolves_enriched_callee_by_span_despite_namespace(tmp_path):
    # The runtime name carries a namespace the graph never stores; the
    # recovered defining position must decide.
    resolved = _resolver(tmp_path).resolve(
        _frame(tmp_path, "src/Registry.php", r"App\Services\Registry::handle", 16),
        ResolutionStats(),
    )

    assert resolved is not None
    assert resolved.qualified_name == f"{_P}.src.Registry.Registry.handle"


def test_resolves_pathless_leaf_by_class_method_tail(tmp_path):
    resolved = _resolver(tmp_path).resolve(
        _frame(tmp_path, "", r"App\Services\Dog->sound", 0), ResolutionStats()
    )

    assert resolved is not None
    assert resolved.qualified_name == f"{_P}.src.Animal.Dog.sound"


def test_resolves_closure_frames_by_embedded_position(tmp_path):
    qualname = f"{{closure:{tmp_path / 'src/Shapes.php'}:10-12}}"

    resolved = _resolver(tmp_path).resolve(
        _frame(tmp_path, "", qualname, 0), ResolutionStats()
    )

    assert resolved is not None
    assert resolved.qualified_name == f"{_P}.src.Shapes.outer.anonymous_9_10"


def test_ambiguous_name_tails_are_unresolved_not_guessed(tmp_path):
    # Two unrelated classes named Dog in different files: a pathless frame
    # must not attach the edge to whichever sorts first.
    nodes = [
        *_NODES,
        _node(
            cs.NodeLabel.METHOD,
            f"{_P}.src.Alpha.Dog.sound",
            "src/Alpha.php",
            3,
            6,
        ),
    ]
    stats = ResolutionStats()

    resolved = PhpFrameResolver(tmp_path, nodes).resolve(
        _frame(tmp_path, "", r"App\Services\Dog->sound", 0), stats
    )

    assert resolved is None
    assert stats.unresolved == {cs.TraceUnresolvedReason.AMBIGUOUS.value: 1}


def test_unresolved_reasons_are_categorised(tmp_path):
    resolver = _resolver(tmp_path)
    stats = ResolutionStats()

    outside = resolver.resolve(
        FramePoint(path="/elsewhere/x.php", qualname="f", line=1), stats
    )
    unknown = resolver.resolve(_frame(tmp_path, "src/Gone.php", "f", 1), stats)
    missing = resolver.resolve(_frame(tmp_path, "", r"App\Ghost->vanish", 0), stats)

    assert outside is None
    assert unknown is None
    assert missing is None
    assert stats.unresolved == {
        cs.TraceUnresolvedReason.OUTSIDE_REPO.value: 1,
        cs.TraceUnresolvedReason.UNKNOWN_PATH.value: 1,
        cs.TraceUnresolvedReason.NO_MATCH.value: 1,
    }
