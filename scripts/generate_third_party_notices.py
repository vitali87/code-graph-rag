#!/usr/bin/env python3
"""Write the third-party licence notices for a PyInstaller binary.

The standalone binaries bundle every runtime dependency into a single file,
and PyInstaller does not carry the wheels' ``.dist-info`` licence files along
with the code it collects. Every permissive licence we depend on (MIT,
Apache-2.0, BSD, MPL-2.0, ...) conditions redistribution on keeping its
copyright notice and licence text, so a binary release owes a notices file
that the PyPI wheel does not: ``pip`` installs each dependency's own licence
file alongside it.

The set of packages listed is the runtime dependency closure of the
installed ``code-graph-rag`` distribution for the interpreter this runs
under, resolved from ``Requires-Dist`` with environment markers applied.
That is what the binary built on this platform contains; developer tooling
installed in the same virtualenv (pylint, pyinstaller, semgrep, ...) is not
reachable from the project's own requirements and so is never listed.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from email.utils import parseaddr
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

DESCRIPTION = "Write the third-party licence notices for a PyInstaller binary."

ROOT_DISTRIBUTION = "code-graph-rag"
ROOT_EXTRAS = frozenset({"treesitter-full"})

# PEP 639 installs License-File entries under `<dist-info>/licenses/`; wheels
# built against older metadata place them in the `.dist-info` root.
LICENSE_DIRS = ("licenses", "")
LICENSE_FILE_HINTS = ("LICENSE", "LICENCE", "COPYING", "NOTICE")
METADATA_LICENSE_FILE = "License-File"
METADATA_LICENSE_EXPRESSION = "License-Expression"
METADATA_LICENSE = "License"
METADATA_CLASSIFIER = "Classifier"
LICENSE_CLASSIFIER_PREFIX = "License :: "
LICENSE_CLASSIFIER_OSI_PREFIX = "License :: OSI Approved :: "
UNKNOWN_LICENSE = "UNKNOWN"

# Some wheels declare a licence but ship no licence file (loguru, logfire-api,
# fastmcp-slim, ...). For those the canonical SPDX text is reproduced with
# the package's copyright holder filled in; a package whose licence has no
# template here stops generation rather than shipping an incomplete notice.
LICENSE_TEXTS_DIR = Path(__file__).resolve().parent / "license_texts"
LICENSE_TEXT_SUFFIX = ".txt"
SPDX_ALIASES: dict[str, str] = {
    "mit": "MIT",
    "mit license": "MIT",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache2": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "isc": "ISC",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
}
TEMPLATE_HOLDER_PATTERN = re.compile(r"<year>\s*<(?:copyright holders|owner)>")
TEMPLATE_NOTE = (
    "(This distribution ships no licence file; the canonical {spdx} text is "
    "reproduced from the SPDX License List.)"
)
COPYRIGHT_LINE = "Copyright (c) {holder}"
METADATA_AUTHOR = "Author"
METADATA_AUTHOR_EMAIL = "Author-email"
METADATA_MAINTAINER = "Maintainer"
METADATA_MAINTAINER_EMAIL = "Maintainer-email"
METADATA_NAME = "Name"

MISSING_TEXT_ERROR = (
    "{count} package(s) ship no licence text and have no SPDX template: {names}"
)

HEADER = """\
THIRD-PARTY SOFTWARE NOTICES

{root} {version} is distributed under the MIT License. The standalone
binary bundles the following open source packages, each of which is the
property of its respective copyright holders and is distributed under the
licence reproduced below.

{count} packages.
"""
SEPARATOR = "-" * 78
ENTRY = """\
{separator}
{name} {version}
License: {license}
{separator}

{text}
"""
ENCODING = "utf-8"


@dataclass(frozen=True)
class Notice:
    name: str
    version: str
    license: str
    texts: tuple[str, ...]


def _license_expression(dist: Distribution) -> str:
    """Prefer PEP 639's `License-Expression`, then `License`, then classifiers."""
    metadata = dist.metadata
    expression = metadata.get(METADATA_LICENSE_EXPRESSION)
    if expression:
        return expression.strip()

    # A multi-line `License` field carries the whole licence text, which
    # belongs in the body, not the one-line summary.
    legacy = metadata.get(METADATA_LICENSE)
    if legacy and legacy.strip() and "\n" not in legacy.strip():
        return legacy.strip()

    classifiers = [
        c.removeprefix(LICENSE_CLASSIFIER_OSI_PREFIX).removeprefix(
            LICENSE_CLASSIFIER_PREFIX
        )
        for c in metadata.get_all(METADATA_CLASSIFIER) or ()
        if c.startswith(LICENSE_CLASSIFIER_PREFIX)
    ]
    if classifiers:
        return "; ".join(classifiers)
    if legacy and legacy.strip():
        return legacy.strip().splitlines()[0]
    return UNKNOWN_LICENSE


def _dist_info_dir(dist: Distribution) -> str | None:
    for path in dist.files or ():
        parts = path.parts
        if len(parts) > 1 and parts[0].endswith(".dist-info"):
            return parts[0]
    return None


def _license_paths(dist: Distribution) -> list[str]:
    """Paths (relative to the install root) of the distribution's licence files."""
    dist_info = _dist_info_dir(dist)
    if dist_info is None:
        return []

    installed = {str(path) for path in dist.files or ()}
    declared = dist.metadata.get_all(METADATA_LICENSE_FILE) or []
    found: list[str] = []
    for declared_path in declared:
        for subdir in LICENSE_DIRS:
            candidate = "/".join(p for p in (dist_info, subdir, declared_path) if p)
            if candidate in installed:
                found.append(candidate)
                break
    if found:
        return found

    # Pre-PEP 639 wheels may ship a licence file without declaring it.
    return sorted(
        path
        for path in installed
        if path.startswith(f"{dist_info}/")
        and any(hint in Path(path).name.upper() for hint in LICENSE_FILE_HINTS)
    )


