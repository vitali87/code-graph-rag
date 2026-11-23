# Rule 4 of the Rust rewrite (docs/reports/RUST_REWRITE_PLAN.md): no test
# disappears silently. A pull request that removes, renames or moves a test
# names its replacement in codebase_rag/tests/removed_tests.toml, and the lint
# job refuses one that does not (docs/reports/PHASE1_SEAMS.md, section 4).
# Each test here builds a small Git repository, commits a base and a head,
# and runs the check between them, as CI runs it between a pull request's
# base and its merge commit.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import test_removals as tr

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "test_removals.py"
_CI = _REPO / ".github" / "workflows" / "ci.yml"

_PYPROJECT = """\
[tool.pytest.ini_options]
testpaths = ["codebase_rag/tests"]
python_files = ["test_*.py", "*_test.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
"""
_A = "codebase_rag/tests/test_a.py"
_B = "codebase_rag/tests/test_b.py"
_LIB = "rust/demo/src/lib.rs"


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.entries: list[str] = []

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def delete(self, rel: str) -> None:
        (self.root / rel).unlink()

    def entry(
        self,
        test: str,
        *,
        replaced_by: list[str] | None = None,
        obsolete: str | None = None,
    ) -> None:
        lines = ["[[removed]]", f'test = "{test}"']
        if replaced_by is not None:
            quoted = ", ".join(f'"{name}"' for name in replaced_by)
            lines.append(f"replaced_by = [{quoted}]")
        if obsolete is not None:
            lines.append(f'obsolete = "{obsolete}"')
        lines += ['reason = "Checked the same thing another way."', "pull_request = 1"]
        self.entries.append("\n".join(lines) + "\n")
        self.write(tr.MAP_PATH, "\n".join(self.entries))

    def commit(self) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "change")
        return self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def problems(self, base: str, **kwargs: int) -> list[tuple[tr.Rule, str]]:
        report = tr.check(self.root, base, **kwargs)
        return [(problem.rule, problem.subject) for problem in report.problems]


