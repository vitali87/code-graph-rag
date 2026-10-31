"""Nothing ships a commit main's CI has not passed.

scripts/require_green_ci.py is the gate; the wiring tests below keep every
shipping path behind it, so a new upload step cannot skip it unnoticed.
"""

from __future__ import annotations

import re
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import require_green_ci as gate

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
REPO = "vitali87/code-graph-rag"
BUMP = "e790ff55eab17cd39d144db99e66dd463f84bd60"
SOURCE = "0db39cb3d96b0ac8c5038ee9646ad684040eba21"
GATE_SCRIPT = "scripts/require_green_ci.py"
RELEASE_TAG_REF = (
    "${{ inputs.tag && format('refs/tags/{0}', inputs.tag) || github.ref }}"
)
# The actions that put something where users install it from.
SHIPPING_ACTIONS = (
    "pypa/gh-action-pypi-publish",
    "docker/build-push-action",
    "softprops/action-gh-release",
)

# The files of the real v0.3.11 bump, as GitHub's commit API returns them.
BUMP_FILES: list[dict[str, Any]] = [
    {
        "filename": "pyproject.toml",
        "status": "modified",
        "patch": '@@ -1,6 +1,6 @@\n [project]\n name = "code-graph-rag"\n'
        '-version = "0.3.10"\n+version = "0.3.11"\n description = "x"',
    },
    {
        "filename": "server.json",
        "status": "modified",
        "patch": '@@ -8,13 +8,13 @@\n   },\n-  "version": "0.3.10",\n'
        '+  "version": "0.3.11",\n   "packages": [\n'
        '       "identifier": "code-graph-rag",\n-      "version": "0.3.10",\n'
        '+      "version": "0.3.11",\n       "runtimeHint": "uvx",',
    },
    {
        "filename": "uv.lock",
        "status": "modified",
        "patch": "@@ -433,7 +433,7 @@ wheels = [\n \n [[package]]\n"
        ' name = "code-graph-rag"\n-version = "0.3.10"\n+version = "0.3.11"\n'
        ' source = { editable = "." }',
    },
]


def _run(sha: str, **updates: Any) -> dict[str, Any]:
    run: dict[str, Any] = {
        "id": 40145807619,
        "run_attempt": 1,
        "path": ".github/workflows/ci.yml",
        "head_sha": sha,
        "head_branch": "main",
        "event": "push",
        "head_repository": {"full_name": REPO},
        "status": "completed",
        "conclusion": "success",
    }
    run.update(updates)
    return run


def _fetcher(
    commits: dict[str, dict[str, Any]], runs: dict[str, list[dict[str, Any]]]
) -> Callable[[str], Any]:
    def fetch(path: str) -> Any:
        if match := re.fullmatch(rf"/repos/{REPO}/commits/([^/?]+)", path):
            return commits[match.group(1)]
        if match := re.fullmatch(
            rf"/repos/{REPO}/actions/workflows/ci\.yml/runs\?head_sha=([0-9a-f]{{40}})"
            r"&event=push&branch=main&per_page=100",
            path,
        ):
            found = runs.get(match.group(1), [])
            return {"total_count": len(found), "workflow_runs": found}
        raise AssertionError(f"unexpected request {path}")

    return fetch


def _bump_commit(files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "sha": BUMP,
        "parents": [{"sha": SOURCE}],
        "files": BUMP_FILES if files is None else files,
    }


