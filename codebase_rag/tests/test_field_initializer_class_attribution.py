# Instance field initializers run when an object is constructed, so their
# calls/instantiations belong to the class's constructor (or the class node
# when there is none), not to the file's Module (issue #3339). Static field
# initializers run at definition time and stay module-attributed.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_relationships,
    run_updater,
)


def _pairs(mock_ingestor: MagicMock, rel_type: str) -> list[tuple[str, str]]:
    return [
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel_type)
    ]


def _calls(mock_ingestor: MagicMock) -> list[tuple[str, str]]:
    return _pairs(mock_ingestor, "CALLS")


def _instantiates(mock_ingestor: MagicMock) -> list[tuple[str, str]]:
    return _pairs(mock_ingestor, "INSTANTIATES")


def test_ts_instance_field_initializers_attribute_to_constructor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "tsfield"
    project.mkdir()
    (project / "svc.ts").write_text(
        """
export function helper(): number { return 1; }
export class Logger { constructor() {} }
export class Svc {
  private readonly logger = new Logger();
  n = helper();
  static s = helper();
  declare d: number;
  @dec(helper())
  decorated = helper();
  constructor() {}
}
function dec(_x: number): (t: unknown) => void { return () => {}; }
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing="typescript")

    calls = _calls(mock_ingestor)
    instantiates = _instantiates(mock_ingestor)
    ctor = "Svc.constructor"
    assert any(
        s.endswith(ctor) and t.endswith("Logger.constructor") for s, t in calls
    ), calls
    assert any(s.endswith(ctor) and t.endswith("Logger") for s, t in instantiates), (
        instantiates
    )
    # `n = helper()` and `decorated = helper()` both reach the
    # constructor: the decorator's argument and the arrow body must not
    # leak into it, but the decorated field's own value is an initializer.
    assert sum(1 for s, t in calls if s.endswith(ctor) and t.endswith("helper")) == 2, (
        calls
    )
    # No module-attributed edge to the constructor's construction dependency.
    assert not any(
        s.endswith("svc") and t.endswith("Logger.constructor") for s, t in calls
    ), calls
    assert not any(
        s.endswith("svc") and t.endswith("Logger") for s, t in instantiates
    ), instantiates
    # The static initializer stays module-attributed.
    assert any(s.endswith("svc") and t.endswith("helper") for s, t in calls), calls


def test_ts_arrow_field_body_calls_stay_with_arrow_method(
    tmp_path: Path,
) -> None:
    # Calls inside an arrow-valued field belong to that arrow's Method
    # node, not to the constructor; only the assignment-time expressions
    # of other fields reach the constructor. Uses the evals capture
    # harness like test_ts_class_field_arrow.py: the module-scope
    # assignment-references pass labels an arrow-valued field Function
    # while the definition pass registers it Method, a pre-existing
    # dangling-REFERENCES mismatch unrelated to this fix, so the audited
    # harness cannot index this shape yet.
    from evals.cgr_graph import _capture

    (tmp_path / "svc.ts").write_text(
        """
export function helper(): number { return 1; }
export class Svc {
  n = helper();
  static s = helper();
  arrow = () => helper();
  constructor() {}
}
""",
        encoding="utf-8",
    )
    ingestor = _capture(tmp_path, "p")
    calls = [
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    ]
    assert ("p.svc.Svc.arrow", "p.svc.helper") in calls, calls
    assert (
        sum(1 for s, t in calls if s == "p.svc.Svc.constructor" and t == "p.svc.helper")
        == 1
    ), calls
    assert ("p.svc", "p.svc.helper") in calls, calls


def test_ts_field_initializer_without_constructor_attributes_to_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "tsnoc"
    project.mkdir()
    (project / "widget.ts").write_text(
        """
export function helper(): number { return 1; }
export class Svc {
  n = helper();
}
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing="typescript")

    sources = {s for s, t in _calls(mock_ingestor) if t.endswith("helper")}
    assert len(sources) == 1, sources
    assert next(iter(sources)).endswith("Svc"), sources


