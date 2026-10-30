"""The tsconfig/jsconfig `paths` and `baseUrl` a repository declares: JSONC
read leniently, each config's aliases prefixed by its directory and base,
`extends` followed for the base, and an aliased specifier's candidates."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.parsers.js_ts.tsconfig import (
    load_jsonc,
    load_ts_base_urls,
    load_ts_path_aliases,
    parse_tsconfig_aliases,
    ts_alias_candidates,
    ts_alias_target,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_jsonc_comments_and_trailing_commas_are_read(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "tsconfig.json",
        '{\n  // paths\n  "compilerOptions": { /* base */ "baseUrl": ".", },\n}\n',
    )
    assert load_jsonc(cfg) == {"compilerOptions": {"baseUrl": "."}}


@pytest.mark.parametrize("text", ["[1, 2]", "{ not json", ""])
def test_a_config_that_is_no_object_reads_as_none(tmp_path: Path, text: str) -> None:
    assert load_jsonc(_write(tmp_path / "tsconfig.json", text)) is None
    assert load_jsonc(tmp_path / "missing.json") is None


def test_wildcard_and_exact_aliases_take_the_dir_and_base_prefix() -> None:
    data = {
        "compilerOptions": {
            "baseUrl": "src",
            "paths": {
                "@app/*": ["app/*", "fallback/*"],
                "config": ["config/index.ts"],
                "bad/*": ["no-wildcard"],
                "empty": [],
            },
        }
    }
    assert parse_tsconfig_aliases(data, "web/") == [
        ("@app/", "web/src/app/", True),
        ("config", "web/src/config/index.ts", False),
    ]
    assert parse_tsconfig_aliases({"compilerOptions": "x"}, "") == []


def test_an_import_matches_every_alias_it_falls_under() -> None:
    aliases = [("@app/", "src/app/", True), ("@app/core", "src/core", False)]
    assert ts_alias_candidates("@app/core", aliases) == [
        (5, "src/app/core"),
        (9, "src/core"),
    ]
    assert ts_alias_candidates("lodash", aliases) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/app/main.ts", "src/app/main"),
        ("src/./app/../lib.tsx", "src/lib"),
        ("../outside", None),
        ("./", None),
    ],
)
def test_an_alias_target_is_a_normalized_path_inside_the_repo(
    raw: str, expected: str | None
) -> None:
    assert ts_alias_target(raw) == expected


def test_aliases_are_found_in_the_repo_and_its_packages(tmp_path: Path) -> None:
    _write(
        tmp_path / "tsconfig.json",
        '{"compilerOptions": {"paths": {"@root/*": ["lib/*"]}}}',
    )
    _write(
        tmp_path / "packages/web/tsconfig.json",
        '{"compilerOptions": {"baseUrl": "src", "paths": {"~/*": ["*"]}}}',
    )
    # Vendored trees are never searched.
    _write(
        tmp_path / "node_modules/x/tsconfig.json",
        '{"compilerOptions": {"paths": {"vendored/*": ["*"]}}}',
    )
    assert sorted(load_ts_path_aliases(tmp_path)) == [
        ("@root/", "lib/", True),
        ("~/", "packages/web/src/", True),
    ]


def test_a_base_url_is_inherited_through_extends(tmp_path: Path) -> None:
    _write(tmp_path / "base.json", '{"compilerOptions": {"baseUrl": "src"}}')
    _write(tmp_path / "tsconfig.json", '{"extends": "./base.json"}')
    _write(tmp_path / "pkg/tsconfig.json", '{"compilerOptions": {}}')
    assert dict(load_ts_base_urls(tmp_path)) == {"": "src/", "pkg.": None}
