"""server.json reaches the MCP Registry only after PyPI serves its release.

The registry checks, when a version is published, that PyPI serves the
package version server.json names and that its README carries the
`mcp-name:` ownership token, and it never lets a listed version change. So
publish.yml publishes server.json after code-graph-rag is on PyPI, and
scripts/check_mcp_registry_release.py refuses a server.json that would point
the registry anywhere else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

from scripts import check_mcp_registry_release as registry_check

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish.yml"
PYPI_README = REPO_ROOT / "PYPI_README.md"
JOB = "publish-mcp-registry"
# The limits server.schema.json (2025-12-11) sets, which the registry
# enforces on publish.
DESCRIPTION_MAX = 100
TITLE_MAX = 100
NAME_PATTERN = re.compile(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+")


# Functional form: "if" and "cancel-in-progress" are not identifiers.
Concurrency = TypedDict("Concurrency", {"group": str, "cancel-in-progress": bool})
Step = TypedDict("Step", {"id": str, "if": str, "run": str, "uses": str}, total=False)
Job = TypedDict(
    "Job",
    {
        "needs": list[str],
        "if": str,
        "concurrency": Concurrency,
        "env": dict[str, str],
        "steps": list[Step],
    },
    total=False,
)


def _manifest() -> registry_check.Manifest:
    return json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))


def _project() -> tuple[str, str]:
    return registry_check.project_metadata()


def _tag() -> str:
    return f"v{_project()[1]}"


def _job() -> Job:
    return yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))["jobs"][JOB]


def _step_index(steps: list[Step], needle: str) -> int:
    return next(i for i, step in enumerate(steps) if needle in step.get("run", ""))


class TestManifestPassesTheRegistrysLimits:
    def test_description_fits(self) -> None:
        """A 133-character description was rejected by the registry."""
        assert len(_manifest()["description"]) <= DESCRIPTION_MAX

    def test_title_fits(self) -> None:
        assert len(_manifest()["title"]) <= TITLE_MAX

    def test_name_is_a_namespaced_server_name(self) -> None:
        assert NAME_PATTERN.fullmatch(_manifest()["name"])

    def test_names_this_projects_pypi_release(self) -> None:
        name, version = _project()
        assert (
            registry_check.manifest_problems(_manifest(), name=name, version=version)
            == []
        )


class TestOwnershipToken:
    def test_the_pypi_readme_carries_it(self) -> None:
        readme = PYPI_README.read_text(encoding="utf-8")
        assert registry_check.has_name_token(readme, _manifest()["name"])

    @pytest.mark.parametrize(
        "readme",
        [
            "<!-- mcp-name: io.github.acme/widget -->",
            "<!-- mcp-name: io.github.acme/widget-->",
            "mcp-name: io.github.acme/widget\n",
            "mcp-name: io.github.acme/widget",
            "mcp-name: io.github.acme/widget-pro\nmcp-name: io.github.acme/widget\n",
        ],
    )
    def test_a_complete_token_counts(self, readme: str) -> None:
        assert registry_check.has_name_token(readme, "io.github.acme/widget")

    @pytest.mark.parametrize(
        "readme",
        [
            "mcp-name: io.github.acme/widget-pro",
            "mcp-name: io.github.acme/widget.v2",
            "mcp-name:io.github.acme/widget",
            "",
        ],
    )
    def test_a_longer_name_or_no_token_does_not(self, readme: str) -> None:
        assert not registry_check.has_name_token(readme, "io.github.acme/widget")


class TestManifestProblems:
    def test_a_version_pypi_does_not_have_is_refused(self) -> None:
        name, _ = _project()
        problems = registry_check.manifest_problems(
            _manifest(), name=name, version="999.0.0"
        )
        assert len(problems) == 2
        assert all("999.0.0" in problem for problem in problems)

    def test_another_package_is_refused(self) -> None:
        name, version = _project()
        manifest = _manifest()
        manifest["packages"][0]["identifier"] = "someone-elses-package"
        problems = registry_check.manifest_problems(
            manifest, name=name, version=version
        )
        assert problems
        assert "someone-elses-package" in problems[0]


class TestMain:
    @pytest.mark.parametrize("tag", ["0.4.14", "v0.4", "v1.0/../x", "latest"])
    def test_a_malformed_tag_is_refused(self, tag: str) -> None:
        assert registry_check.main(["pypi", tag]) == 1

    def test_an_unknown_service_is_refused(self) -> None:
        assert registry_check.main(["npm", _tag()]) == 1

    def test_a_tag_other_than_the_checked_out_release_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(registry_check, "pypi_readme", lambda *_: "")
        assert registry_check.main(["pypi", "v999.0.0"]) == 1
        assert "999.0.0" in capsys.readouterr().err

    def test_a_release_pypi_lacks_waits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(registry_check, "pypi_readme", lambda *_: None)
        assert registry_check.main(["pypi", _tag()]) == registry_check.MISSING

    def test_a_release_without_the_token_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(registry_check, "pypi_readme", lambda *_: "# README\n")
        assert registry_check.main(["pypi", _tag()]) == 1
        assert "mcp-name" in capsys.readouterr().err

    def test_a_release_with_the_token_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        readme = PYPI_README.read_text(encoding="utf-8")
        monkeypatch.setattr(registry_check, "pypi_readme", lambda *_: readme)
        assert registry_check.main(["pypi", _tag()]) == 0

    @pytest.mark.parametrize(
        ("listed", "status"), [(True, 0), (False, registry_check.MISSING)]
    )
    def test_the_registry_lookup(
        self, listed: bool, status: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(registry_check, "registry_lists", lambda *_: listed)
        assert registry_check.main(["registry", _tag()]) == status

    @pytest.mark.parametrize(
        ("service", "lookup"), [("pypi", "pypi_readme"), ("registry", "registry_lists")]
    )
    def test_an_unreachable_service_stops_the_release(
        self, service: str, lookup: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unreachable(*_: str) -> None:
            raise registry_check.urllib.error.URLError("offline")

        monkeypatch.setattr(registry_check, lookup, unreachable)
        assert registry_check.main([service, _tag()]) == 1

    def test_the_registry_lookup_escapes_the_name_and_sees_deleted_versions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        urls: list[str] = []
        monkeypatch.setattr(
            registry_check, "_fetch", lambda url: urls.append(url) or None
        )
        assert not registry_check.registry_lists("io.github.acme/widget", "1.2.3")
        assert urls == [
            "https://registry.modelcontextprotocol.io/v0.1/servers/"
            "io.github.acme%2Fwidget/versions/1.2.3?include_deleted=true"
        ]


class TestWorkflow:
    def test_runs_only_after_code_graph_rag_is_on_pypi(self) -> None:
        job = _job()
        assert "publish" in job["needs"]
        assert "needs.publish.result == 'success'" in job["if"]
        assert "needs.ci-gate.result == 'success'" in job["if"]

    def test_checks_pypi_and_the_registry_before_publishing(self) -> None:
        steps = _job()["steps"]
        pypi = _step_index(steps, "check_mcp_registry_release.py pypi")
        registry = _step_index(steps, "check_mcp_registry_release.py registry")
        publish = _step_index(steps, "mcp-publisher publish")
        assert pypi < registry < publish
        assert "if" not in steps[pypi]
        assert steps[registry]["id"] == "registry"
        for step in steps[registry + 1 :]:
            assert step["if"] == "steps.registry.outputs.publish == 'true'"

    def test_runs_a_pinned_signature_checked_publisher(self) -> None:
        job = _job()
        assert re.fullmatch(r"v\d+\.\d+\.\d+", job["env"]["MCP_PUBLISHER_VERSION"])
        download = next(
            step for step in job["steps"] if "mcp-publisher_" in step.get("run", "")
        )
        assert "releases/latest" not in download["run"]
        assert "cosign verify-blob" in download["run"]
        assert "${MCP_PUBLISHER_VERSION}" in download["run"]

    def test_runs_one_publish_per_release_at_a_time(self) -> None:
        concurrency = _job()["concurrency"]
        assert concurrency["cancel-in-progress"] is False
        assert "inputs.tag || github.ref_name" in concurrency["group"]
