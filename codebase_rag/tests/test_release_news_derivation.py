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

import sys
from pathlib import Path

import pytest
import yaml

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
    script = (
        _security_notice_script()
        .replace("${{ steps.decide.outputs.ghsa }}", "GHSA-aaaa-bbbb-cccc")
        .replace("/tmp/security.md", str(out))
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "GITHUB_REPOSITORY": "vitali87/code-graph-rag",
    }

    subprocess.run(["bash", "-e", "-c", script], check=True, env=env)

    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "## 🔒 Security"
    assert lines[2].startswith("This release contains a security fix; see advisory")
    assert "GHSA-aaaa-bbbb-cccc" in lines[2]
    assert not any("403" in line for line in lines)


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


def test_a_failed_render_restores_the_files_and_fails_the_step(tmp_path: Path) -> None:
    """The render step's own shell, run against a fake `uv` whose generator
    exits 23: NEWS.md and README.md come back as committed and the step
    exits non-zero, where the old shape exited 0 after the restore."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "NEWS.md").write_text("old news\n")
    (repo / "README.md").write_text("old readme\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    (repo / "NEWS.md").write_text("new news\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text('#!/bin/sh\nif [ "$1" = sync ]; then exit 0; fi\nexit 23\n')
    fake_uv.chmod(0o755)
    env = {
        **dict(__import__("os").environ),
        "PATH": f"{fake_bin}:{__import__('os').environ.get('PATH', '')}",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }
    result = subprocess.run(
        ["bash", "-e", "-c", _render_step()["run"]],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert (repo / "NEWS.md").read_text() == "old news\n"
    assert "FAILED" in (tmp_path / "summary.md").read_text()
