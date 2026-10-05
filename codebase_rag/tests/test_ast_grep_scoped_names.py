# Same-named declarations in the structural (ast-grep) tier (issue #2589).
# Every Swift/Kotlin/Solidity definition used to be qualified `<module>.<name>`,
# so overloads, methods of different types and a top-level function sharing a
# name MERGEd onto one node carrying the last one's lines, methods hung off the
# Module instead of their type, and a Swift `extension T` rewrote T's Class
# node with the extension's location. These languages now name members the way
# the tree-sitter tier does: `<module>.<Type>.<member>` with DEFINES_METHOD
# from the type, nested functions under their enclosing function, and the
# function registry's `@line` variant for a true duplicate. The other tier
# languages keep their flat names: an Elixir multi-clause def and a Haskell
# multi-equation function are ONE function spread over several matches.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.ast_grep_tier import AstGrepTier, load_pattern_configs

PROJECT = "proj"
CLASS = cs.NodeLabel.CLASS.value
FUNCTION = cs.NodeLabel.FUNCTION.value
METHOD = cs.NodeLabel.METHOD.value
MODULE = cs.NodeLabel.MODULE.value
DEFINES = cs.RelationshipType.DEFINES.value
DEFINES_METHOD = cs.RelationshipType.DEFINES_METHOD.value
_DEFINITION_LABELS = {CLASS, FUNCTION, METHOD}
_CONTAINMENT = {DEFINES, DEFINES_METHOD}

Node = tuple[str, str, int]
Edge = tuple[str, str, str, str, str]


def _index(tmp_path: Path, name: str, source: str) -> tuple[MagicMock, str]:
    """Run the tier over one file; return the mock and the file's module qn."""
    (tmp_path / name).write_text(source, encoding="utf-8")
    mock = MagicMock()
    AstGrepTier(mock, tmp_path, PROJECT).process_file(tmp_path / name, {})
    stem, _, suffix = name.rpartition(".")
    return mock, f"{PROJECT}.{stem}_{suffix}"


def _short(qn: str, module_qn: str) -> str | None:
    """The qn below `module_qn`, `<module>` for the module, None if outside it."""
    if qn == module_qn:
        return "<module>"
    prefix = f"{module_qn}{cs.SEPARATOR_DOT}"
    return qn.removeprefix(prefix) if qn.startswith(prefix) else None


def _nodes(mock: MagicMock, module_qn: str) -> list[Node]:
    """(label, qn below the module, start line) per emission, repeats kept.

    A list rather than a set: two emissions of one qn are exactly the MERGE
    this issue is about, and a set would hide them.
    """
    nodes: list[Node] = []
    for c in mock.ensure_node_batch.call_args_list:
        if str(c.args[0]) not in _DEFINITION_LABELS:
            continue
        short = _short(c.args[1][cs.KEY_QUALIFIED_NAME], module_qn)
        if short is not None:
            nodes.append((str(c.args[0]), short, c.args[1][cs.KEY_START_LINE]))
    return nodes


def _edges(mock: MagicMock, module_qn: str) -> set[Edge]:
    edges: set[Edge] = set()
    for c in mock.ensure_relationship_batch.call_args_list:
        parent = _short(c.args[0][2], module_qn)
        child = _short(c.args[2][2], module_qn)
        if str(c.args[1]) in _CONTAINMENT and parent and child:
            edges.add(
                (str(c.args[0][0]), parent, str(c.args[1]), str(c.args[2][0]), child)
            )
    return edges


# The issue's repro, line for line (class @3, overloads @7/@8, extension @12
# with its own `request` @14, top-level `request` @17).
SESSION_SWIFT = """import Foundation

open class Session {
    let name: String
    init(name: String) { self.name = name }

    func request(_ url: String) -> String { url }
    func request(_ url: String, method: String) -> String { method + url }
    func download(_ url: String) -> String { url }
}

extension Session: CustomStringConvertible {
    public var description: String { name }
    func request(for id: Int) -> String { String(id) }
}

func request(_ s: Session) -> String { s.request("x") }
"""


def test_swift_overloads_members_and_top_level_stay_distinct(tmp_path: Path) -> None:
    mock, module_qn = _index(tmp_path, "Session.swift", SESSION_SWIFT)
    assert sorted(_nodes(mock, module_qn)) == sorted(
        [
            (CLASS, "Session", 3),
            (METHOD, "Session.init", 5),
            (METHOD, "Session.request", 7),
            (METHOD, "Session.request@8", 8),
            (METHOD, "Session.download", 9),
            (METHOD, "Session.request@14", 14),
            (FUNCTION, "request", 17),
        ]
    )


