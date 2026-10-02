# A PHP anonymous class (`new class (...) extends B implements I { ... }`) had
# no node: tree-sitter-php's `anonymous_class` was not a class type, so its
# methods were ingested as Functions named after the nearest NAMED class. The
# anonymous `handle()` took `Dispatcher.Dispatcher.handle`, pushed the real
# method to `handle@12`, and its CALLS rows pointed at Function endpoints that
# did not exist (issue #2538). The class now gets a Class node anchored under
# its enclosing callable, named `anonymous_<row>_<col>` like the PHP closures
# beside it, and its members are Methods under it.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import NodeLabel, RelationshipType, SupportedLanguage
from codebase_rag.dead_code import dead_code_from_graph, default_dead_code_config
from codebase_rag.language_spec import LANGUAGE_FQN_SPECS
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    force_mtime_after_cache,
    get_node_names,
    get_relationships,
)
from codebase_rag.utils.fqn_resolver import extract_function_fqns
from evals.cgr_graph import _capture

# The issue's repro. The anonymous class starts at row 19, col 25 (0-based,
# the `class` keyword), inside Dispatcher::add.
_DISPATCHER = """\
<?php
namespace App;

interface Handler { public function handle(string $req): string; }

class Dispatcher
{
    private Handler $tip;

    public function __construct(Handler $kernel) { $this->tip = $kernel; }

    public function handle(string $req): string
    {
        return $this->tip->handle($req);
    }

    public function add(string $name): void
    {
        $next = $this->tip;
        $this->tip = new class ($name, $next) implements Handler {
            public function __construct(private string $name, private Handler $next) {}

            public function handle(string $req): string
            {
                return $this->next->handle($req . $this->name);
            }
        };
    }
}
"""

# Two anonymous classes in one file: one with constructor arguments, a base
# class and interfaces (row 16, col 19), one bare `new class { }` (row 32,
# col 19). Both Pipeline and the first anonymous class define `helper`.
_PIPELINE = """\
<?php
namespace App;

interface Stage { public function run(string $in): string; }

abstract class Base
{
    public function label(): string { return 'base'; }
}

class Pipeline
{
    public function helper(string $in): string { return $in; }

    public function build(int $n): Stage
    {
        return new class ($n) extends Base implements Stage, \\Countable {
            public function __construct(private int $n) {}

            public function run(string $in): string
            {
                return $this->helper($in);
            }

            public function helper(string $in): string { return $in; }

            public function count(): int { return $this->n; }
        };
    }

    public function plain(): object
    {
        return new class {
            public function ping(): string { return 'pong'; }
        };
    }
}
"""

# Closures and an arrow fn beside anonymous classes: one inside a closure
# (row 10, col 23) and one at file level (row 17, col 11).
_BOX = """\
<?php
namespace App;

class Box
{
    public function run(): void
    {
        $f = function () { return 1; };
        $g = fn() => 2;
        $h = function () {
            return new class {
                public function inner(): int { return 3; }
            };
        };
    }
}

$top = new class {
    public function hello(): string { return 'hi'; }
};
"""

_DISP = "php_anon.Dispatcher.Dispatcher"
_DISP_ANON = f"{_DISP}.add.anonymous_19_25"
_PIPE = "php_anon.Pipeline.Pipeline"
_BUILD_ANON = f"{_PIPE}.build.anonymous_16_19"
_PLAIN_ANON = f"{_PIPE}.plain.anonymous_32_19"
_BOX_CLASS = "php_anon.Box.Box"


def _index(temp_repo: Path, mock_ingestor: MagicMock, **files: str) -> Path:
    project = temp_repo / "php_anon"
    project.mkdir(exist_ok=True)
    for name, body in files.items():
        (project / f"{name}.php").write_text(body, encoding="utf-8")
    create_and_run_updater(project, mock_ingestor, skip_if_missing="php")
    return project


def _edges(mock_ingestor: MagicMock, rel: RelationshipType) -> set[tuple[str, str]]:
    return {(c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel)}


def _labelled_edges(
    mock_ingestor: MagicMock, rel: RelationshipType
) -> set[tuple[str, str, str, str]]:
    return {
        (str(c.args[0][0]), c.args[0][2], str(c.args[2][0]), c.args[2][2])
        for c in get_relationships(mock_ingestor, rel)
    }


def test_anonymous_class_gets_class_node_under_enclosing_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, Dispatcher=_DISPATCHER)

    assert _DISP_ANON in get_node_names(mock_ingestor, NodeLabel.CLASS)
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.METHOD, f"{_DISP}.add", NodeLabel.CLASS, _DISP_ANON) in defines


