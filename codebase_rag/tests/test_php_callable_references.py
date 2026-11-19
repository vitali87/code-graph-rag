# PHP forms that name a method without a direct `->m()` call produced no graph
# edge, so dead-code reported the target dead (issue #3117). These tests index
# a small project and read both the edges and the dead set. A public method is
# an API root, so dead-code alone cannot prove the edge: each form asserts the
# relationship, and a private callee is what makes the dead-set check mean
# something.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.dead_code import (
    _node_props,
    dead_code_from_graph,
    default_dead_code_config,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _capture, _CapturingIngestor

_PROJECT = "phpdc"

_RESOLVER = r"""<?php
namespace App;

class Resolver
{
    private string $name;

    public function __construct(string $name)
    {
        $this->name = $name;
    }

    public function create(): self
    {
        return new self($this->name);
    }

    public function createStatic(): static
    {
        return new static($this->name);
    }

    public function plain(): self
    {
        return new Resolver($this->name);
    }

    public function qualified(): self
    {
        return new \App\Resolver($this->name);
    }

    public function filterAll(array $items): array
    {
        return array_filter($items, [$this, 'isRoute']);
    }

    public function mapAll(array $items): array
    {
        return array_map([self::class, 'normalize'], $items);
    }

    public function sortAll(array $items): array
    {
        usort($items, [static::class, 'compare']);
        return $items;
    }

    public function keyed(array $items): array
    {
        return array_filter($items, array(0 => $this, 1 => 'isRoute'));
    }

    public function wrapped(): array
    {
        return ([$this, 'isRoute']);
    }

    public function early(): mixed
    {
        return self::normalize(1);
    }

    public function late(): mixed
    {
        return static::normalize(1);
    }

    public function viaVariable(string $value): string
    {
        $upper = function (string $value): string {
            return strtoupper($value);
        };
        return $upper($value);
    }

    public function viaArrow(string $value): string
    {
        $upper = fn(string $v): string => strtoupper($v);
        return $upper($value);
    }

    public function direct(array $items): array
    {
        return array_map(function ($item) { return $item; }, $items);
    }

    public function byString(): void
    {
        $single = 'App\\Resolver::compare';
        $double = "App\\Resolver::compare";
        $folded = 'app\\resolver::isRoute';
    }

    public function invokeTyped(Resolver $obj): void
    {
        call_user_func([$obj, 'isRoute'], null);
    }

    public function invokeOptional(?Resolver $obj): void
    {
        call_user_func([$obj, 'isRoute'], null);
    }

    public function invokeUnion(Resolver|string $obj): void
    {
        call_user_func([$obj, 'isRoute'], null);
    }

    public function invokeUntyped($obj): void
    {
        call_user_func([$obj, 'isRoute'], null);
    }

    public function invokeAmbiguous(Resolver|Child $obj): void
    {
        call_user_func([$obj, 'isRoute'], null);
    }

    public function fromCallable(): void
    {
        \Closure::fromCallable([$this, 'isRoute']);
    }

    public function viaArrowValue(): void
    {
        $route = fn() => [$this, 'isRoute'];
        $named = fn() => 'App\\Resolver::normalize';
    }

    public function captured(Resolver $obj): void
    {
        $held = function () use ($obj) {
            return [$obj, 'isRoute'];
        };
        $arrow = fn() => [$obj, 'compare'];
    }

    public function shadowed(Resolver $obj): void
    {
        $held = function ($obj) {
            return [$obj, 'isRoute'];
        };
    }

    public function arrowOwn(Resolver $obj): void
    {
        $held = fn(Child $obj) => [$obj, 'normalize'];
    }

    public function choose(bool $flag): void
    {
        $chosen = $flag ? [$this, 'normalize'] : [$this, 'compare'];
        $fallback = $flag ?: [$this, 'isRoute'];
    }

    public function negatives(mixed $name, string $c, string $class): void
    {
        $dynamic = [$this, $name];
        $text = 'not a callable';
        $interpolated = "{$c}::isRoute";
        $relative = 'Resolver::isRoute';
        $wide = [$this, 'isRoute', true];
        new $class();
    }

    private function isRoute(mixed $item): bool
    {
        return true;
    }

    private static function normalize(mixed $item): mixed
    {
        return $item;
    }

    private static function compare(mixed $a, mixed $b): int
    {
        return 0;
    }

    private function unused(): int
    {
        return 0;
    }
}

class Child extends Resolver
{
    public function __construct(string $name)
    {
        parent::__construct($name);
    }

    public function makeParent()
    {
        return new parent('x');
    }

    public function parentCall(): mixed
    {
        return parent::normalize(1);
    }

    private static function normalize(mixed $item): mixed
    {
        return $item;
    }
}

class Grandchild extends Child
{
    public function marker(): int
    {
        return 1;
    }
}
"""

