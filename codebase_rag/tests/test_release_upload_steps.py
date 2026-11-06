"""Guards for the steps that put build-binaries.yml's output on the release.

The binaries, their signatures and the build provenance reach the GitHub
release through the ``gh`` CLI every runner already carries. Each test runs
the step's script the way GitHub's ``shell: bash`` does, after expanding its
``${{ }}`` expressions the way GitHub does, against a stand-in ``gh`` that
records its calls. The assertions therefore hold for the script CI executes,
not for a copy of it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from codebase_rag import constants as cs

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build-binaries.yml"

TAG = "v9.9.9"
EXPRESSIONS = {
    "github.ref_name": TAG,
    "github.repository": "owner/repo",
    "github.token": "token",
}
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
RELEASE_TAG_ONLY = "startsWith(github.ref, 'refs/tags/v')"

# Records each call and keeps whether the release exists in a state file, so
# a create and a later view agree. FAKE_CREATE picks what a create does: ok,
# lost-race (another leg created it first) or anything else (it fails).
FAKE_GH = r"""
printf '%s\n' "$*" >> "$GH_LOG"
case "$1 $2" in
  "release view") [ -e "$GH_STATE/release" ] ;;
  "release create")
    case "$FAKE_CREATE" in
      ok) touch "$GH_STATE/release" ;;
      lost-race) touch "$GH_STATE/release"; exit 1 ;;
      *) exit 1 ;;
    esac ;;
  "release upload")
    [ -e "$GH_STATE/release" ] || exit 1
    for arg in "${@:4}"; do
      [ "$arg" = --clobber ] || [ -f "$arg" ] || exit 1
    done ;;
  *) exit 2 ;;