@pytest.fixture
def repo(git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Repo:
    # Set for the whole process, not per call: the check runs Git itself and
    # inherits it. An inherited GIT_DIR would point those calls elsewhere,
    # and a global config could sign commits or install hooks.
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    absent = str(tmp_path / "gitconfig-absent")
    for name, value in {
        "GIT_CONFIG_GLOBAL": absent,
        "GIT_CONFIG_SYSTEM": absent,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(name, value)
    made = _Repo(git_repo)
    made.write("pyproject.toml", _PYPROJECT)
    return made


def _functions(*names: str) -> str:
    return "".join(f"def {name}():\n    pass\n\n\n" for name in names)


def _rust(*names: str) -> str:
    tests = "".join(f"    #[test]\n    fn {name}() {{}}\n" for name in names)
    return f"#[cfg(test)]\nmod tests {{\n{tests}}}\n"


def test_adding_tests_and_changing_none_passes(repo: _Repo) -> None:
    repo.write(_A, _functions("test_one"))
    repo.write(_LIB, _rust("one"))
    base = repo.commit()
    repo.write(_B, _functions("test_two"))
    repo.commit()

    report = tr.check(repo.root, base)

    assert report.problems == ()
    assert (report.base_tests, report.head_tests) == (2, 3)


def test_a_removal_without_an_entry_fails_and_prints_the_entry_to_add(
    repo: _Repo,
) -> None:
    repo.write(_A, "class TestThing:\n    def test_one(self):\n        pass\n")
    repo.write(_B, _functions("test_kept", "test_gone"))
    base = repo.commit()
    repo.write(_A, "class TestThing:\n    pass\n")
    repo.write(_B, _functions("test_kept"))
    repo.commit()

    report = tr.check(repo.root, base, pull_request=4321)

    assert [(p.rule, p.subject) for p in report.problems] == [
        (tr.Rule.NO_ENTRY, f"{_A}::TestThing::test_one"),
        (tr.Rule.NO_ENTRY, f"{_B}::test_gone"),
    ]
    message = report.problems[1].message
    assert tr.MAP_PATH in message
    assert (
        f'[[removed]]\ntest = "{_B}::test_gone"\nreplaced_by = ["<' in message
        and "\npull_request = 4321" in message
    )


def test_a_removal_with_an_entry_passes(repo: _Repo) -> None:
    repo.write(_A, _functions("test_registry_holds_method"))
    base = repo.commit()
    repo.delete(_A)
    repo.write(_B, _functions("test_graph_defines_method"))
    repo.entry(
        f"{_A}::test_registry_holds_method",
        replaced_by=[f"{_B}::test_graph_defines_method"],
    )
    repo.commit()

    assert repo.problems(base) == []


def test_a_replacement_the_head_does_not_have_fails(repo: _Repo) -> None:
    repo.write(_A, _functions("test_one"))
    base = repo.commit()
    repo.delete(_A)
    repo.entry(f"{_A}::test_one", replaced_by=[f"{_B}::test_never_written"])
    repo.commit()

    assert repo.problems(base) == [(tr.Rule.MISSING_REPLACEMENT, f"{_A}::test_one")]


def test_a_chain_of_replacements_is_followed_to_a_test_on_the_head(
    repo: _Repo,
) -> None:
    repo.write(_A, _functions("test_old"))
    repo.commit()
    repo.write(_A, _functions("test_middle"))
    repo.entry(f"{_A}::test_old", replaced_by=[f"{_A}::test_middle"])
    base = repo.commit()
    repo.write(_A, _functions("test_new"))
    repo.entry(f"{_A}::test_middle", replaced_by=[f"{_A}::test_new"])
    repo.commit()

    assert repo.problems(base) == []


def test_a_chain_broken_by_a_removal_without_an_entry_fails_twice(
    repo: _Repo,
) -> None:
    repo.write(_A, _functions("test_old"))
    repo.commit()
    repo.write(_A, _functions("test_middle"))
    repo.entry(f"{_A}::test_old", replaced_by=[f"{_A}::test_middle"])
    base = repo.commit()
    repo.write(_A, _functions("test_new"))
    repo.commit()

    assert repo.problems(base) == [
        (tr.Rule.NO_ENTRY, f"{_A}::test_middle"),
        (tr.Rule.MISSING_REPLACEMENT, f"{_A}::test_old"),
    ]


def test_a_chain_that_loops_back_fails(repo: _Repo) -> None:
    repo.write(_A, _functions("test_one", "test_two"))
    base = repo.commit()
    repo.delete(_A)
    repo.entry(f"{_A}::test_one", replaced_by=[f"{_A}::test_two"])
    repo.entry(f"{_A}::test_two", replaced_by=[f"{_A}::test_one"])
    repo.commit()

    assert repo.problems(base) == [
        (tr.Rule.REPLACEMENT_LOOP, f"{_A}::test_one"),
        (tr.Rule.REPLACEMENT_LOOP, f"{_A}::test_two"),
    ]


def test_an_entry_for_a_test_still_on_the_head_fails(repo: _Repo) -> None:
    repo.write(_A, _functions("test_one", "test_two"))
    base = repo.commit()
    repo.entry(f"{_A}::test_one", replaced_by=[f"{_A}::test_two"])
    repo.commit()

    assert repo.problems(base) == [(tr.Rule.STALE_ENTRY, f"{_A}::test_one")]


def test_obsolete_passes_when_the_code_goes_in_the_same_pull_request(
    repo: _Repo,
) -> None:
    repo.write("codebase_rag/bridge/walk.py", "WALK = 1\n")
    repo.write(_A, _functions("test_walk"))
    base = repo.commit()
    repo.delete("codebase_rag/bridge/walk.py")
    repo.delete(_A)
    repo.entry(f"{_A}::test_walk", obsolete="codebase_rag/bridge/walk.py")
    repo.commit()

    assert repo.problems(base) == []


@pytest.mark.parametrize(
    ("obsolete", "rule"),
    [
        pytest.param(
            "codebase_rag/bridge/walk.py", tr.Rule.OBSOLETE_PRESENT, id="still-there"
        ),
        pytest.param("codebase_rag/brigde", tr.Rule.OBSOLETE_NOT_DELETED, id="typo"),
    ],
)
def test_obsolete_fails_unless_this_pull_request_deletes_the_code(
    repo: _Repo, obsolete: str, rule: tr.Rule
) -> None:
    repo.write("codebase_rag/bridge/walk.py", "WALK = 1\n")
    repo.write(_A, _functions("test_walk"))
    base = repo.commit()
    repo.delete(_A)
    repo.entry(f"{_A}::test_walk", obsolete=obsolete)
    repo.commit()

    assert repo.problems(base) == [(rule, f"{_A}::test_walk")]


def test_a_renamed_test_counts_as_removed_and_its_new_id_is_suggested(
    repo: _Repo,
) -> None:
    repo.write(_A, _functions("test_receivers"))
    base = repo.commit()
    repo.delete(_A)
    repo.write("codebase_rag/tests/go/test_moved.py", _functions("test_receivers"))
    repo.commit()

    report = tr.check(repo.root, base)

    assert [(p.rule, p.subject) for p in report.problems] == [
        (tr.Rule.NO_ENTRY, f"{_A}::test_receivers")
    ]
    assert (
        'replaced_by = ["codebase_rag/tests/go/test_moved.py::test_receivers"]'
        in report.problems[0].message
    )

    repo.entry(
        f"{_A}::test_receivers",
        replaced_by=["codebase_rag/tests/go/test_moved.py::test_receivers"],
    )
    repo.commit()

    assert repo.problems(base) == []


def test_a_removed_rust_test_needs_an_entry_too(repo: _Repo) -> None:
    repo.write(_LIB, _rust("parses_match", "parses_return"))
    base = repo.commit()
    repo.write(_LIB, _rust("parses_match"))
    repo.commit()

    assert repo.problems(base) == [(tr.Rule.NO_ENTRY, f"{_LIB}::parses_return")]

    repo.write("rust/demo/tests/parser.rs", _rust("parses_return_clause"))
    repo.entry(
        f"{_LIB}::parses_return",
        replaced_by=["rust/demo/tests/parser.rs::parses_return_clause"],
    )
    repo.commit()

    assert repo.problems(base) == []


def test_a_dropped_parametrize_case_is_left_to_review(repo: _Repo) -> None:
    case = (
        "import pytest\n\n\n"
        '@pytest.mark.parametrize("x", [{cases}])\n'
        "def test_cases(x):\n    pass\n"
    )
    repo.write(_A, case.format(cases="1, 2"))
    base = repo.commit()
    repo.write(_A, case.format(cases="1"))
    repo.commit()

    assert repo.problems(base) == []


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(
            'test = "a.py::test_x"\nreplaced_by = ["b.py::test_y"]\npull_request = 1\n',
            id="no-reason",
        ),
        pytest.param(
            'test = "a.py::test_x"\nreason = "r"\npull_request = 1\n',
            id="no-replacement",
        ),
        pytest.param(
            'test = "a.py::test_x"\nreplaced_by = ["b.py::test_y"]\n'
            'obsolete = "a.py"\nreason = "r"\npull_request = 1\n',
            id="both",
        ),
        pytest.param(
            'test = "a.py::test_x"\nreplaced_by = ["b.py::test_y[1]"]\n'
            'reason = "r"\npull_request = 1\n',
            id="parameters",
        ),
        pytest.param(
            'test = "a.py::test_x"\nreplaced_by = []\nreason = "r"\npull_request = 1\n',
            id="empty",
        ),
        pytest.param(
            'test = "a.py::test_x"\nreplaced_by = ["b.py::test_y"]\n'
            'reason = "r"\npull_request = "1"\n',
            id="number-as-text",
        ),
        pytest.param(
            'test = "a.py"\nreplaced_by = ["b.py::test_y"]\n'
            'reason = "r"\npull_request = 1\n',
            id="not-a-test-id",
        ),
        pytest.param(
            'test = "a.py::test_x"\nreplaced_by = ["b.py::test_y"]\n'
            'reason = "r"\npull_request = 1\nreviewer = "me"\n',
            id="unknown-key",
        ),
    ],
)
def test_a_malformed_entry_is_refused(entry: str) -> None:
    _, problems = tr.read_map(f"[[removed]]\n{entry}".encode())

    assert [problem.rule for problem in problems] == [tr.Rule.BAD_ENTRY]