_THING = r"""<?php
namespace Other;

class Thing
{
    public static function compare(): int
    {
        return 0;
    }

    private static function secret(): int
    {
        return 1;
    }
}
"""

_BRIDGE = r"""<?php
namespace App;

use Other\Thing as Alias;

class Bridge
{
    public function viaConst(): void
    {
        $f = [Alias::class, 'secret'];
    }

    public function viaAliasString(): void
    {
        $alias = 'Alias::secret';
    }

    public function viaAbsolute(): void
    {
        $absolute = 'Other\\Thing::secret';
    }

    public function makeAlias(): void
    {
        new Alias();
    }
}
"""

_BOX = r"""<?php
namespace App\Sub {
    class Box
    {
        public function ref(): void
        {
            $text = 'App\\Sub\\Box::hidden';
            $const = [Box::class, 'hidden'];
            new Box();
        }

        private function hidden(): void {}

        private function __construct() {}
    }
}
"""


def _index(tmp_path: Path, files: dict[str, str]) -> _CapturingIngestor:
    project = tmp_path / _PROJECT
    for rel, source in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return _capture(project, _PROJECT)


def _dead(ingestor: _CapturingIngestor) -> set[str]:
    nodes = {key: _node_props(props) for key, props in ingestor.nodes.items()}
    return dead_code_from_graph(
        nodes,
        list(ingestor.rels),
        f"{_PROJECT}.",
        default_dead_code_config(include_tests=True, include_classes=False),
    )


def _edges(ingestor: _CapturingIngestor, rel: str) -> set[tuple[str, str]]:
    return {
        (str(src), str(dst))
        for _from_label, src, kind, _to_label, dst in ingestor.rels
        if kind == rel
    }


def _defined(ingestor: _CapturingIngestor, prefix: str) -> set[str]:
    labels = {cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value}
    return {
        str(uid)
        for label, uid in ingestor.nodes
        if label in labels and str(uid).startswith(prefix)
    }


def _one(items: set[str], description: str) -> str:
    assert len(items) == 1, (description, sorted(items))
    return next(iter(items))


_RESOLVER_FILES = {"src/Resolver.php": _RESOLVER}
_R = f"{_PROJECT}.src.Resolver.Resolver"
_CHILD = f"{_PROJECT}.src.Resolver.Child"
_GRAND = f"{_PROJECT}.src.Resolver.Grandchild"


def test_relative_construction_calls_the_constructor(tmp_path: Path) -> None:
    ingestor = _index(tmp_path, _RESOLVER_FILES)
    calls = _edges(ingestor, cs.RelationshipType.CALLS.value)
    instantiates = _edges(ingestor, cs.RelationshipType.INSTANTIATES.value)
    ctor = f"{_R}.__construct"
    child_ctor = f"{_CHILD}.__construct"

    # `new self` is the enclosing class only. `new static` is that class plus
    # every subclass that declares its own constructor. Grandchild does not.
    assert (f"{_R}.create", _R) in instantiates
    assert (f"{_R}.create", ctor) in calls
    assert (f"{_R}.create", _CHILD) not in instantiates
    assert (f"{_R}.createStatic", _R) in instantiates
    assert (f"{_R}.createStatic", ctor) in calls
    assert (f"{_R}.createStatic", _CHILD) in instantiates
    assert (f"{_R}.createStatic", child_ctor) in calls
    assert (f"{_R}.createStatic", _GRAND) not in instantiates

    # `new parent` binds the extends parent. The protected-or-public parent
    # constructor is already an API root, so the edge is the proof.
    assert (f"{_CHILD}.makeParent", _R) in instantiates
    assert (f"{_CHILD}.makeParent", ctor) in calls
    assert (f"{_CHILD}.makeParent", _CHILD) not in instantiates

    # A plain and a leading-slash construction must call `__construct`, not
    # look for Python's `__init__`.
    assert (f"{_R}.plain", _R) in instantiates
    assert (f"{_R}.plain", ctor) in calls
    assert (f"{_R}.qualified", _R) in instantiates
    assert (f"{_R}.qualified", ctor) in calls
    assert not any(dst.endswith(".__init__") for _src, dst in calls)


