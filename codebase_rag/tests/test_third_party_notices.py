"""Guards for the third-party notices shipped beside each PyInstaller binary.

The one-file binaries bundle every runtime dependency without the wheels'
licence files, and every permissive licence in the tree conditions
redistribution on keeping its notice. The generator and the build step that
runs it are the only thing producing that notice, so both are pinned here.
"""

from __future__ import annotations

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


def _fake_dist(
    root: Path,
    name: str,
    metadata_lines: list[str],
    files: dict[str, str],
) -> PathDistribution:
    """Lay out `<name>-1.0.dist-info` the way an installed wheel does."""
    dist_info = root / f"{name}-1.0.dist-info"
    dist_info.mkdir()
    metadata = ["Metadata-Version: 2.4", f"Name: {name}", "Version: 1.0"]
    metadata.extend(metadata_lines)
    (dist_info / "METADATA").write_text("\n".join(metadata) + "\n", encoding="utf-8")
    for relative, text in files.items():
        target = dist_info / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    record_entries = [f"{dist_info.name}/METADATA,,", f"{dist_info.name}/RECORD,,"]
    record_entries.extend(f"{dist_info.name}/{relative},," for relative in files)
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
        assert "<year>" not in text and "<copyright holders>" not in text
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
        assert "Apache License" in text and "Version 2.0" in text

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