def test_two_entries_for_one_test_are_refused() -> None:
    entry = (
        '[[removed]]\ntest = "a.py::test_x"\nreplaced_by = ["b.py::test_y"]\n'
        'reason = "r"\npull_request = 1\n'
    )

    entries, problems = tr.read_map((entry + "\n" + entry).encode())

    assert len(entries) == 1
    assert [(p.rule, p.subject) for p in problems] == [
        (tr.Rule.BAD_ENTRY, "a.py::test_x")
    ]


def test_the_checked_in_map_is_well_formed() -> None:
    text = (_REPO / tr.MAP_PATH).read_bytes()

    _, problems = tr.read_map(text)

    assert problems == []


def test_the_python_listing_is_what_pytest_collects(repo: _Repo) -> None:
    repo.write(
        _A,
        "import sys\n\nimport pytest\n\n\n"
        "def helper():\n    pass\n\n\n"
        "def test_plain():\n    pass\n\n\n"
        "async def test_async():\n    pass\n\n\n"
        "@pytest.fixture\ndef test_named_like_a_test():\n    return 1\n\n\n"
        '@pytest.mark.parametrize("x", [1, 2])\ndef test_cases(x):\n    pass\n\n\n'
        "if sys.version_info >= (3, 0):\n\n    def test_under_if():\n        pass\n\n\n"
        "try:\n\n    def test_under_try():\n        pass\n\n"
        "except ImportError:\n    pass\n\n\n"
        "class _Mixin:\n    def test_inherited(self):\n        pass\n\n\n"
        "class TestOuter(_Mixin):\n"
        "    def test_method(self):\n        pass\n\n"
        "    def helper(self):\n        pass\n\n"
        "    class TestInner:\n        def test_nested(self):\n            pass\n\n"
        "    class Helper:\n        def test_not_collected(self):\n            pass\n\n\n"
        "class TestWithInit:\n    def __init__(self):\n        pass\n\n"
        "    def test_never(self):\n        pass\n\n\n"
        "class TestChild(TestOuter):\n    def test_own(self):\n        pass\n\n\n"
        "class NotATest:\n    def test_x(self):\n        pass\n\n\n"
        "def outer():\n    def test_inner():\n        pass\n",
    )
    repo.write("codebase_rag/tests/sub/thing_test.py", _functions("test_sub"))
    repo.write("codebase_rag/tests/sub/helpers.py", _functions("test_helper"))
    repo.write("other/test_outside.py", _functions("test_outside"))
    repo.commit()
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    env["PYTHONPATH"] = str(_REPO)

    collected = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            "pyproject.toml",
            "--rootdir",
            ".",
        ],
        cwd=repo.root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    pytest_ids = {
        line.split("[", 1)[0] for line in collected.stdout.splitlines() if "::" in line
    }
    assert collected.returncode == 0, collected.stdout + collected.stderr
    assert tr.list_tests(repo.root, "HEAD") == pytest_ids
    assert f"{_A}::TestChild::TestInner::test_nested" in pytest_ids


