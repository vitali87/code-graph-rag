# A breaking release must say what breaks and how to migrate. v0.3.2 shipped
# without that section because nothing in the release job wrote one; the
# notes now live in changes/breaking/<pull request number>.md, and a pull
# request check keeps them in step with the label (issue #3431).

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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
    assert bn.check_pull_request(7, "feat(x)!: y", [bn.LABEL], root) == []


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
    with pytest.raises(ValueError, match="is not a"):
        bn.render_section([path], [], _root(tmp_path, {}))


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
