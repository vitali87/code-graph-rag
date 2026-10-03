# C#'s `static Main` is the program entry point whatever its accessibility,
# as the compiler reads it (issue #2471). `dotnet new console` and most
# samples declare it without `public`, so only the `is_exported` rule ever
# kept it alive: every non-public Main, and everything only it calls, was
# reported dead (7 Main methods and 18 of 22 candidates on jbogard/MediatR).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.types_defs import ResultRow
from evals.dead_code import cgr_dead_code

_METHOD = cs.NodeLabel.METHOD.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_CALLS = cs.RelationshipType.CALLS.value

_MAIN_QN = "proj.app.Program.App.Program.Main"
_HELPER_QN = "proj.app.Program.App.Program.Helper"
_ORPHAN_QN = "proj.app.Program.App.Program.Orphan"


class FakeIngestor:
    def __init__(self, nodes: list[ResultRow], rels: list[ResultRow]) -> None:
        self._nodes = nodes
        self._rels = rels

    def fetch_all(
        self, query: str, params: dict[str, str] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return self._nodes
        return self._rels


def _row(
    qn: str,
    *,
    modifiers: list[str],
    return_type: str,
    param_types: list[str],
    label: str = _METHOD,
    path: str = "app/Program.cs",
    is_exported: bool = False,
) -> ResultRow:
    return {
        "label": label,
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1].split("(", 1)[0],
        "path": path,
        "start_line": 5,
        "end_line": 5,
        "decorators": [],
        "is_exported": is_exported,
        "overrides_external": False,
        "modifiers": modifiers,
        "return_type": return_type,
        "param_types": param_types,
    }


def _helper() -> ResultRow:
    return _row(_HELPER_QN, modifiers=["static"], return_type="void", param_types=[])


def _orphan() -> ResultRow:
    return _row(_ORPHAN_QN, modifiers=["static"], return_type="void", param_types=[])


def _calls(caller: str, callee: str, caller_label: str) -> ResultRow:
    return {
        "from_label": caller_label,
        "from_qn": caller,
        "rel_type": _CALLS,
        "to_label": _METHOD,
        "to_qn": callee,
    }


def _dead(main: ResultRow, label: str = _METHOD) -> set[str]:
    main_qn = str(main["qualified_name"])
    ingestor = FakeIngestor(
        [main, _helper(), _orphan()], [_calls(main_qn, _HELPER_QN, label)]
    )
    config = default_dead_code_config(include_tests=True, include_classes=False)
    return {
        str(row["qualified_name"])
        for row in collect_dead_code(ingestor, "proj", config)
    }


# The shapes the issue lists, as the C# parser records them: no visibility
# modifier, so `is_exported` is False and only the entry-point rule can root
# them.
_ISSUE_SHAPES = [
    pytest.param(["static"], "void", [], id="static-void"),
    pytest.param(["static"], "Task", [], id="static-Task"),
    pytest.param(["static", "async"], "Task", ["string[]"], id="async-Task-args"),
    pytest.param(["static"], "int", [], id="static-int"),
    pytest.param(["static", "async"], "Task<int>", [], id="async-Task-int"),
    pytest.param(["private", "static"], "Task", ["string[]"], id="private-Task-args"),
]


@pytest.mark.parametrize(("modifiers", "return_type", "param_types"), _ISSUE_SHAPES)
def test_non_public_static_main_roots_its_call_tree(
    modifiers: list[str], return_type: str, param_types: list[str]
) -> None:
    main = _row(
        _MAIN_QN, modifiers=modifiers, return_type=return_type, param_types=param_types
    )

    dead = _dead(main)

    assert _MAIN_QN not in dead, dead
    assert _HELPER_QN not in dead, dead
    # The run still reports real dead code beside the entry point.
    assert _ORPHAN_QN in dead, dead


# Spellings the compiler reads as the same entry-point signature.
_EQUIVALENT_SPELLINGS = [
    pytest.param("System.Threading.Tasks.Task", [], id="qualified-Task"),
    pytest.param("System.Threading.Tasks.Task<System.Int32>", [], id="qualified-int"),
    pytest.param("global::System.Int32", [], id="global-Int32"),
    pytest.param("Task < int >", [], id="spaced-generic"),
    pytest.param("void", ["String[]"], id="String-array"),
    pytest.param("void", ["System.String[]"], id="qualified-String-array"),
    pytest.param("void", ["params string[]"], id="params-array"),
    pytest.param("int", ["string[]?"], id="nullable-array"),
    pytest.param("int", ["string?[]"], id="nullable-elements"),
    pytest.param("void", ["string [ ]"], id="spaced-array"),
]


@pytest.mark.parametrize(("return_type", "param_types"), _EQUIVALENT_SPELLINGS)
def test_equivalent_entry_signature_spellings_root_main(
    return_type: str, param_types: list[str]
) -> None:
    main = _row(
        _MAIN_QN, modifiers=["static"], return_type=return_type, param_types=param_types
    )

    dead = _dead(main)

    assert _MAIN_QN not in dead, dead
    assert _HELPER_QN not in dead, dead