def test_callable_values_reference_the_named_method(tmp_path: Path) -> None:
    ingestor = _index(tmp_path, _RESOLVER_FILES)
    refs = _edges(ingestor, cs.RelationshipType.REFERENCES.value)
    is_route = f"{_R}.isRoute"
    normalize = f"{_R}.normalize"
    compare = f"{_R}.compare"

    for caller in ("filterAll", "keyed", "wrapped", "fromCallable"):
        assert (f"{_R}.{caller}", is_route) in refs, caller
    assert (f"{_R}.mapAll", normalize) in refs
    # `static::class` names the method on the enclosing class and on each
    # subclass that declares it. Child does not declare `compare`.
    assert (f"{_R}.sortAll", compare) in refs
    assert (f"{_R}.sortAll", f"{_CHILD}.compare") not in refs

    # A string callable is an absolute FQCN. Case is ASCII-insensitive, and
    # a use-alias is not applied (there is none here; the alias file covers
    # that split). An unqualified `'Resolver::isRoute'` is `\Resolver`.
    assert (f"{_R}.byString", compare) in refs
    assert (f"{_R}.byString", is_route) in refs
    assert (f"{_R}.negatives", is_route) not in refs
    # `new $class()`, `[$this, $name]` and an interpolated string name nothing.
    calls = _edges(ingestor, cs.RelationshipType.CALLS.value)
    instantiates = _edges(ingestor, cs.RelationshipType.INSTANTIATES.value)
    assert not any(src == f"{_R}.negatives" for src, _dst in refs)
    assert not any(src == f"{_R}.negatives" for src, _dst in calls)
    assert not any(src == f"{_R}.negatives" for src, _dst in instantiates)
    assert (f"{_R}.invokeUntyped", is_route) not in refs
    assert (f"{_R}.invokeAmbiguous", is_route) not in refs
    for caller in ("invokeTyped", "invokeOptional", "invokeUnion"):
        assert (f"{_R}.{caller}", is_route) in refs, caller

    # An arrow body is the value, and a closure sees a parameter it captures.
    # Its own parameter, typed or not, hides that outer declaration.
    assert (f"{_R}.viaArrowValue", is_route) in refs
    assert (f"{_R}.viaArrowValue", normalize) in refs
    assert (f"{_R}.captured", is_route) in refs
    assert (f"{_R}.captured", compare) in refs
    assert (f"{_R}.shadowed", is_route) not in refs
    assert (f"{_R}.arrowOwn", f"{_CHILD}.normalize") in refs
    assert (f"{_R}.arrowOwn", normalize) not in refs
    # Both arms of a stored ternary are values. The elvis condition is not
    # an array, so only its alternative adds an edge.
    assert (f"{_R}.choose", normalize) in refs
    assert (f"{_R}.choose", compare) in refs
    assert (f"{_R}.choose", is_route) in refs


def test_relative_scoped_calls_bind_early_late_and_parent(tmp_path: Path) -> None:
    ingestor = _index(tmp_path, _RESOLVER_FILES)
    calls = _edges(ingestor, cs.RelationshipType.CALLS.value)
    own = f"{_R}.normalize"
    overridden = f"{_CHILD}.normalize"

    # `self::` is the enclosing method, not a subclass override. `static::`
    # also reaches a subclass that declares the method. `parent::` is the
    # single extends parent.
    assert (f"{_R}.early", own) in calls
    assert (f"{_R}.early", overridden) not in calls
    assert (f"{_R}.late", own) in calls
    assert (f"{_R}.late", overridden) in calls
    assert (f"{_CHILD}.parentCall", own) in calls
    assert (f"{_CHILD}.parentCall", overridden) not in calls
    # `parent::__construct` inside Child is the parent constructor.
    assert (f"{_CHILD}.__construct", f"{_R}.__construct") in calls


