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

        # make_default runs at module load (CONFIG = ... and the default arg);
        # main runs from the `if __name__` block; helper only runs inside main's
        # body, so it is NOT a module-level call.
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
