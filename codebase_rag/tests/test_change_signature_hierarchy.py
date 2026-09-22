# change_signature (issue #1533): override hierarchies, receivers, static
# methods, and the guards on defaults and dropped parameters.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.signature import change_signature
from codebase_rag.editing.signature_spec import SignatureRefused
from codebase_rag.tests.change_signature_support import (
    PROJECT,
    _index,
    _project,
    _read,
    _smoke,
)

# --- hierarchies ---------------------------------------------------------------------


SHAPES: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/shapes.py": (
        "class Base:\n"
        "    def area(self, scale):\n"
        "        return 0 * scale\n\n\n"
        "class Circle(Base):\n"
        "    def area(self, scale):\n"
        "        return 3 * scale\n\n\n"
        "def total(shape: Base):\n"
        "    return shape.area(2) + Circle().area(scale=3)\n"
    ),
}


def test_a_method_hierarchy_is_rewritten_together(temp_repo: Path) -> None:
    root = _project(temp_repo, SHAPES)
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.shapes.Base.area",
        ["scale", "unit: str"],
        {"unit": "='m'"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert set(report.hierarchy) == {
        f"{PROJECT}.pkg.shapes.Base.area",
        f"{PROJECT}.pkg.shapes.Circle.area",
    }
    shapes = _read(root, "pkg/shapes.py")
    assert shapes.count("def area(self, scale, unit: str):") == 2
    assert "shape.area(2, 'm') + Circle().area(scale=3, unit='m')" in shapes
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message
    _smoke(root, "from pkg.shapes import total, Circle\nassert total(Circle()) == 15")


def test_an_override_with_different_parameters_is_refused(temp_repo: Path) -> None:
    files = dict(SHAPES)
    files["pkg/shapes.py"] = files["pkg/shapes.py"].replace(
        "    def area(self, scale):\n        return 3 * scale",
        "    def area(self, factor):\n        return 3 * factor",
    )
    root = _project(temp_repo, files)
    store, _updater = _index(root)
    with pytest.raises(SignatureRefused, match=r"Circle\.area"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.pkg.shapes.Base.area",
            ["scale", "unit: str"],
            {"unit": "='m'"},
        )
    assert _read(root, "pkg/shapes.py") == files["pkg/shapes.py"]


def test_a_method_whose_receiver_is_not_self_or_cls_is_refused(
    temp_repo: Path,
) -> None:
    # `this` would be remapped as a parameter and every bound call would
    # lose its receiver.
    files = dict(SHAPES)
    files["pkg/shapes.py"] = files["pkg/shapes.py"].replace(
        "def area(self, scale):", "def area(this, scale):"
    )
    root = _project(temp_repo, files)
    store, _updater = _index(root)
    with pytest.raises(SignatureRefused, match=r"`this`.*self or cls"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.pkg.shapes.Base.area",
            ["scale", "unit: str"],
            {"unit": "='m'"},
        )
    assert _read(root, "pkg/shapes.py") == files["pkg/shapes.py"]


@pytest.mark.parametrize("decorator", ["staticmethod", "builtins.staticmethod"])
def test_a_static_method_has_no_receiver(temp_repo: Path, decorator: str) -> None:
    # The indexer reads a decorator by its last name, so the operation must
    # recognise the qualified spelling too.
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/shapes.py": (
                "import builtins\n\n\n"
                "class K:\n"
                f"    @{decorator}\n"
                "    def helper(a):\n"
                "        return a\n\n\n"
                "def on_class():\n"
                "    return K.helper(2)\n\n\n"
                "def on_instance():\n"
                "    return K().helper(3)\n"
            ),
        },
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.shapes.K.helper",
        ["a", "n: int"],
        {"n": "=1"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.unmapped == ()
    shapes = _read(root, "pkg/shapes.py")
    assert "    def helper(a, n: int):" in shapes
    assert "return K.helper(2, 1)" in shapes
    assert "return K().helper(3, 1)" in shapes
    _smoke(
        root,
        "from pkg.shapes import on_class, on_instance\n"
        "assert on_class() + on_instance() == 5",
    )


def test_a_static_methods_self_is_an_ordinary_parameter(temp_repo: Path) -> None:
    """`@staticmethod def f(self, x)` is legal and `self` is NOT a receiver.

    The receiver was popped by NAME before staticness was decided, so
    `self` was treated as the receiver AND re-added from `new_params`,
    writing `def helper(self, self, a, n: int)` -- a duplicate parameter,
    and a SyntaxError in the file the tool had just edited (Copilot,
    #1533). Deciding staticness first makes the operation refuse and roll
    back instead, leaving the source untouched.
    """
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/shapes.py": (
                "class K:\n"
                "    @staticmethod\n"
                "    def helper(self, a):\n"
                "        return self + a\n\n\n"
                "def on_class():\n"
                "    return K.helper(1, 2)\n"
            ),
        },
    )
    store, updater = _index(root)

    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.shapes.K.helper",
        ["self", "a", "n: int"],
        {"n": "=1"},
        reingest=updater.reingest,
    )

    # The call-site binding still counts a receiver for a static method, so
    # the operation cannot map `K.helper(1, 2)` and ROLLS BACK. That is the
    # correct outcome: before the reorder it wrote
    # `def helper(self, self, a, n: int)` -- a duplicate parameter, and a
    # SyntaxError in the file it had just edited.
    assert not report.applied
    assert "too_many" in report.message, report.message
    shapes = _read(root, "pkg/shapes.py")
    assert "def helper(self, self" not in shapes, shapes
    assert "    def helper(self, a):" in shapes, shapes


