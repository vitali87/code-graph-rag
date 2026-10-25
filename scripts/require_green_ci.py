#!/usr/bin/env python3
"""Refuse to ship a commit main's CI has not passed.

Every path that ships runs this against the exact commit it is about to
build: publish.yml (PyPI), docker-publish.yml (GHCR), the release uploads in
build-binaries.yml, and `make release` (scripts/release.sh). Each of those
starts from a tag push, a published release or a manual dispatch, and
version-bump.yml's own gate covers only the tag it pushes itself, so a
release created by hand on a red tag (v0.3.11 tags a red main) shipped with
no check at all.

A commit passes when the CI workflow's run for its push to main concluded
`success`. A version bump whose own run has not concluded passes on its
parent's run instead: version-bump.yml tags and ships the bump before CI on
it can finish, and the bump changes nothing but code-graph-rag's version
strings and the release's news, or nothing at all. Anything else fails,
including every answer GitHub could not give: an unreadable or incomplete
reply is not a pass.

Usage:  python3 scripts/require_green_ci.py <tag|sha>
Reads GH_TOKEN (or GITHUB_TOKEN) and GITHUB_REPOSITORY. Exit 0 when the
commit may ship, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

API = "https://api.github.com"
DEFAULT_REPO = "vitali87/code-graph-rag"
CI_WORKFLOW_FILE = "ci.yml"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
MAIN = "main"
PUSH = "push"
TIMEOUT_S = 30
ATTEMPTS = 3
RETRY_WAIT_S = 10
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
REF_PREFIXES = ("refs/tags/", "refs/heads/")

SUCCESS = "success"
PENDING = "pending"
MISSING = "missing"

# The one line a version bump changes in each file it touches. In
# pyproject.toml and uv.lock that line belongs to the package named on the
# line above it, and only code-graph-rag's own may change: a dependency's
# version line in uv.lock is a dependency change, not a bump.
VERSION_LINES = {
    "pyproject.toml": re.compile(r'version = "[^"]+"'),
    "uv.lock": re.compile(r'version = "[^"]+"'),
    "server.json": re.compile(r'\s*"version": "[^"]+",?'),
}
NAMED_FILES = frozenset({"pyproject.toml", "uv.lock"})
OWN_NAME_CONTEXT = ' name = "code-graph-rag"'
# What a release's bump commits besides the version: its news, and the
# README re-rendered from it. Neither is built into anything that ships (the
# package's readme is PYPI_README.md).
RELEASE_DOCS = frozenset({"NEWS.md", "README.md"})

USAGE = "usage: require_green_ci.py <tag|sha>"
PASSED = "CI passed at {sha}; it may ship."
PASSED_ON_PARENT = (
    "{sha} only bumps the version of {parent}, where CI passed; it may ship."
)
REFUSED = "CI at {sha} concluded {state}; refusing to ship it. Ship a newer commit whose CI passed."
REFUSED_PARENT = "{sha} only bumps the version of {parent}, where CI is {state}; refusing to ship it."
NOT_YET = (
    "CI at {sha} is {state}; refusing to ship it until it passes. Re-run this "
    "once CI at {sha} has passed."
)
UNPROVEN = "Could not prove CI passed at {ref}: {error}; refusing to ship it."

Fetch = Callable[[str], Any]


class GateError(Exception):
    """GitHub's reply cannot prove anything either way."""


def ci_state(fetch: Fetch, repo: str, sha: str) -> str:
    """`success`, `pending`, `missing`, or the conclusion that is not success."""
    query = urllib.parse.urlencode(
        {"head_sha": sha, "event": PUSH, "branch": MAIN, "per_page": 100}
    )
    reply = fetch(f"/repos/{repo}/actions/workflows/{CI_WORKFLOW_FILE}/runs?{query}")
    runs = reply.get("workflow_runs") if isinstance(reply, dict) else None
    total = reply.get("total_count") if isinstance(reply, dict) else None
    if not isinstance(runs, list) or not isinstance(total, int):
        raise GateError("unreadable CI workflow runs reply")
    if total != len(runs):
        raise GateError(f"incomplete CI workflow runs reply ({len(runs)} of {total})")
    ours = [run for run in runs if _is_main_push_run(run, repo, sha)]
    if not ours:
        return MISSING
    latest = max(ours, key=lambda run: (run["id"], run["run_attempt"]))
    if latest.get("status") != "completed":
        return PENDING
    return latest.get("conclusion") or "unknown"


