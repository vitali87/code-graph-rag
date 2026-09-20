"""A C# fluent chain resolves in linear work, not exponential (issue #1800).

`resolve_csharp_method_call` types a chained invocation by resolving its
receiver, and THREE branches did that independently for the same receiver
node -- the class-qn path, the type-name path and the arity path -- so an
n-link chain re-resolved its whole prefix once per branch per link. Measured
on `main`: 58 resolver entries at 4 links, 4,916 at 8, 398,574 at 12 and
3,587,219 at 14 (~9x per two links, i.e. 3^2). A real `microsoft/aspire`
file wedged Pass 3 at 100% CPU for over half an hour.

The entry point now memoises per (module_qn, caller_qn, byte span), which
collapses all three branches at once.

`test_chain_resolution_work_grows_linearly` is the regression test for
#1800, and the only one here that fails on the pre-fix code: it asserts the
COMPLEXITY rather than a wall-clock time, because a timing threshold is flaky
on shared CI while the resolver-entry count is deterministic and is the
quantity the fix changes. The old code resolved these chains CORRECTLY, just
exponentially, so the resolution-coverage tests below pass with the memo
removed; they exist to catch a memo that buys speed by returning wrong or
missing answers, and they do fail if it is made to do so.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers.csharp import type_inference as ti
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_relationships,
    run_updater,
)

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_chain"
    project.mkdir()
    return project


def _chain_source(links: int) -> str:
    chain = "".join(f'\n            .Step{i}("x")' for i in range(links))
    exts = "\n".join(
        f"    public static Builder Step{i}(this Builder b, string s) => b;"
        for i in range(links)
    )
    return (
        "namespace N {\n"
        "  public class Builder { }\n"
        f"  public static class Ext {{\n{exts}\n  }}\n"
        "  public class Use {\n"
        f"    public void Run(Builder stage) {{\n        stage{chain};\n    }}\n"
        "  }\n"
        "}\n"
    )


def _resolver_entries(
    project: Path, mock_ingestor: MagicMock, links: int, monkeypatch: pytest.MonkeyPatch
) -> int:
    """Run one chain of `links` links and count entries into the resolver."""
    (project / "A.cs").write_text(_chain_source(links), encoding="utf-8")
    calls = {"n": 0}
    original = ti.CSharpTypeInferenceEngine.resolve_csharp_method_call

    def counting(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        ti.CSharpTypeInferenceEngine, "resolve_csharp_method_call", counting
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)
    return calls["n"]


def test_chain_resolution_work_grows_linearly(
    csharp_project: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doubling the chain must not square the work.

    Before the memo this ratio was ~80x for 6 -> 12 links (543 -> 398,574).
    The bound of 4x is far above the true linear ratio (45/21 = 2.1) and far
    below the exponential one, so it discriminates without being brittle.
    """
    short = _resolver_entries(csharp_project, mock_ingestor, 6, monkeypatch)
    mock_ingestor.reset_mock()
    long = _resolver_entries(csharp_project, mock_ingestor, 12, monkeypatch)

    assert short > 0, "fixture guard: the resolver was never entered"
    assert long < short * 4, (
        f"chain resolution is superlinear: {short} entries at 6 links, "
        f"{long} at 12 (linear would be about {short * 2})"
    )


def test_long_chain_still_resolves_every_link(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    """The memo must not cost coverage: every link still binds.

    A cache that returned a stale or empty answer would make the timing test
    above pass while silently dropping edges, so the win is only real if the
    chain still resolves completely.
    """
    links = 12
    (csharp_project / "A.cs").write_text(_chain_source(links), encoding="utf-8")
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}
    missing = [
        i for i in range(links) if not any(f"Ext.Step{i}(" in t for t in targets)
    ]
    assert not missing, f"links {missing} did not resolve; got {sorted(targets)}"


def test_memo_does_not_leak_between_resolution_passes(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    """`reset_resolution_caches` clears the chain memo with the others.

    The memo is keyed by byte span, so a re-parsed file whose code moved would
    otherwise be served an answer computed for whatever used to occupy those
    bytes.
    """
    (csharp_project / "A.cs").write_text(_chain_source(4), encoding="utf-8")
    updater = create_and_run_updater(
        csharp_project, mock_ingestor, skip_if_missing=SKIP
    )

    engine = updater.factory.type_inference._csharp_type_inference
    assert engine is not None, "fixture guard: no C# engine was built"
    assert engine._call_memo, "fixture guard: the memo was never populated"

    updater.factory.call_processor.reset_resolution_caches()
    assert not engine._call_memo, "the chain memo survived a cache reset"


# The shape from the file that actually wedged a `microsoft/aspire` index:
# `src/Aspire.Hosting.Python/PythonAppResourceBuilderExtensions.cs`, 1540
# lines, 253.6s on its own. Extension methods on a builder with MIXED
# arities, including zero-argument links -- the uniform one-argument chain
# above exercises the arity branch only incidentally.
ASPIRE_SHAPE = """namespace Aspire.Hosting {
  public class DockerfileBuilder { }
  public static class DockerfileBuilderExtensions {
    public static DockerfileBuilder Comment(this DockerfileBuilder b, string t) => b;
    public static DockerfileBuilder Copy(this DockerfileBuilder b, string s, string d) => b;
    public static DockerfileBuilder EmptyLine(this DockerfileBuilder b) => b;
    public static DockerfileBuilder Run(this DockerfileBuilder b, string cmd) => b;
  }
  public class PythonAppResourceBuilder {
    public void Write(DockerfileBuilder stage) {
        stage
            .Comment("Copy requirements.txt for dependency installation")
            .Copy("requirements.txt", "/app/requirements.txt")
            .EmptyLine()
            .Comment("Install dependencies using pip")
            .Run("apt-get update && pip install -r requirements.txt")
            .EmptyLine()
            .Comment("Copy application code")
            .Copy(".", "/app")
            .EmptyLine();
    }
  }
}
"""


def test_real_world_mixed_arity_chain_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    """Every distinct extension in the reported aspire chain still binds.

    Mixed arities matter: `_receiver_type_arity` is one of the three branches
    that recursed, and a zero-argument link (`EmptyLine()`) is the case where
    an arity-keyed answer is easiest to get wrong.
    """
    (csharp_project / "PythonAppResourceBuilder.cs").write_text(
        ASPIRE_SHAPE, encoding="utf-8"
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}
    for method in ("Comment(", "Copy(", "EmptyLine(", "Run("):
        assert any(f"DockerfileBuilderExtensions.{method}" in t for t in targets), (
            f"{method} did not resolve in the aspire-shaped chain: {sorted(targets)}"
        )
