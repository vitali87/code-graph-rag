# The release workflow once generated Latest News with a second model call
# whose prompt included the previous NEWS.md entries as dedup context. The
# model anchored on those old entries and paraphrased them into fake "news":
# v0.0.720 re-announced Ruby support, structural search and replace, and
# data-flow tracing for C#/Java/C/Go, none of which shipped in that window,
# while the Highlights generated in the same run from the same PR titles were
# accurate. News is therefore derived from the Highlights fragment, with all
# filtering, marker normalisation, and dedup handled deterministically by
# scripts/update_news.py. Guard against reintroducing a
# dedicated news generation or feeding old entries back to the model.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import git_env
from scripts import breaking_notes as bn

_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "version-bump.yml"
)


def _steps() -> list[dict[str, str]]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["bump-version"]["steps"]


def _news_update_script() -> str:
    step = next(step for step in _steps() if step.get("name") == "Update NEWS.md")
    return step["run"]


def test_no_dedicated_news_generation_step() -> None:
    # A second model call for news is the regression: its dedup context of old
    # NEWS entries is what the model paraphrased into fake news.
    names = [step.get("name", "") for step in _steps()]
    assert "Generate news bullets" not in names


def test_news_is_derived_from_highlights() -> None:
    assert "scripts/update_news.py /tmp/highlights.md" in _news_update_script()


def test_no_step_feeds_existing_news_entries_to_a_model() -> None:
    # Grepping NEWS.md entries into a prompt payload is how old entries reached
    # the model's context in the first place.
    for step in _steps():
        script = step.get("run", "")
        assert "known.md" not in script
        assert "newsbullets" not in script


def _security_notice_script() -> str:
    step = next(
        step for step in _steps() if step.get("name") == "Prepend security notice"
    )
    return step["run"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the step runs under the Ubuntu runner's bash; on the Windows runner "
    "`bash` resolves to the WSL launcher, which has no distribution installed",
)
def test_security_notice_falls_back_when_the_advisory_fetch_fails(
    tmp_path: Path,
) -> None:
    # `gh api` prints its HTTP error body to STDOUT, so a draft advisory the
    # token cannot read used to land as the advisory line of the release
    # notes (v0.0.845). The step must fall back to the "see advisory" pointer.
    import os
    import subprocess

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/bin/sh\nprintf '%s' '{\"message\":\"Resource not accessible by "
        'integration","status":"403"}\'\nexit 1\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    out = tmp_path / "security.md"
    script = _security_notice_script().replace("/tmp/security.md", str(out))
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "GITHUB_REPOSITORY": "vitali87/code-graph-rag",
        "GHSA": "GHSA-aaaa-bbbb-cccc",
    }

    subprocess.run(["bash", "-e", "-c", script], check=True, env=env)

    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "## 🔒 Security"
    assert lines[2].startswith("This release contains a security fix; see advisory")
    assert "GHSA-aaaa-bbbb-cccc" in lines[2]
    assert not any("403" in line for line in lines)


_UBUNTU_BASH_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the step runs under the Ubuntu runner's bash; on the Windows runner "
    "`bash` resolves to the WSL launcher, which has no distribution installed",
)
_REPOSITORY = "vitali87/code-graph-rag"
_GHSA = "GHSA-xcq7-p3pf-p88h"
_REPOSITORY_ADVISORY = f"repos/{_REPOSITORY}/security-advisories/{_GHSA}"
_GLOBAL_ADVISORY = f"advisories/{_GHSA}"
_SUMMARY = "OpenTelemetry Python: Unbounded tracestate parsing in opentelemetry-api"
_ADVISORY = {
    "ghsa_id": _GHSA,
    "cve_id": "CVE-2026-114603",
    "summary": _SUMMARY,
    "severity": "medium",
}
# Serves `gh api <path> --jq <filter>` from <path>.json through the real jq, so
# the step's filters run as on the runner, and answers a missing record as gh
# does: the error body on stdout and a nonzero exit.
_FAKE_GH = r"""#!/usr/bin/env bash
path=""
filter=""
while [ $# -gt 0 ]; do
  case "$1" in
    api) ;;
    --jq) filter="$2"; shift ;;
    *) path="$1" ;;
  esac
  shift
done
printf '%s\n' "$path" >> "$GH_FIXTURES/calls"
fixture="$GH_FIXTURES/$(printf '%s' "$path" | tr '/' '_').json"
if [ ! -f "$fixture" ]; then
  printf '%s' '{"message":"Not Found","status":"404"}'
  exit 1
fi
jq -r "$filter" "$fixture"
"""


