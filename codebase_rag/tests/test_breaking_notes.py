# A breaking release must say what breaks and how to migrate. v0.3.2 shipped
# without that section because nothing in the release job wrote one; the
# notes now live in changes/breaking/<pull request number>.md, and a pull
# request check keeps them in step with the label (issue #3431).

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import git_env
from scripts import breaking_notes as bn

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO / ".github" / "workflows"


def _root(tmp_path: Path, fragments: dict[str, str]) -> Path:
    directory = tmp_path / "changes" / "breaking"
    directory.mkdir(parents=True)
    for name, text in fragments.items():
        (directory / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_a_labelled_pull_request_with_notes_passes(tmp_path: Path) -> None:
    root = _root(tmp_path, {"7.md": "Qdrant is no longer the default."})
    added = ["changes/breaking/7.md"]
    assert bn.check_pull_request(7, "feat(x)!: y", [bn.LABEL], root, added) == []


def test_a_pull_request_that_breaks_nothing_passes(tmp_path: Path) -> None:
    # A "!" outside the type prefix is not a breaking marker.
    root = _root(tmp_path, {"3.md": "notes of an earlier pull request"})
    assert bn.check_pull_request(7, "feat: support x!", ["bug"], root) == []


@pytest.mark.parametrize("fragments", [{}, {"7.md": " \n"}])
def test_a_labelled_pull_request_without_notes_fails(
    tmp_path: Path, fragments: dict[str, str]
) -> None:
    errors = bn.check_pull_request(
        7, "feat!: y", [bn.LABEL], _root(tmp_path, fragments)
    )
    assert len(errors) == 1
    assert "changes/breaking/7.md" in errors[0]


def test_a_breaking_title_without_the_label_fails(tmp_path: Path) -> None:
    errors = bn.check_pull_request(7, "feat(cli)!: y", [], _root(tmp_path, {}))
    assert len(errors) == 1
    assert bn.LABEL in errors[0]


# This repository's scopes hold commas, slashes and dots, as in
# "refactor(rename,imports)" and "fix(mcp/server.py)".
@pytest.mark.parametrize(
    "title",
    ["feat(providers/base)!: y", "refactor(rename,imports)!: y", "fix(a.py)!: y"],
)
def test_a_breaking_title_with_any_scope_needs_the_label(
    tmp_path: Path, title: str
) -> None:
    errors = bn.check_pull_request(7, title, [], _root(tmp_path, {}))
    assert len(errors) == 1
    assert bn.LABEL in errors[0]


def test_a_labelled_pull_request_needs_the_breaking_title(tmp_path: Path) -> None:
    root = _root(tmp_path, {"7.md": "x"})
    errors = bn.check_pull_request(7, "feat(x): y", [bn.LABEL], root)
    assert len(errors) == 1
    assert "title has no" in errors[0]


def test_notes_added_under_another_number_fail(tmp_path: Path) -> None:
    # The release publishes every fragment added since the last release, so
    # notes filed under another number would skip the label check.
    root = _root(tmp_path, {"3.md": "x"})
    added = ["changes/breaking/3.md"]
    errors = bn.check_pull_request(7, "fix: y", [], root, added)
    assert len(errors) == 1
    assert "changes/breaking/3.md" in errors[0]


def test_notes_without_the_label_fail(tmp_path: Path) -> None:
    errors = bn.check_pull_request(7, "feat: y", [], _root(tmp_path, {"7.md": "x"}))
    assert len(errors) == 1
    assert "changes/breaking/7.md" in errors[0]


@pytest.mark.parametrize("name", ["notes.md", "7.txt", "07.md"])
def test_a_fragment_no_release_would_publish_fails(tmp_path: Path, name: str) -> None:
    errors = bn.check_pull_request(7, "fix: y", [], _root(tmp_path, {name: "x"}))
    assert len(errors) == 1
    assert name in errors[0]


def test_notes_render_in_pull_request_order(tmp_path: Path) -> None:
    root = _root(tmp_path, {"12.md": "Second.\n", "9.md": "First.\n"})
    section = bn.render_section(
        ["changes/breaking/12.md", "changes/breaking/9.md"], [9, 12], root
    )
    assert section == f"{bn.HEADING}\n\nFirst.\n\nSecond.\n\n"


@pytest.mark.parametrize("fragments", [{}, {"12.md": "\n"}])
def test_a_labelled_pull_request_without_notes_is_still_named(
    tmp_path: Path, fragments: dict[str, str]
) -> None:
    added = [f"changes/breaking/{name}" for name in fragments]
    section = bn.render_section(added, [12], _root(tmp_path, fragments))
    assert section == (
        f"{bn.HEADING}\n\nThis release contains a breaking change; see #12.\n\n"
    )


def test_a_release_with_nothing_breaking_has_no_section(tmp_path: Path) -> None:
    assert bn.render_section([], [], _root(tmp_path, {})) == ""


@pytest.mark.parametrize(
    "path", ["changes/breaking/notes.md", "changes/breaking/old/3.md", "docs/3.md"]
)
def test_a_path_that_is_not_a_fragment_is_refused(tmp_path: Path, path: str) -> None:
    root = _root(tmp_path, {})
    with pytest.raises(ValueError, match="is not a"):
        bn.render_section([path], [], root)


def test_the_check_reports_its_errors_and_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["check", "--number", "999999", "--title", "feat!: y", "--labels-json"]
    assert bn.main([*argv, "[]"]) == 1
    assert capsys.readouterr().out.startswith("::error::")
    assert (
        bn.main(
            ["check", "--number", "999999", "--title", "fix: y", "--labels-json", "[]"]
        )
        == 0
    )


def test_the_check_reads_the_notes_the_pull_request_adds() -> None:
    argv = ["check", "--number", "999999", "--title", "fix: y", "--labels-json"]
    assert bn.main([*argv, "[]", "--added", "changes/breaking/3.md"]) == 1
    assert bn.main([*argv, "[]", "--added", ""]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["render", "--labelled", "12 x"],
        ["render", "--labelled", "-3"],
        ["check", "--number", "1", "--title", "t", "--labels-json", '{"a": 1}'],
    ],
)
def test_malformed_input_is_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        bn.main(argv)
    assert raised.value.code != 0


