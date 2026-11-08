"""Issue #2928: a receiver built from a star-re-exported class is typed.

`client = pkg.Client()` and `with pkg.Client() as client` typed `client`
when `pkg/__init__.py` re-exported `Client` by name (#2558), but not when it
re-exported it with `from ._client import *` (httpx does): the re-export
walk only looked the name up directly, and a star import is one wildcard
entry, so every method call on `client` got no edge.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

CLIENT = """\
__all__ = ["Client"]


class BaseClient:
    def build_request(self, method, url):
        return (method, url)


class Client(BaseClient):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def send(self, request):
        return request


class _Hidden:
    def send(self, request):
        return request
"""

APP = """\
import explicit
import star
import chain


def via_explicit():
    client = explicit.Client()
    client.send(client.build_request("GET", "/"))


def via_star():
    client = star.Client()
    client.send(client.build_request("GET", "/"))


def via_star_with():
    with star.Client() as client:
        client.send(client.build_request("GET", "/"))


def via_star_chain():
    client = chain.Client()
    client.send(client.build_request("GET", "/"))


def via_private_name():
    hidden = star._Hidden()
    hidden.send(1)
"""


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("stardemo") / "stardemo"
    for package in ("explicit", "star"):
        _write(root, f"{package}/_client.py", CLIENT)
    _write(root, "explicit/__init__.py", "from ._client import Client\n")
    _write(root, "star/__init__.py", "from ._client import *\n")
    _write(root, "chain/__init__.py", "from .sub import *\n")
    _write(root, "chain/sub/__init__.py", "from ._impl import *\n")
    _write(root, "chain/sub/_impl.py", CLIENT)
    _write(root, "app.py", APP)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> set[str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix)
        for src, rel, dst, _props in graph.edges
        if rel == cs.RelationshipType.CALLS and src == f"{prefix}app.{caller}"
    }


@pytest.mark.parametrize(
    ("caller", "package"),
    [
        ("via_star", "star._client"),
        ("via_star_with", "star._client"),
        ("via_star_chain", "chain.sub._impl"),
    ],
    ids=["assignment", "with", "two-level-chain"],
)
def test_a_star_re_exported_receiver_binds_its_methods(
    graph: RecordedGraph, caller: str, package: str
) -> None:
    callees = _callees(graph, caller)
    assert f"{package}.Client.send" in callees
    assert f"{package}.BaseClient.build_request" in callees


# Binding order and `__all__` (bot review on PR #2989): Python keeps a name's
# last binding, star or named, and a star binds only what its module's
# `__all__` lists when it has one.
KLASS = "class Client:\n    def send(self, request):\n        return request\n"
HIDDEN_KLASS = '__all__ = ["Other"]\n\n\nclass Other:\n    pass\n\n\n' + KLASS
LISTED = '__all__ = ["Client"]\n\n\n' + KLASS

ORDER_APP = """\
import later_star
import star_after_named
import named_after_star
import excluded
import excluded_only
import listed


def via_later_star():
    client = later_star.Client()
    client.send(1)


def via_star_after_named():
    client = star_after_named.Client()
    client.send(1)


def via_named_after_star():
    client = named_after_star.Client()
    client.send(1)


def via_excluded():
    client = excluded.Client()
    client.send(1)


def via_excluded_only():
    client = excluded_only.Client()
    client.send(1)


def via_listed():
    client = listed.Client()
    client.send(1)


def via_repeated_star():
    client = repeated_star.Client()
    client.send(1)


def via_named_rebound_past_a_star():
    client = named_rebound_past_a_star.Client()
    client.send(1)


def via_star_repeated_past_a_named():
    client = star_repeated_past_a_named.Client()
    client.send(1)


def via_plain_import_after_star():
    client = plain_import_after_star.Client()
    client.send(1)


def via_external_later_star():
    client = external_later_star.Client()
    client.send(1)


def via_later_star_without_it():
    client = later_star_without_it.Client()
    client.send(1)
