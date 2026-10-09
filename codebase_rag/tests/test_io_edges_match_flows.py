"""A function whose code makes a resource flow also reads and writes it.

The flow walk and the I/O walk disagreed for Dart and PHP: the flow walk had
dedicated paths for Dart's selector chains, PHP's `echo` / `print` keywords
and PHP's fieldless `$_GET["q"]` subscript, and the I/O walk had none. So
`ENV::K -> STDOUT` existed while no function in the project read `ENV::K` or
wrote STDOUT (issue #2761).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_ENV_K = "resource::ENV::K"
_STDOUT = "resource::STDOUT::<dynamic>"

_Edges = set[tuple[str, str, str]]


def _index(root: Path, files: dict[str, str]) -> _Edges:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="app",
        capture=resolve_capture(["io"]),
    ).run(force=True)
    return {
        (_short(str(c.args[0][2])), str(c.args[1]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) in ("READS_FROM", "WRITES_TO", "FLOWS_TO")
    }


def _short(src: str) -> str:
    if src.startswith("resource::"):
        return src
    return src.split("(", 1)[0].rsplit(".", 1)[-1]


def _io(edges: _Edges, caller: str) -> set[tuple[str, str]]:
    return {(rel, dst) for src, rel, dst in edges if src == caller}


# One env-to-stdout leak per flow language, all in a function named `f`
# (`F` for C#, whose methods are PascalCase by convention).
_LEAKS = {
    "python": ("a.py", 'import os\n\ndef f():\n    x = os.getenv("K")\n    print(x)\n'),
    "javascript": (
        "a.js",
        "function f() { const x = process.env.K; console.log(x); }\n"
        "module.exports = { f };\n",
    ),
    "go": (
        "a.go",
        'package main\n\nimport (\n\t"fmt"\n\t"os"\n)\n\n'
        'func f() { x := os.Getenv("K"); fmt.Println(x) }\n\n'
        "func main() { f() }\n",
    ),
    "java": (
        "A.java",
        'class A { void f() { String x = System.getenv("K"); '
        "System.out.println(x); } }\n",
    ),
    "rust": (
        "src/main.rs",
        'fn f() { let x = std::env::var("K").unwrap(); println!("{}", x); }\n'
        "fn main() { f(); }\n",
    ),
    "c_sharp": (
        "B.cs",
        "using System;\nclass B { void F() { "
        'var x = Environment.GetEnvironmentVariable("K"); '
        "Console.WriteLine(x); } }\n",
    ),
    "php": ("a.php", '<?php\nfunction f() { $x = getenv("K"); echo $x; }\n'),
    "dart": (
        "a.dart",
        "import 'dart:io';\n\nvoid f() {\n"
        "  final x = Platform.environment['K'];\n  print(x);\n}\n",
    ),
    "scala": (
        "S.scala",
        'object S { def f(): Unit = { val x = sys.env("K"); println(x) } }\n',
    ),
    "lua": ("a.lua", 'function f()\n  local x = os.getenv("K")\n  print(x)\nend\n'),
    "cpp": (
        "c.cpp",
        "#include <cstdlib>\n#include <iostream>\n"
        'void f() { const char* x = std::getenv("K"); std::cout << x; }\n',
    ),
    "c": (
        "d.c",
        "#include <stdlib.h>\n#include <stdio.h>\n"
        'void f(void) { char* x = getenv("K"); printf("%s", x); }\n',
    ),
}


@pytest.mark.parametrize("language", sorted(_LEAKS))
def test_every_resource_flow_has_its_io_edges(tmp_path: Path, language: str) -> None:
    rel, source = _LEAKS[language]
    edges = _index(tmp_path / language, {rel: source})
    flows = {(src, dst) for src, rel_type, dst in edges if rel_type == "FLOWS_TO"}
    assert flows == {(_ENV_K, _STDOUT)}, edges
    caller = "F" if language == "c_sharp" else "f"
    assert _io(edges, caller) == {("READS_FROM", _ENV_K), ("WRITES_TO", _STDOUT)}, edges


_DART = """\
import 'dart:io';
import 'package:http/http.dart' as http;

void dartLeak() {
  final x = Platform.environment['DART_TOKEN'];
  print(x);
}

void toStreams(String s) {
  stdout.write(s);
  stderr.writeln(s);
}

