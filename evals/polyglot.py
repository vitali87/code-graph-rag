# Polyglot (cross-language) ingestion eval. Every other eval indexes a
# single-language (or Python-dominant) corpus, so none checks what happens
# when cgr builds ONE graph over files from every supported language at
# once -- the mixed-language repo it is actually pointed at in the wild.
# This ingests a corpus spanning all 14 SupportedLanguages (including a
# deliberate same-basename collision across three languages) and grades
# cross-language integrity invariants that need no external oracle:
#   1. every language contributes at least one module and one definition
#      (no language silently dropped when mixed in),
#   2. two files that strip to the same module qn get DISTINCT qns
#      (cross-language basename collisions are disambiguated, not overwritten),
#   3. no CALLS/INHERITS/OVERRIDES/IMPLEMENTS/INSTANTIATES edge crosses a
#      language boundary (a Rust call must not resolve onto a Python node --
#      cross-language qn bleed),
#   4. the report is deterministic (same corpus -> same qns), so the collision
#      winner never churns run to run.
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from codebase_rag import constants as cs
from codebase_rag.language_spec import get_language_for_extension

from .cgr_graph import _capture

_MODULE = cs.NodeLabel.MODULE.value
_DEFINITION_LABELS = frozenset(
    {
        cs.NodeLabel.FUNCTION.value,
        cs.NodeLabel.METHOD.value,
        cs.NodeLabel.CLASS.value,
        cs.NodeLabel.INTERFACE.value,
        cs.NodeLabel.ENUM.value,
        cs.NodeLabel.TYPE.value,
        cs.NodeLabel.UNION.value,
    }
)
# Semantic def-to-def edges that must never cross a language boundary.
# IMPORTS/CONTAINS/DEFINES are excluded: an import can legitimately point at
# an external stub, and CONTAINS/DEFINES are module-internal by construction.
_CODE_EDGES = frozenset(
    {
        cs.RelationshipType.CALLS.value,
        cs.RelationshipType.INHERITS.value,
        cs.RelationshipType.OVERRIDES.value,
        cs.RelationshipType.IMPLEMENTS.value,
        cs.RelationshipType.INSTANTIATES.value,
    }
)