"""

# Python keeps a name's last binding in source order, but the import map
# keeps a re-bound key where it was first bound and records plain imports
# before from-imports. A later star whose module binds the name to
# something outside the project still takes it (CodeRabbit, PR #2989).
LATER_ORDER_PACKAGES = {
    "repeated_star": "from ._a import *\nfrom ._b import *\nfrom ._a import *\n",
    "named_rebound_past_a_star": (
        "from ._a import Client\nfrom ._b import *\nfrom ._c import Client\n"
    ),
    "star_repeated_past_a_named": (
        "from ._a import *\nfrom ._b import Client\nfrom ._a import *\n"
    ),
    "plain_import_after_star": "from ._a import *\nimport httpx as Client\n",
    "external_later_star": "from ._a import *\nfrom ._b import *\n",
    "later_star_without_it": "from ._a import *\nfrom ._b import *\n",
}
LATER_ORDER_B = {
    "external_later_star": "from httpx import Client\n",
    "later_star_without_it": "from httpx import Other\n",
}


@pytest.fixture(scope="module")
def order_graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("starorder") / "starorder"
    for package in ("later_star", "star_after_named", "named_after_star", "excluded"):
        _write(root, f"{package}/_a.py", KLASS)
        _write(root, f"{package}/_b.py", KLASS)
    _write(root, "later_star/__init__.py", "from ._a import *\nfrom ._b import *\n")
    _write(
        root,
        "star_after_named/__init__.py",
        "from ._a import Client\nfrom ._b import *\n",
    )
    _write(
        root,
        "named_after_star/__init__.py",
        "from ._b import *\nfrom ._a import Client\n",
    )
    _write(root, "excluded/_a.py", HIDDEN_KLASS)
    _write(root, "excluded/__init__.py", "from ._b import *\nfrom ._a import *\n")
    _write(root, "excluded_only/_a.py", HIDDEN_KLASS)
    _write(root, "excluded_only/__init__.py", "from ._a import *\n")
    _write(root, "listed/_impl.py", LISTED)
    _write(root, "listed/__init__.py", "from ._impl import *\n")
    for package, init in LATER_ORDER_PACKAGES.items():
        for module in ("_a", "_b", "_c"):
            _write(root, f"{package}/{module}.py", KLASS)
        if package in LATER_ORDER_B:
            _write(root, f"{package}/_b.py", LATER_ORDER_B[package])
        _write(root, f"{package}/__init__.py", init)
    _write(
        root,
        "app.py",
        "".join(f"import {package}\n" for package in LATER_ORDER_PACKAGES) + ORDER_APP,
    )
    return _index(root, MagicMock())


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("via_later_star", "later_star._b.Client.send"),
        ("via_star_after_named", "star_after_named._b.Client.send"),
        ("via_named_after_star", "named_after_star._a.Client.send"),
        ("via_excluded", "excluded._b.Client.send"),
        ("via_listed", "listed._impl.Client.send"),
        ("via_repeated_star", "repeated_star._a.Client.send"),
        ("via_named_rebound_past_a_star", "named_rebound_past_a_star._c.Client.send"),
        ("via_star_repeated_past_a_named", "star_repeated_past_a_named._a.Client.send"),
        ("via_later_star_without_it", "later_star_without_it._a.Client.send"),
    ],
    ids=[
        "the-later-star-wins",
        "a-star-after-a-named-import-wins",
        "a-named-import-after-a-star-wins",
        "a-star-whose-all-leaves-it-out-binds-nothing",
        "a-star-whose-all-lists-it-binds-it",
        "a-star-repeated-after-another-wins-again",
        "a-named-import-rebound-after-a-star-wins",
        "a-star-repeated-after-a-named-import-wins",
        "a-later-star-whose-module-lacks-it-keeps-the-earlier",
    ],
)
def test_the_binding_python_keeps_types_the_receiver(
    order_graph: RecordedGraph, caller: str, callee: str
) -> None:
    callees = _callees(order_graph, caller)
    assert callees == {callee}, callees


def test_a_star_whose_all_leaves_the_name_out_binds_nothing(
    order_graph: RecordedGraph,
) -> None:
    # `excluded_only/__init__.py` stars `_a`, whose `__all__` is ["Other"],
    # so `excluded_only.Client` is unbound and the receiver stays untyped.
    assert "excluded_only._a.Client.send" not in _callees(
        order_graph, "via_excluded_only"
    )


@pytest.mark.parametrize(
    "package",
    ["plain_import_after_star", "external_later_star"],
    ids=[
        "a-plain-import-after-a-star-wins",
        "a-later-star-binding-it-outside-the-project-wins",
    ],
)
def test_a_later_binding_outside_the_project_leaves_the_receiver_untyped(
    order_graph: RecordedGraph, package: str
) -> None:
    # `Client` is last bound to httpx's, which the project does not hold,
    # so no edge may reach the earlier star's `_a.Client`.
    callees = _callees(order_graph, f"via_{package}")
    assert f"{package}._a.Client.send" not in callees, callees


# Negative: what must not change.


def test_a_by_name_re_export_still_binds(graph: RecordedGraph) -> None:
    callees = _callees(graph, "via_explicit")
    assert "explicit._client.Client.send" in callees
    assert "explicit._client.BaseClient.build_request" in callees


def test_a_private_name_is_not_taken_through_a_star(graph: RecordedGraph) -> None:
    # `from ._client import *` binds no `_Hidden`, so `star._Hidden` names
    # nothing and its receiver stays untyped.
    assert "star._client._Hidden.send" not in _callees(graph, "via_private_name")


# How `__all__` is read. Each package below stars `_b` and then `_a`, both
# defining `Client`; `_a`'s later star wins exactly when it binds `Client`.
# An `__all__` grown by `.append("x")`, `.extend([...])` or `+=` is read as
# the names it ends up listing, and a plain reassignment replaces it; one
# changed by any other call, or grown by a value the source does not spell,
# falls back to binding every public name, and statements that do not touch
# `__all__` leave it readable.
OTHER = "class Other:\n    pass\n\n\n"
ALL_FORMS = {
    "appended": '__all__ = ["Other"]\n__all__.append("Client")\n',
    "extended": '__all__ = ["Other"]\n__all__.extend(["Client"])\n',
    "augmented": '__all__ = ["Other"]\n__all__ += ["Client"]\n',
    "appended_without": '__all__ = []\n__all__.append("Other")\n',
    "extended_without": '__all__ = []\n__all__.extend(("Other",))\n',
    # A comment among the entries is no entry (Greptile, PR #2989).
    "commented": '__all__ = [\n    # the public API\n    "Other",  # kept\n]\n',
    "commented_extend": '__all__ = []\n__all__.extend([  # more\n    "Other",\n])\n',
    "commented_append": '__all__ = []\n__all__.append(  # one\n    "Other"\n)\n',
    "inserted": '__all__ = ["Other"]\n__all__.insert(0, "Client")\n',
    "appended_name": 'NAME = "Client"\n__all__ = ["Other"]\n__all__.append(NAME)\n',
    "computed": '__all__ = list(("Other", "Client"))\n',
    "reassigned": '__all__ = ["Client"]\n__all__ = ["Other"]\n',
    "recomputed": '__all__ = list(("Other",))\n__all__ = ["Other"]\n',
    "untouched": (
        '"""Implementation details."""\n\n'
        "import warnings\n\n"
        "LIMIT = 3\n"
        'warnings.filterwarnings("ignore")\n'
        "callable(LIMIT)\n"
        '__all__ = ["Other"]\n'
    ),
    # Changed anywhere the literal reader does not read (Greptile and
    # CodeRabbit, PR #2989): inside a block, by a function, or rebound some
    # other way. Each falls back to every public name.
    "appended_in_if": '__all__ = ["Other"]\nif True:\n    __all__.append("Client")\n',
    "extended_in_try": (
        '__all__ = ["Other"]\ntry:\n    __all__.extend(["Client"])\n'
        "except ImportError:\n    pass\n"
    ),
    "appended_in_with": (
        'import contextlib\n\n__all__ = ["Other"]\n'
        "with contextlib.suppress(ImportError):\n"
        '    __all__.append("Client")\n'
    ),
    "appended_in_for": (
        '__all__ = ["Other"]\nfor _name in ("Client",):\n    __all__.append(_name)\n'
    ),
    "grown_in_while": (
        '__all__ = ["Other"]\nwhile "Client" not in __all__:\n'
        '    __all__ += ["Client"]\n'
    ),
    "appended_in_match": (
        '__all__ = ["Other"]\nmatch "Client":\n    case str(_name):\n'
        "        __all__.append(_name)\n"
    ),
    "reassigned_in_nested_blocks": (
        '__all__ = ["Other"]\nif True:\n    for _ in range(1):\n        try:\n'
        '            __all__ = ["Other", "Client"]\n        finally:\n'
        "            pass\n"
    ),
    "appended_by_a_function": (
        "def _export(name):\n    __all__.append(name)\n\n\n"
        '__all__ = ["Other"]\n_export("Client")\n'
    ),
    "appended_in_a_comprehension": (
        '__all__ = ["Other"]\n[__all__.append(_n) for _n in ("Client",)]\n'
    ),
    "item_assigned": '__all__ = ["Other", "Placeholder"]\n__all__[1] = "Client"\n',
    "tuple_assigned": (
        '__all__ = ["Other"]\n__all__, _rest = ["Other", "Client"], None\n'
    ),
    "walrus": '__all__ = ["Other"]\n(__all__ := ["Other", "Client"])\n',
    "deleted": '__all__ = ["Other"]\ndel __all__\n',
    "imported_all": 'from ._base import __all__\n\n__all__ += ["Other"]\n',
    # Reads leave it readable, as does a later top-level reassignment, and
    # another object's `__all__` is not this module's.
    "read_in_blocks": (
        '__all__ = ["Other"]\nfor _name in __all__:\n    assert _name\n'
        'if "Client" in __all__:\n    pass\n\n\n'
        "def __dir__():\n    return list(__all__)\n"
    ),
    "reset_after_a_block": (
        '__all__ = ["Other"]\nif True:\n    __all__.append("Client")\n'
        '__all__ = ["Other"]\n'
    ),
    "another_objects_all": (
        'import types\n\n__all__ = ["Other"]\n_ns = types.SimpleNamespace()\n'
        '_ns.__all__ = ["Other", "Client"]\n'
    ),
    # An `__all__` a def, class body, lambda or comprehension binds as its
    # own is not the module's (Greptile, PR #2989), so the module's list
    # stays readable...
    "local_in_a_function": (
        '__all__ = ["Other"]\n\n\ndef _helper():\n    __all__ = []\n'
        '    __all__.append("Client")\n    return __all__\n\n\n_helper()\n'
    ),
    "parameter": (
        '__all__ = ["Other"]\n\n\ndef _helper(__all__):\n'
        '    __all__.append("Client")\n\n\n_helper([])\n'
    ),
    "typed_default_parameter": (
        '__all__ = ["Other"]\n\n\ndef _helper(__all__: list = []):\n'
        '    __all__.append("Client")\n\n\n_helper()\n'
    ),
    "star_parameter": (
        '__all__ = ["Other"]\n\n\ndef _helper(*__all__):\n'
        '    __all__ += ("Client",)\n\n\n_helper()\n'
    ),
    "class_attribute": (
        '__all__ = ["Other"]\n\n\nclass _Registry:\n    __all__ = ["Client"]\n'
    ),
    "comprehension_target": (
        '__all__ = ["Other"]\n_names = [__all__ for __all__ in ("Client",)]\n'
    ),
    "lambda_parameter": (
        '__all__ = ["Other"]\n_add = lambda __all__: __all__.append("Client")\n'
        "_add([])\n"
    ),
    "lambda_walrus": (
        '__all__ = ["Other"]\n_make = lambda: (__all__ := ["Client"])\n_make()\n'
    ),
    "nonlocal_in_a_closure": (
        '__all__ = ["Other"]\n\n\ndef _outer():\n    __all__ = []\n\n'
        "    def _inner():\n        nonlocal __all__\n"
        '        __all__ = ["Client"]\n\n    _inner()\n    return __all__\n\n\n'
        "_outer()\n"
    ),
    "closure_over_a_local": (
        '__all__ = ["Other"]\n\n\ndef _outer():\n    __all__ = []\n\n'
        '    def _inner():\n        __all__.append("Client")\n\n'
        "    _inner()\n    return __all__\n\n\n_outer()\n"
    ),
    # ...while one that reaches the module's still falls back: a method skips
    # its class body, `global` names the module's, and a walrus in a
    # comprehension binds the scope around it.
    "method_skips_its_class": (
        '__all__ = ["Other"]\n\n\nclass _Registry:\n    __all__ = []\n\n'
        '    def add(self):\n        __all__.append("Client")\n\n\n'
        "_Registry().add()\n"
    ),
    "global_in_a_function": (
        '__all__ = ["Other"]\n\n\ndef _export():\n    global __all__\n'
        '    __all__ = ["Other", "Client"]\n\n\n_export()\n'
    ),
    "global_in_a_class_body": (
        '__all__ = ["Other"]\n\n\nclass _Exports:\n    global __all__\n'
        '    __all__ = ["Other", "Client"]\n'
    ),
    "item_assigned_in_a_function": (
        '__all__ = ["Other", "Placeholder"]\n\n\ndef _fill():\n'
        '    __all__[1] = "Client"\n\n\n_fill()\n'
    ),
    "walrus_in_a_comprehension": (
        '__all__ = ["Other"]\n[(__all__ := ["Other", "Client"]) for _ in (1,)]\n'
    ),
}

# `Pool.__enter__` hands back a `Connection`, so typing `with Pool() as c`
# needs the indexed `Pool` behind the bare name the star re-exports, not the
# same-named class a search by name finds first (`decoy._pool.Pool`).
POOL = """\
__all__ = ["Pool"]


