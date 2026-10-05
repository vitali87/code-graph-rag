"""The docs site's Credits page, built from the notices generator's own list.

Every component the release binary's notices file credits gets one entry:
name, version, licence, a link to its project and the full licence text.
The components come from `generate_third_party_notices`, so the page and the
binary's sidecar cannot drift apart (issue #2175).

Run as a script to write the page; the docs workflow does so before every
site build, since Zensical does not run mkdocs.yml hooks.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import sys
from collections.abc import Iterable
from importlib.metadata import Distribution
from pathlib import Path
from types import ModuleType
from typing import Protocol
from urllib.parse import quote, urlsplit

from packaging.utils import canonicalize_name

NOTICES_MODULE = "generate_third_party_notices"
PYPI_PROJECT_URL = "https://pypi.org/project/{name}/"
# `Project-URL` labels that name the project's own page, most specific first.
# Anything else (Changelog, Issues, Funding) is a fallback only.
HOME_LABELS = ("homepage", "home", "source", "source code", "repository", "code")
ENCODING = "utf-8"
# A project URL is third-party metadata written into Markdown the docs site
# renders: only a web URL with a host is linked, and the characters that
# would end the link or open raw HTML are percent-encoded.
LINK_SCHEMES = frozenset({"http", "https"})
URL_SAFE_CHARS = ":/?#[]@!$&'*+,;=%~"
# HTML escaping stops tags but not Markdown: `[x](javascript:...)` in a
# component's metadata would still render as a link. Escaping the backslash
# and the link brackets makes any such text literal; ordinary distribution
# names and versions contain none of them.
MARKDOWN_LINK_CHARS = ("\\", "[", "]")

PAGE_HEADER = """\
# Credits

code-graph-rag is built on the open-source projects below. Each entry names
the component, the version this release was built against, its licence, a
link to its project and the full licence text. The release binaries ship the
same list as a `.THIRD_PARTY_NOTICES.txt` file beside each binary.

{count} components.
"""
ENTRY = """\
## {name}

**Version:** {version} · **Licence:** {license} · [Project page]({url})

<details><summary>Licence text</summary>

<pre>{text}</pre>

</details>
"""


class NoticeEntry(Protocol):
    """What `render` reads from a `generate_third_party_notices.Notice`."""

    name: str
    version: str
    texts: tuple[str, ...]

    def license_line(self) -> str: ...


def _load_notices() -> ModuleType:
    """Load the sibling notices generator by path; `scripts/` is not a package."""
    existing = sys.modules.get(NOTICES_MODULE)
    if existing is not None and hasattr(existing, "collect_notices"):
        return existing
    path = Path(__file__).with_name(f"{NOTICES_MODULE}.py")
    spec = importlib.util.spec_from_file_location(NOTICES_MODULE, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[NOTICES_MODULE] = module
    spec.loader.exec_module(module)
    return module


def _inline_text(value: str) -> str:
    """Third-party `value` as literal text in a Markdown line."""
    text = html.escape(value)
    for char in MARKDOWN_LINK_CHARS:
        text = text.replace(char, f"\\{char}")
    return text


def _link_target(url: str) -> str | None:
    """`url` made safe inside a Markdown link, or None when it is not a web URL."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in LINK_SCHEMES or not parts.netloc:
        return None
    return quote(url, safe=URL_SAFE_CHARS)


def project_url(dist: Distribution) -> str:
    """The project's page: a `Project-URL`, then `Home-page`, then PyPI.

    A candidate that is not an http(s) URL with a host is skipped, so the
    fallback is always a link the page can carry.
    """
    entries: list[tuple[str, str]] = []
    for value in dist.metadata.get_all("Project-URL") or []:
        label, _, url = value.partition(",")
        if target := _link_target(url.strip()):
            entries.append((label.strip().lower(), target))
    for wanted in HOME_LABELS:
        for label, url in entries:
            if label == wanted:
                return url
    if entries:
        return entries[0][1]
    home = (dist.metadata.get("Home-page") or "").strip()
    if home.upper() != "UNKNOWN" and (target := _link_target(home)):
        return target
    return PYPI_PROJECT_URL.format(name=canonicalize_name(dist.metadata["Name"]))


def render(entries: Iterable[tuple[NoticeEntry, str]]) -> str:
    """The page for `(notice, url)` pairs, sorted by name like the notices file.

    Every value is third-party metadata, so each is HTML-escaped before it
    reaches the rendered Markdown; `url` comes from `project_url`.
    """
    ordered = sorted(entries, key=lambda pair: pair[0].name.lower())
    parts = [PAGE_HEADER.format(count=len(ordered))]
    for notice, url in ordered:
        parts.append(
            ENTRY.format(
                name=_inline_text(notice.name),
                version=_inline_text(notice.version),
                license=_inline_text(notice.license_line()),
                url=url,
                text=html.escape("\n\n".join(notice.texts)),
            )
        )
    return "\n".join(parts)


def build_page() -> str:
    """The Credits page for the installed runtime closure."""
    notices = _load_notices()
    dists = list(notices.runtime_closure().values())
    collected = notices.collect_notices(dists)
    return render(zip(collected, (project_url(dist) for dist in dists), strict=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the docs Credits page.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.write_text(build_page(), encoding=ENCODING)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