def test_the_python_listing_follows_each_trees_own_pyproject(repo: _Repo) -> None:
    repo.write(
        "pyproject.toml",
        "[tool.pytest.ini_options]\n"
        'testpaths = "checks"\n'
        'python_files = ["check_*.py"]\n'
        'python_classes = ["Spec"]\n'
        'python_functions = ["check_*"]\n'
        'norecursedirs = ["fixture*"]\n',
    )
    repo.write(
        "checks/check_a.py",
        _functions("check_one", "test_one")
        + "class SpecThing:\n    def check_two(self):\n        pass\n",
    )
    repo.write("checks/test_b.py", _functions("check_three"))
    repo.write("checks/fixtures_repo/check_c.py", _functions("check_four"))
    repo.write("elsewhere/check_d.py", _functions("check_five"))
    repo.commit()

    assert tr.list_tests(repo.root, "HEAD") == {
        "checks/check_a.py::check_one",
        "checks/check_a.py::SpecThing::check_two",
    }


def test_the_rust_listing_skips_comments_and_strings() -> None:
    source = r"""
// #[test] fn line_comment() {}
/* outer /* nested */ #[test] fn block_comment() {} */
const S: &str = "#[test] fn in_string() {}";
const R: &str = r#"#[test] fn in_raw_string() {}"#;
const Q: char = '"';

#[test]
fn plain() {}

#[test]
#[should_panic(expected = "boom]")]
pub fn after_another_attribute() {}

#[tokio::test]
async fn asynchronous() {}

fn helper<'a>(x: &'a str) -> &'a str { x }

#[cfg(test)]
mod tests {
    #[test] fn on_one_line() { let _ = '\''; }
}
"""

    assert tr.rust_tests("rust/x/src/lib.rs", source) == {
        "rust/x/src/lib.rs::plain",
        "rust/x/src/lib.rs::after_another_attribute",
        "rust/x/src/lib.rs::asynchronous",
        "rust/x/src/lib.rs::on_one_line",
    }


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "check", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_command_fetches_a_base_a_shallow_clone_lacks(
    repo: _Repo, tmp_path: Path
) -> None:
    repo.write(_A, _functions("test_one", "test_two"))
    base = repo.commit()
    repo.write(_A, _functions("test_one"))
    repo.commit()
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", repo.root.as_uri(), str(clone)],
        check=True,
        capture_output=True,
    )

    refused = _run(clone, "--base", base, "--pull-request", "4321")

    assert refused.returncode == 1, refused.stderr
    assert f'test = "{_A}::test_two"' in refused.stdout
    assert "pull_request = 4321" in refused.stdout


