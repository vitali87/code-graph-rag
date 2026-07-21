# A Python chained call on a free-function factory receiver
# (`get_resolver()._is_callback(name)`, django's urls/base.py) uses the factory's
# return as its receiver. Without inferring that return type the final method
# binds to nothing and the class's methods report dead (django
# URLResolver._is_callback). cgr must infer the return type (annotation first,
# then return-statement analysis, transitively through factory hops).
from pathlib import Path

from evals.cgr_graph import _capture


def _calls(tmp_path: Path) -> set[tuple[str, str]]:
    ingestor = _capture(tmp_path, "proj")
    return {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }


def _make(root: Path, files: dict[str, str]) -> None:
    pkg = root / "app"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, body in files.items():
        (pkg / name).write_text(body, encoding="utf-8")


_DECOY = "class Aaa:\n    def run(self):\n        pass\n"


def test_free_factory_return_chain_resolves(tmp_path: Path) -> None:
    # `make_widget().run()` (make_widget returns Widget()) must reach Widget.run.
    # Aaa.run is an alphabetical decoy a bare trie fallback would pick, so passing
    # proves real return typing.
    _make(
        tmp_path,
        {
            "widgets.py": (
                f"{_DECOY}"
                "class Widget:\n"
                "    def run(self):\n"
                "        pass\n"
                "\n"
                "def make_widget():\n"
                "    return Widget()\n"
                "\n"
                "def driver():\n"
                "    make_widget().run()\n"
            )
        },
    )
    calls = _calls(tmp_path)
    assert ("proj.app.widgets.driver", "proj.app.widgets.Widget.run") in calls, sorted(
        c for c in calls if "run" in c[1]
    )
    assert ("proj.app.widgets.driver", "proj.app.widgets.Aaa.run") not in calls


def test_imported_factory_return_chain_resolves(tmp_path: Path) -> None:
    # Factory and returned class live in another module; the call site imports
    # only the factory, so the return type resolves in the FACTORY's module
    # scope, not the caller's.
    _make(
        tmp_path,
        {
            "widgets.py": (
                f"{_DECOY}"
                "class Widget:\n"
                "    def run(self):\n"
                "        pass\n"
                "\n"
                "def make_widget():\n"
                "    return Widget()\n"
            ),
            "driver.py": (
                "from app.widgets import make_widget\n"
                "\n"
                "def driver():\n"
                "    make_widget().run()\n"
            ),
        },
    )
    calls = _calls(tmp_path)
    assert ("proj.app.driver.driver", "proj.app.widgets.Widget.run") in calls, sorted(
        c for c in calls if "run" in c[1]
    )
    assert ("proj.app.driver.driver", "proj.app.widgets.Aaa.run") not in calls


def test_two_hop_factory_chain_resolves(tmp_path: Path) -> None:
    # The exact django shape: get_resolver delegates to a cached inner factory
    # that constructs the class. The inference must follow the
    # factory-returns-factory-call hop transitively.
    _make(
        tmp_path,
        {
            "resolver.py": (
                "import functools\n"
                "\n"
                "class Aaa:\n"
                "    def _is_callback(self, name):\n"
                "        pass\n"
                "\n"
                "class URLResolver:\n"
                "    def _is_callback(self, name):\n"
                "        return name\n"
                "\n"
                "@functools.cache\n"
                "def _get_cached_resolver(urlconf=None):\n"
                "    return URLResolver()\n"
                "\n"
                "def get_resolver(urlconf=None):\n"
                "    return _get_cached_resolver(urlconf)\n"
            ),
            "checks.py": (
                "from app.resolver import get_resolver\n"
                "\n"
                "def check_url(name):\n"
                "    return get_resolver()._is_callback(name)\n"
            ),
        },
    )
    calls = _calls(tmp_path)
    assert (
        "proj.app.checks.check_url",
        "proj.app.resolver.URLResolver._is_callback",
    ) in calls, sorted(c for c in calls if "_is_callback" in c[1])
    assert (
        "proj.app.checks.check_url",
        "proj.app.resolver.Aaa._is_callback",
    ) not in calls


def test_annotated_factory_return_resolves(tmp_path: Path) -> None:
    # The factory body is opaque (returns a cache entry) but its return
    # annotation names the class: the annotation is the return-type source.
    _make(
        tmp_path,
        {
            "widgets.py": (
                f"{_DECOY}"
                "class Widget:\n"
                "    def run(self):\n"
                "        pass\n"
                "\n"
                "_CACHE = {}\n"
                "\n"
                "def make_widget(key) -> Widget:\n"
                "    return _CACHE[key]\n"
                "\n"
                "def driver():\n"
                "    make_widget(1).run()\n"
            )
        },
    )
    calls = _calls(tmp_path)
    assert ("proj.app.widgets.driver", "proj.app.widgets.Widget.run") in calls, sorted(
        c for c in calls if "run" in c[1]
    )
    assert ("proj.app.widgets.driver", "proj.app.widgets.Aaa.run") not in calls