def test_stored_closures_are_referenced_and_direct_arguments_stay_dead(
    tmp_path: Path,
) -> None:
    ingestor = _index(tmp_path, _RESOLVER_FILES)
    refs = _edges(ingestor, cs.RelationshipType.REFERENCES.value)
    dead = _dead(ingestor)

    variable = _one(
        {qn for qn in _defined(ingestor, f"{_R}.viaVariable.") if ".anonymous_" in qn},
        "closure assigned in viaVariable",
    )
    arrow = _one(
        {qn for qn in _defined(ingestor, f"{_R}.viaArrow.") if ".anonymous_" in qn},
        "arrow function assigned in viaArrow",
    )
    direct = _one(
        {qn for qn in _defined(ingestor, f"{_R}.direct.") if ".anonymous_" in qn},
        "closure passed straight to array_map",
    )

    assert (f"{_R}.viaVariable", variable) in refs
    assert (f"{_R}.viaArrow", arrow) in refs
    # A closure passed directly as an argument is issue #2925, not this one.
    assert (f"{_R}.direct", direct) not in refs
    assert variable not in dead, sorted(dead)
    assert arrow not in dead, sorted(dead)
    assert direct in dead, sorted(dead)

    # The private methods named by the repro stay live; a method nobody
    # names does not. Public methods are roots either way.
    for member in ("isRoute", "normalize", "compare", "__construct"):
        assert f"{_R}.{member}" not in dead, (member, sorted(dead))
    assert f"{_CHILD}.normalize" not in dead, sorted(dead)
    assert f"{_CHILD}.__construct" not in dead, sorted(dead)
    assert f"{_R}.unused" in dead, sorted(dead)


def test_string_callable_ignores_aliases_and_class_const_follows_them(
    tmp_path: Path,
) -> None:
    ingestor = _index(
        tmp_path,
        {"src/Other/Thing.php": _THING, "src/Bridge.php": _BRIDGE},
    )
    thing = f"{_PROJECT}.src.Other.Thing.Thing"
    secret = f"{thing}.secret"
    refs = _edges(ingestor, cs.RelationshipType.REFERENCES.value)
    instantiates = _edges(ingestor, cs.RelationshipType.INSTANTIATES.value)
    bridge = f"{_PROJECT}.src.Bridge.Bridge"

    assert (f"{bridge}.viaConst", secret) in refs
    assert (f"{bridge}.viaAbsolute", secret) in refs
    # `'Alias::secret'` is the global class Alias. A string callable does not
    # consult `use` aliases or the current namespace.
    assert not any(src == f"{bridge}.viaAliasString" for src, _dst in refs)
    assert (f"{bridge}.makeAlias", thing) in instantiates


def test_bracketed_namespace_resolves_by_declared_namespace(
    tmp_path: Path,
) -> None:
    ingestor = _index(tmp_path, {"src/Boxed.php": _BOX})
    hidden = _one(
        {qn for qn in _defined(ingestor, f"{_PROJECT}.") if qn.endswith(".Box.hidden")},
        "Box::hidden",
    )
    box = hidden.removesuffix(".hidden")
    ctor = f"{box}.__construct"
    ref = f"{box}.ref"
    refs = _edges(ingestor, cs.RelationshipType.REFERENCES.value)
    calls = _edges(ingestor, cs.RelationshipType.CALLS.value)
    instantiates = _edges(ingestor, cs.RelationshipType.INSTANTIATES.value)

    assert (ref, hidden) in refs
    assert (ref, box) in instantiates
    assert (ref, ctor) in calls
    dead = _dead(ingestor)
    assert hidden not in dead, sorted(dead)
    assert ctor not in dead, sorted(dead)


_PARENT = r"""<?php
namespace App;

class Foo
{
    public function ref(): void
    {
        $f = 'App\\Bar::secret';
    }
}
"""

_NESTED = r"""<?php
namespace App;

class Bar
{
    private static function secret(): int
    {
        return 1;
    }
}
"""


