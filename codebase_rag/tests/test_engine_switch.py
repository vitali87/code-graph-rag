"""The engine's frame switched on, as users run it.

Phase 2's last step (docs/reports/PHASE2_FRAME.md, sections 4 and 13) turns
`engine_frame.ENABLED` on with no check in place: Python's walk, hashes,
change detection and structure run beside the frame's on every index. A
run that finds no difference goes on with the frame's answers. The first
difference, or anything the frame raises, a panic in the extension
included, is named in the run's one warning, and the run goes on with
Python's answers from that hook on, the frame asked no more, so its index
is the one Python alone makes. Each of the frame's answers is planted with
a difference, and made to raise, in turn, on a first index and on a sync
after an edit.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path
from typing import NamedTuple

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import engine_frame
from codebase_rag.checkout_state import state_file
from codebase_rag.tests.conftest import _MockIngestor, create_and_run_updater

# The engine's methods a run asks, one per answer of the frame's, in the
# order it asks them: the in-sync check (on a sync of an indexed checkout),
# the structure, the change detection's baseline, the file plan, the change
# detection, the File rows and the module-name plan.
FIRST_INDEX = (
    "structure",
    "change_baseline",
    "file_plan",
    "changes",
    "file_rows",
    "module_plan",
)
SYNC = ("in_sync", *FIRST_INDEX)
ANSWERS = frozenset(SYNC)
# What every one of the frame's warnings starts with (logs.py).
FRAME_WARNING = "The Rust engine's "
FILES = {
    "a.py": "def a():\n    return 1\n",
    "pkg/__init__.py": "",
    "pkg/b.py": "def b():\n    return 2\n",
    "pkg/sub/c.py": "C = 3\n",
    "docs/notes.md": "# notes\n",
}
EDITED = "pkg/b.py"
# How a run's writes are compared across checkouts and clocks.
REPO_SHOWN = "<repo>"
SYNC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+\+00:00")
SYNC_TIME_SHOWN = "<time>"


class Scenario(NamedTuple):
    """A run the frame is asked in: a first index, or a sync after an edit,
    and the engine's methods it asks."""

    name: str
    asks: tuple[str, ...]


FIRST = Scenario("first index", FIRST_INDEX)
EDIT = Scenario("sync after an edit", SYNC)


def _flip_in_sync(answer: bool) -> bool:
    return not answer


def _drop_last_row(answer: tuple[list, list]) -> tuple[list, list]:
    elements, rows = answer
    return elements, rows[:-1]


def _drop_last_file(answer: tuple[list, ...]) -> tuple[list, ...]:
    files, dirs, symlinks, special, undecodable, hashes = answer
    return files[:-1], dirs, symlinks, special, undecodable, hashes[:-1]


def _flip_full_build(answer: tuple) -> tuple:
    cache_mtime, full_build, *rest = answer
    return (cache_mtime, not full_build, *rest)


def _lose_last_row(answer: list) -> list:
    return [*answer[:-1], None]


def _drop_first_module(answer: list) -> list:
    return answer[1:]


# A difference planted in each answer the frame gives: the raw answer, as
# the engine hands it to Python, changed.
DIFFERENCES: dict[str, Callable] = {
    "in_sync": _flip_in_sync,
    "structure": _drop_last_row,
    "file_plan": _drop_last_file,
    "changes": _flip_full_build,
    "file_rows": _lose_last_row,
    "module_plan": _drop_first_module,
}


