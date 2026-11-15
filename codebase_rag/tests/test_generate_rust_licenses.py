# rusqlite 0.41 pulls in rsqlite-vfs, published from the sqlite-wasm-rs
# workspace without the MIT file the repository keeps at its root, so the
# licence list could not be generated. Its text is taken from a crate of the
# same repository under the same licence, and from nowhere else.

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import generate_rust_licenses as grl

_REPO = "https://github.com/Spxg/sqlite-wasm-rs"
_MIT = "MIT License\n\nCopyright (c) 2024 Spxg"


def _package(root: Path, name: str, licence_file: str | None) -> Path:
    directory = root / name
    directory.mkdir()
    if licence_file is not None:
        (directory / licence_file).write_text(_MIT, encoding=grl.ENCODING)
    return directory


def _text(
    crate: grl.Crate, crates: list[grl.Crate], found: dict[tuple[str, str], grl.Source]
) -> str:
    licence = grl.chosen_licence(crate)
    return grl.licence_text(
        crate, licence, grl.text_directories(crate, licence, crates, found)
    )


def test_a_crate_without_its_file_takes_its_repositorys_text(tmp_path: Path) -> None:
    vfs = grl.Crate("rsqlite-vfs", "0.2.0", "MIT")
    wasm = grl.Crate("sqlite-wasm-rs", "0.6.2", "MIT")
    found = {
        ("rsqlite-vfs", "0.2.0"): grl.Source(_package(tmp_path, "vfs", None), _REPO),
        ("sqlite-wasm-rs", "0.6.2"): grl.Source(
            _package(tmp_path, "wasm", "LICENSE"), _REPO
        ),
    }

    assert _text(vfs, [vfs, wasm], found) == _MIT


def test_a_crate_with_its_own_file_keeps_it(tmp_path: Path) -> None:
    own = _package(tmp_path, "own", "LICENSE-MIT")
    (own / "LICENSE-MIT").write_text("own text", encoding=grl.ENCODING)
    crate = grl.Crate("a", "1.0.0", "MIT OR Apache-2.0")
    sibling = grl.Crate("b", "1.0.0", "MIT")
    found = {
        ("a", "1.0.0"): grl.Source(own, _REPO),
        ("b", "1.0.0"): grl.Source(_package(tmp_path, "b", "LICENSE"), _REPO),
    }

    assert _text(crate, [crate, sibling], found) == "own text"


@pytest.mark.parametrize(
    ("sibling_licence", "sibling_repository"),
    [
        pytest.param("MIT", "https://github.com/someone/else", id="other-repository"),
        pytest.param("Apache-2.0", _REPO, id="other-licence"),
    ],
)
def test_no_text_is_borrowed_from_an_unrelated_crate(
    tmp_path: Path, sibling_licence: str, sibling_repository: str
) -> None:
    crate = grl.Crate("rsqlite-vfs", "0.2.0", "MIT")
    sibling = grl.Crate("other", "1.0.0", sibling_licence)
    found = {
        ("rsqlite-vfs", "0.2.0"): grl.Source(_package(tmp_path, "vfs", None), _REPO),
        ("other", "1.0.0"): grl.Source(
            _package(tmp_path, "other", "LICENSE"), sibling_repository
        ),
    }

    with pytest.raises(SystemExit, match="rsqlite-vfs 0.2.0: no MIT file"):
        _text(crate, [crate, sibling], found)


def test_a_crate_without_a_repository_looks_only_in_its_own_package(
    tmp_path: Path,
) -> None:
    crate = grl.Crate("lonely", "1.0.0", "MIT")
    sibling = grl.Crate("other", "1.0.0", "MIT")
    found = {
        ("lonely", "1.0.0"): grl.Source(_package(tmp_path, "lonely", None), None),
        ("other", "1.0.0"): grl.Source(_package(tmp_path, "other", "LICENSE"), None),
    }

    with pytest.raises(SystemExit, match="lonely 1.0.0: no MIT file"):
        _text(crate, [crate, sibling], found)