def test_reparse_drops_only_that_files_classes(tmp_path: Path) -> None:
    # `src/Foo.php` is a path prefix of `src/Foo/Bar.php`. Re-parsing the
    # parent must not erase the nested class, and must not raise while the
    # index still holds the previous parse.
    project = tmp_path / _PROJECT
    foo = project / "src" / "Foo.php"
    nested = project / "src" / "Foo" / "Bar.php"
    foo.parent.mkdir(parents=True)
    nested.parent.mkdir(parents=True)
    foo.write_text(_PARENT, encoding="utf-8")
    nested.write_text(_NESTED, encoding="utf-8")
    _parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=_CapturingIngestor(),
        repo_path=project,
        parsers=_parsers,
        queries=queries,
        project_name=_PROJECT,
    )
    updater.run(force=True)
    foo_qn = f"{_PROJECT}.src.Foo.Foo"
    bar_qn = f"{_PROJECT}.src.Foo.Bar.Bar"
    index = updater.factory.import_processor.php_class_index
    assert foo_qn in index["app.foo"]
    assert bar_qn in index["app.bar"]

    registry = updater.function_registry
    for qn in list(registry.keys()):
        if qn == foo_qn or qn.startswith(f"{foo_qn}."):
            del registry[qn]
    parsed = updater.factory.definition_processor.process_file(
        foo,
        cs.SupportedLanguage.PHP,
        queries,
        updater.factory.structure_processor.structural_elements,
    )
    assert parsed is not None
    index = updater.factory.import_processor.php_class_index
    assert index["app.foo"] == {foo_qn}
    assert bar_qn in index["app.bar"]


def _import_does_not_redirect_construction(tmp_path: Path, source: str) -> None:
    ingestor = _index(tmp_path, {"src/Resolver.php": source})
    class_qn = f"{_PROJECT}.src.Resolver.Resolver"
    make = f"{class_qn}.make"
    instantiates = _edges(ingestor, cs.RelationshipType.INSTANTIATES.value)
    calls = _edges(ingestor, cs.RelationshipType.CALLS.value)
    assert (make, class_qn) in instantiates
    assert (make, f"{class_qn}.__construct") in calls
    assert not any("Helpers" in dst for _src, dst in instantiates)


def test_stored_callable_without_calls_stays_live(tmp_path: Path) -> None:
    # A file whose methods never call anything still has to reach the class
    # caller pass. Otherwise a stored array or closure gets no REFERENCES
    # edge and the private target is reported dead.
    ingestor = _index(
        tmp_path,
        {
            "src/Quiet.php": r"""<?php
namespace App;

class Quiet
{
    public function hold(): callable
    {
        $cb = [$this, 'normalize'];
        $upper = function (string $value): string {
            return $value;
        };
        return $cb;
    }

    private function normalize(string $value): string
    {
        return $value;
    }

    private function unused(): void
    {
    }
}
"""
        },
    )
    quiet = f"{_PROJECT}.src.Quiet.Quiet"
    refs = _edges(ingestor, cs.RelationshipType.REFERENCES.value)
    closure = _one(
        {qn for qn in _defined(ingestor, f"{quiet}.hold.") if ".anonymous_" in qn},
        "closure assigned in hold",
    )
    assert (f"{quiet}.hold", f"{quiet}.normalize") in refs
    assert (f"{quiet}.hold", closure) in refs
    dead = _dead(ingestor)
    assert f"{quiet}.normalize" not in dead, sorted(dead)
    assert closure not in dead, sorted(dead)
    assert f"{quiet}.unused" in dead, sorted(dead)


def test_callable_array_requires_indexes_zero_and_one() -> None:
    from codebase_rag.parsers.php.callables import callable_array

    parsers, _queries = load_parsers()
    root = (
        parsers[cs.SupportedLanguage.PHP]
        .parse(
            b"<?php\n"
            b"$ok = [$this, 'delete'];\n"
            b"$keyed = array(0 => $this, 1 => 'delete');\n"
            b"$numeric = array('0' => $this, '1' => 'delete');\n"
            b"$swapped = [1 => 'delete', 0 => $this];\n"
            b"$explicit = [$this, 1 => 'delete'];\n"
            b"$hex = [0x0 => $this, 0x1 => 'delete'];\n"
            b"$bits = [0b0 => $this, 0b1 => 'delete'];\n"
            b"$octal = [0o0 => $this, 0o1 => 'delete'];\n"
            b"$signed = [-0 => $this, +1 => 'delete'];\n"
            b"$flags = [false => $this, true => 'delete'];\n"
            b"$floats = [0.0 => $this, 1.0 => 'delete'];\n"
            b"$named = ['object' => $this, 'action' => 'delete'];\n"
            b"$padded = ['01' => $this, '1' => 'delete'];\n"
            b"$gap = [2 => $this, 'delete'];\n"
        )
        .root_node
    )
    arrays = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PHP_ARRAY_CREATION_EXPRESSION:
            arrays.append(node)
        stack.extend(reversed(node.children))
    parsed = [callable_array(node) for node in arrays]
    assert [item[1] if item else None for item in parsed] == [
        "delete",
        "delete",
        "delete",
        "delete",
        "delete",
        "delete",
        "delete",
        "delete",
        "delete",
        "delete",
        "delete",
        None,
        None,
        None,
    ]
    for item in parsed[:11]:
        assert item is not None
        assert item[0].text == b"$this"