def test_swift_methods_and_extension_members_hang_off_their_class(
    tmp_path: Path,
) -> None:
    mock, module_qn = _index(tmp_path, "Session.swift", SESSION_SWIFT)
    assert _edges(mock, module_qn) == {
        (MODULE, "<module>", DEFINES, CLASS, "Session"),
        (CLASS, "Session", DEFINES_METHOD, METHOD, "Session.init"),
        (CLASS, "Session", DEFINES_METHOD, METHOD, "Session.request"),
        (CLASS, "Session", DEFINES_METHOD, METHOD, "Session.request@8"),
        (CLASS, "Session", DEFINES_METHOD, METHOD, "Session.download"),
        (CLASS, "Session", DEFINES_METHOD, METHOD, "Session.request@14"),
        (MODULE, "<module>", DEFINES, FUNCTION, "request"),
    }


def test_swift_extension_does_not_rewrite_its_class(tmp_path: Path) -> None:
    # The extension used to MERGE onto the Class node and move it to line 12.
    mock, module_qn = _index(tmp_path, "Session.swift", SESSION_SWIFT)
    classes = [
        c.args[1]
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == CLASS
    ]
    assert [(p[cs.KEY_START_LINE], p[cs.KEY_END_LINE]) for p in classes] == [(3, 10)]


CLIENT_KT = """package demo

class Client(val base: String) {
    fun get(path: String): String = base + path
    fun get(path: String, retries: Int): String = base + path + retries
    fun post(path: String): String = base + path
}

fun Client.get(id: Int): String = get("/" + id)

fun get(url: String): String = url
"""


def test_kotlin_overloads_extension_function_and_top_level_stay_distinct(
    tmp_path: Path,
) -> None:
    mock, module_qn = _index(tmp_path, "Client.kt", CLIENT_KT)
    assert sorted(_nodes(mock, module_qn)) == sorted(
        [
            (CLASS, "Client", 3),
            (METHOD, "Client.get", 4),
            (METHOD, "Client.get@5", 5),
            (METHOD, "Client.post", 6),
            (METHOD, "Client.get@9", 9),
            (FUNCTION, "get", 11),
        ]
    )


def test_kotlin_extension_function_attaches_to_its_receiver_class(
    tmp_path: Path,
) -> None:
    mock, module_qn = _index(tmp_path, "Client.kt", CLIENT_KT)
    assert _edges(mock, module_qn) == {
        (MODULE, "<module>", DEFINES, CLASS, "Client"),
        (CLASS, "Client", DEFINES_METHOD, METHOD, "Client.get"),
        (CLASS, "Client", DEFINES_METHOD, METHOD, "Client.get@5"),
        (CLASS, "Client", DEFINES_METHOD, METHOD, "Client.post"),
        (CLASS, "Client", DEFINES_METHOD, METHOD, "Client.get@9"),
        (MODULE, "<module>", DEFINES, FUNCTION, "get"),
    }


# The issue comment's OpenZeppelin shape: overloads inside a contract, the
# same name in an interface, and a library function named like a free one.
ERC721_SOL = """pragma solidity ^0.8.0;

contract ERC721 {
    constructor(string memory n) {}
    function safeTransferFrom(address from, address to, uint256 id) public {}
    function safeTransferFrom(address from, address to, uint256 id, bytes memory data) public {}
    modifier onlyOwner() { _; }
}
interface IERC721 { function safeTransferFrom(address from, address to, uint256 id) external; }
library Math { function add(uint a) internal pure returns (uint) { return a; } }
function add(uint a, uint b) pure returns (uint) { return a + b; }
"""


def test_solidity_overloads_stay_distinct_under_their_contract(
    tmp_path: Path,
) -> None:
    mock, module_qn = _index(tmp_path, "ERC721.sol", ERC721_SOL)
    assert sorted(_nodes(mock, module_qn)) == sorted(
        [
            (CLASS, "ERC721", 3),
            (METHOD, "ERC721.constructor", 4),
            (METHOD, "ERC721.safeTransferFrom", 5),
            (METHOD, "ERC721.safeTransferFrom@6", 6),
            (METHOD, "ERC721.onlyOwner", 7),
            (CLASS, "IERC721", 9),
            (METHOD, "IERC721.safeTransferFrom", 9),
            (CLASS, "Math", 10),
            (METHOD, "Math.add", 10),
            (FUNCTION, "add", 11),
        ]
    )
    edges = _edges(mock, module_qn)
    assert (
        CLASS,
        "ERC721",
        DEFINES_METHOD,
        METHOD,
        "ERC721.safeTransferFrom@6",
    ) in edges
    assert (CLASS, "IERC721", DEFINES_METHOD, METHOD, "IERC721.safeTransferFrom") in (
        edges
    )
    assert (MODULE, "<module>", DEFINES, FUNCTION, "add") in edges