class TestVerdict:
    def test_a_commit_whose_ci_passed_ships(self) -> None:
        fetch = _fetcher({SOURCE: {"sha": SOURCE}}, {SOURCE: [_run(SOURCE)]})
        assert gate.verdict(fetch, REPO, SOURCE)[:2] == (True, SOURCE)

    @pytest.mark.parametrize(
        "conclusion", ["failure", "cancelled", "timed_out", "skipped", None]
    )
    def test_a_commit_whose_ci_did_not_pass_is_refused(
        self, conclusion: str | None
    ) -> None:
        fetch = _fetcher(
            {SOURCE: {"sha": SOURCE}},
            {SOURCE: [_run(SOURCE, conclusion=conclusion)]},
        )
        assert gate.verdict(fetch, REPO, SOURCE)[0] is False

    def test_v0_3_11_on_todays_red_main_is_refused(self) -> None:
        # The bump's own run and its source's both failed: the exact tag a
        # hand-made release could have shipped.
        fetch = _fetcher(
            {"v0.3.11": _bump_commit()},
            {
                BUMP: [_run(BUMP, conclusion="failure")],
                SOURCE: [_run(SOURCE, conclusion="failure")],
            },
        )
        ok, sha, message = gate.verdict(fetch, REPO, "refs/tags/v0.3.11")
        assert (ok, sha) == (False, BUMP)
        assert "failure" in message

    @pytest.mark.parametrize(
        "own_runs",
        [[], [_run(BUMP, status="in_progress", conclusion=None)]],
        ids=["no-run-yet", "running"],
    )
    def test_a_fresh_bump_of_a_green_commit_ships_on_its_parent(
        self, own_runs: list[dict[str, Any]]
    ) -> None:
        # version-bump.yml pushes the tag right after the bump, long before
        # CI on the bump itself can finish.
        fetch = _fetcher(
            {"v0.3.11": _bump_commit()}, {BUMP: own_runs, SOURCE: [_run(SOURCE)]}
        )
        assert gate.verdict(fetch, REPO, "v0.3.11")[:2] == (True, BUMP)

    def test_a_fresh_release_bump_with_news_ships_on_its_parent(self) -> None:
        news = {"filename": "NEWS.md", "status": "modified", "patch": "+- new"}
        fetch = _fetcher(
            {"v0.3.11": _bump_commit([*BUMP_FILES, news])},
            {BUMP: [], SOURCE: [_run(SOURCE)]},
        )
        assert gate.verdict(fetch, REPO, "v0.3.11")[:2] == (True, BUMP)

    def test_a_bump_of_a_red_commit_is_refused(self) -> None:
        fetch = _fetcher(
            {"v0.3.11": _bump_commit()},
            {SOURCE: [_run(SOURCE, conclusion="failure")]},
        )
        assert gate.verdict(fetch, REPO, "v0.3.11")[0] is False

    def test_a_bump_whose_own_ci_failed_is_refused_despite_a_green_parent(
        self,
    ) -> None:
        fetch = _fetcher(
            {"v0.3.11": _bump_commit()},
            {BUMP: [_run(BUMP, conclusion="failure")], SOURCE: [_run(SOURCE)]},
        )
        assert gate.verdict(fetch, REPO, "v0.3.11")[0] is False

    def test_a_commit_that_is_more_than_a_bump_never_ships_on_its_parent(
        self,
    ) -> None:
        code = {"filename": "codebase_rag/main.py", "status": "modified", "patch": "+x"}
        fetch = _fetcher(
            {"v9.9.9": _bump_commit([*BUMP_FILES, code])}, {SOURCE: [_run(SOURCE)]}
        )
        assert gate.verdict(fetch, REPO, "v9.9.9")[0] is False

    def test_a_merge_commit_never_ships_on_a_parent(self) -> None:
        merge = _bump_commit() | {"parents": [{"sha": SOURCE}, {"sha": "1" * 40}]}
        fetch = _fetcher({"v9.9.9": merge}, {SOURCE: [_run(SOURCE)]})
        assert gate.verdict(fetch, REPO, "v9.9.9")[0] is False

    @pytest.mark.parametrize(
        "foreign",
        [
            {"path": ".github/workflows/other.yml"},
            {"head_branch": "feature"},
            {"event": "pull_request"},
            {"event": "workflow_dispatch"},
            {"head_repository": {"full_name": "someone/fork"}},
            {"head_repository": None},
        ],
    )
    def test_a_run_that_is_not_mains_push_ci_is_no_evidence(
        self, foreign: dict[str, Any]
    ) -> None:
        fetch = _fetcher({SOURCE: {"sha": SOURCE}}, {SOURCE: [_run(SOURCE, **foreign)]})
        assert gate.verdict(fetch, REPO, SOURCE)[0] is False

    def test_the_newest_run_decides(self) -> None:
        older = _run(SOURCE, id=1)
        newer = _run(SOURCE, id=2, conclusion="failure")
        fetch = _fetcher({SOURCE: {"sha": SOURCE}}, {SOURCE: [newer, older]})
        assert gate.verdict(fetch, REPO, SOURCE)[0] is False

    def test_an_incomplete_runs_reply_is_not_a_pass(self) -> None:
        def fetch(path: str) -> Any:
            if "/commits/" in path:
                return {"sha": SOURCE}
            return {"total_count": 2, "workflow_runs": [_run(SOURCE)]}

        with pytest.raises(gate.GateError):
            gate.verdict(fetch, REPO, SOURCE)

    def test_a_reply_without_a_commit_sha_is_not_a_pass(self) -> None:
        with pytest.raises(gate.GateError):
            gate.verdict(lambda _path: {"message": "Not Found"}, REPO, "v0.0.0")