def _run_security_notice(
    tmp_path: Path, records: dict[str, dict[str, object]]
) -> tuple[str, list[str]]:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for path, record in records.items():
        fixture = fixtures / f"{path.replace('/', '_')}.json"
        fixture.write_text(json.dumps(record), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(_FAKE_GH, encoding="utf-8", newline="\n")
    fake_gh.chmod(0o755)
    out = tmp_path / "security.md"
    script = _security_notice_script().replace("/tmp/security.md", str(out))
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "GITHUB_REPOSITORY": _REPOSITORY,
        "GHSA": _GHSA,
        "GH_FIXTURES": str(fixtures),
    }

    subprocess.run(["bash", "-e", "-c", script], check=True, env=env)

    calls = fixtures / "calls"
    lines = out.read_text(encoding="utf-8").splitlines()
    return lines[2], calls.read_text(encoding="utf-8").splitlines()


@_UBUNTU_BASH_ONLY
def test_a_dependency_advisory_links_to_the_global_advisory_database(
    tmp_path: Path,
) -> None:
    # v0.2.10 and v0.2.16 linked their dependency advisories (mcp, certifi)
    # under this repository's security/advisories, where they 404. The global
    # record scores through cvss_severities, 0.0 marking an unscored version.
    record = {
        **_ADVISORY,
        "cvss_severities": {"cvss_v3": {"score": 5.3}, "cvss_v4": {"score": 0.0}},
    }

    notice, calls = _run_security_notice(tmp_path, {_GLOBAL_ADVISORY: record})

    assert notice == (
        f"{_SUMMARY} (medium severity, CVSS 5.3), dependency advisory "
        f"[{_GHSA}](https://github.com/advisories/{_GHSA}) / CVE-2026-114603."
    )
    assert calls == [_REPOSITORY_ADVISORY, _GLOBAL_ADVISORY]


@_UBUNTU_BASH_ONLY
def test_a_repository_advisory_keeps_its_repository_link(tmp_path: Path) -> None:
    record = {**_ADVISORY, "cvss": {"score": 5.3}}

    notice, calls = _run_security_notice(tmp_path, {_REPOSITORY_ADVISORY: record})

    assert notice == (
        f"{_SUMMARY} (medium severity, CVSS 5.3), advisory "
        f"[{_GHSA}](https://github.com/{_REPOSITORY}/security/advisories/{_GHSA})"
        " / CVE-2026-114603."
    )
    # The repository's own record wins; the global database is not consulted.
    assert calls == [_REPOSITORY_ADVISORY]


@_UBUNTU_BASH_ONLY
def test_an_advisory_found_nowhere_still_points_at_this_repository(
    tmp_path: Path,
) -> None:
    # Most likely this repository's own draft, which the token cannot read and
    # which publishes at that address later (issue #1149).
    notice, calls = _run_security_notice(tmp_path, {})

    assert notice == (
        "This release contains a security fix; see advisory "
        f"[{_GHSA}](https://github.com/{_REPOSITORY}/security/advisories/{_GHSA})."
    )
    assert calls == [_REPOSITORY_ADVISORY, _GLOBAL_ADVISORY]


def _render_step() -> dict[str, str]:
    return next(
        step
        for step in _steps()
        if step.get("name") == "Regenerate README from the updated NEWS.md"
    )


def test_the_readme_render_is_its_own_blocking_step() -> None:
    """The render used to run inside the news step under continue-on-error:
    a generator failure was the condition of the recovery block, the
    checkout succeeded, the step exited 0 and the release shipped stale
    generated docs (issue #1971). The news derivation stays tolerated; the
    render is a step of its own with no tolerance, run only when NEWS.md
    changed."""
    news = next(step for step in _steps() if step.get("name") == "Update NEWS.md")
    assert news.get("continue-on-error") is True
    assert news.get("id") == "update_news"
    render = _render_step()
    assert "continue-on-error" not in render
    assert "steps.update_news.outputs.news_changed == 'true'" in render["if"]
    assert "generate_readme.py" in render["run"]
    assert "generate_readme.py" not in _news_update_script()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the step runs under the Ubuntu runner's bash; on the Windows runner "
    "`bash` resolves to the WSL launcher, which has no distribution installed",
)
def test_a_failed_render_restores_the_files_and_fails_the_step(
    git_repo: Path, tmp_path: Path
) -> None:
    """The render step's own shell, run against a fake `uv` whose generator
    rewrites README.md and then exits 23: NEWS.md and README.md both come
    back as committed and the step exits non-zero, where the old shape
    exited 0 after the restore."""
    import subprocess

    # `git_env`, not `{**os.environ, ...}`: an inherited GIT_DIR or
    # GIT_WORK_TREE would point every git call here, and the step's own
    # `git checkout`, at some other repository (bot review).
    env = git_env(
        GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig-absent"),
        GIT_CONFIG_SYSTEM=str(tmp_path / "gitconfig-absent"),
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    (git_repo / "NEWS.md").write_text("old news\n")
    (git_repo / "README.md").write_text("old readme\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "base"], cwd=git_repo, check=True, env=env
    )
    (git_repo / "NEWS.md").write_text("new news\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    # The generator gets as far as rewriting README.md before it fails: the
    # partially-written shape the restore exists for.
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = sync ]; then exit 0; fi\n'
        'printf "half-rendered readme\\n" > README.md\n'
        "exit 23\n"
    )
    fake_uv.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-c", _render_step()["run"]],
        cwd=git_repo,
        env={
            **env,
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        },
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert (git_repo / "NEWS.md").read_text() == "old news\n"
    assert (git_repo / "README.md").read_text() == "old readme\n"
    assert "FAILED" in (tmp_path / "summary.md").read_text()


