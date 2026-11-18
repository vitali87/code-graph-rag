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

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
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
        rows.append(
            [
                c.args[0][2],
                c.args[2][2],
                props[cs.KEY_LINE],
                str(props[cs.KEY_RESOLUTION]),
            ]
        )
print(json.dumps(rows))
"""

# Hash seeds for the indexing processes, besides the test's own.
_HASH_SEEDS = ("1", "2")
_CHILD_TIMEOUT_SECONDS = 300


# A CALLS edge as (caller qn, callee qn, line, resolution).
_CallRow = tuple[str, str, int, str]


def _by_line(rows: list[_CallRow]) -> dict[int, set[tuple[str, str]]]:
    """Every CALLS edge on each line, as its callee's class and method and
    the edge's resolution: a call bound to the right method and also to a
    wrong one must not pass for right."""
    found: dict[int, set[tuple[str, str]]] = {}
    for _caller, callee_qn, line, resolution in rows:
        callee = cs.SEPARATOR_DOT.join(callee_qn.split(cs.SEPARATOR_DOT)[-2:])
        found.setdefault(line, set()).add((callee, resolution))
    return found


def _call_rows(mock: MagicMock) -> list[_CallRow]:
    rows: list[_CallRow] = []
    for c in get_relationships(mock, cs.RelationshipType.CALLS):
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        rows.append(
            (
                c.args[0][2],
                c.args[2][2],
                props[cs.KEY_LINE],
                str(props[cs.KEY_RESOLUTION]),
            )
        )
    return rows


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def _index(
    root: Path, files: dict[str, str], mock: MagicMock
) -> dict[int, set[tuple[str, str]]]:
    _write(root, files)
    create_and_run_updater(root, mock)
    return _by_line(_call_rows(mock))


def _exact(callee: str) -> set[tuple[str, str]]:
    return {(callee, cs.EdgeResolution.EXACT.value)}


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
    _write(temp_repo, {"ops.ts": OPS_TS})
    create_and_run_updater(temp_repo, mock_ingestor)
    expected = sorted(_call_rows(mock_ingestor))

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
        assert sorted(tuple(row) for row in rows) == expected, seed


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


SHAPES_JAVA = """\
package app;

class Shape { int area() { return 0; } }
class Circle extends Shape { int area() { return 1; } }
class Square extends Shape { int area() { return 2; } }