class Planted:
    """The process's engine with each call to an answer's method recorded,
    and one method's answer changed, or replaced by an error."""

    def __init__(
        self,
        real: engine_frame.Engine,
        method: str | None = None,
        change: Callable | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.real = real
        self._method = method
        self._change = change
        self._error = error
        self.asked: list[str] = []

    def __getattr__(self, name: str) -> object:
        found = getattr(self.real, name)
        if name not in ANSWERS:
            return found

        def call(*args: object, **kwargs: object) -> object:
            self.asked.append(name)
            answer = found(*args, **kwargs)
            if name != self._method:
                return answer
            if self._error is not None:
                raise self._error
            assert self._change is not None
            return self._change(answer)

        return call


class Answers:
    """What each of the frame's hooks returned on the runs it watched:
    whether the run went on with Python's answer, the object it was given."""

    def __init__(self) -> None:
        self.used: list[tuple[str, bool]] = []

    def python_s(self) -> list[str]:
        return [name for name, python in self.used if python]

    def frame_s(self) -> list[str]:
        return [name for name, python in self.used if not python]


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frame as users run it: on, with no check in place, which the
    `rust` and `both` cells put in for their whole session."""
    monkeypatch.setattr(engine_frame, "ENABLED", True)
    monkeypatch.setattr(engine_frame, "CHECK", None)


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Answers:
    """Each hook watched, as the updater and the structure pass call them."""
    found = Answers()

    def watch(name: str, used_of: Callable[[object], object]) -> None:
        original = getattr(engine_frame, name)

        def watched(*args: object) -> object:
            returned = original(*args)
            found.used.append((name, used_of(returned) is args[-1]))
            return returned

        monkeypatch.setattr(engine_frame, name, watched)

    for name in ("plan_for_run", "changes_for_run", "file_rows_for_run"):
        watch(name, _itself)
    watch("structure_for_run", _first)
    return found


def _itself(returned: object) -> object:
    return returned


def _first(returned: object) -> object:
    assert isinstance(returned, tuple)
    return returned[0]


def _plant(monkeypatch: pytest.MonkeyPatch, planted: Planted) -> Planted:
    monkeypatch.setattr(engine_frame, "engine", lambda: planted)
    # The suite's frame checks read the engine type's constants off
    # `type(engine())`, so the stand-in's type carries them too.
    for name, value in vars(type(planted.real)).items():
        if name.isupper():
            monkeypatch.setattr(Planted, name, value, raising=False)
    return planted


def _real_engine() -> engine_frame.Engine:
    made = engine_frame.new_engine()
    # The checkout builds its own crate, so the frame is always there.
    assert made is not None, "the extension lacks the engine; rebuild the crate"
    return made


def _repo(root: Path) -> Path:
    repo = root / "repo"
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=cs.ENCODING_UTF8)
    return repo


def _edit(repo: Path) -> None:
    path = repo / EDITED
    path.write_text("def b():\n    return 3\n", encoding=cs.ENCODING_UTF8)
    # Past the cache's own mtime, whatever the file system's resolution.
    stamp = path.stat()
    os.utime(path, (stamp.st_atime + 5, stamp.st_mtime + 5))


class Indexed(NamedTuple):
    """What a run left: every write it sent the graph, with the checkout's
    path written as `<repo>`, its hash cache, its warnings from the frame,
    and whether it found the checkout in sync."""

    writes: list[str]
    cache: dict[str, str]
    warnings: list[str]
    in_sync: bool


def _indexed(repo: Path) -> Indexed:
    """Run once over `repo`, as `cgr start` does on a checkout."""
    warned: list[str] = []
    sink = logger.add(lambda message: warned.append(str(message)), level="WARNING")
    ingestor = _MockIngestor()
    try:
        updater = create_and_run_updater(repo, ingestor)
    finally:
        logger.remove(sink)
    # The graph's paths are POSIX-spelled (`C:/...` on Windows), while the
    # checkout's own spelling, raw or as a repr escapes it, can appear in
    # other writes; each stands for the checkout, so two runs in different
    # folders compare equal.
    resolved = repo.resolve()
    roots = (resolved.as_posix(), repr(str(resolved))[1:-1], str(resolved))
    cache = state_file(repo, cs.HASH_CACHE_FILENAME)
    return Indexed(
        writes=sorted(
            SYNC_TIME.sub(SYNC_TIME_SHOWN, _with_root_shown(str(call), roots))
            for call in ingestor.method_calls
        ),
        cache=json.loads(cache.read_text(encoding=cs.ENCODING_UTF8)),
        warnings=[line for line in warned if FRAME_WARNING in line],
        in_sync=updater.skipped_because_in_sync,
    )


def _prepared(root: Path, scenario: Scenario) -> Path:
    """A checkout ready for the scenario's run: a fresh one, or one indexed
    once, with the frame as it stands, and then edited."""
    repo = _repo(root)
    if scenario is EDIT:
        _indexed(repo)
        _edit(repo)
    return repo


def _with_root_shown(text: str, roots: tuple[str, ...]) -> str:
    """`text` with each spelling of the checkout's root replaced by one mark."""
    for root in roots:
        text = text.replace(root, REPO_SHOWN)
    return text


def _python_alone(root: Path, scenario: Scenario) -> Indexed:
    """The scenario's run as Python alone makes it: the frame off."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(engine_frame, "ENABLED", False)
        patch.setattr(engine_frame, "CHECK", None)
        return _indexed(_prepared(root, scenario))


# The hooks `Answers` watches, by the engine's method each asks: the ones
# whose answer is an object the run goes on with. The in-sync verdict is a
# bool, the same object on both sides when they agree, and the module names
# are always Python's (section 3).
WATCHED = {
    "structure": "structure_for_run",
    "file_plan": "plan_for_run",
    "changes": "changes_for_run",
    "file_rows": "file_rows_for_run",
}


def _watched_before(scenario: Scenario, method: str | None) -> list[str]:
    """The watched hooks a run asks before `method`, in order: every one
    of them when `method` is None."""
    asks = list(scenario.asks)
    before = asks if method is None else asks[: asks.index(method)]
    return [WATCHED[name] for name in before if name in WATCHED]


def _asked_through(scenario: Scenario, method: str) -> list[str]:
    asks = list(scenario.asks)
    return asks[: asks.index(method) + 1]


class PanicException(BaseException):
    """What pyo3 raises for a Rust panic, by the module and name it gives
    it, for an install without the ast-grep binding to panic for real."""


PanicException.__module__ = "pyo3_runtime"


@cache
def _panic_type() -> type[BaseException]:
    """The type pyo3 raises a Rust panic as: the ast-grep binding's own,
    which panics on a language it has no grammar for, when the extra is
    installed. Each pyo3 extension makes a type of its own, of one module
    and name."""
    try:
        import ast_grep_py
    except ImportError:
        return PanicException
    try:
        ast_grep_py.SgRoot("select 1", "sql")
    except BaseException as error:
        assert not isinstance(error, Exception)
        return type(error)
    raise AssertionError("ast-grep parsed a language it has no grammar for")


FAILURE_MESSAGE = "boom"
SCENARIOS = [FIRST, EDIT]
SCENARIO_IDS = [scenario.name for scenario in SCENARIOS]


def _cases(methods: frozenset[str]) -> Iterator[object]:
    for scenario in SCENARIOS:
        for method in scenario.asks:
            if method in methods:
                yield pytest.param(scenario, method, id=f"{scenario.name}-{method}")


class TestARun:
    @pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
    def test_with_no_difference_goes_on_with_the_frame_s_answers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        production: None,
        answers: Answers,
        scenario: Scenario,
    ) -> None:
        python = _python_alone(tmp_path / "python", scenario)
        repo = _prepared(tmp_path / "frame", scenario)
        answers.used.clear()
        planted = _plant(monkeypatch, Planted(_real_engine()))

        run = _indexed(repo)

        assert planted.asked == list(scenario.asks)
        assert run.warnings == []
        assert answers.frame_s() == _watched_before(scenario, None)
        assert answers.python_s() == []
        assert run.writes == python.writes
        assert run.cache == python.cache
        assert run.in_sync is python.in_sync is False

    @pytest.mark.parametrize(("scenario", "method"), _cases(frozenset(DIFFERENCES)))
    def test_with_a_difference_warns_once_and_goes_on_with_python_s_answers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        production: None,
        answers: Answers,
        scenario: Scenario,
        method: str,
    ) -> None:
        python = _python_alone(tmp_path / "python", scenario)
        repo = _prepared(tmp_path / "frame", scenario)
        answers.used.clear()
        planted = _plant(
            monkeypatch, Planted(_real_engine(), method, DIFFERENCES[method])
        )

        run = _indexed(repo)

        # Asked up to the difference, and no more.
        assert planted.asked == _asked_through(scenario, method)
        (warning,) = run.warnings
        assert str(repo) in warning
        assert "this run uses Python's" in warning
        assert answers.frame_s() == _watched_before(scenario, method)
        assert run.writes == python.writes
        assert run.cache == python.cache
        assert run.in_sync is python.in_sync is False

    @pytest.mark.parametrize("failure", ["panic", "error"])
    @pytest.mark.parametrize(("scenario", "method"), _cases(ANSWERS))
    def test_whose_frame_raises_warns_once_and_goes_on_with_python_s_answers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        production: None,
        answers: Answers,
        scenario: Scenario,
        method: str,
        failure: str,
    ) -> None:
        kind = _panic_type() if failure == "panic" else RuntimeError
        error = kind(FAILURE_MESSAGE)
        python = _python_alone(tmp_path / "python", scenario)
        repo = _prepared(tmp_path / "frame", scenario)
        answers.used.clear()
        planted = _plant(monkeypatch, Planted(_real_engine(), method, error=error))

        run = _indexed(repo)

        assert planted.asked == _asked_through(scenario, method)
        (warning,) = run.warnings
        assert str(repo) in warning
        assert f"raised\t{kind.__name__}: {FAILURE_MESSAGE}" in warning
        assert answers.frame_s() == _watched_before(scenario, method)
        assert run.writes == python.writes
        assert run.cache == python.cache
        assert run.in_sync is python.in_sync is False

    def test_goes_back_to_python_s_answers_for_itself_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, production: None
    ) -> None:
        repo = _repo(tmp_path)
        planted = _plant(
            monkeypatch, Planted(_real_engine(), "file_plan", _drop_last_file)
        )
        first = _indexed(repo)
        _edit(repo)
        planted.asked.clear()
        second = _indexed(repo)
        clean = _plant(monkeypatch, Planted(_real_engine()))
        (repo / "a.py").write_text("def a():\n    return 4\n", encoding="utf-8")
        _edit(repo)
        third = _indexed(repo)

        # One warning in each run that differs, and the run after them asks
        # the frame for every answer again.
        assert len(first.warnings) == 1
        assert len(second.warnings) == 1
        assert planted.asked == _asked_through(EDIT, "file_plan")
        assert third.warnings == []
        assert clean.asked == list(EDIT.asks)


class TestOneRun:
    def test_shares_one_warning_and_a_hook_outside_one_is_a_run_of_its_own(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, production: None
    ) -> None:
        repo = _repo(tmp_path)
        _plant(monkeypatch, Planted(_real_engine(), "file_plan", _drop_last_file))
        python = engine_frame.python_file_plan(repo)

        def twice() -> tuple[list[engine_frame.FilePlan], list[str]]:
            warned: list[str] = []
            sink = logger.add(
                lambda message: warned.append(str(message)), level="WARNING"
            )
            try:
                used = [
                    engine_frame.plan_for_run(repo, None, None, python)
                    for _ in range(2)
                ]
            finally:
                logger.remove(sink)
            return used, [line for line in warned if FRAME_WARNING in line]

        outside, warned_outside = twice()
        inside, warned_inside = engine_frame.one_run(twice)()

        assert all(used is python for used in [*outside, *inside])
        assert len(warned_outside) == 2
        assert len(warned_inside) == 1

    def test_an_interrupt_from_the_frame_still_stops_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, production: None
    ) -> None:
        repo = _repo(tmp_path)
        _plant(
            monkeypatch,
            Planted(_real_engine(), "file_plan", error=KeyboardInterrupt()),
        )

        with pytest.raises(KeyboardInterrupt):
            engine_frame.plan_for_run(
                repo, None, None, engine_frame.python_file_plan(repo)
            )