esac
"""


def _step(job: str, name: str) -> dict:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return next(s for s in workflow["jobs"][job]["steps"] if s.get("name") == name)


def _render(text: str, values: dict[str, str]) -> str:
    def value(match: re.Match[str]) -> str:
        assert match.group(1) in values, f"unexpected expression {match.group(0)}"
        return values[match.group(1)]

    return EXPRESSION.sub(value, text)


def _run(
    step: dict,
    work: Path,
    *,
    release_exists: bool = True,
    create: str = "ok",
    values: dict[str, str] = EXPRESSIONS,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("the workflow step requires bash")
    assert "run" in step, (
        f"{step['name']!r} must be a script step calling the runner's gh, "
        "not a third-party release action"
    )
    # `gh` is a shell function running the stand-in through bash, and every
    # path is relative to the step's directory, so the stand-in needs no
    # executable bit, no PATH lookup and no path translation: the Windows
    # runner's bash finds it the way Linux and macOS do.
    (work / "fake-gh.sh").write_text(FAKE_GH, encoding="utf-8", newline="\n")
    state = work / "state"
    state.mkdir()
    if release_exists:
        (state / "release").touch()
    (work / "gh.log").touch()
    script = 'gh() { bash ./fake-gh.sh "$@"; }\n' + _render(step["run"], values)
    (work / "step.sh").write_text(script, encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        **{k: _render(str(v), values) for k, v in step.get("env", {}).items()},
        "GH_LOG": "gh.log",
        "GH_STATE": "state",
        "FAKE_CREATE": create,
    }
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "step.sh"],
        cwd=work,
        env=env,
        capture_output=True,
        encoding=cs.ENCODING_UTF8,
        timeout=30,
        check=False,
    )
    return result, (work / "gh.log").read_text(encoding="utf-8").splitlines()


def _uploads(calls: list[str]) -> list[set[str]]:
    uploads = []
    for call in calls:
        args = call.split(" ")
        if args[:2] == ["release", "upload"]:
            assert args[2] == TAG
            assert "--clobber" in args[3:], "a re-run must replace, not collide"
            uploads.append(set(args[3:]) - {"--clobber"})
    return uploads


def _files(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")


class TestBinaryUpload:
    BINARIES = (
        "dist/code-graph-rag-linux-amd64",
        "dist/code-graph-rag-linux-amd64.cdx.json",
        "dist/code-graph-rag-linux-amd64.THIRD_PARTY_NOTICES.txt",
    )

    def _step(self) -> dict:
        return _step("build", "Upload to release")

    def test_runs_only_for_a_release_tag(self) -> None:
        assert self._step().get("if") == RELEASE_TAG_ONLY

    def test_runs_under_bash_on_the_windows_leg_too(self) -> None:
        # The Windows runner's default shell is pwsh, which neither expands
        # the glob nor reads `||`.
        assert self._step().get("shell") == "bash"

    def test_uploads_every_binary_file_to_an_existing_release(
        self, tmp_path: Path
    ) -> None:
        _files(tmp_path, *self.BINARIES, "dist/unrelated.whl")

        result, calls = _run(self._step(), tmp_path)

        assert result.returncode == 0, result.stderr
        assert not any(call.startswith("release create") for call in calls)
        assert _uploads(calls) == [set(self.BINARIES)]

    def test_creates_the_release_when_the_tag_has_none(self, tmp_path: Path) -> None:
        _files(tmp_path, *self.BINARIES)

        result, calls = _run(self._step(), tmp_path, release_exists=False)

        assert result.returncode == 0, result.stderr
        (create,) = [call for call in calls if call.startswith("release create")]
        assert create.split(" ")[2] == TAG
        assert "--verify-tag" in create.split(" "), (
            "a release must never create the tag it names"
        )
        assert _uploads(calls) == [set(self.BINARIES)]

    def test_a_leg_whose_create_loses_the_race_still_uploads(
        self, tmp_path: Path
    ) -> None:
        _files(tmp_path, *self.BINARIES)

        result, calls = _run(
            self._step(), tmp_path, release_exists=False, create="lost-race"
        )

        assert result.returncode == 0, result.stderr
        assert _uploads(calls) == [set(self.BINARIES)]

    def test_fails_without_uploading_when_no_release_can_be_made(
        self, tmp_path: Path
    ) -> None:
        _files(tmp_path, *self.BINARIES)

        result, calls = _run(
            self._step(), tmp_path, release_exists=False, create="fail"
        )

        assert result.returncode != 0
        assert not any(call.startswith("release upload") for call in calls)

    def test_fails_when_the_build_left_no_binaries(self, tmp_path: Path) -> None:
        _files(tmp_path, "dist/unrelated.whl")

        result, calls = _run(self._step(), tmp_path)

        assert result.returncode != 0
        assert any(call.startswith("release upload") for call in calls), (
            "the failure must come from the upload finding nothing, "
            "not from an earlier step"
        )


class TestSignatureUpload:
    def _step(self) -> dict:
        return _step("sign-release", "Upload signatures to release")

    def test_uploads_every_signature_and_nothing_else(self, tmp_path: Path) -> None:
        signatures = {"artifacts/a.sigstore.json", "artifacts/b.sigstore.json"}
        _files(tmp_path, *signatures, "artifacts/a", "artifacts/b")

        result, calls = _run(self._step(), tmp_path)

        assert result.returncode == 0, result.stderr
        assert _uploads(calls) == [signatures]

    def test_no_signatures_uploads_nothing(self, tmp_path: Path) -> None:
        _files(tmp_path, "artifacts/a")

        result, calls = _run(self._step(), tmp_path)

        assert result.returncode == 0, result.stderr
        assert _uploads(calls) == []


class TestProvenance:
    BUNDLE = "steps.attest.outputs.bundle-path"

    def test_stages_the_attested_bundle(self, tmp_path: Path) -> None:
        _files(tmp_path, "bundle.jsonl")
        (tmp_path / "artifacts").mkdir()
        values = {**EXPRESSIONS, self.BUNDLE: str(tmp_path / "bundle.jsonl")}

        result, _ = _run(
            _step("sign-release", "Stage provenance bundle"), tmp_path, values=values
        )

        assert result.returncode == 0, result.stderr
        staged = tmp_path / "artifacts" / "multiple.intoto.jsonl"
        assert staged.read_text(encoding="utf-8") == "bundle.jsonl"

    def test_a_hostile_bundle_path_is_never_executed(self, tmp_path: Path) -> None:
        (tmp_path / "artifacts").mkdir()
        values = {**EXPRESSIONS, self.BUNDLE: "$(touch pwned)"}

        result, _ = _run(
            _step("sign-release", "Stage provenance bundle"), tmp_path, values=values
        )

        assert not (tmp_path / "pwned").exists(), (
            "the step output was expanded into the script and ran as code"
        )
        assert result.returncode != 0, "a path that names no file must fail the copy"

    def test_uploads_the_staged_bundle(self, tmp_path: Path) -> None:
        _files(tmp_path, "artifacts/multiple.intoto.jsonl", "artifacts/a")

        result, calls = _run(
            _step("sign-release", "Upload provenance to release"), tmp_path
        )

        assert result.returncode == 0, result.stderr
        assert _uploads(calls) == [{"artifacts/multiple.intoto.jsonl"}]