public class Main {
  int total(boolean round) {
    int sum = 0;
    {
      Shape s = new Circle();
      sum += s.area();
      s = new Square();
      sum += s.area();
    }
    {
      Shape s = new Circle();
      if (round) {
        s = new Square();
      }
      sum += s.area();
    }
    {
      Square s = new Square();
      sum += s.area();
    }
    return sum;
  }
}
"""


def test_a_java_reassignment_retypes_its_variable_from_there_on(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The flat map reads `s = new Square()` as well as the declarations, and
    # a block's span must not undo that: after the assignment the variable
    # is a Square (bot review), before it still the Circle it was made.
    edges = _index(temp_repo, {_JAVA_MAIN_PATH: SHAPES_JAVA}, mock_ingestor)

    assert edges[12] == _exact("Circle.area()")
    assert edges[14] == _exact("Square.area()")
    # Assigned in a branch: a Square from the assignment to the block's end.
    assert edges[21] == _exact("Square.area()")
    assert edges[25] == _exact("Square.area()")


def test_a_ts_reassignment_retypes_its_variable_from_there_on(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = (
        "class Shape { area(): number { return 0; } }\n"
        "class Circle extends Shape { area(): number { return 1; } }\n"
        "class Square extends Shape { area(): number { return 2; } }\n"
        "\n"
        "export function total(): number {\n"
        "  let sum = 0;\n"
        "  {\n"
        "    let s: Shape = new Circle();\n"
        "    s = new Square();\n"
        "    sum += s.area();\n"
        "  }\n"
        "  {\n"
        "    const s = new Circle();\n"
        "    sum += s.area();\n"
        "  }\n"
        "  return sum;\n"
        "}\n"
    )

    edges = _index(temp_repo, {"shapes.ts": source}, mock_ingestor)

    assert edges[10] == _exact("Square.area")
    assert edges[14] == _exact("Circle.area")


def test_an_uncalled_arrow_does_not_retype_the_local_it_assigns(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The bot review's repro: `later` is never called, so `s` is still the A.
    source = (
        "class A { m(): number { return 1; } }\n"
        "class B { m(): number { return 2; } }\n"
        "class C { m(): number { return 3; } }\n"
        "\n"
        "export function run(): number {\n"
        "  let s = new A();\n"
        "  const later = () => { s = new B(); };\n"
        "  const result = s.m();\n"
        "  { let s = new C(); }\n"
        "  return result;\n"
        "}\n"
    )

    edges = _index(temp_repo, {"run.ts": source}, mock_ingestor)

    assert edges[8] == _exact("A.m")


def test_an_assignment_in_a_callback_retypes_the_rest_of_the_callback(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The bot review's repro: after `s = new B()` the callback's own `s.m()`
    # calls B's; the outer `s` the next statement reads is still the A.
    source = (
        "class A { m(): number { return 1; } }\n"
        "class B { m(): number { return 2; } }\n"
        "class C { m(): number { return 3; } }\n"
        "\n"
        "export function run(items: number[]): number {\n"
        "  let s = new A();\n"
        "  items.forEach(() => {\n"
        "    s = new B();\n"
        "    s.m();\n"
        "  });\n"
        "  const result = s.m();\n"
        "  { let s = new C(); }\n"
        "  return result;\n"
        "}\n"
    )

    edges = _index(temp_repo, {"run.ts": source}, mock_ingestor)

    assert edges[9] == _exact("B.m")
    assert edges[11] == _exact("A.m")


def test_an_assignment_in_a_callback_does_not_retype_the_outer_local(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Neither callback has run when `x.m()` does (one is only stored, the
    # other handed to `later`): their `x = new B()` says nothing about the
    # `x` the next statement reads (bot review). A reassignment in the
    # function's own body still does.
    source = (
        "class A { m(): number { return 1; } }\n"
        "class B { m(): number { return 2; } }\n"
        "class C { m(): number { return 3; } }\n"
        "\n"
        "export function run(later: (f: () => void) => void): number {\n"
        "  let sum = 0;\n"
        "  {\n"
        "    let x = new A();\n"
        "    const reset = () => {\n"
        "      x = new B();\n"
        "    };\n"
        "    later(function () {\n"
        "      x = new B();\n"
        "    });\n"
        "    sum += x.m();\n"
        "    x = new B();\n"
        "    sum += x.m();\n"
        "  }\n"
        "  {\n"
        "    const x = new C();\n"
        "    sum += x.m();\n"
        "  }\n"
        "  return sum;\n"
        "}\n"
    )

    edges = _index(temp_repo, {"run.ts": source}, mock_ingestor)

    assert edges[15] == _exact("A.m")
    assert edges[17] == _exact("B.m")
    assert edges[21] == _exact("C.m")


def test_a_js_reassignment_from_an_untyped_call_keeps_the_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `x = make()` with `make(): A` keeps `x` an A; `x = other()`, a call with
    # no type to read, changes nothing rather than typing `x` by the name
    # `other` (bot review).
    source = (
        "class A { m(): number { return 1; } }\n"
        "class C { m(): number { return 3; } }\n"
        "\n"
        "function make(): A { return new A(); }\n"
        "declare function other(): unknown;\n"
        "\n"
        "export function run(): number {\n"
        "  let sum = 0;\n"
        "  {\n"
        "    let x = new A();\n"
        "    x = make();\n"
        "    sum += x.m();\n"
        "    x = other();\n"
        "    sum += x.m();\n"
        "  }\n"
        "  {\n"
        "    const x = new C();\n"
        "    sum += x.m();\n"
        "  }\n"
        "  return sum;\n"
        "}\n"
    )

    edges = _index(temp_repo, {"run.ts": source}, mock_ingestor)

    assert edges[12] == _exact("A.m")
    assert edges[14] == _exact("A.m")
    assert edges[18] == _exact("C.m")


def test_a_java_write_in_an_anonymous_class_stays_there(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `s = new Square()` in the Runnable's `run` writes its own field `s`,
    # and runs when `run` does: the method's `s` is still the Circle.
    source = (
        "package app;\n"
        "\n"
        "class Shape { int area() { return 0; } }\n"
        "class Circle extends Shape { int area() { return 1; } }\n"
        "class Square extends Shape { int area() { return 2; } }\n"
        "\n"
        "public class Main {\n"
        "  int total() {\n"
        "    int sum = 0;\n"
        "    {\n"
        "      Shape s = new Circle();\n"
        "      Runnable task = new Runnable() {\n"
        "        Shape s;\n"
        "        public void run() {\n"
        "          s = new Square();\n"
        "        }\n"
        "      };\n"
        "      sum += s.area();\n"
        "    }\n"
        "    {\n"
        "      Square s = new Square();\n"
        "      sum += s.area();\n"
        "    }\n"
        "    return sum;\n"
        "  }\n"
        "}\n"
    )

    edges = _index(temp_repo, {_JAVA_MAIN_PATH: source}, mock_ingestor)

    assert edges[18] == _exact("Circle.area()")
    assert edges[22] == _exact("Square.area()")


def test_a_go_call_initializer_reads_a_range_receiver_s_element(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # In the loop `src` is the range's B, so `x := src.Make()` is what
    # B.Make returns, not what the parameter's A.Make does (bot review).
    source = (
        "package main\n"
        "\n"
        "type X struct{}\n"
        "\n"
        "func (X) Run() int { return 1 }\n"
        "\n"
        "type Z struct{}\n"
        "\n"
        "func (Z) Run() int { return 2 }\n"
        "\n"
        "type Y struct{}\n"
        "\n"
        "func (Y) Run() int { return 3 }\n"
        "\n"
        "type A struct{}\n"
        "\n"
        "func (A) Make() X { return X{} }\n"
        "\n"
        "type B struct{}\n"
        "\n"
        "func (B) Make() Z { return Z{} }\n"
        "\n"
        "func F(src A, bs []B) int {\n"
        "\tsum := 0\n"
        "\tfor _, src := range bs {\n"
        "\t\tx := src.Make()\n"
        "\t\tsum += x.Run()\n"
        "\t}\n"
        "\t{\n"
        "\t\tx := Y{}\n"
        "\t\tsum += x.Run()\n"
        "\t}\n"
        "\treturn sum\n"
        "}\n"
    )

    edges = _index(temp_repo, {"recv.go": source}, mock_ingestor)

    assert edges[26] == _exact("B.Make")
    assert edges[27] == _exact("Z.Run")
    assert edges[31] == _exact("Y.Run")


def test_a_go_reassignment_keeps_the_declared_interface(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A Go assignment cannot change a variable's type: `s` stays the Shape it
    # was declared, as the compiler binds it, whatever the other block's `s`
    # is. The flat map typed it as that other block's Square.
    source = (
        "package main\n"
        "\n"
        "type Shape interface{ Area() int }\n"
        "\n"
        "type Circle struct{}\n"
        "\n"
        "func (Circle) Area() int { return 1 }\n"
        "\n"
        "type Square struct{}\n"
        "\n"
        "func (Square) Area() int { return 2 }\n"
        "\n"
        "func Total() int {\n"
        "\tsum := 0\n"
        "\t{\n"
        "\t\tvar s Shape = Circle{}\n"
        "\t\ts = Square{}\n"
        "\t\tsum += s.Area()\n"
        "\t}\n"
        "\t{\n"
        "\t\ts := Circle{}\n"
        "\t\tsum += s.Area()\n"
        "\t}\n"
        "\treturn sum\n"
        "}\n"
    )

    edges = _index(temp_repo, {"shapes.go": source}, mock_ingestor)

    # The interface's method, and its implementations as the possible
    # targets of a call through it.
    assert edges[18] == {
        ("Shape.Area", cs.EdgeResolution.EXACT.value),
        ("Circle.Area", cs.EdgeResolution.HEURISTIC.value),
        ("Square.Area", cs.EdgeResolution.HEURISTIC.value),
    }
    assert edges[22] == _exact("Circle.Area")


def test_a_for_of_loop_walks_the_container_declared_where_it_is(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The loop's `x` is an element of the `items` in scope at the loop, an
    # A[], not the B[] the other branch declares under the same name.
    source = (
        "class A { m(): number { return 1; } }\n"
        "class B { m(): number { return 2; } }\n"
        "class C { m(): number { return 3; } }\n"
        "\n"
        "export function run(flag: boolean): number {\n"
        "  let sum = 0;\n"
        "  if (flag) {\n"
        "    const items: A[] = [new A()];\n"
        "    for (const x of items) {\n"
        "      sum += x.m();\n"
        "    }\n"
        "  } else {\n"
        "    const items: B[] = [new B()];\n"
        "    const x = new C();\n"
        "    sum += x.m();\n"
        "    sum += items.length;\n"
        "  }\n"
        "  return sum;\n"
        "}\n"
    )

    edges = _index(temp_repo, {"loop.ts": source}, mock_ingestor)

    assert edges[10] == _exact("A.m")
    assert edges[15] == _exact("C.m")


def test_a_go_call_initializer_reads_its_receiver_in_scope(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `x := src.Make()` calls the A `src` declared in its block, so `x` is
    # what A.Make returns, whatever the other block's `src` is.
    source = (
        "package main\n"
        "\n"
        "type X struct{}\n"
        "\n"
        "func (X) Run() int { return 1 }\n"
        "\n"
        "type Z struct{}\n"
        "\n"
        "func (Z) Run() int { return 2 }\n"
        "\n"
        "type Y struct{}\n"
        "\n"
        "func (Y) Run() int { return 3 }\n"
        "\n"
        "type A struct{}\n"
        "\n"
        "func (A) Make() X { return X{} }\n"
        "\n"
        "type B struct{}\n"
        "\n"
        "func (B) Make() Z { return Z{} }\n"
        "\n"
        "func F() int {\n"
        "\tsum := 0\n"
        "\t{\n"
        "\t\tsrc := A{}\n"
        "\t\tx := src.Make()\n"
        "\t\tsum += x.Run()\n"
        "\t}\n"
        "\t{\n"
        "\t\tsrc := B{}\n"
        "\t\t_ = src\n"
        "\t\tx := Y{}\n"
        "\t\tsum += x.Run()\n"
        "\t}\n"
        "\treturn sum\n"
        "}\n"
    )

    edges = _index(temp_repo, {"recv.go": source}, mock_ingestor)

    assert edges[27] == _exact("A.Make")
    assert edges[28] == _exact("X.Run")
    assert edges[34] == _exact("Y.Run")


def test_an_enhanced_for_variable_is_not_in_scope_in_its_iterable(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `for (Item s : s.items())` iterates another `s` (the field): the loop
    # variable is in scope in the loop body alone.
    source = (
        "package app;\n"
        "\n"
        "import java.util.List;\n"
        "\n"
        "class Item { int size() { return 1; } }\n"
        "class Source { List<Item> items() { return null; } }\n"
        "class Other { int size() { return 2; } }\n"
        "\n"
        "public class Main {\n"
        "  Source s;\n"
        "\n"
        "  int total() {\n"
        "    int sum = 0;\n"
        "    for (Item s : s.items()) {\n"
        "      sum += s.size();\n"
        "    }\n"
        "    {\n"
        "      Other s = new Other();\n"
        "      sum += s.size();\n"
        "    }\n"
        "    return sum;\n"
        "  }\n"
        "}\n"
    )
    _write(temp_repo, {_JAVA_MAIN_PATH: source})
    updater = create_and_run_updater(temp_repo, mock_ingestor)
    parsers, _queries = load_parsers()
    tree = parsers[cs.SupportedLanguage.JAVA].parse(source.encode())
    method = next(
        node
        for node in _descendants(tree.root_node)
        if node.type == cs.TS_METHOD_DECLARATION
    )
    iterable = source.encode().index(b"s.items()")
    engine = updater.factory.type_inference.java_type_inference

    spans = engine.collect_scoped_declarations(method, updater.project_name, {})

    assert {name for _start, _end, name, _type in spans} == {"s"}
    assert not any(start <= iterable < end for start, end, _name, _type in spans)
    assert _by_line(_call_rows(mock_ingestor))[15] == _exact("Item.size()")


def _descendants(node: Node) -> list[Node]:
    found: list[Node] = []
    pending = [node]
    while pending:
        current = pending.pop()
        found.append(current)
        pending.extend(current.children)
    return found


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
