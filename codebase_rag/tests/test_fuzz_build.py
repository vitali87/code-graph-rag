"""Contracts of the ClusterFuzzLite build and the workflows that run it.

Nothing else in the suite sees these files, and most of their failure modes
are silent: a dictionary libFuzzer refuses stops a target from starting, a
coverage build of a harness that cannot take the coverage stub produces no
report, and an unpinned action or a missing job timeout changes nothing
until the day it matters.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FUZZ_DIR = ROOT / "fuzz"
CFLITE_DIR = ROOT / ".clusterfuzzlite"
WORKFLOWS = ROOT / ".github" / "workflows"
HARNESSES = sorted(FUZZ_DIR.glob("fuzz_*.py"))
CFLITE_WORKFLOWS = sorted(WORKFLOWS.glob("cflite_*.yml"))
CFLITE_ACTION = re.compile(r"^google/clusterfuzzlite/actions/(\w+)@(\S+)$")
FULL_SHA = re.compile(r"[0-9a-f]{40}")

# What OSS-Fuzz's compile_python_fuzzer prepends to every harness in a
# coverage build, verbatim.
COVERAGE_STUB = """###### Coverage stub
import atexit
import coverage
cov = coverage.coverage(data_file='.coverage', cover_pylib=True)
cov.start()
# Register an exist handler that will print coverage
def exit_handler():
    cov.stop()
    cov.save()
atexit.register(exit_handler)
####### End of coverage stub
"""

# libFuzzer keeps dictionary words in a FixedWord<64> and silently skips any
# longer entry, so a longer one is dead weight nobody is told about.
MAX_DICTIONARY_WORD = 64
C_SPACE = b" \t\n\v\f\r"
HEX_DIGITS = b"0123456789abcdefABCDEF"

# The longest the PR fuzz job may hold a pull request, and the share of it
# the image build and the corpus download need before fuzzing starts.
PR_JOB_MINUTES = 30
PR_BUILD_SECONDS = 900


def _ids(path: Path) -> str:
    return path.stem


def compiles_under_the_coverage_stub(source: str) -> bool:
    try:
        compile(COVERAGE_STUB + source, "<harness>", "exec")
    except SyntaxError:
        return False
    return True


@pytest.mark.parametrize("harness", HARNESSES, ids=_ids)
def test_every_harness_compiles_under_the_coverage_stub(harness: Path) -> None:
    """A `from __future__` import must be the first statement, so a harness
    holding one is a SyntaxError once the stub is in front of it, and the
    coverage job reports on nothing."""
    assert compiles_under_the_coverage_stub(harness.read_text(encoding="utf-8"))


def test_the_coverage_stub_check_refuses_a_future_import() -> None:
    assert not compiles_under_the_coverage_stub(
        '"""Doc."""\nfrom __future__ import annotations\n'
    )
    assert compiles_under_the_coverage_stub('"""Doc."""\nimport sys\n')


def _dictionary_word(line: bytes) -> bytes:
    """One entry, read the way libFuzzer's ParseOneDictionaryEntry reads it."""
    left, right = 0, len(line) - 1
    while left < right and line[left] in C_SPACE:
        left += 1
    while right > left and line[right] in C_SPACE:
        right -= 1
    if right - left < 2 or line[right] != ord('"'):
        raise ValueError(f"not a quoted entry: {line!r}")
    right -= 1
    while left < right and line[left] != ord('"'):
        left += 1
    if left >= right:
        raise ValueError(f"no opening quote: {line!r}")
    word = bytearray()
    pos = left + 1
    while pos <= right:
        byte = line[pos]
        if not 0x20 <= byte <= 0x7E and byte not in C_SPACE:
            raise ValueError(f"unprintable byte {byte:#x}: {line!r}")
        if byte != ord("\\"):
            word.append(byte)
            pos += 1
        elif pos + 1 <= right and line[pos + 1] in b'\\"':
            word.append(line[pos + 1])
            pos += 2
        elif (
            pos + 3 <= right
            and line[pos + 1] == ord("x")
            and line[pos + 2] in HEX_DIGITS
            and line[pos + 3] in HEX_DIGITS
        ):
            word.append(int(line[pos + 2 : pos + 4], 16))
            pos += 4
        else:
            raise ValueError(f"invalid escape: {line!r}")
    return bytes(word)