class TestIsVersionBump:
    def test_the_real_bump_is_one(self) -> None:
        assert gate.is_version_bump(BUMP_FILES) is True

    def test_a_dependency_version_in_the_lock_is_not(self) -> None:
        dependency = {
            "filename": "uv.lock",
            "status": "modified",
            "patch": '@@ -1,4 +1,4 @@\n name = "certifi"\n'
            '-version = "2026.10.23"\n+version = "2026.10.24"',
        }
        assert gate.is_version_bump([dependency]) is False

    @pytest.mark.parametrize(
        "entry",
        [
            {"filename": "pyproject.toml", "status": "modified"},
            {"filename": "pyproject.toml", "status": "added", "patch": "+x"},
            {"filename": "Makefile", "status": "modified", "patch": "-a\n+b"},
            {
                "filename": "pyproject.toml",
                "status": "modified",
                "patch": ' name = "code-graph-rag"\n-version = "1"\n+version = "2"\n+x = 1',
            },
            {
                "filename": "pyproject.toml",
                "status": "modified",
                "patch": ' name = "code-graph-rag"\n+version = "2"',
            },
            {"filename": "server.json", "status": "modified", "patch": " unchanged"},
        ],
        ids=[
            "no-patch",
            "added-file",
            "other-file",
            "extra-line",
            "addition-only",
            "no-change",
        ],
    )
    def test_anything_else_is_not(self, entry: dict[str, Any]) -> None:
        assert gate.is_version_bump([entry]) is False

    def test_a_release_bump_with_its_news_is_one(self) -> None:
        # version-bump.yml commits NEWS.md and the README re-rendered from it
        # with the version whenever the release has highlights.
        docs = [
            {"filename": name, "status": "modified", "patch": "+- a highlight"}
            for name in ("NEWS.md", "README.md")
        ]
        assert gate.is_version_bump([*BUMP_FILES, *docs]) is True

    def test_an_empty_bump_is_one(self) -> None:
        # A version set by hand still gets its bump commit, with no change.
        assert gate.is_version_bump([]) is True

    def test_an_unreadable_file_list_is_not(self) -> None:
        assert gate.is_version_bump(None) is False

    def test_it_admits_every_file_version_bump_yml_commits(self) -> None:
        steps = _workflow("version-bump.yml")["jobs"]["bump-version"]["steps"]
        commit = next(step for step in steps if step.get("id") == "commit")
        added = re.search(r"^\s*git add (.+)$", commit["run"], re.MULTILINE)
        assert added is not None
        assert set(added.group(1).split()) <= (
            set(gate.VERSION_LINES) | gate.RELEASE_DOCS
        )


class TestMain:
    def test_a_pass_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
        fetch = _fetcher(
            {"v0.3.11": _bump_commit()}, {BUMP: [], SOURCE: [_run(SOURCE)]}
        )
        assert gate.main(["gate", "refs/tags/v0.3.11"], fetch) == 0

    def test_a_refusal_exits_non_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
        fetch = _fetcher(
            {SOURCE: {"sha": SOURCE}}, {SOURCE: [_run(SOURCE, conclusion="failure")]}
        )
        assert gate.main(["gate", SOURCE], fetch) == 1

    def test_an_unreachable_github_is_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)

        def fetch(_path: str) -> Any:
            raise urllib.error.URLError("down")

        assert gate.main(["gate", SOURCE], fetch) == 1

    def test_no_ref_is_a_refusal(self) -> None:
        assert gate.main(["gate"], _fetcher({}, {})) == 1
        assert gate.main(["gate", ""], _fetcher({}, {})) == 1


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _runs_gate(step: dict[str, Any]) -> bool:
    return GATE_SCRIPT in str(step.get("run", ""))


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


class TestEveryShippingPathIsGated:
    def test_every_workflow_that_ships_runs_the_gate(self) -> None:
        # A new upload step in a workflow that never asks is the hole this
        # gate closes; catch it here rather than on a red release.
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            if any(action in text for action in SHIPPING_ACTIONS):
                assert GATE_SCRIPT in text, f"{path.name} ships without the gate"

    def test_publish_ships_nothing_until_the_gate_passes(self) -> None:
        jobs = _workflow("publish.yml")["jobs"]
        assert any(_runs_gate(step) for step in jobs["ci-gate"]["steps"])
        for name, job in jobs.items():
            if name == "ci-gate":
                continue
            assert "ci-gate" in _needs(job), f"{name} does not wait for the gate"
            # The release tag, never a job output: CodeQL reads a ref taken
            # from another job's output as untrusted code checked out in a
            # privileged run (cache poisoning).
            refs = [
                step["with"]["ref"]
                for step in job.get("steps", [])
                if "actions/checkout" in str(step.get("uses", ""))
            ]
            if "uses" in job:
                refs.append(job["with"]["ref"])
            assert all(RELEASE_TAG_REF == ref for ref in refs), name
        assert "needs.ci-gate.result == 'success'" in jobs["publish"]["if"]

    def test_docker_checks_the_commit_it_builds_before_pushing(self) -> None:
        steps = _workflow("docker-publish.yml")["jobs"]["build-and-push"]["steps"]
        gate_at = next(i for i, step in enumerate(steps) if _runs_gate(step))
        push_at = next(
            i
            for i, step in enumerate(steps)
            if "docker/build-push-action" in str(step.get("uses", ""))
        )
        assert gate_at < push_at
        assert "if" not in steps[gate_at]
        assert '"$GITHUB_SHA"' in steps[gate_at]["run"]

    def test_binaries_reach_a_release_only_through_the_gate(self) -> None:
        jobs = _workflow("build-binaries.yml")["jobs"]
        assert any(_runs_gate(step) for step in jobs["ci-gate"]["steps"])
        assert "ci-gate" in _needs(jobs["build"])
        assert "needs.ci-gate.result == 'success'" in jobs["build"]["if"]
        assert "build" in _needs(jobs["sign-release"])

    def test_make_release_asks_before_it_uploads(self) -> None:
        script = (ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")
        assert 0 <= script.find(GATE_SCRIPT) < script.index("twine upload")
