"""Issue #2736: functions a framework or the interpreter registers by decorator
are not dead code.

The default root decorators covered `route`, `task`, `fixture`, `command` and
the rest, but not Celery's `@shared_task`, Flask's `@app.errorhandler` and
request hooks, FastAPI's `@app.exception_handler` / `@app.middleware`,
`@atexit.register`, Django's `@receiver` or SQLAlchemy's
`@event.listens_for`. A private handler registered that way has no caller in
the code and was reported dead. A `functools.singledispatch` implementation
(`@generic.register`) is live exactly when its generic is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.dead_code import default_dead_code_config
from evals.dead_code import cgr_dead_code

PROJECT = "proj"


def _dead(tmp_path: Path, source: str) -> set[str]:
    root = tmp_path / PROJECT
    root.mkdir()
    (root / "app.py").write_text(source, encoding="utf-8")
    dead = cgr_dead_code(root, PROJECT, default_dead_code_config(False, False))
    return {qn.removeprefix(f"{PROJECT}.app.") for qn in dead}


@pytest.mark.parametrize(
    "decorator",
    [
        "@shared_task",
        "@app.errorhandler(404)",
        "@app.before_request",
        "@app.after_request",
        "@app.teardown_request",
        "@bp.before_app_request",
        "@api.exception_handler(ValueError)",
        '@api.middleware("http")',
        "@atexit.register",
        "@admin.register(Author)",
        "@receiver(post_save)",
        '@event.listens_for(Engine, "connect")',
    ],
)
def test_a_registered_private_handler_is_not_dead(
    tmp_path: Path, decorator: str
) -> None:
    source = f"{decorator}\ndef _handler(*args):\n    return 1\n"

    assert "_handler" not in _dead(tmp_path, source)


SINGLEDISPATCH = """import functools


@functools.singledispatch
def _render(obj):
    return str(obj)


@_render.register
def _render_int(obj: int):
    return f"int:{obj}"


@_render.register(float)
def _render_float(obj):
    return f"float:{obj}"
"""

USED = "\n\ndef show(x):\n    return _render(x)\n"


def test_a_singledispatch_implementation_lives_with_its_live_generic(
    tmp_path: Path,
) -> None:
    dead = _dead(tmp_path, SINGLEDISPATCH + USED)

    assert not {"_render", "_render_int", "_render_float"} & dead


METHOD_DISPATCH = """import functools


class Renderer:
    @functools.singledispatchmethod
    def render(self, obj):
        return str(obj)

    @render.register
    def _(self, obj: int):
        return f"int:{obj}"


def show(x):
    return Renderer().render(x)
"""


def test_a_singledispatchmethod_implementation_lives_with_its_generic(
    tmp_path: Path,
) -> None:
    assert not {q for q in _dead(tmp_path, METHOD_DISPATCH) if "Renderer._" in q}


STACKED = """import functools


@functools.singledispatch
def _gen_a(obj):
    return "a"


@functools.singledispatch
def _gen_b(obj):
    return "b"


@_gen_a.register(int)
@_gen_b.register(int)
def _impl(obj):
    return "int"
"""


@pytest.mark.parametrize("live_generic", ["_gen_a", "_gen_b"])
def test_an_implementation_stacked_on_two_generics_lives_with_either(
    tmp_path: Path, live_generic: str
) -> None:
    source = STACKED + f"\n\ndef show(x):\n    return {live_generic}(x)\n"

    assert "_impl" not in _dead(tmp_path, source)


ATEXIT_AND_DISPATCH = """import atexit
import functools


@functools.singledispatch
def _gen(obj):
    return "gen"


@atexit.register
@_gen.register(int)
def _cleanup(obj=0):
    return "cleanup"
"""


def test_another_register_decorator_still_roots_a_dispatch_implementation(
    tmp_path: Path,
) -> None:
    dead = _dead(tmp_path, ATEXIT_AND_DISPATCH)

    assert "_gen" in dead
    assert "_cleanup" not in dead


# Negative: what must not change.


def test_the_implementations_of_a_dead_generic_stay_dead(tmp_path: Path) -> None:
    dead = _dead(tmp_path, SINGLEDISPATCH)

    assert {"_render", "_render_int", "_render_float"} <= dead


def test_an_implementation_stacked_on_two_dead_generics_stays_dead(
    tmp_path: Path,
) -> None:
    assert {"_gen_a", "_gen_b", "_impl"} <= _dead(tmp_path, STACKED)


@pytest.mark.parametrize(
    "decorator",
    [
        "@functools.lru_cache",
        "@staticmethod_like",
        "@my.decorates",
        # The project's own registry: only `atexit` and Django's `admin` are
        # rooted by default, and `--decorator-root registry.register` adds it.
        "@registry.register",
        "@myatexit.register",
    ],
)
def test_an_unrelated_decorator_does_not_root(tmp_path: Path, decorator: str) -> None:
    source = f"{decorator}\ndef _handler(*args):\n    return 1\n"

    assert "_handler" in _dead(tmp_path, source)


def test_a_private_function_nobody_registers_is_still_dead(tmp_path: Path) -> None:
    assert "_unused" in _dead(tmp_path, "def _unused():\n    return 1\n")
