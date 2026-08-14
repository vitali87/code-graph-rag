# dotnet-trace exports sampled .NET stacks as speedscope JSON; the converter
# must turn in-scope frame adjacencies into interchange call records, seeing
# through runtime assemblies, stripping assembly prefixes and argument lists,
# and weighting edges by sample weight (issue #1249).

from __future__ import annotations

import json

import pytest

from codebase_rag import constants as cs
from codebase_rag.trace.records import read_trace_file
from codebase_rag.trace.speedscope import convert_speedscope


def _speedscope(frames, samples, weights=None):
    return {
        "$schema": "https://www.speedscope.app/file-format-schema.json",
        "shared": {"frames": [{"name": name} for name in frames]},
        "profiles": [
            {
                "type": "sampled",
                "name": "thread 1",
                "unit": "milliseconds",
                "startValue": 0,
                "endValue": 100,
                "samples": samples,
                "weights": weights or [1] * len(samples),
            }
        ],
    }


_FRAMES = [
    "Process64 Process(1) Args: ",  # synthetic root dotnet-trace emits
    "MyApp!MyApp.Program.Main(class System.String[])",
    "System.Private.CoreLib!System.Collections.Generic.Dictionary`2[...].get_Item(!0)",
    "MyApp!MyApp.Services.Registry.Handle(class System.String)",
    "MyApp!MyApp.Services.Registry.Greet()",
    "Microsoft.Extensions.DependencyInjection!Microsoft.Extensions.Internal.Glue.Invoke()",
    "MyApp!MyApp.Worker+<RunAsync>d__3.MoveNext()",
]


def _convert(tmp_path, profile, include=("MyApp",), workload=None):
    profile_path = tmp_path / "trace.speedscope.json"
    profile_path.write_text(json.dumps(profile))
    output = tmp_path / "trace.jsonl"
    count = convert_speedscope(
        profile_path,
        output=output,
        include=include,
        workload=workload,
    )
    header, records = read_trace_file(output)
    return count, header, list(records)


def test_converts_adjacent_project_frames_to_edges(tmp_path):
    profile = _speedscope(
        _FRAMES,
        samples=[[0, 1, 3, 4], [0, 1, 3, 4], [0, 1, 3]],
        weights=[2, 3, 1],
    )

    count, header, records = _convert(tmp_path, profile)

    assert header.language == cs.TRACE_LANGUAGE_DOTNET
    assert count == len(records)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    assert ("MyApp.Services.Registry.Handle", "MyApp.Services.Registry.Greet") in edges
    dispatch = edges[
        ("MyApp.Services.Registry.Handle", "MyApp.Services.Registry.Greet")
    ]
    assert dispatch.count == 5
    assert edges[("MyApp.Program.Main", "MyApp.Services.Registry.Handle")].count == 6


def test_sees_through_runtime_assembly_frames(tmp_path):
    # Main -> Dictionary.get_Item -> Handle: the BCL frame is glue.
    profile = _speedscope(_FRAMES, samples=[[0, 1, 2, 3]])

    _count, _header, records = _convert(tmp_path, profile)

    edges = {(r.caller.qualname, r.callee.qualname) for r in records}
    assert ("MyApp.Program.Main", "MyApp.Services.Registry.Handle") in edges
    assert not any("Dictionary" in a or "Dictionary" in b for a, b in edges)


def test_di_glue_between_project_frames_is_walked_through(tmp_path):
    profile = _speedscope(_FRAMES, samples=[[0, 1, 5, 6]])

    _count, _header, records = _convert(tmp_path, profile)

    edges = {(r.caller.qualname, r.callee.qualname) for r in records}
    assert ("MyApp.Program.Main", "MyApp.Worker+<RunAsync>d__3.MoveNext") in edges


def test_frames_outside_include_prefixes_produce_no_edges(tmp_path):
    profile = _speedscope(_FRAMES, samples=[[0, 2, 5]])

    count, _header, records = _convert(tmp_path, profile)

    assert count == 0
    assert records == []


def test_workload_label_lands_on_every_record(tmp_path):
    profile = _speedscope(_FRAMES, samples=[[0, 1, 3]])

    _count, _header, records = _convert(tmp_path, profile, workload="dotnet-test")

    assert records
    for record in records:
        assert record.workloads == ("dotnet-test",)


def test_recursive_frames_do_not_self_loop_per_sample(tmp_path):
    # Handle appearing twice in one stack yields one Handle->Handle edge,
    # not an edge per repetition.
    profile = _speedscope(_FRAMES, samples=[[0, 1, 3, 3, 4]])

    _count, _header, records = _convert(tmp_path, profile)

    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    recursion = edges[
        ("MyApp.Services.Registry.Handle", "MyApp.Services.Registry.Handle")
    ]
    assert recursion.count == 1


