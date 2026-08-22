"""Lean parameter-slot extraction for Lua and Scala (issue #1365). Every other
lean language got slots in #1195/#1169; these two fell through to an empty list,
so no parameter was ever seeded and BOTH composition directions stayed inert:
argument-into-callee-sink (#1169) and the pass-through return (#1363)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])
_ENV = "resource::ENV::SECRET"
_STDOUT = "resource::STDOUT::<dynamic>"
_FILENAMES = {"lua": "m.lua", "scala": "M.scala"}


def _flows(tmp_path: Path, language: str, source: str) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"parser not available: {language}")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / _FILENAMES[language]).write_text(source, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }


def test_lua_argument_reaches_a_sink_inside_the_callee(tmp_path: Path) -> None:
    source = (
        "local function logIt(msg) print(msg) end\n"
        'local function caller() local s = os.getenv("SECRET") logIt(s) end\n'
    )
    assert (_ENV, _STDOUT) in _flows(tmp_path, "lua", source)


def test_lua_maps_the_argument_to_the_right_slot(tmp_path: Path) -> None:
    # The secret is the SECOND argument, and only the second parameter is
    # printed: a compacting or shifted slot list would fabricate the edge.
    leaks = (
        "local function logIt(tag, msg) print(msg) end\n"
        'local function caller() local s = os.getenv("SECRET") logIt("app", s) end\n'
    )
    clean = (
        "local function logIt(tag, msg) print(tag) end\n"
        'local function caller() local s = os.getenv("SECRET") logIt("app", s) end\n'
    )
    assert (_ENV, _STDOUT) in _flows(tmp_path / "a", "lua", leaks)
    assert (_ENV, _STDOUT) not in _flows(tmp_path / "b", "lua", clean)


def test_lua_passthrough_return_carries_taint(tmp_path: Path) -> None:
    source = (
        "local function forward(v) return v end\n"
        'local function caller() local s = os.getenv("SECRET") print(forward(s)) end\n'
    )
    assert (_ENV, _STDOUT) in _flows(tmp_path, "lua", source)


def test_scala_argument_reaches_a_sink_inside_the_callee(tmp_path: Path) -> None:
    source = (
        "object M {\n"
        "  def logIt(msg: String): Unit = println(msg)\n"
        "  def caller(): Unit = {\n"
        '    val s = System.getenv("SECRET")\n'
        "    logIt(s)\n"
        "  }\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) in _flows(tmp_path, "scala", source)


def test_scala_maps_the_argument_to_the_right_slot(tmp_path: Path) -> None:
    leaks = (
        "object M {\n"
        "  def logIt(tag: String, msg: String): Unit = println(msg)\n"
        "  def caller(): Unit = {\n"
        '    val s = System.getenv("SECRET")\n'
        '    logIt("app", s)\n'
        "  }\n"
        "}\n"
    )
    clean = (
        "object M {\n"
        "  def logIt(tag: String, msg: String): Unit = println(tag)\n"
        "  def caller(): Unit = {\n"
        '    val s = System.getenv("SECRET")\n'
        '    logIt("app", s)\n'
        "  }\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) in _flows(tmp_path / "a", "scala", leaks)
    assert (_ENV, _STDOUT) not in _flows(tmp_path / "b", "scala", clean)


def test_scala_passthrough_return_carries_taint(tmp_path: Path) -> None:
    source = (
        "object M {\n"
        "  def forward(v: String): String = v\n"
        "  def caller(): Unit = {\n"
        '    val s = System.getenv("SECRET")\n'
        "    println(forward(s))\n"
        "  }\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) in _flows(tmp_path, "scala", source)


def test_a_scala_body_that_derives_an_unrelated_value_stays_clean(
    tmp_path: Path,
) -> None:
    # A def's body IS its value, but `v.length` is a LENGTH, not the secret.
    # Treating any body mentioning a parameter as a pass-through is the false
    # positive this rule could introduce.
    source = (
        "object M {\n"
        "  def size(v: String): Int = v.length\n"
        "  def caller(): Unit = {\n"
        '    val s = System.getenv("SECRET")\n'
        "    println(size(s))\n"
        "  }\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) not in _flows(tmp_path, "scala", source)


def test_a_scala_block_ending_in_a_definition_returns_nothing(tmp_path: Path) -> None:
    # A block whose last child is a `val` yields Unit, so the caller's binding
    # must not inherit the secret. The helper writes nowhere, so a bogus return
    # value is the only thing that could produce an edge.
    source = (
        "object M {\n"
        "  def hold(v: String): Unit = { val kept = v }\n"
        "  def caller(): Unit = {\n"
        '    val s = System.getenv("SECRET")\n'
        "    println(hold(s))\n"
        "  }\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) not in _flows(tmp_path, "scala", source)


def test_lua_taint_passed_into_the_vararg_slot_does_not_propagate(
    tmp_path: Path,
) -> None:
    # `...` takes a slot but binds no name, so a secret landing in it seeds
    # nothing and the flow stops. Asserting the real behaviour rather than the
    # behaviour I first assumed: Lua's vararg is always LAST, so it can never
    # displace a named parameter, and the shift this slot guards against in
    # other languages is unreachable here. The limitation is the propagation,
    # not the mapping.
    source = (
        "local function logIt(tag, ...) print(...) end\n"
        'local function caller() local s = os.getenv("SECRET") logIt("app", s) end\n'
    )
    assert (_ENV, _STDOUT) not in _flows(tmp_path, "lua", source)


def test_lua_named_parameter_before_a_vararg_still_maps(tmp_path: Path) -> None:
    # The named parameters preceding `...` must still resolve by position.
    source = (
        "local function logIt(msg, ...) print(msg) end\n"
        'local function caller() local s = os.getenv("SECRET") logIt(s) end\n'
    )
    assert (_ENV, _STDOUT) in _flows(tmp_path, "lua", source)
