# Traceback-to-graph correlation (issue #227): CPython traceback text maps to
# graph nodes through the dynamic-trace resolver, and root-cause candidates
# rank by FLOWS_TO sources into the failing frame, presence on the crashing
# stack, and reverse-CALLS proximity to the failure.

from __future__ import annotations

import traceback
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.crash_correlation import (
    CYPHER_CRASH_CALLS,
    explain_traceback,
    parse_python_traceback,
    rank_root_causes,
)
from codebase_rag.cypher_queries import CYPHER_TRACE_CALLABLES
from codebase_rag.flow_verdict import CYPHER_FLOW_COVERAGE_GAPS, CYPHER_FLOW_EDGES

_P = "proj__cafe01"


def _real_chained_traceback() -> str:
    """A genuine CPython traceback, chained, with a method frame."""

    class Service:
        def handle(self) -> None:
            try:
                {}["missing"]
            except KeyError as e:
                raise ValueError("wrapped failure") from e

    try:
        Service().handle()
    except ValueError:
        return traceback.format_exc()
    raise AssertionError("unreachable")


def test_parses_a_real_chained_traceback_using_the_final_section():
    parsed = parse_python_traceback(_real_chained_traceback())
    assert parsed.exception_type == "ValueError"
    assert parsed.exception_message == "wrapped failure"
    # Only the propagated (final) section's frames are kept, and the source
    # snippet lines between File lines are not misread as frames.
    assert [frame.qualname for frame in parsed.frames] == [
        "_real_chained_traceback",
        "handle",
    ]
    assert all(
        frame.path.endswith("test_crash_correlation.py") for frame in parsed.frames
    )
    assert all(frame.line > 0 for frame in parsed.frames)


def test_parses_a_bare_exception_without_message():
    text = (
        "Traceback (most recent call last):\n"
        '  File "/app/main.py", line 3, in <module>\n'
        "    run()\n"
        "KeyboardInterrupt\n"
    )
    parsed = parse_python_traceback(text)
    assert parsed.exception_type == "KeyboardInterrupt"
    assert parsed.exception_message == ""
    assert parsed.frames[0].qualname == "<module>"


def _callable_row(label: str, qn: str, start: int | None, end: int | None) -> dict:
    return {
        cs.KEY_LABEL: label,
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_PATH: "app/service.py",
        cs.KEY_START_LINE: start,
        cs.KEY_END_LINE: end,
    }


def _fetch_all_for(*, flow_edges: list[tuple[str, str]], gaps: list[str] | None = None):
    callables = [
        _callable_row(cs.NodeLabel.MODULE, f"{_P}.app.service", None, None),
        _callable_row(cs.NodeLabel.FUNCTION, f"{_P}.app.service.load_config", 2, 6),
        _callable_row(cs.NodeLabel.FUNCTION, f"{_P}.app.service.handle", 8, 12),
        _callable_row(cs.NodeLabel.FUNCTION, f"{_P}.app.service.dispatch", 14, 18),
        _callable_row(cs.NodeLabel.FUNCTION, f"{_P}.app.service.main", 20, 24),
    ]
    calls = [
        {"from_qn": f"{_P}.app.service.main", "to_qn": f"{_P}.app.service.dispatch"},
        {"from_qn": f"{_P}.app.service.dispatch", "to_qn": f"{_P}.app.service.handle"},
        {
            "from_qn": f"{_P}.app.service.main",
            "to_qn": f"{_P}.app.service.load_config",
        },
    ]

    def fetch_all(query: str, params: dict | None = None) -> list[dict]:
        if query == CYPHER_TRACE_CALLABLES:
            return callables
        if query == CYPHER_CRASH_CALLS:
            return calls
        if query == CYPHER_FLOW_EDGES:
            return [{"source": s, "target": t} for s, t in flow_edges]
        if query == CYPHER_FLOW_COVERAGE_GAPS:
            return [{cs.KEY_PATH: path} for path in gaps or []]
        raise AssertionError(f"unexpected query: {query}")

    return fetch_all


def _crash_text(repo: Path) -> str:
    src = (repo / "app" / "service.py").as_posix()
    return (
        "Traceback (most recent call last):\n"
        f'  File "{src}", line 22, in main\n'
        "    dispatch(cfg)\n"
        f'  File "{src}", line 16, in dispatch\n'
        "    return handle(cfg)\n"
        f'  File "{src}", line 10, in handle\n'
        "    return cfg.timeout\n"
        "AttributeError: 'NoneType' object has no attribute 'timeout'\n"
    )


