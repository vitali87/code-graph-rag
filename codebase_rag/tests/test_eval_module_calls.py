from __future__ import annotations

from pathlib import Path

from evals.module_calls import (
    cgr_module_calls,
    oracle_module_calls,
    score_module_calls,
)

_FIXTURE = """def make_default():
    return 1


def helper():
    return 2


def main():
    helper()


def with_default(x=make_default()):
    return x


CONFIG = make_default()


if __name__ == "__main__":
    main()
"""


def _names(edges: set[tuple[str, ...]]) -> set[str]:
    return {e.target_name for e in edges}


class TestModuleCallEval:
    def _write(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "app.py").write_text(_FIXTURE, encoding="utf-8")
        return proj

    def test_oracle_counts_only_definition_time_calls(self, tmp_path: Path) -> None:
        proj = self._write(tmp_path)
        oracle = oracle_module_calls(proj, "proj")

        # (H) make_default runs at module load (CONFIG = ... and the default arg);
        # (H) main runs from the `if __name__` block; helper only runs inside main's
        # (H) body, so it is NOT a module-level call.
        assert _names(oracle) == {"make_default", "main"}

    def test_cgr_matches_oracle_module_calls(self, tmp_path: Path) -> None:
        proj = self._write(tmp_path)
        cgr = cgr_module_calls(proj, "proj")
        oracle = oracle_module_calls(proj, "proj")

        _tp, fp, fn, precision, recall = score_module_calls(cgr, oracle)

        assert fp == 0, f"spurious module calls: {sorted(_names(cgr - oracle))}"
        assert fn == 0, f"missed module calls: {sorted(_names(oracle - cgr))}"
        assert precision == 1.0
        assert recall == 1.0

    def test_nested_call_is_not_module_attributed(self, tmp_path: Path) -> None:
        proj = self._write(tmp_path)
        cgr = cgr_module_calls(proj, "proj")

        assert "helper" not in _names(cgr)

    def _oracle_for(self, tmp_path: Path, source: str) -> set[str]:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "app.py").write_text(source, encoding="utf-8")
        return _names(oracle_module_calls(proj, "proj"))

    def test_lambda_body_call_is_deferred(self, tmp_path: Path) -> None:
        # (H) `helper` runs only when `work()` is called, not at import.
        names = self._oracle_for(
            tmp_path,
            "def helper():\n    return 1\n\n\nwork = lambda: helper()\n",
        )
        assert "helper" not in names

    def test_generator_expression_call_is_deferred(self, tmp_path: Path) -> None:
        # (H) a generator is lazy: `helper` runs only when the generator is consumed.
        names = self._oracle_for(
            tmp_path,
            "def helper():\n    return 1\n\n\ngen = (helper() for _ in range(2))\n",
        )
        assert "helper" not in names

    def test_generator_outermost_iterable_is_eager(self, tmp_path: Path) -> None:
        # (H) the first iterable of a generator is evaluated when the generator is
        # (H) created (at import), so `load_items` is a module call but the lazy
        # (H) body call `helper` is not.
        names = self._oracle_for(
            tmp_path,
            "def helper():\n    return 1\n\n\n"
            "def load_items():\n    return [1]\n\n\n"
            "gen = (helper(x) for x in load_items())\n",
        )
        assert "load_items" in names
        assert "helper" not in names

    def test_list_comprehension_call_is_module_attributed(self, tmp_path: Path) -> None:
        # (H) a list comprehension runs eagerly at import, so its call counts.
        names = self._oracle_for(
            tmp_path,
            "def helper():\n    return 1\n\n\nout = [helper() for _ in range(2)]\n",
        )
        assert "helper" in names

    def test_return_annotation_counted_without_future_import(
        self, tmp_path: Path
    ) -> None:
        # (H) without postponed annotations, `Result()` runs at import.
        names = self._oracle_for(
            tmp_path,
            "def Result():\n    return 1\n\n\ndef route() -> Result():\n    return 1\n",
        )
        assert "Result" in names

    def test_annotation_not_counted_with_future_import(self, tmp_path: Path) -> None:
        # (H) with postponed annotations, the annotation is a string and never runs.
        names = self._oracle_for(
            tmp_path,
            "from __future__ import annotations\n\n\n"
            "def Result():\n    return 1\n\n\ndef route() -> Result():\n    return 1\n",
        )
        assert "Result" not in names
