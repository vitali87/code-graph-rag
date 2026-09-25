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
import importlib.util
import platform
import re
import sys
import sysconfig
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from email.utils import parseaddr
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path
from types import ModuleType

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

DESCRIPTION = "Write the third-party licence notices for a PyInstaller binary."


def _load_bundle_contents() -> ModuleType:
    """Load the sibling module by path.

    `scripts/` is not a package, so a plain `import bundle_contents` resolves
    only when Python happens to put this file's directory on `sys.path` --
    true when run as a script, false when the tests load this module by file
    path, and invisible to a type checker either way.
    """
    # One authoritative module object. `setdefault` used to keep an already
    # registered module while this function executed and returned a DIFFERENT
    # one, so a reload or a monkeypatch reached the registered object while
    # the generator went on using its own copy -- measured: with a sentinel
    # installed, `sys.modules['bundle_contents'] is <returned>` was False and
    # the two carried different attributes (Greptile, #2110). The tests load
    # this module by path and register it themselves, which is exactly the
    # condition that splits them.
    existing = sys.modules.get("bundle_contents")
    if existing is not None:
        if not hasattr(existing, "bundled_components") or not hasattr(
            existing, "native_libraries"
        ):
            # Something registered a different module under this name. Say so
            # rather than failing later with an AttributeError on a name that
            # gives no hint where the wrong module came from.
            # Message inlined: this runs at module level, ABOVE the
            # constants block, so a name defined there is not yet bound and
            # the raise would be a NameError instead (measured).
            raise ImportError(
                f"sys.modules['bundle_contents'] is {existing!r}, which lacks "
                "`bundled_components` or `native_libraries`; something "
                "registered a different module under that name"
            )
        return existing

    path = Path(__file__).resolve().parent / "bundle_contents.py"
    spec = importlib.util.spec_from_file_location("bundle_contents", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bundle_contents"] = module
    spec.loader.exec_module(module)
    return module


_bundle_contents = _load_bundle_contents()
bundled_components = _bundle_contents.bundled_components
native_libraries = _bundle_contents.native_libraries

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
# A wheel that ships its licences as package data may carry several, one per
# vendored sub-component under a different licence (pywin32 ships BSD-3-Clause
# for win32/pythonwin/win32com and LGPL-2.1 for the vendored adodbapi). Naming
# the file each text came from keeps the reader able to tell which component a
# licence governs.
COMPONENT_NOTE = "(Licence shipped at {path}.)"
COPYRIGHT_LINE = "Copyright (c) {holder}"
METADATA_AUTHOR = "Author"
METADATA_AUTHOR_EMAIL = "Author-email"
METADATA_MAINTAINER = "Maintainer"
METADATA_MAINTAINER_EMAIL = "Maintainer-email"
METADATA_NAME = "Name"

MISSING_TEXT_ERROR = (
    "{count} package(s) ship no licence text and have no SPDX template: {names}"
)
UNREADABLE_BINARY_WARNING = (
    "warning: {path} could not be read as a PyInstaller archive; licences "
    "will not be filtered to the bundle's contents"
)
UNREADABLE_LICENSE_ERROR = (
    "{name} installs a licence at {path} that could not be read; the notice "
    "would be missing a licence the binary must reproduce"
)

HEADER = """\
THIRD-PARTY SOFTWARE NOTICES

{root} {version} is distributed under the MIT License. The standalone
binary bundles the following third-party packages, each of which is the
property of its respective copyright holders and is distributed under the
licence reproduced below.

{count} packages.
"""
LICENSE_WITH_SOURCES = "{license} (text as shipped in {sources})"
SEPARATOR = "-" * 78
ENTRY = """\
{separator}
{name} {version}
License: {license}
{separator}

{text}
"""
ENCODING = "utf-8"

# The binary also carries what no wheel owns: the Python interpreter and the
# shared libraries PyInstaller copies in beside it (OpenSSL, libffi, zlib, the
# C runtime, ...). `runtime_closure` walks wheels only, so these would ship
# with no notice at all. Each entry names the files it covers, keyed by the
# release binaries' actual contents, and the upstream licence text kept under
# `license_texts/native/`.
NATIVE_TEXTS_DIR = LICENSE_TEXTS_DIR / "native"
CPYTHON_COMPONENT = "CPython"
CPYTHON_LICENSE = "PSF-2.0"
CPYTHON_LICENSE_FILE = "LICENSE.txt"
BUNDLED_AS = "(bundled as {files})"


@dataclass(frozen=True)
class NativeComponent:
    name: str
    license: str
    patterns: tuple[str, ...]
    text_file: str | None

    def matches(self, filename: str) -> bool:
        return any(re.search(p, filename, re.IGNORECASE) for p in self.patterns)


NATIVE_COMPONENTS: tuple[NativeComponent, ...] = (
    # The interpreter's text is read from the interpreter itself, so it is the
    # exact licence of the build that was bundled rather than a stored copy.
    NativeComponent(
        CPYTHON_COMPONENT, CPYTHON_LICENSE, (r"^libpython3", r"^python3\d*\.dll$"), None
    ),
    NativeComponent(
        "OpenSSL", "Apache-2.0", (r"^libssl[.-]", r"^libcrypto[.-]"), "OpenSSL.txt"
    ),
    NativeComponent("libffi", "MIT", (r"^libffi[.-]",), "libffi.txt"),
    NativeComponent("Expat", "MIT", (r"^libexpat[.-]",), "expat.txt"),
    NativeComponent("zlib", "Zlib", (r"^libz\.", r"^zlib1?\.dll$"), "zlib.txt"),
    NativeComponent("bzip2", "bzip2-1.0.6", (r"^libbz2[.-]",), "bzip2.txt"),
    # 0BSD from XZ Utils 5.6; earlier releases put liblzma in the public
    # domain, which asks for nothing, so the stricter text covers both.
    NativeComponent("liblzma (XZ Utils)", "0BSD", (r"^liblzma[.-]",), "liblzma.txt"),
    NativeComponent(
        "ncurses",
        "X11-distribute-modifications-variant",
        (r"^lib(tinfo|ncurses|panel|form|menu)w?[.-]",),
        "ncurses.txt",
    ),
    NativeComponent(
        "libuuid (util-linux)", "BSD-3-Clause", (r"^libuuid[.-]",), "libuuid.txt"
    ),
    NativeComponent(
        "GCC runtime libraries",
        "GPL-3.0-or-later WITH GCC-exception-3.1",
        (r"^libgcc_s[.-]", r"^libstdc\+\+[.-]"),
        "gcc-runtime.txt",
    ),
    NativeComponent(
        "Microsoft Visual C++ runtime and Universal CRT",
        "LicenseRef-Microsoft-Distributable-Code",
        (r"^vcruntime140", r"^msvcp140", r"^ucrtbase\.dll$", r"^api-ms-win-"),
        "microsoft-runtime.txt",
    ),
)

# Copyleft libraries with no linking exception. Bundling one inside the
# one-file executable would put the whole binary under its terms, so the
# notice step fails the release instead of documenting it. GNU Readline is
# the measured case: the Linux binary shipped `libreadline.so.8` because the
# interpreter's `readline` module was collected.
FORBIDDEN_NATIVE: dict[str, str] = {
    r"^libreadline[.-]": "GNU Readline (GPL-3.0-or-later)",
    r"^libgdbm": "GNU dbm (GPL-3.0-or-later)",
}

FORBIDDEN_NATIVE_ERROR = (
    "the binary bundles {files}, which is {component}; copyleft without a "
    "linking exception cannot ship inside an MIT-licensed executable -- exclude "
    "the module that pulls it in (PYINSTALLER_EXCLUDED_MODULES)"
)
UNKNOWN_NATIVE_ERROR = (
    "the binary bundles native libraries with no licence entry: {files}; add "
    "them to NATIVE_COMPONENTS with their upstream licence text"
)
MISSING_CPYTHON_LICENSE_ERROR = (
    "no CPython {file} found under {paths}; the interpreter is bundled in every "
    "binary and its licence must be reproduced"
)


class UnreadableLicenseError(RuntimeError):
    """A licence file resolved but could not be read."""


class NativeLicenseError(RuntimeError):
    """A bundled native library is forbidden or has no licence entry."""


@dataclass(frozen=True)
class Notice:
    name: str
    version: str
    license: str
    texts: tuple[str, ...]
    # Where the text came from, when that is not the distribution's own
    # metadata. Names files; deliberately does NOT name licences. Deriving a
    # licence from its text relabelled PSF as GPL in testing, because licence
    # texts quote other licences -- the PSF text contains both
    # "GNU GENERAL PUBLIC LICENSE" and a 0BSD grant, neither of which applies.
    sources: tuple[str, ...] = ()

    def license_line(self) -> str:
        """The `License:` value, qualified when the text is not the declared one.

        A declared string can be flatly wrong: pywin32 declares PSF and ships
        BSD-3-Clause, so a reader sees a header asserting one licence over the
        body of another. Naming the source file lets the reader resolve the
        contradiction without the generator having to adjudicate it.
        """
        if not self.sources:
            return self.license
        return LICENSE_WITH_SOURCES.format(
            license=self.license, sources=", ".join(self.sources)
        )


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


def _component_of(path: str) -> str:
    """The top-level directory a package-data licence governs."""
    return path.replace("\\", "/").split("/")[0].lower()


def _license_paths(
    dist: Distribution, bundled: frozenset[str] = frozenset()
) -> list[str]:
    """Paths (relative to the install root) of the distribution's licence files.

    `bundled` names the components the binary actually carries; empty means
    unknown and disables filtering entirely.
    """
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

    # A distribution may declare LICENSE while leaving a separate NOTICE
    # undeclared. Scan conventional names even when declared paths resolved.
    conventional = sorted(
        path
        for path in installed
        if path.startswith(f"{dist_info}/")
        and any(hint in Path(path).name.upper() for hint in LICENSE_FILE_HINTS)
    )
    resolved = found + [path for path in conventional if path not in found]
    if resolved:
        return resolved

    # Some wheels install their licence as package data rather than metadata
    # (pywin32 ships `win32/license.txt` and declares no `License-File`), so
    # fall back to conventional names anywhere the distribution installs.
    # Only reached when the `.dist-info` scan found nothing, so a wheel with
    # proper metadata never picks up a vendored dependency's licence.
    package_data = sorted(
        path
        for path in installed
        if not path.startswith(f"{dist_info}/")
        and Path(path).name.upper().removesuffix(".TXT") in LICENSE_FILE_HINTS
    )
    # Each of these governs the component it sits in, not the distribution, so
    # one the build excluded is a licence the binary does not owe. pywin32
    # vendors an LGPL-2.1 `adodbapi` that nothing imports: reproducing its text
    # asserts copyleft terms over a binary carrying no copyleft code.
    kept = [path for path in package_data if _component_of(path) in bundled]
    # Falling back to the full set covers BOTH ways the filter can come up
    # empty, which is why no separate `if not bundled` guard is needed:
    #
    #   unknown bundle -- an unreadable binary yields an empty set, nothing
    #     matches, and dropping every licence is far worse than keeping some
    #     the build excluded;
    #   no matching component -- the distribution is in the runtime closure,
    #     so it owes something; a package shipping only data files would
    #     otherwise lose every licence it has.
    #
    # An earlier version short-circuited the first case explicitly. Deleting
    # that branch changed no behaviour under mutation, because an empty set
    # matches nothing and lands here anyway -- so it was dead code asserting a
    # guarantee this line already makes.
    return kept or package_data


def _read_installed(dist: Distribution, path: str) -> str | None:
    """Read an installed file named relative to the install root."""
    dist_info = _dist_info_dir(dist)
    # `read_text` suppresses `FileNotFoundError` and `PermissionError` but not
    # a decode failure, so both branches share one guard: an unreadable file
    # must reach the caller's refusal rather than abort the build.
    try:
        if dist_info is not None and path.startswith(f"{dist_info}/"):
            return dist.read_text(path.split("/", 1)[1])
        located = Path(str(dist.locate_file(path)))
        return located.read_text(encoding=ENCODING)
    except (OSError, UnicodeDecodeError):
        return None


def _license_texts(
    dist: Distribution, bundled: frozenset[str] = frozenset()
) -> tuple[str, ...]:
    texts: list[str] = []
    dist_info = _dist_info_dir(dist)
    paths = _license_paths(dist, bundled)
    for path in paths:
        # `read_text` resolves against the `.dist-info` directory, so a
        # licence installed as package data is reached through the install
        # root instead.
        text = _read_installed(dist, path)
        if not (text and text.strip()):
            # A path resolved but could not be read. Dropping it would ship a
            # notice missing a licence the binary is obliged to reproduce, and
            # the refusal in `main` only fires when NO text was found at all,
            # so refuse here instead of continuing.
            raise UnreadableLicenseError(
                UNREADABLE_LICENSE_ERROR.format(
                    name=dist.metadata[METADATA_NAME], path=path
                )
            )
        body = text.strip()
        # Only package-data licences need provenance, and only when the
        # distribution ships more than one: a single file governs the whole
        # package and naming it adds nothing.
        outside = dist_info is None or not path.startswith(f"{dist_info}/")
        if outside and len(paths) > 1:
            body = f"{COMPONENT_NOTE.format(path=path)}\n\n{body}"
        texts.append(body)
    if texts:
        return tuple(texts)

    legacy = dist.metadata.get(METADATA_LICENSE)
    if legacy and "\n" in legacy.strip():
        return (legacy.strip(),)
    return ()


def _package_data_sources(
    dist: Distribution, bundled: frozenset[str] = frozenset()
) -> tuple[str, ...]:
    """Package-data licence paths whose text the notice reproduces.

    Provenance for the header. Only paths OUTSIDE `.dist-info` qualify: a
    `.dist-info` licence is the distribution's own declared licence, so naming
    it tells the reader nothing they did not already read on the line above.

    Added rather than folded into `_license_texts`, whose single-tuple return
    is pinned by fourteen existing assertions; widening it would rewrite all
    of them to say the same thing.
    """
    dist_info = _dist_info_dir(dist)
    return tuple(
        path
        for path in _license_paths(dist, bundled)
        if dist_info is None or not path.startswith(f"{dist_info}/")
    )


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


def _notice(dist: Distribution, bundled: frozenset[str] = frozenset()) -> Notice:
    expression = _license_expression(dist)
    texts = _license_texts(dist, bundled)
    sources = _package_data_sources(dist, bundled) if texts else ()
    if not texts:
        template = _template_text(dist, expression)
        if template is not None:
            # A template IS the declared licence's canonical text, so there is
            # no discrepancy for the header to flag.
            texts = (template,)
    return Notice(
        name=dist.metadata[METADATA_NAME],
        version=dist.version,
        license=expression,
        texts=texts,
        sources=sources,
    )


def collect_notices(
    dists: Iterable[Distribution], bundled: frozenset[str] = frozenset()
) -> list[Notice]:
    return [_notice(dist, bundled) for dist in dists]


def _cpython_license_text() -> str:
    """The running interpreter's own `LICENSE.txt`.

    The generator runs in the virtualenv the binary was frozen from, so its
    base interpreter IS the bundled one. POSIX builds keep the file in the
    stdlib directory, Windows builds at the install root; the Windows copy
    also carries the licences of what that build links in.
    """
    candidates = [
        Path(sysconfig.get_path("stdlib")) / CPYTHON_LICENSE_FILE,
        Path(sys.base_prefix) / CPYTHON_LICENSE_FILE,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding=ENCODING).strip()
    raise NativeLicenseError(
        MISSING_CPYTHON_LICENSE_ERROR.format(
            file=CPYTHON_LICENSE_FILE,
            paths=", ".join(str(c.parent) for c in candidates),
        )
    )


def _native_text(component: NativeComponent) -> str:
    if component.text_file is None:
        return _cpython_license_text()
    return (NATIVE_TEXTS_DIR / component.text_file).read_text(encoding=ENCODING).strip()


def native_notices(libraries: frozenset[str] | None) -> list[Notice]:
    """Notices for the interpreter and the native libraries bundled with it.

    `libraries` is `None` when the binary was not given or could not be read.
    CPython is in every binary regardless, so its notice is always produced;
    the rest need the binary's actual contents.
    """
    found = sorted(libraries or ())
    forbidden: dict[str, list[str]] = {}
    for filename in found:
        for pattern, component in FORBIDDEN_NATIVE.items():
            if re.search(pattern, filename, re.IGNORECASE):
                forbidden.setdefault(component, []).append(filename)
    if forbidden:
        raise NativeLicenseError(
            "\n".join(
                FORBIDDEN_NATIVE_ERROR.format(files=", ".join(files), component=name)
                for name, files in forbidden.items()
            )
        )

    by_component: dict[str, list[str]] = {}
    unknown = []
    for filename in found:
        component = next((c for c in NATIVE_COMPONENTS if c.matches(filename)), None)
        if component is None:
            unknown.append(filename)
        else:
            by_component.setdefault(component.name, []).append(filename)
    if unknown:
        raise NativeLicenseError(UNKNOWN_NATIVE_ERROR.format(files=", ".join(unknown)))

    notices = []
    for component in NATIVE_COMPONENTS:
        files = by_component.get(component.name)
        if component.name == CPYTHON_COMPONENT:
            version = platform.python_version()
            if files:
                version = f"{version} {BUNDLED_AS.format(files=', '.join(files))}"
        elif files:
            version = BUNDLED_AS.format(files=", ".join(files))
        else:
            continue
        notices.append(
            Notice(
                name=component.name,
                version=version,
                license=component.license,
                texts=(_native_text(component),),
            )
        )
    return notices


def render(notices: list[Notice], root: str = ROOT_DISTRIBUTION) -> str:
    root_version = distribution(root).version
    parts = [HEADER.format(root=root, version=root_version, count=len(notices))]
    for notice in sorted(notices, key=lambda n: n.name.lower()):
        parts.append(
            ENTRY.format(
                separator=SEPARATOR,
                name=notice.name,
                version=notice.version,
                license=notice.license_line(),
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
    parser.add_argument(
        "--binary",
        type=Path,
        default=None,
        help=(
            "the built one-file binary; when given, a package's package-data "
            "licences are filtered to the components it actually bundles"
        ),
    )
    args = parser.parse_args(argv)

    bundled: frozenset[str] = frozenset()
    libraries: frozenset[str] | None = None
    if args.binary is not None:
        bundled = bundled_components(args.binary)
        libraries = native_libraries(args.binary)
        if not bundled:
            # Unreadable is not empty. Say so, because the notice silently
            # reverts to the unfiltered wheel contents.
            print(UNREADABLE_BINARY_WARNING.format(path=args.binary), file=sys.stderr)  # noqa: T201

    try:
        notices = collect_notices(runtime_closure().values(), bundled)
        notices += native_notices(libraries)
    except (UnreadableLicenseError, NativeLicenseError) as error:
        print(error, file=sys.stderr)  # noqa: T201
        return 1

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
