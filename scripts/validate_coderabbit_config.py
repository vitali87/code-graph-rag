#!/usr/bin/env python3
"""Validate `.coderabbit.yaml` before it can reach main (issue #1823).

Two settings in that file are load-bearing and both fail silently:

* `reviews.pre_merge_checks.docstrings.mode` must be the STRING `"off"`
  (issue #1617). Unquoted, YAML yields the boolean `False`, the exemption
  stops applying, and every PR touching a test warns again.
* `reviews.auto_review.base_branches` must be a non-empty list of valid
  regular expressions (issue #1581). Misspell the key and the block is
  ignored, auto-review silently reverts to the default branch only, and
  stacked PRs go unreviewed -- 14 of 18 open PRs, when that was measured.

Validating against the vendor schema is not sufficient on its own, which is
the part worth recording. The schema sets `additionalProperties: false` at
the ROOT object only, so an unknown key nested under `reviews.auto_review`
validates clean: `base_branchez` is reported as valid. A schema check alone
would therefore be a check that cannot fail in the direction it exists for,
because both failure modes above leave everything looking green.

So this asserts the keys it cares about are PRESENT and correctly SHAPED,
and treats the schema as an additional check rather than the only one.
`check-yaml` in pre-commit covers syntax and nothing else.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

# Both are quoted here for the same reason the file must quote them: bare
# `off` is a YAML boolean.
DOCSTRINGS_MODE_PATH = "reviews.pre_merge_checks.docstrings.mode"
DOCSTRINGS_MODE_EXPECTED = "off"
BASE_BRANCHES_PATH = "reviews.auto_review.base_branches"


class ConfigError(ValueError):
    """A config CodeRabbit would reject, or would silently read differently."""


def _walk(data: Any, path: str) -> Any:
    """Return the value at a dotted path, raising if a segment is missing.

    Reports the deepest segment that did resolve, because the common failure
    is a single misspelt key in an otherwise correct block and naming the
    whole path leaves you comparing it against the file by eye.
    """
    node = data
    walked: list[str] = []
    for segment in path.split("."):
        if not isinstance(node, dict):
            where = ".".join(walked) or "the document root"
            raise ConfigError(
                f"{path}: expected a mapping at {where}, got {type(node).__name__}"
            )
        if segment not in node:
            where = ".".join(walked) or "the document root"
            near = ", ".join(sorted(map(str, node))) or "nothing"
            raise ConfigError(
                f"{path}: {where} has no '{segment}' key. It defines: {near}. "
                f"A misspelt key is ignored rather than rejected, so the "
                f"setting silently reverts to its default."
            )
        node = node[segment]
        walked.append(segment)
    return node


def _check_docstrings_mode(data: Any) -> None:
    """Require the docstring pre-merge check to be the string 'off'."""
    mode = _walk(data, DOCSTRINGS_MODE_PATH)
    if mode is False:
        # `mode: off` unquoted is the YAML 1.1 boolean, and the schema wants
        # a string, so name the cause rather than reporting 'False'.
        raise ConfigError(
            f"{DOCSTRINGS_MODE_PATH}: parsed as the boolean False. Bare 'off' "
            f'is a YAML boolean; quote it (mode: "off") so it stays the '
            f"string the schema requires (issue #1617)."
        )
    if not isinstance(mode, str):
        raise ConfigError(
            f"{DOCSTRINGS_MODE_PATH}: must be the string "
            f'"{DOCSTRINGS_MODE_EXPECTED}", got {type(mode).__name__} {mode!r}'
        )
    if mode != DOCSTRINGS_MODE_EXPECTED:
        raise ConfigError(
            f"{DOCSTRINGS_MODE_PATH}: must be "
            f'"{DOCSTRINGS_MODE_EXPECTED}", got {mode!r} (issue #1617)'
        )


def _check_base_branches(data: Any) -> int:
    """Require a non-empty list of compilable regexes, returning its length."""
    patterns = _walk(data, BASE_BRANCHES_PATH)
    if not isinstance(patterns, list):
        raise ConfigError(
            f"{BASE_BRANCHES_PATH}: must be a list, got "
            f"{type(patterns).__name__} {patterns!r}"
        )
    if not patterns:
        # An empty list parses cleanly and reviews nothing beyond the default
        # branch, which is the exact behaviour issue #1581 was filed about.
        raise ConfigError(
            f"{BASE_BRANCHES_PATH}: is empty, so auto-review covers only the "
            f"default branch and every stacked PR is skipped (issue #1581)."
        )
    for index, pattern in enumerate(patterns):
        where = f"{BASE_BRANCHES_PATH}[{index}]"
        if not isinstance(pattern, str) or not pattern.strip():
            raise ConfigError(f"{where}: must be a non-empty string, got {pattern!r}")
        try:
            re.compile(pattern)
        except re.error as exc:
            # CodeRabbit matches these as regexes; one that cannot compile is
            # dropped rather than reported back to us.
            raise ConfigError(f"{where}: invalid regex {pattern!r}: {exc}") from exc
    return len(patterns)


def _check_against_vendor_schema(data: Any, schema: Any) -> None:
    """Report vendor-schema violations, if a schema was supplied.

    This runs in addition to the key checks above, never instead of them:
    the schema's `additionalProperties: false` applies to the root object
    only, so it accepts an unknown key nested anywhere below it.
    """
    from jsonschema import Draft202012Validator

    errors = sorted(
        Draft202012Validator(schema).iter_errors(data),
        key=lambda err: list(err.absolute_path),
    )
    if errors:
        first = errors[0]
        where = ".".join(str(part) for part in first.absolute_path) or "root"
        raise ConfigError(
            f"{where}: {first.message} ({len(errors)} schema violation(s) in total)"
        )


def validate_coderabbit_config(text: str, schema: Any | None = None) -> int:
    """Check a CodeRabbit config, returning how many base branches it lists.

    Raises `ConfigError` naming the setting at fault. `schema`, when given,
    is the parsed vendor JSON schema; it is optional so the checks that
    matter still run with no network access.
    """
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse .coderabbit.yaml: {exc}") from exc

    if data is None:
        raise ConfigError(".coderabbit.yaml is empty")
    if not isinstance(data, dict):
        raise ConfigError(
            f".coderabbit.yaml must be a mapping, got {type(data).__name__}"
        )

    _check_docstrings_mode(data)
    count = _check_base_branches(data)
    if schema is not None:
        _check_against_vendor_schema(data, schema)
    return count


def main() -> int:
    """Validate the shipped config, printing the failure and returning 1."""
    config = Path(__file__).parent.parent / ".coderabbit.yaml"
    try:
        count = validate_coderabbit_config(config.read_text(encoding="utf-8"))
    except ConfigError as exc:
        sys.stderr.write(f"{config.name}: {exc}\n")
        return 1
    sys.stdout.write(f"{config.name}: {count} auto-review base branch(es) OK\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