def read_dictionary(text: bytes) -> list[bytes]:
    """libFuzzer's ParseDictionaryFile: a ValueError wherever libFuzzer
    prints `ParseDictionaryFile: error` and refuses to start the target."""
    if not text:
        raise ValueError("an empty dictionary")
    words = []
    for line in text.split(b"\n"):
        stripped = line.lstrip(C_SPACE)
        if not stripped or stripped.startswith(b"#"):
            continue
        words.append(_dictionary_word(line))
    return words


@pytest.mark.parametrize("harness", HARNESSES, ids=_ids)
def test_every_target_ships_a_dictionary_libfuzzer_loads_whole(harness: Path) -> None:
    dictionary = FUZZ_DIR / f"{harness.stem}.dict"
    assert dictionary.is_file(), f"{harness.name} has no {dictionary.name}"
    words = read_dictionary(dictionary.read_bytes())
    assert words
    too_long = [w for w in words if len(w) > MAX_DICTIONARY_WORD]
    assert not too_long, f"libFuzzer skips these without a word: {too_long}"
    assert len(words) == len(set(words)), "a word is listed twice"


def test_every_dictionary_belongs_to_a_target() -> None:
    targets = {h.stem for h in HARNESSES}
    assert {d.stem for d in FUZZ_DIR.glob("*.dict")} <= targets


@pytest.mark.parametrize(
    "line",
    [
        b'kw="\\q"',
        b'kw="\\x4"',
        b'"caf\xc3\xa9"',
        b'kw=""',
        b"kw=unquoted",
        b'kw="unterminated',
        b'"\x01"',
    ],
    ids=[
        "unknown-escape",
        "short-hex",
        "raw-utf8",
        "empty",
        "unquoted",
        "unterminated",
        "control-byte",
    ],
)
def test_the_dictionary_reader_refuses_what_libfuzzer_refuses(line: bytes) -> None:
    with pytest.raises(ValueError):
        read_dictionary(b"# a comment\n" + line + b"\n")


def test_the_dictionary_reader_decodes_what_libfuzzer_decodes() -> None:
    text = b'# c\n\n  kw="a\\\\b\\"c\\x41"\n"\t"\n'
    assert read_dictionary(text) == [b'a\\b"cA', b"\t"]


def _code(path: Path) -> str:
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def exact_pin(dockerfile: str, package: str) -> str | None:
    match = re.search(rf'"{re.escape(package)}==([0-9][0-9A-Za-z.]*)"', dockerfile)
    return match.group(1) if match else None


@pytest.mark.parametrize("package", ["atheris", "coverage", "pyinstaller"])
def test_the_image_pins_every_tool_it_installs(package: str) -> None:
    """An unpinned atheris is a different fuzzing engine from one day to the
    next, and a coverage.py newer than the data format the runner reads
    produces a report the runner cannot open."""
    assert exact_pin(_code(CFLITE_DIR / "Dockerfile"), package)


def test_the_pin_check_refuses_a_range() -> None:
    assert exact_pin('pip install "atheris>=2.3.0"', "atheris") is None
    assert exact_pin('pip install "atheris==3.1.0"', "atheris") == "3.1.0"


# --- build.sh, run against stand-ins for every tool it calls ----------------

_STUBS = {
    "python3": """#!/bin/bash
echo "python3 $*" >> "$STUB_LOG"
if [ "$1" = -c ]; then
  case "$2" in *SupportedLanguage*) echo tree_sitter_python ;; esac
fi
""",
    "uv": """#!/bin/bash
echo "uv $*" >> "$STUB_LOG"
if [ "$1" = export ]; then
  [ -n "${UV_EXPORT_FAILS:-}" ] && exit 1
  while [ $# -gt 0 ]; do
    [ "$1" = --output-file ] && echo "locked==1.0" > "$2"
    shift
  done
fi
exit 0
""",
    "compile_python_fuzzer": """#!/bin/bash
echo "compile_python_fuzzer $*" >> "$STUB_LOG"
target="$OUT/$(basename -s .py "$1")"
printf '#!/bin/bash\\necho "run $(basename "$0") $*" >> "$STUB_LOG"\\n[ "$(basename "$0")" != "${STUB_FAIL_TARGET:-}" ]\\n' > "$target"
chmod +x "$target"
""",
    "zip": """#!/bin/bash
echo "zip $*" >> "$STUB_LOG"
touch "$2"
""",
}


