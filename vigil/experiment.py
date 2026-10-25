import time
from pathlib import Path

from loguru import logger

from . import bench, gate
from . import constants as vc
from . import logs as ls
from .decide import Decision, Findings, decide_adopt, decide_upgrade
from .impact import cone, import_roots, installed_files, tests_for_paths
from .ledger import Ledger
from .types_defs import Cone, Item, Outcome, SuiteRun
from .workspace import (
    WorkspaceError,
    add_worktree,
    changed_paths,
    commit_all,
    corpus_checkout,
    lock_upgrade,
    remove_worktree,
    rewrite_requirement,
    run,
    slug,
    sync,
    work_dir,
)


class Night:
    """What one night's experiments share: the commit every candidate is cut
    from, the pinned corpora, and one synced baseline per venv profile.

    Baselines are measured in the same container on the same night as the
    candidates they judge, never carried over from an earlier night: a cloud
    machine can change hardware between sessions, and a baseline timed on
    other hardware would make every candidate look faster or slower.
    """

    def __init__(self, repo: Path, night: str, base_ref: str) -> None:
        self.repo = repo
        self.night = night
        done = run(["git", "rev-parse", base_ref], repo)
        if done.returncode != 0:
            raise WorkspaceError(done.stderr.strip())
        self.base_sha = done.stdout.strip()
        self.ledger = Ledger(repo)
        self.corpora = {c.name: corpus_checkout(c) for c in vc.CORPORA}
        self._bases: dict[tuple[tuple[str, ...], tuple[str, ...]], Path] = {}
        self._base_tests: dict[tuple[Path, tuple[str, ...]], SuiteRun] = {}
        self._files: dict[str, list[str]] | None = None

    def base(self, extras: tuple[str, ...], groups: tuple[str, ...]) -> Path:
        key = (extras, groups)
        if key not in self._bases:
            name = slug(f"base-{self.base_sha[:8]}-{'-'.join(extras + groups)}")
            path, _ = add_worktree(self.repo, name, self.base_sha)
            sync(path, extras, groups)
            self._bases[key] = path
        return self._bases[key]

    def base_tests(self, base: Path, tests: list[str]) -> SuiteRun:
        key = (base, tuple(tests))
        if key not in self._base_tests:
            self._base_tests[key] = gate.run_tests(
                base, tests, f"base-{len(self._base_tests)}"
            )
        return self._base_tests[key]

    def installed(self) -> dict[str, list[str]]:
        if self._files is None:
            self._files = installed_files()
        return self._files

    def close(self) -> None:
        for path in self._bases.values():
            remove_worktree(self.repo, path, f"{vc.BRANCH_PREFIX}{path.name}")


def profile(item: Item) -> tuple[tuple[str, ...], tuple[str, ...]]:
    extras = vc.VENV_EXTRAS
    groups: tuple[str, ...] = ()
    if (
        item.scope == vc.Scope.EXTRA
        and item.scope_name
        and item.scope_name not in extras
    ):
        extras = (*extras, item.scope_name)
    if item.scope == vc.Scope.GROUP and item.scope_name and item.scope_name != "dev":
        groups = (item.scope_name,)
    return extras, groups


def untestable(item: Item) -> str | None:
    if item.scope == vc.Scope.EXTRA and item.scope_name in vc.UNTESTABLE_EXTRAS:
        return (
            f"the `{item.scope_name}` extra is too heavy to install per candidate here"
        )
    if item.scope == vc.Scope.GROUP and item.package not in vc.TOOL_GATES:
        return f"no gate defined for the contributor tool {item.package}"
    return None


def _reach(c: Cone, tests: list[str]) -> str:
    if not c.imports:
        return f"not installed in the parent venv; judged by {len(tests)} test files and the corpora"
    return (
        f"reaches {len(c.source_files)} cgr modules via {', '.join(c.imports)}; "
        f"{len(tests)} test files selected"
    )


def _tool_regression(base: Path, cand: Path, tool: str) -> str | None:
    b_fail, b_lines = gate.tool_diagnostics(base, tool)
    c_fail, c_lines = gate.tool_diagnostics(cand, tool)
    if c_fail > b_fail or c_lines > b_lines:
        return f"{tool} gate: {b_fail} -> {c_fail} failing commands, {b_lines} -> {c_lines} output lines"
    return None


def _measure_candidate(
    night: Night, base: Path, cand: Path, label: str, f: Findings
) -> Findings:
    if f.new_failures or f.tool_regression:
        return f
    deltas = gate.digest_gate(base, cand, night.corpora, label)
    if deltas:
        return f._replace(deltas=deltas)
    comparison = bench.interleaved(
        base, cand, vc.BENCH_CORPUS, night.corpora[vc.BENCH_CORPUS]
    )
    return f._replace(comparison=comparison)


def _outcome(
    night: Night,
    item_id: str,
    experiment: vc.Experiment,
    decision: Decision,
    started: float,
    branch: str | None,
) -> Outcome:
    outcome = Outcome(
        item_id=item_id,
        experiment=experiment,
        verdict=decision.verdict,
        headline=decision.headline,
        evidence=decision.evidence,
        branch=branch,
        night=night.night,
        minutes=round((time.monotonic() - started) / 60, 1),
    )
    night.ledger.record(outcome)
    logger.info(
        ls.OUTCOME.format(
            verdict=outcome.verdict, item=item_id, headline=outcome.headline
        )
    )
    return outcome