def test_anonymous_class_members_are_methods_of_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, Dispatcher=_DISPATCHER)

    methods = get_node_names(mock_ingestor, NodeLabel.METHOD)
    assert {f"{_DISP_ANON}.__construct", f"{_DISP_ANON}.handle"} <= methods
    defines_method = _edges(mock_ingestor, RelationshipType.DEFINES_METHOD)
    assert (_DISP_ANON, f"{_DISP_ANON}.handle") in defines_method
    assert (_DISP_ANON, f"{_DISP_ANON}.__construct") in defines_method
    functions = get_node_names(mock_ingestor, NodeLabel.FUNCTION)
    assert not {qn for qn in functions if qn.startswith(_DISP)}, functions


def test_enclosing_class_methods_keep_their_bare_names(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, Dispatcher=_DISPATCHER)

    methods = get_node_names(mock_ingestor, NodeLabel.METHOD)
    assert {f"{_DISP}.__construct", f"{_DISP}.handle", f"{_DISP}.add"} <= methods
    assert not {qn for qn in methods if "@" in qn}, methods


def test_anonymous_class_calls_do_not_collide_with_enclosing_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's bogus edges: the anonymous `handle` calling itself as an
    # `overload`, and the real `handle` calling the anonymous one.
    _index(temp_repo, mock_ingestor, Dispatcher=_DISPATCHER)

    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{_DISP}.handle", f"{_DISP_ANON}.handle") not in calls
    assert not {
        (src, dst) for src, dst in calls if src == f"{_DISP}.handle" and "@" in dst
    }


def test_anonymous_class_implements_its_interface(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, Dispatcher=_DISPATCHER)

    implements = _edges(mock_ingestor, RelationshipType.IMPLEMENTS)
    assert (_DISP_ANON, "php_anon.Dispatcher.Handler") in implements
    overrides = _edges(mock_ingestor, RelationshipType.OVERRIDES)
    assert (f"{_DISP_ANON}.handle", "php_anon.Dispatcher.Handler.handle") in overrides


def test_anonymous_class_with_args_extends_and_implements(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, Pipeline=_PIPELINE)

    assert _BUILD_ANON in get_node_names(mock_ingestor, NodeLabel.CLASS)
    inherits = _edges(mock_ingestor, RelationshipType.INHERITS)
    implements = _edges(mock_ingestor, RelationshipType.IMPLEMENTS)
    assert (_BUILD_ANON, "php_anon.Pipeline.Base") in inherits
    assert (_BUILD_ANON, "php_anon.Pipeline.Stage") in implements
    methods = get_node_names(mock_ingestor, NodeLabel.METHOD)
    assert {
        f"{_BUILD_ANON}.__construct",
        f"{_BUILD_ANON}.run",
        f"{_BUILD_ANON}.helper",
        f"{_BUILD_ANON}.count",
    } <= methods


def test_this_call_inside_anonymous_class_resolves_to_its_own_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, Pipeline=_PIPELINE)

    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{_BUILD_ANON}.run", f"{_BUILD_ANON}.helper") in calls
    assert (f"{_BUILD_ANON}.run", f"{_PIPE}.helper") not in calls
    # Pipeline::helper keeps its bare name; the anonymous helper no longer
    # claims it.
    assert f"{_PIPE}.helper" in get_node_names(mock_ingestor, NodeLabel.METHOD)


def test_bare_new_class_gets_its_own_class_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, Pipeline=_PIPELINE)

    classes = get_node_names(mock_ingestor, NodeLabel.CLASS)
    assert _PLAIN_ANON in classes
    assert f"{_PLAIN_ANON}.ping" in get_node_names(mock_ingestor, NodeLabel.METHOD)
    defines = _edges(mock_ingestor, RelationshipType.DEFINES)
    assert (f"{_PIPE}.plain", _PLAIN_ANON) in defines


def test_two_anonymous_classes_in_one_file_get_distinct_stable_names(
    temp_repo: Path,
) -> None:
    def anon_classes(ingestor: MagicMock) -> set[str]:
        return {
            qn
            for qn in get_node_names(ingestor, NodeLabel.CLASS)
            if ".anonymous_" in qn
        }

    first = MagicMock()
    project = _index(temp_repo, first, Pipeline=_PIPELINE)
    assert anon_classes(first) == {_BUILD_ANON, _PLAIN_ANON}

    # An edit below both classes makes the re-index parse the file again;
    # neither class moved, so neither name does.
    source = project / "Pipeline.php"
    source.write_text(f"{_PIPELINE}// trailing edit\n", encoding="utf-8")
    force_mtime_after_cache(project, source)
    second = MagicMock()
    create_and_run_updater(project, second, skip_if_missing="php")
    assert anon_classes(second) == {_BUILD_ANON, _PLAIN_ANON}