# The breaking-change notes a release adds follow the security notice
# (issue #3431).
def _release_step(name: str) -> dict[str, str]:
    return next(step for step in _steps() if step.get("name") == name)


_NOTICE = "Prepend breaking-change notice"


def test_every_release_runs_the_breaking_change_step() -> None:
    assert _release_step(_NOTICE)["if"] == "steps.decide.outputs.release == 'true'"


def test_the_section_sits_between_the_security_notice_and_the_highlights() -> None:
    script = _release_step("Create release")["run"]
    order = [
        script.index(f"/tmp/{part}.md >>")
        for part in ("security", "breaking", "highlights", "notes")
    ]
    assert order == sorted(order)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=git_env(),
        check=True,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    return _git(repo, "rev-parse", "HEAD")


def _run_notice(
    tmp_path: Path, latest: str, labelled: str, *, released: bool = True
) -> tuple[subprocess.CompletedProcess[str], Path]:
    if sys.platform == "win32":
        pytest.skip("the step runs under the Ubuntu runner's bash")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    script = (_WORKFLOW.parents[2] / "scripts" / "breaking_notes.py").read_text("utf-8")
    _commit(
        upstream,
        {"scripts/breaking_notes.py": script, "changes/breaking/10.md": "Old."},
    )
    if released:
        _git(upstream, "tag", "v0.1.0")
    head = _commit(
        upstream,
        {"changes/breaking/10.md": "Old, edited.", "changes/breaking/11.md": "New."},
    )
    # A shallow clone, as the release job's checkout is: the tag's commit is
    # not in it until the step fetches it.
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", "--depth=1", upstream.as_uri(), str(work))
    out = tmp_path / "breaking.md"
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-e",
            "-c",
            _release_step(_NOTICE)["run"].replace("/tmp/breaking.md", str(out)),
        ],
        cwd=work,
        env={
            **git_env(),
            "LATEST_TAG": latest,
            "BREAKING_PRS": labelled,
            "GITHUB_SHA": head,
        },
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    return result, out


def test_the_release_publishes_only_the_notes_added_since_the_last_release(
    tmp_path: Path,
) -> None:
    result, out = _run_notice(tmp_path, "v0.1.0", "11 12")
    assert result.returncode == 0, result.stderr
    assert out.read_text("utf-8") == (
        f"{bn.HEADING}\n\nNew.\n\nThis release contains a breaking change; see #12.\n\n"
    )


def test_a_malformed_release_tag_fails_the_step(tmp_path: Path) -> None:
    result, out = _run_notice(tmp_path, "v0.1.0 HEAD", "")
    assert result.returncode != 0
    assert "refusing malformed release tag" in result.stdout
    assert not out.exists()


def test_a_first_release_publishes_every_note(tmp_path: Path) -> None:
    result, out = _run_notice(tmp_path, "", "", released=False)
    assert result.returncode == 0, result.stderr
    assert out.read_text("utf-8") == f"{bn.HEADING}\n\nOld, edited.\n\nNew.\n\n"


def test_a_failed_release_lookup_fails_the_step(tmp_path: Path) -> None:
    # "Decide" passes an empty tag when its lookup fails as well as when
    # nothing was released; read as a first release, the body would repeat
    # every note ever written.
    result, out = _run_notice(tmp_path, "", "")
    assert result.returncode != 0
    assert "release tags exist" in result.stdout
    assert not out.exists()
