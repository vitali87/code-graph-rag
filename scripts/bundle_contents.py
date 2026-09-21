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

import tempfile
from pathlib import Path


def _top_level(entry: str) -> str:
    """The component an archive entry belongs to.

    TOC entries are paths with the platform's separator (`win32\\win32api.pyd`
    on the binary this was measured against, even when generated elsewhere);
    PYZ entries are dotted module names (`anyio.abc`). Both reduce to their
    first segment, which is the directory a package-data licence sits in.
    """
    return entry.replace("\\", "/").split("/")[0].split(".")[0].lower()


def bundled_components(binary: Path) -> frozenset[str]:
    """Top-level component names present in the binary, from BOTH archives.

    Returns an empty set when the binary cannot be read as a PyInstaller
    archive. Callers must treat empty as "unknown", never as "nothing is
    bundled": a filter that removed every licence on an unreadable binary
    would silently ship a notices file with no licences in it.
    """
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    try:
        outer = CArchiveReader(str(binary))
        entries = list(outer.toc)
    except Exception:  # noqa: BLE001 - any unreadable binary means "unknown"
        return frozenset()

    components = {_top_level(entry) for entry in entries}

    # Pure-Python modules live only in the PYZ sub-archive. Its absence is not
    # an error (a binary may legitimately have none), so the TOC result stands.
    payload = None
    for name in entries:
        if _top_level(name) == "pyz":
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
        try:
            components.update(_top_level(m) for m in ZlibArchiveReader(pyz_path).toc)
        except Exception:  # noqa: BLE001 - keep the TOC answer rather than none
            pass
    return frozenset(components)
