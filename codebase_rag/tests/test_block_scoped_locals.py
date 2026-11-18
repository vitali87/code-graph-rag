"""A local declared again in another block is another variable (issue #3332).

TypeScript/JavaScript, Go and Java kept one type per name for a whole
function, filled from every declaration in it: a `const op` in each `switch`
case, `s` in both arms of an `if`/`else`, a Go `s :=` shadowing an outer `s`.
Every call through the name then bound `exact` to that one type's method,
including the calls made where the other declaration is in scope, and in
TypeScript which declaration won changed from run to run.

Each call is now typed by the declaration in scope where it sits: the
innermost one whose scope holds the call and that starts before it. A name
declared once keeps its one type everywhere, as before.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

OPS_TS = """\
abstract class Change { abstract applyTo(s: number): number; }
class Edit extends Change { applyTo(s: number): number { return s + 1; } }
class Select extends Change { applyTo(s: number): number { return s + 2; } }
class SetValue extends Change { applyTo(s: number): number { return s + 3; } }

export function apply(kind: string, s: number): number {
  switch (kind) {
    case "edit": {
      const op = new Edit();
      return op.applyTo(s);
    }
    case "select": {
      const op = new Select();
      return op.applyTo(s);
    }
    default: {
      const op = new SetValue();
      return op.applyTo(s);
    }
  }
}
"""

SHAPES_GO = """\
package main

type Circle struct{}

func (Circle) Area() int { return 1 }

type Square struct{}

func (Square) Area() int { return 2 }

func Total(both bool) int {
\ts := Circle{}
\tif both {
\t\ts := Square{}
\t\t_ = s.Area()
\t}
\treturn s.Area()
}
"""

MAIN_JAVA = """\
package app;

class Circle { int area() { return 1; } }
class Square { int area() { return 2; } }

public class Main {
  int total(boolean round) {
    if (round) {
      Circle s = new Circle();
      return s.area();
    } else {
      Square s = new Square();
      return s.area();
    }
  }
}
"""

_JAVA_MAIN_PATH = "src/main/java/app/Main.java"

# Indexes the repository given and prints each CALLS edge as `_call_row`
# reads it, from a process with its own hash seed.
_CHILD_INDEX = """\
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

mock = MagicMock()
parsers, queries = load_parsers()
GraphUpdater(
    ingestor=mock,
    repo_path=Path(sys.argv[1]),
    parsers=parsers,
    queries=queries,
    skip_embeddings=True,
).run()
rows = []
for c in mock.ensure_relationship_batch.call_args_list:
    if c.args[1] == cs.RelationshipType.CALLS:
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        rows.append([c.args[2][2], props[cs.KEY_LINE], str(props[cs.KEY_RESOLUTION])])
print(json.dumps(rows))
"""

# Hash seeds for the indexing processes, besides the test's own.
_HASH_SEEDS = ("1", "2")
_CHILD_TIMEOUT_SECONDS = 300


# A CALLS edge as (callee qn, line, resolution).
_CallRow = tuple[str, int, str]


def _by_line(rows: list[_CallRow]) -> dict[int, list[str]]:
    """Each CALLS edge's line, with its callee's class and method and the
    edge's resolution."""
    found: dict[int, list[str]] = {}
    for callee_qn, line, resolution in rows:
        callee = callee_qn.split(cs.SEPARATOR_DOT)
        found[line] = [cs.SEPARATOR_DOT.join(callee[-2:]), resolution]
    return found


def _call_rows(mock: MagicMock) -> list[_CallRow]:
    rows: list[_CallRow] = []
    for c in get_relationships(mock, cs.RelationshipType.CALLS):
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        rows.append((c.args[2][2], props[cs.KEY_LINE], str(props[cs.KEY_RESOLUTION])))
    return rows


def _index(root: Path, files: dict[str, str], mock: MagicMock) -> dict[int, list[str]]:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    create_and_run_updater(root, mock)
    return _by_line(_call_rows(mock))


def _exact(callee: str) -> list[str]:
    return [callee, cs.EdgeResolution.EXACT.value]


