# change_signature on JavaScript definitions (issue #1908), end to end
# against a real index of a small fixture repo. The header and site rules
# themselves are covered in test_signature_js.py and test_signature_js_bind.py.

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.editing.signature import change_signature
from codebase_rag.editing.signature_spec import SignatureRefused, SignatureReport
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "sigjs"
UTIL = "lib/util.js"
APP = "lib/app.js"
HELPER = f"{PROJECT}.lib.util.helper"
AREA = f"{PROJECT}.lib.util.Shape.area"
UTIL_SOURCE = (
    "function helper(a, b = 'x') {\n  return String(a) + b;\n}\n\n"
    "class Shape {\n  area(scale, pad = 0) {\n    return scale + pad;\n  }\n}\n\n"
    "module.exports = { helper, Shape };\n"
)
APP_SOURCE = (
    "const { helper, Shape } = require('./util');\n\n"
    "function run() {\n  return helper(2);\n}\n\n"
    "function runBoth() {\n  return helper(3, 'z') + new Shape().area(1, 2);\n}\n"
)


def _indexed(temp_repo: Path, util: str = UTIL_SOURCE, app: str = APP_SOURCE) -> Path:
    root = temp_repo / PROJECT
    for rel, text in {UTIL: util, APP: app}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    _STORES[root] = store
    return root


_STORES: dict[Path, _StatefulIngestor] = {}


def _change(
    root: Path,
    qn: str,
    new_params: list[str],
    mapping: dict[str, str] | None = None,
) -> SignatureReport:
    return change_signature(
        root, _STORES[root].fetch_all, PROJECT, qn, new_params, mapping
    )


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def test_a_new_parameter_is_added_to_the_header_and_every_site(
    temp_repo: Path,
) -> None:
    root = _indexed(temp_repo)

    report = _change(root, HELPER, ["a", "n", "b"], {"n": "=1"})

    assert report.applied, report.message
    assert "function helper(a, n, b = 'x') {" in _read(root, UTIL)
    app = _read(root, APP)
    assert "return helper(2, 1);" in app
    assert "return helper(3, 1, 'z')" in app


def test_a_method_takes_a_new_trailing_parameter(temp_repo: Path) -> None:
    root = _indexed(temp_repo)

    report = _change(root, AREA, ["scale", "pad = 0", "unit = 'px'"], {"unit": "='em'"})

    assert report.applied, report.message
    assert "  area(scale, pad = 0, unit = 'px') {" in _read(root, UTIL)
    assert "new Shape().area(1, 2, 'em')" in _read(root, APP)


def test_reordered_names_swap_places_at_every_site(temp_repo: Path) -> None:
    root = _indexed(
        temp_repo,
        util="function helper(a, b) {\n  return a - b;\n}\nmodule.exports = { helper };\n",
        app="const { helper } = require('./util');\nconst x = 1;\nconst y = 2;\n"
        "function run() {\n  return helper(x, y);\n}\n",
    )

    report = _change(root, HELPER, ["b", "a"])

    assert report.applied, report.message
    assert "function helper(b, a) {" in _read(root, UTIL)
    assert "return helper(y, x);" in _read(root, APP)


def test_a_skipped_default_is_passed_as_void_0(temp_repo: Path) -> None:
    # JavaScript has no keywords to skip a slot with; the undefined value is
    # what an omitted argument is, and it still selects the default. `void 0`
    # spells it where a caller's own `undefined` binding cannot shadow it.
    root = _indexed(temp_repo)

    report = _change(root, HELPER, ["a", "b = 'x'", "c = 0"], {"c": "=5"})

    assert report.applied, report.message
    assert "return helper(2, void 0, 5);" in _read(root, APP)


def test_a_required_parameter_may_follow_a_defaulted_one(temp_repo: Path) -> None:
    # Valid JavaScript, unlike Python; a site that leaves `b` out would stay
    # unmapped, since there is no value to carry.
    util = "function helper(a = 1, b) {\n  return a + b;\n}\nmodule.exports = { helper };\n"
    app = "const { helper } = require('./util');\nfunction run() {\n  return helper(2, 3);\n}\n"
    root = _indexed(temp_repo, util=util, app=app)

    report = _change(root, HELPER, ["a = 1", "b", "c"], {"c": "=4"})

    assert report.applied, report.message
    assert "function helper(a = 1, b, c) {" in _read(root, UTIL)
    assert "return helper(2, 3, 4);" in _read(root, APP)


def test_a_mapped_value_that_is_not_one_argument_refuses(temp_repo: Path) -> None:
    root = _indexed(temp_repo)

    with pytest.raises(SignatureRefused, match="not one JavaScript expression"):
        _change(root, HELPER, ["a", "n", "b"], {"n": "=1, 2"})
    assert _read(root, UTIL) == UTIL_SOURCE
    assert _read(root, APP) == APP_SOURCE


def test_a_dropped_parameter_the_body_still_reads_refuses(temp_repo: Path) -> None:
    root = _indexed(temp_repo)

    with pytest.raises(SignatureRefused, match="still uses `b`"):
        _change(root, HELPER, ["a"])
    assert _read(root, UTIL) == UTIL_SOURCE


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        # `(b = a, a = 1)` would throw ReferenceError on `helper()`: `a` is
        # not bound yet when `b`'s default runs.
        ("(a = 1, b = a)", "the default of b reads `a`"),
        # `(b = g(), a = f())` would call `g` before `f` on `helper()`.
        ("(a = f(), b = g())", "would run in the other order"),
    ],
    ids=["reads-later", "runs-code"],
)
def test_a_reorder_that_changes_how_defaults_run_refuses(
    temp_repo: Path, params: str, reason: str
) -> None:
    util = (
        "function f() { return 1; }\nfunction g() { return 2; }\n"
        f"function helper{params} {{\n  return b;\n}}\n"
        "module.exports = { helper };\n"
    )
    app = "const { helper } = require('./util');\nfunction run() {\n  return helper();\n}\n"
    root = _indexed(temp_repo, util=util, app=app)

    with pytest.raises(SignatureRefused, match=reason):
        _change(root, HELPER, ["b", "a"])
    assert _read(root, UTIL) == util
    assert _read(root, APP) == app


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        ("helper(...xs)", "cannot be read positionally"),
        ("helper(1, 2, 3)", "passes 3"),
        ("helper(first(), second())", "in another order"),
    ],
    ids=["spread", "too-many", "effects-reordered"],
)
def test_a_site_positional_rewriting_cannot_keep_is_left_unmapped(
    temp_repo: Path, call: str, reason: str
) -> None:
    app = (
        "const { helper } = require('./util');\nconst xs = [1];\n"
        "function first() { return 1; }\nfunction second() { return 2; }\n"
        f"function run() {{\n  return {call};\n}}\n"
    )
    root = _indexed(
        temp_repo,
        util="function helper(a, b) {\n  return a - b;\n}\nmodule.exports = { helper };\n",
        app=app,
    )

    report = _change(root, HELPER, ["b", "a"])

    assert [site.reason for site in report.unmapped if reason in site.reason], (
        report.unmapped
    )
    assert f"return {call};" in _read(root, APP)
