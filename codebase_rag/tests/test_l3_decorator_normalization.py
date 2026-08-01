# Covers the L3 eval harness (evals/calls_trace.py): a call to a functools.wraps
# decorated function dispatches through the decorator's generic wrapper at
# runtime, but cgr's static graph resolves it to the function itself. The trace
# must attribute the wrapper frame to the wrapped function so the two agree.
from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

from evals.calls_trace import trace_calls

MOD_SRC = textwrap.dedent(
    """
    from functools import wraps


    def guard(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        return wrapper


    def helper():
        return 1


    @guard
    def target_fn():
        return helper()


    def caller():
        return target_fn()
    """
)


def _load_module(mod_path: Path):
    spec = importlib.util.spec_from_file_location("evaltest_decorator_mod", mod_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _trace(tmp_path: Path) -> set[tuple[str, str]]:
    pkg = tmp_path / "pkgx"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    mod_path = pkg / "mod.py"
    mod_path.write_text(MOD_SRC)
    module = _load_module(mod_path)
    return trace_calls(module.caller, pkg, "pkgx")


class TestDecoratorWrapperNormalization:
    def test_call_attributed_to_wrapped_function_not_wrapper(
        self, tmp_path: Path
    ) -> None:
        edges = _trace(tmp_path)
        assert ("pkgx.mod.caller", "pkgx.mod.target_fn") in edges, edges

    def test_no_generic_wrapper_node_appears(self, tmp_path: Path) -> None:
        edges = _trace(tmp_path)
        wrapper_edges = [
            (frm, to)
            for frm, to in edges
            if frm.endswith("wrapper") or to.endswith("wrapper")
        ]
        assert wrapper_edges == [], wrapper_edges

    def test_wrapped_function_body_calls_are_preserved(self, tmp_path: Path) -> None:
        edges = _trace(tmp_path)
        assert ("pkgx.mod.target_fn", "pkgx.mod.helper") in edges, edges
