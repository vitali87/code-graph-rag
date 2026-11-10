"""`move` must not skip an import because the text appears somewhere.

The move rewrites call sites to `pkg.new.helper(...)` and then adds
`import pkg.new` unless it is already there. "Already there" was decided
by a regex over the raw file, so an aliased import, a comment or a string
literal all counted -- and the import was never added while the call
sites had already been rewritten. Both files parse, so a parse-based
postcondition cannot catch it; the program dies at runtime with
`NameError: name 'pkg' is not defined` on the line the move just wrote.
"""

from __future__ import annotations

import pytest

from codebase_rag.editing.move import _module_bound

BINDS = [
    pytest.param("import pkg.new\n", id="plain"),
    # `import pkg.new.sub` binds `pkg`, so `pkg.new.helper()` resolves.
    # Verified against a real interpreter: it must stay True.
    pytest.param("import pkg.new.sub\n", id="submodule-still-binds"),
]

DOES_NOT_BIND = [
    pytest.param("import pkg.new as n\nn.helper()\n", id="aliased"),
    pytest.param("# TODO: import pkg.new later\n", id="comment"),
    pytest.param('DOC = "import pkg.new"\n', id="string-literal"),
    pytest.param("helper()\n", id="absent"),
    pytest.param("from pkg import new\n", id="from-import-binds-new-not-pkg"),
]


@pytest.mark.parametrize("source", BINDS)
def test_the_module_is_bound(source: str) -> None:
    assert _module_bound(source, "pkg.new") is True


@pytest.mark.parametrize("source", DOES_NOT_BIND)
def test_the_module_is_not_bound(source: str) -> None:
    assert _module_bound(source, "pkg.new") is False


def test_an_alias_does_not_count_as_the_import() -> None:
    """The case that breaks working code, spelled out.

    `import pkg.new as n` binds only `n`. Treating it as the import means
    the move emits `pkg.new.helper()` with `pkg` unbound.
    """
    assert _module_bound("import pkg.new as n\n", "pkg.new") is False
