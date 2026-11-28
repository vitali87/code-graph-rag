"""One very deep function costs no other function its calls (issue #3173).

The Rust, Go and C++ local-type passes walked a function body with Python
recursion, one frame per tree level. A generated `else if` chain of ~1,000
branches, or one condition of ~1,000 `||` terms, raised RecursionError, and
the call pass caught it for the WHOLE file: every call in it was dropped,
neighbours included, while the sync still reported "done".

With the I/O group on, the flow walk ran first and recursed the same way,
once per `else if` or `||` term and once per `if` nested in another's block,
so the deep caller lost every call it made (issue #3819).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from tree_sitter import Node
from typer.testing import CliRunner, Result

from codebase_rag import cli
from codebase_rag import constants as cs
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.call_processor import CallProcessor
from codebase_rag.parsers.type_inference import TypeInferenceEngine
from codebase_rag.tests.conftest import _MockIngestor, create_and_run_updater
from codebase_rag.types_defs import ASTNode, LanguageQueries

# Past Python's default recursion limit of 1,000 frames in all three grammars.
BRANCHES = 1200
TERMS = 1000

_RS_MANIFEST = '[package]\nname = "deep"\nversion = "0.1.0"\nedition = "2021"\n'
_GO_MANIFEST = "module deep\n\ngo 1.21\n"

# Two types share `run`, so only a typed local picks the right one.
_RS_TYPES = (
    "pub struct Worker {}\npub struct Idle {}\n"
    "impl Worker { pub fn run(&self) -> u32 { 1 } }\n"
    "impl Idle { pub fn run(&self) -> u32 { 0 } }\n"
)
_GO_TYPES = (
    "type Worker struct{}\ntype Idle struct{}\n\n"
    "func (w Worker) Run() int { return 1 }\nfunc (i Idle) Run() int { return 0 }\n\n"
)
_CPP_TYPES = (
    "class Worker { public: int run() { return 1; } };\n"
    "class Idle { public: int run() { return 0; } };\n"
)


def _else_if(head: str, mid: str, body: str, count: int) -> str:
    return "\n".join(
        (head if i == 0 else mid).format(i=i) + "\n        " + body.format(i=i)
        for i in range(count)
    )


def _rust(dispatch_body: str) -> dict[str, str]:
    return {
        "Cargo.toml": _RS_MANIFEST,
        "src/lib.rs": _RS_TYPES
        + "pub fn helper() -> u32 { 1 }\npub fn other() -> u32 { 2 }\n"
        + "pub fn dispatch(x: u32) -> u32 {\n    let w = Worker {};\n"
        + dispatch_body
        + "\n}\npub fn simple() -> u32 { other() }\n",
    }


def _go(dispatch_body: str) -> dict[str, str]:
    return {
        "go.mod": _GO_MANIFEST,
        "a.go": "package deep\n\n"
        + _GO_TYPES
        + "func helper() int { return 1 }\nfunc other() int { return 2 }\n\n"
        + "func dispatch(x int) int {\n    w := Worker{}\n"
        + dispatch_body
        + "\n}\n\nfunc simple() int { return other() }\n",
    }


def _cpp(dispatch_body: str) -> dict[str, str]:
    return {
        "a.cpp": _CPP_TYPES
        + "int helper() { return 1; }\nint other() { return 2; }\n"
        + "int dispatch(int x) {\n    Worker w;\n"
        + dispatch_body
        + "\n}\nint simple() { return other(); }\n",
    }


def _or_terms(count: int, operator: str = " || ") -> str:
    return operator.join(f"x == {i}" for i in range(count))


_SOURCES: dict[tuple[str, str], Callable[[], dict[str, str]]] = {
    ("rust", "else-if"): lambda: _rust(
        _else_if("    if x == {i} {{", "    }} else if x == {i} {{", "{i}", BRANCHES)
        + "\n    } else {\n        helper() + w.run()\n    }"
    ),
    ("rust", "or-chain"): lambda: _rust(
        f"    if {_or_terms(TERMS)} {{ helper() + w.run() }} else {{ 0 }}"
    ),
    ("go", "else-if"): lambda: _go(
        _else_if(
            "    if x == {i} {{", "    }} else if x == {i} {{", "return {i}", BRANCHES
        )
        + "\n    } else {\n        return helper() + w.Run()\n    }"
    ),
    ("go", "or-chain"): lambda: _go(
        f"    if {_or_terms(TERMS)} {{\n        return helper() + w.Run()\n    }}\n"
        "    return 0"
    ),
    ("cpp", "else-if"): lambda: _cpp(
        _else_if(
            "    if (x == {i}) {{",
            "    }} else if (x == {i}) {{",
            "return {i};",
            BRANCHES,
        )
        + "\n    } else {\n        return helper() + w.run();\n    }"
    ),
    ("cpp", "or-chain"): lambda: _cpp(
        f"    if ({_or_terms(TERMS)}) {{ return helper() + w.run(); }}\n    return 0;"
    ),
}

_SHALLOW: dict[str, Callable[[], dict[str, str]]] = {
    "rust": lambda: _rust("    if x == 0 { helper() + w.run() } else { 0 }"),
    "go": lambda: _go(
        "    if x == 0 {\n        return helper() + w.Run()\n    }\n    return 0"
    ),
    "cpp": lambda: _cpp(
        "    if (x == 0) { return helper() + w.run(); }\n    return 0;"
    ),
}

_WORKER_RUN = {
    "rust": "src.lib.Worker.run",
    "go": "a.Worker.Run",
    "cpp": "a.Worker.run",
}
_MODULE = {"rust": "src.lib", "go": "a", "cpp": "a"}
_FILE = {"rust": "src/lib.rs", "go": "a.go", "cpp": "a.cpp"}


def _index(
    repo: Path,
    files: dict[str, str],
    mock: MagicMock,
    capture: CaptureSelection | None = None,
) -> GraphUpdater:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return create_and_run_updater(repo, mock, capture=capture)


def _edges(
    repo: Path, mock: MagicMock, rel: cs.RelationshipType
) -> set[tuple[str, str]]:
    prefix = f"{repo.name}."
    return {
        (str(c.args[0][2]).removeprefix(prefix), str(c.args[2][2]).removeprefix(prefix))
        for c in mock.ensure_relationship_batch.call_args_list
        if c.args[1] == rel.value
    }


def _calls(repo: Path, mock: MagicMock) -> set[tuple[str, str]]:
    return _edges(repo, mock, cs.RelationshipType.CALLS)


@pytest.mark.parametrize(("lang", "shape"), list(_SOURCES), ids=str)
def test_a_deep_function_keeps_every_call_in_its_file(
    temp_repo: Path, mock_ingestor: MagicMock, lang: str, shape: str
) -> None:
    # The autouse pass-failure gate fails this test on any swallowed error.
    updater = _index(temp_repo, _SOURCES[(lang, shape)](), mock_ingestor)

    mod = _MODULE[lang]
    calls = _calls(temp_repo, mock_ingestor)
    assert (f"{mod}.simple", f"{mod}.other") in calls, calls
    assert (f"{mod}.dispatch", f"{mod}.helper") in calls, calls
    # The deep function's own locals are typed, not skipped: `w.run()` binds
    # the Worker method alone, as it does in a shallow function.
    worker_calls = {to for frm, to in calls if frm == f"{mod}.dispatch"} - {
        f"{mod}.helper"
    }
    assert worker_calls == {f"{mod}.{_WORKER_RUN[lang].removeprefix(mod + '.')}"}, calls
    assert updater.call_pass_failures == []


@pytest.mark.parametrize("lang", list(_SHALLOW))
def test_a_shallow_function_types_its_locals_alike(
    temp_repo: Path, mock_ingestor: MagicMock, lang: str
) -> None:
    # Negative control: the deep cases above must type `w` exactly as here.
    _index(temp_repo, _SHALLOW[lang](), mock_ingestor)
    mod = _MODULE[lang]
    calls = _calls(temp_repo, mock_ingestor)
    assert {to for frm, to in calls if frm == f"{mod}.dispatch"} == {
        f"{mod}.helper",
        f"{mod}.{_WORKER_RUN[lang].removeprefix(mod + '.')}",
    }, calls


def test_failed_local_types_cost_only_that_functions_typing(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    _fail_on_swallowed_pass_errors: list[str],
) -> None:
    # Whatever walker still overflows on a deeper shape is contained to its
    # function: the function's calls resolve untyped, its neighbours' stay.
    original = TypeInferenceEngine.build_local_variable_type_map

    def overflow(
        self: TypeInferenceEngine,
        caller_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        class_context: str | None = None,
    ) -> dict[str, str]:
        name = caller_node.child_by_field_name(cs.FIELD_NAME)
        if name is not None and name.text == b"dispatch":
            raise RecursionError("maximum recursion depth exceeded")
        return original(self, caller_node, module_qn, language, class_context)

    with patch.object(TypeInferenceEngine, "build_local_variable_type_map", overflow):
        updater = _index(temp_repo, _SHALLOW["rust"](), mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert ("src.lib.simple", "src.lib.other") in calls, calls
    assert ("src.lib.dispatch", "src.lib.helper") in calls, calls
    logged = _fail_on_swallowed_pass_errors
    assert len(logged) == 1, logged
    assert logged[0].startswith("Failed to infer local types in "), logged
    assert f"{temp_repo.name}.src.lib.dispatch (src/lib.rs)" in logged[0], logged
    assert updater.call_pass_failures == [temp_repo / "src/lib.rs"]
    logged.clear()


def test_a_failed_caller_costs_only_its_own_calls(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    _fail_on_swallowed_pass_errors: list[str],
) -> None:
    original = CallProcessor._ingest_function_calls

    def explode(
        self: CallProcessor,
        caller_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None = None,
        call_nodes: list[Node] | None = None,
        call_name_cache: dict[int, str | None] | None = None,
    ) -> None:
        if caller_qn.endswith(".dispatch"):
            raise RuntimeError("caller pass exploded")
        original(
            self,
            caller_node,
            caller_qn,
            caller_type,
            module_qn,
            language,
            queries,
            class_context,
            call_nodes,
            call_name_cache,
        )

    with patch.object(CallProcessor, "_ingest_function_calls", explode):
        updater = _index(temp_repo, _SHALLOW["go"](), mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert ("a.simple", "a.other") in calls, calls
    assert not {to for frm, to in calls if frm == "a.dispatch"}, calls
    logged = _fail_on_swallowed_pass_errors
    assert len(logged) == 1, logged
    # Still the prefix the suite's pass-failure gate watches (issue #1070).
    assert logged[0].startswith("Failed to process calls in "), logged
    assert f"{temp_repo.name}.a.dispatch (a.go)" in logged[0], logged
    assert updater.call_pass_failures == [temp_repo / "a.go"]
    logged.clear()


# --- the I/O group -----------------------------------------------------------

_IO = resolve_capture([cs.CaptureGroup.IO])
# The group's flow walk alone: FLOWS_TO without READS_FROM/WRITES_TO.
_FLOW = resolve_capture([f"{cs.CAPTURE_ADD_PREFIX}{cs.RelationshipType.FLOWS_TO}"])


def _js(dispatch_body: str) -> dict[str, str]:
    return {
        "a.js": "function helper() { return 1 }\nfunction other() { return 2 }\n"
        + "function dispatch(x) {\n"
        + dispatch_body
        + "\n}\nfunction simple() { return other() }\n",
    }


def _py(dispatch_body: str) -> dict[str, str]:
    return {
        "a.py": "def helper():\n    return 1\n\n\ndef other():\n    return 2\n\n\n"
        + "def dispatch(x):\n"
        + dispatch_body
        + "\n\n\ndef simple():\n    return other()\n",
    }


def _lua(dispatch_body: str) -> dict[str, str]:
    return {
        "a.lua": "function helper() return 1 end\nfunction other() return 2 end\n"
        + "function dispatch(x)\n"
        + dispatch_body
        + "\nend\nfunction simple() return other() end\n",
    }


# Each of the I/O group's flow walks: the flat one (Rust, Go, C++ and Lua,
# whose `else if` nests in an `else` block), the JS/TS one and the Python
# one, each with a condition or a value that nests one level per term.
_IO_SOURCES: dict[tuple[str, str], Callable[[], dict[str, str]]] = {
    **_SOURCES,
    ("lua", "else-if"): lambda: _lua(
        "if x == 0 then return 0\n"
        + "".join(f"else if x == {i} then return {i}\n" for i in range(1, BRANCHES))
        + "else return helper()\n"
        + "end\n" * BRANCHES
    ),
    ("javascript", "else-if"): lambda: _js(
        _else_if(
            "    if (x === {i}) {{",
            "    }} else if (x === {i}) {{",
            "return {i}",
            BRANCHES,
        )
        + "\n    } else {\n        return helper()\n    }"
    ),
    ("javascript", "or-chain"): lambda: _js(
        f"    if ({_or_terms(TERMS)}) {{ return helper() }}\n    return 0"
    ),
    ("javascript", "or-value"): lambda: _js(
        f"    const v = {_or_terms(TERMS)}\n    return v ? helper() : 0"
    ),
    ("python", "or-chain"): lambda: _py(
        f"    if {_or_terms(TERMS, ' or ')}:\n        return helper()\n    return 0"
    ),
    ("python", "or-value"): lambda: _py(
        f"    v = {_or_terms(TERMS, ' or ')}\n    return helper() if v else 0"
    ),
}
_IO_MODULE = {**_MODULE, "lua": "a", "javascript": "a", "python": "a"}
# Lua's `else if` opens a block per link, and the I/O group's access walk
# (not its flow walk) still takes a frame per nested block, so Lua's chain
# holds the flow walk alone to the run without the group.
_IO_CAPTURE = {"lua": _FLOW}


def _index_both(
    tmp_path: Path, files: dict[str, str], capture: CaptureSelection = _IO
) -> tuple[tuple[Path, _MockIngestor, GraphUpdater], tuple[Path, _MockIngestor]]:
    # The same repository, indexed once with the I/O group on and once
    # without it; the two checkouts share a name, so their qns do too.
    with_io, without = tmp_path / "io" / "deep", tmp_path / "plain" / "deep"
    io_mock, plain_mock = _MockIngestor(), _MockIngestor()
    updater = _index(with_io, files, io_mock, capture=capture)
    _index(without, files, plain_mock)
    return (with_io, io_mock, updater), (without, plain_mock)


@pytest.mark.parametrize(("lang", "shape"), list(_IO_SOURCES), ids=str)
def test_a_deep_function_keeps_its_calls_with_the_io_group_on(
    tmp_path: Path, lang: str, shape: str
) -> None:
    (with_io, io_mock, updater), (without, plain_mock) = _index_both(
        tmp_path, _IO_SOURCES[(lang, shape)](), _IO_CAPTURE.get(lang, _IO)
    )

    mod = _IO_MODULE[lang]
    expected = _calls(without, plain_mock)
    assert (f"{mod}.dispatch", f"{mod}.helper") in expected, expected
    assert _calls(with_io, io_mock) == expected
    assert updater.call_pass_failures == []


# Go's deep shapes read the environment before the chain and write what they
# read in the branch at its far end; JS and Python read it in the deepest
# operand of a value. Each must flow as its shallow version does.
_GO_IO = (
    ("package deep\n\n", 'package deep\n\nimport "os"\n\n'),
    ("    w := Worker{}\n", '    w := Worker{}\n    v := os.Getenv("DEEP")\n'),
    (
        "return helper() + w.Run()",
        'os.WriteFile("out.txt", []byte(v), 0644)\n        return helper() + w.Run()',
    ),
)


def _go_io(files: dict[str, str]) -> dict[str, str]:
    text = files["a.go"]
    for old, new in _GO_IO:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return {**files, "a.go": text}


def _js_io(terms: int) -> dict[str, str]:
    rest = "".join(f" || x === {i}" for i in range(1, terms))
    return {
        "a.js": 'const fs = require("fs")\n\nfunction dispatch(x) {\n'
        + f"    const v = process.env.DEEP{rest}\n"
        + '    fs.writeFileSync("out.txt", v)\n}\n',
    }


def _py_io(terms: int) -> dict[str, str]:
    rest = "".join(f" or x == {i}" for i in range(1, terms))
    return {
        "a.py": "import os\n\n\ndef dispatch(x):\n"
        + f'    v = os.getenv("DEEP"){rest}\n'
        + '    with open("out.txt", "w") as out:\n        out.write(v)\n',
    }


_FLOW_SOURCES: dict[tuple[str, str], tuple[Callable[[], dict[str, str]], ...]] = {
    ("go", "else-if"): (
        lambda: _go_io(_SOURCES[("go", "else-if")]()),
        lambda: _go_io(_SHALLOW["go"]()),
    ),
    ("go", "or-chain"): (
        lambda: _go_io(_SOURCES[("go", "or-chain")]()),
        lambda: _go_io(_SHALLOW["go"]()),
    ),
    ("javascript", "or-value"): (lambda: _js_io(TERMS), lambda: _js_io(2)),
    ("python", "or-value"): (lambda: _py_io(TERMS), lambda: _py_io(2)),
}


@pytest.mark.parametrize(("lang", "shape"), list(_FLOW_SOURCES), ids=str)
def test_a_deep_function_flows_as_a_shallow_one_does(
    tmp_path: Path, lang: str, shape: str
) -> None:
    deep_source, shallow_source = _FLOW_SOURCES[(lang, shape)]
    (deep, deep_mock, updater), (without, plain_mock) = _index_both(
        tmp_path / "deep", deep_source()
    )
    shallow, shallow_mock = tmp_path / "shallow" / "deep", _MockIngestor()
    _index(shallow, shallow_source(), shallow_mock, capture=_IO)

    flows = _edges(shallow, shallow_mock, cs.RelationshipType.FLOWS_TO)
    assert ("resource::ENV::DEEP", "resource::FILE::out.txt") in flows, flows
    assert _edges(deep, deep_mock, cs.RelationshipType.FLOWS_TO) == flows
    assert _calls(deep, deep_mock) == _calls(without, plain_mock)
    assert updater.call_pass_failures == []


# --- nested ifs ---------------------------------------------------------------

# Each `if` in the block of another took three or four frames of the flow walk,
# so this many levels passed Python's default limit of 1,000 frames.
DEPTH = 400


class _Nested(NamedTuple):
    # A repository whose `dispatch(x)` has `body` for its body, beside
    # `helper`, `other` and `simple`; one level of nesting, opened and closed;
    # and what the innermost level runs: an environment read written to
    # stdout, then the call every run must keep.
    files: Callable[[str], dict[str, str]]
    opens: str
    closes: str
    inner: str

    def source(self, depth: int) -> dict[str, str]:
        return self.files(
            "".join(self.opens.format(i=i) + "\n" for i in range(depth))
            + f"{self.inner}\n"
            + f"{self.closes}\n" * depth
        )


def _c_family(
    path: str, header: str, fn: str, dispatch: str, io: str, footer: str = ""
) -> _Nested:
    # `fn` is a no-argument signature for {name}, `dispatch` the deep one's.
    def files(body: str) -> dict[str, str]:
        return {
            path: header
            + f"{fn.format(name='helper')} {{ return 1; }}\n"
            + f"{fn.format(name='other')} {{ return 2; }}\n"
            + f"{dispatch} {{\n{body}\n    return 0;\n}}\n"
            + f"{fn.format(name='simple')} {{ return other(); }}\n"
            + footer
        }

    x = "$x" if path.endswith(".php") else "x"
    return _Nested(files, f"if ({x} == {{i}}) {{{{", "}", f"{io}\nreturn helper();")


_NESTED_LANGS: dict[str, _Nested] = {
    "go": _Nested(
        lambda body: {
            "go.mod": _GO_MANIFEST,
            "a.go": 'package deep\n\nimport (\n\t"fmt"\n\t"os"\n)\n\n'
            + "func helper() int { return 1 }\nfunc other() int { return 2 }\n\n"
            + f"func dispatch(x int) int {{\n{body}\n\treturn 0\n}}\n\n"
            + "func simple() int { return other() }\n",
        },
        "if x == {i} {{",
        "}",
        'fmt.Println(os.Getenv("DEEP"))\nreturn helper()',
    ),
    "rust": _Nested(
        lambda body: {
            "Cargo.toml": _RS_MANIFEST,
            "src/lib.rs": "pub fn helper() -> u32 { 1 }\npub fn other() -> u32 { 2 }\n"
            + f"pub fn dispatch(x: u32) -> u32 {{\n{body}\n    0\n}}\n"
            + "pub fn simple() -> u32 { other() }\n",
        },
        "if x == {i} {{",
        "}",
        'println!("{:?}", std::env::var("DEEP"));\nreturn helper();',
    ),
    "cpp": _c_family(
        "a.cpp",
        "#include <cstdio>\n#include <cstdlib>\n",
        "int {name}()",
        "int dispatch(int x)",
        'printf("%s", std::getenv("DEEP"));',
    ),
    "c": _c_family(
        "a.c",
        "#include <stdio.h>\n#include <stdlib.h>\n",
        "int {name}(void)",
        "int dispatch(int x)",
        'printf("%s", getenv("DEEP"));',
    ),
    "java": _c_family(
        "A.java",
        "class A {\n",
        "static int {name}()",
        "static int dispatch(int x)",
        'System.out.println(System.getenv("DEEP"));',
        "}\n",
    ),
    "c_sharp": _c_family(
        "A.cs",
        "using System;\nclass A {\n",
        "static int {name}()",
        "static int dispatch(int x)",
        'Console.WriteLine(Environment.GetEnvironmentVariable("DEEP"));',
        "}\n",
    ),
    "javascript": _c_family(
        "a.js",
        "",
        "function {name}()",
        "function dispatch(x)",
        "console.log(process.env.DEEP);",
    ),
    "typescript": _c_family(
        "a.ts",
        "",
        "export function {name}(): number",
        "export function dispatch(x: number): number",
        "console.log(process.env.DEEP);",
    ),
    "php": _c_family(
        "a.php",
        "<?php\n",
        "function {name}()",
        "function dispatch($x)",
        "echo getenv('DEEP');",
    ),
    "scala": _Nested(
        lambda body: {
            "A.scala": "object A {\n  def helper(): Int = 1\n  def other(): Int = 2\n"
            + f"  def dispatch(x: Int): Int = {{\n{body}\n    0\n  }}\n"
            + "  def simple(): Int = other()\n}\n",
        },
        "if (x == {i}) {{",
        "}",
        'println(System.getenv("DEEP"))\nreturn helper()',
    ),
    "dart": _c_family(
        "a.dart",
        "import 'dart:io';\n",
        "int {name}()",
        "int dispatch(int x)",
        "print(Platform.environment['DEEP']);",
    ),
    "lua": _Nested(
        lambda body: _lua(f"{body}\nreturn 0"),
        "if x == {i} then",
        "end",
        'print(os.getenv("DEEP"))\nreturn helper()',
    ),
}


def _py_nested(depth: int) -> dict[str, str]:
    # Python nests by indentation: one more column per level.
    ifs = "".join(" " * (4 + i) + f"if x == {i}:\n" for i in range(depth))
    inner = " " * (4 + depth)
    body = f'{ifs}{inner}print(os.getenv("DEEP"))\n{inner}return helper()\n    return 0'
    return {"a.py": "import os\n\n\n" + _py(body)["a.py"]}


_NESTED: dict[str, Callable[[int], dict[str, str]]] = {
    **{name: lang.source for name, lang in _NESTED_LANGS.items()},
    "python": _py_nested,
}


@pytest.mark.parametrize("lang", list(_NESTED))
def test_deeply_nested_ifs_keep_their_calls_and_flow_as_one_if_does(
    tmp_path: Path, lang: str
) -> None:
    # The group's access walk takes a frame per nested block too (issue
    # #3825), so these runs hold its flow walk alone.
    (deep, deep_mock, updater), (without, plain_mock) = _index_both(
        tmp_path / "deep", _NESTED[lang](DEPTH), _FLOW
    )
    shallow, shallow_mock = tmp_path / "shallow" / "deep", _MockIngestor()
    _index(shallow, _NESTED[lang](1), shallow_mock, capture=_FLOW)

    expected = _calls(without, plain_mock)
    assert any(
        "dispatch" in frm.lower() and "helper" in to.lower() for frm, to in expected
    ), expected
    assert _calls(deep, deep_mock) == expected
    assert updater.call_pass_failures == []
    flows = _edges(shallow, shallow_mock, cs.RelationshipType.FLOWS_TO)
    assert any(
        src.startswith("resource::ENV::") and dst.startswith("resource::STDOUT::")
        for src, dst in flows
    ), flows
    assert _edges(deep, deep_mock, cs.RelationshipType.FLOWS_TO) == flows


# --- the sync summary ---------------------------------------------------------


@pytest.fixture(autouse=False)
def _wide(monkeypatch: pytest.MonkeyPatch) -> None:
    # One line per message whatever the tmp path's length.
    monkeypatch.setattr(cli.app_context, "console", Console(width=1000))


def _start(repo: Path, failed: list[Path]) -> Result:
    def build(**kwargs: str) -> MagicMock:
        updater = MagicMock()
        updater.project_name = kwargs["project_name"]
        updater.skipped_because_in_sync = False
        updater.call_pass_failures = failed
        return updater

    connection = MagicMock()
    connection.__enter__.return_value = MagicMock()
    connection.__exit__.return_value = False
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=connection),
        patch("codebase_rag.graph_updater.GraphUpdater", side_effect=build),
        patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        return CliRunner().invoke(
            app,
            [
                "start",
                "--no-start-stack",
                "--repo-path",
                str(repo),
                "--project-name",
                "deep",
                "--update-graph",
            ],
        )


@pytest.mark.usefixtures("_wide")
def test_the_sync_summary_counts_files_whose_call_pass_failed(tmp_path: Path) -> None:
    result = _start(tmp_path, [tmp_path / "src/lib.rs", tmp_path / "go/a.go"])

    assert result.exit_code == 0, result.output
    assert cs.CLI_MSG_SYNC_DONE.split("{")[0] in result.output, result.output
    warning = cs.CLI_MSG_CALL_PASS_FAILURES.format(count=2, files="go/a.go, src/lib.rs")
    assert warning in result.output, result.output


@pytest.mark.usefixtures("_wide")
def test_a_clean_sync_prints_no_call_pass_warning(tmp_path: Path) -> None:
    result = _start(tmp_path, [])

    assert result.exit_code == 0, result.output
    assert cs.CLI_MSG_SYNC_DONE.split("{")[0] in result.output, result.output
    assert "Call analysis failed" not in result.output, result.output


@pytest.mark.usefixtures("_wide")
def test_a_long_failure_list_is_capped(tmp_path: Path) -> None:
    failed = [tmp_path / f"gen/f{i:02}.rs" for i in range(13)]
    result = _start(tmp_path, failed)

    assert result.exit_code == 0, result.output
    shown = ", ".join(f"gen/f{i:02}.rs" for i in range(cs.CLI_CALL_PASS_FAILURES_SHOWN))
    files = f"{shown}, " + cs.CLI_MSG_AND_MORE.format(count=3)
    assert cs.CLI_MSG_CALL_PASS_FAILURES.format(count=13, files=files) in (
        result.output
    ), result.output


def test_a_rerun_does_not_report_the_previous_runs_failures(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    _fail_on_swallowed_pass_errors: list[str],
) -> None:
    # Negative: the list is per run. A reused updater (watch mode) whose next
    # run finds the repo in sync walks no calls and has nothing to report.
    original = TypeInferenceEngine.build_local_variable_type_map

    def overflow(
        self: TypeInferenceEngine,
        caller_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        class_context: str | None = None,
    ) -> dict[str, str]:
        raise RecursionError("maximum recursion depth exceeded")

    with patch.object(TypeInferenceEngine, "build_local_variable_type_map", overflow):
        updater = _index(temp_repo, _SHALLOW["cpp"](), mock_ingestor)
    assert updater.call_pass_failures == [temp_repo / "a.cpp"]
    _fail_on_swallowed_pass_errors.clear()

    with patch.object(TypeInferenceEngine, "build_local_variable_type_map", original):
        updater.run()
    assert updater.skipped_because_in_sync
    assert updater.call_pass_failures == []
