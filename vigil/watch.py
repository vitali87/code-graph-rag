import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import httpx
from loguru import logger
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from . import constants as vc
from . import logs as ls
from .types_defs import Item


class Declared(NamedTuple):
    package: str
    scope: vc.Scope
    scope_name: str | None
    requirement: Requirement
    written: str


class Release(NamedTuple):
    version: str
    released: str | None
    links: dict[str, str]


def declared(pyproject: dict) -> list[Declared]:
    """Every requirement cgr declares, one per package.

    A package listed in several places keeps the scope that reaches the most
    installs (vc.SCOPE_RANK), so jedi in both the `test` and
    `python-semantics` extras is one item, not two.
    """
    project = pyproject.get("project", {})
    sources: list[tuple[vc.Scope, str | None, list[str]]] = [
        (vc.Scope.RUNTIME, None, project.get("dependencies", []))
    ]
    sources += [
        (vc.Scope.EXTRA, name, reqs)
        for name, reqs in project.get("optional-dependencies", {}).items()
    ]
    sources += [
        (vc.Scope.GROUP, name, [r for r in reqs if isinstance(r, str)])
        for name, reqs in pyproject.get("dependency-groups", {}).items()
    ]
    best: dict[str, Declared] = {}
    for scope, scope_name, reqs in sources:
        for written in reqs:
            try:
                req = Requirement(written)
            except InvalidRequirement:
                logger.warning(ls.BAD_REQUIREMENT.format(req=written))
                continue
            package = canonicalize_name(req.name)
            current = best.get(package)
            if current is None or vc.SCOPE_RANK[scope] < vc.SCOPE_RANK[current.scope]:
                best[package] = Declared(package, scope, scope_name, req, written)
    return sorted(best.values(), key=lambda d: d.package)


def locked_versions(lock: dict) -> dict[str, str]:
    return {
        canonicalize_name(p["name"]): p["version"]
        for p in lock.get("package", [])
        if "version" in p
    }


def latest_release(payload: dict) -> Release | None:
    """The newest final release in a PyPI JSON payload.

    Computed from `releases` rather than read from `info.version`, which
    names whatever was uploaded last and can be a backport to an old line.
    Yanked files and pre-releases never count: no resolver picks them for a
    range that does not ask.
    """
    best: tuple[Version, str, str | None] | None = None
    for raw, files in payload.get("releases", {}).items():
        try:
            version = Version(raw)
        except InvalidVersion:
            continue
        live = [f for f in files if not f.get("yanked")]
        if version.is_prerelease or version.is_devrelease or not live:
            continue
        if best is None or version > best[0]:
            uploaded = min(f.get("upload_time_iso_8601", "") for f in live) or None
            best = (version, raw, uploaded)
    if best is None:
        return None
    urls = payload.get("info", {}).get("project_urls") or {}
    return Release(best[1], best[2], dict(urls))


def item_id(package: str, version: str) -> str:
    return vc.ITEM_ID_SEPARATOR.join((vc.ITEM_SOURCE_PYPI, package, version))


def outdated(
    decls: list[Declared], locked: dict[str, str], releases: dict[str, Release]
) -> list[Item]:
    items: list[Item] = []
    for d in decls:
        release = releases.get(d.package)
        pinned = locked.get(d.package)
        if release is None or pinned is None:
            continue
        if Version(release.version) <= Version(pinned):
            continue
        links = {
            "pypi": vc.PYPI_PROJECT_URL.format(name=d.package, version=release.version)
        }
        links.update(release.links)
        items.append(
            Item(
                id=item_id(d.package, release.version),
                package=d.package,
                scope=d.scope,
                scope_name=d.scope_name,
                requirement=d.written,
                locked=pinned,
                latest=release.version,
                in_range=d.requirement.specifier.contains(release.version),
                released=release.released,
                links=links,
            )
        )
    return items


def fetch_releases(packages: list[str]) -> Iterator[tuple[str, Release]]:
    with httpx.Client(timeout=vc.HTTP_TIMEOUT_S, follow_redirects=True) as client:
        for package in packages:
            response = client.get(vc.PYPI_JSON_URL.format(name=package))
            if response.status_code != httpx.codes.OK:
                logger.warning(
                    ls.PYPI_MISS.format(package=package, status=response.status_code)
                )
                continue
            release = latest_release(response.json())
            if release is not None:
                yield package, release


def scan(repo: Path) -> list[Item]:
    pyproject = tomllib.loads((repo / vc.PYPROJECT).read_text(encoding=vc.ENCODING))
    lock = tomllib.loads((repo / vc.UV_LOCK).read_text(encoding=vc.ENCODING))
    decls = declared(pyproject)
    releases = dict(fetch_releases([d.package for d in decls]))
    return outdated(decls, locked_versions(lock), releases)
