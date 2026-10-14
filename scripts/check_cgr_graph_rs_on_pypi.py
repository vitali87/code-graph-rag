#!/usr/bin/env python3
"""Refuse a release until PyPI has the cgr-graph-rs wheels it requires.

code-graph-rag requires the cgr-graph-rs version in rust/cgr_graph_rs on
every platform .github/workflows/rust-wheels.yml builds a wheel for, and the
crate has no sdist to fall back on. publish.yml runs this twice: to decide
whether to build and upload the crate (a release an earlier run left partial
counts as missing), then again before uploading code-graph-rag. `make
release` (scripts/release.sh) cannot build every platform's wheel, so it runs
this before uploading anything and stops while any of those wheels is missing.

Exit status: 0 when every wheel is there, INCOMPLETE when the release or any
wheel is missing, 1 when PyPI could not be asked.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CRATE_PYPROJECT = REPO_ROOT / "rust" / "cgr_graph_rs" / "pyproject.toml"
DIST_NAME = "cgr-graph-rs"
RELEASE_URL = "https://pypi.org/pypi/{name}/{version}/json"
TIMEOUT_S = 15
INCOMPLETE = 2
# The version goes into a URL; a release version is plain MAJOR.MINOR.PATCH.
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
WHEEL_SUFFIX = ".whl"
# The platform part of each wheel's tag, by the target rust-wheels.yml builds;
# a test keeps the two lists the same.
WHEEL_TAGS = {
    "x86_64-unknown-linux-gnu": "manylinux2014_x86_64",
    "aarch64-unknown-linux-gnu": "manylinux2014_aarch64",
    "x86_64-unknown-linux-musl": "musllinux_1_2_x86_64",
    "aarch64-unknown-linux-musl": "musllinux_1_2_aarch64",
    "universal2-apple-darwin": "universal2",
    "x86_64-pc-windows-msvc": "win_amd64",
}
MISSING_RELEASE = (
    "{name} {version} is not on PyPI, and code-graph-rag requires it. "
    "Publish it first: tag the release so publish.yml builds and uploads it, "
    "or run the Rust Wheels workflow and upload its wheels."
)
MISSING_WHEELS = "{name} {version} on PyPI has no wheel for: {targets}."
UNREACHABLE = "Could not ask PyPI about {name} {version}: {error}"
MALFORMED = "Refusing malformed {name} version {version!r}."
FOUND = "{name} {version} has a wheel on PyPI for every platform it is required on."


def crate_version() -> str:
    with CRATE_PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def missing_targets(filenames: Iterable[str]) -> list[str]:
    wheels = [name for name in filenames if name.endswith(WHEEL_SUFFIX)]
    return [
        target
        for target, tag in WHEEL_TAGS.items()
        if not any(tag in wheel for wheel in wheels)
    ]


def released_filenames(version: str) -> list[str] | None:
    """The release's file names, or None when PyPI has no such release."""
    url = RELEASE_URL.format(name=DIST_NAME, version=version)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:  # noqa: S310 - fixed https://pypi.org URL
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    return [entry["filename"] for entry in payload.get("urls", [])]


def main() -> int:
    version = crate_version()
    if not VERSION_PATTERN.fullmatch(version):
        print(MALFORMED.format(name=DIST_NAME, version=version), file=sys.stderr)  # noqa: T201
        return 1
    try:
        filenames = released_filenames(version)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        print(  # noqa: T201
            UNREACHABLE.format(name=DIST_NAME, version=version, error=error),
            file=sys.stderr,
        )
        return 1
    if filenames is None:
        print(MISSING_RELEASE.format(name=DIST_NAME, version=version), file=sys.stderr)  # noqa: T201
        return INCOMPLETE
    if missing := missing_targets(filenames):
        print(  # noqa: T201
            MISSING_WHEELS.format(
                name=DIST_NAME, version=version, targets=", ".join(missing)
            ),
            file=sys.stderr,
        )
        return INCOMPLETE
    print(FOUND.format(name=DIST_NAME, version=version))  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