def test_grouped_alias_callback_binds_the_imported_class(tmp_path: Path) -> None:
    # `use App\{Resolver as R}` must not fall back to a class named `R` in
    # the enclosing namespace. The private method on Resolver stays live.
    ingestor = _index(
        tmp_path,
        {
            "src/Grouped.php": r"""<?php
namespace App;

use App\{Resolver as R};

class Caller
{
    public function go(): void
    {
        $cb = [R::class, 'hidden'];
    }
}

class R
{
    private function hidden(): void {}
}

class Resolver
{
    private function hidden(): void {}
}
"""
        },
    )
    caller = f"{_PROJECT}.src.Grouped.Caller.go"
    refs = _edges(ingestor, cs.RelationshipType.REFERENCES.value)
    hidden = {dst for src, dst in refs if src == caller and dst.endswith(".hidden")}
    assert hidden == {f"{_PROJECT}.src.Grouped.Resolver.hidden"}
    dead = _dead(ingestor)
    assert f"{_PROJECT}.src.Grouped.Resolver.hidden" not in dead, sorted(dead)
    assert f"{_PROJECT}.src.Grouped.R.hidden" in dead, sorted(dead)


def test_cased_method_and_constructor_stay_bound(tmp_path: Path) -> None:
    # `self::normalize()` names `Normalize`, and `new static` includes a
    # subclass whose constructor is spelled `__CONSTRUCT`.
    ingestor = _index(
        tmp_path,
        {
            "src/Cased.php": r"""<?php
namespace App;

class Base
{
    public function early(): void
    {
        self::normalize();
    }

    public function make(): static
    {
        return new static();
    }

    private static function Normalize(): void {}

    public function __construct() {}
}

class Child extends Base
{
    public function __CONSTRUCT()
    {
        parent::__construct();
    }
}
"""
        },
    )
    base = f"{_PROJECT}.src.Cased.Base"
    child = f"{_PROJECT}.src.Cased.Child"
    calls = _edges(ingestor, cs.RelationshipType.CALLS.value)
    instantiates = _edges(ingestor, cs.RelationshipType.INSTANTIATES.value)
    assert (f"{base}.early", f"{base}.Normalize") in calls
    assert (f"{base}.make", base) in instantiates
    assert (f"{base}.make", child) in instantiates
    assert (f"{base}.make", f"{base}.__construct") in calls
    assert (f"{base}.make", f"{child}.__CONSTRUCT") in calls
    assert (f"{child}.__CONSTRUCT", f"{base}.__construct") in calls
    dead = _dead(ingestor)
    assert f"{base}.Normalize" not in dead, sorted(dead)


def test_string_keyed_array_does_not_keep_the_method(tmp_path: Path) -> None:
    ingestor = _index(
        tmp_path,
        {
            "src/Keyed.php": r"""<?php
namespace App;

class Box
{
    public function hold(): void
    {
        $named = ['object' => $this, 'action' => 'delete'];
        $real = [1 => 'keep', 0 => $this];
    }

    private function delete(): void {}

    private function keep(): void {}
}
"""
        },
    )
    box = f"{_PROJECT}.src.Keyed.Box"
    refs = _edges(ingestor, cs.RelationshipType.REFERENCES.value)
    assert (f"{box}.hold", f"{box}.keep") in refs
    assert (f"{box}.hold", f"{box}.delete") not in refs
    dead = _dead(ingestor)
    assert f"{box}.delete" in dead, sorted(dead)
    assert f"{box}.keep" not in dead, sorted(dead)


def test_function_import_does_not_redirect_construction(tmp_path: Path) -> None:
    _import_does_not_redirect_construction(
        tmp_path,
        r"""<?php
namespace App;

use function Helpers\Resolver;

class Resolver
{
    public function make(): void
    {
        new Resolver();
    }

    private function __construct() {}
}
""",
    )


def test_const_import_does_not_redirect_construction(tmp_path: Path) -> None:
    _import_does_not_redirect_construction(
        tmp_path,
        r"""<?php
namespace App;

use const Helpers\Resolver;

class Resolver
{
    public function make(): void
    {
        new Resolver();
    }

    private function __construct() {}
}
""",
    )