NESTED_KT = """fun outer() {
    fun helper() {}
}
fun other() {
    fun helper() {}
}
"""


def test_kotlin_local_functions_nest_under_their_enclosing_function(
    tmp_path: Path,
) -> None:
    mock, module_qn = _index(tmp_path, "Nested.kt", NESTED_KT)
    assert sorted(_nodes(mock, module_qn)) == sorted(
        [
            (FUNCTION, "outer", 1),
            (FUNCTION, "outer.helper", 2),
            (FUNCTION, "other", 4),
            (FUNCTION, "other.helper", 5),
        ]
    )
    assert _edges(mock, module_qn) == {
        (MODULE, "<module>", DEFINES, FUNCTION, "outer"),
        (FUNCTION, "outer", DEFINES, FUNCTION, "outer.helper"),
        (MODULE, "<module>", DEFINES, FUNCTION, "other"),
        (FUNCTION, "other", DEFINES, FUNCTION, "other.helper"),
    }


NESTED_SWIFT = """class Outer {
    struct Inner {
        func deep() {}
    }
}
extension Outer.Inner {
    func more() {}
}
extension Array {
    func dedupe() -> [Element] { self }
}
extension Array {
    func dedupe(by key: Int) -> [Element] { self }
}
"""


def test_swift_nested_types_and_foreign_extensions(tmp_path: Path) -> None:
    # A nested type keeps its outer type in its name and, as in the
    # tree-sitter tier, is DEFINED by the module. An extension of a type this
    # file does not declare (Array) emits no Class for it: its members are
    # Methods under the type's name, DEFINED by the module, the shape a Rust
    # `impl Trait for String` block takes in the tree-sitter tier.
    mock, module_qn = _index(tmp_path, "Nested.swift", NESTED_SWIFT)
    assert sorted(_nodes(mock, module_qn)) == sorted(
        [
            (CLASS, "Outer", 1),
            (CLASS, "Outer.Inner", 2),
            (METHOD, "Outer.Inner.deep", 3),
            (METHOD, "Outer.Inner.more", 7),
            (METHOD, "Array.dedupe", 10),
            (METHOD, "Array.dedupe@13", 13),
        ]
    )
    assert _edges(mock, module_qn) == {
        (MODULE, "<module>", DEFINES, CLASS, "Outer"),
        (MODULE, "<module>", DEFINES, CLASS, "Outer.Inner"),
        (CLASS, "Outer.Inner", DEFINES_METHOD, METHOD, "Outer.Inner.deep"),
        (CLASS, "Outer.Inner", DEFINES_METHOD, METHOD, "Outer.Inner.more"),
        (MODULE, "<module>", DEFINES, METHOD, "Array.dedupe"),
        (MODULE, "<module>", DEFINES, METHOD, "Array.dedupe@13"),
    }


RECEIVERS_KT = """fun Int.toPx(): Int = this
fun Float.toPx(): Float = this
fun <T> List<T>.second(): T = this[1]
fun String?.orBlank(): String = this ?: ""
"""


def test_kotlin_same_named_extensions_on_foreign_receivers_stay_distinct(
    tmp_path: Path,
) -> None:
    # `toPx` on Int and on Float is the everyday Kotlin shape; both used to be
    # `<module>.toPx`. Generic arguments and `?` are not part of the type name.
    mock, module_qn = _index(tmp_path, "Ext.kt", RECEIVERS_KT)
    assert sorted(_nodes(mock, module_qn)) == sorted(
        [
            (METHOD, "Int.toPx", 1),
            (METHOD, "Float.toPx", 2),
            (METHOD, "List.second", 3),
            (METHOD, "String.orBlank", 4),
        ]
    )
    assert {edge[:3] for edge in _edges(mock, module_qn)} == {
        (MODULE, "<module>", DEFINES)
    }


