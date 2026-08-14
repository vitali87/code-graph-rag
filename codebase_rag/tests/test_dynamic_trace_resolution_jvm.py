# JVM runtime frames use binary names (Outer$Inner, <init>, lambda$run$0,
# Scala's Util$) and package-derived paths, while the graph stores dotted
# qualified names composed from the filesystem path with raw-source parameter
# signatures. The JVM resolver must bridge the two (issue #1248).

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.trace.records import FramePoint
from codebase_rag.trace.resolution import (
    CallableNode,
    JvmFrameResolver,
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
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.src.main.java.com.example.Foo.Foo.bar()",
        "src/main/java/com/example/Foo.java",
        5,
        9,
    ),
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.src.main.java.com.example.Foo.Foo.bar(String)",
        "src/main/java/com/example/Foo.java",
        11,
        14,
    ),
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.src.main.java.com.example.Foo.Foo.Foo(String)",
        "src/main/java/com/example/Foo.java",
        2,
        4,
    ),
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.src.main.java.com.example.Outer.Outer.Inner.value()",
        "src/main/java/com/example/Outer.java",
        8,
        10,
    ),
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.src.main.java.com.example.Client.Client.start()",
        "src/main/java/com/example/Client.java",
        6,
        20,
    ),
    # An anonymous class override is stored as a Function threaded through
    # the enclosing method, with no `$1` anywhere.
    _node(
        cs.NodeLabel.FUNCTION,
        f"{_P}.src.main.java.com.example.Client.Client.start.run",
        "src/main/java/com/example/Client.java",
        12,
        14,
    ),
    # Scala: no parameter signature, object stored as a plain class scope.
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.Util.Util.free",
        "Util.scala",
        3,
        5,
    ),
]


def _resolver() -> JvmFrameResolver:
    return JvmFrameResolver(_NODES)


def _frame(path, qualname, line):
    return FramePoint(path=path, qualname=qualname, line=line)


def test_resolves_plain_method_and_prefers_line_span_among_overloads():
    resolver = _resolver()
    stats = ResolutionStats()

    no_arg = resolver.resolve(_frame("com/example/Foo.java", "Foo.bar", 6), stats)
    with_arg = resolver.resolve(_frame("com/example/Foo.java", "Foo.bar", 12), stats)

    assert no_arg is not None
    assert no_arg.qualified_name.endswith("Foo.bar()")
    assert with_arg is not None
    assert with_arg.qualified_name.endswith("Foo.bar(String)")
    assert stats.total == 0


def test_resolves_constructor_to_class_named_node():
    resolved = _resolver().resolve(
        _frame("com/example/Foo.java", "Foo.<init>", 3), ResolutionStats()
    )

    assert resolved is not None
    assert resolved.qualified_name.endswith("Foo.Foo(String)")


def test_resolves_inner_class_dollar_to_dotted_nesting():
    resolved = _resolver().resolve(
        _frame("com/example/Outer.java", "Outer$Inner.value", 9), ResolutionStats()
    )

    assert resolved is not None
    assert resolved.qualified_name.endswith("Outer.Inner.value()")


def test_resolves_lambda_body_to_enclosing_method_by_span():
    resolved = _resolver().resolve(
        _frame("com/example/Client.java", "Client.lambda$start$0", 17),
        ResolutionStats(),
    )

    assert resolved is not None
    assert resolved.qualified_name.endswith("Client.start()")


def test_resolves_anonymous_class_method_by_span():
    resolved = _resolver().resolve(
        _frame("com/example/Client.java", "Client$1.run", 13), ResolutionStats()
    )

    assert resolved is not None
    assert resolved.label == cs.NodeLabel.FUNCTION
    assert resolved.qualified_name.endswith("Client.start.run")


def test_resolves_scala_object_trailing_dollar():
    resolved = _resolver().resolve(
        _frame("Util.scala", "Util$.free", 4), ResolutionStats()
    )

    assert resolved is not None
    assert resolved.qualified_name == f"{_P}.Util.Util.free"


def test_anonymous_class_constructor_is_synthetic():
    # `new Runnable() {...}` compiles to Client$1.<init> at the expression
    # line; resolving it by span would fabricate a self-edge on the
    # enclosing method.
    stats = ResolutionStats()

    resolved = _resolver().resolve(
        _frame("com/example/Client.java", "Client$1.<init>", 11), stats
    )

    assert resolved is None
    assert stats.unresolved == {cs.TraceUnresolvedReason.SYNTHETIC.value: 1}


def test_static_initializer_is_synthetic():
    stats = ResolutionStats()

    resolved = _resolver().resolve(
        _frame("com/example/Foo.java", "Foo.<clinit>", 1), stats
    )

    assert resolved is None
    assert stats.unresolved == {cs.TraceUnresolvedReason.SYNTHETIC.value: 1}


def test_unknown_path_and_no_match_are_counted():
    resolver = _resolver()
    stats = ResolutionStats()

    assert resolver.resolve(_frame("com/other/Gone.java", "Gone.x", 1), stats) is None
    assert (
        resolver.resolve(_frame("com/example/Foo.java", "Foo.absent", 99), stats)
        is None
    )
    assert stats.unresolved == {
        cs.TraceUnresolvedReason.UNKNOWN_PATH.value: 1,
        cs.TraceUnresolvedReason.NO_MATCH.value: 1,
    }
