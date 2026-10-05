"""I/O inside an inline callback is credited to the function that writes it.

The lean (non-Python) walks pruned every nested callable as "its own caller,
walked separately", but an inline callback (`items.forEach(x =>
console.log(x))`, a Go func literal, a Java/C# lambda, a Rust closure) never
gets a caller pass of its own. Its reads and writes were recorded nowhere,
and no flow went through it, while a Python lambda's were credited to the
enclosing function (issue #2772).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_STDOUT = "resource::STDOUT::<dynamic>"
_SECRET = "resource::ENV::SECRET"

_FILES = {
    "cb.js": """\
const fs = require("fs");

function jsCb(items) {
  items.forEach((x) => console.log(x));
}

function jsRead(p) {
  fs.readFile(p, (err, data) => console.log(data));
}

function jsLeak(items) {
  const s = process.env.SECRET;
  items.forEach(function (x) { console.log(s); });
}

const handler = (req) => { console.log(req); };

function outer() {
  function inner() { console.log("inner"); }
  return inner;
}

function shadow(items) {
  items.forEach((console) => console.log(1));
}

function pick(items) {
  items.forEach((x) => { return process.env.TOKEN; });
  return 1;
}

function usePick(items) {
  console.log(pick(items));
}

function shadowTaint(items) {
  const x = process.env.OTHER;
  items.forEach((x) => console.error(x));
}

module.exports = { jsCb, jsRead, jsLeak, handler, outer, shadow, usePick, shadowTaint };
""",
    "cb.ts": """\
export function tsCb(items: string[]) {
  items.forEach((x) => console.log(x));
}
""",
    "cb.go": """\
package main

import (
\t"fmt"
\t"os"
)

func goCb(items []string) {
\tfunc() { fmt.Println(items) }()
\tgo func() { fmt.Println("bg") }()
}

func goLeak() {
\ts := os.Getenv("SECRET")
\tfunc() { fmt.Println(s) }()
}

func main() { goCb(nil); goLeak() }
""",
    "Cb.java": """\
import java.util.List;

class Cb {
  static void javaCb(List<String> items) {
    items.forEach(x -> System.out.println(x));
  }

  static void javaLeak(List<String> items) {
    String s = System.getenv("SECRET");
    items.forEach(x -> { System.out.println(s); });
  }
}
""",
    "Cb.cs": """\
using System;
using System.Collections.Generic;

class Cb {
  static void CsCb(List<string> items) {
    items.ForEach(x => Console.WriteLine(x));
  }
}
""",
    "src/main.rs": """\
fn rust_cb(items: Vec<String>) {
    items.iter().for_each(|x| println!("{}", x));
}

fn rust_leak(items: Vec<String>) {
    let s = std::env::var("SECRET").unwrap();
    items.iter().for_each(|_x| println!("{}", s));
}

fn main() { rust_cb(vec![]); rust_leak(vec![]); }
""",
}


@pytest.fixture(scope="module")
def edges(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
    # Module scope runs before conftest's autouse grammar skips, so a base
    # install (Python grammar only) must skip here, not build an empty graph.
    for lang in ("javascript", "typescript", "go", "java", "c_sharp", "rust"):
        if lang not in parsers:
            pytest.skip(f"{lang} parser not available")
    root = tmp_path_factory.mktemp("io2772") / "cb"
    for rel, text in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="cb",
        capture=resolve_capture(["io"]),
    ).run(force=True)
    return {
        (_short(str(c.args[0][2])), str(c.args[1]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) in ("READS_FROM", "WRITES_TO", "FLOWS_TO")
    }


def _short(src: str) -> str:
    # A function or method by its own name (`CsCb(List)` -> `CsCb`); a
    # resource, the source of a resource flow, as it is.
    if src.startswith("resource::"):
        return src
    return src.split("(", 1)[0].rsplit(".", 1)[-1]


def _io(edges: set[tuple[str, str, str]], caller: str) -> set[tuple[str, str]]:
    return {(rel, dst) for src, rel, dst in edges if src == caller}


def _flows(edges: set[tuple[str, str, str]]) -> set[tuple[str, str]]:
    return {(src, dst) for src, rel, dst in edges if rel == "FLOWS_TO"}


@pytest.mark.parametrize(
    "caller",
    ["jsCb", "tsCb", "goCb", "javaCb", "CsCb", "rust_cb"],
)
def test_a_callback_write_lands_on_its_enclosing_function(
    edges: set[tuple[str, str, str]], caller: str
) -> None:
    assert ("WRITES_TO", _STDOUT) in _io(edges, caller), edges


def test_a_read_callback_keeps_both_edges(edges: set[tuple[str, str, str]]) -> None:
    assert _io(edges, "jsRead") == {
        ("READS_FROM", "resource::FILE::<dynamic>"),
        ("WRITES_TO", _STDOUT),
    }, edges


@pytest.mark.parametrize("caller", ["jsLeak", "goLeak", "javaLeak", "rust_leak"])
def test_a_secret_flows_through_a_callback(
    edges: set[tuple[str, str, str]], caller: str
) -> None:
    assert ("READS_FROM", _SECRET) in _io(edges, caller), edges
    assert ("WRITES_TO", _STDOUT) in _io(edges, caller), edges
    assert (_SECRET, _STDOUT) in _flows(edges), edges


def test_nested_definitions_keep_their_own_io(
    edges: set[tuple[str, str, str]],
) -> None:
    # Negatives: a named arrow and a nested function declaration are walked
    # as callers of their own, so their writes are not also credited to the
    # module or the enclosing function.
    assert _io(edges, "handler") == {("WRITES_TO", _STDOUT)}, edges
    assert _io(edges, "inner") == {("WRITES_TO", _STDOUT)}, edges
    assert _io(edges, "outer") == set(), edges
    assert _io(edges, "cb") == set(), edges


def test_a_callback_parameter_shadows_the_outer_name(
    edges: set[tuple[str, str, str]],
) -> None:
    # Negatives: a parameter named `console` is not the console, and a
    # parameter named like a tainted outer local is not that local.
    assert _io(edges, "shadow") == set(), edges
    assert ("resource::ENV::OTHER", "resource::STDERR::<dynamic>") not in _flows(
        edges
    ), edges


def test_a_return_in_a_callback_does_not_return_from_the_caller(
    edges: set[tuple[str, str, str]],
) -> None:
    # Negative: `pick` returns 1; the env read is the callback's return.
    assert ("resource::ENV::TOKEN", _STDOUT) not in _flows(edges), edges