class Connection:
    def execute(self, query):
        return query


class Pool:
    def __enter__(self) -> Connection:
        return Connection()

    def __exit__(self, *exc):
        pass

    def execute(self, query):
        return query
"""

DECOY_POOL = """\
class Pool:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def execute(self, query):
        return query
"""

ALL_APP = (
    "\n".join(f"import {package}" for package in ALL_FORMS)
    + "\nimport later_named\nfrom pooled import Pool\n"
    + "".join(
        f"\n\ndef via_{package}():\n    client = {package}.Client()\n"
        "    client.send(1)\n"
        for package in (*ALL_FORMS, "later_named")
    )
    + """

def via_bare_with():
    with Pool() as connection:
        connection.execute("SELECT 1")
"""
)


@pytest.fixture(scope="module")
def all_graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("starall") / "starall"
    for package, dunder_all in ALL_FORMS.items():
        _write(root, f"{package}/_a.py", f"{dunder_all}\n\n{OTHER}{KLASS}")
        _write(root, f"{package}/_b.py", KLASS)
        _write(root, f"{package}/__init__.py", "from ._b import *\nfrom ._a import *\n")
    _write(root, "imported_all/_base.py", '__all__ = ["Client"]\n')
    # A by-name import of another name after the star leaves `Client` to it.
    _write(root, "later_named/_impl.py", LISTED)
    _write(root, "later_named/_other.py", OTHER)
    _write(
        root,
        "later_named/__init__.py",
        "from ._impl import *\nfrom ._other import Other\n",
    )
    _write(root, "decoy/_pool.py", DECOY_POOL)
    _write(root, "pooled/_pool.py", POOL)
    _write(root, "pooled/__init__.py", "from ._pool import *\n")
    _write(root, "app.py", ALL_APP)
    return _index(root, MagicMock())


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("via_appended", "appended._a.Client.send"),
        ("via_extended", "extended._a.Client.send"),
        ("via_augmented", "augmented._a.Client.send"),
        ("via_appended_without", "appended_without._b.Client.send"),
        ("via_extended_without", "extended_without._b.Client.send"),
        ("via_commented", "commented._b.Client.send"),
        ("via_commented_extend", "commented_extend._b.Client.send"),
        ("via_commented_append", "commented_append._b.Client.send"),
        ("via_inserted", "inserted._a.Client.send"),
        ("via_appended_name", "appended_name._a.Client.send"),
        ("via_computed", "computed._a.Client.send"),
        ("via_reassigned", "reassigned._b.Client.send"),
        ("via_recomputed", "recomputed._b.Client.send"),
        ("via_untouched", "untouched._b.Client.send"),
        ("via_appended_in_if", "appended_in_if._a.Client.send"),
        ("via_extended_in_try", "extended_in_try._a.Client.send"),
        ("via_appended_in_with", "appended_in_with._a.Client.send"),
        ("via_appended_in_for", "appended_in_for._a.Client.send"),
        ("via_grown_in_while", "grown_in_while._a.Client.send"),
        ("via_appended_in_match", "appended_in_match._a.Client.send"),
        (
            "via_reassigned_in_nested_blocks",
            "reassigned_in_nested_blocks._a.Client.send",
        ),
        ("via_appended_by_a_function", "appended_by_a_function._a.Client.send"),
        (
            "via_appended_in_a_comprehension",
            "appended_in_a_comprehension._a.Client.send",
        ),
        ("via_item_assigned", "item_assigned._a.Client.send"),
        ("via_tuple_assigned", "tuple_assigned._a.Client.send"),
        ("via_walrus", "walrus._a.Client.send"),
        ("via_deleted", "deleted._a.Client.send"),
        ("via_imported_all", "imported_all._a.Client.send"),
        ("via_read_in_blocks", "read_in_blocks._b.Client.send"),
        ("via_reset_after_a_block", "reset_after_a_block._b.Client.send"),
        ("via_another_objects_all", "another_objects_all._b.Client.send"),
        ("via_local_in_a_function", "local_in_a_function._b.Client.send"),
        ("via_parameter", "parameter._b.Client.send"),
        ("via_typed_default_parameter", "typed_default_parameter._b.Client.send"),
        ("via_star_parameter", "star_parameter._b.Client.send"),
        ("via_class_attribute", "class_attribute._b.Client.send"),
        ("via_comprehension_target", "comprehension_target._b.Client.send"),
        ("via_lambda_parameter", "lambda_parameter._b.Client.send"),
        ("via_lambda_walrus", "lambda_walrus._b.Client.send"),
        ("via_nonlocal_in_a_closure", "nonlocal_in_a_closure._b.Client.send"),
        ("via_closure_over_a_local", "closure_over_a_local._b.Client.send"),
        ("via_method_skips_its_class", "method_skips_its_class._a.Client.send"),
        ("via_global_in_a_function", "global_in_a_function._a.Client.send"),
        ("via_global_in_a_class_body", "global_in_a_class_body._a.Client.send"),
        (
            "via_item_assigned_in_a_function",
            "item_assigned_in_a_function._a.Client.send",
        ),
        (
            "via_walrus_in_a_comprehension",
            "walrus_in_a_comprehension._a.Client.send",
        ),
        ("via_later_named", "later_named._impl.Client.send"),
        (
            "via_bare_with",
            frozenset(
                {
                    "pooled._pool.Connection.execute",
                    "pooled._pool.Pool.__enter__",
                    "pooled._pool.Pool.__exit__",
                }
            ),
        ),
    ],
    ids=[
        "append-lists-it",
        "extend-lists-it",
        "plus-equals-lists-it",
        "append-of-another-name-leaves-it-out",
        "extend-of-other-names-leaves-it-out",
        "a-comment-in-the-list-is-no-entry",
        "a-comment-in-an-extend-is-no-entry",
        "a-comment-in-an-append-is-no-entry",
        "another-list-method-falls-back-to-every-public-name",
        "append-of-a-non-literal-falls-back-to-every-public-name",
        "a-computed-all-falls-back-to-every-public-name",
        "a-reassignment-replaces-the-earlier-list",
        "a-literal-reassignment-replaces-a-computed-all",
        "unrelated-statements-keep-all-readable",
        "an-append-in-an-if-falls-back",
        "an-extend-in-a-try-falls-back",
        "an-append-in-a-with-falls-back",
        "an-append-in-a-for-falls-back",
        "a-plus-equals-in-a-while-falls-back",
        "an-append-in-a-match-falls-back",
        "a-reassignment-in-nested-blocks-falls-back",
        "an-append-by-a-function-defined-first-falls-back",
        "an-append-in-a-comprehension-falls-back",
        "an-item-assignment-falls-back",
        "a-tuple-assignment-falls-back",
        "a-walrus-falls-back",
        "a-del-falls-back",
        "an-imported-all-falls-back",
        "reads-in-blocks-keep-all-readable",
        "a-later-reassignment-replaces-a-block-change",
        "another-objects-all-is-not-the-modules",
        "a-functions-local-is-not-the-modules",
        "a-parameter-is-not-the-modules",
        "a-typed-default-parameter-is-not-the-modules",
        "a-star-parameter-is-not-the-modules",
        "a-class-attribute-is-not-the-modules",
        "a-comprehension-target-is-not-the-modules",
        "a-lambda-parameter-is-not-the-modules",
        "a-lambdas-walrus-is-not-the-modules",
        "a-nonlocal-is-the-enclosing-defs",
        "a-closure-reads-its-enclosing-defs-local",
        "a-method-skips-its-class-body-and-falls-back",
        "a-global-in-a-def-falls-back",
        "a-global-in-a-class-body-falls-back",
        "an-item-assignment-in-a-def-falls-back",
        "a-walrus-in-a-comprehension-falls-back",
        "a-later-by-name-import-of-another-name-keeps-the-star",
        "with-on-a-bare-name-imported-through-a-star",
    ],
)
def test_the_all_a_module_builds_decides_what_its_star_binds(
    all_graph: RecordedGraph, caller: str, callee: str | frozenset[str]
) -> None:
    # A `with` statement also calls its manager's `__enter__` and `__exit__`.
    expected = {callee} if isinstance(callee, str) else set(callee)
    callees = _callees(all_graph, caller)
    assert callees == expected, callees