def run_upgrade(night: Night, item: Item) -> Outcome:
    started = time.monotonic()
    f = Findings(item=item, experiment=vc.Experiment.UPGRADE)
    reason = untestable(item)
    if reason is not None:
        return _outcome(
            night,
            item.id,
            f.experiment,
            decide_upgrade(f._replace(untestable=reason)),
            started,
            None,
        )

    imports = import_roots(night.installed(), item.package)
    reach_cone = (
        cone(night.repo, item.package, imports)
        if imports
        else Cone(package=item.package, imports=[], source_files=[], test_files=[])
    )
    tests = gate.select_tests(night.repo, item.package, reach_cone)
    f = f._replace(reach=_reach(reach_cone, tests))

    extras, groups = profile(item)
    label = slug(f"{item.package}-{item.latest}")
    cand, branch = add_worktree(night.repo, label, night.base_sha)
    keep = False
    try:
        try:
            if not item.in_range:
                f = f._replace(
                    widened=rewrite_requirement(cand, item.requirement, item.latest)
                )
            lock_upgrade(cand, item.package, item.latest)
            sync(cand, extras, groups)
            base = night.base(extras, groups)
        except WorkspaceError as exc:
            decision = decide_upgrade(f._replace(error=str(exc)))
            return _outcome(night, item.id, f.experiment, decision, started, None)

        if item.package in vc.TOOL_GATES:
            f = f._replace(tool_regression=_tool_regression(base, cand, item.package))
        base_run = night.base_tests(base, tests)
        cand_run = gate.run_tests(cand, tests, label)
        f = f._replace(
            baseline_tests=base_run,
            candidate_tests=cand_run,
            new_failures=gate.new_failures(base_run, cand_run),
        )
        f = _measure_candidate(night, base, cand, label, f)
        decision = decide_upgrade(f)
        if decision.verdict == vc.Verdict.READY:
            sha = commit_all(cand, _commit_message(item, f.widened, decision))
            logger.info(ls.COMMITTED.format(branch=branch, sha=sha))
            keep = True
        return _outcome(
            night, item.id, f.experiment, decision, started, branch if keep else None
        )
    finally:
        # The branch outlives the worktree when the result is worth
        # merging; the checkout and its venv are only scaffolding.
        remove_worktree(night.repo, cand, None if keep else branch)


def _commit_message(item: Item, widened: str | None, decision: Decision) -> str:
    subject = f"build(deps): bump {item.package} from {item.locked} to {item.latest}"
    body = [decision.headline]
    if widened:
        body.append(f"Requirement: {item.requirement} -> {widened}")
    body += decision.evidence
    return "\n\n".join([subject, "\n".join(body)])


def new_candidate(night: Night, name: str) -> tuple[Path, str]:
    path, branch = add_worktree(night.repo, slug(name), night.base_sha)
    sync(path, vc.VENV_EXTRAS)
    return path, branch


def run_adopt(night: Night, item_id: str, name: str, idea: str) -> Outcome:
    """Judge a candidate an agent already edited in worktree `name`."""
    started = time.monotonic()
    cand = work_dir() / "wt" / slug(name)
    branch = f"{vc.BRANCH_PREFIX}{slug(name)}"
    placeholder = Item(
        id=item_id,
        package=item_id,
        scope=vc.Scope.RUNTIME,
        requirement="",
        locked="",
        latest="",
        in_range=True,
    )
    f = Findings(item=placeholder, experiment=vc.Experiment.ADOPT)
    f = f._replace(frozen=gate.frozen_touched(cand, night.base_sha))
    changed = [
        p for p in changed_paths(cand, night.base_sha) if p.endswith(vc.PY_SUFFIX)
    ]
    keep = False
    if not f.frozen:
        base = night.base(vc.VENV_EXTRAS, ())
        tests = tests_for_paths(night.repo, changed)
        f = f._replace(
            reach=f"{len(changed)} files changed; {len(tests)} test files selected"
        )
        for tool in vc.TOOL_GATES:
            regression = _tool_regression(base, cand, tool)
            if regression:
                f = f._replace(tool_regression=regression)
                break
        base_run = night.base_tests(base, tests)
        cand_run = gate.run_tests(cand, tests, slug(name))
        f = f._replace(
            baseline_tests=base_run,
            candidate_tests=cand_run,
            new_failures=gate.new_failures(base_run, cand_run),
        )
        f = _measure_candidate(night, base, cand, slug(name), f)
    decision = decide_adopt(f, idea)
    if decision.verdict == vc.Verdict.READY:
        commit_all(cand, f"perf: {idea}\n\n" + "\n".join(decision.evidence))
        keep = True
    outcome = _outcome(
        night, item_id, f.experiment, decision, started, branch if keep else None
    )
    remove_worktree(night.repo, cand, None if keep else branch)
    return outcome
