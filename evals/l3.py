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


FIXTURE_JS_UTIL = """export function greet(name) {
  return "hi " + name;
}


export class Base {
  speak() {
    return this.sound();
  }

  sound() {
    return "...";
  }
}
"""

FIXTURE_JS_APP = """import { greet, Base } from "./util.js";


class Dog extends Base {
  sound() {
    return "woof";
  }
}


function run() {
  const d = new Dog();
  return d.speak() + greet("dog");
}


const handler = () => run();

export { run, handler };
"""


FIXTURE_TS_SHAPES = """export interface Shape {
  area(): number;
}


export abstract class Base implements Shape {
  abstract area(): number;

  describe(): string {
    return `area=${this.area()}`;
  }
}
"""

FIXTURE_TS_MAIN = """import { Base, Shape } from "./shapes";


class Square extends Base {
  constructor(private side: number) {
    super();
  }

  area(): number {
    return this.side * this.side;
  }
}


function total(shapes: Shape[]): number {
  return shapes.reduce((acc, s) => acc + s.area(), 0);
}


function run(): string {
  const sq = new Square(3);
  return sq.describe() + total([sq]);
}

export { run };
"""

FIXTURE_RS_SHAPES = """pub trait Shape {
    fn area(&self) -> f64;
}

pub struct Square {
    pub side: f64,
}

impl Square {
    pub fn new(side: f64) -> Square {
        Square { side }
    }
}

impl Shape for Square {
    fn area(&self) -> f64 {
        self.side * self.side
    }
}

pub fn describe(s: &dyn Shape) -> f64 {
    s.area()
}
"""

FIXTURE_RS_MAIN = """mod shapes;

use shapes::{describe, Shape, Square};

fn run() -> f64 {
    let sq = Square::new(3.0);
    describe(&sq) + sq.area()
}

fn main() {
    run();
}
"""

FIXTURE_GO_MAIN = """package fixture

type Shape interface {
	Area() float64
}

type Square struct {
	Side float64
}

func (s Square) Area() float64 {
	return s.Side * s.Side
}

func describe(s Shape) float64 {
	return s.Area()
}

func Run() float64 {
	sq := Square{Side: 3.0}
	return describe(sq) + sq.Area()
}
"""


FIXTURE_JAVA = """package fixture;

interface Shape {
    double area();
}

class Square implements Shape {
    private double side;

    Square(double side) {
        this.side = side;
    }

    public double area() {
        return this.side * this.side;
    }
}

public class Service {
    double describe(Shape s) {
        return s.area();
    }

    double run() {
        Square sq = new Square(3.0);
        return describe(sq) + sq.area();
    }
}
"""

FIXTURE_C_HEADER = """int square(int x);
int compute(int n);
"""

FIXTURE_C_SRC = """#include "calc.h"

int square(int x) {
    return x * x;
}

int compute(int n) {
    return square(n) + square(n + 1);
}
"""

FIXTURE_CPP = """class Shape {
public:
    virtual double area() const = 0;
    double describe() const { return area(); }
};

class Square : public Shape {
    double side;

public:
    Square(double s) : side(s) {}
    double area() const override { return side * side; }
};

double run() {
    Square sq(3.0);
    return sq.describe() + sq.area();
}
"""

FIXTURE_LUA = """local M = {}

function M.square(x)
  return x * x
end

function M.compute(n)
  return M.square(n) + M.square(n + 1)
end

return M
"""

FIXTURE_PHP = """<?php

interface Shape {
    public function area(): float;
}

class Square implements Shape {
    private float $side;

    public function __construct(float $side) {
        $this->side = $side;
    }

    public function area(): float {
        return $this->side * $this->side;
    }
}

function describe(Shape $s): float {
    return $s->area();
}

function run(): float {
    $sq = new Square(3.0);
    return describe($sq) + $sq->area();
}
"""

FIXTURE_SCALA = """package fixture

trait Shape {
  def area(): Double
}

class Square(side: Double) extends Shape {
  def area(): Double = side * side
}

object Service {
  def describe(s: Shape): Double = s.area()

  def run(): Double = {
    val sq = new Square(3.0)
    describe(sq) + sq.area()
  }
}
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
    (pkg / "util.js").write_text(FIXTURE_JS_UTIL)
    (pkg / "app.js").write_text(FIXTURE_JS_APP)
    (pkg / "shapes.ts").write_text(FIXTURE_TS_SHAPES)
    (pkg / "main.ts").write_text(FIXTURE_TS_MAIN)
    (pkg / "shapes.rs").write_text(FIXTURE_RS_SHAPES)
    (pkg / "main.rs").write_text(FIXTURE_RS_MAIN)
    (pkg / "service.go").write_text(FIXTURE_GO_MAIN)
    (pkg / "Service.java").write_text(FIXTURE_JAVA)
    (pkg / "calc.h").write_text(FIXTURE_C_HEADER)
    (pkg / "calc.c").write_text(FIXTURE_C_SRC)
    (pkg / "shapes.cpp").write_text(FIXTURE_CPP)
    (pkg / "module.lua").write_text(FIXTURE_LUA)
    (pkg / "service.php").write_text(FIXTURE_PHP)
    (pkg / "Shapes.scala").write_text(FIXTURE_SCALA)


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