def test_explain_resolves_frames_and_attaches_neighbourhood(tmp_path):
    fetch_all = _fetch_all_for(
        flow_edges=[(f"{_P}.app.service.load_config", f"{_P}.app.service.handle")]
    )
    report = explain_traceback(fetch_all, _P, tmp_path, _crash_text(tmp_path))
    assert report.exception_type == "AttributeError"
    assert [frame.qualified_name for frame in report.frames] == [
        f"{_P}.app.service.main",
        f"{_P}.app.service.dispatch",
        f"{_P}.app.service.handle",
    ]
    failing = report.frames[-1]
    assert failing.callers == (f"{_P}.app.service.dispatch",)
    assert failing.flow_sources == (f"{_P}.app.service.load_config",)
    assert report.frames[0].callees == (
        f"{_P}.app.service.dispatch",
        f"{_P}.app.service.load_config",
    )
    assert report.flow_gaps == ()


def test_explain_marks_out_of_repo_frames_with_a_reason(tmp_path):
    text = (
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3.12/site-packages/lib.py", line 5, in call\n'
        "    fn()\n"
        f'  File "{(tmp_path / "app" / "service.py").as_posix()}", line 10, in handle\n'
        "    return cfg.timeout\n"
        "AttributeError: 'NoneType' object has no attribute 'timeout'\n"
    )
    fetch_all = _fetch_all_for(flow_edges=[])
    report = explain_traceback(fetch_all, _P, tmp_path, text)
    outside, inside = report.frames
    assert outside.qualified_name is None
    assert outside.unresolved_reason == cs.TraceUnresolvedReason.OUTSIDE_REPO.value
    assert inside.qualified_name == f"{_P}.app.service.handle"


def test_rank_places_the_flow_writer_in_the_top_candidates(tmp_path):
    # The planted defect: load_config returns None, which FLOWS_TO the failing
    # read in handle. It is neither on the stack nor a caller of handle, so
    # only the flow signal can surface it.
    fetch_all = _fetch_all_for(
        flow_edges=[(f"{_P}.app.service.load_config", f"{_P}.app.service.handle")]
    )
    report = rank_root_causes(fetch_all, _P, tmp_path, _crash_text(tmp_path))
    assert report.failing == f"{_P}.app.service.handle"
    assert report.flow_used is True
    ranked = [candidate.qualified_name for candidate in report.candidates]
    assert ranked == [
        f"{_P}.app.service.dispatch",
        f"{_P}.app.service.load_config",
        f"{_P}.app.service.main",
    ]
    dispatch, load_config, main = report.candidates
    # dispatch: direct caller (0.4) + one frame above the failure (0.3).
    assert dispatch.score == 0.7
    assert dispatch.call_path == (
        f"{_P}.app.service.dispatch",
        f"{_P}.app.service.handle",
    )
    # load_config: pure flow signal, with the node's own location attached.
    assert load_config.score == 0.6
    assert load_config.path == "app/service.py"
    assert load_config.line == 2
    assert any("FLOWS_TO" in reason for reason in load_config.reasons)
    # main: two-step caller (0.2) + on the stack (0.3), shortest path recorded.
    assert main.score == 0.5
    assert main.call_path == (
        f"{_P}.app.service.main",
        f"{_P}.app.service.dispatch",
        f"{_P}.app.service.handle",
    )


def test_rank_degrades_to_calls_only_when_flow_is_absent(tmp_path):
    fetch_all = _fetch_all_for(flow_edges=[], gaps=["app/service.py"])
    report = rank_root_causes(fetch_all, _P, tmp_path, _crash_text(tmp_path))
    assert report.flow_used is False
    assert report.flow_gaps == ("app/service.py",)
    ranked = [candidate.qualified_name for candidate in report.candidates]
    assert ranked == [f"{_P}.app.service.dispatch", f"{_P}.app.service.main"]


def test_parses_a_real_exception_group_using_the_last_sub_exception():
    def leaf_a():
        raise ValueError("a failed")

    def gather():
        errs = []
        try:
            leaf_a()
        except Exception as e:
            errs.append(e)
        raise ExceptionGroup("parallel failures", errs)

    try:
        gather()
    except BaseException:
        text = traceback.format_exc()
    # The box margin (+, |) is stripped and the last sub-exception's own
    # traceback wins: the deepest real cause, not the group wrapper.
    parsed = parse_python_traceback(text)
    assert parsed.exception_type == "ValueError"
    assert parsed.exception_message == "a failed"
    assert [frame.qualname for frame in parsed.frames] == ["gather", "leaf_a"]