def test_each_switch_case_calls_through_its_own_const(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    edges = _index(temp_repo, {"ops.ts": OPS_TS}, mock_ingestor)

    assert edges[10] == _exact("Edit.applyTo")
    assert edges[14] == _exact("Select.applyTo")
    assert edges[18] == _exact("SetValue.applyTo")


def test_the_ts_edges_are_the_same_in_every_process(
    temp_repo: Path, mock_ingestor: MagicMock, tmp_path: Path
) -> None:
    # Which `const op` the flat map kept depended on the order tree-sitter
    # returned the declarators in, which differs between processes; four
    # identical runs bound all three calls to Select twice and to SetValue
    # twice.
    expected = _index(temp_repo, {"ops.ts": OPS_TS}, mock_ingestor)

    for seed in _HASH_SEEDS:
        env = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "CGR_HOME": str(tmp_path / f"home-{seed}"),
        }
        result = subprocess.run(
            [sys.executable, "-c", _CHILD_INDEX, str(temp_repo)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_CHILD_TIMEOUT_SECONDS,
            check=True,
            env=env,
        )
        rows = json.loads(result.stdout.strip().splitlines()[-1])
        assert _by_line([(qn, line, how) for qn, line, how in rows]) == expected, seed


def test_a_shadowing_go_local_owns_its_block(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    edges = _index(temp_repo, {"shapes.go": SHAPES_GO}, mock_ingestor)

    assert edges[15] == _exact("Square.Area")
    # The outer `s` is a Circle again once the block ends.
    assert edges[17] == _exact("Circle.Area")


def test_a_go_closure_parameter_shadows_the_outer_local(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = SHAPES_GO.replace(
        "\tif both {\n\t\ts := Square{}\n\t\t_ = s.Area()\n\t}\n",
        "\tarea := func(s Square) int {\n\t\treturn s.Area()\n\t}\n\t_ = area\n",
    )

    edges = _index(temp_repo, {"shapes.go": source}, mock_ingestor)

    assert edges[14] == _exact("Square.Area")
    assert edges[17] == _exact("Circle.Area")


def test_java_if_and_else_locals_are_two_variables(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    edges = _index(temp_repo, {_JAVA_MAIN_PATH: MAIN_JAVA}, mock_ingestor)

    assert edges[10] == _exact("Circle.area()")
    assert edges[13] == _exact("Square.area()")


def test_an_enhanced_for_variable_does_not_retype_a_local_of_its_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The loop variables used to be collected after the locals, so the loop's
    # Square typed the block's Circle `s` too.
    source = MAIN_JAVA.replace(
        "  int total(boolean round) {\n"
        "    if (round) {\n"
        "      Circle s = new Circle();\n"
        "      return s.area();\n"
        "    } else {\n"
        "      Square s = new Square();\n"
        "      return s.area();\n"
        "    }\n"
        "  }\n",
        "  int total(java.util.List<Square> squares) {\n"
        "    int sum = 0;\n"
        "    {\n"
        "      Circle s = new Circle();\n"
        "      sum += s.area();\n"
        "    }\n"
        "    for (Square s : squares) {\n"
        "      sum += s.area();\n"
        "    }\n"
        "    return sum;\n"
        "  }\n",
    )

    edges = _index(temp_repo, {_JAVA_MAIN_PATH: source}, mock_ingestor)

    assert edges[11] == _exact("Circle.area()")
    assert edges[14] == _exact("Square.area()")


JS_VAR = """\
class Edit { applyTo(s) { return s + 1; } }
class Select { applyTo(s) { return s + 2; } }

export function run(flag) {
  if (flag) {
    var op = new Edit();
  }
  op.applyTo(1);
  if (!flag) {
    var op = new Select();
    op.applyTo(2);
  }
}
"""


def test_a_js_var_declared_in_two_blocks_is_one_function_scoped_variable(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A `var` is the function's, so the one the first block declares is still
    # the binding after that block; read as block-scoped, the call on line 8
    # would be outside both and take the flat map's last declaration.
    edges = _index(temp_repo, {"run.js": JS_VAR}, mock_ingestor)

    assert edges[8] == _exact("Edit.applyTo")
    assert edges[11] == _exact("Select.applyTo")


def test_an_anonymous_callback_s_own_const_stays_inside_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The callback's calls are its enclosing function's, so its locals are
    # walked with that function's, and scoped to the callback.
    source = (
        "class Edit { applyTo(s: number): number { return s + 1; } }\n"
        "class Select { applyTo(s: number): number { return s + 2; } }\n"
        "\n"
        "export function run(items: number[]): void {\n"
        "  const op = new Edit();\n"
        "  items.forEach((n) => {\n"
        "    const op = new Select();\n"
        "    op.applyTo(n);\n"
        "  });\n"
        "  op.applyTo(1);\n"
        "}\n"
    )

    edges = _index(temp_repo, {"run.ts": source}, mock_ingestor)

    assert edges[8] == _exact("Select.applyTo")
    assert edges[10] == _exact("Edit.applyTo")


# Negative: what must not change.


def test_a_js_var_declared_once_in_a_block_types_the_whole_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = JS_VAR.replace(
        "  if (!flag) {\n    var op = new Select();\n    op.applyTo(2);\n  }\n", ""
    )

    edges = _index(temp_repo, {"run.js": source}, mock_ingestor)

    assert edges[8] == _exact("Edit.applyTo")


def test_a_go_short_declaration_reusing_a_name_assigns_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `s, n := mk(), 2` in the block that declares `s` assigns the Circle `s`
    # and declares `n` alone: nothing types `mk()`, and reading it as a new
    # `s` would leave the call after it untyped.
    source = (
        "package main\n"
        "\n"
        "type Circle struct{}\n"
        "\n"
        "func (Circle) Area() int { return 1 }\n"
        "\n"
        "type Square struct{}\n"
        "\n"
        "func (Square) Area() int { return 2 }\n"
        "\n"
        "func Total(mk func() Circle) int {\n"
        "\ts := Circle{}\n"
        "\tif true {\n"
        "\t\ts := Square{}\n"
        "\t\t_ = s.Area()\n"
        "\t}\n"
        "\ts, n := mk(), 2\n"
        "\treturn s.Area() + n\n"
        "}\n"
    )

    edges = _index(temp_repo, {"shapes.go": source}, mock_ingestor)

    assert edges[15] == _exact("Square.Area")
    assert edges[18] == _exact("Circle.Area")


def test_an_untyped_redeclaration_keeps_the_one_type_the_name_has(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The inner `err` cannot be typed (a second result), and the outer one is
    # an `error`: nothing says the two differ, so the inner keeps `error`
    # rather than go untyped and bind `err.Error()` to the one project type
    # with an Error method.
    source = (
        "package main\n"
        "\n"
        "type MyErr struct{}\n"
        "\n"
        'func (MyErr) Error() string { return "" }\n'
        "\n"
        "func two() (int, error) { return 0, nil }\n"
        "\n"
        "func Run() error {\n"
        "\tvar err error\n"
        "\tif x, err := two(); err != nil {\n"
        "\t\t_ = x\n"
        "\t\tmsg := err.Error()\n"
        "\t\t_ = msg\n"
        "\t}\n"
        "\treturn err\n"
        "}\n"
    )

    edges = _index(temp_repo, {"errs.go": source}, mock_ingestor)

    assert 13 not in edges
