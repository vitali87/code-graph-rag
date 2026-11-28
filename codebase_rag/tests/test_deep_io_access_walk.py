"""A deep function keeps its calls with the I/O group on (issue #3825).

The I/O access walk of every language but Python took three Python frames per
nested block, so a block nested a few hundred deep raised RecursionError. The
call pass caught it for the caller, which lost every call it made. Lua's
`else if` opens a block per link, so a generated chain of ~1,000 branches
nests as deep as it is long. The walk's check of a client receiver took a
frame per `||` term, and the EXPOSES pass's walk of a Go function wiring a
connect handler one per tree level, with the same result.

The group's flow walk still recurses on some of these shapes (issue #3819), so
these runs turn on every edge of the group but FLOWS_TO: what they hold is the
other passes'.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.conftest import _MockIngestor, create_and_run_updater

# Past Python's default recursion limit of 1,000 frames: a chain of this many
# links, blocks nested this deep at three frames per block, or a condition or
# a receiver of this many terms.
BRANCHES = 1200
DEPTH = 400
TERMS = 1000

_IO_ACCESS = resolve_capture(
    [cs.CaptureGroup.IO, f"{cs.CAPTURE_DROP_PREFIX}{cs.RelationshipType.FLOWS_TO}"]
)
_IO_RELS = (cs.RelationshipType.READS_FROM, cs.RelationshipType.WRITES_TO)


class _Lang(NamedTuple):
    files: Callable[[str], dict[str, str]]
    # What the deepest branch runs: an environment read, written to stdout,
    # then the call every run must keep.
    io: str
    ret: str
    # One level of nesting, opened and closed.
    opens: str
    closes: str


def _c_like(
    name: str, header: str, fn: str, dispatch: str, io: str, footer: str = ""
) -> _Lang:
    # A C-family file: `fn` is a no-argument signature for {name}, `dispatch`
    # the deep function's, which takes `x`.
    def files(body: str) -> dict[str, str]:
        return {
            name: header
            + fn.format(name="helper")
            + " { return 1; }\n"
            + fn.format(name="other")
            + " { return 2; }\n"
            + dispatch
            + " {\n"
            + body
            + "\n    return 0;\n}\n"
            + fn.format(name="simple")
            + " { return other(); }\n"
            + footer
        }

    return _Lang(files, io, "return helper();", "if (x == {i}) {{", "}")


_LANGS: dict[str, _Lang] = {
    "go": _Lang(
        lambda body: {
            "go.mod": "module deep\n\ngo 1.21\n",
            "a.go": 'package deep\n\nimport (\n\t"fmt"\n\t"os"\n)\n\n'
            + "func helper() int { return 1 }\nfunc other() int { return 2 }\n\n"
            + "func dispatch(x int) int {\n"
            + body
            + "\n\treturn 0\n}\n\nfunc simple() int { return other() }\n",
        },
        'fmt.Println(os.Getenv("DEEP"))',
        "return helper()",
        "if x == {i} {{",
        "}",
    ),
    "rust": _Lang(
        lambda body: {
            "Cargo.toml": '[package]\nname = "deep"\nversion = "0.1.0"\n'
            'edition = "2021"\n',
            "src/lib.rs": "pub fn helper() -> u32 { 1 }\npub fn other() -> u32 { 2 }\n"
            + "pub fn dispatch(x: u32) -> u32 {\n"
            + body
            + "\n    0\n}\npub fn simple() -> u32 { other() }\n",
        },
        'println!("{:?}", std::env::var("DEEP"));',
        "return helper();",
        "if x == {i} {{",
        "}",
    ),
    "cpp": _c_like(
        "a.cpp",
        "#include <cstdio>\n#include <cstdlib>\n",
        "int {name}()",
        "int dispatch(int x)",
        'printf("%s", std::getenv("DEEP"));',
    ),
    "c": _c_like(
        "a.c",
        "#include <stdio.h>\n#include <stdlib.h>\n",
        "int {name}(void)",
        "int dispatch(int x)",
        'printf("%s", getenv("DEEP"));',
    ),
    "java": _c_like(
        "A.java",
        "class A {\n",
        "static int {name}()",
        "static int dispatch(int x)",
        'System.out.println(System.getenv("DEEP"));',
        "}\n",
    ),
    "c_sharp": _c_like(
        "A.cs",
        "using System;\nclass A {\n",
        "static int {name}()",
        "static int dispatch(int x)",
        'Console.WriteLine(Environment.GetEnvironmentVariable("DEEP"));',
        "}\n",
    ),
    "javascript": _c_like(
        "a.js",
        "",
        "function {name}()",
        "function dispatch(x)",
        "console.log(process.env.DEEP);",
    ),
    "typescript": _c_like(
        "a.ts",
        "",
        "export function {name}(): number",
        "export function dispatch(x: number): number",
        "console.log(process.env.DEEP);",
    ),
    "php": _Lang(
        lambda body: {
            "a.php": "<?php\nfunction helper() { return 1; }\n"
            + "function other() { return 2; }\nfunction dispatch($x) {\n"
            + body
            + "\n    return 0;\n}\nfunction simple() { return other(); }\n",
        },
        "echo getenv('DEEP');",
        "return helper();",
        "if ($x == {i}) {{",
        "}",
    ),
    "scala": _Lang(
        lambda body: {
            "A.scala": "object A {\n  def helper(): Int = 1\n  def other(): Int = 2\n"
            + "  def dispatch(x: Int): Int = {\n"
            + body
            + "\n    0\n  }\n  def simple(): Int = other()\n}\n",
        },
        'println(System.getenv("DEEP"))',
        "return helper()",
        "if (x == {i}) {{",
        "}",
    ),
    "dart": _c_like(
        "a.dart",
        "import 'dart:io';\n",
        "int {name}()",
        "int dispatch(int x)",
        "print(Platform.environment['DEEP']);",
    ),
    "lua": _Lang(
        lambda body: {
            "a.lua": "function helper() return 1 end\nfunction other() return 2 end\n"
            + "function dispatch(x)\n"
            + body
            + "\n    return 0\nend\nfunction simple() return other() end\n",
        },
        'print(os.getenv("DEEP"))',
        "return helper()",
        "if x == {i} then",
        "end",
    ),
}


def _nested(lang: _Lang, depth: int) -> dict[str, str]:
    # The I/O and the call sit in the innermost of `depth` nested blocks.
    return lang.files(
        "".join(lang.opens.format(i=i) + "\n" for i in range(depth))
        + f"{lang.io}\n{lang.ret}\n"
        + f"{lang.closes}\n" * depth
    )


def _lua_else_if(branches: int) -> dict[str, str]:
    # `else if`, not `elseif`: each link is an `if` in the block of the
    # previous one's `else`.
    lua = _LANGS["lua"]
    return lua.files(
        "if x == 0 then return 0\n"
        + "".join(f"else if x == {i} then return {i}\n" for i in range(1, branches))
        + f"else {lua.io}\n{lua.ret}\n"
        + "end\n" * branches
    )


def _or_receiver(lang: _Lang, terms: int) -> dict[str, str]:
    # An options-object client call whose receiver is `terms` alternatives,
    # each of which must name a client for the call to be a network read.
    client = " || ".join(["client"] * terms)
    return lang.files(
        f"const r = ({client}).get({{ url: '/x' }});\n{lang.io}\n{lang.ret}"
    )


# A connect-go handler wiring call: the EXPOSES pass walks the bindings of
# the function that makes one.
_GO_WIRING = (
    "path, handler := userv1connect.NewUserServiceHandler(&Impl{})\n"
    "http.Handle(path, handler)\n"
)


def _go_connect(body: str) -> dict[str, str]:
    files = _LANGS["go"].files(_GO_WIRING + body)
    files["a.go"] = (
        files["a.go"].replace(
            "import (\n", 'import (\n\t"deep/gen/userv1connect"\n\t"net/http"\n', 1
        )
        + "\ntype Impl struct{}\n"
    )
    return files


def _go_else_if(branches: int) -> str:
    go = _LANGS["go"]
    return (
        "if x == 0 {\n\treturn 0\n"
        + "".join(f"}} else if x == {i} {{\n\treturn {i}\n" for i in range(1, branches))
        + f"}} else {{\n{go.io}\n{go.ret}\n}}"
    )


def _go_or_chain(terms: int) -> str:
    go = _LANGS["go"]
    condition = " || ".join(f"x == {i}" for i in range(terms))
    return f"if {condition} {{\n{go.io}\n{go.ret}\n}}"


type _Source = Callable[[], dict[str, str]]

# Each deep shape and a shallow one whose I/O it must match.
_DEEP: dict[tuple[str, str], tuple[_Source, _Source]] = {
    ("lua", "else-if"): (
        lambda: _lua_else_if(BRANCHES),
        lambda: _nested(_LANGS["lua"], 1),
    ),
    **{
        (name, "nested"): (
            lambda lang=lang: _nested(lang, DEPTH),
            lambda lang=lang: _nested(lang, 1),
        )
        for name, lang in _LANGS.items()
    },
    ("go", "connect-else-if"): (
        lambda: _go_connect(_go_else_if(BRANCHES)),
        lambda: _go_connect(_go_or_chain(1)),
    ),
    ("go", "connect-or-chain"): (
        lambda: _go_connect(_go_or_chain(TERMS)),
        lambda: _go_connect(_go_or_chain(1)),
    ),
    **{
        (name, "or-receiver"): (
            lambda lang=_LANGS[name]: _or_receiver(lang, TERMS),
            lambda lang=_LANGS[name]: _or_receiver(lang, 2),
        )
        for name in ("javascript", "typescript")
    },
}


def _index(
    repo: Path, files: dict[str, str], capture: CaptureSelection | None
) -> tuple[_MockIngestor, GraphUpdater]:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    mock = _MockIngestor()
    return mock, create_and_run_updater(repo, mock, capture=capture)


def _edges(
    repo: Path, mock: _MockIngestor, rels: tuple[cs.RelationshipType, ...]
) -> set[tuple[str, str, str]]:
    prefix = f"{repo.name}."
    return {
        (
            str(c.args[0][2]).removeprefix(prefix),
            str(c.args[1]),
            str(c.args[2][2]).removeprefix(prefix),
        )
        for c in mock.ensure_relationship_batch.call_args_list
        if c.args[1] in {rel.value for rel in rels}
    }


@pytest.mark.parametrize(("lang", "shape"), list(_DEEP), ids=str)
def test_a_deep_function_keeps_its_calls_and_io_with_the_io_group_on(
    tmp_path: Path, lang: str, shape: str
) -> None:
    deep_source, shallow_source = _DEEP[(lang, shape)]
    # Three checkouts of one name, so their qualified names agree.
    deep, plain, shallow = (tmp_path / run / "deep" for run in ("io", "plain", "flat"))
    deep_mock, updater = _index(deep, deep_source(), _IO_ACCESS)
    plain_mock, _ = _index(plain, deep_source(), None)
    shallow_mock, _ = _index(shallow, shallow_source(), _IO_ACCESS)

    calls = (cs.RelationshipType.CALLS,)
    expected = _edges(plain, plain_mock, calls)
    assert any(
        "dispatch" in frm.lower() and "helper" in to.lower() for frm, _, to in expected
    ), expected
    assert _edges(deep, deep_mock, calls) == expected
    assert updater.call_pass_failures == []
    # The deep branch's I/O lands as the shallow one's does.
    io = _edges(shallow, shallow_mock, _IO_RELS)
    assert {rel for _, rel, _ in io} == {r.value for r in _IO_RELS}, io
    assert _edges(deep, deep_mock, _IO_RELS) == io