# --- negative: what must NOT change ---------------------------------------


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        (
            "Solo.kt",
            "class A { fun run() {} }\nclass B { fun run() {} }\nfun main() {}\n",
            [
                (CLASS, "A", 1),
                (METHOD, "A.run", 1),
                (CLASS, "B", 2),
                (METHOD, "B.run", 2),
                (FUNCTION, "main", 3),
            ],
        ),
        (
            "Solo.swift",
            "struct A { func run() {} }\nstruct B { func run() {} }\nfunc main() {}\n",
            [
                (CLASS, "A", 1),
                (METHOD, "A.run", 1),
                (CLASS, "B", 2),
                (METHOD, "B.run", 2),
                (FUNCTION, "main", 3),
            ],
        ),
        (
            "Solo.sol",
            "contract A { function run() public {} }\n"
            "contract B { function run() public {} }\n",
            [
                (CLASS, "A", 1),
                (METHOD, "A.run", 1),
                (CLASS, "B", 2),
                (METHOD, "B.run", 2),
            ],
        ),
    ],
)
def test_a_name_unique_in_its_scope_keeps_its_bare_qualified_name(
    tmp_path: Path, name: str, source: str, expected: list[Node]
) -> None:
    # The type is what tells `A.run` from `B.run`; no `@line` is minted for a
    # definition that is the only one of its name in its scope.
    mock, module_qn = _index(tmp_path, name, source)
    assert sorted(_nodes(mock, module_qn)) == sorted(expected)


def test_a_lone_top_level_function_keeps_its_exact_qualified_name(
    tmp_path: Path,
) -> None:
    mock, module_qn = _index(tmp_path, "Main.kt", "fun main() {}\n")
    assert _nodes(mock, module_qn) == [(FUNCTION, "main", 1)]
    assert _edges(mock, module_qn) == {(MODULE, "<module>", DEFINES, FUNCTION, "main")}


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        # Ruby: flat names, unchanged -- both `run`s are still one qn.
        (
            "app.rb",
            "class A\n  def run\n  end\nend\nclass B\n  def run\n  end\nend\n",
            [
                (FUNCTION, "run", 2),
                (FUNCTION, "run", 6),
                (CLASS, "A", 1),
                (CLASS, "B", 5),
            ],
        ),
        # Elixir: `def f(0)` and `def f(n)` are two clauses of ONE function.
        (
            "m.ex",
            "defmodule M do\n  def f(0), do: 0\n  def f(n), do: n\nend\n",
            [(FUNCTION, "f", 2), (FUNCTION, "f", 3), (CLASS, "M", 1)],
        ),
        # Haskell: two equations of ONE function.
        (
            "F.hs",
            "fact 0 = 1\nfact n = n * fact (n - 1)\n",
            [(FUNCTION, "fact", 1), (FUNCTION, "fact", 2)],
        ),
        # Bash: a redefinition replaces the function; still one qn.
        (
            "run.sh",
            "f() { echo a; }\nf() { echo b; }\n",
            [(FUNCTION, "f", 1), (FUNCTION, "f", 2)],
        ),
    ],
)
def test_languages_the_issue_does_not_name_keep_flat_names(
    tmp_path: Path, name: str, source: str, expected: list[Node]
) -> None:
    mock, module_qn = _index(tmp_path, name, source)
    assert _nodes(mock, module_qn) == expected
    assert {edge[:3] for edge in _edges(mock, module_qn)} == {
        (MODULE, "<module>", DEFINES)
    }


def test_only_the_three_named_languages_opt_in() -> None:
    scoped = {
        config.ast_grep_id
        for config in load_pattern_configs().values()
        if config.scoped_names
    }
    assert scoped == {"swift", "kotlin", "solidity"}


def test_tree_sitter_languages_never_reach_the_tier(tmp_path: Path) -> None:
    from codebase_rag.language_spec import LANGUAGE_SPECS

    tier = AstGrepTier(MagicMock(), tmp_path, PROJECT)
    claimed = {
        extension
        for spec in LANGUAGE_SPECS.values()
        for extension in spec.file_extensions
        if tier.handles(extension)
    }
    assert claimed == set()