def test_anonymous_class_in_closure_and_at_file_level(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, Box=_BOX)

    closure = f"{_BOX_CLASS}.run.anonymous_9_13"
    in_closure = f"{closure}.anonymous_10_23"
    top = "php_anon.Box.anonymous_17_11"
    assert {in_closure, top} <= get_node_names(mock_ingestor, NodeLabel.CLASS)
    methods = get_node_names(mock_ingestor, NodeLabel.METHOD)
    assert {f"{in_closure}.inner", f"{top}.hello"} <= methods
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.FUNCTION, closure, NodeLabel.CLASS, in_closure) in defines
    assert (NodeLabel.MODULE, "php_anon.Box", NodeLabel.CLASS, top) in defines
    functions = get_node_names(mock_ingestor, NodeLabel.FUNCTION)
    assert not {qn for qn in functions if qn.endswith((".inner", ".hello"))}


# A closure in an anonymous class's method (row 10, col 21) and an anonymous
# class nested in another's method (row 16, col 27).
_OUTER = """\
<?php
namespace App;

class Outer
{
    public function make(): object
    {
        return new class {
            public function go(): int
            {
                $f = function () { return 2; };
                return $f();
            }

            public function child(): object
            {
                return new class {
                    public function leaf(): int { return 1; }
                };
            }
        };
    }
}
"""


def test_closure_and_nested_anonymous_class_inside_anonymous_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, Outer=_OUTER)

    anon = "php_anon.Outer.Outer.make.anonymous_7_19"
    nested = f"{anon}.child.anonymous_16_27"
    closure = f"{anon}.go.anonymous_10_21"
    assert {anon, nested} <= get_node_names(mock_ingestor, NodeLabel.CLASS)
    assert closure in get_node_names(mock_ingestor, NodeLabel.FUNCTION)
    assert f"{nested}.leaf" in get_node_names(mock_ingestor, NodeLabel.METHOD)
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.METHOD, f"{anon}.go", NodeLabel.FUNCTION, closure) in defines
    assert (NodeLabel.METHOD, f"{anon}.child", NodeLabel.CLASS, nested) in defines


def test_source_lookup_names_anonymous_methods_as_the_graph_does(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `get_function_source` finds a node's code by rebuilding qns from the
    # AST through the same FQN spec; it must agree with the indexed names.
    project = _index(temp_repo, mock_ingestor, Dispatcher=_DISPATCHER)
    source = project / "Dispatcher.php"
    parser = load_parsers()[0][SupportedLanguage.PHP]
    tree = parser.parse(source.read_bytes())

    fqns = {
        fqn
        for fqn, _node in extract_function_fqns(
            tree.root_node,
            source,
            project,
            "php_anon",
            LANGUAGE_FQN_SPECS[SupportedLanguage.PHP],
        )
    }
    methods = get_node_names(mock_ingestor, NodeLabel.METHOD)
    assert {f"{_DISP_ANON}.__construct", f"{_DISP_ANON}.handle"} <= fqns & methods
    assert {f"{_DISP}.__construct", f"{_DISP}.handle"} <= fqns & methods


# --- Negative: neighbouring shapes stay exactly as they were. These hold on
# main too; they guard what the fix must not move. ---

# A named class beside an anonymous class whose members collide with nothing.
_CIRCLE = """\
<?php
namespace App;

interface Shape {}
class Base {}

class Circle extends Base implements Shape
{
    public function area(): float { return 1.0; }

    public function factory(): object
    {
        return new class {
            public function ping(): string { return 'pong'; }
        };
    }
}
"""


def test_named_class_is_unchanged(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    _index(temp_repo, mock_ingestor, Circle=_CIRCLE)

    circle = "php_anon.Circle.Circle"
    assert circle in get_node_names(mock_ingestor, NodeLabel.CLASS)
    methods = get_node_names(mock_ingestor, NodeLabel.METHOD)
    assert {f"{circle}.area", f"{circle}.factory"} <= methods
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.MODULE, "php_anon.Circle", NodeLabel.CLASS, circle) in defines
    defines_method = _edges(mock_ingestor, RelationshipType.DEFINES_METHOD)
    assert {m for c, m in defines_method if c == circle} == {
        f"{circle}.area",
        f"{circle}.factory",
    }
    inherits = _edges(mock_ingestor, RelationshipType.INHERITS)
    implements = _edges(mock_ingestor, RelationshipType.IMPLEMENTS)
    assert inherits == {(circle, "php_anon.Circle.Base")}
    assert implements == {(circle, "php_anon.Circle.Shape")}


def test_closures_and_arrow_functions_are_unchanged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, Box=_BOX)

    functions = get_node_names(mock_ingestor, NodeLabel.FUNCTION)
    closures = {
        f"{_BOX_CLASS}.run.anonymous_7_13",
        f"{_BOX_CLASS}.run.anonymous_8_13",
        f"{_BOX_CLASS}.run.anonymous_9_13",
    }
    assert closures <= functions
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    for closure in closures:
        assert (NodeLabel.METHOD, f"{_BOX_CLASS}.run", NodeLabel.FUNCTION, closure) in (
            defines
        )
    classes = get_node_names(mock_ingestor, NodeLabel.CLASS)
    assert not classes & closures
    assert _BOX_CLASS in classes


