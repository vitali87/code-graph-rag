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


def test_explain_reports_a_measured_resolution_rate(tmp_path):
    """Criterion 1 of #227: the rate must be measured, not left to the caller.

    Per-frame `unresolved_reason` already says WHICH frames failed; the
    aggregate says HOW MANY, which is the number the criterion asks for and
    the one an agent needs to judge whether a report is worth acting on. A
    report where one frame in three resolved is a different artefact from one
    where all three did, and nothing distinguished them.
    """
    fetch_all = _fetch_all_for(flow_edges=[])
    report = explain_traceback(fetch_all, _P, tmp_path, _crash_text(tmp_path))

    assert report.resolution.total == 3
    assert report.resolution.resolved == 3
    assert report.resolution.rate == 1.0


def test_resolution_rate_counts_only_the_frames_that_resolved(tmp_path):
    """A partially-resolved stack must not report a perfect rate.

    Pinned as exact counts AND the ratio. Asserting only `rate < 1.0` would
    pass for any wrong denominator -- counting resolved frames over resolved
    frames, or skipping unresolved ones entirely, both of which are the
    plausible mistakes here and both of which return 1.0 or a rate over a
    denominator that hides the gap.

    The ratio is 2/5, chosen because it collides with NOTHING. The obvious
    fixture is one library frame and one repo frame, giving 0.5 -- and at
    `total=2, resolved=1` six formulas all produce 0.5: the correct one,
    `1 - resolved/total` (the INVERTED rate, which reports failure while
    looking like success), `unresolved/total`, `1/total`,
    `resolved/(resolved+1)` and a hardcoded `resolved/2`. The assertion would
    hold for five wrong implementations.

    2/5 is also not 1/3, which still collides with `1/total`. Other tests here
    do catch these collectively, via the fully-resolved and nothing-resolved
    cases -- but a guard that discriminates only with help from its siblings
    loses its coverage the moment one is weakened, and nothing records which
    test was load-bearing.
    """
    src = (tmp_path / "app" / "service.py").as_posix()
    text = (
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3.12/site-packages/lib.py", line 5, in call\n'
        "    fn()\n"
        '  File "/usr/lib/python3.12/json/decoder.py", line 9, in decode\n'
        "    raise err\n"
        '  File "/usr/lib/python3.12/json/__init__.py", line 3, in loads\n'
        "    return _default_decoder.decode(s)\n"
        f'  File "{src}", line 16, in dispatch\n'
        "    return handle(cfg)\n"
        f'  File "{src}", line 10, in handle\n'
        "    return cfg.timeout\n"
        "AttributeError: 'NoneType' object has no attribute 'timeout'\n"
    )
    fetch_all = _fetch_all_for(flow_edges=[])

    report = explain_traceback(fetch_all, _P, tmp_path, text)

    assert report.resolution.total == 5
    assert report.resolution.resolved == 2
    assert report.resolution.rate == 0.4


def test_resolution_counts_the_qualified_name_not_the_absence_of_a_reason(
    tmp_path, monkeypatch
):
    """A frame with neither a qn nor a reason counts as UNRESOLVED.

    Keying `resolved` on `unresolved_reason is None` instead passes every
    other test in this file -- measured. The two predicates agree today
    because every failure path in `FrameResolver.resolve` records a reason
    before returning None, so this condition is not reachable through the
    shipped resolver.

    Pinned anyway, and as BEHAVIOUR rather than as an early return, because
    "unreachable" describes the current resolver and not a promise:
    `_resolve_stack` derives the reason with `next(iter(stats.unresolved),
    None)`, so one future failure path that forgets to record makes it real.
    The resolver is patched to BE that future path, so the assertion runs
    through `explain_traceback` and covers the line that picks the predicate
    -- asserting over a hand-built `FrameResolutionRate` would pin arithmetic
    production never executes.
    """
    from codebase_rag.crash_correlation import FrameResolver as _FR

    real_resolve = _FR.resolve

    def resolve_without_recording(self, frame, stats):
        match = real_resolve(self, frame, stats)
        if match is None:
            # The future failure path: returns None, records nothing.
            stats.unresolved.clear()
        return match

    monkeypatch.setattr(_FR, "resolve", resolve_without_recording)

    text = (
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3.12/site-packages/lib.py", line 5, in call\n'
        "    fn()\n"
        f'  File "{(tmp_path / "app" / "service.py").as_posix()}", line 10, in handle\n'
        "    return cfg.timeout\n"
        "AttributeError: 'NoneType' object has no attribute 'timeout'\n"
    )
    report = explain_traceback(_fetch_all_for(flow_edges=[]), _P, tmp_path, text)

    outside = report.frames[0]
    assert outside.qualified_name is None
    assert outside.unresolved_reason is None, "fixture did not reach the case"

    assert report.resolution.resolved == 1, (
        "a frame with no qualified name was counted as resolved because it "
        "carried no unresolved_reason; the rate must count what the graph "
        "actually anchored"
    )
    assert report.resolution.total == 2
    assert report.resolution.rate == 0.5