def test_optional_annotated_factory_return_resolves(tmp_path: Path) -> None:
    # `-> Widget | None` is the idiomatic optional-factory signature; the
    # non-None operand is the receiver type.
    _make(
        tmp_path,
        {
            "widgets.py": (
                f"{_DECOY}"
                "class Widget:\n"
                "    def run(self):\n"
                "        pass\n"
                "\n"
                "_CACHE = {}\n"
                "\n"
                "def make_widget(key) -> Widget | None:\n"
                "    return _CACHE.get(key)\n"
                "\n"
                "def driver():\n"
                "    make_widget(1).run()\n"
            )
        },
    )
    calls = _calls(tmp_path)
    assert ("proj.app.widgets.driver", "proj.app.widgets.Widget.run") in calls, sorted(
        c for c in calls if "run" in c[1]
    )


def test_unknown_factory_return_does_not_bind_bare(tmp_path: Path) -> None:
    # The factory's return type is uninferrable (opaque body, no annotation):
    # the chained method must DROP, never rebind by bare name to the
    # alphabetical decoy.
    _make(
        tmp_path,
        {
            "widgets.py": (
                f"{_DECOY}"
                "_CACHE = {}\n"
                "\n"
                "def make_widget(key):\n"
                "    return _CACHE[key]\n"
                "\n"
                "def driver():\n"
                "    make_widget(1).run()\n"
            )
        },
    )
    calls = _calls(tmp_path)
    assert ("proj.app.widgets.driver", "proj.app.widgets.Aaa.run") not in calls, sorted(
        c for c in calls if "run" in c[1]
    )


def test_local_var_factory_assignment_types_receiver(tmp_path: Path) -> None:
    # `r = make_widget(); r.run()` must type r via the factory's return, not
    # via the bare-name trie (the decoy makes a trie hit ambiguous and wrong).
    _make(
        tmp_path,
        {
            "widgets.py": (
                f"{_DECOY}"
                "class Widget:\n"
                "    def run(self):\n"
                "        pass\n"
                "\n"
                "def make_widget():\n"
                "    return Widget()\n"
                "\n"
                "def driver():\n"
                "    r = make_widget()\n"
                "    r.run()\n"
            )
        },
    )
    calls = _calls(tmp_path)
    assert ("proj.app.widgets.driver", "proj.app.widgets.Widget.run") in calls, sorted(
        c for c in calls if "run" in c[1]
    )
    assert ("proj.app.widgets.driver", "proj.app.widgets.Aaa.run") not in calls


def test_reexported_factory_return_chain_resolves(tmp_path: Path) -> None:
    # django imports get_resolver through package re-exports (`from django.urls
    # import get_resolver` -> __init__ -> .resolvers): the import target is the
    # package qn, not the function qn, so resolution follows the re-export hops.
    _make(
        tmp_path,
        {
            "resolver.py": (
                "class Aaa:\n"
                "    def _is_callback(self, name):\n"
                "        pass\n"
                "\n"
                "class URLResolver:\n"
                "    def _is_callback(self, name):\n"
                "        return name\n"
                "\n"
                "def get_resolver(urlconf=None):\n"
                "    return URLResolver()\n"
            ),
            "checks.py": (
                "from app import get_resolver\n"
                "\n"
                "def check_url(name):\n"
                "    return get_resolver()._is_callback(name)\n"
            ),
        },
    )
    (tmp_path / "app" / "__init__.py").write_text(
        "from app.resolver import get_resolver\n", encoding="utf-8"
    )
    calls = _calls(tmp_path)
    assert (
        "proj.app.checks.check_url",
        "proj.app.resolver.URLResolver._is_callback",
    ) in calls, sorted(c for c in calls if "_is_callback" in c[1])
    assert (
        "proj.app.checks.check_url",
        "proj.app.resolver.Aaa._is_callback",
    ) not in calls


def test_factory_returning_local_variable_resolves(tmp_path: Path) -> None:
    # The factory builds the instance into a local and returns the identifier;
    # the identifier's type comes from the factory's own local-variable map.
    _make(
        tmp_path,
        {
            "widgets.py": (
                f"{_DECOY}"
                "class Widget:\n"
                "    def run(self):\n"
                "        pass\n"
                "\n"
                "def make_widget():\n"
                "    w = Widget()\n"
                "    return w\n"
                "\n"
                "def driver():\n"
                "    make_widget().run()\n"
            )
        },
    )
    calls = _calls(tmp_path)
    assert ("proj.app.widgets.driver", "proj.app.widgets.Widget.run") in calls, sorted(
        c for c in calls if "run" in c[1]
    )


def test_self_annotated_classmethod_factory_resolves(tmp_path: Path) -> None:
    # `-> Self` on a classmethod factory names the enclosing class as a full
    # qn, so a cross-module `Widget.create().run()` chain resolves without a
    # caller-scope class lookup.
    _make(
        tmp_path,
        {
            "widgets.py": (
                f"{_DECOY}"
                "from typing import Self\n"
                "\n"
                "class Widget:\n"
                "    @classmethod\n"
                "    def create(cls) -> Self:\n"
                "        return cls()\n"
                "\n"
                "    def run(self):\n"
                "        pass\n"
            ),
            "driver.py": (
                "from app.widgets import Widget\n"
                "\n"
                "def driver():\n"
                "    Widget.create().run()\n"
            ),
        },
    )
    calls = _calls(tmp_path)
    assert ("proj.app.driver.driver", "proj.app.widgets.Widget.run") in calls, sorted(
        c for c in calls if "run" in c[1]
    )
    assert ("proj.app.driver.driver", "proj.app.widgets.Aaa.run") not in calls