def test_a_base_that_reads_as_an_option_never_reaches_git_fetch(
    repo: _Repo, tmp_path: Path
) -> None:
    repo.write(_A, _functions("test_one"))
    repo.commit()
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", repo.root.as_uri(), str(clone)],
        check=True,
        capture_output=True,
    )

    refused = _run(clone, "--base=--upload-pack=touch planted")

    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert not (clone / "planted").exists()
    assert not (repo.root / "planted").exists()


def test_the_command_exits_0_on_a_clean_change_and_2_when_it_cannot_run(
    repo: _Repo,
) -> None:
    repo.write(_A, _functions("test_one"))
    base = repo.commit()
    repo.write(_B, _functions("test_two"))
    repo.commit()

    passed = _run(repo.root, "--base", base)
    unknown = _run(repo.root, "--base", "0" * 40)

    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert unknown.returncode == 2, unknown.stdout + unknown.stderr


def test_the_lint_job_checks_every_pull_request_against_its_base() -> None:
    steps = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]["lint"]["steps"]

    step = next(s for s in steps if "scripts/test_removals.py" in s.get("run", ""))

    assert step["if"] == "github.event_name == 'pull_request'"
    assert step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert step["env"]["PR"] == "${{ github.event.pull_request.number }}"
    assert 'check --base "$BASE_SHA" --pull-request "$PR"' in step["run"]
