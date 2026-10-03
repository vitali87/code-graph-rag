"""A PHP `new C(...)` records INSTANTIATES on the class it names and CALLS
its `__construct`, the class name resolved the way PHP resolves it: through
the file's namespace, its `use` imports and a leading `\\` (issue #2466).
A method called on the constructed object binds on that class."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

PROJECT = "phpnew"

BOX = """<?php
namespace App;
class Box {
    public function __construct(int $n = 0) {}
    public function bump(): int { return 1; }
}
"""

USE = """<?php
namespace App;
function viaVar(): int    { $b = new Box(1); return $b->bump(); }
function chained(): int   { return (new Box())->bump(); }
function qualified(): int { $b = new \\App\\Box(); return $b->bump(); }
echo viaVar() + chained() + qualified();
"""

BOX_QN = f"{PROJECT}.src.Box.Box"
CTOR_QN = f"{BOX_QN}.__construct"
BUMP_QN = f"{BOX_QN}.bump"

Edge = tuple[str, str, str, str]


def _graph(
    temp_repo: Path, mock_ingestor: MagicMock, files: dict[str, str]
) -> set[Edge]:
    root = temp_repo / PROJECT
    for rel_path, source in files.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    create_and_run_updater(root, mock_ingestor, skip_if_missing="php")
    edges: set[Edge] = set()
    for rel in (cs.RelationshipType.CALLS, cs.RelationshipType.INSTANTIATES):
        for c in get_relationships(mock_ingestor, rel.value):
            props = c.kwargs.get("properties") or {}
            edges.add(
                (
                    str(c.args[0][2]),
                    rel.value,
                    str(c.args[2][2]),
                    str(props.get(cs.KEY_RESOLUTION)),
                )
            )
    return edges


def _targets(edges: set[Edge], caller: str, rel: str) -> dict[str, str]:
    return {dst: res for src, r, dst, res in edges if src == caller and r == rel}


_INST = cs.RelationshipType.INSTANTIATES.value
_CALLS = cs.RelationshipType.CALLS.value


def test_each_issue_construction_site_instantiates_box(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    edges = _graph(temp_repo, mock_ingestor, {"src/Box.php": BOX, "src/use.php": USE})
    for fn in ("viaVar", "chained", "qualified"):
        caller = f"{PROJECT}.src.use.{fn}"
        assert _targets(edges, caller, _INST) == {BOX_QN: "exact"}, (fn, edges)


def test_each_issue_construction_site_calls_construct(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    edges = _graph(temp_repo, mock_ingestor, {"src/Box.php": BOX, "src/use.php": USE})
    for fn in ("viaVar", "chained", "qualified"):
        calls = _targets(edges, f"{PROJECT}.src.use.{fn}", _CALLS)
        assert calls.get(CTOR_QN) == "exact", (fn, calls)


def test_method_on_constructed_object_binds_exactly(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    edges = _graph(temp_repo, mock_ingestor, {"src/Box.php": BOX, "src/use.php": USE})
    for fn in ("viaVar", "chained", "qualified"):
        calls = _targets(edges, f"{PROJECT}.src.use.{fn}", _CALLS)
        assert calls.get(BUMP_QN) == "exact", (fn, calls)


def test_same_file_class_is_instantiated(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = (
        "<?php\nnamespace App;\n"
        "class Box { public function __construct() {} }\n"
        "function make(): Box { return new Box(); }\n"
    )
    edges = _graph(temp_repo, mock_ingestor, {"src/one.php": source})
    caller = f"{PROJECT}.src.one.make"
    assert _targets(edges, caller, _INST) == {f"{PROJECT}.src.one.Box": "exact"}
    assert f"{PROJECT}.src.one.Box.__construct" in _targets(edges, caller, _CALLS)


def test_use_import_alias_and_group_resolve_the_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    models = (
        "<?php\nnamespace App\\Models;\n"
        "class Box { public function __construct() {} }\n"
        "class Crate {}\n"
    )
    http = (
        "<?php\nnamespace App\\Http;\n"
        "use App\\Models\\Box as Parcel;\n"
        "use App\\{Models\\Crate};\n"
        "use App\\Models;\n"
        "function aliased() { return new Parcel(); }\n"
        "function grouped() { return new Crate(); }\n"
        "function viaNamespaceAlias() { return new Models\\Box(); }\n"
    )
    edges = _graph(
        temp_repo,
        mock_ingestor,
        {"src/Models/All.php": models, "src/Http/Ctl.php": http},
    )
    box = f"{PROJECT}.src.Models.All.Box"
    crate = f"{PROJECT}.src.Models.All.Crate"
    ctl = f"{PROJECT}.src.Http.Ctl"
    assert _targets(edges, f"{ctl}.aliased", _INST) == {box: "exact"}
    assert _targets(edges, f"{ctl}.grouped", _INST) == {crate: "exact"}
    assert _targets(edges, f"{ctl}.viaNamespaceAlias", _INST) == {box: "exact"}


def test_namespace_relative_name_is_instantiated(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    use = "<?php\nnamespace App;\nfunction rel() { return new namespace\\Box(); }\n"
    edges = _graph(temp_repo, mock_ingestor, {"src/Box.php": BOX, "src/rel.php": use})
    assert _targets(edges, f"{PROJECT}.src.rel.rel", _INST) == {BOX_QN: "exact"}


def test_inherited_construct_is_called(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    sub = "<?php\nnamespace App;\nclass Sub extends Box {}\n"
    use = "<?php\nnamespace App;\nfunction make() { return new Sub(); }\n"
    edges = _graph(
        temp_repo,
        mock_ingestor,
        {"src/Box.php": BOX, "src/Sub.php": sub, "src/make.php": use},
    )
    caller = f"{PROJECT}.src.make.make"
    assert _targets(edges, caller, _INST) == {f"{PROJECT}.src.Sub.Sub": "exact"}
    assert _targets(edges, caller, _CALLS).get(CTOR_QN) == "exact"


def test_global_namespace_class_is_instantiated(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    widget = "<?php\nclass Widget { public function __construct() {} }\n"
    use = (
        "<?php\n"
        "class Local {}\n"
        "function local() { return new Local(); }\n"
        "function other() { return new Widget(); }\n"
        "function rooted() { return new \\Widget(); }\n"
    )
    edges = _graph(
        temp_repo, mock_ingestor, {"lib/Widget.php": widget, "app/use.php": use}
    )
    caller = f"{PROJECT}.app.use"
    widget_qn = f"{PROJECT}.lib.Widget.Widget"
    assert _targets(edges, f"{caller}.local", _INST) == {f"{caller}.Local": "exact"}
    assert set(_targets(edges, f"{caller}.other", _INST)) == {widget_qn}
    assert set(_targets(edges, f"{caller}.rooted", _INST)) == {widget_qn}
    assert f"{widget_qn}.__construct" in _targets(edges, f"{caller}.other", _CALLS)


# --- what must NOT bind ---------------------------------------------------------


def test_use_import_of_another_namespace_shadows_the_local_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PHP binds `Box` to the import, never to `App\Box` beside the caller, so
    # an unindexed import target leaves the site without an edge.
    use = (
        "<?php\nnamespace App;\nuse Vendor\\Box;\n"
        "function vendor() { return new Box(); }\n"
    )
    edges = _graph(temp_repo, mock_ingestor, {"src/Box.php": BOX, "src/v.php": use})
    caller = f"{PROJECT}.src.v.vendor"
    assert _targets(edges, caller, _INST) == {}
    assert CTOR_QN not in _targets(edges, caller, _CALLS)


def test_spelled_out_foreign_namespace_never_rebinds_by_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    use = (
        "<?php\nnamespace App;\n"
        "function rooted() { return new \\Vendor\\Box(); }\n"
        "function elsewhere() { return new Other(); }\n"
    )
    other = "<?php\nnamespace Lib;\nclass Other {}\n"
    edges = _graph(
        temp_repo,
        mock_ingestor,
        {"src/Box.php": BOX, "src/v.php": use, "lib/Other.php": other},
    )
    # `new Other()` in `App` means `App\Other`, which no file declares.
    for fn in ("rooted", "elsewhere"):
        assert _targets(edges, f"{PROJECT}.src.v.{fn}", _INST) == {}, fn


def test_use_function_import_does_not_alias_a_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Functions and classes are separate symbol tables in PHP: a function
    # import named like a class leaves `new Box()` on `App\Box`, and a
    # same-named function in `App` is not what `new` builds.
    helpers = "<?php\nnamespace Lib;\nfunction Box() { return 1; }\n"
    box_fn = "<?php\nnamespace App;\nfunction box() { return 2; }\n"
    use = (
        "<?php\nnamespace App;\nuse function Lib\\Box;\n"
        "function make() { return new Box(); }\n"
    )
    edges = _graph(
        temp_repo,
        mock_ingestor,
        {
            "src/Box.php": BOX,
            "src/fn.php": box_fn,
            "lib/helpers.php": helpers,
            "src/make.php": use,
        },
    )
    caller = f"{PROJECT}.src.make.make"
    assert _targets(edges, caller, _INST) == {BOX_QN: "exact"}
    calls = _targets(edges, caller, _CALLS)
    assert f"{PROJECT}.lib.helpers.Box" not in calls
    assert f"{PROJECT}.src.fn.box" not in calls


def test_class_without_construct_takes_instantiates_only(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    plain = "<?php\nnamespace App;\nclass Plain {}\n"
    use = "<?php\nnamespace App;\nfunction make() { return new Plain(); }\n"
    edges = _graph(temp_repo, mock_ingestor, {"src/Plain.php": plain, "src/m.php": use})
    caller = f"{PROJECT}.src.m.make"
    assert _targets(edges, caller, _INST) == {f"{PROJECT}.src.Plain.Plain": "exact"}
    assert _targets(edges, caller, _CALLS) == {}


def test_interface_and_dynamic_class_names_are_not_instantiated(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    iface = "<?php\nnamespace App;\ninterface Shape {}\n"
    use = (
        "<?php\nnamespace App;\n"
        "function iface() { return new Shape(); }\n"
        "function dynamic(string $cls) { return new $cls(); }\n"
    )
    edges = _graph(
        temp_repo,
        mock_ingestor,
        {"src/Box.php": BOX, "src/Shape.php": iface, "src/d.php": use},
    )
    for fn in ("iface", "dynamic"):
        caller = f"{PROJECT}.src.d.{fn}"
        assert _targets(edges, caller, _INST) == {}, fn
        assert _targets(edges, caller, _CALLS) == {}, fn


def test_receiver_not_provably_one_class_keeps_the_name_fallback(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Only a variable every binding of which is `new Box` is typed Box. A
    # parameter, a reassignment to another class, a `foreach` rebinding, and a
    # method Box does not have all keep the name-only fallback (heuristic).
    other = (
        "<?php\nnamespace App;\n"
        "class Other { public function bump(): int { return 2; }"
        " public function only(): int { return 3; } }\n"
    )
    use = (
        "<?php\nnamespace App;\n"
        "function param(Box $b) { $b = new Box(); return $b->bump(); }\n"
        "function swapped() { $b = new Box(); $b = new Other(); return $b->bump(); }\n"
        "function looped(array $xs) { $b = new Box();"
        " foreach ($xs as $b) {} return $b->bump(); }\n"
        "function missing() { $b = new Box(); return $b->only(); }\n"
    )
    edges = _graph(
        temp_repo,
        mock_ingestor,
        {"src/Box.php": BOX, "src/Other.php": other, "src/r.php": use},
    )
    for fn in ("param", "swapped", "looped"):
        calls = _targets(edges, f"{PROJECT}.src.r.{fn}", _CALLS)
        bumps = {qn: res for qn, res in calls.items() if qn.endswith(".bump")}
        assert bumps and set(bumps.values()) == {"heuristic"}, (fn, calls)
    calls = _targets(edges, f"{PROJECT}.src.r.missing", _CALLS)
    assert calls.get(f"{PROJECT}.src.Other.Other.only") == "heuristic", calls


# --- braced namespaces: each declaration's own namespace decides ----------------

BRACED = """<?php
namespace Vendor {
    class Box { public function __construct() {} }
}
namespace {
    function make() { return new Box(); }
    function vendor() { return new Vendor\\Box(); }
}
"""


def test_global_new_never_binds_a_class_declared_in_a_braced_namespace(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A file of braced blocks records no module-level namespace, so its
    # `Vendor\Box` once passed for a global class: a global `new Box()`
    # bound it, in the same file and from another one.
    widget = "<?php\nnamespace Vendor {\n    class Widget {}\n}\n"
    other = "<?php\nfunction build() { return new Widget(); }\n"
    edges = _graph(
        temp_repo,
        mock_ingestor,
        {"src/braced.php": BRACED, "lib/Widget.php": widget, "app/b.php": other},
    )
    make = f"{PROJECT}.src.braced.make"
    assert _targets(edges, make, _INST) == {}
    assert _targets(edges, make, _CALLS) == {}
    assert _targets(edges, f"{PROJECT}.app.b.build", _INST) == {}


def test_namespaced_new_binds_a_class_declared_in_a_braced_namespace(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    caller = "<?php\nnamespace App;\nfunction far() { return new \\Vendor\\Box(); }\n"
    edges = _graph(
        temp_repo, mock_ingestor, {"src/braced.php": BRACED, "src/far.php": caller}
    )
    # A braced block names its classes: `Vendor\Box` registers as
    # `braced.Vendor.Box`.
    box = f"{PROJECT}.src.braced.Vendor.Box"
    for fn in (f"{PROJECT}.src.braced.vendor", f"{PROJECT}.src.far.far"):
        assert _targets(edges, fn, _INST) == {box: "exact"}, fn
        assert _targets(edges, fn, _CALLS) == {f"{box}.__construct": "exact"}, fn


def test_global_braced_block_class_still_binds_in_its_own_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = (
        "<?php\nnamespace Vendor {\n    class Other {}\n}\n"
        "namespace {\n    class Box {}\n"
        "    function make() { return new Box(); }\n}\n"
    )
    edges = _graph(temp_repo, mock_ingestor, {"src/mixed.php": source})
    assert _targets(edges, f"{PROJECT}.src.mixed.make", _INST) == {
        f"{PROJECT}.src.mixed.Box": "exact"
    }


def test_statement_and_single_braced_namespaces_still_bind_exactly(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    stated = "<?php\nnamespace App;\nclass Stated {}\n"
    braced = "<?php\nnamespace App {\n    class Braced {}\n}\n"
    caller = (
        "<?php\nnamespace App;\n"
        "function both() { new Stated(); return new Braced(); }\n"
    )
    edges = _graph(
        temp_repo,
        mock_ingestor,
        {"src/Stated.php": stated, "src/Braced.php": braced, "src/c.php": caller},
    )
    assert _targets(edges, f"{PROJECT}.src.c.both", _INST) == {
        f"{PROJECT}.src.Stated.Stated": "exact",
        f"{PROJECT}.src.Braced.App.Braced": "exact",
    }


# --- PHP method names are case-insensitive ---------------------------------------

LINEAGE = """<?php
namespace App;
class Base {
    public function __construct() {}
    public function bump(): int { return 1; }
}
class Child extends Base {
    public function __CONSTRUCT() {}
    public function BUMP(): int { return 2; }
}
class Solo { public function __Construct() {} }
"""


def test_constructor_declared_in_another_casing_is_the_one_called(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    caller = (
        "<?php\nnamespace App;\n"
        "function child() { return new Child(); }\n"
        "function solo() { return new Solo(); }\n"
    )
    edges = _graph(
        temp_repo, mock_ingestor, {"src/L.php": LINEAGE, "src/c.php": caller}
    )
    lineage = f"{PROJECT}.src.L"
    assert _targets(edges, f"{PROJECT}.src.c.child", _CALLS) == {
        f"{lineage}.Child.__CONSTRUCT": "exact"
    }
    assert _targets(edges, f"{PROJECT}.src.c.solo", _CALLS) == {
        f"{lineage}.Solo.__Construct": "exact"
    }


def test_typed_receiver_method_in_another_casing_is_the_one_called(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    caller = (
        "<?php\nnamespace App;\n"
        "function child() { $c = new Child(); return $c->bump(); }\n"
    )
    edges = _graph(
        temp_repo, mock_ingestor, {"src/L.php": LINEAGE, "src/c.php": caller}
    )
    calls = _targets(edges, f"{PROJECT}.src.c.child", _CALLS)
    assert calls.get(f"{PROJECT}.src.L.Child.BUMP") == "exact", calls
    assert f"{PROJECT}.src.L.Base.bump" not in calls, calls


def test_child_without_constructor_reaches_the_parents_in_any_casing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = (
        "<?php\nnamespace App;\n"
        "class Root { public function __CONSTRUCT() {} }\n"
        "class Leaf extends Root {}\n"
        "function make() { return new Leaf(); }\n"
    )
    edges = _graph(temp_repo, mock_ingestor, {"src/R.php": source})
    caller = f"{PROJECT}.src.R.make"
    assert _targets(edges, caller, _INST) == {f"{PROJECT}.src.R.Leaf": "exact"}
    assert _targets(edges, caller, _CALLS) == {
        f"{PROJECT}.src.R.Root.__CONSTRUCT": "exact"
    }
