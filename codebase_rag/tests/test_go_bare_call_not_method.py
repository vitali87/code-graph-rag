"""Issue #3783: a Go call with no selector never names a method.

`copyToLayer(u.base, u.layer, name)` inside `func (u *CacheOnReadFs)
copyToLayer(name string) error` calls the package function `copyToLayer`
(spf13/afero). Go fills in no receiver: a bare `f(...)` reaches a local
function value, then the package's functions, then a dot-import's; a method
is reached only through a selector (`u.copyToLayer(...)`). The scope walk
stepped from the method into its type and bound the call to the method
itself, `exact`, so the package function lost its caller and `dead-code`
listed it.

Each case runs on tree-sitter alone and with the go/types helper, whose
facts already answer the package function, when Go is installed.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.tests.conftest import get_relationships, run_updater

# The runs on the real go/types helper share its build, and the fingerprint's
# tool and grammar versions are read once (conftest).
pytestmark = pytest.mark.usefixtures(
    "one_go_helper_per_session", "fingerprint_inputs_once_per_session"
)

GO_MOD = "module example.com/proj\n\ngo 1.22\n"

UTIL = (
    "package layer\n"
    "\n"
    "func copyToLayer(base, layer string, name string) error { return nil }\n"
)
CACHE = (
    "package layer\n"
    "\n"
    "type Cache struct{ base, layer string }\n"
    "\n"
    "func (u *Cache) copyToLayer(name string) error {\n"
    "\treturn copyToLayer(u.base, u.layer, name)\n"
    "}\n"
    "\n"
    "func (u *Cache) Open(name string) error {\n"
    "\treturn u.copyToLayer(name)\n"
    "}\n"
    "\n"
    "func Use(c *Cache) error {\n"
    '\treturn c.copyToLayer("x")\n'
    "}\n"
    "\n"
    "func (u *Cache) Retry() error {\n"
    "\tRetry := func() error { return nil }\n"
    "\treturn Retry()\n"
    "}\n"
    "\n"
    "type Version byte\n"
    "\n"
    "func (u *Cache) Version() Version {\n"
    "\treturn Version(len(u.base))\n"
    "}\n"
)
STORE = "package store\n\nfunc Load() error { return nil }\n"
DOT = (
    "package layer\n"
    "\n"
    'import . "example.com/proj/store"\n'
    "\n"
    "type Loader struct{}\n"
    "\n"
    "func (l Loader) Load() error {\n"
    "\treturn Load()\n"
    "}\n"
    "\n"
    "func Free() error { return Load() }\n"
)
FILES = {
    "go.mod": GO_MOD,
    "layer/util.go": UTIL,
    "layer/cache.go": CACHE,
    "layer/dot.go": DOT,
    "store/load.go": STORE,
}

METHOD = "proj.layer.cache.Cache.copyToLayer"
FUNCTION = "proj.layer.util.copyToLayer"


def _write(root: Path) -> None:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _set_frontend(
    root: Path, monkeypatch: pytest.MonkeyPatch, frontend: cs.GoFrontend
) -> None:
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", frontend)
    if frontend != cs.GoFrontend.GOTYPES:
        return
    from codebase_rag.parsers.go_frontend import (
        go_frontend_available,
        run_go_frontend,
    )

    if shutil.which("go") is None or not go_frontend_available():
        pytest.skip("go toolchain not available")
    if not run_go_frontend(root).call_sites:
        pytest.skip("gotypes frontend could not build in this environment")


@pytest.fixture(params=[cs.GoFrontend.TREESITTER, cs.GoFrontend.GOTYPES])
def calls(
    request: pytest.FixtureRequest,
    temp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[tuple[str, str], set[str]]:
    """{(caller qn, callee qn): resolutions} for every CALLS edge."""
    root = temp_repo / "proj"
    _write(root)
    _set_frontend(root, monkeypatch, request.param)
    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing="go")
    edges: dict[tuple[str, str], set[str]] = {}
    for c in get_relationships(ingestor, cs.RelationshipType.CALLS):
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        key = (str(c.args[0][2]), str(c.args[2][2]))
        edges.setdefault(key, set()).add(str(props.get(cs.KEY_RESOLUTION)))
    return edges


def test_a_bare_call_in_a_method_binds_the_package_function(
    calls: dict[tuple[str, str], set[str]],
) -> None:
    assert calls[(METHOD, FUNCTION)] == {cs.EdgeResolution.EXACT}, calls
    assert (METHOD, METHOD) not in calls, calls


def test_the_method_is_reached_through_its_selector(
    calls: dict[tuple[str, str], set[str]],
) -> None:
    # Negatives of the rule: `u.copyToLayer(name)` and `c.copyToLayer("x")`
    # name the method, from a sibling method and from a package function.
    assert calls[("proj.layer.cache.Cache.Open", METHOD)] == {
        cs.EdgeResolution.EXACT
    }, calls
    assert calls[("proj.layer.cache.Use", METHOD)] == {cs.EdgeResolution.EXACT}, calls
    assert not any(
        callee == FUNCTION
        for caller, callee in calls
        if caller in {"proj.layer.cache.Cache.Open", "proj.layer.cache.Use"}
    ), calls


def test_a_bare_call_in_a_method_binds_a_dot_imported_function(
    calls: dict[tuple[str, str], set[str]],
) -> None:
    # As it does from a package function of the same file.
    loader, load = "proj.layer.dot.Loader.Load", "proj.store.load.Load"
    assert calls[(loader, load)] == calls[("proj.layer.dot.Free", load)], calls
    assert (loader, loader) not in calls, calls


def test_a_local_function_value_named_like_the_method_is_not_the_method(
    calls: dict[tuple[str, str], set[str]],
) -> None:
    # `Retry := func() error {...}; Retry()` calls the local value, which the
    # graph has no node for, and the type checker gives no fact for a call
    # through a variable: no first-party edge, and none to the method.
    retry = "proj.layer.cache.Cache.Retry"
    assert not any(caller == retry for caller, _ in calls), calls


def test_a_conversion_to_a_type_named_like_the_method_is_not_a_call(
    calls: dict[tuple[str, str], set[str]],
) -> None:
    # `Version(x)` inside `func (u *Cache) Version() Version` converts to the
    # package's `type Version byte` (google/uuid's `Time`, `Domain` and
    # `Version`): no call at all, so no CALLS edge, and none to the method.
    version = "proj.layer.cache.Cache.Version"
    assert not any(caller == version for caller, _ in calls), calls
