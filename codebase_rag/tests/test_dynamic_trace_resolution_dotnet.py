# .NET sampled frames carry CLR names with no file paths: nested classes use
# `+`, constructors are `.ctor`, properties are `get_`/`set_` accessors, and
# async methods surface as compiler state machines. The resolver must
# demangle these onto the graph's namespace-bearing qualified names
# (issue #1249).

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.trace.records import FramePoint
from codebase_rag.trace.resolution import (
    CallableNode,
    DotnetFrameResolver,
    ResolutionStats,
)

_P = "proj"


def _node(label, qualified_name, path="Services/GreetingService.cs"):
    return CallableNode(
        label=label,
        qualified_name=qualified_name,
        path=path,
        start_line=1,
        end_line=50,
    )


_NODES = [
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.Services.GreetingService.MyApp.Services.GreetingService.Greet(string)",
    ),
    # Zero-arg methods carry no parentheses in the graph.
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.Services.GreetingService.MyApp.Services.GreetingService.Reset",
    ),
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.Worker.MyApp.Worker.RunAsync",
        path="Worker.cs",
    ),
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.Worker.MyApp.Worker.Worker(string)",
        path="Worker.cs",
    ),
    # A property is one Method node, no accessor prefix.
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.Worker.MyApp.Worker.Size",
        path="Worker.cs",
    ),
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.Box.MyApp.Outer.Inner.M",
        path="Box.cs",
    ),
    # A C# local function nests under its hosting method.
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.Worker.MyApp.Worker.Run.Local",
        path="Worker.cs",
    ),
    # An accessor-hosted local nests at class level (the accessor body is not
    # its own scope in the static tier).
    _node(
        cs.NodeLabel.METHOD,
        f"{_P}.Worker.MyApp.Worker.Local",
        path="Worker.cs",
    ),
]


def _resolver():
    return DotnetFrameResolver(_NODES)


def _frame(qualname):
    return FramePoint(path="", qualname=qualname, line=0)


def _resolve(qualname, stats=None):
    return _resolver().resolve(_frame(qualname), stats or ResolutionStats())


def test_resolves_plain_method_with_and_without_signature():
    signatured = _resolve("MyApp.Services.GreetingService.Greet")
    bare = _resolve("MyApp.Services.GreetingService.Reset")

    assert signatured is not None
    assert signatured.qualified_name.endswith("Greet(string)")
    assert bare is not None
    assert bare.qualified_name.endswith("GreetingService.Reset")


def test_resolves_async_state_machine_to_source_method():
    resolved = _resolve("MyApp.Worker+<RunAsync>d__3.MoveNext")

    assert resolved is not None
    assert resolved.qualified_name.endswith("MyApp.Worker.RunAsync")


def test_resolves_constructor_to_class_named_node():
    resolved = _resolve("MyApp.Worker..ctor")

    assert resolved is not None
    assert resolved.qualified_name.endswith("MyApp.Worker.Worker(string)")


def test_resolves_local_function_to_nested_node():
    # `<Run>g__Local|0_0` is a C# local function; it resolves to the
    # `Run.Local` node the static tier nests under the hosting method, not to
    # `Run` and not dropped as synthetic.
    resolved = _resolve("MyApp.Worker.<Run>g__Local|0_0")

    assert resolved is not None
    assert resolved.qualified_name.endswith("MyApp.Worker.Run.Local")


def test_resolves_accessor_hosted_local_function_at_class_level():
    # A local function inside a property accessor nests at class level, since
    # the accessor body is not its own scope in the static tier.
    resolved = _resolve("MyApp.Worker.<get_Size>g__Local|0_0")

    assert resolved is not None
    assert resolved.qualified_name.endswith("MyApp.Worker.Local")


def test_resolves_property_accessors_to_property_node():
    getter = _resolve("MyApp.Worker.get_Size")
    setter = _resolve("MyApp.Worker.set_Size")

    assert getter is not None
    assert getter.qualified_name.endswith("MyApp.Worker.Size")
    assert setter is not None
    assert setter.qualified_name.endswith("MyApp.Worker.Size")


def test_resolves_display_class_lambda_to_enclosing_method():
    resolved = _resolve("MyApp.Worker+<>c.<RunAsync>b__2_0")

    assert resolved is not None
    assert resolved.qualified_name.endswith("MyApp.Worker.RunAsync")


def test_resolves_nested_class_plus_to_dots():
    resolved = _resolve("MyApp.Outer+Inner.M")

    assert resolved is not None
    assert resolved.qualified_name.endswith("MyApp.Outer.Inner.M")


def test_static_initializer_and_unknown_are_categorised():
    stats = ResolutionStats()
    resolver = _resolver()

    cctor = resolver.resolve(_frame("MyApp.Worker..cctor"), stats)
    unknown = resolver.resolve(_frame("MyApp.Gone.Missing"), stats)

    assert cctor is None
    assert unknown is None
    assert stats.unresolved == {
        cs.TraceUnresolvedReason.SYNTHETIC.value: 1,
        cs.TraceUnresolvedReason.NO_MATCH.value: 1,
    }


def test_resolves_generic_arity_names_to_source_nodes():
    # dotnet-trace keeps CLR arity markers (`Cache`1`, `Get`1`); the graph
    # stores the source spelling, so they must be stripped before matching.
    nodes = [_node(cs.NodeLabel.METHOD, f"{_P}.Cache.MyApp.Cache.Get(string)")]
    resolver = DotnetFrameResolver(nodes)

    resolved = resolver.resolve(_frame("MyApp.Cache`1.Get`1"), ResolutionStats())

    assert resolved is not None
    assert resolved.qualified_name.endswith("MyApp.Cache.Get(string)")
