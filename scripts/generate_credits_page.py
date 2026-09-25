"""The docs site's Credits page, built from the notices generator's own list.

Every component the release binary's notices file credits gets one entry:
name, version, licence, a link to its project and the full licence text.
The components come from `generate_third_party_notices`, so the page and the
binary's sidecar cannot drift apart (issue #2175).

Run as a script to write the page; the mkdocs hook in
`scripts/mkdocs_credits_hook.py` calls `build_page` during every docs build.
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

from packaging.utils import canonicalize_name

NOTICES_MODULE = "generate_third_party_notices"
PYPI_PROJECT_URL = "https://pypi.org/project/{name}/"
# `Project-URL` labels that name the project's own page, most specific first.
# Anything else (Changelog, Issues, Funding) is a fallback only.
HOME_LABELS = ("homepage", "home", "source", "source code", "repository", "code")
ENCODING = "utf-8"

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


def project_url(dist: Distribution) -> str:
    """The project's page: a `Project-URL`, then `Home-page`, then PyPI."""
    entries: list[tuple[str, str]] = []
    for value in dist.metadata.get_all("Project-URL") or []:
        label, _, url = value.partition(",")
        if url.strip():
            entries.append((label.strip().lower(), url.strip()))
    for wanted in HOME_LABELS:
        for label, url in entries:
            if label == wanted:
                return url
    if entries:
        return entries[0][1]
    home = (dist.metadata.get("Home-page") or "").strip()
    if home and home.upper() != "UNKNOWN":
        return home
    return PYPI_PROJECT_URL.format(name=canonicalize_name(dist.metadata["Name"]))


def render(entries: Iterable[tuple[object, str]]) -> str:
    """The page for `(notice, url)` pairs, sorted by name like the notices file."""
    ordered = sorted(entries, key=lambda pair: str(getattr(pair[0], "name")).lower())
    parts = [PAGE_HEADER.format(count=len(ordered))]
    for notice, url in ordered:
        parts.append(
            ENTRY.format(
                name=notice.name,  # type: ignore[attr-defined]
                version=notice.version,  # type: ignore[attr-defined]
                license=notice.license_line(),  # type: ignore[attr-defined]
                url=url,
                text=html.escape("\n\n".join(notice.texts)),  # type: ignore[attr-defined]
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