def test_every_definition_is_reachable_from_its_module(tmp_path: Path) -> None:
    # A file's re-parse deletes whatever its Module reaches over
    # DEFINES/DEFINES_METHOD; a node hung off a Class the file never emits
    # would outlive every re-index.
    for name, source in (
        ("Session.swift", SESSION_SWIFT),
        ("Nested.swift", NESTED_SWIFT),
        ("Client.kt", CLIENT_KT),
        ("Ext.kt", RECEIVERS_KT),
        ("ERC721.sol", ERC721_SOL),
    ):
        mock, module_qn = _index(tmp_path, name, source)
        children: dict[str, set[str]] = {}
        for _, parent, _, _, child in _edges(mock, module_qn):
            children.setdefault(parent, set()).add(child)
        reached: set[str] = set()
        frontier = ["<module>"]
        while frontier:
            for child in children.get(frontier.pop(), ()):
                if child not in reached:
                    reached.add(child)
                    frontier.append(child)
        emitted = {qn for _, qn, _ in _nodes(mock, module_qn)}
        assert emitted == reached, (name, emitted ^ reached)


# Kotlin and Python laid out line for line, so each tier's names can be
# compared directly: a language moving between tiers keeps its qns.
PARITY_PY = """class Client:
    def get(self, path): return path
    def get(self, path, retries): return path
    def post(self, path): return path
def get(url): return url
def outer():
    def helper(): return 1
    return helper
"""
PARITY_KT = """class Client {
    fun get(path: String) = path
    fun get(path: String, retries: Int) = path
    fun post(path: String) = path }
fun get(url: String) = url
fun outer() {
    fun helper() = 1
}
"""


def test_names_match_the_tree_sitter_tier_for_the_same_shapes(
    tmp_path: Path,
) -> None:
    (tmp_path / "shapes.py").write_text(PARITY_PY, encoding="utf-8")
    (tmp_path / "shapes.kt").write_text(PARITY_KT, encoding="utf-8")
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    modules = {
        c.args[1][cs.KEY_PATH]: c.args[1][cs.KEY_QUALIFIED_NAME]
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == MODULE
    }
    python_qn, kotlin_qn = modules["shapes.py"], modules["shapes.kt"]
    assert sorted(_nodes(mock, python_qn)) == sorted(
        [
            (CLASS, "Client", 1),
            (METHOD, "Client.get", 2),
            (METHOD, "Client.get@3", 3),
            (METHOD, "Client.post", 4),
            (FUNCTION, "get", 5),
            (FUNCTION, "outer", 6),
            (FUNCTION, "outer.helper", 7),
        ]
    )
    assert sorted(_nodes(mock, kotlin_qn)) == sorted(_nodes(mock, python_qn))
    assert _edges(mock, kotlin_qn) == _edges(mock, python_qn)


# --- config: the new keys are validated like the existing ones -------------


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    from codebase_rag.parsers import ast_grep_tier

    (tmp_path / "lang.yaml").write_text(
        'ast_grep_id: kotlin\nextensions: [".xx"]\n' + body, encoding="utf-8"
    )
    monkeypatch.setattr(ast_grep_tier, "_PATTERNS_DIR", tmp_path)
    ast_grep_tier.load_pattern_configs()


@pytest.mark.parametrize(
    "body",
    [
        "functions:\n  - kind: f\n    receiver_child: receiver_type\n",
        "type_extensions:\n  - kind: class_declaration\n",
    ],
)
def test_extension_keys_without_scoped_names_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    # With flat names there is no scope to name a member under, so the key
    # would silently do nothing.
    with pytest.raises(ValueError, match="scoped_names"):
        _load(tmp_path, monkeypatch, body)


def test_receiver_child_on_a_pattern_rule_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="'receiver_child' applies to 'kind'"):
        _load(
            tmp_path,
            monkeypatch,
            "scoped_names: true\nfunctions:\n"
            "  - pattern: 'fun $NAME'\n    receiver_child: receiver_type\n",
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Client", "Client"),
        ("List<T>", "List"),
        ("Map<K, List<V>>", "Map"),
        ("String?", "String"),
        ("Outer.Inner", "Outer.Inner"),
        # not a type name: the declaration is left unscoped, never named
        # after a stray token
        ("[String]", None),
        ("`odd name`", None),
        ("(Int) -> Unit", None),
    ],
)
def test_type_path_reads_the_named_type(text: str, expected: str | None) -> None:
    from codebase_rag.parsers.ast_grep_scope import type_path

    assert type_path(text) == expected


def test_an_unreadable_extension_is_not_read_as_a_class(tmp_path: Path) -> None:
    # `extension [String]` (SE-0361) names no type this can qualify under;
    # its member falls back to the module rather than a Class `[String]`.
    mock, module_qn = _index(
        tmp_path,
        "Sugar.swift",
        'extension [String] {\n    func joined2() -> String { "" }\n}\n',
    )
    assert _nodes(mock, module_qn) == [(FUNCTION, "joined2", 2)]