def test_a_bare_generator_argument_is_unmapped_not_rewritten(
    temp_repo: Path,
) -> None:
    """`helper(x for x in xs)` passes a GENERATOR, not a list of values.

    tree-sitter gives the call a `generator_expression` as its arguments
    node rather than an `argument_list`, so binding it positionally would
    rewrite a generator as if it were an ordinary value and change what
    the call means. It must be listed unmapped instead (Copilot, #1533).
    """
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/app.py": (
                "def helper(a):\n"
                "    return sum(a)\n\n\n"
                "def run(xs):\n"
                "    return helper(x for x in xs)\n"
            ),
        },
    )
    store, updater = _index(root)

    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.app.helper",
        ["a", "n: int"],
        {"n": "=1"},
        reingest=updater.reingest,
    )

    (skipped,) = report.unmapped
    assert skipped.path == "pkg/app.py"
    # The REASON matters, not just the refusal: without the argument-list
    # check the call is still skipped, but by an arity miscount that counts
    # the generator's own children as two positional arguments. That is the
    # right answer for the wrong reason, and it would stop being right for
    # a one-parameter generator call.
    assert cs.SIGNATURE_SITE_UNREADABLE.split("{")[0] in skipped.reason, skipped.reason
    # The generator survives verbatim: it was never bound as a value.
    assert "helper(x for x in xs)" in _read(root, "pkg/app.py")


def test_a_default_reading_a_renamed_parameter_refuses(temp_repo: Path) -> None:
    """`def f(a, b=a)` renamed a->x would write `def f(x, b=a)`.

    A default is evaluated at DEFINITION time in the enclosing scope, so
    that file raises NameError the moment it is imported -- and it PARSES,
    so the syntax postcondition cannot catch it. `_body_references` walks
    the body only and never saw the reference (Copilot, #1533).
    """
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/app.py": "def f(a, b=a):\n    return b\n",
        },
    )
    store, updater = _index(root)

    with pytest.raises(SignatureRefused, match="evaluated at definition time"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.pkg.app.f",
            ["x", "b=a"],
            {"x": "a"},
            reingest=updater.reingest,
        )

    assert "def f(a, b=a):" in _read(root, "pkg/app.py")


