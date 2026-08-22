from pathlib import Path

from evals.agentic_qa import (
    build_cases,
    build_multihop_cases,
    extract_answer_files,
    grade,
    percentile,
)

# helper() is called from two files and defined in one; the definition file
# also calls it, so it belongs to the expected answer set.
_CORE = """\
def long_helper_name():
    return 1


def caller():
    return long_helper_name()
"""

_USER = """\
from core import long_helper_name


def run():
    return long_helper_name()
"""

_IDLE = """\
VALUE = 42
"""


def _write_fixture(root: Path) -> None:
    (root / "core.py").write_text(_CORE)
    (root / "user.py").write_text(_USER)
    (root / "idle.py").write_text(_IDLE)


def test_build_cases_derives_ground_truth_from_oracle(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    cases = build_cases(tmp_path, sample=10)
    assert [c.name for c in cases] == ["long_helper_name"]
    (case,) = cases
    assert case.expected_files == frozenset({"core.py", "user.py"})
    assert "`long_helper_name`" in case.question


def test_build_cases_is_deterministic(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    assert build_cases(tmp_path, sample=10) == build_cases(tmp_path, sample=10)


def test_build_multihop_cases_walks_one_extra_hop(tmp_path: Path) -> None:
    # core.py and user.py call long_helper_name directly; second.py calls
    # wrapper_function (unique, defined in core.py, calls long_helper_name),
    # so the depth-2 answer adds second.py to the direct caller files.
    (tmp_path / "core.py").write_text(
        "def long_helper_name():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def wrapper_function():\n"
        "    return long_helper_name()\n"
    )
    (tmp_path / "user.py").write_text(
        "from core import long_helper_name\n"
        "\n"
        "\n"
        "def run_once():\n"
        "    return long_helper_name()\n"
    )
    (tmp_path / "second.py").write_text(
        "from core import wrapper_function\n\nprint(wrapper_function())\n"
    )
    cases = build_multihop_cases(tmp_path, sample=10)
    assert [c.name for c in cases] == ["long_helper_name"]
    (case,) = cases
    assert case.expected_files == frozenset({"core.py", "user.py", "second.py"})
    assert "directly calls `long_helper_name`" in case.question


def test_build_multihop_cases_drops_ambiguous_intermediates(tmp_path: Path) -> None:
    # The enclosing function `handle_event` is defined twice, so "callers of
    # the intermediate" is ambiguous under name matching → no case emitted.
    (tmp_path / "core.py").write_text(
        "def long_helper_name():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def handle_event():\n"
        "    return long_helper_name()\n"
    )
    (tmp_path / "user.py").write_text(
        "from core import long_helper_name\n"
        "\n"
        "\n"
        "def handle_event():\n"
        "    return long_helper_name()\n"
        "\n"
        "\n"
        "def other_entrypoint():\n"
        "    return handle_event()\n"
    )
    assert build_multihop_cases(tmp_path, sample=10) == []


def test_extract_answer_files_normalizes_paths() -> None:
    answer = (
        "The callers are:\n- ./db/models/query.py\n- django/apps/config.py\nplus prose."
    )
    files = extract_answer_files(answer, expected_root="django")
    assert files == frozenset({"db/models/query.py", "apps/config.py"})


def test_grade_scores_partial_and_exact() -> None:
    expected = frozenset({"a.py", "b.py"})
    partial = grade(frozenset({"a.py", "c.py"}), expected)
    assert partial["f1"] == 0.5
    assert not partial["exact"]
    perfect = grade(expected, expected)
    assert perfect["f1"] == 1.0
    assert perfect["exact"]
    empty = grade(frozenset(), expected)
    assert empty["f1"] == 0.0


def test_percentile_bounds() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 50) == 3.0
    assert percentile(values, 95) == 4.0
    assert percentile([], 95) == 0.0


def test_summarize_aggregates_metrics() -> None:
    from evals.agentic_qa import summarize
    from evals.types_defs import QARecord

    def rec(f1: float, exact: bool, seconds: float, err: str = "") -> QARecord:
        return QARecord(
            condition="grep",
            name="x",
            f1=f1,
            exact=exact,
            seconds=seconds,
            input_tokens=100,
            output_tokens=10,
            answered=[],
            expected=[],
            error=err,
        )

    summary = summarize([rec(1.0, True, 2.0), rec(0.5, False, 4.0, "boom")])
    assert summary["questions"] == 2
    assert summary["mean_f1"] == 0.75
    assert summary["exact_rate"] == 0.5
    assert summary["errors"] == 1
    assert summary["mean_seconds"] == 3.0
    assert summary["mean_input_tokens"] == 100
    assert summarize([]) == {}