def _license_texts(dist: Distribution) -> tuple[str, ...]:
    texts: list[str] = []
    for path in _license_paths(dist):
        text = dist.read_text(path.split("/", 1)[1]) if "/" in path else None
        if text and text.strip():
            texts.append(text.strip())
    if texts:
        return tuple(texts)

    legacy = dist.metadata.get(METADATA_LICENSE)
    if legacy and "\n" in legacy.strip():
        return (legacy.strip(),)
    return ()


def _copyright_holder(dist: Distribution) -> str:
    metadata = dist.metadata
    for field in (METADATA_AUTHOR, METADATA_MAINTAINER):
        value = metadata.get(field)
        if value and value.strip():
            return value.strip()
    # PEP 621 folds the name into the address: `Jane Doe <jane@example.org>`.
    for field in (METADATA_AUTHOR_EMAIL, METADATA_MAINTAINER_EMAIL):
        value = metadata.get(field)
        if value:
            name, address = parseaddr(value)
            if name.strip():
                return name.strip()
            if address.strip():
                return address.strip()
    return metadata[METADATA_NAME]


def _spdx_id(expression: str) -> str | None:
    return SPDX_ALIASES.get(expression.strip().lower())


def _template_text(dist: Distribution, expression: str) -> str | None:
    """Canonical SPDX text for `expression` with the holder filled in, if templated."""
    spdx = _spdx_id(expression)
    if spdx is None:
        return None
    template_path = LICENSE_TEXTS_DIR / f"{spdx}{LICENSE_TEXT_SUFFIX}"
    if not template_path.is_file():
        return None

    holder = _copyright_holder(dist)
    template = template_path.read_text(encoding=ENCODING).strip()
    body, substituted = TEMPLATE_HOLDER_PATTERN.subn(holder, template, count=1)
    if not substituted:
        body = f"{COPYRIGHT_LINE.format(holder=holder)}\n\n{body}"
    return f"{TEMPLATE_NOTE.format(spdx=spdx)}\n\n{body}"


def _active_requirements(
    dist: Distribution, extras: frozenset[str]
) -> list[Requirement]:
    """`Requires-Dist` entries whose markers hold for this interpreter and extras."""
    active: list[Requirement] = []
    for spec in dist.requires or ():
        try:
            requirement = Requirement(spec)
        except InvalidRequirement:
            continue
        if requirement.marker is None:
            active.append(requirement)
            continue
        # A requirement gated on an extra is active only for that extra; one
        # with a plain platform marker evaluates identically for every extra.
        if any(
            requirement.marker.evaluate({"extra": extra}) for extra in extras | {""}
        ):
            active.append(requirement)
    return active


def runtime_closure(
    root: str = ROOT_DISTRIBUTION, root_extras: frozenset[str] = ROOT_EXTRAS
) -> dict[str, Distribution]:
    """Installed distributions reachable from `root`, keyed by canonical name.

    The root itself is excluded: the notices file credits third parties, and
    the project's own licence ships as `LICENSE`.
    """
    root_dist = distribution(root)
    seen: dict[str, Distribution] = {}
    pending: deque[tuple[Distribution, frozenset[str]]] = deque(
        [(root_dist, root_extras)]
    )
    visited_with_extras: set[tuple[str, frozenset[str]]] = set()

    while pending:
        dist, extras = pending.popleft()
        key = (canonicalize_name(dist.metadata["Name"]), extras)
        if key in visited_with_extras:
            continue
        visited_with_extras.add(key)

        for requirement in _active_requirements(dist, extras):
            name = canonicalize_name(requirement.name)
            try:
                dep = distribution(name)
            except PackageNotFoundError:
                continue
            if name != canonicalize_name(root):
                seen.setdefault(name, dep)
            pending.append((dep, frozenset(requirement.extras)))

    return seen


def _notice(dist: Distribution) -> Notice:
    expression = _license_expression(dist)
    texts = _license_texts(dist)
    if not texts:
        template = _template_text(dist, expression)
        if template is not None:
            texts = (template,)
    return Notice(
        name=dist.metadata[METADATA_NAME],
        version=dist.version,
        license=expression,
        texts=texts,
    )


def collect_notices(dists: Iterable[Distribution]) -> list[Notice]:
    return [_notice(dist) for dist in dists]


def render(notices: list[Notice], root: str = ROOT_DISTRIBUTION) -> str:
    root_version = distribution(root).version
    parts = [HEADER.format(root=root, version=root_version, count=len(notices))]
    for notice in sorted(notices, key=lambda n: n.name.lower()):
        parts.append(
            ENTRY.format(
                separator=SEPARATOR,
                name=notice.name,
                version=notice.version,
                license=notice.license,
                text="\n\n".join(notice.texts),
            )
        )
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="file to write; parent directory must exist",
    )
    args = parser.parse_args(argv)

    notices = collect_notices(runtime_closure().values())

    # A notice without its licence text does not satisfy the licence it is
    # meant to satisfy, so refuse to produce the file rather than ship it.
    missing = sorted(n.name for n in notices if not n.texts)
    if missing:
        print(  # noqa: T201
            MISSING_TEXT_ERROR.format(count=len(missing), names=", ".join(missing)),
            file=sys.stderr,
        )
        return 1

    args.output.write_text(render(notices), encoding=ENCODING)
    print(f"wrote {args.output} ({len(notices)} packages)")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
