# A Dart method passed as a tear-off (`Timer(d, _tick)`,
# `onPressed: _handleTap`, `controller.addListener(_update)`) is invoked by
# the receiving framework, never by first-party code, so dead-code flagged
# every callback handler: 129 of the wonderous app's remaining candidates
# were `_handleX` tear-offs. Mirror the C# method-group treatment: record the
# pass as a REFERENCES edge from the passing scope.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

REFERENCES = cs.RelationshipType.REFERENCES.value


def _run_rels(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
    if "dart" not in parsers:
        pytest.skip("dart parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
    }


def _has(
    rels: set[tuple[str, str, str]], caller_suffix: str, rel: str, callee_suffix: str
) -> bool:
    return any(
        a.endswith(caller_suffix) and r == rel and b.endswith(callee_suffix)
        for a, r, b in rels
    )


def test_positional_tearoff_to_external_callee_is_referenced(
    tmp_path: Path,
) -> None:
    files = {
        "app.dart": (
            "class Controller {\n"
            "  void tick() {}\n"
            "  void unused() {}\n"
            "  void start() {\n"
            "    schedule(tick);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, ".Controller.start", REFERENCES, ".Controller.tick"), rels
    assert not _has(rels, ".Controller.start", REFERENCES, ".Controller.unused")


def test_named_argument_tearoff_is_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Panel {\n"
            "  void handleTap() {}\n"
            "  void build() {\n"
            "    render(onPressed: handleTap, count: 3);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, ".Panel.build", REFERENCES, ".Panel.handleTap"), rels


def test_receiver_call_tearoff_is_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Watcher {\n"
            "  void update() {}\n"
            "  void attach(dynamic controller) {\n"
            "    controller.addListener(update);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, ".Watcher.attach", REFERENCES, ".Watcher.update"), rels