def test_resolution_rate_of_a_frameless_traceback_is_zero_not_one(tmp_path):
    """total == 0 must score 0.0, exercising the empty branch itself.

    The test below covers "frames exist, none resolved". It does NOT cover
    "no frames at all", because its fixture has total == 1 so the `if
    self.total` guard is always taken -- measured: mutating the empty case to
    return 1.0 left that test green. A frameless traceback is the only input
    that runs the branch, and an exception raised with no stack produces one.
    """
    text = "Traceback (most recent call last):\nRuntimeError: boom\n"
    fetch_all = _fetch_all_for(flow_edges=[])

    report = explain_traceback(fetch_all, _P, tmp_path, text)

    assert report.resolution.total == 0
    assert report.resolution.resolved == 0
    assert report.resolution.rate == 0.0


def test_resolution_rate_of_an_unresolvable_stack_is_zero_not_one(tmp_path):
    """Frames present, none resolved: 0.0 over a real denominator.

    Distinct from the frameless case above -- here `total` is non-zero, so
    this exercises the division rather than the empty guard.
    """
    text = (
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3.12/site-packages/lib.py", line 5, in call\n'
        "    fn()\n"
        "RuntimeError: boom\n"
    )
    fetch_all = _fetch_all_for(flow_edges=[])

    report = explain_traceback(fetch_all, _P, tmp_path, text)

    assert report.resolution.total == 1
    assert report.resolution.resolved == 0
    assert report.resolution.rate == 0.0


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


def test_every_resolve_failure_path_records_an_unresolved_reason() -> None:
    """The premise `explain_traceback` picks its predicate on (issue #227).

    `crash_correlation.py` keys `resolved` on `qualified_name is not None`
    rather than on `unresolved_reason is None`, and justifies that in a comment
    saying the two agree today "because every failure path in
    `FrameResolver.resolve` records a reason before returning None".

    That claim is about ANOTHER file, which can change without touching this
    one -- so nothing would report it becoming false. If a future failure path
    forgets to record, `_resolve_stack` derives `reason = None` via
    `next(iter(stats.unresolved), None)`, and the rejected predicate would
    start counting unresolvable frames as resolved.

    Structural rather than behavioural: reaching all fourteen failure paths
    through the resolver would need fixtures for each, and the property is
    about the code's shape rather than its output. Same discipline as
    `test_mcp_read_handler_lock.py`, which parses the shipped file.

    Checks the PRECEDING SIBLING STATEMENT rather than a window of source
    text. The first version matched `"record" in context` over four lines,
    which a comment satisfies: deleting the real `stats.record(...)` call and
    leaving `return None  # record` passed it. That is "does this symbol
    appear" standing in for "is this call made" -- a guard certifying an
    invariant it could not check (CodeRabbit, #1487).
    """
    import ast

    def _records_reason(statement: ast.stmt) -> bool:
        """Whether the statement is a `stats.record(...)` call.

        The RECEIVER is checked, not just the method name. An earlier version
        accepted any `<object>.record(...)`, so a branch calling
        `logger.record(...)` before `return None` passed while recording
        nothing on `stats` -- verified by mutation (CodeRabbit, #1487).

        `stats` is the parameter name every `resolve` implementation uses for
        its `ResolutionStats`; a rename would fail here, which is the intended
        outcome since the premise this pins is stated in terms of that object.
        """
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr == "record"
        ):
            return False
        receiver = statement.value.func.value
        return isinstance(receiver, ast.Name) and receiver.id == "stats"

    def _own_body_returns(node: ast.AST) -> list[tuple[list[ast.stmt], int]]:
        """Every bare `return None` in this scope, with its sibling list.

        Nested functions are pruned: a `return None` inside a closure belongs
        to that closure's contract, not the resolver's, and its siblings are
        not the resolver's branch.
        """
        found: list[tuple[list[ast.stmt], int]] = []
        stack: list[ast.AST] = [node]
        while stack:
            current = stack.pop()
            for field, value in ast.iter_fields(current):
                if not isinstance(value, list):
                    continue
                body = [item for item in value if isinstance(item, ast.stmt)]
                for index, item in enumerate(body):
                    if isinstance(
                        item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                    ):
                        continue
                    if (
                        isinstance(item, ast.Return)
                        and isinstance(item.value, ast.Constant)
                        and item.value.value is None
                    ):
                        found.append((body, index))
                    stack.append(item)
        return found

    source = (
        Path(__file__).resolve().parents[1] / "trace" / "resolution.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    unrecorded: list[str] = []
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "resolve":
            continue
        for body, index in _own_body_returns(node):
            checked += 1
            preceding = body[index - 1] if index else None
            if preceding is None or not _records_reason(preceding):
                unrecorded.append(f"resolution.py:{body[index].lineno}")

    assert checked, "found no bare `return None` in any resolve(); parser drifted"
    assert not unrecorded, (
        f"{unrecorded} return None without an immediately preceding "
        "`.record(...)` call, so `unresolved_reason is None` no longer implies "
        "the frame resolved -- see the predicate comment in "
        "crash_correlation.explain_traceback"
    )