def test_converts_evented_profiles_from_dotnet_trace(tmp_path):
    # dotnet-trace convert emits evented profiles (frame open/close), not
    # sampled ones; each in-scope activation under an in-scope ancestor is
    # one observed call relationship.
    profile = {
        "shared": {"frames": [{"name": name} for name in _FRAMES]},
        "profiles": [
            {
                "type": "evented",
                "unit": "milliseconds",
                "startValue": 0,
                "endValue": 10,
                "events": [
                    {"type": "O", "frame": 1, "at": 0},
                    {"type": "O", "frame": 2, "at": 1},
                    {"type": "O", "frame": 3, "at": 2},
                    {"type": "O", "frame": 4, "at": 3},
                    {"type": "C", "frame": 4, "at": 4},
                    {"type": "C", "frame": 3, "at": 5},
                    {"type": "O", "frame": 3, "at": 6},
                    {"type": "C", "frame": 3, "at": 7},
                    {"type": "C", "frame": 2, "at": 8},
                    {"type": "C", "frame": 1, "at": 9},
                ],
            }
        ],
    }

    count, header, records = _convert(tmp_path, profile)

    assert header.language == cs.TRACE_LANGUAGE_DOTNET
    assert count == len(records)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    # Main opened Handle twice through BCL glue; Handle opened Greet once.
    assert edges[("MyApp.Program.Main", "MyApp.Services.Registry.Handle")].count == 2
    assert (
        edges[("MyApp.Services.Registry.Handle", "MyApp.Services.Registry.Greet")].count
        == 1
    )


def test_malformed_speedscope_is_rejected(tmp_path):
    profile_path = tmp_path / "broken.json"
    profile_path.write_text("{}")

    with pytest.raises(ValueError):
        convert_speedscope(profile_path, output=tmp_path / "out.jsonl", include=("X",))


def test_fractional_weights_accumulate_before_rounding(tmp_path):
    # Speedscope permits fractional weights; truncating each sample would
    # lose their combined contribution (0.5 + 2.0 must round to 3, not 2).
    profile = _speedscope(
        _FRAMES,
        samples=[[0, 1, 3], [0, 1, 3]],
        weights=[0.5, 2.0],
    )

    _count, _header, records = _convert(tmp_path, profile)

    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    assert edges[("MyApp.Program.Main", "MyApp.Services.Registry.Handle")].count == 3


@pytest.mark.parametrize(
    "profile",
    [
        {
            "shared": {"frames": [{"name": n} for n in _FRAMES]},
            "profiles": [{"type": "sampled", "samples": "nope", "weights": []}],
        },
        {
            "shared": {"frames": [{"name": n} for n in _FRAMES]},
            "profiles": [{"type": "evented", "events": {"not": "a list"}}],
        },
        # A sampled stack that is not a list.
        {
            "shared": {"frames": [{"name": n} for n in _FRAMES]},
            "profiles": [{"type": "sampled", "samples": ["nope"], "weights": [1]}],
        },
        # A frame index outside the frame table.
        {
            "shared": {"frames": [{"name": n} for n in _FRAMES]},
            "profiles": [{"type": "sampled", "samples": [[999]], "weights": [1]}],
        },
        # A non-object event entry.
        {
            "shared": {"frames": [{"name": n} for n in _FRAMES]},
            "profiles": [{"type": "evented", "events": ["nope"]}],
        },
        # An unknown event type.
        {
            "shared": {"frames": [{"name": n} for n in _FRAMES]},
            "profiles": [{"type": "evented", "events": [{"type": "X", "frame": 0}]}],
        },
        # A close event with nothing open (stack underflow).
        {
            "shared": {"frames": [{"name": n} for n in _FRAMES]},
            "profiles": [{"type": "evented", "events": [{"type": "C"}]}],
        },
    ],
)
def test_recognised_profiles_with_malformed_payloads_are_rejected(tmp_path, profile):
    profile_path = tmp_path / "trace.speedscope.json"
    profile_path.write_text(json.dumps(profile))

    with pytest.raises(ValueError):
        convert_speedscope(
            profile_path, output=tmp_path / "out.jsonl", include=("MyApp",)
        )


def test_non_finite_sample_weights_default_to_one():
    # json.loads accepts NaN/Infinity; a non-finite weight must not corrupt the
    # aggregated count, so it falls back to 1 like any other invalid weight.
    from codebase_rag.trace.speedscope import _sample_weight

    assert _sample_weight([float("inf")], 0) == 1.0
    assert _sample_weight([float("-inf")], 0) == 1.0
    assert _sample_weight([float("nan")], 0) == 1.0
    assert _sample_weight([2.5], 0) == 2.5