def test_parses_a_unicode_exception_name():
    text = (
        "Traceback (most recent call last):\n"
        '  File "/app/main.py", line 3, in <module>\n'
        "    run()\n"
        "ÉchecRéseau: connexion perdue\n"
    )
    parsed = parse_python_traceback(text)
    assert parsed.exception_type == "ÉchecRéseau"
    assert parsed.exception_message == "connexion perdue"


def test_flow_gaps_survive_unrelated_flow_edges(tmp_path):
    # A flow edge elsewhere in the project must not hide that coverage gaps
    # exist: the failing file's absence from flow analysis stays disclosed.
    fetch_all = _fetch_all_for(
        flow_edges=[(f"{_P}.other.writer", f"{_P}.other.reader")],
        gaps=["app/service.py"],
    )
    report = rank_root_causes(fetch_all, _P, tmp_path, _crash_text(tmp_path))
    assert report.flow_used is True
    assert report.flow_gaps == ("app/service.py",)


def test_rank_anchors_on_the_innermost_resolved_frame_and_discloses_it(tmp_path):
    # The crash propagates from inside a library: the deepest frame cannot
    # resolve, the anchor is the deepest project frame, and the report says
    # the anchor is not the crash site.
    src = (tmp_path / "app" / "service.py").as_posix()
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{src}", line 16, in dispatch\n'
        "    return handle(cfg)\n"
        f'  File "{src}", line 10, in handle\n'
        "    return lib.parse(cfg)\n"
        '  File "/usr/lib/python3.12/site-packages/lib.py", line 5, in parse\n'
        "    return cfg.timeout\n"
        "AttributeError: 'NoneType' object has no attribute 'timeout'\n"
    )
    fetch_all = _fetch_all_for(flow_edges=[])
    report = rank_root_causes(fetch_all, _P, tmp_path, text)
    assert report.failing == f"{_P}.app.service.handle"
    assert report.anchor_is_crash_site is False
    # The fully resolved scenario claims the crash site outright.
    resolved = rank_root_causes(fetch_all, _P, tmp_path, _crash_text(tmp_path))
    assert resolved.anchor_is_crash_site is True


def test_shortest_call_paths_are_deterministic_over_diamonds(tmp_path):
    # main reaches handle through both branch_a and branch_b; the recorded
    # shortest path must not depend on graph row order.
    callables = [
        _callable_row(cs.NodeLabel.FUNCTION, f"{_P}.app.service.handle", 8, 12),
        _callable_row(cs.NodeLabel.FUNCTION, f"{_P}.app.service.branch_a", 14, 16),
        _callable_row(cs.NodeLabel.FUNCTION, f"{_P}.app.service.branch_b", 18, 20),
        _callable_row(cs.NodeLabel.FUNCTION, f"{_P}.app.service.main", 22, 26),
    ]
    calls = [
        {"from_qn": f"{_P}.app.service.branch_b", "to_qn": f"{_P}.app.service.handle"},
        {"from_qn": f"{_P}.app.service.branch_a", "to_qn": f"{_P}.app.service.handle"},
        {"from_qn": f"{_P}.app.service.main", "to_qn": f"{_P}.app.service.branch_b"},
        {"from_qn": f"{_P}.app.service.main", "to_qn": f"{_P}.app.service.branch_a"},
    ]

    def fetch_all(query: str, params: dict | None = None) -> list[dict]:
        if query == CYPHER_TRACE_CALLABLES:
            return callables
        if query == CYPHER_CRASH_CALLS:
            return calls
        return []

    src = (tmp_path / "app" / "service.py").as_posix()
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{src}", line 10, in handle\n'
        "    boom()\n"
        "RuntimeError: boom\n"
    )
    report = rank_root_causes(fetch_all, _P, tmp_path, text)
    main = next(
        c for c in report.candidates if c.qualified_name == f"{_P}.app.service.main"
    )
    assert main.call_path == (
        f"{_P}.app.service.main",
        f"{_P}.app.service.branch_a",
        f"{_P}.app.service.handle",
    )


def test_rank_with_no_resolvable_frame_reports_nothing(tmp_path):
    text = (
        "Traceback (most recent call last):\n"
        '  File "/elsewhere/x.py", line 5, in run\n'
        "    boom()\n"
        "RuntimeError: nope\n"
    )
    fetch_all = _fetch_all_for(flow_edges=[])
    report = rank_root_causes(fetch_all, _P, tmp_path, text)
    assert report.failing is None
    assert report.candidates == ()
    assert report.exception_type == "RuntimeError"