# A tiny, self-contained module per language: each declares a module plus at
# least one definition and one intra-file call, so "language present" and the
# cross-language-edge check both have material. shapes.{rs,cpp,ts} share a
# basename on purpose -- they are THE collision the disambiguation must split.
_RS_SHAPES = """pub trait Shape {
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

_CPP_SHAPES = """class Shape {
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

_TS_SHAPES = """export interface Shape {
  area(): number;
}


export abstract class Base implements Shape {
  abstract area(): number;

  describe(): string {
    return `area=${this.area()}`;
  }
}
"""

POLYGLOT_SOURCES: dict[str, str] = {
    "analytics.py": (
        "def helper(x):\n    return x + 1\n\n\ndef run():\n    return helper(1)\n"
    ),
    "client.js": (
        "export function greet(name) {\n"
        "  return 'hi ' + name;\n"
        "}\n\n"
        "export function run() {\n"
        "  return greet('x');\n"
        "}\n"
    ),
    "orders.ts": (
        "export function total(xs: number[]): number {\n"
        "  return xs.length;\n"
        "}\n\n"
        "export function run(): number {\n"
        "  return total([1]);\n"
        "}\n"
    ),
    "widget.tsx": (
        "export function Label() {\n"
        "  return <span>hi</span>;\n"
        "}\n\n"
        "export function App() {\n"
        "  return Label();\n"
        "}\n"
    ),
    "gateway.go": (
        "package gateway\n\n"
        "func Helper() int {\n"
        "    return 1\n"
        "}\n\n"
        "func Run() int {\n"
        "    return Helper()\n"
        "}\n"
    ),
    "pricing.scala": (
        "package fixture\n\n"
        "object Pricing {\n"
        "  def helper(): Int = 1\n"
        "  def run(): Int = helper()\n"
        "}\n"
    ),
    "Billing.java": (
        "package fixture;\n\n"
        "class Billing {\n"
        "    int helper() { return 1; }\n"
        "    int run() { return helper(); }\n"
        "}\n"
    ),
    "hashing.c": (
        "int square(int x) {\n"
        "    return x * x;\n"
        "}\n\n"
        "int run() {\n"
        "    return square(2);\n"
        "}\n"
    ),
    "router.php": (
        "<?php\n"
        "function helper() {\n"
        "    return 1;\n"
        "}\n\n"
        "function run() {\n"
        "    return helper();\n"
        "}\n"
    ),
    "config.lua": (
        "local function helper()\n"
        "    return 1\n"
        "end\n\n"
        "local function run()\n"
        "    return helper()\n"
        "end\n\n"
        "return { run = run }\n"
    ),
    "Inventory.cs": (
        "namespace Fixture;\n\n"
        "class Inventory {\n"
        "    int Helper() { return 1; }\n"
        "    int Run() { return Helper(); }\n"
        "}\n"
    ),
    "catalog.dart": (
        "int helper(int x) {\n"
        "  return x + 1;\n"
        "}\n\n"
        "int run() {\n"
        "  return helper(1);\n"
        "}\n"
    ),
    # the cross-language collision trio -- same basename, three languages.
    "shapes.rs": _RS_SHAPES,
    "shapes.cpp": _CPP_SHAPES,
    "shapes.ts": _TS_SHAPES,
}

# The languages the corpus is meant to exercise -- one per SupportedLanguage.
EXPECTED_LANGUAGES: frozenset[cs.SupportedLanguage] = frozenset(cs.SupportedLanguage)

# Basenames that appear under more than one file in the corpus: the
# disambiguation must give each colliding file its own module qn.
COLLIDING_BASENAMES: frozenset[str] = frozenset({"shapes"})


class PolyglotReport(NamedTuple):
    # language -> set of that language's module qns.
    modules_by_language: dict[cs.SupportedLanguage, frozenset[str]]
    # language -> set of that language's definition qns.
    defs_by_language: dict[cs.SupportedLanguage, frozenset[str]]
    # SupportedLanguages with zero modules in the graph.
    missing_languages: frozenset[cs.SupportedLanguage]
    # rel_type, src_qn, dst_qn for every code edge whose endpoints resolve
    # to modules of DIFFERENT languages (should always be empty).
    cross_language_edges: frozenset[tuple[str, str, str]]
    # source relative path -> the module qn cgr assigned it, for files whose
    # basename collides (should be all-distinct across a collision group).
    collision_qns: dict[str, str]


def build_polyglot_corpus(root: Path) -> None:
    """Write the all-language corpus into ``root`` (created if missing)."""
    root.mkdir(parents=True, exist_ok=True)
    for name, content in POLYGLOT_SOURCES.items():
        (root / name).write_text(content, encoding="utf-8")


def _language_of_path(path: str) -> cs.SupportedLanguage | None:
    suffix = Path(path).suffix
    if not suffix:
        return None
    try:
        return get_language_for_extension(suffix)
    except Exception:
        return None


def cgr_polyglot(target: Path, project: str) -> PolyglotReport:
    ingestor = _capture(target, project)

    # module qn -> language, from each Module node's recorded path.
    module_language: dict[str, cs.SupportedLanguage] = {}
    module_qns_by_lang: dict[cs.SupportedLanguage, set[str]] = {}
    path_of_module: dict[str, str] = {}
    for (label, _uid), props in ingestor.nodes.items():
        if label != _MODULE:
            continue
        qn = props.get(cs.KEY_QUALIFIED_NAME)
        path = props.get(cs.KEY_PATH)
        if not isinstance(qn, str) or not isinstance(path, str):
            continue
        lang = _language_of_path(path)
        if lang is None:
            continue
        module_language[qn] = lang
        module_qns_by_lang.setdefault(lang, set()).add(qn)
        path_of_module[qn] = path

    # The longest module-qn that prefixes a given qn is the module it lives
    # in. ponytail: linear scan per qn; the corpus is ~15 modules, not worth
    # a trie.
    def owning_module(qn: str) -> str | None:
        best: str | None = None
        for m in module_language:
            if (qn == m or qn.startswith(m + cs.SEPARATOR_DOT)) and (
                best is None or len(m) > len(best)
            ):
                best = m
        return best

    defs_by_lang: dict[cs.SupportedLanguage, set[str]] = {}
    for (label, _uid), props in ingestor.nodes.items():
        if label not in _DEFINITION_LABELS:
            continue
        qn = props.get(cs.KEY_QUALIFIED_NAME)
        if not isinstance(qn, str):
            continue
        m = owning_module(qn)
        if m is None:
            continue
        defs_by_lang.setdefault(module_language[m], set()).add(qn)

    cross_edges: set[tuple[str, str, str]] = set()
    for from_label, from_val, rel_type, to_label, to_val in ingestor.rels:
        if rel_type not in _CODE_EDGES:
            continue
        src, dst = str(from_val), str(to_val)
        src_mod, dst_mod = owning_module(src), owning_module(dst)
        if src_mod is None or dst_mod is None:
            continue
        if module_language[src_mod] != module_language[dst_mod]:
            cross_edges.add((rel_type, src, dst))

    collision_qns: dict[str, str] = {
        path: qn
        for qn, path in path_of_module.items()
        if Path(path).stem in COLLIDING_BASENAMES
    }

    present = frozenset(module_qns_by_lang)
    return PolyglotReport(
        modules_by_language={
            lang: frozenset(qns) for lang, qns in module_qns_by_lang.items()
        },
        defs_by_language={lang: frozenset(qns) for lang, qns in defs_by_lang.items()},
        missing_languages=EXPECTED_LANGUAGES - present,
        cross_language_edges=frozenset(cross_edges),
        collision_qns=collision_qns,
    )