def _run_build(
    tmp_path: Path, **env: str
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    if sys.platform == "win32" or shutil.which("bash") is None:
        pytest.skip("build.sh runs only in the Linux build image")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in _STUBS.items():
        path = stubs / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    src, out, log = tmp_path / "src", tmp_path / "out", tmp_path / "log"
    src.mkdir()
    out.mkdir()
    log.touch()
    (src / "code-graph-rag").symlink_to(ROOT)
    result = subprocess.run(
        ["bash", "-eu", str(CFLITE_DIR / "build.sh")],
        cwd=ROOT,
        env={
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "SRC": str(src),
            "OUT": str(out),
            "STUB_LOG": str(log),
            "SANITIZER": "address",
            "HOME": str(tmp_path),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result, out, log.read_text(encoding="utf-8").splitlines()


def test_the_build_installs_the_locked_dependency_set(tmp_path: Path) -> None:
    result, _, log = _run_build(tmp_path)
    assert result.returncode == 0, result.stderr
    exports = [line for line in log if line.startswith("uv export")]
    assert len(exports) == 1 and "--frozen" in exports[0].split(), exports
    installs = [line for line in log if line.startswith("uv pip install")]
    assert len(installs) == 2, installs
    locked, project = (line.split() for line in installs)
    assert "-r" in locked and "--no-deps" not in locked
    assert project[-1] == "." and "--no-deps" in project, project
    assert not [line for line in log if " -m pip install" in line], (
        "a pip install resolves its own versions instead of the lockfile's"
    )


def test_the_build_fails_when_the_lockfile_cannot_be_exported(
    tmp_path: Path,
) -> None:
    """Falling back to an unlocked install would fuzz versions nobody ships."""
    result, out, log = _run_build(tmp_path, UV_EXPORT_FAILS="1")
    assert result.returncode != 0
    assert not [line for line in log if line.startswith("uv pip install")]
    assert not list(out.iterdir())


def test_the_build_ships_each_dictionary_and_starts_each_target_with_it(
    tmp_path: Path,
) -> None:
    """The smoke run passes the dictionary, so one libFuzzer would refuse
    fails the build that ships it, not the first fuzzing run."""
    result, out, log = _run_build(tmp_path)
    assert result.returncode == 0, result.stderr
    for harness in HARNESSES:
        shipped = out / f"{harness.stem}.dict"
        assert shipped.read_bytes() == (FUZZ_DIR / f"{harness.stem}.dict").read_bytes()
        runs = [line for line in log if line.startswith(f"run {harness.stem} ")]
        assert len(runs) == 1, (harness.stem, runs)
        assert f"-dict={shipped}" in runs[0].split(), runs[0]
        assert (out / f"{harness.stem}_seed_corpus.zip").exists()


def test_the_build_fails_when_a_target_does_not_start(tmp_path: Path) -> None:
    result, _, _ = _run_build(tmp_path, STUB_FAIL_TARGET="fuzz_parse_source")
    assert result.returncode != 0
    assert "fuzz_parse_source failed to run" in result.stderr


# --- workflows ----------------------------------------------------------------


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on") or workflow[True]


def _steps(workflow: dict[str, Any], action: str) -> list[tuple[str, dict[str, Any]]]:
    found = []
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            match = CFLITE_ACTION.match(step.get("uses", ""))
            if match and match.group(1) == action:
                found.append((job_name, step.get("with", {})))
    return found


def cflite_refs(workflow: dict[str, Any]) -> set[str]:
    refs = set()
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            match = CFLITE_ACTION.match(step.get("uses", ""))
            if match:
                refs.add(match.group(2))
    return refs


def test_every_clusterfuzzlite_workflow_runs_one_pinned_action_commit() -> None:
    refs = set().union(
        *(cflite_refs(yaml.safe_load(w.read_text())) for w in CFLITE_WORKFLOWS)
    )
    assert len(refs) == 1 and FULL_SHA.fullmatch(next(iter(refs))), refs


def test_the_pin_check_refuses_a_tag() -> None:
    workflow = {
        "jobs": {
            "j": {
                "steps": [{"uses": "google/clusterfuzzlite/actions/build_fuzzers@v1"}]
            }
        }
    }
    refs = cflite_refs(workflow)
    assert refs == {"v1"} and not FULL_SHA.fullmatch("v1")


@pytest.mark.parametrize("workflow", CFLITE_WORKFLOWS, ids=_ids)
def test_every_clusterfuzzlite_job_has_a_timeout(workflow: Path) -> None:
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    assert all(isinstance(j.get("timeout-minutes"), int) for j in jobs.values())


@pytest.mark.parametrize("workflow", CFLITE_WORKFLOWS, ids=_ids)
def test_every_run_names_python(workflow: Path) -> None:
    """The run action picks its reproduce timeout from `language`: python
    gets 120s, anything else 30s, too short for a frozen target to start."""
    for _, inputs in _steps(_workflow(workflow.name), "run_fuzzers"):
        assert inputs.get("language") == "python", inputs


def holds_a_pull_request(job: dict[str, Any], inputs: dict[str, Any]) -> bool:
    return (
        job["timeout-minutes"] > PR_JOB_MINUTES
        or int(inputs["fuzz-seconds"]) + PR_BUILD_SECONDS > job["timeout-minutes"] * 60
    )


def test_the_pr_job_fuzzes_every_core_within_its_bound() -> None:
    workflow = _workflow("cflite_pr.yml")
    [(job_name, inputs)] = _steps(workflow, "run_fuzzers")
    job = workflow["jobs"][job_name]
    assert inputs["mode"] == "code-change"
    assert not holds_a_pull_request(job, inputs)
    assert str(inputs.get("parallel-fuzzing")).lower() == "true"
    assert str(inputs.get("output-sarif")).lower() == "true"


def test_the_bound_check_refuses_a_budget_the_timeout_cannot_hold() -> None:
    assert holds_a_pull_request({"timeout-minutes": 30}, {"fuzz-seconds": 1200})
    assert holds_a_pull_request({"timeout-minutes": 60}, {"fuzz-seconds": 60})
    assert not holds_a_pull_request({"timeout-minutes": 30}, {"fuzz-seconds": 600})


def scheduled_seconds(value: str | int) -> int:
    """The budget a scheduled run gets: `${{ inputs.x || N }}` is N there,
    because a schedule event has no inputs."""
    if isinstance(value, int):
        return value
    match = re.fullmatch(r"\$\{\{\s*inputs\.fuzz-seconds\s*\|\|\s*(\d+)\s*\}\}", value)
    if match is None:
        raise ValueError(f"not a dispatch input with a default: {value!r}")
    return int(match.group(1))


def test_the_budget_reader_refuses_an_input_without_a_default() -> None:
    with pytest.raises(ValueError):
        scheduled_seconds("${{ inputs.fuzz-seconds }}")
    assert scheduled_seconds("${{ inputs.fuzz-seconds || 7200 }}") == 7200


# The jobs that run one target each, by workflow and run mode.
PER_TARGET_RUNS = [("cflite_batch.yml", "batch"), ("cflite_cron.yml", "prune")]


def _run_job(name: str, mode: str) -> tuple[dict[str, Any], dict[str, Any]]:
    workflow = _workflow(name)
    [(job_name, inputs)] = [
        (j, i) for j, i in _steps(workflow, "run_fuzzers") if i["mode"] == mode
    ]
    return workflow["jobs"][job_name], inputs


def test_the_batch_gives_every_target_a_job_of_its_own_on_every_core() -> None:
    """cifuzz's batch runner keeps going after a crash and writes SARIF for
    whichever target ran LAST, so several targets in one job report the
    wrong one. One target per job makes the report the target's own, and
    hands it the whole runner rather than a fifth of the budget."""
    workflow = _workflow("cflite_batch.yml")
    job, inputs = _run_job("cflite_batch.yml", "batch")
    assert sorted(job["strategy"]["matrix"]["target"]) == [h.stem for h in HARNESSES]
    assert str(inputs.get("parallel-fuzzing")).lower() == "true"
    assert str(inputs.get("output-sarif")).lower() == "true"
    seconds = scheduled_seconds(inputs["fuzz-seconds"])
    dispatch = _triggers(workflow)["workflow_dispatch"]["inputs"]["fuzz-seconds"]
    assert int(dispatch["default"]) == seconds >= 3600
    assert seconds + PR_BUILD_SECONDS <= job["timeout-minutes"] * 60


def test_the_prune_gives_every_target_a_job_of_its_own() -> None:
    """A merge that outlasts its budget raises, and cifuzz's prune loop does
    not catch it, so in one shared job a single slow target leaves every
    target after it unpruned for good."""
    job, inputs = _run_job("cflite_cron.yml", "prune")
    assert sorted(job["strategy"]["matrix"]["target"]) == [h.stem for h in HARNESSES]
    assert int(inputs["fuzz-seconds"]) + PR_BUILD_SECONDS <= job["timeout-minutes"] * 60


def _selection_script(name: str, mode: str) -> str:
    steps = _run_job(name, mode)[0]["steps"]
    build = next(i for i, s in enumerate(steps) if "build_fuzzers" in s.get("uses", ""))
    run = next(i for i, s in enumerate(steps) if "run_fuzzers" in s.get("uses", ""))
    [select] = [s for s in steps[build + 1 : run] if "run" in s]
    assert select["env"]["TARGET"] == "${{ matrix.target }}"
    return select["run"]


def _fake_build(build_out: Path, stems: list[str]) -> set[str]:
    """What compile_python_fuzzer and build.sh leave per target: an
    extensionless wrapper cifuzz detects by the libFuzzer symbol, the frozen
    executable beside it, and the files ClusterFuzzLite picks up by name."""
    build_out.mkdir()
    for stem in stems:
        wrapper = build_out / stem
        wrapper.write_text("#!/bin/sh\n# LLVMFuzzerTestOneInput\n", encoding="utf-8")
        wrapper.chmod(0o755)
        (build_out / f"{stem}.pkg").write_bytes(b"\x7fELF")
        (build_out / f"{stem}.pkg").chmod(0o755)
        for extra in (".dict", ".pkg.deps.zip", "_seed_corpus.zip"):
            (build_out / f"{stem}{extra}").write_bytes(b"")
    (build_out / "llvm-symbolizer").write_bytes(b"")
    return {p.name for p in build_out.iterdir()}


def _select(tmp_path: Path, run: tuple[str, str], stems: list[str], target: str):
    if sys.platform == "win32" or shutil.which("bash") is None:
        pytest.skip("the step runs on an ubuntu runner")
    before = _fake_build(tmp_path / "build-out", stems)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    sudo = stubs / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o755)
    script = tmp_path / "select.sh"
    script.write_text(_selection_script(*run), encoding="utf-8")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=tmp_path,
        env={
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "TARGET": target,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    after = {p.name for p in (tmp_path / "build-out").iterdir()}
    return result, before, after


@pytest.mark.parametrize("run", PER_TARGET_RUNS, ids=lambda r: r[1])
def test_a_per_target_job_keeps_only_its_own_target(
    tmp_path: Path, run: tuple[str, str]
) -> None:
    stems = [h.stem for h in HARNESSES] + ["fuzz_parse_source_more"]
    result, before, after = _select(tmp_path, run, stems, "fuzz_parse_source")
    assert result.returncode == 0, result.stderr
    kept = {
        n
        for n in before
        if n.split(".")[0].removesuffix("_seed_corpus") == "fuzz_parse_source"
    }
    assert after == kept | {"llvm-symbolizer"}, sorted(after)


@pytest.mark.parametrize("run", PER_TARGET_RUNS, ids=lambda r: r[1])
def test_a_per_target_job_fails_when_its_target_was_not_built(
    tmp_path: Path, run: tuple[str, str]
) -> None:
    """Otherwise a renamed harness leaves its job fuzzing nothing, green."""
    stems = [h.stem for h in HARNESSES]
    result, before, after = _select(tmp_path, run, stems, "fuzz_renamed_away")
    assert result.returncode != 0
    assert after == before


UPLOAD_SARIF = re.compile(r"^github/codeql-action/upload-sarif@[0-9a-f]{40}$")
SARIF_FILE = "cifuzz-sarif/results.sarif"


def uploads_its_sarif(job: dict[str, Any]) -> bool:
    """`output-sarif` only writes cifuzz-sarif/results.sarif into the
    workspace; nothing reaches code scanning unless a later step in the same
    job uploads it, and that step must run after the fuzz step has failed on
    a crash, and be skipped when the fuzz step never got far enough to write
    the file."""
    steps = job.get("steps", [])
    for index, step in enumerate(steps):
        match = CFLITE_ACTION.match(step.get("uses", ""))
        if not (match and match.group(1) == "run_fuzzers"):
            continue
        if str(step.get("with", {}).get("output-sarif")).lower() != "true":
            continue
        if not any(
            UPLOAD_SARIF.match(later.get("uses", ""))
            and later.get("with", {}).get("sarif_file") == SARIF_FILE
            and "always()" in str(later.get("if", ""))
            and f"hashFiles('{SARIF_FILE}')" in str(later.get("if", ""))
            for later in steps[index + 1 :]
        ):
            return False
        if job.get("permissions", {}).get("security-events") != "write":
            return False
    return True


@pytest.mark.parametrize("workflow", CFLITE_WORKFLOWS, ids=_ids)
def test_every_sarif_the_fuzzers_write_reaches_code_scanning(workflow: Path) -> None:
    for name, job in _workflow(workflow.name)["jobs"].items():
        assert uploads_its_sarif(job), name


def test_the_upload_check_refuses_a_sarif_left_on_disk() -> None:
    run = {
        "uses": "google/clusterfuzzlite/actions/run_fuzzers@" + "a" * 40,
        "with": {"output-sarif": True},
    }
    upload = {
        "uses": "github/codeql-action/upload-sarif@" + "b" * 40,
        "if": f"always() && hashFiles('{SARIF_FILE}') != ''",
        "with": {"sarif_file": SARIF_FILE},
    }
    allowed = {"security-events": "write"}
    assert uploads_its_sarif({"permissions": allowed, "steps": [run, upload]})
    assert not uploads_its_sarif({"permissions": allowed, "steps": [run]})
    assert not uploads_its_sarif({"permissions": allowed, "steps": [upload, run]})
    assert not uploads_its_sarif({"steps": [run, upload]})
    for broken in (
        {**upload, "if": f"hashFiles('{SARIF_FILE}') != ''"},
        {**upload, "if": "always()"},
        {**upload, "uses": "github/codeql-action/upload-sarif@v4"},
        {**upload, "with": {"sarif_file": "results.sarif"}},
    ):
        assert not uploads_its_sarif({"permissions": allowed, "steps": [run, broken]})


def test_the_cron_workflow_prunes_and_measures_coverage() -> None:
    workflow = _workflow("cflite_cron.yml")
    modes = {
        inputs["mode"]: (job, inputs) for job, inputs in _steps(workflow, "run_fuzzers")
    }
    assert set(modes) == {"prune", "coverage"}
    coverage_job, coverage_run = modes["coverage"]
    assert coverage_run["sanitizer"] == "coverage"
    [build] = [i for j, i in _steps(workflow, "build_fuzzers") if j == coverage_job]
    assert build["sanitizer"] == "coverage"
    # Coverage replays the corpora the prune has just merged, and still runs
    # when one target's prune failed.
    prune_job, _ = modes["prune"]
    coverage = workflow["jobs"][coverage_job]
    assert coverage["needs"] in (prune_job, [prune_job])
    assert "!cancelled()" in coverage["if"]
    assert {"schedule", "workflow_dispatch"} <= set(_triggers(workflow))


def test_main_builds_are_uploaded_for_pull_requests_to_compare_against() -> None:
    """Without a build of the base, a PR that meets a crash already on main
    is failed for it."""
    workflow = _workflow("cflite_build.yml")
    assert _triggers(workflow)["push"]["branches"] == ["main"]
    [(_, inputs)] = _steps(workflow, "build_fuzzers")
    assert str(inputs.get("upload-build")).lower() == "true"