void runIt() {
  Process.run('sh', ['-c', 'ls']);
}

void fetchIt() {
  http.get(Uri.parse('https://example.com'));
}

void postIt() {
  final u = Platform.environment['U'];
  http.post(Uri.parse('https://example.com'), body: u);
}

String readInline() {
  return File('in.txt').readAsStringSync();
}

void writeBound(String s) {
  var f = File('out.txt');
  f.writeAsStringSync(s);
}

void shadowed(stdout) {
  stdout.write('x');
}

void notEnv() {
  final environment = {'K': 'v'};
  print(environment['K']);
}

final top = Platform.environment['TOP'];
"""

_PHP = """\
<?php
function phpEcho() {
    $x = getenv("PHP_TOKEN");
    echo $x;
}

function phpQuery() {
    $q = $_GET["q"];
    print $q;
}

function plainArray() {
    $arr = ["q" => 1];
    return $arr["q"];
}
"""


# A first-party library imported under the `http` prefix is not the package.
_NOT_HTTP = """\
import 'package:app/http.dart' as http;

void notHttp() {
  http.get(Uri.parse('https://example.com'));
}
"""


@pytest.fixture(scope="module")
def dart_php(tmp_path_factory: pytest.TempPathFactory) -> _Edges:
    # Module scope runs before conftest's autouse grammar skips, so a base
    # install (Python grammar only) must skip here, not build an empty graph.
    parsers, _ = load_parsers()
    for lang in ("dart", "php"):
        if lang not in parsers:
            pytest.skip(f"{lang} parser not available")
    root = tmp_path_factory.mktemp("io2761") / "app"
    return _index(
        root, {"leak.dart": _DART, "leak.php": _PHP, "not_http.dart": _NOT_HTTP}
    )


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        (
            "dartLeak",
            {("READS_FROM", "resource::ENV::DART_TOKEN"), ("WRITES_TO", _STDOUT)},
        ),
        (
            "toStreams",
            {("WRITES_TO", _STDOUT), ("WRITES_TO", "resource::STDERR::<dynamic>")},
        ),
        ("runIt", {("WRITES_TO", "resource::PROCESS::sh")}),
        ("fetchIt", {("READS_FROM", "resource::NETWORK::<dynamic>")}),
        (
            "postIt",
            {
                ("READS_FROM", "resource::ENV::U"),
                ("WRITES_TO", "resource::NETWORK::<dynamic>"),
            },
        ),
        ("readInline", {("READS_FROM", "resource::FILE::in.txt")}),
        ("writeBound", {("WRITES_TO", "resource::FILE::out.txt")}),
        (
            "phpEcho",
            {("READS_FROM", "resource::ENV::PHP_TOKEN"), ("WRITES_TO", _STDOUT)},
        ),
        ("phpQuery", {("READS_FROM", "resource::NETWORK::q"), ("WRITES_TO", _STDOUT)}),
    ],
)
def test_dart_and_php_io_edges(
    dart_php: _Edges, caller: str, expected: set[tuple[str, str]]
) -> None:
    assert _io(dart_php, caller) == expected, dart_php


def test_a_prefixed_http_import_carries_the_flow(dart_php: _Edges) -> None:
    # `import 'package:http/http.dart' as http`: both walks now see the
    # package behind its prefix, so the env value reaching `http.post` is a
    # flow as well as two I/O edges.
    flows = {(src, dst) for src, rel, dst in dart_php if rel == "FLOWS_TO"}
    assert ("resource::ENV::U", "resource::NETWORK::<dynamic>") in flows, flows


def test_look_alikes_are_not_io(dart_php: _Edges) -> None:
    # Negatives: a parameter named `stdout` is not the stream, a local map
    # named `environment` is not the process env, a PHP array subscript is
    # not `$_GET`, and module-level Dart code (which holds every function
    # body) is credited to no function.
    assert _io(dart_php, "shadowed") == set(), dart_php
    assert not [e for e in _io(dart_php, "notEnv") if e[0] == "READS_FROM"], dart_php
    assert _io(dart_php, "plainArray") == set(), dart_php
    assert _io(dart_php, "notHttp") == set(), dart_php
    assert not any(dst == "resource::ENV::TOP" for _src, _rel, dst in dart_php), (
        dart_php
    )
    assert not any(src.startswith("leak") for src, _rel, _dst in dart_php), dart_php
