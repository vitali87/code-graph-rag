#!/usr/bin/env python3
"""Read what a PyInstaller one-file binary actually bundles.

The notices generator derives its package list from the *installed wheel*,
which over-reports: a wheel may ship components the build excludes, and the
notice then reproduces licences the binary does not carry. `pywin32` is the
measured case -- the wheel vendors an LGPL-2.1 `adodbapi`, nothing imports it,
PyInstaller leaves it out, and the notice claimed copyleft terms anyway.

A one-file binary holds TWO archives, and reading either alone gives a wrong
answer:

* `CArchiveReader(exe).toc` lists extension modules (`.pyd`/`.so`), data files
  and `.dist-info` directories.
* `PYZ.pyz`, a sub-archive read with `ZlibArchiveReader`, holds every
  pure-Python module.

Measured on the CI-built Windows binary (2026-09-21): 1,551 TOC entries and
6,821 PYZ modules. A TOC-only scan reports 86 of 136 shipped packages absent,
including `anyio`, `anthropic`, `cachetools` and `cffi` -- all certainly
present. Deriving notices from the TOC alone would drop licences the binary
genuinely owes, which is a far worse defect than the over-inclusion it fixes.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path


def _top_level(entry: str, *, dotted: bool) -> set[str]:
    """The component name(s) an archive entry can be matched by.

    The two archives name things differently, and a dot means opposite things
    in each, so they cannot be reduced the same way:

    * TOC entries are PATHS (`win32\\win32api.pyd`, `base_library/LICENSE`).
      A dot is a file extension, so the component is the first path segment
      kept WHOLE -- `ruamel.yaml/LICENSE` belongs to `ruamel.yaml`.
    * PYZ entries are DOTTED MODULE NAMES (`anyio.abc`, `ruamel.yaml.main`).
      A dot is a package separator, and nothing in the name says where the
      DISTRIBUTION boundary falls: `ruamel.yaml` ships its licence under
      `ruamel.yaml/`, while `anyio` ships under `anyio/`.

    So a PYZ module yields every dotted prefix (`ruamel`, `ruamel.yaml`) and
    the licence directory matches whichever one it is named for. Reducing to
    the first segment instead drops `ruamel.yaml/LICENSE`, a licence the
    binary carries -- the licence-losing direction.
    """
    head = entry.replace("\\", "/").split("/")[0].lower()
    if not dotted:
        return {head}
    parts = head.split(".")
    return {".".join(parts[: i + 1]) for i in range(len(parts))}


# PyInstaller's typecode for the embedded PYZ archive (`building/api.py`
# maps 'PYZ' -> 'z'), and what the bootloader itself matches on.
PYZ_TYPECODE = "z"
# Fallback only. The build OVERRIDES the embedded name to `PYZ.pyz` whatever
# the intermediate was called ("Override PYZ name in the PKG archive into
# PYZ.pyz, regardless of what the original name was", api.py:345), so the
# on-disk `PYZ-00.pyz` never reaches the TOC. Verified on the Windows, macOS
# and Linux CI binaries: each has exactly one entry, named `PYZ.pyz`.
PYZ_FALLBACK_PATTERN = re.compile(r"^pyz(-\d+)?\.pyz$")


def _pyz_entry_names(outer: object) -> list[str]:
    """TOC entries holding the PYZ sub-archive, typecode first.

    Matching the NAME alone is fragile: it is a build detail, whereas the
    typecode is the contract the bootloader relies on (Copilot, #2110). The
    reader exposes the TOC as a dict of `name -> (..., typecode)` here, but
    that shape is not guaranteed across versions, so the name check remains as
    a fallback rather than the primary test.
    """
    toc = getattr(outer, "toc", None)
    by_typecode: list[str] = []
    if isinstance(toc, dict):
        for name, entry in toc.items():
            if isinstance(entry, tuple) and entry and entry[-1] == PYZ_TYPECODE:
                by_typecode.append(name)
    if by_typecode:
        return by_typecode
    return [
        name
        for name in (toc or ())
        if PYZ_FALLBACK_PATTERN.match(Path(str(name).replace("\\", "/")).name.lower())
    ]


# A shared library at the archive root that no wheel owns: the interpreter
# and the system libraries PyInstaller copied in beside it. Python extension
# modules also end in `.so`/`.pyd` but carry an ABI tag, and belong to a
# distribution whose own licence the notice already reproduces; libraries a
# wheel vendors sit under that wheel's directory, never at the root.
NATIVE_LIBRARY_PATTERN = re.compile(r"(\.so(\.\d+)*|\.dylib|\.dll)$", re.IGNORECASE)
EXTENSION_MODULE_PATTERN = re.compile(r"\.(cpython-\d+|abi3|cp\d+)[.-]", re.IGNORECASE)


def native_libraries(binary: Path) -> frozenset[str] | None:
    """Root-level shared libraries in the binary, or `None` when unreadable.

    `None` and an empty set mean different things, as in
    `bundled_components`: a binary that bundles no native libraries is
    possible, whereas an unreadable one says nothing about what it carries.
    """
    try:
        from PyInstaller.archive.readers import CArchiveReader

        entries = list(CArchiveReader(str(binary)).toc)
    except Exception:  # noqa: BLE001 - any unreadable binary means "unknown"
        return None
    return frozenset(
        entry
        for entry in entries
        if "/" not in entry.replace("\\", "/")
        and NATIVE_LIBRARY_PATTERN.search(entry)
        and not EXTENSION_MODULE_PATTERN.search(entry)
    )


def bundled_components(binary: Path) -> frozenset[str]:
    """Top-level component names present in the binary, from BOTH archives.

    Returns an empty set when the binary cannot be read as a PyInstaller
    archive. Callers must treat empty as "unknown", never as "nothing is
    bundled": a filter that removed every licence on an unreadable binary
    would silently ship a notices file with no licences in it.
    """
    try:
        return _read_archives(binary)
    except Exception:  # noqa: BLE001 - any unreadable binary means "unknown"
        # Deliberately covers the PYZ read too. A binary whose TOC parses but
        # whose PYZ does not is a BROKEN INSTRUMENT, and the TOC alone reports
        # every pure-Python package absent -- so degrading to it would filter
        # out licences the binary genuinely owes, which is the one outcome
        # this module exists to prevent. "Unknown" disables filtering and the
        # caller warns; a partial answer would be silently wrong.
        return frozenset()


def _read_archives(binary: Path) -> frozenset[str]:
    """Both archives, or raise. See `bundled_components` for the policy."""
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    outer = CArchiveReader(str(binary))
    entries = list(outer.toc)
    components: set[str] = set()
    for entry in entries:
        components |= _top_level(entry, dotted=False)

    # Pure-Python modules live only in the PYZ sub-archive. Its absence is not
    # an error (a binary may legitimately have none), so the TOC result stands.
    payload = None
    for name in _pyz_entry_names(outer):
        extracted = outer.extract(name)
        # `extract` returns bytes on some PyInstaller versions and
        # `(flag, bytes)` on others; take the payload either way.
        payload = extracted[1] if isinstance(extracted, tuple) else extracted
        break
    if payload is None:
        return frozenset(components)

    with tempfile.TemporaryDirectory() as tmp:
        pyz_path = Path(tmp) / "PYZ.pyz"
        pyz_path.write_bytes(payload)
        # `str`, not the `Path`. `ZlibArchiveReader` parses a `?offset` suffix
        # off the name with `filename.rfind('?')`, so a `Path` raises
        # `AttributeError` before the file is ever opened. That failure used
        # to be swallowed below, leaving a TOC-only answer that reported every
        # pure-Python package absent -- 96 components instead of 109 on the
        # macOS binary, with `anyio` and `click` among the missing.
        for module in ZlibArchiveReader(str(pyz_path)).toc:
            components |= _top_level(module, dotted=True)
    return frozenset(components)
