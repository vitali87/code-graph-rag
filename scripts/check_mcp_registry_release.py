#!/usr/bin/env python3
"""Hold server.json back from the MCP Registry until PyPI serves its release.

The registry takes server.json as the record of a release: at publish time it
fetches the PyPI version server.json names and refuses it unless that
version's README carries the `mcp-name: <server name>` ownership token, and a
version it has listed cannot be published again. publish.yml runs this after
uploading code-graph-rag, before `mcp-publisher publish`, so the registry
never lists a version PyPI does not serve:

    check_mcp_registry_release.py pypi vX.Y.Z      is that release on PyPI?
    check_mcp_registry_release.py registry vX.Y.Z  is it in the registry?

Both first refuse a server.json whose versions disagree with the tag or with
pyproject.toml, or whose package is not this project's PyPI distribution.

Exit status: 0 when the release is there, MISSING when it is not (yet), 1
when server.json is wrong or the service could not be asked.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TypedDict

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "server.json"
PYPROJECT = REPO_ROOT / "pyproject.toml"
PYPI_RELEASE_URL = "https://pypi.org/pypi/{name}/{version}/json"
# A deleted version still holds its number, and the registry hides it unless
# asked, so a rerun would try to publish it again and fail.
REGISTRY_VERSION_URL = (
    "https://registry.modelcontextprotocol.io/v0.1/servers/{name}/versions/{version}"
    "?include_deleted=true"
)
TIMEOUT_S = 15
MISSING = 2
PYPI = "pypi"
REGISTRY = "registry"
# The version goes into both services' URLs; a release is plain MAJOR.MINOR.PATCH.
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
# The registry's own rule for where the token ends: a character that cannot
# continue a server name, or an HTML comment close.
NAME_CHAR = re.compile(r"[A-Za-z0-9._/-]")
COMMENT_CLOSES = ("-->", "--!>")

USAGE = f"usage: check_mcp_registry_release.py {{{PYPI}|{REGISTRY}}} vX.Y.Z"
BAD_TAG = "Refusing release tag {tag!r}: expected vMAJOR.MINOR.PATCH."
VERSION_MISMATCH = (
    "server.json {field} is {found!r}, but the release is {expected!r}. The "
    "registry would list a version PyPI does not serve under this tag."
)
PYPROJECT_MISMATCH = (
    "pyproject.toml version is {found!r}, but the release is {expected!r}: "
    "the tag does not point at that release's commit."
)
NOT_OUR_PACKAGE = (
    "server.json package {index} is {found!r}; the registry release must point "
    "at the PyPI distribution {expected!r}."
)
NOT_ON_PYPI = "{name} {version} is not on PyPI yet."
NO_TOKEN = (
    "{name} {version} on PyPI lacks the 'mcp-name: {server}' token the "
    "registry requires in its README (PYPI_README.md)."
)
ON_PYPI = "{name} {version} is on PyPI with the registry's ownership token."
NOT_LISTED = "{server} {version} is not in the MCP Registry yet."
LISTED = "{server} {version} is already in the MCP Registry."
UNREACHABLE = "Could not ask {service} about {name} {version}: {error}"


class Package(TypedDict, total=False):
    registryType: str
    identifier: str
    version: str


class Manifest(TypedDict, total=False):
    name: str
    title: str
    description: str
    version: str
    packages: list[Package]


class PyPIInfo(TypedDict, total=False):
    description: str | None


class PyPIRelease(TypedDict, total=False):
    info: PyPIInfo


def load_manifest() -> Manifest:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def project_metadata() -> tuple[str, str]:
    with PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return project["name"], project["version"]


def manifest_problems(manifest: Manifest, *, name: str, version: str) -> list[str]:
    problems = []
    if manifest.get("version") != version:
        problems.append(
            VERSION_MISMATCH.format(
                field="version", found=manifest.get("version"), expected=version
            )
        )
    for index, package in enumerate(manifest.get("packages", [])):
        if (package.get("registryType"), package.get("identifier")) != (PYPI, name):
            problems.append(
                NOT_OUR_PACKAGE.format(
                    index=index, found=package.get("identifier"), expected=name
                )
            )
        if package.get("version") != version:
            problems.append(
                VERSION_MISMATCH.format(
                    field=f"packages[{index}].version",
                    found=package.get("version"),
                    expected=version,
                )
            )
    return problems


def has_name_token(readme: str, server: str) -> bool:
    token = f"mcp-name: {server}"
    start = readme.find(token)
    while start >= 0:
        rest = readme[start + len(token) :]
        if not rest or not NAME_CHAR.match(rest[0]) or rest.startswith(COMMENT_CLOSES):
            return True
        start = readme.find(token, start + 1)
    return False


def _fetch(url: str) -> bytes | None:
    """The body at url, or None when the service answers 404."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 - fixed https URLs above
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - fixed https URLs above
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def pypi_readme(name: str, version: str) -> str | None:
    """The README PyPI holds for the release, or None when it has none."""
    body = _fetch(PYPI_RELEASE_URL.format(name=name, version=version))
    if body is None:
        return None
    release: PyPIRelease = json.loads(body)
    return release.get("info", {}).get("description") or ""


def registry_lists(server: str, version: str) -> bool:
    url = REGISTRY_VERSION_URL.format(
        name=urllib.parse.quote(server, safe=""), version=version
    )
    return _fetch(url) is not None


def check_pypi(name: str, server: str, version: str) -> int:
    readme = pypi_readme(name, version)
    if readme is None:
        print(NOT_ON_PYPI.format(name=name, version=version), file=sys.stderr)  # noqa: T201
        return MISSING
    if not has_name_token(readme, server):
        print(  # noqa: T201
            NO_TOKEN.format(name=name, version=version, server=server),
            file=sys.stderr,
        )
        return 1
    print(ON_PYPI.format(name=name, version=version))  # noqa: T201
    return 0


def check_registry(server: str, version: str) -> int:
    if registry_lists(server, version):
        print(LISTED.format(server=server, version=version))  # noqa: T201
        return 0
    print(NOT_LISTED.format(server=server, version=version))  # noqa: T201
    return MISSING


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in (PYPI, REGISTRY):
        print(USAGE, file=sys.stderr)  # noqa: T201
        return 1
    service, tag = args
    if not (tag.startswith("v") and VERSION_PATTERN.fullmatch(tag[1:])):
        print(BAD_TAG.format(tag=tag), file=sys.stderr)  # noqa: T201
        return 1
    version = tag[1:]
    name, project_version = project_metadata()
    manifest = load_manifest()
    problems = manifest_problems(manifest, name=name, version=version)
    if project_version != version:
        problems.append(
            PYPROJECT_MISMATCH.format(found=project_version, expected=version)
        )
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)  # noqa: T201
        return 1
    server = manifest.get("name", "")
    try:
        if service == PYPI:
            return check_pypi(name, server, version)
        return check_registry(server, version)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        print(  # noqa: T201
            UNREACHABLE.format(
                service=service, name=server, version=version, error=error
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
