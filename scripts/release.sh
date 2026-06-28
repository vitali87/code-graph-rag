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
