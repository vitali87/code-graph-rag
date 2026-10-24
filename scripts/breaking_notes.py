#!/usr/bin/env python3
"""Breaking-change notes: the pull request check and the release section.

A pull request labelled "breaking change" carries its migration notes as
changes/breaking/<pull request number>.md. A file is reviewed with the code and
fixed once merged; a pull request description is neither, as its author can
edit it at any time. `check` makes the label, a conventional-commit "!" in the
title and the notes agree, so neither reaches main without the other. `render`
turns the notes a release adds into its "⚠️ Breaking changes" section and names
a labelled pull request that added none, so a breaking release is never
published without the section, as v0.3.2 was (issue #3431). Standard library
only, like update_news.py, so both workflows run it without syncing the
project environment.
"""

# ruff: noqa: T201
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRAGMENT_DIR = PurePosixPath("changes/breaking")
LABEL = "breaking change"
HEADING = "## ⚠️ Breaking changes"
# A conventional-commit header, as ci.yml's title check parses it: a scope is
# whatever sits in the parentheses, "rename,imports" and "mcp/server.py" among
# them. Only the "!" before the colon marks a breaking change, never one in the
# description.
BREAKING_TITLE = re.compile(r"^[a-z]+(\([^()]+\))?!: ")
FRAGMENT_NAME = re.compile(r"^([1-9][0-9]*)\.md$")


def _has_text(path: Path) -> bool:
    return path.is_file() and bool(path.read_text(encoding="utf-8").strip())


def check_pull_request(
    number: int, title: str, labels: list[str], root: Path, added: Iterable[str] = ()
) -> list[str]:
    errors: list[str] = []
    labelled = LABEL in labels
    fragment = FRAGMENT_DIR / f"{number}.md"
    if BREAKING_TITLE.match(title) and not labelled:
        errors.append(
            f'The title marks a breaking change with "!", but the pull request '
            f'is not labelled "{LABEL}". A maintainer applies the label.'
        )
    if labelled and not BREAKING_TITLE.match(title):
        errors.append(
            f'The pull request is labelled "{LABEL}", but its title has no "!" '
            'before the colon. Mark it, as in "feat(scope)!: summary".'
        )
    if labelled and not _has_text(root / fragment):
        errors.append(
            f'The pull request is labelled "{LABEL}", but {fragment} is missing '
            "or empty. Add the release notes there: what changed, how to keep "
            "the old behaviour, what happens to existing installs."
        )
    if not labelled and (root / fragment).exists():
        errors.append(
            f"{fragment} holds breaking-change notes, but the pull request is "
            f'not labelled "{LABEL}", so its release would not be breaking.'
        )
    # The release publishes every fragment added since the last release, so
    # notes filed under another number would never meet the label check.
    errors.extend(
        f"{path} is added by this pull request, which may add notes only as {fragment}."
        for path in added
        if PurePosixPath(path) != fragment
    )
    directory = root / FRAGMENT_DIR
    if directory.is_dir():
        errors.extend(
            f"{FRAGMENT_DIR / entry.name} is not named <pull request number>.md, "
            "so no release would publish it."
            for entry in sorted(directory.iterdir())
            if not FRAGMENT_NAME.match(entry.name)
        )
    return errors


def render_section(added: list[str], labelled: list[int], root: Path) -> str:
    notes: dict[int, str] = {}
    for line in added:
        path = PurePosixPath(line)
        match = FRAGMENT_NAME.match(path.name)
        if path.parent != FRAGMENT_DIR or match is None:
            raise ValueError(f"{line} is not a {FRAGMENT_DIR}/<number>.md fragment")
        if text := (root / path).read_text(encoding="utf-8").strip():
            notes[int(match.group(1))] = text
    blocks = [notes[number] for number in sorted(notes)]
    blocks += [
        f"This release contains a breaking change; see #{number}."
        for number in sorted(set(labelled) - notes.keys())
    ]
    return "\n\n".join([HEADING, *blocks]) + "\n\n" if blocks else ""


def _pull_requests(text: str) -> list[int]:
    numbers = text.split()
    if not all(re.fullmatch(r"[1-9][0-9]*", number) for number in numbers):
        raise argparse.ArgumentTypeError(f"not pull request numbers: {text!r}")
    return [int(number) for number in numbers]


def _labels(text: str) -> list[str]:
    labels = json.loads(text)
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise argparse.ArgumentTypeError(f"not a JSON list of labels: {text!r}")
    return labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Breaking-change notes: the pull request check and the "
        "release section."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check one pull request")
    check.add_argument("--number", type=int, required=True)
    check.add_argument("--title", required=True)
    check.add_argument("--labels-json", type=_labels, required=True)
    check.add_argument(
        "--added",
        type=str.splitlines,
        default=[],
        help="the paths under changes/breaking/ the pull request adds, one per line",
    )
    render = commands.add_parser(
        "render", help="write the release section from fragment paths on stdin"
    )
    render.add_argument("--labelled", type=_pull_requests, default=[])
    args = parser.parse_args(argv)
    if args.command == "check":
        errors = check_pull_request(
            args.number, args.title, args.labels_json, PROJECT_ROOT, args.added
        )
        for error in errors:
            print(f"::error::{error}")
        return 1 if errors else 0
    added = [line.strip() for line in sys.stdin if line.strip()]
    sys.stdout.write(render_section(added, args.labelled, PROJECT_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
