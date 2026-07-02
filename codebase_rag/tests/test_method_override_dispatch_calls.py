from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_calls(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    # (H) Build the graph for `files` and return CALLS edges as (caller_qn, callee_qn).
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    for name, src in files.items():
        (tmp_path / name).write_text(src, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    out: set[tuple[str, str]] = set()
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] == "CALLS":
            out.add((c.args[0][2], c.args[2][2]))
    return out


def _has(calls: set[tuple[str, str]], caller_suffix: str, callee_suffix: str) -> bool:
    return any(
        a.endswith(caller_suffix) and b.endswith(callee_suffix) for a, b in calls
    )


def test_self_call_dispatches_to_subclass_override(tmp_path: Path) -> None:
    # (H) Base.run invokes self.op(); Sub overrides op. The runtime receiver may be a
    # (H) Sub, so the call graph must connect Base.run -> Sub.op or Sub.op is dead.
    src = (
        "class Base:\n"
        "    def run(self):\n"
        "        return self.op()\n"
        "    def op(self):\n"
        "        return 0\n\n\n"
        "class Sub(Base):\n"
        "    def op(self):\n"
        "        return 1\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.Base.run", "m.Sub.op")
    # (H) The base edge must still be present.
    assert _has(calls, "m.Base.run", "m.Base.op")


def test_self_call_dispatches_to_transitive_override(tmp_path: Path) -> None:
    # (H) Override two levels down must also be connected.
    src = (
        "class Base:\n"
        "    def run(self):\n"
        "        return self.op()\n"
        "    def op(self):\n"
        "        return 0\n\n\n"
        "class Mid(Base):\n"
        "    pass\n\n\n"
        "class Leaf(Mid):\n"
        "    def op(self):\n"
        "        return 2\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.Base.run", "m.Leaf.op")


def test_super_call_does_not_fan_out_to_own_override(tmp_path: Path) -> None:
    # (H) Sub.op delegates to the parent with super().op(); this explicitly targets
    # (H) Base.op, not a virtual dispatch, so it must NOT create a false recursive
    # (H) Sub.op -> Sub.op edge via override fan-out.
    src = (
        "class Base:\n"
        "    def op(self):\n"
        "        return 0\n\n\n"
        "class Sub(Base):\n"
        "    def op(self):\n"
        "        return super().op()\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert not _has(calls, "m.Sub.op", "m.Sub.op")


def test_explicit_base_qualified_call_does_not_fan_out(tmp_path: Path) -> None:
    # (H) A call naming the exact implementation (Base.op(x)) is not a virtual
    # (H) dispatch, so it must not fan out to subclass overrides.
    src = (
        "class Base:\n"
        "    def op(self):\n"
        "        return 0\n\n\n"
        "class Sub(Base):\n"
        "    def op(self):\n"
        "        return 1\n\n\n"
        "def run(b):\n"
        "    return Base.op(b)\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert not _has(calls, "m.run", "m.Sub.op")


def test_no_override_dispatch_without_subclasses(tmp_path: Path) -> None:
    # (H) A method with no overriding subclass must not gain spurious edges.
    src = (
        "class Only:\n"
        "    def run(self):\n"
        "        return self.op()\n"
        "    def op(self):\n"
        "        return 0\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.Only.run", "m.Only.op")
    assert not any(
        a.endswith("m.Only.run") and b.endswith(".op") and not b.endswith("Only.op")
        for a, b in calls
    )


def test_self_call_in_abstract_base_reaches_all_overrides(tmp_path: Path) -> None:
    # (H) Base._chunk calls self._raw(); Base._raw is @abstractmethod and TWO subclasses
    # (H) override it. The edge must anchor on the enclosing class (Base) and reach BOTH
    # (H) overrides plus the base declaration, not just the alphabetically-first override
    # (H) the trie happens to pick (SqsBatchJob/SqsKick/SqsDelete shape).
    src = (
        "from abc import ABC, abstractmethod\n"
        "class Base(ABC):\n"
        "    @abstractmethod\n"
        "    def _raw(self, ctx):\n"
        "        pass\n"
        "    def _chunk(self, ctx):\n"
        "        return self._raw(ctx)\n\n\n"
        "class Kick(Base):\n"
        "    def _raw(self, ctx):\n"
        "        return 1\n\n\n"
        "class Delete(Base):\n"
        "    def _raw(self, ctx):\n"
        "        return 2\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.Base._chunk", "m.Kick._raw")
    assert _has(calls, "m.Base._chunk", "m.Delete._raw")


def test_self_call_binds_to_own_class_method_amid_cousin_overrides(
    tmp_path: Path,
) -> None:
    # (H) OpenAIClient.gen calls self._generate(); a sibling subclass AnthropicClient also
    # (H) defines _generate. The call must bind to the ENCLOSING class's own method
    # (H) (OpenAIClient._generate), not an arbitrary cousin override (OpenAIClient shape).
    src = (
        "from abc import ABC, abstractmethod\n"
        "class BaseLLM(ABC):\n"
        "    @abstractmethod\n"
        "    def _generate(self, m):\n"
        "        pass\n\n\n"
        "class OpenAIClient(BaseLLM):\n"
        "    def _generate(self, m):\n"
        "        return 1\n"
        "    def gen(self, m):\n"
        "        return self._generate(m)\n\n\n"
        "class AnthropicClient(BaseLLM):\n"
        "    def _generate(self, m):\n"
        "        return 2\n"
    )
    calls = _run_calls(tmp_path, {"m.py": src})
    assert _has(calls, "m.OpenAIClient.gen", "m.OpenAIClient._generate")
