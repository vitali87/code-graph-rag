#!/usr/bin/env bash
set -euo pipefail

# Local release: sync server.json to the pyproject version, then build, verify,
# and publish that version to PyPI and create the matching git tag and GitHub
# Release. Use this when the GitHub Actions publish workflow is unavailable
# (e.g. billing disabled).
#
# Credentials: twine prompts for a PyPI token (username __token__). To avoid the
# prompt, export TWINE_USERNAME=__token__ and TWINE_PASSWORD=pypi-... or set up
# ~/.pypirc beforehand.

VERSION=$(grep -E '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
TAG="v${VERSION}"

echo "==> Releasing ${TAG}"

if [ -n "$(git status --porcelain)" ]; then
  echo "Error: working tree is not clean. Commit or stash changes first." >&2
  exit 1
fi

if git rev-parse "${TAG}" >/dev/null 2>&1; then
  echo "Error: tag ${TAG} already exists. Bump the version in pyproject.toml first." >&2
  exit 1
fi

# Nothing ships that main's CI has not passed, here as in publish.yml. It
# asks about HEAD before the server.json sync below, whose commit only ever
# exists locally; that commit changes nothing but version strings.
echo "==> Requiring green CI on main at HEAD"
GH_TOKEN="${GH_TOKEN:-$(gh auth token 2>/dev/null || true)}" \
  uv run --no-project --python ">=3.12" python scripts/require_green_ci.py "$(git rev-parse HEAD)"

# code-graph-rag requires cgr-graph-rs, whose wheels this machine cannot build
# for every platform; publishing without them would make this version
# uninstallable, so stop before anything is committed or uploaded.
echo "==> Checking cgr-graph-rs on PyPI"
uv run --no-project --python ">=3.12" python scripts/check_cgr_graph_rs_on_pypi.py

echo "==> Syncing server.json to ${VERSION}"
perl -i -pe 's/"version": "[^"]*"/"version": "'"${VERSION}"'"/g' server.json
if [ -n "$(git status --porcelain server.json)" ]; then
  git commit -m "chore: sync server.json version to ${VERSION}" server.json
fi

echo "==> Building distributions"
rm -rf dist/
uv build

echo "==> Checking distributions"
uvx twine check dist/*

echo "==> Uploading to PyPI"
uvx twine upload dist/*

echo "==> Tagging and creating GitHub Release"
git tag "${TAG}"
git push origin "${TAG}"
# Note: this fires the publish.yml workflow, which will fail harmlessly while
# Actions billing is unavailable. PyPI is already published by the step above.
gh release create "${TAG}" --generate-notes --target main

echo "==> Released ${TAG} at https://pypi.org/project/code-graph-rag/${VERSION}/"