# --- Dead code: an anonymous class follows the existing override and
# factory-class rules; its methods are not roots of their own. ---

_DEAD_CODE = """\
<?php
namespace App;

interface Handler { public function handle(string $req): string; }
interface Idle { public function idle(): void; }

abstract class Shape
{
    abstract public function area(): float;

    public function describe(): string { return (string) $this->area(); }
}

class Runner
{
    public function run(Handler $h): string { return $h->handle('x'); }
}

class Factory
{
    public function make(): Handler
    {
        return new class implements Handler, Idle {
            public function handle(string $req): string { return $req; }
            public function idle(): void {}
            public function extra(): void {}
        };
    }

    public function shape(): Shape
    {
        return new class extends Shape {
            public function area(): float { return 1.0; }
        };
    }
}

function show(Shape $s): string { return $s->describe(); }

$r = new Runner();
echo $r->run(null);
echo show(null);
"""
_MAKE_ANON = "php_dead.main.Factory.make.anonymous_22_19"
_SHAPE_ANON = "php_dead.main.Factory.shape.anonymous_31_19"


def _dead(tmp_path: Path, source: str) -> tuple[set[str], set[str]]:
    """The dead set, and every Method indexed, so "not dead" never passes
    for a method that was not indexed at all."""
    project = tmp_path / "php_dead"
    project.mkdir()
    (project / "main.php").write_text(source, encoding="utf-8")
    ingestor = _capture(project, "php_dead")
    dead = dead_code_from_graph(
        ingestor.nodes,
        list(ingestor.rels),
        "php_dead.",
        default_dead_code_config(False, False),
    )
    methods = {str(uid) for label, uid in ingestor.nodes if label == NodeLabel.METHOD}
    return dead, methods


def test_anonymous_override_of_live_base_method_is_not_dead(tmp_path: Path) -> None:
    dead, methods = _dead(tmp_path, _DEAD_CODE)

    # shape() is never called, so the class it builds is not reached through
    # its factory, and no call site names area() on it. Shape::describe calls
    # Shape::area, and dispatch lands on the override: the override rule
    # keeps it live.
    assert "php_dead.main.Factory.shape" in dead, sorted(dead)
    assert f"{_SHAPE_ANON}.area" in methods - dead, sorted(dead)


def test_anonymous_override_of_live_interface_method_is_not_dead(
    tmp_path: Path,
) -> None:
    dead, methods = _dead(tmp_path, _DEAD_CODE)

    assert "php_dead.main.Factory.make" in dead, sorted(dead)
    assert f"{_MAKE_ANON}.handle" in methods - dead, sorted(dead)


def test_anonymous_class_methods_are_not_roots_of_their_own(tmp_path: Path) -> None:
    dead, _methods = _dead(tmp_path, _DEAD_CODE)

    # idle() overrides Idle::idle, which nothing calls, and extra() overrides
    # nothing: with the factory dead too, both are dead code.
    assert {f"{_MAKE_ANON}.idle", f"{_MAKE_ANON}.extra"} <= dead, sorted(dead)


def test_anonymous_class_methods_live_when_factory_is_live(tmp_path: Path) -> None:
    dead, methods = _dead(tmp_path, _DEAD_CODE + "(new Factory())->make();\n")

    # The factory-class rule: a class built in a live function escapes it,
    # so every method on it is dispatch surface.
    anon_methods = {qn for qn in methods if qn.startswith(f"{_MAKE_ANON}.")}
    assert anon_methods == {
        f"{_MAKE_ANON}.handle",
        f"{_MAKE_ANON}.idle",
        f"{_MAKE_ANON}.extra",
    }
    assert "php_dead.main.Factory.make" not in dead, sorted(dead)
    assert not anon_methods & dead, sorted(dead)