# Negative: a method the compiler would not pick as the entry point stays a
# candidate, and so does the helper only it calls.
_NOT_ENTRY_POINTS = [
    pytest.param("Main", [], "void", [], id="instance-Main"),
    pytest.param("Main", ["static"], "ValueTask", [], id="ValueTask"),
    pytest.param("Main", ["static"], "Task<string>", [], id="Task-of-string"),
    pytest.param("Main", ["static"], "long", [], id="long"),
    pytest.param("Main", ["static"], "int?", [], id="nullable-int"),
    pytest.param("Main", ["static"], "string", [], id="string-return"),
    pytest.param("Main", ["static"], "void", ["string"], id="string-param"),
    pytest.param("Main", ["static"], "void", ["int[]"], id="int-array-param"),
    pytest.param("Main", ["static"], "void", ["string[]", "int"], id="two-params"),
    pytest.param("main", ["static"], "void", [], id="lowercase-main"),
    pytest.param("MainAsync", ["static"], "Task", [], id="MainAsync"),
]


@pytest.mark.parametrize(
    ("name", "modifiers", "return_type", "param_types"), _NOT_ENTRY_POINTS
)
def test_non_entry_signatures_stay_candidates(
    name: str, modifiers: list[str], return_type: str, param_types: list[str]
) -> None:
    qn = f"proj.app.Program.App.Program.{name}"
    main = _row(
        qn, modifiers=modifiers, return_type=return_type, param_types=param_types
    )

    dead = _dead(main)

    assert qn in dead, dead
    assert _HELPER_QN in dead, dead


def test_static_main_outside_csharp_stays_a_candidate() -> None:
    # Java's entry point is lowercase `main`; a Java `static void Main()` is
    # an ordinary method, and the rule is gated to .cs files.
    qn = "proj.app.Program.Main"
    main = _row(
        qn,
        modifiers=["static"],
        return_type="void",
        param_types=[],
        path="app/Program.java",
    )

    dead = _dead(main)

    assert qn in dead, dead


def test_top_level_local_function_named_main_stays_a_candidate() -> None:
    # Beside top-level statements, a `static void Main()` is a local
    # function (a Function node), never the entry point: the compiler warns
    # it is unused.
    qn = "proj.app.Program.Main"
    main = _row(
        qn, modifiers=["static"], return_type="void", param_types=[], label=_FUNCTION
    )

    dead = _dead(main, label=_FUNCTION)

    assert qn in dead, dead


def test_public_main_stays_rooted() -> None:
    # Neighbouring behaviour: a public Main was already API surface.
    main = _row(
        _MAIN_QN,
        modifiers=["public", "static"],
        return_type="void",
        param_types=["string[]"],
        is_exported=True,
    )

    dead = _dead(main)

    assert _MAIN_QN not in dead, dead
    assert _HELPER_QN not in dead, dead


# End to end through the C# parser: the issue's six shapes, with file-scoped
# and block namespaces, each calling a helper.
_PROGRAMS = {
    "v1": "namespace V1;\nclass Program\n{\n"
    "    static void Main() { Helper(); }\n"
    "    static void Helper() { }\n}\n",
    "v2": "using System.Threading.Tasks;\nnamespace V2;\nclass Program\n{\n"
    "    static Task Main() { Helper(); return Task.CompletedTask; }\n"
    "    static void Helper() { }\n}\n",
    "v3": "using System.Threading.Tasks;\nnamespace V3;\nclass Program\n{\n"
    "    static async Task Main(string[] args) { Helper(); await Task.Yield(); }\n"
    "    static void Helper() { }\n}\n",
    "v4": "namespace V4;\nclass Program\n{\n"
    "    static int Main() { Helper(); return 0; }\n"
    "    static void Helper() { }\n}\n",
    "v5": "using System.Threading.Tasks;\nnamespace V5;\nclass Program\n{\n"
    "    static async Task<int> Main() { Helper(); await Task.Yield(); return 0; }\n"
    "    static void Helper() { }\n}\n",
    "v6": "using System.Threading.Tasks;\nnamespace V6\n{\n    class Program\n    {\n"
    "        static Task Main() { Helper(); return Task.CompletedTask; }\n"
    "        static void Helper() { }\n    }\n}\n",
}
_NOT_PROGRAMS = (
    "using System.Threading.Tasks;\nnamespace W;\n"
    "class Worker\n{\n"
    "    void Main() { Chore(); }\n"
    "    void Chore() { }\n}\n"
    "class Runner\n{\n"
    "    static ValueTask Main() { Step(); return default; }\n"
    "    static void Step() { }\n}\n"
)


def _write_programs(root: Path) -> Path:
    repo = root / "csmain"
    for folder, source in _PROGRAMS.items():
        (repo / folder).mkdir(parents=True)
        (repo / folder / "Program.cs").write_text(source, encoding="utf-8")
    (repo / "w").mkdir()
    (repo / "w" / "Worker.cs").write_text(_NOT_PROGRAMS, encoding="utf-8")
    return repo


def _members(dead: set[str], folder: str) -> set[str]:
    # `Class.Method` without any signature suffix, so the check does not
    # depend on how a method qn spells its parameters.
    prefix = f"proj.{folder}."
    return {
        ".".join(qn.split("(", 1)[0].rsplit(".", 2)[-2:])
        for qn in dead
        if qn.startswith(prefix)
    }


def test_parsed_main_methods_root_their_helpers(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_c_sharp")
    config = default_dead_code_config(include_tests=True, include_classes=False)

    dead = cgr_dead_code(_write_programs(tmp_path), "proj", config)

    for folder in _PROGRAMS:
        assert _members(dead, folder) == set(), (folder, sorted(dead))
    # Negative: an instance Main and a ValueTask Main are not entry points.
    assert _members(dead, "w") == {
        "Worker.Main",
        "Worker.Chore",
        "Runner.Main",
        "Runner.Step",
    }, sorted(dead)
