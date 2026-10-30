"""Java's share of the name-based callee resolution: the constructors a
`new Foo(...)` runs, which every language that names its constructors after
the class (Java, C#, C++) reaches the same way.

A Java method call itself is typed by the Java engine
(`JavaTypeInferenceEngine`), which `CallResolver` bridges to; what the
resolver keeps for Java beyond that bridge is this lookup."""

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol
from ...utils import qn_markers


def java_constructor_targets(
    function_registry: FunctionRegistryTrieProtocol, class_qn: str
) -> set[tuple[str, str]]:
    # A Java constructor is registered as a method directly under its class whose
    # simple name equals the class's simple name (`Foo.Foo(int)`). `new Foo(...)`
    # resolves to the CLASS, so redirect a CALLS edge to each declared constructor
    # (all overloads); argument-type overload selection is not attempted, which is
    # unnecessary for reachability and never fabricates a call to a
    # non-constructor. Only constructors DIRECTLY on the class match (a nested
    # class's constructor has an extra qn segment and is excluded).
    # A duplicate-suffixed class (`Box@8` for `class Box<T>` beside `class
    # Box`) declares its constructor under its natural name, so the
    # marker is not part of the name to match (issue #2007), and nor is a
    # generic twin's CLR arity (`PB`1` declares `PB(...)`, issue #2579).
    simple = qn_markers.strip_arity_marker(
        qn_markers.strip_dup_marker(class_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1])
    )
    targets: set[tuple[str, str]] = set()
    for qn, node_type in function_registry.find_with_prefix(class_qn):
        head = qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
        parent, dot, mname = head.rpartition(cs.SEPARATOR_DOT)
        if dot and parent == class_qn and mname == simple:
            targets.add((node_type, qn))
    return targets
