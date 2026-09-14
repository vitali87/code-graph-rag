"""A local binding of an imported module's name shadows the module.

Issue #1907. `helpers = supplied` (or a parameter, a loop target, a `case`
capture, ...) makes `helpers` local for the WHOLE function body, so
`helpers.make_pair()` can never reach `proj.helpers.make_pair`; the resolver
still linked it there because the two-part path read the module's import map
without asking whether the caller binds the receiver name itself.

Each defect test pairs with a control: the same body with the shadowing
binding removed, which MUST keep the edge, so a green means the binding was
honoured rather than the harness seeing nothing. A body-level
`from . import helpers` re-binds the name to the same module and keeps the
edge (the one binding form that is not a shadow).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_HELPERS = (
    "from .engine import Banner\n"
    "\n"
    "def make_pair() -> tuple[int, Banner]:\n"
    "    return (0, Banner())\n"
)
_ENGINE = "class Banner:\n    def render(self) -> int:\n        return 1\n"

# Every binding form Python treats as local for the whole body. Each body
# calls helpers.make_pair() on a receiver the function itself binds.
_SHADOWS = {
    "assignment": (
        "def use(supplied) -> int:\n"
        "    helpers = supplied\n"
        "    w = helpers.make_pair()\n"
        "    return w[0]\n"
    ),
    "assignment_after_call": (
        "def use(supplied) -> int:\n"
        "    w = helpers.make_pair()\n"
        "    helpers = supplied\n"
        "    return w[0]\n"
    ),
    "parameter": (
        "def use(helpers) -> int:\n    w = helpers.make_pair()\n    return w[0]\n"
    ),
    "for_target": (
        "def use(xs) -> int:\n"
        "    for helpers in xs:\n"
        "        w = helpers.make_pair()\n"
        "    return w[0]\n"
    ),
    "with_alias": (
        "def use(cm) -> int:\n"
        "    with cm as helpers:\n"
        "        w = helpers.make_pair()\n"
        "    return w[0]\n"
    ),
    "walrus": (
        "def use(supplied) -> int:\n"
        "    if (helpers := supplied):\n"
        "        w = helpers.make_pair()\n"
        "    return w[0]\n"
    ),
    "case_capture": (
        "def use(supplied) -> int:\n"
        "    match supplied:\n"
        "        case [helpers]:\n"
        "            w = helpers.make_pair()\n"
        "    return w[0]\n"
    ),
    "enclosing_def_local": (
        "def outer(supplied) -> int:\n"
        "    helpers = supplied\n"
        "    def use() -> int:\n"
        "        w = helpers.make_pair()\n"
        "        return w[0]\n"
        "    return use()\n"
    ),
}

_CONTROL = "def use(supplied) -> int:\n    w = helpers.make_pair()\n    return w[0]\n"

_REIMPORT = (
    "def use(supplied) -> int:\n"
    "    from . import helpers\n"
    "    w = helpers.make_pair()\n"
    "    return w[0]\n"
)


def _build(tmp_path: Path, body: str) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_ENGINE)
    (repo / "helpers.py").write_text(_HELPERS)
    (repo / "app.py").write_text("from . import helpers\n\n" + body)
    return repo


def _calls_from_use(repo: Path) -> set[str]:
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    return {
        str(tgt)
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value and str(src).endswith(".use")
    }


@pytest.mark.parametrize("shape", sorted(_SHADOWS))
def test_a_local_binding_shadows_the_imported_module(
    tmp_path: Path, shape: str
) -> None:
    calls = _calls_from_use(_build(tmp_path, _SHADOWS[shape]))
    assert "proj.helpers.make_pair" not in calls, (
        f"{shape}: `helpers` is local for the whole body of use(), so "
        f"helpers.make_pair() cannot reach the module; got {sorted(calls)}"
    )


def test_the_unshadowed_call_keeps_its_edge(tmp_path: Path) -> None:
    """The control: without the local binding the edge exists, so the tests
    above measure suppression, not an absent harness."""
    calls = _calls_from_use(_build(tmp_path, _CONTROL))
    assert "proj.helpers.make_pair" in calls, sorted(calls)


def test_a_body_level_reimport_is_not_a_shadow(tmp_path: Path) -> None:
    calls = _calls_from_use(_build(tmp_path, _REIMPORT))
    assert "proj.helpers.make_pair" in calls, sorted(calls)


_OPTIONAL_IMPORT = (
    "def use() -> int:\n"
    "    try:\n"
    "        from . import helpers\n"
    "    except ImportError:\n"
    "        helpers = None\n"
    "    return helpers.make_pair()[0]\n"
)

_HANDLER_GUARDS_ANOTHER_IMPORT = (
    "def use() -> int:\n"
    "    try:\n"
    "        from . import engine\n"
    "    except ImportError:\n"
    "        helpers = None\n"
    "    return helpers.make_pair()[0]\n"
)


def test_an_optional_import_fallback_is_not_a_shadow(tmp_path: Path) -> None:
    """`except ImportError: helpers = None` guarding `from . import helpers`
    binds the name, but on the path where it resolves to anything reachable
    the try body's import bound it to the module. Unioning both branches
    dropped an edge `main` kept."""
    calls = _calls_from_use(_build(tmp_path, _OPTIONAL_IMPORT))
    assert "proj.helpers.make_pair" in calls, sorted(calls)


_GLOBAL_DECLARED = (
    "def use() -> int:\n"
    "    global helpers\n"
    "    w = helpers.make_pair()\n"
    "    helpers = None\n"
    "    return w[0]\n"
)

_NONLOCAL_DECLARED = (
    "def outer():\n"
    "    def use() -> int:\n"
    "        nonlocal helpers\n"
    "        return helpers.make_pair()[0]\n"
    "    return use\n"
)

_GLOBAL_IN_A_NESTED_DEF = (
    "def use(supplied) -> int:\n"
    "    def inner():\n"
    "        global helpers\n"
    "        helpers = None\n"
    "    helpers = supplied\n"
    "    return helpers.make_pair()[0]\n"
)


@pytest.mark.parametrize("shape", ["global_declared", "nonlocal_declared"])
def test_a_declared_non_local_name_is_not_a_shadow(tmp_path: Path, shape: str) -> None:
    """`global helpers` makes every assignment in the body write the module's
    binding, so the name is not local and the imported module stays
    reachable. Treating the assignment as a shadow dropped an edge `main`
    resolves (Greptile on #1907)."""
    body = {
        "global_declared": _GLOBAL_DECLARED,
        "nonlocal_declared": _NONLOCAL_DECLARED,
    }[shape]
    calls = _calls_from_use(_build(tmp_path, body))
    assert "proj.helpers.make_pair" in calls, sorted(calls)


def test_a_declaration_in_a_nested_def_does_not_reach_its_parent(
    tmp_path: Path,
) -> None:
    """The discriminating control: a `global` inside a nested function binds
    in THAT scope. The enclosing function's own `helpers = supplied` is still
    an ordinary shadow, so a green above cannot come from exempting any body
    that merely contains the keyword."""
    calls = _calls_from_use(_build(tmp_path, _GLOBAL_IN_A_NESTED_DEF))
    assert "proj.helpers.make_pair" not in calls, sorted(calls)


def test_a_handler_guarding_another_import_still_shadows(tmp_path: Path) -> None:
    """The discriminating case: the exemption is for a handler guarding an
    import of THIS name. Guarding some other import leaves `helpers = None`
    an ordinary shadow, so a green above cannot come from exempting every
    except-clause binding."""
    calls = _calls_from_use(_build(tmp_path, _HANDLER_GUARDS_ANOTHER_IMPORT))
    assert "proj.helpers.make_pair" not in calls, sorted(calls)


_SHADOWED_SIBLING = (
    "def shadowed(supplied) -> int:\n"
    "    helpers = supplied\n"
    "    w = helpers.make_pair()\n"
    "    return w[0]\n"
)


@pytest.mark.parametrize("order", ["shadowed_first", "use_first"])
def test_siblings_do_not_share_an_answer_through_the_cache(
    tmp_path: Path, order: str
) -> None:
    """The resolver caches (call_name, module_qn) when the caller has no
    typed locals, and both callers here have none. Whichever resolves first,
    the unshadowed one keeps its edge and the shadowed one gets none: the
    shadow check must run before the cache is read, or `use` resolved first
    hands `shadowed` its cached hit."""
    parts = [_SHADOWED_SIBLING, _CONTROL]
    if order == "use_first":
        parts.reverse()
    repo = _build(tmp_path, "\n".join(parts))
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    by_caller: dict[str, set[str]] = {}
    for _sl, src, rel, _tl, tgt in store.edges:
        if rel == cs.RelationshipType.CALLS.value:
            by_caller.setdefault(str(src).rsplit(".", 1)[-1], set()).add(str(tgt))
    assert "proj.helpers.make_pair" in by_caller.get("use", set()), by_caller
    assert "proj.helpers.make_pair" not in by_caller.get("shadowed", set()), by_caller


def test_a_typed_shadow_resolves_through_its_type(tmp_path: Path) -> None:
    """`helpers = Banner()` shadows the module too, but the local is typed,
    so `helpers.render()` is Banner.render; the shadow rule must not turn a
    typed local into an unknown value. (An annotation-only local,
    `helpers: Banner = supplied`, is not typed by the map today, so it is an
    unknown value here and gets no edge; pre-existing, out of scope.)"""
    body = (
        "from .engine import Banner\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    helpers = Banner()\n"
        "    return helpers.render()\n"
    )
    calls = _calls_from_use(_build(tmp_path, body))
    assert "proj.engine.Banner.render" in calls, sorted(calls)
    assert "proj.helpers.make_pair" not in calls, sorted(calls)
