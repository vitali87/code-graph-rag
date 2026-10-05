"""A module-scoped fixture over a missing grammar skips, not fails (#1371).

The autouse `_skip_when_grammar_missing` wraps `GraphUpdater.run` per test,
and a module-scoped fixture is set up before any function-scoped autouse
one. So a module fixture that indexed a JavaScript repo on a base install
(Python grammar only) ran over an empty graph and failed every test of the
module, the "Unit Tests (base install)" job's failures on recent PRs, where
a per-test fixture would have skipped. `create_and_run_updater`, which every
such fixture calls, now makes the same check itself. JavaScript is reported
unavailable here, so this runs the same on every install.
"""

from __future__ import annotations

import pytest

MODULE = """
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests import conftest as tests_conftest

UNAVAILABLE = {unavailable}


@pytest.fixture(scope="module")
def indexed(tmp_path_factory):
    root = tmp_path_factory.mktemp("repo")
    (root / "{filename}").write_text("{source}")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tests_conftest, "_unavailable_grammars", lambda: UNAVAILABLE)
        yield tests_conftest.create_and_run_updater(root, MagicMock())


def test_reads_the_graph(indexed):
    {body}
"""

JS_UNAVAILABLE = "frozenset({cs.SupportedLanguage.JS})"
JS_FILE = ("app.js", "export function f() { return 1; }\\\\n")
PY_FILE = ("app.py", "def f():\\\\n    return 1\\\\n")


def _run(
    pytester: pytest.Pytester, unavailable: str, file: tuple[str, str], body: str
) -> pytest.RunResult:
    filename, source = file
    pytester.makepyfile(
        MODULE.format(
            unavailable=unavailable, filename=filename, source=source, body=body
        )
    )
    # A subprocess, so the outer session's own grammar-skip wrapper around
    # `GraphUpdater.run` is not in the inner one: on a base install it would
    # skip the full-install case for the real missing grammar.
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rs")


def test_a_module_fixture_over_a_missing_grammar_skips(
    pytester: pytest.Pytester,
) -> None:
    result = _run(
        pytester,
        JS_UNAVAILABLE,
        JS_FILE,
        'pytest.fail("indexed a repo whose grammar is missing")',
    )

    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*javascript parser not available*"])


# Negative: what must not change.


def test_a_module_fixture_over_installed_grammars_still_runs(
    pytester: pytest.Pytester,
) -> None:
    result = _run(pytester, JS_UNAVAILABLE, PY_FILE, "assert indexed is not None")

    result.assert_outcomes(passed=1)


def test_a_full_install_still_indexes_the_repo(pytester: pytest.Pytester) -> None:
    result = _run(pytester, "frozenset()", JS_FILE, "assert indexed is not None")

    result.assert_outcomes(passed=1)
