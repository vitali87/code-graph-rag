"""`JavaResolver`: the constructors a `new Foo(...)` runs, found among the
class's own members by the class's simple name, and the bridge from a Java
method call to the Java engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.java.resolution import JavaResolver
from codebase_rag.tests.collaborator_support import first, index, parse
from evals.recorded_graph import RecordingIngestor

_SRC = "jp.src.main.java.app"
_USE = (
    "package app;\n"
    "public class Use {\n"
    "  class Inner { Inner() {} Inner(int x) {} }\n"
    "  Use() {}\n"
    "  Use(int a, String b) {}\n"
    "  void Use2() {}\n"
    "  int size() { return 0; }\n"
    "  int run() { return this.size(); }\n"
    "}\n"
)


@pytest.fixture
def resolver(temp_repo: Path, mock_ingestor: RecordingIngestor) -> JavaResolver:
    updater = index(
        temp_repo / "jp", mock_ingestor, {"src/main/java/app/Use.java": _USE}
    )
    java = updater.factory.call_processor._resolver.java
    assert isinstance(java, JavaResolver)
    return java


def test_a_classs_constructors_are_its_own_members_named_like_it(
    resolver: JavaResolver,
) -> None:
    assert resolver.java_constructor_targets(f"{_SRC}.Use.Use") == {
        (cs.NodeLabel.METHOD, f"{_SRC}.Use.Use.Use()"),
        (cs.NodeLabel.METHOD, f"{_SRC}.Use.Use.Use(int,String)"),
    }


def test_a_nested_classs_constructors_are_its_own(resolver: JavaResolver) -> None:
    assert resolver.java_constructor_targets(f"{_SRC}.Use.Use.Inner") == {
        (cs.NodeLabel.METHOD, f"{_SRC}.Use.Use.Inner.Inner()"),
        (cs.NodeLabel.METHOD, f"{_SRC}.Use.Use.Inner.Inner(int)"),
    }
    assert resolver.java_constructor_targets(f"{_SRC}.Missing") == set()


def test_a_method_call_binds_through_the_java_engine(resolver: JavaResolver) -> None:
    call = first(parse(cs.SupportedLanguage.JAVA, _USE), cs.TS_METHOD_INVOCATION)
    assert resolver.resolve_java_method_call(
        call, f"{_SRC}.Use", None, f"{_SRC}.Use.Use.run()"
    ) == (cs.NodeLabel.METHOD, f"{_SRC}.Use.Use.size()")
    # The memos reset without forgetting what the registry holds.
    resolver.reset_resolution_memos()
    assert resolver.java_constructor_targets(f"{_SRC}.Use.Use")
