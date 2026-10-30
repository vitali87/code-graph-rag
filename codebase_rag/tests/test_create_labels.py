"""`scripts/create_labels.py` syncs `.github/labels.yml` to GitHub: existing
labels are patched, new ones created, and any failure fails the run. The
GitHub API is replaced by a recording fake, so nothing leaves the machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

pytest.importorskip("requests")

from scripts import create_labels as labels


@dataclass
class _Response:
    status_code: int = 200
    payload: list[dict[str, str]] = field(default_factory=list)
    text: str = ""

    def json(self) -> list[dict[str, str]]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _GitHub:
    """Records every request and answers from per-label scripts."""

    def __init__(self, existing: list[str], answers: dict[str, Any]) -> None:
        self.existing = existing
        self.answers = answers
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("GET", url, kwargs))
        return self.answers.get("GET") or _Response(
            payload=[{"name": n, "color": "000000"} for n in self.existing]
        )

    def _write(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs))
        answer = self.answers.get(kwargs["json"]["name"], _Response(201))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def patch(self, url: str, **kwargs: Any) -> _Response:
        return self._write("PATCH", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> _Response:
        return self._write("POST", url, **kwargs)


@pytest.fixture
def manifest() -> list[labels.Label]:
    # main syncs the shipped manifest, so the expectations are built from it.
    return labels.load_labels_config()


def _github(monkeypatch: pytest.MonkeyPatch, existing: list[str], **answers: Any):
    fake = _GitHub(existing, answers)
    monkeypatch.setattr(
        labels,
        "requests",
        SimpleNamespace(get=fake.get, patch=fake.patch, post=fake.post),
    )
    return fake


def _cli() -> typer.Typer:
    # What `typer.run(main)` builds when the script runs.
    app = typer.Typer()
    app.command()(labels.main)
    return app


def test_the_shipped_manifest_loads_as_labels() -> None:
    loaded = labels.load_labels_config()
    names = [label["name"] for label in loaded]
    assert "bug" in names
    assert len(names) == len(set(names))
    assert all(len(label["color"]) == 6 for label in loaded)


def test_an_explicit_token_wins_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert labels.get_github_token("given", "o/r") == ("given", "o/r")
    assert labels.get_github_token(None, "o/r") == ("from-env", "o/r")
    monkeypatch.delenv("GITHUB_TOKEN")
    with pytest.raises(typer.Exit) as exit_info:
        labels.get_github_token(None)
    assert exit_info.value.exit_code == 1


def test_existing_labels_are_patched_and_new_ones_created(
    monkeypatch: pytest.MonkeyPatch,
    manifest: list[labels.Label],
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = manifest[0]["name"]
    github = _github(monkeypatch, [first], **{first: _Response(200)})

    labels.main(token="t0ken", repo="o/r")

    (get, *writes) = github.calls
    assert get[:2] == ("GET", "https://api.github.com/repos/o/r/labels")
    assert get[2]["headers"]["Authorization"] == "token t0ken"
    assert get[2]["timeout"] == labels.REQUEST_TIMEOUT_SECONDS
    assert writes[0][:2] == (
        "PATCH",
        f"https://api.github.com/repos/o/r/labels/{first}",
    )
    assert [method for method, _, _ in writes[1:]] == ["POST"] * (len(manifest) - 1)
    assert [w[2]["json"]["name"] for w in writes] == [m["name"] for m in manifest]
    assert all(w[2]["json"]["color"] for w in writes)
    assert f"Created/Updated: {len(manifest)}, Failed: 0" in capsys.readouterr().out


def test_failed_labels_fail_the_run_but_not_the_rest(
    monkeypatch: pytest.MonkeyPatch,
    manifest: list[labels.Label],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rejected, broken, stale = (label["name"] for label in manifest[:3])
    github = _github(
        monkeypatch,
        [stale],
        **{
            rejected: _Response(422, text="invalid"),
            broken: ConnectionError("reset"),
            stale: _Response(500, text="oops"),
        },
    )

    with pytest.raises(typer.Exit) as exit_info:
        labels.main(token="t", repo="o/r")

    assert exit_info.value.exit_code == 1
    assert len(github.calls) == 1 + len(manifest)
    out = capsys.readouterr().out
    # Named by the verb, not the past tense the success line uses.
    assert f"Failed to create label: {rejected}\n   Response: 422 - invalid" in out
    assert f"Failed to update label: {stale}\n   Response: 500 - oops" in out
    assert f"Error with label {broken}: reset" in out
    assert f"Created/Updated: {len(manifest) - 3}, Failed: 3" in out


def test_a_failed_listing_stops_before_writing(
    monkeypatch: pytest.MonkeyPatch, manifest: list[labels.Label]
) -> None:
    github = _github(monkeypatch, [], GET=_Response(401))
    with pytest.raises(typer.Exit) as exit_info:
        labels.main(token="t", repo="o/r")
    assert exit_info.value.exit_code == 1
    assert [method for method, _, _ in github.calls] == ["GET"]


def test_the_command_reads_its_token_and_repo_options(
    monkeypatch: pytest.MonkeyPatch, manifest: list[labels.Label]
) -> None:
    github = _github(monkeypatch, [])
    result = CliRunner().invoke(_cli(), ["--token", "cli-token", "--repo", "me/mine"])
    assert result.exit_code == 0, result.output
    assert github.calls[0][1] == "https://api.github.com/repos/me/mine/labels"
    assert github.calls[0][2]["headers"]["Authorization"] == "token cli-token"
