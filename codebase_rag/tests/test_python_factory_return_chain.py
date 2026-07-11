# (H) A Python chained call on a free-function factory receiver
# (H) (`get_resolver()._is_callback(name)`, django's urls/base.py) has the return of
# (H) a factory function as its receiver. Without inferring that return type the
# (H) final method binds to nothing and the returned class's methods report as dead
# (H) (django URLResolver._is_callback). cgr must infer the factory's return type
# (H) (annotation first, then return-statement analysis, transitively through
# (H) factory-calls-factory hops) and resolve the chained method on it.
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
    # (H) `make_widget().run()` where make_widget returns Widget() must reach
    # (H) Widget.run. Aaa.run is an alphabetical decoy: a bare trie fallback would
    # (H) pick it, so a passing test proves real return typing.
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
    # (H) The factory and the returned class live in another module; the call site
    # (H) imports only the factory. The return type must resolve in the FACTORY's
    # (H) module scope, not the caller's.
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
    # (H) The exact django shape: get_resolver delegates to a cached inner factory
    # (H) that constructs the class. The inference must follow the
    # (H) factory-returns-factory-call hop transitively.
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
    # (H) The factory body is opaque (returns a cache entry) but its return
    # (H) annotation names the class: the annotation is the return-type source.
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
    # (H) `-> Widget | None` is the idiomatic optional-factory signature; the
    # (H) non-None operand is the receiver type.
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
    # (H) The factory's return type is uninferrable (opaque body, no annotation):
    # (H) the chained method must DROP, never rebind by bare name to the
    # (H) alphabetical decoy.
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
    # (H) `r = make_widget(); r.run()` must type r via the factory's return, not
    # (H) via the bare-name trie (the decoy makes a trie hit ambiguous and wrong).
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


def test_factory_returning_local_variable_resolves(tmp_path: Path) -> None:
    # (H) The factory builds the instance into a local and returns the identifier;
    # (H) the identifier's type comes from the factory's own local-variable map.
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
