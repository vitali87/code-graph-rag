# A Dart getter access is an attribute read, not an invocation: no CALLS
# edge can ever land on one, so dead-code flagged every read-only getter
# (roughly 15 of the wonderous app's ~20 residual candidates: _enableVideo,
# _artifactRoute, startYr/endYr and kin). Mirror the C# property-read
# design: mark getter_signature methods is_property and emit REFERENCES
# edges for bare and receiver-position reads (issue #869).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

REFERENCES = cs.RelationshipType.REFERENCES.value


def _run(tmp_path: Path, files: dict[str, str]) -> MagicMock:
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
    return mock


def _rels(mock: MagicMock) -> set[tuple[str, str, str]]:
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


def test_getter_is_marked_is_property(tmp_path: Path) -> None:
    mock = _run(
        tmp_path,
        {"m.dart": "class Money {\n  bool get enabled => true;\n}\n"},
    )
    props: dict[str, dict] = {}
    for c in mock.ensure_node_batch.call_args_list:
        if c.args[0] == cs.NodeLabel.METHOD:
            props.setdefault(c.args[1][cs.KEY_QUALIFIED_NAME], {}).update(c.args[1])
    enabled = next(v for k, v in props.items() if k.endswith("Money.enabled"))
    assert enabled.get(cs.KEY_IS_PROPERTY) is True, enabled


def test_bare_getter_read_is_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Player {\n"
            "  bool get enableVideo => true;\n"
            "  bool get unusedFlag => false;\n"
            "  void start() {\n"
            "    if (enableVideo) {\n"
            "      run();\n"
            "    }\n"
            "  }\n"
            "  void run() {}\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Player.start", REFERENCES, ".Player.enableVideo"), rels
    assert not _has(rels, ".Player.start", REFERENCES, ".Player.unusedFlag")


def test_receiver_getter_read_is_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Marker {\n"
            "  int get startYr => 1900;\n"
            "}\n"
            "\n"
            "class Timeline {\n"
            "  void draw(Marker marker) {\n"
            "    print(marker.startYr);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Timeline.draw", REFERENCES, ".Marker.startYr"), rels


def test_this_qualified_getter_read_is_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Gauge {\n"
            "  int get level => 1;\n"
            "  void show() {\n"
            "    print(this.level);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Gauge.show", REFERENCES, ".Gauge.level"), rels


def test_local_shadow_suppresses_bare_read(tmp_path: Path) -> None:
    # A local (or parameter) named like the getter hides it for bare reads;
    # emitting an edge here would fabricate liveness for a dead getter.
    files = {
        "app.dart": (
            "class Cart {\n"
            "  int get total => 3;\n"
            "  void tally(int total) {\n"
            "    print(total);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert not _has(rels, ".Cart.tally", REFERENCES, ".Cart.total"), rels


def test_cascade_getter_read_is_referenced(tmp_path: Path) -> None:
    # `marker..startYr` reads through a cascade_section, not the ordinary
    # selector chain; a cascade holding an argument_part is an invocation
    # the call pass owns, and a cascade WRITE (`..endYr = 5`) targets the
    # setter, so neither may fabricate a getter read.
    files = {
        "app.dart": (
            "class Marker {\n"
            "  int get startYr => 1900;\n"
            "  int get endYr => 2000;\n"
            "  void refresh() {}\n"
            "}\n"
            "\n"
            "class Board {\n"
            "  void ping(Marker marker) {\n"
            "    marker..startYr;\n"
            "    marker..refresh();\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Board.ping", REFERENCES, ".Marker.startYr"), rels
    assert not _has(rels, ".Board.ping", REFERENCES, ".Marker.refresh"), rels


def test_closure_shadow_does_not_suppress_outer_read(tmp_path: Path) -> None:
    # A closure's parameter named like the getter shadows it only INSIDE the
    # closure: the enclosing method's own bare read still resolves to the
    # getter and must be referenced, while the closure-internal read of the
    # parameter must not be.
    files = {
        "app.dart": (
            "class Cart {\n"
            "  int get total => 3;\n"
            "  int get untouched => 4;\n"
            "  void tally() {\n"
            "    run((int total) {\n"
            "      print(total);\n"
            "      print(untouched);\n"
            "    });\n"
            "    print(total);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Cart.tally", REFERENCES, ".Cart.total"), rels
    assert _has(rels, ".Cart.tally", REFERENCES, ".Cart.untouched"), rels


def test_getter_call_chain_is_not_double_counted(tmp_path: Path) -> None:
    # `other.total()` is an invocation the call pass already resolves; the
    # read pass must not add a REFERENCES edge for the same chain, or every
    # method call would double as a phantom property read.
    files = {
        "app.dart": (
            "class Engine {\n"
            "  void ignite() {}\n"
            "  void fire(Engine other) {\n"
            "    other.ignite();\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert not _has(rels, ".Engine.fire", REFERENCES, ".Engine.ignite"), rels
