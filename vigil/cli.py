import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from . import constants as vc
from . import logs as ls
from .experiment import Night, new_candidate, run_adopt, run_upgrade
from .impact import cone, import_roots, installed_files
from .ledger import Ledger
from .report import render
from .types_defs import Item, Outcome
from .watch import scan
from .workspace import run

app = typer.Typer(
    no_args_is_help=True,
    help="vigil: turn the night's releases into experiments on cgr, report verdicts.",
)
console = Console()

Repo = Annotated[Path, typer.Option(help="The cgr checkout vigil watches.")]
BaseRef = Annotated[str, typer.Option(help="Commit every candidate is cut from.")]


def tonight() -> str:
    return datetime.now(ZoneInfo(vc.NIGHT_TZ)).date().isoformat()


@app.callback()
def _quiet() -> None:
    # cgr's graph service logs each query at DEBUG; vigil's own log is the
    # one worth reading during a night.
    logger.remove()
    logger.add(sys.stderr, level="INFO")


@app.command()
def index(repo: Repo = Path(".")) -> None:
    """Sync cgr's graph of itself, which `impact` reads."""
    logger.info(ls.INDEXING.format(repo=repo.resolve()))
    cgr = Path(sys.executable).parent / "cgr"
    done = run(
        [str(cgr), "-q", "start", "--repo-path", str(repo), "--update-graph"], repo
    )
    if done.returncode != 0:
        typer.echo(done.stderr, err=True)
        raise typer.Exit(done.returncode)


@app.command()
def watch(
    repo: Repo = Path("."),
    as_json: Annotated[
        bool, typer.Option("--json", help="Print items as JSON.")
    ] = False,
    include_seen: Annotated[
        bool, typer.Option(help="Also list items already tried.")
    ] = False,
) -> None:
    """List dependency releases cgr has not tried yet."""
    items = _items(repo, include_seen)
    if as_json:
        typer.echo(json.dumps([i.model_dump(mode="json") for i in items], indent=2))
        return
    table = Table("item", "scope", "locked", "latest", "in range", "released")
    for i in items:
        scope = f"{i.scope}:{i.scope_name}" if i.scope_name else str(i.scope)
        table.add_row(
            i.id,
            scope,
            i.locked,
            i.latest,
            "yes" if i.in_range else "no",
            (i.released or "")[:10],
        )
    console.print(table)


def _items(repo: Path, include_seen: bool) -> list[Item]:
    items = scan(repo)
    fresh = set(Ledger(repo).unseen([i.id for i in items]))
    logger.info(ls.NEW_ITEMS.format(new=len(fresh), total=len(items)))
    return items if include_seen else [i for i in items if i.id in fresh]


@app.command()
def impact(package: str, repo: Repo = Path(".")) -> None:
    """What a package reaches in cgr: modules importing it and their tests."""
    imports = import_roots(installed_files(), package)
    typer.echo(cone(repo, package, imports).model_dump_json(indent=2))


@app.command()
def upgrade(
    item_ids: Annotated[
        list[str] | None, typer.Argument(help="Items to run; default all new.")
    ] = None,
    repo: Repo = Path("."),
    base_ref: BaseRef = vc.DEFAULT_BASE_REF,
    limit: Annotated[int, typer.Option(help="Run at most this many items.")] = 0,
) -> None:
    """Move each new dependency release into a candidate and judge it."""
    items = _items(repo, include_seen=bool(item_ids))
    if item_ids:
        items = [i for i in items if i.id in item_ids]
    if limit:
        items = items[:limit]
    if not items:
        logger.info(ls.NOTHING_TO_RUN)
        return
    night = Night(repo.resolve(), tonight(), base_ref)
    try:
        for item in items:
            _echo(run_upgrade(night, item))
    finally:
        night.close()


@app.command()
def candidate(
    name: str, repo: Repo = Path("."), base_ref: BaseRef = vc.DEFAULT_BASE_REF
) -> None:
    """Make a synced worktree for an agent to edit, cut from the base."""
    night = Night(repo.resolve(), tonight(), base_ref)
    path, branch = new_candidate(night, name)
    logger.info(ls.CANDIDATE_READY.format(path=path, branch=branch))
    typer.echo(path)


@app.command()
def gate(
    name: str,
    item: Annotated[str, typer.Option(help="The item that prompted the idea.")],
    idea: Annotated[str, typer.Option(help="One line: what the candidate changes.")],
    repo: Repo = Path("."),
    base_ref: BaseRef = vc.DEFAULT_BASE_REF,
) -> None:
    """Judge an edited candidate: frozen paths, tools, tests, graph, speed."""
    night = Night(repo.resolve(), tonight(), base_ref)
    try:
        _echo(run_adopt(night, f"{item}{vc.ITEM_ID_SEPARATOR}{name}", name, idea))
    finally:
        night.close()


@app.command()
def note(
    item: str,
    verdict: Annotated[
        vc.Verdict, typer.Option(help="ACT, READY, DISCARDED or WATCHING.")
    ],
    headline: Annotated[str, typer.Option(help="One line for the report.")],
    evidence: Annotated[
        list[str] | None, typer.Option(help="Supporting lines.")
    ] = None,
    repo: Repo = Path("."),
) -> None:
    """Record a judgement made without an experiment, such as release notes
    that touch nothing cgr uses."""
    outcome = Outcome(
        item_id=item,
        experiment=vc.Experiment.ADOPT,
        verdict=verdict,
        headline=headline,
        evidence=evidence or [],
        night=tonight(),
    )
    Ledger(repo).record(outcome)
    _echo(outcome)


@app.command()
def report(
    repo: Repo = Path("."),
    night: Annotated[
        str | None, typer.Option(help="YYYY-MM-DD; default tonight.")
    ] = None,
    base_ref: BaseRef = vc.DEFAULT_BASE_REF,
    considered: Annotated[int, typer.Option(help="Items looked at tonight.")] = 0,
) -> None:
    """Render and save the morning standing for a night."""
    which = night or tonight()
    ledger = Ledger(repo)
    outcomes = ledger.night(which)
    base = run(["git", "rev-parse", "--short", base_ref], repo).stdout.strip()
    text = render(which, outcomes, base, considered or len(outcomes))
    logger.info(ls.WROTE_REPORT.format(path=ledger.write_report(which, text)))
    typer.echo(text)


def _echo(outcome: Outcome) -> None:
    typer.echo(f"{outcome.verdict} {outcome.item_id}: {outcome.headline}")
