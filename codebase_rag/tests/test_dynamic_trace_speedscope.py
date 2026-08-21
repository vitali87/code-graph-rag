# dotnet-trace exports sampled .NET stacks as speedscope JSON; the converter
# must turn in-scope frame adjacencies into interchange call records, seeing
# through runtime assemblies, stripping assembly prefixes and argument lists,
# and weighting edges by sample weight (issue #1249).

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.trace.records import read_trace_file
from codebase_rag.trace.resolution import _demangle_clr_name
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
    # A sampled speedscope profile yields approximate edges.
    assert header.sampled is True
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
    # An evented profile records explicit open/close events, so its edges are
    # exact and must not be flagged approximate.
    assert header.sampled is False
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


def _dotnet_with_sdk() -> str | None:
    """The dotnet path only when an SDK is installed (not a runtime-only setup).

    ``shutil.which("dotnet")`` also succeeds for runtime-only installs, which
    cannot build; probing ``--list-sdks`` (bounded, at import time) degrades a
    build-incapable install to "unavailable" instead of failing the live test.
    """
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        return None
    try:
        probe = subprocess.run(
            [dotnet, "--list-sdks"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return dotnet if probe.returncode == 0 and probe.stdout.strip() else None


def _dotnet_trace() -> str | None:
    """dotnet-trace on PATH, or in the default global-tools directory."""
    found = shutil.which("dotnet-trace")
    if found:
        return found
    candidate = Path.home() / ".dotnet" / "tools" / "dotnet-trace"
    return str(candidate) if candidate.exists() else None


_dotnet = _dotnet_with_sdk()
_dotnet_trace = _dotnet_trace()
_NET_NETWORK_ERRORS = (
    "unable to load the service index",
    "no such host",
    "could not resolve",
    "connection refused",
    "connection timed out",
    "name or service not known",
    "network is unreachable",
    "failed to retrieve information about",
)
# The installed SDK is too old to target net8.0: an environment gap, not a
# regression, so it skips rather than failing.
_NET_SDK_ERRORS = ("does not support targeting", "no .net sdks were found")

# An xUnit v3 test assembly runs its tests in-process as a plain executable, so
# `dotnet-trace collect -- dotnet Tests.dll` traces the actual test run in one
# process. `dotnet test` instead forks a testhost the single-process sampler
# cannot follow, which is why the recipe runs the assembly directly.
_NET_CSPROJ = """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>disable</Nullable>
    <ImplicitUsings>disable</ImplicitUsings>
    <DebugType>portable</DebugType>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="xunit.v3" Version="1.0.0" />
  </ItemGroup>
</Project>
"""

# The concrete Dog is chosen at runtime by reflection (opaque to static
# analysis); the interface call Dispatch -> IAnimal.Speak must resolve to the
# concrete Dog.Speak, and the async methods run through compiler state machines.
_NET_TESTS = """\
using System;
using System.Runtime.CompilerServices;
using System.Threading.Tasks;
using Xunit;
namespace Demo {
  public interface IAnimal { long Speak(); }
  public class Dog : IAnimal { public long Speak() { long a=0; for(long i=0;i<40000000;i++) a+=i%7; return a; } }
  public class Worker {
    private readonly IAnimal _animal;
    public Worker(IAnimal animal) { _animal = animal; }
    // NoInlining keeps the forwarding frame in Release-mode samples, so the
    // Dispatch -> Dog.Speak dispatch edge is not erased by the JIT.
    [MethodImpl(MethodImplOptions.NoInlining)]
    public long Dispatch() { return _animal.Speak(); }
    public async Task<long> RunAsync() { await Task.Yield(); long s=0; for(int k=0;k<20;k++) s+=Dispatch(); return s; }
  }
  public class WorkerTests {
    [Fact]
    public async Task DispatchesThroughReflectionResolvedType() {
      var type = Type.GetType("Demo.Dog");
      var animal = (IAnimal)Activator.CreateInstance(type);
      var worker = new Worker(animal);
      Assert.True(await worker.RunAsync() >= 0);
    }
  }
}
"""


@pytest.mark.slow
@pytest.mark.skipif(
    _dotnet is None or _dotnet_trace is None or sys.platform == "win32",
    reason="the dotnet SDK and dotnet-trace are required (EventPipe is validated on Unix)",
)
def test_live_dotnet_test_run_captures_dispatch_and_async(tmp_path):
    # A real xUnit test run under dotnet-trace: the reflection-resolved interface
    # dispatch must resolve to the concrete Dog.Speak (the runtime-only edge; the
    # sampled stack records the concrete implementation, not a receiver-type
    # field), and the async methods must resolve back to their source
    # declarations, not the state-machine internals.
    (tmp_path / "Tests.csproj").write_text(_NET_CSPROJ)
    (tmp_path / "Tests.cs").write_text(_NET_TESTS)
    env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")

    build = subprocess.run(
        [_dotnet, "build", "-c", "Release", "-v", "q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=600,
    )
    if build.returncode != 0:
        output = (build.stdout + build.stderr).lower()
        if any(marker in output for marker in _NET_NETWORK_ERRORS):
            pytest.skip(f"NuGet unreachable: {(build.stdout + build.stderr)[-300:]}")
        if any(marker in output for marker in _NET_SDK_ERRORS):
            pytest.skip(
                f"no net8.0-capable SDK: {(build.stdout + build.stderr)[-300:]}"
            )
        raise AssertionError(f"dotnet build failed:\n{build.stdout[-1500:]}")

    dll = tmp_path / "bin" / "Release" / "net8.0" / "Tests.dll"
    nettrace = tmp_path / "run.nettrace"
    subprocess.run(
        [_dotnet_trace, "collect", "--output", str(nettrace), "--", _dotnet, str(dll)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=300,
    )
    subprocess.run(
        [
            _dotnet_trace,
            "convert",
            str(nettrace),
            "--format",
            "speedscope",
            "--output",
            str(tmp_path / "run"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=300,
    )

    output = tmp_path / "trace.jsonl"
    count = convert_speedscope(
        tmp_path / "run.speedscope.json",
        output=output,
        include=["Demo"],
        workload="dotnet-test",
    )
    assert count > 0
    _header, records_iter = read_trace_file(output)
    records = list(records_iter)
    edges = {(r.caller.qualname, r.callee.qualname) for r in records}

    # The interface dispatch resolves to the concrete Dog.Speak: static analysis
    # sees IAnimal.Speak, the runtime sample records the concrete receiver type.
    assert ("Demo.Worker.Dispatch", "Demo.Dog.Speak") in edges, sorted(edges)
    for record in records:
        assert record.workloads == ("dotnet-test",)

    # Async frames surface as state-machine MoveNext internals; the CLR resolver
    # maps each back to its source method, not the compiler's <M>d__N machinery.
    movenext = {
        frame.qualname
        for record in records
        for frame in (record.caller, record.callee)
        if "MoveNext" in frame.qualname
    }
    assert movenext, sorted(edges)
    # Each observed state-machine frame must resolve to a real async method in
    # the sample project, not merely to some string: the two async methods are
    # Worker.RunAsync and the async test method.
    expected_declarations = {
        "Demo.Worker.RunAsync",
        "Demo.WorkerTests.DispatchesThroughReflectionResolvedType",
    }
    resolved = {_demangle_clr_name(name) for name in movenext}
    assert "Demo.Worker.RunAsync" in resolved, sorted(movenext)
    assert resolved <= expected_declarations, sorted(resolved)
