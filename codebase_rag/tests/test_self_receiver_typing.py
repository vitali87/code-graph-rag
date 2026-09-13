"""`self.method()` and `cls.method()` receivers are typed as the enclosing
class (issue #1901).

The local type map never carried a `self` entry, so a right-hand-side
`self.parse()` had no receiver type, the assigned local stayed untyped, and a
later call on it fell to the bare-name fallback: `w.render()` bound to every
class defining `render`. The map is now seeded with the enclosing class for
Python methods, the way the Rust branch already seeds `self`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

# The decoy defines the colliding NAME; nothing in the fixture ever calls it.
_DECOY_COLLIDES = (
    "class Banner:\n    def render(self) -> str:\n        return 'banner'\n"
)
# Instrument check: with the name gone, no edge can be emitted for any reason.
_DECOY_CONTROL = "class Banner:\n    def paint(self) -> str:\n        return 'banner'\n"

_WIDGET = "class Widget:\n    def render(self) -> str:\n        return 'w'\n"

_SELF_ASSIGN = (
    "from proj.widget import Widget\n\n\n"
    "class Parser:\n"
    "    def parse(self) -> Widget:\n"
    "        return Widget()\n\n"
    "    def run(self) -> str:\n"
    "        w = self.parse()\n"
    "        return w.render()\n"
)
_CLS_ASSIGN = (
    "from proj.widget import Widget\n\n\n"
    "class Parser:\n"
    "    @classmethod\n"
    "    def make(cls) -> Widget:\n"
    "        return Widget()\n\n"
    "    @classmethod\n"
    "    def build(cls) -> str:\n"
    "        w = cls.make()\n"
    "        return w.render()\n"
)
_OTHER = (
    "from proj.banner import Banner\n\n\n"
    "class Other:\n    def parse(self) -> Banner:\n        return Banner()\n\n\n"
    "def pick() -> Other:\n    return Other()\n"
)
_CLS_ALIAS_IN_STATIC = (
    "from proj.widget import Widget\n"
    "from proj.other import pick\n\n\n"
    "class Parser:\n"
    "    def parse(self) -> Widget:\n"
    "        return Widget()\n\n"
    "    @staticmethod\n"
    "    def st() -> str:\n"
    "        cls = pick()\n"
    "        w = cls.parse()\n"
    "        return w.render()\n"
)
_CLS_ALIAS_IN_METHOD = (
    "from proj.widget import Widget\n"
    "from proj.other import pick\n\n\n"
    "class Parser:\n"
    "    def parse(self) -> Widget:\n"
    "        return Widget()\n\n"
    "    def run(self) -> str:\n"
    "        cls = pick()\n"
    "        w = cls.parse()\n"
    "        return w.render()\n"
)
_CLOSURE = (
    "from proj.widget import Widget\n\n\n"
    "class Parser:\n"
    "    def parse(self) -> Widget:\n"
    "        return Widget()\n\n"
    "    def run(self) -> str:\n"
    "        def inner() -> str:\n"
    "            w = self.parse()\n"
    "            return w.render()\n"
    "        return inner()\n"
)
_RETURNS_LOCAL = (
    "from proj.widget import Widget\n\n\n"
    "class Parser:\n"
    "    def parse(self) -> Widget:\n"
    "        return Widget()\n\n"
    "    def run_ret(self):\n"
    "        w = self.parse()\n"
    "        return w\n\n\n"
    "def use(p: Parser) -> str:\n"
    "    r = p.run_ret()\n"
    "    return r.render()\n"
)
_SELF_SHADOWED = (
    "from proj.widget import Widget\n\n\n"
    "class Parser:\n"
    "    def parse(self) -> Widget:\n"
    "        return Widget()\n\n"
    "    def run(self) -> str:\n"
    "        self = Widget()\n"
    "        return self.render()\n"
)


def _render_targets(repo: Path, caller_suffix: str) -> set[str]:
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    return {
        str(tgt)
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value
        and str(src).endswith(caller_suffix)
        and str(tgt).endswith(".render")
    }


def _build(tmp_path: Path, app: str, decoy: str) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    (repo / "widget.py").write_text(_WIDGET, encoding="utf-8")
    (repo / "banner.py").write_text(decoy, encoding="utf-8")
    (repo / "other.py").write_text(_OTHER, encoding="utf-8")
    (repo / "app.py").write_text(app, encoding="utf-8")
    return repo


@pytest.mark.parametrize(
    ("app", "caller"),
    [(_SELF_ASSIGN, ".Parser.run"), (_CLS_ASSIGN, ".Parser.build")],
    ids=["self", "cls"],
)
def test_a_local_assigned_from_a_self_call_is_typed(
    tmp_path: Path, app: str, caller: str
) -> None:
    repo = _build(tmp_path, app, _DECOY_COLLIDES)
    assert _render_targets(repo, caller) == {"proj.widget.Widget.render"}, (
        "the receiver assigned from self.parse() was untyped and matched by name"
    )


@pytest.mark.parametrize(
    ("app", "caller"),
    [(_SELF_ASSIGN, ".Parser.run"), (_CLS_ASSIGN, ".Parser.build")],
    ids=["self", "cls"],
)
def test_the_control_cannot_produce_the_decoy_edge(
    tmp_path: Path, app: str, caller: str
) -> None:
    # Validates the instrument: with the decoy's method renamed there is no
    # name to collide with, so the assertion above cannot pass for a wrong
    # reason; the true edge is still there.
    repo = _build(tmp_path, app, _DECOY_CONTROL)
    assert _render_targets(repo, caller) == {"proj.widget.Widget.render"}


def test_a_local_named_self_keeps_its_own_type(tmp_path: Path) -> None:
    # Seeding uses setdefault: a binding of that name in the body wins.
    repo = _build(tmp_path, _SELF_SHADOWED, _DECOY_COLLIDES)
    assert _render_targets(repo, ".Parser.run") == {"proj.widget.Widget.render"}


@pytest.mark.parametrize(
    ("app", "caller"),
    [(_CLS_ALIAS_IN_STATIC, ".Parser.st"), (_CLS_ALIAS_IN_METHOD, ".Parser.run")],
    ids=["staticmethod", "instance-method"],
)
def test_a_body_binding_named_cls_keeps_the_type_the_alias_pass_gives_it(
    tmp_path: Path, app: str, caller: str
) -> None:
    # `cls = pick()` is an alias to an `Other`, whose `parse()` returns a
    # Banner. A seed present during the walk would have made the alias pass
    # yield to it and typed `w` as a Widget (found by the local review).
    repo = _build(tmp_path, app, _DECOY_COLLIDES)
    assert _render_targets(repo, caller) == {"proj.banner.Banner.render"}


def test_a_closure_inside_a_method_sees_the_methods_self(tmp_path: Path) -> None:
    # `inner` has no `self` parameter of its own; the enclosing method's is
    # what types `w`. Asserted with `in`, not `==`: a function nested in a
    # method is ingested twice today, once by the module-function pass with
    # no class context, and that pass still emits the bare-name edge. That
    # double ingestion is pre-existing and filed separately.
    repo = _build(tmp_path, _CLOSURE, _DECOY_COLLIDES)
    assert "proj.widget.Widget.render" in _render_targets(repo, ".inner")


def test_a_returned_local_typed_from_self_types_the_caller(tmp_path: Path) -> None:
    # Return-statement analysis builds the same map; with the class passed
    # there too, `return w` carries `Widget` to `r = p.run_ret()`.
    repo = _build(tmp_path, _RETURNS_LOCAL, _DECOY_COLLIDES)
    assert _render_targets(repo, ".use") == {"proj.widget.Widget.render"}


_SHAPES = (
    "from proj.widget import Widget\n"
    "from proj.other import pick\n\n\n"
    "class Parser:\n"
    "    def parse(self) -> Widget:\n"
    "        return Widget()\n\n"
    "    @staticmethod\n"
    "    def static_self(self) -> str:\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def shadowing(self) -> str:\n"
    "        def inner(self) -> str:\n"
    "            w = self.parse()\n"
    "            return w.render()\n"
    "        return inner(self)\n\n"
    "    def rebinding(self) -> str:\n"
    "        self = pick()\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def plain(self) -> str:\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def other_first(other, self) -> str:\n"
    "        w = self.parse()\n"
    "        return w.render()\n"
)


def _method_maps(tmp_path: Path) -> dict[str, dict[str, str]]:
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    repo = _build(tmp_path, _SHAPES, _DECOY_CONTROL)
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store, repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run(force=True)
    python = next(p for k, p in parsers.items() if str(k) == "python")
    tree = python.parse((repo / "app.py").read_bytes())
    class_node = next(
        n for n in tree.root_node.children if n.type == "class_definition"
    )
    ti = updater.factory.type_inference
    maps: dict[str, dict[str, str]] = {}
    for child in class_node.child_by_field_name("body").children:
        node = (
            child.child_by_field_name("definition")
            if child.type == "decorated_definition"
            else child
        )
        if node is None or node.type != "function_definition":
            continue
        name = node.child_by_field_name("name").text.decode()
        maps[name] = ti.build_local_variable_type_map(
            node, "proj.app", cs.SupportedLanguage.PYTHON, "proj.app.Parser"
        )
        if name == "shadowing":
            inner = next(
                n
                for n in node.child_by_field_name("body").children
                if n.type == "function_definition"
            )
            maps["shadowing.inner"] = ti.build_local_variable_type_map(
                inner, "proj.app", cs.SupportedLanguage.PYTHON, "proj.app.Parser"
            )
    return maps


_BINDING_FORMS = (
    "from proj.widget import Widget\n"
    "from proj.other import pick\n\n\n"
    "class Parser:\n"
    "    def parse(self) -> Widget:\n"
    "        return Widget()\n\n"
    "    def for_target(self) -> str:\n"
    "        for self in [pick()]:\n"
    "            pass\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def with_alias(self) -> str:\n"
    "        with pick() as self:\n"
    "            w = self.parse()\n"
    "        return w.render()\n\n"
    "    def except_alias(self) -> str:\n"
    "        try:\n"
    "            pass\n"
    "        except ValueError as self:\n"
    "            w = self.parse()\n"
    "        return w.render()\n\n"
    "    def walrus(self) -> str:\n"
    "        if (self := pick()):\n"
    "            pass\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def unpacked(self) -> str:\n"
    "        other, self = pick(), pick()\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def attribute_target(self) -> str:\n"
    "        self.cache = pick()\n"
    "        self.items[0] = pick()\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def nested_default(self) -> str:\n"
    "        def inner(x=(self := pick())) -> int:\n"
    "            return 1\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def nested_return_annotation(self) -> str:\n"
    "        def inner() -> (self := pick()):\n"
    "            return 1\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def nested_param_annotation(self) -> str:\n"
    "        def inner(x: (self := pick())) -> int:\n"
    "            return 1\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def match_capture(self) -> str:\n"
    "        match pick():\n"
    "            case self:\n"
    "                pass\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def match_keyword(self) -> str:\n"
    "        match pick():\n"
    "            case Widget(name=self):\n"
    "                pass\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def match_sequence(self) -> str:\n"
    "        match pick():\n"
    "            case [self, *rest]:\n"
    "                pass\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def match_splat(self) -> str:\n"
    "        match pick():\n"
    "            case [first, *self]:\n"
    "                pass\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def match_as(self) -> str:\n"
    "        match pick():\n"
    "            case Widget() as self:\n"
    "                pass\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def match_keyword_name(self) -> str:\n"
    "        match pick():\n"
    "            case Widget(self=1):\n"
    "                pass\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def match_value_pattern(self) -> str:\n"
    "        match pick():\n"
    "            case Widget.self:\n"
    "                pass\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def with_tuple_alias(self) -> str:\n"
    "        with pick() as (self, other):\n"
    "            pass\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def import_alias(self) -> str:\n"
    "        from proj.other import pick as self\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def import_bare(self) -> str:\n"
    "        from proj.other import self\n"
    "        w = self.parse()\n"
    "        return w.render()\n\n"
    "    def nested_scope_only(self) -> str:\n"
    "        class Inner:\n"
    "            def go(self) -> int:\n"
    "                self = pick()\n"
    "                return 1\n"
    "        w = self.parse()\n"
    "        return w.render()\n"
)


def _binding_form_maps(tmp_path: Path) -> dict[str, dict[str, str]]:
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    repo = _build(tmp_path, _BINDING_FORMS, _DECOY_CONTROL)
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store, repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run(force=True)
    python = next(p for k, p in parsers.items() if str(k) == "python")
    tree = python.parse((repo / "app.py").read_bytes())
    class_node = next(
        n for n in tree.root_node.children if n.type == "class_definition"
    )
    ti = updater.factory.type_inference
    return {
        node.child_by_field_name(
            "name"
        ).text.decode(): ti.build_local_variable_type_map(
            node, "proj.app", cs.SupportedLanguage.PYTHON, "proj.app.Parser"
        )
        for node in class_node.child_by_field_name("body").children
        if node.type == "function_definition"
    }


def test_every_binding_form_of_the_receiver_suppresses_the_seed(tmp_path: Path) -> None:
    # A receiver rebound by ANY binding form in the method's own body is a
    # value the seed must not type: `w` must not be a Widget in those methods.
    maps = _binding_form_maps(tmp_path)
    for method in ("for_target", "with_alias", "except_alias", "walrus", "unpacked"):
        assert maps[method].get("w") != "Widget", method
    # A nested def's DEFAULT evaluates in the method's scope: a walrus there
    # rebinds the receiver even though the def's body is another scope.
    assert maps["nested_default"].get("w") != "Widget"
    # So do its RETURN ANNOTATION and parameter ANNOTATIONS (the bot found
    # the return annotation: the def is skipped as a scope, but the
    # annotation is evaluated by the enclosing method).
    assert maps["nested_return_annotation"].get("w") != "Widget"
    assert maps["nested_param_annotation"].get("w") != "Widget"
    # A `case` capture in any position, a destructuring `with ... as (a, b)`
    # target and an import alias are bindings too (local reviewer).
    for method in (
        "match_capture",
        "match_keyword",
        "match_sequence",
        "match_splat",
        "match_as",
        "with_tuple_alias",
        "import_alias",
        "import_bare",
    ):
        assert maps[method].get("w") != "Widget", method


def test_an_attribute_or_subscript_target_is_not_a_rebinding(tmp_path: Path) -> None:
    # `self.cache = ...` and `self.items[0] = ...` mutate through the
    # receiver; they do not rebind it, so the seed must stay (found by the
    # bot: nearly every method assigns an attribute of self).
    maps = _binding_form_maps(tmp_path)
    assert maps["attribute_target"].get("w") == "Widget"
    # Nor is the KEYWORD of `Widget(self=1)` or the value pattern
    # `Widget.self`: neither names a local, so the seed must stay.
    assert maps["match_keyword_name"].get("w") == "Widget"
    assert maps["match_value_pattern"].get("w") == "Widget"


def test_a_rebinding_in_a_nested_scope_does_not_suppress_the_seed(
    tmp_path: Path,
) -> None:
    # `Inner.go` rebinds ITS OWN `self`; the enclosing method's receiver is
    # untouched and still types `w` (found by the bot's execution).
    maps = _binding_form_maps(tmp_path)
    assert maps["nested_scope_only"].get("w") == "Widget"


def test_only_a_bound_receiver_is_seeded(tmp_path: Path) -> None:
    # A staticmethod's `self`, a nested def's own `self` parameter, a
    # receiver rebound in the body and a first parameter not named self/cls
    # are caller-supplied values of unknown type: `w` must stay untyped (or
    # take the factory's type). The plain method types `w` from the class.
    maps = _method_maps(tmp_path)
    assert maps["plain"].get("w") == "Widget"
    assert "w" not in maps["static_self"]
    assert "w" not in maps["shadowing.inner"]
    assert maps["rebinding"].get("w") == "Banner", (
        "the factory's type must win over the class"
    )
    assert "w" not in maps["other_first"]


def test_the_seed_types_the_assigned_local_and_is_not_exported(tmp_path: Path) -> None:
    # The mechanism itself: with the class context, the method's map types
    # `w` from `self.parse()`; `self` and `cls` themselves are NOT left in
    # the map, nor anything expanded from them, so `self.m()` calls keep the
    # resolver's own policy for them (concrete sibling over abstract stub).
    # A module-level function's map has none of it.
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    repo = _build(
        tmp_path,
        _SELF_ASSIGN + "\n\ndef free() -> int:\n    return 1\n",
        _DECOY_CONTROL,
    )
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store, repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run(force=True)
    python = next(p for k, p in parsers.items() if str(k) == "python")
    tree = python.parse((repo / "app.py").read_bytes())
    class_node = next(
        n for n in tree.root_node.children if n.type == "class_definition"
    )
    body = class_node.child_by_field_name("body")
    run_node = next(
        n
        for n in body.children
        if n.type == "function_definition"
        and n.child_by_field_name("name").text == b"run"
    )
    free_node = next(
        n for n in tree.root_node.children if n.type == "function_definition"
    )
    ti = updater.factory.type_inference
    with_context = ti.build_local_variable_type_map(
        run_node, "proj.app", cs.SupportedLanguage.PYTHON, "proj.app.Parser"
    )
    assert with_context.get("w") == "Widget"
    assert not [k for k in with_context if k.split(".")[0] in ("self", "cls")]
    without_context = ti.build_local_variable_type_map(
        run_node, "proj.app", cs.SupportedLanguage.PYTHON, None
    )
    assert "w" not in without_context, "the seed is what types w"
    free_map = ti.build_local_variable_type_map(
        free_node, "proj.app", cs.SupportedLanguage.PYTHON, None
    )
    assert not {"self", "cls", "w"} & set(free_map)
