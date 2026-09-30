"""`with X() as v` / `async with` and `v = mod.Class()` type `v` (issue #2558).

Two of the most common ways to obtain an object in Python never gave the
variable a type: the `as` target of a with statement (the type pass only
looked at assignments and `for` targets), and a class constructed through a
module attribute (`pkg.Client()`, `pkg._client.Client()`), whose dotted text
was stored raw and never resolved through the import map or the package's
re-exports. A method call on such a variable then got no edge, or fell to the
name-only fallback, which bound `client.send()` to the free function
`pkg._api.send` in another module.

The with target takes the type `X.__enter__` (`__aenter__` for `async with`)
gives it: its declared return type, or what its `return` statements return,
where `return self` and `-> Self` mean the class being entered (a subclass
that inherits the method included). Only when no indexed class in X's bases
defines the method (X is external, or inherits it from an unindexed base such
as `contextlib.AbstractContextManager`, whose `__enter__` returns `self`) is
the target typed as X itself. An `__enter__` that returns something the pass
cannot read leaves the target untyped rather than guessing X.

Some assertions read the type map directly: the fallback can pick the right
method by coincidence, which would let an edge-level check pass either way.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "proj"
CLIENT = f"{PROJECT}.pkg._client.Client"
ASYNC_CLIENT = f"{PROJECT}.pkg._client.AsyncClient"
API_SEND = f"{PROJECT}.pkg._api.send"

# The issue's package: `Client` is defined in a private module and
# re-exported by the package, next to a same-named free function `send`.
PKG = {
    "pkg/__init__.py": (
        "from ._client import Client, AsyncClient\nfrom ._api import send\n"
    ),
    "pkg/_client.py": (
        "class Client:\n"
        '    def __enter__(self) -> "Client":\n'
        "        return self\n"
        "    def __exit__(self, *exc: object) -> None:\n"
        "        pass\n"
        "    def send(self, request: str) -> str:\n"
        "        return request\n"
        "\n"
        "class AsyncClient:\n"
        '    async def __aenter__(self) -> "AsyncClient":\n'
        "        return self\n"
        "    async def __aexit__(self, *exc: object) -> None:\n"
        "        pass\n"
        "    async def send(self, request: str) -> str:\n"
        "        return request\n"
    ),
    "pkg/_api.py": "def send(request: str) -> str:\n    return request\n",
}

# Context managers whose `__enter__` says different things about the value
# it hands to the `as` target.
MANAGERS = (
    "import contextlib\n"
    "\n"
    "class Session:\n"
    "    def send(self) -> int:\n"
    "        return 1\n"
    "    def fork(self) -> Other:\n"
    "        return Other()\n"
    "\n"
    "class Pool:\n"
    "    def __enter__(self) -> Session:\n"
    "        return Session()\n"
    "    def __exit__(self, *exc: object) -> None:\n"
    "        pass\n"
    "    def send(self) -> int:\n"
    "        return 2\n"
    "\n"
    "class Factory:\n"
    "    def __enter__(self):\n"
    "        return Session()\n"
    "    def __exit__(self, *exc):\n"
    "        pass\n"
    "    def send(self) -> int:\n"
    "        return 3\n"
    "\n"
    "class Base:\n"
    "    def __enter__(self):\n"
    "        return self\n"
    "    def __exit__(self, *exc):\n"
    "        pass\n"
    "    def send(self) -> int:\n"
    "        return 4\n"
    "\n"
    "class Sub(Base):\n"
    "    def send(self) -> int:\n"
    "        return 5\n"
    "\n"
    "def make_sub() -> Sub:\n"
    "    return Sub()\n"
    "\n"
    "class SelfBase:\n"
    '    def __enter__(self) -> "Self":\n'
    "        return self\n"
    "    def __exit__(self, *exc):\n"
    "        pass\n"
    "\n"
    "class SelfSub(SelfBase):\n"
    "    def send(self) -> int:\n"
    "        return 6\n"
    "\n"
    "class Managed(contextlib.AbstractContextManager):\n"
    "    def __exit__(self, *exc):\n"
    "        pass\n"
    "    def send(self) -> int:\n"
    "        return 7\n"
    "\n"
    "class Holder:\n"
    "    def __init__(self, conn):\n"
    "        self._conn = conn\n"
    "    def __enter__(self):\n"
    "        return self._conn\n"
    "    def __exit__(self, *exc):\n"
    "        pass\n"
    "    def send(self) -> int:\n"
    "        return 8\n"
    "\n"
    "class Silent:\n"
    "    def __enter__(self) -> None:\n"
    "        pass\n"
    "    def __exit__(self, *exc):\n"
    "        pass\n"
    "    def send(self) -> int:\n"
    "        return 9\n"
    "\n"
    "class Pair:\n"
    "    def __enter__(self):\n"
    "        return self\n"
    "    def __exit__(self, *exc):\n"
    "        pass\n"
    "    def send(self) -> int:\n"
    "        return 10\n"
    "\n"
    "class Other:\n"
    "    def send(self) -> int:\n"
    "        return 11\n"
    "\n"
    "class Reader:\n"
    "    def read(self) -> str:\n"
    '        return ""\n'
)


def _build(tmp_path: Path, source: str, managers: bool) -> GraphUpdater:
    repo = tmp_path / PROJECT
    files = {**PKG, "app.py": source}
    if managers:
        files["managers.py"] = MANAGERS
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=_StatefulIngestor(), repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run(force=True)
    return updater


def _calls(
    tmp_path: Path, source: str, managers: bool = True
) -> dict[str, dict[str, str]]:
    """{caller's simple name: {callee qn: resolution}} for `app.py`'s CALLS.

    Without `managers` the repository is the issue's package alone, so an
    untyped `client.send()` falls back to `pkg._api.send` exactly as reported.
    """
    ingestor = _build(tmp_path, source, managers).ingestor
    assert isinstance(ingestor, _StatefulIngestor)
    return _app_calls(ingestor)


def _app_calls(ingestor: _StatefulIngestor) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for edge in ingestor.keyed_edges:
        _src_label, src, rel, _dst_label, dst, _site = edge
        if rel != cs.RelationshipType.CALLS or not str(src).startswith(
            f"{PROJECT}.app."
        ):
            continue
        resolution = ingestor.edge_props.get(edge, {}).get(cs.KEY_RESOLUTION)
        found.setdefault(str(src).rsplit(".", 1)[-1], {})[str(dst)] = str(resolution)
    return found


def _functions(node: Node) -> Iterator[Node]:
    for child in node.children:
        if child.type == cs.TS_PY_FUNCTION_DEFINITION:
            yield child
        yield from _functions(child)


def _local_types(tmp_path: Path, source: str, function: str) -> dict[str, str]:
    updater = _build(tmp_path, source, managers=True)
    parsers, _ = load_parsers()
    tree = parsers[cs.SupportedLanguage.PYTHON].parse(source.encode())
    node = next(
        candidate
        for candidate in _functions(tree.root_node)
        if (candidate.child_by_field_name("name").text or b"").decode() == function
    )
    engine = updater.factory.type_inference.python_type_inference
    return engine.build_local_variable_type_map(node, f"{PROJECT}.app")


EXACT = str(cs.EdgeResolution.EXACT)


# --- the issue's table ---------------------------------------------------------


def test_with_on_an_imported_class_binds_the_method_exactly(tmp_path: Path) -> None:
    calls = _calls(
        tmp_path,
        "from pkg import Client\n\n"
        "def use():\n"
        "    with Client() as client:\n"
        '        client.send("b")\n',
        managers=False,
    )
    assert calls["use"].get(f"{CLIENT}.send") == EXACT
    assert API_SEND not in calls["use"]


def test_with_on_a_module_attribute_class_binds_the_method_exactly(
    tmp_path: Path,
) -> None:
    calls = _calls(
        tmp_path,
        "import pkg\n\n"
        "def use():\n"
        "    with pkg.Client() as client:\n"
        '        client.send("a")\n',
        managers=False,
    )
    assert calls["use"].get(f"{CLIENT}.send") == EXACT
    assert API_SEND not in calls["use"]


def test_async_with_types_the_target_from_aenter(tmp_path: Path) -> None:
    calls = _calls(
        tmp_path,
        "import pkg\n\n"
        "async def use():\n"
        "    async with pkg.AsyncClient() as client:\n"
        '        await client.send("d")\n',
        managers=False,
    )
    assert calls["use"].get(f"{ASYNC_CLIENT}.send") == EXACT
    assert API_SEND not in calls["use"]


def test_with_on_a_class_aliased_from_its_defining_module(tmp_path: Path) -> None:
    calls = _calls(
        tmp_path,
        "def use():\n"
        "    from pkg._client import Client as C\n"
        "    with C() as client:\n"
        '        client.send("g")\n',
        managers=False,
    )
    assert calls["use"].get(f"{CLIENT}.send") == EXACT
    assert API_SEND not in calls["use"]


def test_module_attribute_construction_through_a_reexport(tmp_path: Path) -> None:
    calls = _calls(
        tmp_path,
        "import pkg\n\ndef use():\n    client = pkg.Client()\n    client.send('c')\n",
        managers=False,
    )
    assert calls["use"].get(f"{CLIENT}.send") == EXACT


def test_module_attribute_construction_through_the_defining_module(
    tmp_path: Path,
) -> None:
    # `import pkg._client` is recorded as `pkg -> proj.pkg._client`, so the
    # dotted path repeats the module's tail.
    source = (
        "import pkg, pkg._client\n\n"
        "def via_defining_module():\n"
        "    client = pkg._client.Client()\n"
        "    client.send('i')\n\n"
        "def via_reexport():\n"
        "    client = pkg.Client()\n"
        "    client.send('h')\n"
    )
    calls = _calls(tmp_path, source, managers=False)
    for caller in ("via_defining_module", "via_reexport"):
        assert calls[caller].get(f"{CLIENT}.send") == EXACT, caller
        assert API_SEND not in calls[caller], caller


def test_module_attribute_construction_stores_the_class_qn(tmp_path: Path) -> None:
    types = _local_types(
        tmp_path,
        "import pkg\n\ndef use():\n    client = pkg.Client()\n    client.send('c')\n",
        "use",
    )
    assert types["client"] == CLIENT


# --- what `__enter__` returns decides ------------------------------------------


def test_enter_annotated_with_another_class_types_the_target_as_that_class(
    tmp_path: Path,
) -> None:
    calls = _calls(
        tmp_path,
        "from managers import Pool\n\n"
        "def use():\n"
        "    with Pool() as session:\n"
        "        session.send()\n",
    )
    assert calls["use"].get(f"{PROJECT}.managers.Session.send") == EXACT
    assert f"{PROJECT}.managers.Pool.send" not in calls["use"]


def test_enter_returning_a_constructed_class_types_the_target_as_that_class(
    tmp_path: Path,
) -> None:
    types = _local_types(
        tmp_path,
        "from managers import Factory\n\n"
        "def use():\n"
        "    with Factory() as session:\n"
        "        session.send()\n",
        "use",
    )
    assert types["session"] == f"{PROJECT}.managers.Session"


def test_inherited_enter_returning_self_types_the_target_as_the_subclass(
    tmp_path: Path,
) -> None:
    calls = _calls(
        tmp_path,
        "from managers import Sub\n\n"
        "def use():\n"
        "    with Sub() as sub:\n"
        "        sub.send()\n",
    )
    assert calls["use"].get(f"{PROJECT}.managers.Sub.send") == EXACT
    assert f"{PROJECT}.managers.Base.send" not in calls["use"]


def test_inherited_enter_annotated_self_types_the_target_as_the_subclass(
    tmp_path: Path,
) -> None:
    calls = _calls(
        tmp_path,
        "from managers import SelfSub\n\n"
        "def use():\n"
        "    with SelfSub() as sub:\n"
        "        sub.send()\n",
    )
    assert calls["use"].get(f"{PROJECT}.managers.SelfSub.send") == EXACT


def test_enter_from_an_unindexed_base_falls_back_to_the_class(
    tmp_path: Path,
) -> None:
    # `contextlib.AbstractContextManager.__enter__` returns self; no indexed
    # class in Managed's bases defines `__enter__`, so the target is Managed.
    calls = _calls(
        tmp_path,
        "from managers import Managed\n\n"
        "def use():\n"
        "    with Managed() as managed:\n"
        "        managed.send()\n",
    )
    assert calls["use"].get(f"{PROJECT}.managers.Managed.send") == EXACT


def test_each_item_of_a_parenthesised_with_types_its_own_target(
    tmp_path: Path,
) -> None:
    types = _local_types(
        tmp_path,
        "from managers import Pool, Sub\n\n"
        "def use():\n"
        "    with (Pool() as session, Sub() as sub):\n"
        "        session.send()\n"
        "        sub.send()\n",
        "use",
    )
    assert types["session"] == f"{PROJECT}.managers.Session"
    assert types["sub"] == "Sub"


def test_a_manager_typed_by_a_factory_is_entered(tmp_path: Path) -> None:
    # `sub` is typed only by the complex assignment pass, which runs after
    # the with targets are first typed; the target is retried after it.
    types = _local_types(
        tmp_path,
        "from managers import make_sub\n\n"
        "def use():\n"
        "    sub = make_sub()\n"
        "    with sub as entered:\n"
        "        entered.send()\n",
        "use",
    )
    assert types["entered"] == f"{PROJECT}.managers.Sub"


def test_a_local_assigned_from_the_target_is_typed(tmp_path: Path) -> None:
    types = _local_types(
        tmp_path,
        "from managers import Pool\n\n"
        "def use():\n"
        "    with Pool() as session:\n"
        "        forked = session.fork()\n"
        "    forked.send()\n",
        "use",
    )
    assert types["forked"] == "Other"


def test_a_with_binding_after_an_assignment_takes_over(tmp_path: Path) -> None:
    types = _local_types(
        tmp_path,
        "from managers import Other, Sub\n\n"
        "def use():\n"
        "    v = Other()\n"
        "    with Sub() as v:\n"
        "        v.send()\n",
        "use",
    )
    assert types["v"] == "Sub"


def test_an_external_class_types_the_target_and_stops_the_name_fallback(
    tmp_path: Path,
) -> None:
    # The same treatment `writer = pd.ExcelWriter(p)` already gets: a known
    # external type, so `writer.send()` is not bound by name to a first-party
    # `send`.
    calls = _calls(
        tmp_path,
        "import pandas as pd\n\n"
        "def use(path):\n"
        "    with pd.ExcelWriter(path) as writer:\n"
        "        writer.send()\n",
    )
    assert not any(callee.endswith(".send") for callee in calls.get("use", {}))


# --- negatives: what must stay as it was -----------------------------------------


def test_open_leaves_the_target_untyped(tmp_path: Path) -> None:
    source = (
        "from managers import Reader\n\n"
        "def use(path):\n"
        "    with open(path) as f:\n"
        "        return f.read()\n"
    )
    assert "f" not in _local_types(tmp_path, source, "use")


def test_open_does_not_bind_to_a_first_party_method_exactly(tmp_path: Path) -> None:
    calls = _calls(
        tmp_path,
        "from managers import Reader\n\n"
        "def use(path):\n"
        "    with open(path) as f:\n"
        "        return f.read()\n",
    )
    assert calls.get("use", {}).get(f"{PROJECT}.managers.Reader.read") != EXACT


def test_enter_returning_an_unreadable_value_is_not_the_class(tmp_path: Path) -> None:
    # `return self._conn` is not the Holder; guessing Holder would bind
    # `conn.send()` to Holder.send.
    types = _local_types(
        tmp_path,
        "from managers import Holder\n\n"
        "def use(raw):\n"
        "    with Holder(raw) as conn:\n"
        "        conn.send()\n",
        "use",
    )
    assert "conn" not in types


def test_enter_returning_none_is_not_the_class(tmp_path: Path) -> None:
    types = _local_types(
        tmp_path,
        "from managers import Silent\n\n"
        "def use():\n"
        "    with Silent() as nothing:\n"
        "        nothing.send()\n",
        "use",
    )
    assert "nothing" not in types


def test_only_the_item_with_the_alias_types_it(tmp_path: Path) -> None:
    # `with a, b as v` binds v to b's `__enter__`, never a's.
    types = _local_types(
        tmp_path,
        "from managers import Pool, Sub\n\n"
        "def use():\n"
        "    with Pool(), Sub() as v:\n"
        "        v.send()\n",
        "use",
    )
    assert types["v"] == "Sub"


def test_a_destructuring_target_is_not_typed(tmp_path: Path) -> None:
    types = _local_types(
        tmp_path,
        "from managers import Pair\n\n"
        "def use():\n"
        "    with Pair() as (first, second):\n"
        "        first.send()\n",
        "use",
    )
    assert "first" not in types
    assert "second" not in types


def test_a_name_rebound_later_keeps_the_later_type(tmp_path: Path) -> None:
    source = (
        "from managers import Other, Sub\n\n"
        "def use():\n"
        "    with Sub() as v:\n"
        "        pass\n"
        "    v = Other()\n"
        "    v.send()\n"
    )
    assert _local_types(tmp_path, source, "use")["v"] == "Other"


def test_a_name_rebound_later_calls_the_later_class(tmp_path: Path) -> None:
    calls = _calls(
        tmp_path,
        "from managers import Other, Sub\n\n"
        "def use():\n"
        "    with Sub() as v:\n"
        "        pass\n"
        "    v = Other()\n"
        "    v.send()\n",
    )
    assert calls["use"].get(f"{PROJECT}.managers.Other.send") == EXACT
    assert f"{PROJECT}.managers.Sub.send" not in calls["use"]


def test_a_with_in_a_nested_def_does_not_type_the_outer_name(tmp_path: Path) -> None:
    types = _local_types(
        tmp_path,
        "from managers import Sub\n\n"
        "def outer(v):\n"
        "    def inner():\n"
        "        with Sub() as v:\n"
        "            v.send()\n"
        "    v.send()\n",
        "outer",
    )
    assert "v" not in types


def test_an_external_module_attribute_constructor_stays_as_written(
    tmp_path: Path,
) -> None:
    types = _local_types(
        tmp_path,
        "import pandas as pd\n\ndef use(rows):\n    df = pd.DataFrame(rows)\n",
        "use",
    )
    assert types["df"] == "pd.DataFrame"


# --- incremental: the entered type is read from another file ----------------------


def test_reingesting_only_the_caller_types_the_target_like_a_clean_index(
    tmp_path: Path,
) -> None:
    # A fresh updater re-parses app.py alone; `Pool.__enter__` lives in
    # managers.py, which it does not re-parse, and must still decide the type.
    updater = _build(tmp_path, "def use():\n    return 1\n", managers=True)
    store = updater.ingestor
    assert isinstance(store, _StatefulIngestor)
    (tmp_path / PROJECT / "app.py").write_text(
        "from managers import Pool\n\n"
        "def use():\n"
        "    with Pool() as session:\n"
        "        session.send()\n",
        encoding="utf-8",
    )
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=tmp_path / PROJECT,
        parsers=parsers,
        queries=queries,
    ).reingest(["app.py"])
    store.flush_all()

    assert _app_calls(store)["use"] == {f"{PROJECT}.managers.Session.send": EXACT}
