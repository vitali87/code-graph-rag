"""A typed first-party receiver whose class lacks the method is a known
non-edge, never a bare-name trie match (issue #1897).

`p: Product` then `p.go()` where `Product` defines no `go`: the typed lookup
finds nothing and resolution used to continue to the name trie, binding `go`
to an unrelated `Elsewhere.go`. The chained and inline receiver paths already
drop this shape; this pins the plain-variable path to the same policy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_PRODUCT_WITHOUT_GO = "class Product:\n    def size(self) -> int:\n        return 1\n"
_PRODUCT_WITH_GO = (
    "class Product:\n    def size(self) -> int:\n        return 1\n\n"
    "    def go(self) -> int:\n        return 2\n"
)
_PRODUCT_INHERITS_GO = (
    "class Base:\n    def go(self) -> int:\n        return 3\n\n\n"
    "class Product(Base):\n    def size(self) -> int:\n        return 1\n"
)
# The decoy defines the colliding NAME; nothing in the fixture ever calls it.
_DECOY_COLLIDES = "class Elsewhere:\n    def go(self) -> int:\n        return 9\n"
# Instrument check: with the name gone, no edge can be emitted for any reason.
_DECOY_CONTROL = "class Elsewhere:\n    def stop(self) -> int:\n        return 9\n"

_APP_PARAM = "from proj.models import Product\n\n\ndef run(p: Product) -> int:\n    return p.go()\n"
_APP_LOCAL = (
    "from proj.models import Product\n\n\n"
    "def run() -> int:\n    p: Product = Product()\n    return p.go()\n"
)


def _go_edges(repo: Path) -> set[str]:
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
        and str(src).endswith(".app.run")
        and str(tgt).endswith(".go")
    }


def _build(tmp_path: Path, product: str, decoy: str, app: str) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    (repo / "models.py").write_text(product, encoding="utf-8")
    (repo / "elsewhere.py").write_text(decoy, encoding="utf-8")
    (repo / "app.py").write_text(app, encoding="utf-8")
    return repo


@pytest.mark.parametrize(
    "app", [_APP_PARAM, _APP_LOCAL], ids=["annotated-param", "annotated-local"]
)
def test_a_typed_receiver_whose_class_lacks_the_method_emits_no_edge(
    tmp_path: Path, app: str
) -> None:
    repo = _build(tmp_path, _PRODUCT_WITHOUT_GO, _DECOY_COLLIDES, app)
    assert _go_edges(repo) == set(), (
        "p is a Product, which has no go(); the call was bound by name to Elsewhere.go"
    )


@pytest.mark.parametrize(
    "app", [_APP_PARAM, _APP_LOCAL], ids=["annotated-param", "annotated-local"]
)
def test_the_control_cannot_produce_the_edge_it_is_checking_for(
    tmp_path: Path, app: str
) -> None:
    # Validates the instrument: with the decoy's method renamed there is no
    # name to collide with, so the test above cannot pass for a wrong reason.
    repo = _build(tmp_path, _PRODUCT_WITHOUT_GO, _DECOY_CONTROL, app)
    assert _go_edges(repo) == set()


def test_a_typed_receiver_whose_class_defines_the_method_still_binds(
    tmp_path: Path,
) -> None:
    repo = _build(tmp_path, _PRODUCT_WITH_GO, _DECOY_COLLIDES, _APP_PARAM)
    assert _go_edges(repo) == {"proj.models.Product.go"}


def test_an_inherited_method_still_binds_to_the_base(tmp_path: Path) -> None:
    # `_try_resolve_method` includes the inheritance walk, so a method the
    # class gets from a first-party base is found, not dropped.
    repo = _build(tmp_path, _PRODUCT_INHERITS_GO, _DECOY_COLLIDES, _APP_PARAM)
    assert _go_edges(repo) == {"proj.models.Base.go"}


def test_an_untyped_receiver_keeps_the_name_fallback(tmp_path: Path) -> None:
    # The policy is about KNOWN types. An untyped receiver's method is unknown
    # rather than known-absent, and the trie may still bind it.
    app = "def run(p) -> int:\n    return p.go()\n"
    repo = _build(tmp_path, _PRODUCT_WITHOUT_GO, _DECOY_COLLIDES, app)
    assert _go_edges(repo) == {"proj.elsewhere.Elsewhere.go"}


def test_same_named_local_classes_are_left_to_the_fallback(tmp_path: Path) -> None:
    # Two functions each define a local `Analyzer`; the bare type name
    # resolves to ONE of them, and judging absence against the wrong one
    # dropped `second`'s real call (found by the local review).
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "app.py").write_text(
        "def first() -> int:\n"
        "    class Analyzer:\n"
        "        def size(self) -> int:\n"
        "            return 1\n"
        "    return Analyzer().size()\n\n\n"
        "def second() -> int:\n"
        "    class Analyzer:\n"
        "        def go(self) -> int:\n"
        "            return 2\n"
        "    a = Analyzer()\n"
        "    return a.go()\n",
        encoding="utf-8",
    )
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    edges = {
        (str(src), str(tgt))
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value and str(tgt).endswith(".go")
    }
    assert edges == {("proj.app.second", "proj.app.second.Analyzer.go")}


def test_an_optional_annotation_is_reduced_before_the_same_named_lookup(
    tmp_path: Path,
) -> None:
    # `a: Analyzer | None` resolves to the module-level `Analyzer` (no `go`);
    # the same-named local class in `first` defines `go`, so the call must
    # be left to the fallback. Searching for `Analyzer | None.go` would find
    # nothing and drop it.
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "app.py").write_text(
        "class Analyzer:\n"
        "    def size(self) -> int:\n"
        "        return 1\n\n\n"
        "def first() -> int:\n"
        "    class Analyzer:\n"
        "        def go(self) -> int:\n"
        "            return 2\n"
        "    return Analyzer().go()\n\n\n"
        "def second(a: Analyzer | None) -> int:\n"
        "    return a.go()\n",
        encoding="utf-8",
    )
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    edges = {
        str(tgt)
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value
        and str(src) == "proj.app.second"
        and str(tgt).endswith(".go")
    }
    assert edges == {"proj.app.first.Analyzer.go"}


def test_a_same_named_class_in_another_module_does_not_rescue_the_call(
    tmp_path: Path,
) -> None:
    # `b.Product.go` is unrelated to the receiver's `a.Product`; its
    # existence must not send the call back to the fallback, which would
    # bind it there.
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "a.py").write_text(
        "class Product:\n    def size(self) -> int:\n        return 1\n",
        encoding="utf-8",
    )
    (repo / "b.py").write_text(
        "class Product:\n    def go(self) -> int:\n        return 9\n", encoding="utf-8"
    )
    (repo / "app.py").write_text(
        "from proj.a import Product\n\n\ndef run(p: Product) -> int:\n    return p.go()\n",
        encoding="utf-8",
    )
    assert _go_edges(repo) == set()


def test_a_same_named_class_in_a_child_module_does_not_rescue_the_call(
    tmp_path: Path,
) -> None:
    # The caller lives in the package module `proj.app`; `proj.app.util` is a
    # CHILD module whose `Product.go` shares the qn prefix but is unrelated.
    # Ownership is decided by the defining module, not the prefix.
    repo = tmp_path / "proj"
    (repo / "app").mkdir(parents=True)
    (repo / "__init__.py").touch()
    (repo / "app" / "__init__.py").write_text(
        "class Product:\n    def size(self) -> int:\n        return 1\n\n\n"
        "def run(p: Product) -> int:\n    return p.go()\n",
        encoding="utf-8",
    )
    (repo / "app" / "util.py").write_text(
        "class Product:\n    def go(self) -> int:\n        return 9\n", encoding="utf-8"
    )
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    edges = {
        (str(src), str(tgt))
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value
        and str(src).endswith(".run")
        and str(tgt).endswith(".go")
    }
    assert edges == set()


def test_a_nested_function_named_like_the_type_does_not_rescue_the_call(
    tmp_path: Path,
) -> None:
    # `factory.Product.go` is a function nested in a function named `Product`,
    # not a class method; the rescue requires the owner to be a class.
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "app.py").write_text(
        "class Product:\n    def size(self) -> int:\n        return 1\n\n\n"
        "def factory() -> int:\n"
        "    def Product() -> int:\n"
        "        def go() -> int:\n"
        "            return 9\n"
        "        return go()\n"
        "    return Product()\n\n\n"
        "def run(p: Product) -> int:\n    return p.go()\n",
        encoding="utf-8",
    )
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    edges = {
        str(tgt)
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value
        and str(src) == "proj.app.run"
        and str(tgt).endswith(".go")
    }
    assert edges == set()


def test_a_child_modules_class_does_not_rescue_the_call_on_a_scoped_reingest(
    tmp_path: Path,
) -> None:
    # During a scoped re-ingest the module map holds only the re-parsed
    # files, so `proj.app.util` is absent from it; ownership must come from
    # the registry, which is rehydrated for every file (found by the bot's
    # execution of the previous cut).
    repo = tmp_path / "proj"
    (repo / "app").mkdir(parents=True)
    (repo / "__init__.py").touch()
    (repo / "app" / "__init__.py").write_text(
        "class Product:\n    def size(self) -> int:\n        return 1\n\n\n"
        "def run(p: Product) -> int:\n    return p.go()\n",
        encoding="utf-8",
    )
    (repo / "app" / "util.py").write_text(
        "class Product:\n    def go(self) -> int:\n        return 9\n", encoding="utf-8"
    )
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    (repo / "app" / "__init__.py").write_text(
        "class Product:\n    def size(self) -> int:\n        return 2\n\n\n"
        "def run(p: Product) -> int:\n    return p.go()\n",
        encoding="utf-8",
    )
    GraphUpdater(
        ingestor=store, repo_path=repo, parsers=parsers, queries=queries
    ).reingest([repo / "app" / "__init__.py"])
    edges = {
        (str(src), str(tgt))
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value
        and str(src).endswith(".run")
        and str(tgt).endswith(".go")
    }
    assert edges == set()


def test_a_same_module_function_of_that_name_is_not_bound_either(
    tmp_path: Path,
) -> None:
    # The import probe's same-module fallback used to bind `p.go()` to a
    # module-level `go()` before the guard ran; the guard now runs first.
    app = (
        "from proj.models import Product\n\n\n"
        "def go() -> int:\n    return 7\n\n\n"
        "def run(p: Product) -> int:\n    return p.go()\n"
    )
    repo = _build(tmp_path, _PRODUCT_WITHOUT_GO, _DECOY_CONTROL, app)
    assert _go_edges(repo) == set()
    # Control: with `Product.go` defined the guard stays out of the way.
    repo2 = _build(tmp_path / "second", _PRODUCT_WITH_GO, _DECOY_CONTROL, app)
    assert _go_edges(repo2) == {"proj.models.Product.go"}


def _rust_run_go_targets(tmp_path: Path, files: dict[str, str]) -> set[str]:
    parsers, queries = load_parsers()
    if "rust" not in {str(k) for k in parsers}:
        pytest.skip("rust parser not available")
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "proj"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    for name, source in files.items():
        (repo / "src" / name).write_text(source, encoding="utf-8")
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    return {
        str(tgt)
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value
        and str(src).endswith(".app.run")
        and str(tgt).endswith(".go")
    }


def test_a_rust_method_from_another_modules_impl_still_binds(tmp_path: Path) -> None:
    # Rust registers impl-block methods under the impl's module, not the
    # struct's qn: `Product.go` exists, under `ext`, and the guard's
    # registry-wide check must find it there.
    targets = _rust_run_go_targets(
        tmp_path,
        {
            "lib.rs": "pub mod models;\npub mod ext;\npub mod app;\n",
            "models.rs": "pub struct Product;\n",
            "ext.rs": "use crate::models::Product;\nimpl Product { pub fn go(&self) -> i32 { 3 } }\n",
            "app.rs": "use crate::models::Product;\n\npub fn run(p: Product) -> i32 {\n    p.go()\n}\n",
        },
    )
    assert targets == {"proj.src.ext.Product.go"}


def test_a_rust_trait_default_method_still_binds(tmp_path: Path) -> None:
    # A trait's default method registers under the TRAIT: no `Product.go`
    # exists anywhere, so only the Rust exemption keeps this edge.
    targets = _rust_run_go_targets(
        tmp_path,
        {
            "lib.rs": "pub mod models;\npub mod app;\n",
            "models.rs": (
                "pub trait Runner {\n    fn go(&self) -> i32 { 3 }\n}\n\n"
                "pub struct Product;\n\nimpl Runner for Product {}\n"
            ),
            "app.rs": (
                "use crate::models::{Product, Runner};\n\n"
                "pub fn run(p: Product) -> i32 {\n    p.go()\n}\n"
            ),
        },
    )
    assert targets == {"proj.src.models.Runner.go"}
