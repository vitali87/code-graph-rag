"""Guards for the third-party notices shipped beside each PyInstaller binary.

The one-file binaries bundle every runtime dependency without the wheels'
licence files, and every permissive licence in the tree conditions
redistribution on keeping its notice. The generator and the build step that
runs it are the only thing producing that notice, so both are pinned here.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from importlib.metadata import PathDistribution
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build-binaries.yml"
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_third_party_notices.py"
BUNDLE_PATH = REPO_ROOT / "scripts" / "bundle_contents.py"

BUILD_JOB_ID = "build"
NOTICES_STEP_NAME = "Generate third-party notices"
BINARY_ARTIFACT_PREFIX = "dist/code-graph-rag-"

DEV_ONLY_DISTRIBUTIONS = ("pytest", "ruff", "pylint", "pyinstaller", "semgrep")
RUNTIME_DISTRIBUTIONS = ("pydantic", "tree-sitter", "loguru", "typer")


@pytest.fixture(scope="module")
def notices() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "generate_third_party_notices", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_third_party_notices"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bundle() -> ModuleType:
    """Load `scripts/bundle_contents.py` by path; `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("bundle_contents", BUNDLE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bundle_contents"] = module
    spec.loader.exec_module(module)
    return module


def _fake_dist(
    root: Path,
    name: str,
    metadata_lines: list[str],
    files: dict[str, str],
    package_files: dict[str, str] | None = None,
) -> PathDistribution:
    """Lay out `<name>-1.0.dist-info` the way an installed wheel does.

    `files` are installed under the `.dist-info` directory; `package_files`
    are installed beside it, the way pywin32 ships `win32/license.txt`.
    """
    dist_info = root / f"{name}-1.0.dist-info"
    dist_info.mkdir()
    metadata = ["Metadata-Version: 2.4", f"Name: {name}", "Version: 1.0"]
    metadata.extend(metadata_lines)
    (dist_info / "METADATA").write_text("\n".join(metadata) + "\n", encoding="utf-8")
    for relative, text in files.items():
        target = dist_info / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for relative, text in (package_files or {}).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    record_entries = [f"{dist_info.name}/METADATA,,", f"{dist_info.name}/RECORD,,"]
    record_entries.extend(f"{dist_info.name}/{relative},," for relative in files)
    record_entries.extend(f"{relative},," for relative in (package_files or {}))
    (dist_info / "RECORD").write_text(
        "\n".join(record_entries) + "\n", encoding="utf-8"
    )
    return PathDistribution(dist_info)


class TestRuntimeClosure:
    def test_excludes_dev_tooling_installed_in_the_same_environment(
        self, notices: ModuleType
    ) -> None:
        closure = notices.runtime_closure()

        leaked = [name for name in DEV_ONLY_DISTRIBUTIONS if name in closure]
        assert not leaked, (
            f"{leaked} are dev-group tools and are not bundled into the binary; "
            "listing them would credit (and, for the GPL ones, misattribute) "
            "code the release does not contain"
        )

    def test_includes_runtime_dependencies(self, notices: ModuleType) -> None:
        closure = notices.runtime_closure()

        assert all(name in closure for name in RUNTIME_DISTRIBUTIONS)
        assert notices.ROOT_DISTRIBUTION not in closure

    def test_extra_gated_requirements_follow_the_requested_extras(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(
            tmp_path,
            "root",
            [
                "Requires-Dist: always",
                'Requires-Dist: optional; extra == "full"',
                'Requires-Dist: never; python_version < "3"',
            ],
            {},
        )

        plain = {r.name for r in notices._active_requirements(dist, frozenset())}
        full = {r.name for r in notices._active_requirements(dist, frozenset({"full"}))}

        assert plain == {"always"}
        assert full == {"always", "optional"}


class TestLicenseDiscovery:
    def test_prefers_pep639_licenses_directory(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        # The declared name carries none of the LICENSE/COPYING/NOTICE hints,
        # so only the License-File lookup under `licenses/` can find it.
        dist = _fake_dist(
            tmp_path,
            "modern",
            ["License-Expression: MIT", "License-File: TERMS.txt"],
            {"licenses/TERMS.txt": "MIT text"},
        )

        assert notices._license_expression(dist) == "MIT"
        assert notices._license_texts(dist) == ("MIT text",)

    def test_falls_back_to_dist_info_root_for_legacy_wheels(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(
            tmp_path,
            "legacy",
            ["License: Apache-2.0", "License-File: COPYING"],
            {"COPYING": "Apache text"},
        )

        assert notices._license_expression(dist) == "Apache-2.0"
        assert notices._license_texts(dist) == ("Apache text",)

    def test_finds_undeclared_licence_file_by_name(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(
            tmp_path,
            "undeclared",
            ["Classifier: License :: OSI Approved :: BSD License"],
            {"LICENSE.txt": "BSD text"},
        )

        assert notices._license_expression(dist) == "BSD License"
        assert notices._license_texts(dist) == ("BSD text",)

    def test_finds_licence_file_installed_outside_dist_info(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """pywin32 ships its licence as package data, not `.dist-info` metadata."""
        dist = _fake_dist(
            tmp_path,
            "packagedata",
            ["License: BSD-3-Clause"],
            {},
            package_files={"packagedata/license.txt": "Shipped BSD text"},
        )

        assert notices._license_texts(dist) == ("Shipped BSD text",)

    def test_package_data_licence_is_preferred_over_a_mislabelled_template(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """pywin32 declares `License: PSF` but ships BSD-3-Clause text.

        Reproducing a template keyed off the declared string would ship the
        wrong licence, so the text the wheel actually installs must win.
        """
        dist = _fake_dist(
            tmp_path,
            "mislabelled",
            ["License: PSF", "Author: Mark Hammond (et al)"],
            {},
            package_files={
                "mislabelled/license.txt": "Redistribution and use ... BSD-3-Clause",
            },
        )

        texts = notices._license_texts(dist)
        assert texts == ("Redistribution and use ... BSD-3-Clause",)
        assert not any(notices.TEMPLATE_NOTE[:20] in text for text in texts)

    def test_multiple_package_data_licences_name_their_component(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """pywin32 ships BSD-3-Clause per component plus LGPL for adodbapi.

        Without provenance the reader cannot tell which licence governs which
        sub-component, so each text names the file it came from.
        """
        dist = _fake_dist(
            tmp_path,
            "multi",
            ["License: PSF"],
            {},
            package_files={
                "vendored/license.txt": "LGPL text",
                "multi/license.txt": "BSD text",
            },
        )

        texts = notices._license_texts(dist)
        assert len(texts) == 2
        assert "multi/license.txt" in "\n".join(texts)
        assert "vendored/license.txt" in "\n".join(texts)
        assert "LGPL text" in "\n".join(texts)
        assert "BSD text" in "\n".join(texts)

    def test_single_package_data_licence_needs_no_provenance(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """One file governs the whole package, so naming it adds nothing."""
        dist = _fake_dist(
            tmp_path,
            "solo",
            ["License: MIT"],
            {},
            package_files={"solo/LICENSE": "only text"},
        )

        assert notices._license_texts(dist) == ("only text",)

    def test_package_data_scan_ignores_unrelated_files(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """Only conventional licence names are collected, not every shipped file."""
        dist = _fake_dist(
            tmp_path,
            "noisy",
            ["License: MIT"],
            {},
            package_files={
                "noisy/__init__.py": "raise SystemExit",
                "noisy/README.md": "not a licence",
                # A real shape: `identify/vendor/licenses.py` exists in this
                # venv. A substring rule would collect it as licence text.
                "noisy/vendor/licenses.py": "SOURCE CODE, NOT A LICENCE",
                "noisy/LICENSE": "the real text",
            },
        )

        assert notices._license_texts(dist) == ("the real text",)

    def test_undecodable_package_data_licence_refuses_rather_than_crashes(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """An unreadable licence must reach the refusal, not abort the build."""
        dist = _fake_dist(tmp_path, "binary", ["License: MIT"], {})
        target = tmp_path / "binary" / "LICENSE"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"Copyright \xa9 2020 Some\xff Holder")
        (tmp_path / "binary-1.0.dist-info" / "RECORD").write_text(
            "binary-1.0.dist-info/METADATA,,\n"
            "binary-1.0.dist-info/RECORD,,\n"
            "binary/LICENSE,,\n",
            encoding="utf-8",
        )

        with pytest.raises(notices.UnreadableLicenseError) as excinfo:
            notices._license_texts(dist)
        assert "binary/LICENSE" in str(excinfo.value)

    def test_undecodable_dist_info_licence_refuses_rather_than_crashing(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """`read_text` suppresses missing/permission errors but not decode ones.

        An escaping `UnicodeDecodeError` would abort the release build with a
        traceback, bypassing the refusal path entirely.
        """
        dist = _fake_dist(tmp_path, "badmeta", ["License: MIT"], {})
        dist_info = tmp_path / "badmeta-1.0.dist-info"
        (dist_info / "LICENSE").write_bytes(b"Copyright \xff\xfe bad")
        (dist_info / "RECORD").write_text(
            "badmeta-1.0.dist-info/METADATA,,\n"
            "badmeta-1.0.dist-info/RECORD,,\n"
            "badmeta-1.0.dist-info/LICENSE,,\n",
            encoding="utf-8",
        )

        with pytest.raises(notices.UnreadableLicenseError) as excinfo:
            notices._license_texts(dist)
        assert "badmeta-1.0.dist-info/LICENSE" in str(excinfo.value)

    def test_partial_read_failure_refuses_rather_than_dropping_a_licence(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """One unreadable licence among several must not be silently dropped.

        Dropping it leaves `texts` truthy, so the refusal in `main` never
        fires and the binary ships missing a licence it must reproduce.
        """
        dist = _fake_dist(tmp_path, "partial", ["License: MIT"], {})
        for sub, data in (("a", b"\xff\xfe bad \xff"), ("b", b"good text")):
            target = tmp_path / sub / "LICENSE"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (tmp_path / "partial-1.0.dist-info" / "RECORD").write_text(
            "partial-1.0.dist-info/METADATA,,\n"
            "partial-1.0.dist-info/RECORD,,\n"
            "a/LICENSE,,\n"
            "b/LICENSE,,\n",
            encoding="utf-8",
        )

        with pytest.raises(notices.UnreadableLicenseError) as excinfo:
            notices._license_texts(dist)
        assert "a/LICENSE" in str(excinfo.value)

    def test_includes_undeclared_notice_with_declared_license(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(
            tmp_path,
            "notice",
            ["License-Expression: Apache-2.0", "License-File: LICENSE"],
            {
                "licenses/LICENSE": "Apache text",
                "licenses/NOTICE": "Required attribution",
            },
        )

        assert notices._license_texts(dist) == (
            "Apache text",
            "Required attribution",
        )

    def test_multiline_license_field_is_body_not_summary(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(
            tmp_path,
            "inline",
            ["License: MIT License", "        Copyright (c) 2022 Someone"],
            {},
        )

        assert notices._license_expression(dist) == "MIT License"
        assert notices._license_texts(dist) == (
            "MIT License\nCopyright (c) 2022 Someone",
        )

    def test_unknown_when_metadata_carries_nothing(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(tmp_path, "bare", [], {})

        assert notices._license_expression(dist) == notices.UNKNOWN_LICENSE
        assert notices._license_texts(dist) == ()
        assert notices._notice(dist).texts == ()


class TestTemplateFallback:
    def test_spdx_text_with_holder_when_wheel_ships_no_file(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(
            tmp_path,
            "textless",
            ["License-Expression: MIT", "Author-email: Jane Doe <jane@example.org>"],
            {},
        )

        (text,) = notices._notice(dist).texts

        assert text.startswith(notices.TEMPLATE_NOTE.format(spdx="MIT"))
        assert "Copyright (c) Jane Doe" in text
        assert text.count("Copyright (c)") == 1, "template already carries the prefix"
        assert "<year>" not in text
        assert "<copyright holders>" not in text
        assert "Permission is hereby granted, free of charge" in text

    def test_classifier_alias_reaches_the_template(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(
            tmp_path,
            "classified",
            [
                "Classifier: License :: OSI Approved :: Apache Software License",
                "Author: ACME Corp",
            ],
            {},
        )

        (text,) = notices._notice(dist).texts

        # Apache-2.0 carries no holder placeholder, so the line is prepended.
        assert text.splitlines()[2] == "Copyright (c) ACME Corp"
        assert "Apache License" in text
        assert "Version 2.0" in text

    def test_shipped_file_wins_over_the_template(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(
            tmp_path,
            "shipped",
            ["License-Expression: MIT", "License-File: LICENSE"],
            {"licenses/LICENSE": "the real MIT text"},
        )

        assert notices._notice(dist).texts == ("the real MIT text",)

    def test_holder_falls_back_to_the_package_name(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(tmp_path, "anon", ["License-Expression: ISC"], {})

        assert notices._copyright_holder(dist) == "anon"

    def test_every_alias_has_a_template_file(self, notices: ModuleType) -> None:
        for spdx in set(notices.SPDX_ALIASES.values()):
            assert (notices.LICENSE_TEXTS_DIR / f"{spdx}.txt").is_file(), spdx


class TestRender:
    def test_entries_are_sorted_case_insensitively(self, notices: ModuleType) -> None:
        listed = [
            notices.Notice("zeta", "2.0", "MIT", ("MIT text",)),
            notices.Notice("Alpha", "1.0", "Apache-2.0", ("Apache text",)),
        ]

        rendered = notices.render(listed)

        assert rendered.index("Alpha 1.0") < rendered.index("zeta 2.0")
        assert "License: Apache-2.0" in rendered
        assert "MIT text" in rendered
        assert "2 packages." in rendered

    def test_main_writes_the_real_closure(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        output = tmp_path / "notices.txt"

        assert notices.main(["--output", str(output)]) == 0

        text = output.read_text(encoding="utf-8")
        assert "THIRD-PARTY SOFTWARE NOTICES" in text
        assert all(f"\n{name} " in text for name in RUNTIME_DISTRIBUTIONS)
        assert not any(f"\n{name} " in text for name in DEV_ONLY_DISTRIBUTIONS)

    def test_main_refuses_to_write_an_incomplete_notice(
        self,
        notices: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bare = _fake_dist(tmp_path, "bare", ["License-Expression: LicenseRef-X"], {})
        monkeypatch.setattr(notices, "runtime_closure", lambda: {"bare": bare})
        output = tmp_path / "notices.txt"

        assert notices.main(["--output", str(output)]) == 1

        assert not output.exists()
        assert "bare" in capsys.readouterr().err


class TestWorkflowStep:
    def _steps(self) -> list[dict]:
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        return workflow["jobs"][BUILD_JOB_ID]["steps"]

    def test_notices_step_runs_after_build_and_before_upload(self) -> None:
        names = [step.get("name") for step in self._steps()]

        assert NOTICES_STEP_NAME in names
        assert names.index("Build binary") < names.index(NOTICES_STEP_NAME)
        assert names.index(NOTICES_STEP_NAME) < names.index("Upload binary artifact")

    def test_notices_output_matches_the_release_globs(self) -> None:
        step = next(s for s in self._steps() if s.get("name") == NOTICES_STEP_NAME)

        assert "scripts/generate_third_party_notices.py" in step["run"]
        assert f'--output "{BINARY_ARTIFACT_PREFIX}' in step["run"], (
            "the upload, release and signing steps glob dist/code-graph-rag-*; "
            "a notices file outside that prefix is built and then dropped"
        )

    def test_pr_trigger_covers_the_generator(self) -> None:
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

        assert (
            "scripts/generate_third_party_notices.py"
            in (workflow[True]["pull_request"]["paths"])
        )


class TestBundleFilteredLicences:
    """A notice must not reproduce licences for code the binary excludes.

    pywin32 vendors an LGPL-2.1 `adodbapi` that nothing imports, so
    PyInstaller leaves it out. Reproducing its text asserts copyleft terms
    over a binary carrying no copyleft code -- an inaccuracy in the file whose
    whole purpose is stating licensing accurately.
    """

    def test_excluded_component_licence_is_dropped(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(
            tmp_path,
            "winkit",
            ["License: PSF"],
            {},
            package_files={
                "adodbapi/license.txt": "LGPL text",
                "win32/license.txt": "BSD text",
            },
        )

        paths = notices._license_paths(dist, frozenset({"win32"}))

        assert paths == ["win32/license.txt"]
        assert "LGPL text" not in "\n".join(
            notices._license_texts(dist, frozenset({"win32"}))
        )

    def test_unfiltered_without_a_bundle_keeps_every_licence(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """The control. An empty set means "unknown", never "nothing shipped".

        Without this the filtered assertion above passes just as well against
        a generator that drops package-data licences unconditionally.
        """
        dist = _fake_dist(
            tmp_path,
            "winkit",
            ["License: PSF"],
            {},
            package_files={
                "adodbapi/license.txt": "LGPL text",
                "win32/license.txt": "BSD text",
            },
        )

        assert len(notices._license_paths(dist)) == 2
        assert len(notices._license_paths(dist, frozenset())) == 2

    def test_an_unknown_bundle_keeps_licences_a_partial_match_would_drop(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """An empty `bundled` matches nothing, so the full set is restored.

        There is deliberately no `if not bundled` short-circuit: deleting one
        changed no behaviour under mutation, because an empty set matches
        nothing and falls through `kept or package_data` anyway. This pins the
        OUTCOME rather than the mechanism, so the guard stays unnecessary.
        """
        dist = _fake_dist(
            tmp_path,
            "mixed",
            ["License: PSF"],
            {},
            package_files={
                "shipped/LICENSE": "kept text",
                "excluded/LICENSE": "dropped text",
            },
        )

        # A readable bundle naming only one component filters to that one.
        assert notices._license_paths(dist, frozenset({"shipped"})) == [
            "shipped/LICENSE"
        ]
        # An unreadable one must keep BOTH, not just the matching one.
        assert len(notices._license_paths(dist, frozenset())) == 2

    def test_a_package_with_no_bundled_component_keeps_its_licences(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """Filtering must never empty a notice.

        The distribution is in the runtime closure, so it owes something. A
        package contributing only data files would otherwise match no
        component and lose every licence it ships.
        """
        dist = _fake_dist(
            tmp_path,
            "dataonly",
            ["License: MIT"],
            {},
            package_files={"dataonly/LICENSE": "MIT text"},
        )

        assert notices._license_paths(dist, frozenset({"something-else"})) == [
            "dataonly/LICENSE"
        ]

    def test_dist_info_licences_are_never_filtered(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """A distribution always owes its own declared licence."""
        dist = _fake_dist(
            tmp_path,
            "normal",
            ["License-File: LICENSE"],
            {"LICENSE": "MIT text"},
        )

        assert notices._license_texts(dist, frozenset({"unrelated"})) == ("MIT text",)


class TestLicenceHeaderProvenance:
    """`License:` can contradict the text beneath it (issue #2105).

    pywin32 declares PSF and ships BSD-3-Clause, so the header asserts a
    licence that appears nowhere in the body. The header names the file the
    text came from rather than guessing a licence from the text: licence texts
    quote other licences, and detecting by phrase relabelled PSF-2.0
    `typing-extensions` as GPL in testing.
    """

    def test_package_data_licence_is_named_in_the_header(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        dist = _fake_dist(
            tmp_path,
            "winkit",
            ["License: PSF"],
            {},
            package_files={"win32/license.txt": "BSD text"},
        )

        line = notices._notice(dist).license_line()

        assert line.startswith("PSF"), "the declared string is kept, not replaced"
        assert "win32/license.txt" in line

    def test_a_declared_licence_file_needs_no_qualifier(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """The control: a normal wheel's header is unchanged.

        Without this, a `license_line` that appended provenance to everything
        would satisfy the assertion above.
        """
        dist = _fake_dist(
            tmp_path,
            "normal",
            ["License-Expression: MIT", "License-File: LICENSE"],
            {"LICENSE": "MIT text"},
        )

        notice = notices._notice(dist)

        assert notice.sources == ()
        assert notice.license_line() == "MIT"

    def test_a_template_licence_needs_no_qualifier(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """A template IS the declared licence's text, so nothing contradicts."""
        dist = _fake_dist(tmp_path, "nofile", ["License: MIT"], {})

        notice = notices._notice(dist)

        assert notice.texts, "the template should have supplied the text"
        assert notice.license_line() == "MIT"

    def test_the_qualifier_reaches_the_rendered_entry(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        """A header that never renders fixes nothing the reader can see."""
        dist = _fake_dist(
            tmp_path,
            "winkit",
            ["License: PSF"],
            {},
            package_files={"win32/license.txt": "BSD text"},
        )

        rendered = notices.render([notices._notice(dist)])

        assert "License: PSF (text as shipped in win32/license.txt)" in rendered


class TestBundleContents:
    """Reading what the binary carries. Both archives, or the answer is wrong.

    A TOC-only scan reports 86 of 136 shipped packages absent, because
    PyInstaller keeps pure-Python modules in a `PYZ.pyz` sub-archive. Deriving
    notices from that would drop licences the binary genuinely owes.
    """

    def test_unreadable_binary_reports_unknown_not_empty(
        self, bundle: ModuleType, tmp_path: Path
    ) -> None:
        """The consequential direction.

        An unreadable binary must not read as "nothing is bundled": that would
        filter every package-data licence out of the notices file.
        """
        not_a_binary = tmp_path / "broken.exe"
        not_a_binary.write_bytes(b"not a pyinstaller archive")

        assert bundle.bundled_components(not_a_binary) == frozenset()

    def test_missing_binary_does_not_raise(
        self, bundle: ModuleType, tmp_path: Path
    ) -> None:
        assert bundle.bundled_components(tmp_path / "absent.exe") == frozenset()

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            ("win32\\win32api.pyd", {"win32"}),
            ("win32/win32api.pyd", {"win32"}),
            ("PYZ.pyz", {"pyz.pyz"}),
            ("base_library/LICENSE", {"base_library"}),
            # A dot in a TOC path is an extension, so the directory is kept
            # whole -- this is the key a package-data licence is matched by.
            ("ruamel.yaml/LICENSE", {"ruamel.yaml"}),
        ],
    )
    def test_toc_entries_reduce_to_their_directory(
        self, bundle: ModuleType, entry: str, expected: set[str]
    ) -> None:
        assert bundle._top_level(entry, dotted=False) == expected

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            ("anyio", {"anyio"}),
            ("anyio.abc", {"anyio", "anyio.abc"}),
            ("pywintypes", {"pywintypes"}),
            # Every prefix, because nothing in the module name says where the
            # DISTRIBUTION boundary falls: `ruamel.yaml` ships its licence
            # under `ruamel.yaml/`, so that key must be offered.
            (
                "ruamel.yaml.main",
                {"ruamel", "ruamel.yaml", "ruamel.yaml.main"},
            ),
        ],
    )
    def test_pyz_modules_offer_every_dotted_prefix(
        self, bundle: ModuleType, entry: str, expected: set[str]
    ) -> None:
        assert bundle._top_level(entry, dotted=True) == expected


class TestBundleWorkflowWiring:
    """The filter only works if the build actually passes the binary."""

    def _steps(self) -> list[dict]:
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        return workflow["jobs"][BUILD_JOB_ID]["steps"]

    def test_notices_step_passes_the_built_binary(self) -> None:
        step = next(s for s in self._steps() if s.get("name") == NOTICES_STEP_NAME)

        assert "--binary" in step["run"], (
            "without --binary the generator falls back to the installed wheel "
            "and reproduces licences for components the build excluded"
        )

    def test_notices_step_uses_the_windows_suffix(self) -> None:
        """The Windows binary is `.exe`; a missing suffix silently unfilters it."""
        step = next(s for s in self._steps() if s.get("name") == NOTICES_STEP_NAME)

        assert ".exe" in step["run"]

    def test_pr_trigger_covers_the_bundle_reader(self) -> None:
        """The generator imports it, so a change there can break the build."""
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

        assert "scripts/bundle_contents.py" in workflow[True]["pull_request"]["paths"]


class TestPyzArchiveIsActuallyRead:
    """The PYZ half must run, and only a real archive proves it does.

    Every other test here uses synthetic component sets, so all of them pass
    against a `bundled_components` that never opens the sub-archive at all.
    That is not hypothetical: `ZlibArchiveReader` parses a `?offset` suffix
    with `filename.rfind('?')`, so passing a `Path` raises `AttributeError`
    before the file is opened. Under a `try/except` that returned the TOC-only
    answer, 47 tests stayed green while the binary reported every pure-Python
    package absent -- 96 components instead of 294 on the macOS binary, with
    `anyio` and `click` among the missing, which drops licences the binary
    owes.

    These build a genuine PYZ with PyInstaller's own writer (a few hundred
    bytes, no binary build) so the reader is exercised for real.
    """

    @staticmethod
    def _write_pyz(path: Path, names: list[str]) -> None:
        from PyInstaller.archive.writers import ZlibArchiveWriter

        code_dict = {name: compile("X = 1\n", name, "exec") for name in names}
        ZlibArchiveWriter(
            str(path),
            [(name, "/nonexistent", "PYMODULE") for name in names],
            code_dict=code_dict,
        )

    @classmethod
    def _write_binary(cls, root: Path, modules: list[str], data: list[str]) -> Path:
        """A real one-file archive: a PYZ inside a CArchive, ~600 bytes."""
        from PyInstaller.archive.writers import CArchiveWriter

        pyz = root / "PYZ.pyz"
        cls._write_pyz(pyz, modules)
        entries = [("PYZ.pyz", str(pyz), 0, "z")]
        for name in data:
            blob = root / name.replace("/", "_")
            blob.write_text("licence text")
            entries.append((name, str(blob), 0, "x"))

        binary = root / "fake_binary"
        CArchiveWriter(str(binary), entries, pylib_name="libpython3.12.so")
        return binary

    def test_pure_python_modules_reach_bundled_components(
        self, bundle: ModuleType, tmp_path: Path
    ) -> None:
        """End to end, through the module's own entry point.

        This is the assertion that catches the `Path`/`str` defect. Reading
        the archive directly in a test would exercise PyInstaller rather than
        `bundled_components`, and passes either way -- measured: with the
        defect reintroduced, a direct-read version of this test stayed green
        while the shipped function reported every pure-Python package absent.
        """
        binary = self._write_binary(
            tmp_path, ["anyio.abc", "click.core"], ["win32/license.txt"]
        )

        components = bundle.bundled_components(binary)

        # From the PYZ. Absent entirely when the sub-archive is not read.
        assert "anyio" in components
        assert "click" in components
        # From the TOC, which a broken PYZ read would still return -- so this
        # one alone cannot tell the two apart.
        assert "win32" in components

    def test_a_dotted_distribution_matches_end_to_end(
        self, bundle: ModuleType, notices: ModuleType, tmp_path: Path
    ) -> None:
        """`ruamel.yaml/LICENSE` must match a bundled `ruamel.yaml.*` module."""
        binary = self._write_binary(tmp_path, ["ruamel.yaml.main"], [])

        components = bundle.bundled_components(binary)

        assert notices._component_of("ruamel.yaml/LICENSE") in components

    def test_the_reader_is_given_a_string(self, bundle: ModuleType) -> None:
        """Pins OUR call, not PyInstaller's tolerance of a `Path`.

        Asserting that `ZlibArchiveReader` REJECTS a `Path` would make a
        third-party limitation a CI requirement: if PyInstaller becomes
        Path-compatible the assertion fails with no product regression
        (Greptile, #2110). What matters is that this module passes a string,
        which is true whatever the library later accepts.

        The end-to-end test above is what proves the archive is really read;
        this one names the argument type so a "tidy-up" that drops `str()`
        has a test to answer to rather than only a comment.
        """
        source = (
            Path(bundle.__file__).read_text(encoding="utf-8") if bundle.__file__ else ""
        )
        tree = ast.parse(source)

        reader_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ZlibArchiveReader"
        ]

        assert reader_calls, "bundle_contents never constructs a ZlibArchiveReader"
        for call in reader_calls:
            assert call.args, "ZlibArchiveReader called with no argument"
            first = call.args[0]
            why = (
                "ZlibArchiveReader must be given `str(...)`; it parses a "
                "`?offset` suffix off the name, so a Path raises "
                "AttributeError before the archive is opened and the PYZ "
                "half silently never runs"
            )
            assert isinstance(first, ast.Call), why
            assert isinstance(first.func, ast.Name), why
            assert first.func.id == "str", why

    def test_a_dotted_module_matches_its_licence_directory(
        self, bundle: ModuleType, notices: ModuleType, tmp_path: Path
    ) -> None:
        """`ruamel.yaml` ships its licence under `ruamel.yaml/`, not `ruamel/`.

        Reducing a PYZ module to its first segment yields `ruamel`, which
        matches no licence directory, so the filter drops a licence the binary
        carries. The two sides must agree on the key.
        """
        keys = bundle._top_level("ruamel.yaml.main", dotted=True)

        assert notices._component_of("ruamel.yaml/LICENSE") in keys
        # The single-segment case must keep working.
        assert notices._component_of("anyio/LICENSE") in bundle._top_level(
            "anyio.abc", dotted=True
        )

    def test_a_toc_path_keeps_its_dotted_directory(self, bundle: ModuleType) -> None:
        """A dot in a TOC entry is an extension, not a package separator."""
        assert bundle._top_level("ruamel.yaml/LICENSE", dotted=False) == {"ruamel.yaml"}
        assert bundle._top_level("win32\\win32api.pyd", dotted=False) == {"win32"}

    def test_an_unreadable_pyz_reports_unknown_not_the_toc_answer(
        self, bundle: ModuleType, tmp_path: Path
    ) -> None:
        """Degrading to TOC-only is the licence-dropping direction.

        A binary whose TOC parses but whose PYZ does not is a broken
        instrument. Returning its TOC would filter against a set that omits
        every pure-Python package, so the whole read must report "unknown"
        and let the caller disable filtering.
        """
        from unittest import mock

        binary = tmp_path / "fake"
        binary.write_bytes(b"x")

        with mock.patch.object(
            bundle, "_read_archives", side_effect=RuntimeError("pyz unreadable")
        ):
            assert bundle.bundled_components(binary) == frozenset()


class TestPyzEntryLookup:
    """Finding the PYZ must not hinge on a build-time filename.

    Copilot raised (#2110) that one-file archives can name the embedded
    archive `PYZ-00.pyz` with an indexed suffix. On this project's PyInstaller
    the embedded name is always `PYZ.pyz` -- the build overrides it ("Override
    PYZ name in the PKG archive into PYZ.pyz, regardless of what the original
    name was", `building/api.py:345`), and all three CI binaries confirm it --
    so the concrete claim did not hold. The underlying point does: the name is
    a build detail, while the TYPECODE is the contract the bootloader matches
    on, so the typecode leads and the name is only a fallback.
    """

    def test_the_typecode_finds_an_indexed_pyz(self, bundle: ModuleType) -> None:
        """The case raised: a name the literal check would miss."""

        class _Archive:
            toc = {
                "PYZ-00.pyz": (0, 0, 0, 0, "z"),
                "win32/win32api.pyd": (0, 0, 0, 0, "m"),
            }

        assert bundle._pyz_entry_names(_Archive()) == ["PYZ-00.pyz"]

    def test_the_typecode_is_preferred_over_the_name(self, bundle: ModuleType) -> None:
        """A `.pyz`-named entry without the typecode is not the sub-archive."""

        class _Archive:
            toc = {
                "PYZ.pyz": (0, 0, 0, 0, "z"),
                "vendored/decoy.pyz": (0, 0, 0, 0, "m"),
            }

        assert bundle._pyz_entry_names(_Archive()) == ["PYZ.pyz"]

    @pytest.mark.parametrize("name", ["PYZ.pyz", "PYZ-00.pyz", "pyz-12.pyz"])
    def test_the_name_fallback_covers_both_conventions(
        self, bundle: ModuleType, name: str
    ) -> None:
        """Used only when the TOC is not the dict shape carrying typecodes."""

        class _Archive:
            toc = [name, "win32/win32api.pyd"]

        assert bundle._pyz_entry_names(_Archive()) == [name]

    def test_an_archive_with_no_pyz_yields_nothing(self, bundle: ModuleType) -> None:
        """A binary may legitimately have none; the TOC answer then stands."""

        class _Archive:
            toc = {"win32/win32api.pyd": (0, 0, 0, 0, "m")}

        assert bundle._pyz_entry_names(_Archive()) == []

    def test_a_real_binary_resolves_through_the_typecode(
        self, bundle: ModuleType, tmp_path: Path
    ) -> None:
        """End to end on an archive built here, not a hand-made TOC."""
        binary = TestPyzArchiveIsActuallyRead._write_binary(tmp_path, ["anyio.abc"], [])

        from PyInstaller.archive.readers import CArchiveReader

        names = bundle._pyz_entry_names(CArchiveReader(str(binary)))

        assert names, "the PYZ was not found in a real one-file archive"
        assert "anyio" in bundle.bundled_components(binary)


# Root-level shared libraries in the v0.0.945 release binaries, read from each
# archive's TOC. These are what `runtime_closure` never sees: no wheel owns
# them, so without a native entry they shipped with no notice at all.
LINUX_RELEASE_LIBRARIES = (
    "libbz2.so.1.0",
    "libcrypto.so.3",
    "libffi.so.8",
    "libgcc_s.so.1",
    "liblzma.so.5",
    "libpython3.12.so.1.0",
    "libssl.so.3",
    "libstdc++.so.6",
    "libtinfo.so.6",
    "libuuid.so.1",
    "libz.so.1",
)
DARWIN_RELEASE_LIBRARIES = ("libcrypto.3.dylib", "libssl.3.dylib")
WINDOWS_RELEASE_LIBRARIES = (
    "VCRUNTIME140.dll",
    "VCRUNTIME140_1.dll",
    "api-ms-win-core-console-l1-1-0.dll",
    "api-ms-win-crt-runtime-l1-1-0.dll",
    "libcrypto-3.dll",
    "libffi-8.dll",
    "libssl-3.dll",
    "python3.dll",
    "python312.dll",
    "ucrtbase.dll",
)


class TestNativeLibraries:
    def test_root_libraries_are_listed_and_extension_modules_are_not(
        self, bundle: ModuleType, tmp_path: Path
    ) -> None:
        binary = TestPyzArchiveIsActuallyRead._write_binary(
            tmp_path,
            ["anyio.abc"],
            [
                "libssl.so.3",
                "libcrypto.3.dylib",
                "python312.dll",
                "_cffi_backend.cpython-312-x86_64-linux-gnu.so",
                "mgclient.cpython-312-darwin.so",
                "_rust.abi3.so",
                "python3.12/lib-dynload/_ssl.cpython-312-x86_64-linux-gnu.so",
                "pywin32_system32/pywintypes312.dll",
            ],
        )

        assert bundle.native_libraries(binary) == frozenset(
            {"libssl.so.3", "libcrypto.3.dylib", "python312.dll"}
        )

    def test_an_unreadable_binary_is_unknown_not_empty(
        self, bundle: ModuleType, tmp_path: Path
    ) -> None:
        not_a_binary = tmp_path / "plain.txt"
        not_a_binary.write_text("not an archive")

        assert bundle.native_libraries(not_a_binary) is None


class TestNativeNotices:
    @pytest.mark.parametrize(
        ("libraries", "expected"),
        [
            (
                LINUX_RELEASE_LIBRARIES,
                {
                    "CPython",
                    "OpenSSL",
                    "libffi",
                    "zlib",
                    "bzip2",
                    "liblzma (XZ Utils)",
                    "ncurses",
                    "libuuid (util-linux)",
                    "GCC runtime libraries",
                },
            ),
            (DARWIN_RELEASE_LIBRARIES, {"CPython", "OpenSSL"}),
            # A distro interpreter links the system Expat rather than the copy
            # CPython vendors; a local build on Ubuntu 24.04 bundled it.
            (("libexpat.so.1",), {"CPython", "Expat"}),
            (
                WINDOWS_RELEASE_LIBRARIES,
                {
                    "CPython",
                    "OpenSSL",
                    "libffi",
                    "Microsoft Visual C++ runtime and Universal CRT",
                },
            ),
        ],
    )
    def test_every_release_library_is_covered(
        self, notices: ModuleType, libraries: tuple[str, ...], expected: set[str]
    ) -> None:
        produced = notices.native_notices(frozenset(libraries))

        assert {n.name for n in produced} == expected
        assert all(n.texts and n.texts[0] for n in produced)

    def test_the_notice_names_the_files_it_covers(self, notices: ModuleType) -> None:
        produced = notices.native_notices(frozenset(DARWIN_RELEASE_LIBRARIES))

        openssl = next(n for n in produced if n.name == "OpenSSL")
        assert "libcrypto.3.dylib" in openssl.version
        assert "libssl.3.dylib" in openssl.version
        assert "The OpenSSL Project Authors" in openssl.texts[0]

    def test_cpython_is_credited_even_without_a_binary(
        self, notices: ModuleType
    ) -> None:
        """The interpreter is in every binary, readable or not."""
        produced = notices.native_notices(None)

        assert [n.name for n in produced] == ["CPython"]
        assert "PYTHON SOFTWARE FOUNDATION LICENSE" in produced[0].texts[0]

    def test_bundled_readline_refuses_the_notice(self, notices: ModuleType) -> None:
        """The regression: v0.0.945 for Linux shipped GNU Readline (GPL-3.0)."""
        with pytest.raises(notices.NativeLicenseError, match="GNU Readline"):
            notices.native_notices(
                frozenset(LINUX_RELEASE_LIBRARIES) | {"libreadline.so.8"}
            )

    def test_an_unlisted_library_refuses_the_notice(self, notices: ModuleType) -> None:
        """A new library must get a licence entry, not ship unattributed."""
        with pytest.raises(notices.NativeLicenseError, match="libsqlite3.so.0"):
            notices.native_notices(frozenset({"libsqlite3.so.0"}))

    def test_a_missing_interpreter_licence_refuses_the_notice(
        self,
        notices: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(notices.sysconfig, "get_path", lambda _: str(tmp_path))
        monkeypatch.setattr(notices.sys, "base_prefix", str(tmp_path))

        with pytest.raises(notices.NativeLicenseError, match="LICENSE.txt"):
            notices.native_notices(None)

    def test_every_native_text_file_exists(self, notices: ModuleType) -> None:
        for component in notices.NATIVE_COMPONENTS:
            if component.text_file is not None:
                path = notices.NATIVE_TEXTS_DIR / component.text_file
                assert path.read_text(encoding="utf-8").strip(), component.name


class TestNativeNoticesEndToEnd:
    def test_main_credits_the_bundled_libraries(
        self, notices: ModuleType, tmp_path: Path
    ) -> None:
        binary = TestPyzArchiveIsActuallyRead._write_binary(
            tmp_path, ["anyio.abc"], ["libssl.so.3", "libffi.so.8"]
        )
        output = tmp_path / "notices.txt"

        assert notices.main(["--output", str(output), "--binary", str(binary)]) == 0

        text = output.read_text(encoding="utf-8")
        assert "\nOpenSSL (bundled as libssl.so.3)" in text
        assert "\nlibffi (bundled as libffi.so.8)" in text
        assert "\nCPython " in text

    def test_main_refuses_a_binary_carrying_readline(
        self,
        notices: ModuleType,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        binary = TestPyzArchiveIsActuallyRead._write_binary(
            tmp_path, ["anyio.abc"], ["libreadline.so.8"]
        )
        output = tmp_path / "notices.txt"

        assert notices.main(["--output", str(output), "--binary", str(binary)]) == 1

        assert not output.exists()
        assert "GNU Readline" in capsys.readouterr().err

    def test_main_refuses_a_binary_it_cannot_inventory(
        self,
        notices: ModuleType,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unread inventory must not pass as one with nothing to check."""
        not_a_binary = tmp_path / "plain.txt"
        not_a_binary.write_text("not an archive")
        output = tmp_path / "notices.txt"

        assert (
            notices.main(["--output", str(output), "--binary", str(not_a_binary)]) == 1
        )

        assert not output.exists()
        assert "native libraries cannot be checked" in capsys.readouterr().err