def test_the_check_reruns_when_the_label_or_title_changes() -> None:
    workflow = yaml.safe_load(
        (_WORKFLOWS / "breaking-change-notes.yml").read_text("utf-8")
    )
    # PyYAML reads the bare key `on` as True.
    types = set(workflow[True]["pull_request"]["types"])
    assert {"labeled", "unlabeled", "edited", "synchronize"} <= types
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", "")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=git_env(),
        check=True,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
    )


def _commit(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")


def _run_workflow_check(
    tmp_path: Path, fragment: str, title: str, labels: list[str]
) -> subprocess.CompletedProcess[str]:
    if sys.platform == "win32":
        pytest.skip("the step runs under the Ubuntu runner's bash")
    steps = yaml.safe_load(
        (_WORKFLOWS / "breaking-change-notes.yml").read_text("utf-8")
    )["jobs"]["breaking-change-notes"]["steps"]
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    script = (_REPO / "scripts" / "breaking_notes.py").read_text("utf-8")
    _commit(
        upstream,
        {"scripts/breaking_notes.py": script, "changes/breaking/3.md": "Old."},
    )
    # Correcting an earlier pull request's notes is allowed; only added
    # notes must carry this pull request's number.
    _commit(
        upstream,
        {f"changes/breaking/{fragment}": "New.", "changes/breaking/3.md": "Fixed."},
    )
    # Cloned as deep as the job checks out: the pull request's merge commit
    # and the base it is compared with.
    depth = steps[0]["with"]["fetch-depth"]
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", f"--depth={depth}", upstream.as_uri(), str(work))
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-c", steps[1]["run"]],
        cwd=work,
        env={
            **git_env(),
            "PR_NUMBER": "7",
            "PR_TITLE": title,
            "PR_LABELS": json.dumps(labels),
        },
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )


def test_the_workflow_passes_the_notes_the_pull_request_adds(tmp_path: Path) -> None:
    result = _run_workflow_check(tmp_path, "7.md", "feat!: y", [bn.LABEL])
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_workflow_rejects_notes_added_under_another_number(
    tmp_path: Path,
) -> None:
    result = _run_workflow_check(tmp_path, "5.md", "fix: y", [])
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.startswith("::error::changes/breaking/5.md")
