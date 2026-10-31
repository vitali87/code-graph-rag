"""Guards for the poor-quality pull request sweep.

The sweep warns each pull request labelled ``poor-quality`` and closes it
once the warning is a week old with no update since. It runs on a schedule
rather than on the label event: commenting on a fork's pull request from a
label-triggered run needs ``pull_request_target``, which runs with a write
token in reach of the pull request.

Each test runs the step's script under node, the way ``actions/github-script``
wraps it in an async function, against a stand-in GitHub client that records
the comments posted and the pull requests closed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from codebase_rag import constants as cs

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "poor-quality-management.yml"
JOB_ID = "close-stale-poor-quality"

LABEL = "poor-quality"
BOT = "github-actions[bot]"
WARNING = "⚠️ **This PR has been marked as poor-quality.**"
CLOSING = "🚫 **This PR has been automatically closed.**"

DRIVER = r"""
const fs = require('fs');
const [scriptPath, fixturePath] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const calls = [];
// A list call answers with one page, as GitHub's API does; only paginate
// walks every page.
const page = (items, p) => {
  const size = p.per_page || 30;
  const start = ((p.page || 1) - 1) * size;
  return { data: items.slice(start, start + size), total: items.length };
};
const github = {
  rest: {
    pulls: {
      list: async (p) => page(fixture.prs, p),
      update: async (p) => { calls.push(['update', p.pull_number, p.state]); return {}; },
    },
    issues: {
      listEvents: async (p) => page(fixture.events[p.issue_number] || [], p),
      listComments: async (p) => page(fixture.comments[p.issue_number] || [], p),
      createComment: async (p) => { calls.push(['comment', p.issue_number, p.body]); return {}; },
    },
  },
  paginate: async (method, params) => {
    const items = [];
    for (let n = 1; ; n++) {
      const { data, total } = await method({ ...params, page: n });
      items.push(...data);
      if (items.length >= total || data.length === 0) return items;
    }
  },
};
const context = { repo: { owner: 'owner', repo: 'repo' } };
const core = { info() {}, warning() {}, setFailed(message) { throw new Error(message); } };
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
console.log = () => {};
(async () => {
  await new AsyncFunction('github', 'context', 'core', fs.readFileSync(scriptPath, 'utf8'))(
    github, context, core);
  process.stdout.write(JSON.stringify(calls));
})().catch((error) => { process.stderr.write(String(error.stack)); process.exit(1); });
"""


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _ago(days: float) -> str:
    moment = datetime.now(UTC) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Repo:
    def __init__(self) -> None:
        self.prs: list[dict] = []
        self.events: dict[int, list[dict]] = {}
        self.comments: dict[int, list[dict]] = {}

    def pr(
        self,
        number: int,
        *,
        labelled: float | None = None,
        warned: float | None = None,
        updated: float | None = None,
        warned_by: str = BOT,
    ) -> None:
        # `updated` defaults to the latest thing this fixture did to the pull
        # request, so a warning that bumps `updated_at` is modelled too.
        latest = min(d for d in (labelled, warned, 30.0) if d is not None)
        self.prs.append(
            {
                "number": number,
                "labels": [{"name": LABEL}] if labelled is not None else [],
                "updated_at": _ago(latest if updated is None else updated),
            }
        )
        if labelled is not None:
            self.events[number] = [
                {
                    "event": "labeled",
                    "label": {"name": LABEL},
                    "created_at": _ago(labelled),
                }
            ]
        if warned is not None:
            self.comments[number] = [
                {
                    "user": {"login": warned_by},
                    "body": f"{WARNING}\n\nmore",
                    "created_at": _ago(warned),
                }
            ]

    def sweep(self, tmp_path: Path) -> list[list]:
        node = shutil.which("node")
        if node is None:
            pytest.skip("the sweep's script requires node")
        (step,) = _workflow()["jobs"][JOB_ID]["steps"]
        script = tmp_path / "script.js"
        script.write_text(step["with"]["script"], encoding="utf-8")
        driver = tmp_path / "driver.js"
        driver.write_text(DRIVER, encoding="utf-8")
        fixture = tmp_path / "fixture.json"
        fixture.write_text(
            json.dumps(
                {"prs": self.prs, "events": self.events, "comments": self.comments}
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [node, str(driver), str(script), str(fixture)],
            capture_output=True,
            encoding=cs.ENCODING_UTF8,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


def _warnings(calls: list[list]) -> list[int]:
    return [c[1] for c in calls if c[0] == "comment" and c[2].startswith(WARNING)]


def _closed(calls: list[list]) -> list[int]:
    closing = [c[1] for c in calls if c[0] == "comment" and c[2].startswith(CLOSING)]
    updated = [c[1] for c in calls if c[0] == "update" and c[2] == "closed"]
    assert closing == updated, "every close is announced, and only a close is"
    return updated


def test_no_trigger_runs_with_a_write_token_in_reach_of_a_pull_request() -> None:
    workflow = _workflow()
    # PyYAML reads the bare key `on` as the boolean true.
    triggers = workflow.get("on", workflow.get(True))

    assert set(triggers) == {"schedule", "workflow_dispatch"}


def test_a_newly_labelled_pr_is_warned_and_left_open(tmp_path: Path) -> None:
    repo = _Repo()
    repo.pr(1, labelled=0.5)

    calls = repo.sweep(tmp_path)

    assert _warnings(calls) == [1]
    assert _closed(calls) == []


def test_a_pr_warned_over_a_week_ago_and_untouched_is_closed(tmp_path: Path) -> None:
    repo = _Repo()
    repo.pr(1, labelled=8.5, warned=8)

    calls = repo.sweep(tmp_path)

    assert _closed(calls) == [1]
    assert _warnings(calls) == []


def test_a_pr_warned_within_the_week_is_left_open(tmp_path: Path) -> None:
    repo = _Repo()
    repo.pr(1, labelled=3.5, warned=3)

    calls = repo.sweep(tmp_path)

    assert calls == []


def test_a_pr_updated_after_its_warning_is_left_open(tmp_path: Path) -> None:
    repo = _Repo()
    repo.pr(1, labelled=8.5, warned=8, updated=2)

    calls = repo.sweep(tmp_path)

    assert calls == []


def test_a_warning_from_before_a_relabelling_does_not_count(tmp_path: Path) -> None:
    repo = _Repo()
    repo.pr(1, labelled=0.5, warned=20)

    calls = repo.sweep(tmp_path)

    assert _warnings(calls) == [1]
    assert _closed(calls) == []


def test_someone_else_quoting_the_warning_does_not_count(tmp_path: Path) -> None:
    repo = _Repo()
    repo.pr(1, labelled=8.5, warned=8, warned_by="contributor")

    calls = repo.sweep(tmp_path)

    assert _warnings(calls) == [1]
    assert _closed(calls) == [], "a pull request is never closed unwarned"


def test_a_labelled_pr_beyond_the_first_page_is_warned(tmp_path: Path) -> None:
    repo = _Repo()
    for number in range(1, 101):
        repo.pr(number)
    repo.pr(101, labelled=0.5)

    calls = repo.sweep(tmp_path)

    assert _warnings(calls) == [101]


def test_a_label_event_beyond_the_first_page_counts(tmp_path: Path) -> None:
    repo = _Repo()
    repo.pr(1, labelled=0.5)
    repo.events[1] = [
        {"event": "commented", "created_at": _ago(2)} for _ in range(100)
    ] + repo.events[1]

    calls = repo.sweep(tmp_path)

    assert _warnings(calls) == [1]


def test_an_unlabelled_pr_is_ignored(tmp_path: Path) -> None:
    repo = _Repo()
    repo.pr(1, warned=8)

    assert repo.sweep(tmp_path) == []