def test_java_instance_field_initializers_attribute_to_constructor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "jfield"
    (project / "com" / "example").mkdir(parents=True)
    (project / "com" / "example" / "M.java").write_text(
        """
package com.example;
class Factory { Factory() {} }
class Svc {
  private final Factory factory = new Factory();
  int n = helper();
  static int s = helper();
  Svc() {}
  int helper() { return 1; }
}
""",
        encoding="utf-8",
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="java")

    calls = _calls(mock_ingestor)
    instantiates = _instantiates(mock_ingestor)
    assert any(
        s.endswith("Svc.Svc()") and t.endswith("Factory.Factory()") for s, t in calls
    ), calls
    assert any(
        s.endswith("Svc.Svc()") and t.endswith("Factory") for s, t in instantiates
    ), instantiates
    assert any(
        s.endswith("Svc.Svc()") and t.endswith("Svc.helper()") for s, t in calls
    ), calls
    # The static initializer stays module-attributed.
    helper_sources = {s for s, t in calls if t.endswith("Svc.helper()")}
    assert len(helper_sources) == 2, helper_sources
    assert any(s.endswith("Svc.Svc()") for s in helper_sources)
    assert any(not s.endswith("Svc()") for s in helper_sources)


def test_java_overloaded_constructor_chain_attributes_to_sink_only(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "jchain"
    (project / "com" / "example").mkdir(parents=True)
    (project / "com" / "example" / "M.java").write_text(
        """
package com.example;
class Factory { Factory() {} }
class Chain {
  Factory f = new Factory();
  Chain() { this(1); }
  Chain(int x) {}
}
""",
        encoding="utf-8",
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="java")

    calls = _calls(mock_ingestor)
    sink_edges = [
        (s, t)
        for s, t in calls
        if s.endswith("Chain.Chain(int)") and t.endswith("Factory.Factory()")
    ]
    assert len(sink_edges) == 1, calls
    assert not any(
        s.endswith("Chain.Chain()") and t.endswith("Factory.Factory()")
        for s, t in calls
    ), calls


def test_java_inner_class_field_initializer_attributes_to_inner(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "jinner"
    (project / "com" / "example").mkdir(parents=True)
    (project / "com" / "example" / "M.java").write_text(
        """
package com.example;
class Factory { Factory() {} }
class Outer {
  Factory f = new Factory();
  Outer() {}
  static class Inner {
    Factory g = new Factory();
    Inner() {}
  }
}
""",
        encoding="utf-8",
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="java")

    calls = _calls(mock_ingestor)
    assert any(
        s.endswith("Outer.Outer()") and t.endswith("Factory.Factory()")
        for s, t in calls
    ), calls
    assert any(
        s.endswith("Outer.Inner.Inner()") and t.endswith("Factory.Factory()")
        for s, t in calls
    ), calls
    assert not any(
        s.endswith("Outer") and t.endswith("Factory.Factory()") for s, t in calls
    ), calls


def test_csharp_instance_field_initializers_attribute_to_constructor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "csfield"
    project.mkdir()
    (project / "App.cs").write_text(
        """
namespace N;
public class Factory { public Factory() {} }
public class Svc {
  private readonly Factory factory = new Factory();
  private readonly int n = Helper();
  private static int s = Helper();
  public Svc() {}
  public int Helper() { return 1; }
}
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing="c_sharp")

    calls = _calls(mock_ingestor)
    instantiates = _instantiates(mock_ingestor)
    assert any(
        s.endswith("Svc.Svc") and t.endswith("Factory.Factory") for s, t in calls
    ), calls
    assert any(
        s.endswith("Svc.Svc") and t.endswith("Factory") for s, t in instantiates
    ), instantiates
    # The bare call in the initializer resolves in the enclosing class scope.
    helper_sources = {s for s, t in calls if t.endswith("Svc.Helper")}
    assert len(helper_sources) == 1, helper_sources
    assert next(iter(helper_sources)).endswith("Svc.Svc"), helper_sources
    # Nothing from the initializer is left on the module.
    assert not any(
        s.endswith("App") and t.endswith("Factory.Factory") for s, t in calls
    ), calls


def test_csharp_overloaded_constructor_chain_attributes_to_sink_only(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "csChain"
    project.mkdir()
    (project / "App.cs").write_text(
        """
namespace N;
public class Factory { public Factory() {} }
public class Chain {
  private Factory f = new Factory();
  public Chain() : this(1) {}
  public Chain(int x) {}
}
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing="c_sharp")

    calls = _calls(mock_ingestor)
    sink_edges = [
        (s, t)
        for s, t in calls
        if s.endswith("Chain.Chain(int)") and t.endswith("Factory.Factory")
    ]
    assert len(sink_edges) == 1, calls
    assert not any(
        s.endswith("Chain.Chain")
        and not s.endswith("(int)")
        and t.endswith("Factory.Factory")
        for s, t in calls
    ), calls
