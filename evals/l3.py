import json
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

from . import constants as ec
from . import logs as ls
from .calls_trace import trace_calls
from .cgr_graph import extract_cgr_calls

console = Console()

FIXTURE_A = """class Animal:
    def speak(self) -> str:
        return self.sound()

    def sound(self) -> str:
        return "..."


class Dog(Animal):
    def sound(self) -> str:
        return "woof"


def make(kind: str) -> Animal:
    return Dog() if kind == "dog" else Animal()
"""

FIXTURE_B = """from .a import Animal, Dog, make


def greet(kind: str) -> str:
    animal = make(kind)
    return describe(animal)


def describe(animal: Animal) -> str:
    return animal.speak()


def run() -> str:
    d = Dog()
    return d.speak() + greet("dog")
"""


FIXTURE_C = """import asyncio
from dataclasses import dataclass
from functools import wraps
from typing import Iterator

from .a import Animal, Dog


def trace(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


@dataclass
class Counter:
    total: int = 0

    def add(self, value: int) -> int:
        self.total += value
        return self.total

    @property
    def doubled(self) -> int:
        return self.total * 2

    @staticmethod
    def zero() -> int:
        return 0

    @classmethod
    def start(cls) -> "Counter":
        return cls(total=cls.zero())


class Shelter(Animal):
    def __init__(self) -> None:
        self.pets: list[Animal] = []

    def admit(self, pet: Animal) -> None:
        self.pets.append(pet)

    def noises(self) -> list[str]:
        return [pet.sound() for pet in self.pets]

    def loud(self) -> dict[str, str]:
        return {pet.sound(): pet.speak() for pet in self.pets}


@trace
def build_shelter(count: int) -> Shelter:
    shelter = Shelter()
    for _ in range(count):
        shelter.admit(Dog())
    return shelter


def categorize(value: int) -> str:
    match value:
        case 0:
            return Counter.zero.__name__
        case n if n > 0:
            return "positive"
        case _:
            return "negative"


def stream(limit: int) -> Iterator[int]:
    counter = Counter.start()
    for i in range(limit):
        yield counter.add(i)


async def gather(limit: int) -> int:
    counter = Counter()
    await asyncio.sleep(0)
    return counter.add(limit)


def run_rich() -> int:
    shelter = build_shelter(2)
    total = sum(len(noise) for noise in shelter.noises())
    apply = lambda c: c.doubled
    return total + apply(Counter.start())
"""


class _NullIngestor:
    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        return None

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _is_dunder_callee(qn: str) -> bool:
    name = qn.rsplit(ec.SEP, 1)[-1]
    return name.startswith("__") and name.endswith("__")


def _write_fixture(root: Path) -> None:
    pkg = root / "fixture"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").touch()
    (pkg / "a.py").write_text(FIXTURE_A)
    (pkg / "b.py").write_text(FIXTURE_B)
    (pkg / "c.py").write_text(FIXTURE_C)


def main(
    target: Annotated[
        Path, typer.Option(help="cgr source to evaluate CALLS recall for.")
    ] = Path(ec.DEFAULT_TARGET),
    project_name: Annotated[str, typer.Option(help="cgr project name.")] = "",
    out_dir: Annotated[Path, typer.Option(help="Directory for the calls diff.")] = Path(
        ec.DEFAULT_OUT_DIR
    ),
) -> None:
    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.L3_STATIC.format(target=target, project=project))
    static_calls = extract_cgr_calls(target, project)
    logger.success(ls.L3_STATIC_DONE.format(count=len(static_calls)))

    workspace = out_dir / ec.L3_WORKSPACE
    _write_fixture(workspace)
    parsers, queries = load_parsers()

    def workload() -> None:
        GraphUpdater(
            ingestor=_NullIngestor(),
            repo_path=workspace / "fixture",
            parsers=parsers,
            queries=queries,
            project_name=project,
        ).run(force=True)

    logger.info(ls.L3_TRACING.format(target=target))
    traced = trace_calls(workload, target, project)
    logger.success(ls.L3_TRACED_DONE.format(count=len(traced)))

    missed = sorted(traced - static_calls)

    out_dir.mkdir(parents=True, exist_ok=True)
    diff_path = out_dir / ec.L3_DIFF_FILENAME
    diff_path.write_text(
        json.dumps({"missing": [f"{a} -> {b}" for a, b in missed]}, indent=2),
        encoding="utf-8",
    )
    logger.success(ls.WROTE_DIFF.format(path=diff_path))

    explicit = {(a, b) for (a, b) in traced if not _is_dunder_callee(b)}
    table = Table(title="cgr L3 CALLS recall (execution-traced ground truth)")
    table.add_column("scope")
    table.add_column("traced", justify="right")
    table.add_column("captured", justify="right")
    table.add_column("missed", justify="right")
    table.add_column("recall", justify="right")
    for label, edges in (("all calls", traced), ("explicit (no dunders)", explicit)):
        captured = edges & static_calls
        recall = len(captured) / len(edges) if edges else 1.0
        table.add_row(
            label,
            str(len(edges)),
            str(len(captured)),
            str(len(edges) - len(captured)),
            f"{recall:.4f}",
        )
    console.print(table)


if __name__ == "__main__":
    typer.run(main)