def _is_main_push_run(run: object, repo: str, sha: str) -> bool:
    # The query's filters again, on the reply: a run of another workflow,
    # branch, event or fork at this commit is no evidence about main.
    if not isinstance(run, dict):
        return False
    head_repo = run.get("head_repository")
    return (
        isinstance(run.get("id"), int)
        and isinstance(run.get("run_attempt"), int)
        and run.get("path") == CI_WORKFLOW_PATH
        and run.get("head_sha") == sha
        and run.get("head_branch") == MAIN
        and run.get("event") == PUSH
        and isinstance(head_repo, dict)
        and head_repo.get("full_name") == repo
    )


def is_version_bump(files: object) -> bool:
    """Whether a commit's changed files hold nothing but a version bump.

    That is what version-bump.yml commits: code-graph-rag's version lines,
    the release's news, or nothing at all (a version set by hand still gets
    its empty bump commit).
    """
    if not isinstance(files, list):
        return False
    for entry in files:
        if not isinstance(entry, dict):
            return False
        name = entry.get("filename")
        if name in RELEASE_DOCS and entry.get("status") == "modified":
            continue
        pattern = VERSION_LINES.get(name) if isinstance(name, str) else None
        patch = entry.get("patch")
        if pattern is None or entry.get("status") != "modified":
            return False
        # GitHub leaves `patch` out of a diff too large to show: unproven.
        if not isinstance(patch, str):
            return False
        if not _only_version_pairs(patch, pattern, name in NAMED_FILES):
            return False
    return True


def _only_version_pairs(patch: str, pattern: re.Pattern[str], named: bool) -> bool:
    # Every change is one removed version line followed by its replacement,
    # and in a named file it sits right under code-graph-rag's own name.
    lines = patch.split("\n")
    pairs = 0
    i = 0
    while i < len(lines):
        if not lines[i].startswith(("-", "+")):
            i += 1
            continue
        old = lines[i]
        new = lines[i + 1] if i + 1 < len(lines) else ""
        if not (
            old.startswith("-")
            and new.startswith("+")
            and pattern.fullmatch(old[1:])
            and pattern.fullmatch(new[1:])
        ):
            return False
        if named and (i == 0 or lines[i - 1] != OWN_NAME_CONTEXT):
            return False
        pairs += 1
        i += 2
    return pairs > 0


def verdict(fetch: Fetch, repo: str, ref: str) -> tuple[bool, str, str]:
    """Whether the commit `ref` names may ship, that commit, and why."""
    for prefix in REF_PREFIXES:
        ref = ref.removeprefix(prefix)
    commit = fetch(f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}")
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
        raise GateError(f"no commit SHA for {ref!r}")
    state = ci_state(fetch, repo, sha)
    if state == SUCCESS:
        return True, sha, PASSED.format(sha=sha)
    if state not in (PENDING, MISSING):
        return False, sha, REFUSED.format(sha=sha, state=state)
    parents = commit.get("parents")
    if (
        isinstance(parents, list)
        and len(parents) == 1
        and isinstance(parents[0], dict)
        and is_version_bump(commit.get("files"))
    ):
        parent = parents[0].get("sha")
        if not isinstance(parent, str) or not SHA_PATTERN.fullmatch(parent):
            raise GateError(f"no parent SHA for {sha}")
        parent_state = ci_state(fetch, repo, parent)
        if parent_state == SUCCESS:
            return True, sha, PASSED_ON_PARENT.format(sha=sha, parent=parent)
        return (
            False,
            sha,
            REFUSED_PARENT.format(sha=sha, parent=parent, state=parent_state),
        )
    return False, sha, NOT_YET.format(sha=sha, state=state)


def github_fetch(token: str | None) -> Fetch:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def fetch(path: str) -> Any:
        request = urllib.request.Request(API + path, headers=headers)  # noqa: S310 - fixed https://api.github.com URL
        for attempt in range(1, ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - fixed https://api.github.com URL
                    return json.load(response)
            except urllib.error.HTTPError as error:
                # A 4xx answers the question (no such ref, no access); only
                # a server error is worth asking again.
                if error.code < 500 or attempt == ATTEMPTS:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == ATTEMPTS:
                    raise
            time.sleep(RETRY_WAIT_S)
        raise GateError("no reply")

    return fetch


def _report(message: str, *, error: bool) -> None:
    if error and os.environ.get("GITHUB_ACTIONS") == "true":
        message = f"::error::{message}"
    print(message, file=sys.stderr if error else sys.stdout)  # noqa: T201


def main(argv: list[str], fetch: Fetch | None = None) -> int:
    if len(argv) != 2 or not argv[1]:
        _report(USAGE, error=True)
        return 1
    ref = argv[1]
    repo = os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO
    if fetch is None:
        fetch = github_fetch(
            os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        )
    try:
        ok, _sha, message = verdict(fetch, repo, ref)
    except (GateError, urllib.error.URLError, TimeoutError, ValueError) as error:
        _report(UNPROVEN.format(ref=ref, error=error), error=True)
        return 1
    _report(message, error=not ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
