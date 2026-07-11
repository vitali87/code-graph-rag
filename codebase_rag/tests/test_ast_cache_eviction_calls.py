from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    # (H) CALLS edges as (caller_qn, callee_qn).
    out: set[tuple[str, str]] = set()
    for c in mock_ingestor.ensure_relationship_batch.call_args_list:
        if c.args[1] == "CALLS":
            out.add((c.args[0][2], c.args[2][2]))
    return out


def test_calls_survive_ast_cache_eviction(tmp_path: Path) -> None:
    # (H) The AST cache is bounded and evicts on large repos. Pass 3 must still
    # (H) emit calls for every parsed file, not just cache survivors. With the
    # (H) cache pinned to one entry, all but the last-parsed file are evicted
    # (H) during Pass 2; every module's intra-file call must still be recorded.
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")

    for name in ("a", "b", "c", "d", "e"):
        (tmp_path / f"{name}.py").write_text(
            "def helper():\n    return 1\n\n\ndef top():\n    return helper()\n",
            encoding="utf-8",
        )

    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    )
    updater.ast_cache.max_entries = 1  # force eviction of all but the last file
    updater.run()

    calls = _calls(mock)
    for name in ("a", "b", "c", "d", "e"):
        assert any(
            caller.endswith(f"{name}.top") and callee.endswith(f"{name}.helper")
            for caller, callee in calls
        ), (
            f"missing top->helper CALLS edge for evicted module {name}; calls={sorted(calls)}"
        )

    # (H) A reused updater must reset per-run parse tracking, not accumulate.
    parsed_after_first = len(updater._parsed_files)
    updater.run(force=True)
    assert len(updater._parsed_files) == parsed_after_first


def test_factory_return_inference_survives_ast_cache_eviction(tmp_path: Path) -> None:
    # (H) Type inference reads OTHER modules' ASTs (factory return statements,
    # (H) class bodies, self-assignment maps). On a repo larger than
    # (H) CACHE_MAX_ENTRIES the factory's module is often evicted by the time
    # (H) the caller's calls are processed (django: urls/resolvers.py evicted
    # (H) before contrib/admindocs/views.py resolves get_resolver()); the
    # (H) lookup must reload from disk, not silently drop the inferred type.
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")

    (tmp_path / "widgets.py").write_text(
        "class Aaa:\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "class Widget:\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "def make_widget():\n"
        "    return Widget()\n",
        encoding="utf-8",
    )
    (tmp_path / "driver.py").write_text(
        "from widgets import make_widget\n"
        "\n"
        "def driver():\n"
        "    make_widget().run()\n",
        encoding="utf-8",
    )

    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    )
    updater.ast_cache.max_entries = 1
    updater.run()

    calls = _calls(mock)
    assert any(
        caller.endswith("driver.driver") and callee.endswith("widgets.Widget.run")
        for caller, callee in calls
    ), sorted(calls)
    assert not any(callee.endswith("widgets.Aaa.run") for _, callee in calls)
