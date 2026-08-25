#!/usr/bin/env python3
"""Validate `.github/labels.yml` before it can reach main (issue #1434).

`label-sync.yml` triggers on push to main with a paths filter, so it never
runs on a pull request: a malformed manifest fails after merge, on main,
invisibly to the PR that broke it. This is the same shape as a check that
cannot fail the thing it guards, so the validation runs as a pre-commit hook
and in CI instead.

The rules mirror what the sync action actually consumes: a list of mappings,
each with a unique `name`, a bare six-digit lowercase hex `color`, and a
`description`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

COLOR_RE = re.compile(r"^[0-9a-f]{6}$")
REQUIRED_KEYS = ("name", "color", "description")


class LabelError(ValueError):
    """A manifest that the label syncer would reject or misread."""


def validate_labels(text: str) -> int:
    """Check a labels manifest, returning how many entries it defines.

    Raises `LabelError` with a message naming the offending entry. Colour
    casing is enforced because GitHub happens to preserve it, which makes
    an uppercase value work by undocumented behaviour rather than by
    contract.
    """
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LabelError(f"could not parse labels.yml: {exc}") from exc

    if data is None:
        raise LabelError("labels.yml is empty")
    if not isinstance(data, list):
        raise LabelError(
            f"labels.yml must be a list of labels, got {type(data).__name__}"
        )
    if not data:
        # An empty list parses cleanly and would leave the syncer managing
        # nothing, so a change that empties the file must not read as valid.
        raise LabelError("labels.yml defines no labels")

    seen: set[str] = set()
    for index, entry in enumerate(data):
        where = f"entry {index}"
        if not isinstance(entry, dict):
            raise LabelError(f"{where}: must be a mapping, got {type(entry).__name__}")
        for key in REQUIRED_KEYS:
            if key not in entry:
                raise LabelError(f"{where}: missing '{key}'")

        # The syncer consumes name and description as strings. Coercing a
        # null, number or list here would push a malformed value through to
        # the API instead of reporting it against the line that wrote it.
        name = entry["name"]
        if not isinstance(name, str) or not name.strip():
            raise LabelError(f"{where}: name must be a non-empty string, got {name!r}")
        if name != name.strip():
            # Surrounding whitespace survives into the label and is invisible
            # in review, so `gh pr edit --add-label` then misses it.
            raise LabelError(
                f"{where}: name has leading or trailing whitespace: {name!r}"
            )
        if name in seen:
            raise LabelError(f"{where}: duplicate label name {name!r}")
        seen.add(name)

        description = entry["description"]
        if not isinstance(description, str) or not description.strip():
            raise LabelError(
                f"{where} ({name}): description must be a non-empty string, "
                f"got {description!r}"
            )

        raw_color = entry["color"]
        if raw_color is None:
            # An unquoted '#rrggbb' is a YAML comment, so the value parses as
            # null: report the cause rather than the confusing 'None'.
            raise LabelError(
                f"{where} ({name}): color is empty. An unquoted '#' starts a "
                f"YAML comment, so write six bare hex digits (b60205)."
            )
        if not isinstance(raw_color, str):
            # `color: 000000` is the integer 0, and `color: 123456` is an int
            # too: both need quoting to survive as six digits.
            raise LabelError(
                f"{where} ({name}): color parsed as {type(raw_color).__name__} "
                f"{raw_color!r}; quote it so leading zeros and digit-only "
                f"values stay text (color: '000000')."
            )
        if not COLOR_RE.match(raw_color):
            raise LabelError(
                f"{where} ({name}): color must be six lowercase hex digits "
                f"with no '#', got {raw_color!r}"
            )
    return len(data)


def main() -> int:
    """Validate the manifest, printing the failure and returning non-zero."""
    manifest = Path(__file__).parent.parent / ".github" / "labels.yml"
    try:
        count = validate_labels(manifest.read_text(encoding="utf-8"))
    except LabelError as exc:
        sys.stderr.write(f"{manifest.name}: {exc}\n")
        return 1
    sys.stdout.write(f"{manifest.name}: {count} labels OK\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
