"""`scripts/generate_rust_licenses.py` writes the licence file the
cgr-graph-rs wheel ships, and CI's `--check` fails when it falls behind
`Cargo.lock`. Cargo is replaced by recorded `cargo tree` / `cargo metadata`
answers here, so what is pinned is the script's own reading of them: which
crates count, which licence each is used under, and the file it renders."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import generate_rust_licenses as gen

_TREE = "\n".join(
    [
        "cgr_graph_rs v0.9.0 (/src/rust/cgr_graph_rs)|MIT",
        "memchr v2.7.4|Unlicense OR MIT",
        "libsqlite3-sys v0.30.1|MIT",
        "adler2 v2.0.0|0BSD OR MIT OR Apache-2.0",
        "memchr v2.7.4|Unlicense OR MIT (*)",
        "zlib-rs v0.5.0|Zlib",
    ]
)


def _crate_dir(root: Path, name: str, files: dict[str, str]) -> Path:
    path = root / name
    path.mkdir()
    for file_name, text in files.items():
        (path / file_name).write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def cargo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Answer `cargo tree` and `cargo metadata` from crates under tmp_path."""
    registry = tmp_path / "registry"
    registry.mkdir()
    sources = {
        ("memchr", "2.7.4"): _crate_dir(
            registry, "memchr", {"LICENSE-MIT": "memchr MIT\n", "UNLICENSE": "u"}
        ),
        ("libsqlite3-sys", "0.30.1"): _crate_dir(
            registry, "libsqlite3-sys", {"LICENSE": "sqlite-sys MIT"}
        ),
        ("adler2", "2.0.0"): _crate_dir(
            registry, "adler2", {"LICENSE-MIT": "adler MIT", "LICENSE": "other"}
        ),
        ("zlib-rs", "0.5.0"): _crate_dir(registry, "zlib-rs", {"LICENSE": "zlib"}),
    }
    metadata = {
        "packages": [
            {"name": n, "version": v, "manifest_path": str(p / "Cargo.toml")}
            for (n, v), p in sources.items()
        ]
    }
    calls: list[tuple[str, ...]] = []

    def run(args: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess:
        assert kwargs["cwd"] == gen.CRATE_DIR
        assert kwargs["check"] is True
        calls.append(args)
        out = _TREE if args[1] == "tree" else json.dumps(metadata)
        return subprocess.CompletedProcess(args, 0, stdout=out)

    monkeypatch.setattr(gen, "subprocess", SimpleNamespace(run=run))
    return calls


def test_only_third_party_linked_crates_count_once_each(
    cargo: list[tuple[str, ...]],
) -> None:
    crates = gen.linked_crates()
    # The crate being built is left out, and a deduplicated repeat is one.
    assert crates == [
        gen.Crate("adler2", "2.0.0", "0BSD OR MIT OR Apache-2.0"),
        gen.Crate("libsqlite3-sys", "0.30.1", "MIT"),
        gen.Crate("memchr", "2.7.4", "Unlicense OR MIT"),
        gen.Crate("zlib-rs", "0.5.0", "Zlib"),
    ]
    # Normal edges only, so build scripts and proc macros ship nothing.
    assert cargo == [gen.TREE_ARGS]
    assert "normal,no-proc-macro" in gen.TREE_ARGS


@pytest.mark.parametrize(
    ("expression", "chosen"),
    [
        ("MIT OR Apache-2.0", "MIT"),
        ("Apache-2.0 OR MIT", "MIT"),
        ("MIT/Apache-2.0", "MIT"),
        ("Apache-2.0 OR Zlib", "Zlib"),
        ("(Apache-2.0)", "Apache-2.0"),
    ],
)
def test_the_shortest_usual_licence_is_chosen(expression: str, chosen: str) -> None:
    assert gen.chosen_licence(gen.Crate("c", "1.0.0", expression)) == chosen


def test_a_crate_with_no_usable_licence_stops_the_build() -> None:
    with pytest.raises(SystemExit, match=r"gpl-only 1\.0\.0: no licence among"):
        gen.chosen_licence(gen.Crate("gpl-only", "1.0.0", "GPL-3.0-only"))


def test_the_licence_text_prefers_the_licence_specific_file(tmp_path: Path) -> None:
    source = _crate_dir(tmp_path, "c", {"LICENSE-MIT": " mit text \n", "LICENSE": "x"})
    crate = gen.Crate("c", "1.0.0", "MIT")
    assert gen.licence_text(crate, "MIT", source) == "mit text"
    with pytest.raises(SystemExit, match="no Apache-2.0 file in"):
        gen.licence_text(crate, "Apache-2.0", _crate_dir(tmp_path, "bare", {}))


def test_render_lists_each_crate_with_its_text_and_sqlite_after_its_crate(
    cargo: list[tuple[str, ...]],
) -> None:
    rendered = gen.render()

    assert rendered.startswith(gen.HEADER)
    sections = re.findall(r"^## (.+)$", rendered, flags=re.MULTILINE)
    assert sections == [
        "adler2 2.0.0",
        "libsqlite3-sys 0.30.1",
        "SQLite (compiled in by libsqlite3-sys)",
        "memchr 2.7.4",
        "zlib-rs 0.5.0",
    ]
    memchr = "Licence: Unlicense OR MIT (used under MIT)\n\n```text\nmemchr MIT\n```"
    assert memchr in rendered
    sqlite = gen.SQLITE_TEXT.read_text(encoding="utf-8").strip()
    assert f"Licence: public domain\n\n```text\n{sqlite}\n```" in rendered


def test_check_fails_on_a_stale_file_and_passes_once_written(
    cargo: list[tuple[str, ...]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "rust" / "THIRD_PARTY_LICENSES.md"
    output.parent.mkdir()
    monkeypatch.setattr(gen, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gen, "OUTPUT", output)

    assert gen.main(["--check"]) == 1
    stale = f"{Path('rust', 'THIRD_PARTY_LICENSES.md')} does not match Cargo.lock"
    assert stale in capsys.readouterr().err
    assert not output.exists()

    assert gen.main([]) == 0
    assert gen.main(["--check"]) == 0
    output.write_text(output.read_text(encoding="utf-8") + "edited\n", "utf-8")
    assert gen.main(["--check"]) == 1


def test_the_shipped_file_uses_each_crate_under_the_licence_chosen_now() -> None:
    """Checked without cargo: each section the committed file holds names the
    licence `chosen_licence` picks today for the expression it declares."""
    shipped = gen.OUTPUT.read_text(encoding="utf-8")
    assert shipped.startswith(gen.HEADER)
    declared = re.findall(
        r"^## (\S+) (\S+)\n\nLicence: (.+) \(used under (\S+)\)$",
        shipped,
        flags=re.MULTILINE,
    )
    assert declared, "no crate sections parsed; the section format changed"
    for name, version, expression, used in declared:
        assert gen.chosen_licence(gen.Crate(name, version, expression)) == used, name
    has_sqlite = any(name == gen.SQLITE_CRATE for name, *_ in declared)
    assert ("## SQLite (compiled in by libsqlite3-sys)" in shipped) == has_sqlite
