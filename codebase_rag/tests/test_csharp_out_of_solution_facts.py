from __future__ import annotations

from pathlib import Path

import pytest

_SLN = """Microsoft Visual Studio Solution File, Format Version 12.00
# Visual Studio Version 17
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Main", "Main\\Main.csproj", "{11111111-1111-1111-1111-111111111111}"
EndProject
Global
EndGlobal
"""

_SLNX = """<Solution>
  <Project Path="Main/Main.csproj" />
</Solution>
"""

_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
"""

_MAIN_SRC = """namespace Main;
public class Lib
{
    public void Go() { Step(); }
    public void Step() { }
}
"""

_SAMPLE_SRC = """namespace Samples;
public class Demo
{
    public void Run() { Helper("x"); }
    public void Helper(string s) { }
    public void Helper(int i) { }
}
"""


def _solution_repo(root: Path, solution_name: str, solution_body: str) -> None:
    (root / "Main").mkdir(parents=True)
    (root / "Samples").mkdir()
    (root / solution_name).write_text(solution_body, encoding="utf-8")
    (root / "Main" / "Main.csproj").write_text(_CSPROJ, encoding="utf-8")
    (root / "Main" / "Lib.cs").write_text(_MAIN_SRC, encoding="utf-8")
    (root / "Samples" / "Samples.csproj").write_text(_CSPROJ, encoding="utf-8")
    (root / "Samples" / "Sample.cs").write_text(_SAMPLE_SRC, encoding="utf-8")


def test_sln_member_projects_parsed(temp_repo: Path) -> None:
    from codebase_rag.parsers.csharp_frontend.frontend import _solution_member_projects

    _solution_repo(temp_repo, "Repo.sln", _SLN)
    members = _solution_member_projects(temp_repo / "Repo.sln")

    assert members == {(temp_repo / "Main" / "Main.csproj").resolve()}


def test_slnx_member_projects_parsed(temp_repo: Path) -> None:
    from codebase_rag.parsers.csharp_frontend.frontend import _solution_member_projects

    _solution_repo(temp_repo, "Repo.slnx", _SLNX)
    members = _solution_member_projects(temp_repo / "Repo.slnx")

    assert members == {(temp_repo / "Main" / "Main.csproj").resolve()}


def test_uncovered_projects_found_for_solution(temp_repo: Path) -> None:
    from codebase_rag.parsers.csharp_frontend.frontend import uncovered_csharp_projects

    _solution_repo(temp_repo, "Repo.sln", _SLN)
    uncovered = uncovered_csharp_projects(temp_repo, temp_repo / "Repo.sln")

    assert uncovered == [temp_repo / "Samples" / "Samples.csproj"]


def test_uncovered_projects_found_for_single_csproj(temp_repo: Path) -> None:
    from codebase_rag.parsers.csharp_frontend.frontend import uncovered_csharp_projects

    _solution_repo(temp_repo, "Repo.sln", _SLN)
    (temp_repo / "Repo.sln").unlink()
    uncovered = uncovered_csharp_projects(temp_repo, temp_repo / "Main" / "Main.csproj")

    assert uncovered == [temp_repo / "Samples" / "Samples.csproj"]


def test_uncovered_projects_skips_ignored_dirs(temp_repo: Path) -> None:
    from codebase_rag.parsers.csharp_frontend.frontend import uncovered_csharp_projects

    _solution_repo(temp_repo, "Repo.sln", _SLN)
    (temp_repo / "Samples" / "obj").mkdir()
    (temp_repo / "Samples" / "obj" / "Generated.csproj").write_text(
        _CSPROJ, encoding="utf-8"
    )
    uncovered = uncovered_csharp_projects(temp_repo, temp_repo / "Repo.sln")

    assert uncovered == [temp_repo / "Samples" / "Samples.csproj"]


_CONSUMER_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <Reference Include="Main">
      <HintPath>../Main/bin/Debug/net8.0/Main.dll</HintPath>
    </Reference>
  </ItemGroup>
</Project>
"""

_CONSUMER_SRC = """namespace Samples;
public class Consumer
{
    public void Use()
    {
        new Main.Lib().Go();
        System.Console.WriteLine("done");
    }
}
"""


def test_first_party_assembly_reference_is_not_marked_external(
    temp_repo: Path,
) -> None:
    # (H) Polly-shaped backfire: samples consume the repo's own published
    # (H) package, so their calls into first-party code resolve to METADATA.
    # (H) The assembly is still built from this repo, so marking those sites
    # (H) external would suppress the tree-sitter fallback edges that
    # (H) correctly bind them to the first-party source (the 3272->3238
    # (H) regression). Metadata from a first-party-named assembly must emit
    # (H) neither a positive fact nor an external fact.
    import subprocess

    from codebase_rag.parsers.csharp_frontend import (
        csharp_frontend_available,
        run_csharp_frontend,
    )

    if not csharp_frontend_available():
        pytest.skip("dotnet not available")

    _solution_repo(temp_repo, "Repo.sln", _SLN)
    (temp_repo / "Samples" / "Samples.csproj").write_text(
        _CONSUMER_CSPROJ, encoding="utf-8"
    )
    (temp_repo / "Samples" / "Sample.cs").write_text(_CONSUMER_SRC, encoding="utf-8")
    build = subprocess.run(
        ["dotnet", "build", str(temp_repo / "Main" / "Main.csproj")],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if build.returncode != 0:
        pytest.skip(f"dotnet build unavailable in this environment: {build.stderr}")

    facts = run_csharp_frontend(temp_repo)
    if not facts.call_sites:
        pytest.skip("Roslyn frontend could not build/restore in this environment")

    sample_externals = [k for k in facts.external_sites if k[0] == "Samples/Sample.cs"]
    assert not any(k[3] == "Go" for k in sample_externals), facts.external_sites
    # (H) A genuinely external call in the same file stays an external fact.
    assert any(k[3] == "WriteLine" for k in sample_externals), facts.external_sites


def test_roslyn_facts_cover_projects_outside_the_solution(temp_repo: Path) -> None:
    # (H) Polly-shaped layout: the solution lists only Main, while Samples sits
    # (H) beside it un-referenced (bench/samples projects). Facts must still
    # (H) cover Samples' files or every call in them degrades to tree-sitter
    # (H) heuristics (the #794 recall tail).
    from codebase_rag.parsers.csharp_frontend import (
        csharp_frontend_available,
        run_csharp_frontend,
    )

    if not csharp_frontend_available():
        pytest.skip("dotnet not available")

    _solution_repo(temp_repo, "Repo.sln", _SLN)
    facts = run_csharp_frontend(temp_repo)
    if not facts.call_sites:
        pytest.skip("Roslyn frontend could not build/restore in this environment")

    main_calls = [k for k in facts.call_sites if k[0] == "Main/Lib.cs"]
    sample_calls = [k for k in facts.call_sites if k[0] == "Samples/Sample.cs"]
    assert main_calls, facts.call_sites
    assert any(k[3] == "Helper" for k in sample_calls), facts.call_sites