def test_a_default_not_reading_the_renamed_parameter_still_renames(
    temp_repo: Path,
) -> None:
    """The control: a default that does NOT read the renamed name is fine,
    so the refusal cannot be satisfied by refusing every default."""
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/app.py": "def f(a, b=1):\n    return a + b\n",
        },
    )
    store, updater = _index(root)

    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.app.f",
        ["x", "b=1"],
        {"x": "a"},
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert "def f(x, b=1):" in _read(root, "pkg/app.py")


def test_an_explicit_spelling_re_annotates_every_override(
    temp_repo: Path,
) -> None:
    """Spelling a kept parameter out again re-annotates it everywhere.

    `carried` was inferred by comparing each new spec with the primary's,
    and a bare name resolves TO the primary's spec -- so an explicit
    `a: int` that matched the primary compared equal, counted as carried,
    and left an override declaring `a: str` untouched (Copilot, #1533).
    """
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/app.py": (
                "class Base:\n    def f(self, a: int) -> int:\n        return a\n\n\n"
                "class Sub(Base):\n    def f(self, a: str) -> int:\n"
                "        return len(a)\n"
            ),
        },
    )
    store, updater = _index(root)

    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.app.Base.f",
        ["a: int", "n: int"],
        {"n": "=1"},
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    app = _read(root, "pkg/app.py")
    assert "def f(self, a: int, n: int) -> int:" in app, app
    assert "a: str" not in app, app


def test_a_bare_name_keeps_each_overrides_own_spelling(temp_repo: Path) -> None:
    """The control: a BARE name carries each override's own annotation, so
    the fix above cannot be satisfied by re-annotating unconditionally."""
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/app.py": (
                "class Base:\n    def f(self, a: int) -> int:\n        return a\n\n\n"
                "class Sub(Base):\n    def f(self, a: str) -> int:\n"
                "        return len(a)\n"
            ),
        },
    )
    store, updater = _index(root)

    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.app.Base.f",
        ["a", "n: int"],
        {"n": "=1"},
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    app = _read(root, "pkg/app.py")
    assert "def f(self, a: int, n: int) -> int:" in app, app
    assert "def f(self, a: str, n: int) -> int:" in app, app


def test_dropping_a_parameter_the_body_reads_refuses(temp_repo: Path) -> None:
    """`def f(a): return a` emptied writes `def f(): return a`.

    That PARSES, so the syntax postcondition passes, and the arity
    contract only checks call sites -- the NameError surfaces when the
    function is finally called (Copilot, #1533).
    """
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/app.py": "def f(a):\n    return a\n\n\ndef call():\n    return f(1)\n",
        },
    )
    store, updater = _index(root)

    with pytest.raises(SignatureRefused, match="still reads"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.pkg.app.f",
            [],
            None,
            reingest=updater.reingest,
        )

    assert "def f(a):" in _read(root, "pkg/app.py")


def test_dropping_a_parameter_the_body_ignores_still_applies(
    temp_repo: Path,
) -> None:
    """The control: a parameter the body never reads drops cleanly, so the
    refusal cannot be satisfied by refusing every removal."""
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/app.py": "def f(a):\n    return 1\n\n\ndef call():\n    return f(2)\n",
        },
    )
    store, updater = _index(root)

    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.app.f",
        [],
        None,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    app = _read(root, "pkg/app.py")
    assert "def f():" in app, app
    assert "return f()" in app, app


@pytest.mark.parametrize(
    "body",
    [
        "def f(a):\n    return obj.a\n",
        "def f(a):\n    return helper(a=1)\n",
    ],
    ids=["attribute-name", "keyword-label"],
)
def test_a_same_named_attribute_or_keyword_is_not_a_read(
    temp_repo: Path, body: str
) -> None:
    """`obj.a` and `helper(a=1)` spell `a` without reading the parameter.

    The drop guard matched any identifier, so both refused a removal that
    is perfectly legal -- the guard has to use the same `_is_a_reference`
    test the body rewrite does (CodeRabbit, #1533).
    """
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/app.py": body + "\n\ndef call():\n    return f(1)\n",
        },
    )
    store, updater = _index(root)

    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.app.f",
        [],
        None,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert "def f():" in _read(root, "pkg/app.py")
