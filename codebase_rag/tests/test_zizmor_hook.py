"""The zizmor pre-commit hook audits offline.

zizmor runs its online audits whenever GH_TOKEN, GITHUB_TOKEN or
ZIZMOR_GITHUB_TOKEN is set. Without `--offline` the hook's verdict, and the
GitHub API calls it makes on every commit touching a workflow, would depend on
the committer's environment.
"""

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[2] / ".pre-commit-config.yaml"


def _zizmor_hooks() -> list[dict]:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return [
        hook
        for repo in config["repos"]
        if repo["repo"].endswith("/zizmor-pre-commit")
        for hook in repo["hooks"]
        if hook["id"] == "zizmor"
    ]


def test_the_zizmor_hook_runs_offline() -> None:
    (hook,) = _zizmor_hooks()
    args = hook.get("args", [])
    assert "--offline" in args
    # Args set here replace the upstream hook's, which pass --no-progress
    # (zizmorcore/zizmor#582), so that one has to be repeated.
    assert "--no-progress" in args
