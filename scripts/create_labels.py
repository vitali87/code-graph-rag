#!/usr/bin/env python3
# ruff: noqa: T201

import os
import sys
from pathlib import Path
from typing import Annotated

try:
    import requests
    import typer
    import yaml
except ImportError:
    print("Error: Required packages not installed.")
    print("Run: uv add pyyaml requests typer")
    sys.exit(1)


def load_labels_config() -> list[dict]:
    """Load labels from .github/labels.yml"""
    labels_file = Path(__file__).parent.parent / ".github" / "labels.yml"

    if not labels_file.exists():
        print(f"Error: Labels file not found at {labels_file}")
        sys.exit(1)

    with open(labels_file, encoding="utf-8") as f:
        labels = yaml.safe_load(f)

    return labels


def get_github_token(
    token: str | None = None, repo: str = "vitali87/code-graph-rag"
) -> tuple[str, str]:
    """Get GitHub token from environment or command line"""
    resolved_token = token or os.environ.get("GITHUB_TOKEN")

    if not resolved_token:
        print("Error: GitHub token required.")
        print("Provide via --token argument or GITHUB_TOKEN environment variable")
        sys.exit(1)

    assert resolved_token is not None
    return resolved_token, repo


def get_existing_labels(repo: str, token: str) -> dict[str, dict]:
    """Get existing labels from repository"""
    url = f"https://api.github.com/repos/{repo}/labels"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return {label["name"]: label for label in response.json()}


def create_or_update_label(
    repo: str, token: str, label: dict, existing: dict | None = None
):
    """Create or update a label"""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    data = {
        "name": label["name"],
        "color": label["color"],
        "description": label.get("description", ""),
    }

    if existing:
        url = f"https://api.github.com/repos/{repo}/labels/{label['name']}"
        response = requests.patch(url, headers=headers, json=data)
        action = "Updated"
    else:
        url = f"https://api.github.com/repos/{repo}/labels"
        response = requests.post(url, headers=headers, json=data)
        action = "Created"

    if response.status_code in (200, 201):
        print(f"OK {action} label: {label['name']}")
        return True
    else:
        print(f"Failed to {action.lower()} label: {label['name']}")
        print(f"   Response: {response.status_code} - {response.text}")
        return False


def main(
    token: Annotated[
        str | None, typer.Option(help="GitHub personal access token")
    ] = None,
    repo: Annotated[
        str, typer.Option(help="Repository in owner/name format")
    ] = "vitali87/code-graph-rag",
) -> None:
    """Main function"""
    resolved_token, resolved_repo = get_github_token(token, repo)
    labels_config = load_labels_config()

    print(f"Syncing labels for {resolved_repo}")
    print(f"Loading {len(labels_config)} labels from configuration\n")

    try:
        existing_labels = get_existing_labels(resolved_repo, resolved_token)
        print(f"Found {len(existing_labels)} existing labels\n")
    except Exception as e:
        print(f"Error fetching existing labels: {e}")
        sys.exit(1)

    success_count = 0
    fail_count = 0

    for label in labels_config:
        existing = existing_labels.get(label["name"])

        try:
            if create_or_update_label(resolved_repo, resolved_token, label, existing):
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            print(f"Error with label {label['name']}: {e}")
            fail_count += 1

    print(f"\nDone! Created/Updated: {success_count}, Failed: {fail_count}")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    typer.run(main)
