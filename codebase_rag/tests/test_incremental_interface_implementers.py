"""An incremental sync keeps the sole-implementer edges a clean index emits (#2403).

A call typed to an interface (a Java/C#/PHP interface, a Dart abstract class,
a Rust trait) resolves to the interface's own method, and when the interface
has exactly one implementer the call ALSO reaches that implementer's method.
The implementer set came from parsing alone, and an incremental run parses
only the changed files, so re-parsing just the caller lost the edge: gson's
`getFieldNames` kept its `FieldNamingStrategy.translateName` edge and dropped
the seven into `FieldNamingPolicy`'s enum-constant bodies, and serde_json's
`u64::error_halfscale()` dropped its edge into `impl FloatErrors for u64`.

The override walk reads the same set. Without it an unchanged class that both
extends a class and implements an interface fell through to the superclass,
so the incremental graph gained an OVERRIDES edge a clean index never has.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_incremental_duplicate_variants import (
    CallEdge,
    Snapshot,
    _calls,
    _calls_from,
    _index,
    _snapshot,
    _touch_after_cache,
    _write,
)
from evals.cgr_graph import _StatefulIngestor

PROJECT = "proj"

JAVA_STRATEGY = "package demo;\n\npublic interface Strategy {\n  String translate(String name);\n}\n"
JAVA_CALLER = (
    "package demo;\n"
    "\n"
    "public class Caller {\n"
    "  private final Strategy strategy;\n"
    "\n"
    "  public Caller(Strategy strategy) {\n"
    "    this.strategy = strategy;\n"
    "  }\n"
    "\n"
    "  public String run(String name) {\n"
    "    return strategy.translate(name);\n"
    "  }\n"
    "}\n"
)
JAVA_UPPER = (
    "package demo;\n"
    "\n"
    "public class Upper implements Strategy {\n"
    "  @Override\n"
    "  public String translate(String name) {\n"
    "    return name.toUpperCase();\n"
    "  }\n"
    "}\n"
)
JAVA_LOWER_CLASS = (
    "class Lower implements Strategy {\n"
    "  @Override\n"
    "  public String translate(String name) {\n"
    "    return name.toLowerCase();\n"
    "  }\n"
    "}\n"
)
JAVA: dict[str, str] = {
    "src/demo/Strategy.java": JAVA_STRATEGY,
    "src/demo/Upper.java": JAVA_UPPER,
    "src/demo/Caller.java": JAVA_CALLER,
}
JAVA_CALLER_QN = f"{PROJECT}.src.demo.Caller.Caller.run(String)"
JAVA_UPPER_QN = f"{PROJECT}.src.demo.Upper.Upper.translate(String)"

# The gson shape: the enum implements the interface and every constant body
# overrides its method, so the sole implementer's method is itself a natural
# qn plus `@line` variants.
JAVA_ENUM: dict[str, str] = {
    "src/demo/Strategy.java": JAVA_STRATEGY,
    "src/demo/Policy.java": (
        "package demo;\n"
        "\n"
        "public enum Policy implements Strategy {\n"
        "  IDENTITY {\n"
        "    @Override\n"
        "    public String translate(String name) {\n"
        "      return name;\n"
        "    }\n"
        "  },\n"
        "  UPPER {\n"
        "    @Override\n"
        "    public String translate(String name) {\n"
        "      return name.toUpperCase();\n"
        "    }\n"
        "  }\n"
        "}\n"
    ),
    "src/demo/Caller.java": JAVA_CALLER,
}
JAVA_POLICY_QN = f"{PROJECT}.src.demo.Policy.Policy.translate(String)"

CSHARP: dict[str, str] = {
    "IStrategy.cs": (
        "namespace Demo\n{\n    public interface IStrategy\n    {\n"
        "        string Translate(string name);\n    }\n}\n"
    ),
    "Upper.cs": (
        "namespace Demo\n{\n    public class Upper : IStrategy\n    {\n"
        "        public string Translate(string name)\n        {\n"
        "            return name.ToUpper();\n        }\n    }\n}\n"
    ),
    "Caller.cs": (
        "namespace Demo\n{\n    public class Caller\n    {\n"
        "        private readonly IStrategy strategy;\n\n"
        "        public Caller(IStrategy strategy)\n        {\n"
        "            this.strategy = strategy;\n        }\n\n"
        "        public string Run(string name)\n        {\n"
        "            return strategy.Translate(name);\n        }\n    }\n}\n"
    ),
}

PHP: dict[str, str] = {
    "src/Strategy.php": (
        "<?php\nnamespace Demo;\n\ninterface Strategy\n{\n"
        "    public function translate(string $name): string;\n}\n"
    ),
    "src/Upper.php": (
        "<?php\nnamespace Demo;\n\nclass Upper implements Strategy\n{\n"
        "    public function translate(string $name): string\n    {\n"
        "        return strtoupper($name);\n    }\n}\n"
    ),
    "src/Caller.php": (
        "<?php\nnamespace Demo;\n\nclass Caller\n{\n"
        "    public function run(Strategy $strategy): string\n    {\n"
        "        return $strategy->translate('x');\n    }\n}\n"
    ),
}

DART: dict[str, str] = {
    "pubspec.yaml": "name: demo\n",
    "lib/strategy.dart": "abstract class Strategy {\n  String translate(String name);\n}\n",
    "lib/upper.dart": (
        "import 'strategy.dart';\n\nclass Upper implements Strategy {\n"
        "  @override\n  String translate(String name) => name.toUpperCase();\n}\n"
    ),
    "lib/caller.dart": (
        "import 'strategy.dart';\n\nclass Caller {\n  final Strategy strategy;\n"
        "  Caller(this.strategy);\n\n  String run(String name) {\n"
        "    return strategy.translate(name);\n  }\n}\n"
    ),
}

RUST_CARGO = '[package]\nname = "demo"\nversion = "0.1.0"\n'
RUST_STRATEGY = (
    "pub trait Strategy {\n    fn translate(&self, name: &str) -> String;\n}\n"
)
RUST: dict[str, str] = {
    "Cargo.toml": RUST_CARGO,
    "src/lib.rs": "pub mod strategy;\npub mod upper;\npub mod caller;\n",
    "src/strategy.rs": RUST_STRATEGY,
    "src/upper.rs": (
        "use crate::strategy::Strategy;\n\npub struct Upper;\n\n"
        "impl Strategy for Upper {\n"
        "    fn translate(&self, name: &str) -> String {\n"
        "        name.to_uppercase()\n    }\n}\n"
    ),
    "src/caller.rs": (
        "use crate::strategy::Strategy;\n\n"
        "pub fn run(strategy: &dyn Strategy) -> String {\n"
        '    strategy.translate("x")\n}\n'
    ),
}

# The serde_json shape: the implementer is a primitive, so no node stands for
# it and no IMPLEMENTS edge records the pair; only its methods' OVERRIDES do.
RUST_PRIMITIVE: dict[str, str] = {
    "Cargo.toml": RUST_CARGO,
    "src/lib.rs": "mod lexical;\n",
    "src/lexical/mod.rs": "mod algorithm;\nmod errors;\n",
    "src/lexical/errors.rs": (
        "pub(crate) trait FloatErrors {\n"
        "    fn error_scale() -> u32;\n"
        "    fn error_halfscale() -> u32;\n"
        "}\n"
        "\n"
        "impl FloatErrors for u64 {\n"
        "    fn error_scale() -> u32 {\n"
        "        8\n"
        "    }\n"
        "\n"
        "    fn error_halfscale() -> u32 {\n"
        "        u64::error_scale() / 2\n"
        "    }\n"
        "}\n"
    ),
    "src/lexical/algorithm.rs": (
        "use super::errors::*;\n\n"
        "pub(crate) fn multiply() -> u32 {\n    u64::error_halfscale()\n}\n"
    ),
}

# An inherent method sharing the trait method's name: parsing knows it
# implements nothing, and an unchanged file must not gain an OVERRIDES edge
# for it. The trait-typed call fans out to both, as a clean index does.
RUST_INHERENT: dict[str, str] = {
    **RUST,
    "src/upper.rs": (
        "use crate::strategy::Strategy;\n\npub struct Upper;\n\n"
        "impl Upper {\n"
        "    pub fn translate(&self, name: &str) -> String {\n"
        "        name.to_lowercase()\n    }\n}\n\n"
        "impl Strategy for Upper {\n"
        "    fn translate(&self, name: &str) -> String {\n"
        "        name.to_uppercase()\n    }\n}\n"
    ),
}

RUST_INHERENT_UNCALLED: dict[str, str] = {
    **RUST_INHERENT,
    "src/caller.rs": "pub fn run() {}\n",
}

# Every trait method has a default body, and `u64` takes them all: no node,
# no method, so nothing in the graph records that impl. A clean index counts
# two implementers and emits no sole-implementer edge.
RUST_EMPTY_PRIMITIVE_IMPL: dict[str, str] = {
    **RUST,
    "src/strategy.rs": (
        "pub trait Strategy {\n"
        "    fn translate(&self, name: &str) -> String {\n"
        "        name.to_string()\n"
        "    }\n"
        "}\n"
    ),
    "src/upper.rs": RUST["src/upper.rs"] + "\nimpl Strategy for u64 {}\n",
}

# `C extends B implements I`, B extends A: both A and I declare `m`, and the
# walk reaches I first (one hop) where A is two hops up.
JAVA_OVERRIDE_ORDER: dict[str, str] = {
    "src/demo/A.java": "package demo;\n\npublic class A {\n  public void m() {}\n}\n",
    "src/demo/B.java": "package demo;\n\npublic class B extends A {\n}\n",
    "src/demo/I.java": "package demo;\n\npublic interface I {\n  void m();\n}\n",
    "src/demo/C.java": (
        "package demo;\n\npublic class C extends B implements I {\n"
        "  @Override\n  public void m() {}\n}\n"
    ),
    "src/demo/Caller.java": "package demo;\n\npublic class Caller {\n  public void run() {}\n}\n",
}

TYPESCRIPT: dict[str, str] = {
    "src/strategy.ts": "export interface Strategy {\n  translate(name: string): string;\n}\n",
    "src/upper.ts": (
        "import { Strategy } from './strategy';\n\n"
        "export class Upper implements Strategy {\n"
        "  translate(name: string): string {\n    return name.toUpperCase();\n  }\n}\n"
    ),
    "src/caller.ts": (
        "import { Strategy } from './strategy';\n\n"
        "export function run(strategy: Strategy): string {\n"
        "  return strategy.translate('x');\n}\n"
    ),
}


def _clean_then_incremental(
    root: Path, files: dict[str, str], edited: str
) -> tuple[_StatefulIngestor, set[CallEdge], Snapshot]:
    _write(root, files)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    clean_calls, clean = _calls(store), _snapshot(store)
    _touch_after_cache(root / edited, root, _comment(edited))
    _index(store, root, force=False)
    return store, clean_calls, clean


def _comment(path: str) -> str:
    return "#" if path.endswith(".py") else "//"


@pytest.mark.parametrize(
    ("files", "edited", "caller", "target"),
    [
        (JAVA, "src/demo/Caller.java", JAVA_CALLER_QN, JAVA_UPPER_QN),
        (JAVA_ENUM, "src/demo/Caller.java", JAVA_CALLER_QN, JAVA_POLICY_QN),
        (
            CSHARP,
            "Caller.cs",
            f"{PROJECT}.Caller.Demo.Caller.Run(string)",
            f"{PROJECT}.Upper.Demo.Upper.Translate(string)",
        ),
        (
            PHP,
            "src/Caller.php",
            f"{PROJECT}.src.Caller.Caller.run",
            f"{PROJECT}.src.Upper.Upper.translate",
        ),
        (
            DART,
            "lib/caller.dart",
            f"{PROJECT}.lib.caller.Caller.run",
            f"{PROJECT}.lib.upper.Upper.translate",
        ),
        (
            RUST,
            "src/caller.rs",
            f"{PROJECT}.src.caller.run",
            f"{PROJECT}.src.upper.Upper.translate",
        ),
        (
            RUST_PRIMITIVE,
            "src/lexical/algorithm.rs",
            f"{PROJECT}.src.lexical.algorithm.multiply",
            f"{PROJECT}.src.lexical.errors.u64.error_halfscale",
        ),
        (
            RUST_INHERENT,
            "src/caller.rs",
            f"{PROJECT}.src.caller.run",
            f"{PROJECT}.src.upper.Upper.translate",
        ),
    ],
    ids=[
        "java",
        "java-enum-constant-bodies",
        "csharp",
        "php",
        "dart",
        "rust",
        "rust-trait-impl-for-a-primitive",
        "rust-inherent-method-named-like-the-trait-method",
    ],
)
def test_reparsing_only_the_caller_keeps_the_sole_implementer_edge(
    temp_repo: Path, files: dict[str, str], edited: str, caller: str, target: str
) -> None:
    root = temp_repo / PROJECT
    store, clean_calls, clean = _clean_then_incremental(root, files, edited)
    into_implementer = {
        edge for edge in _calls_from(clean_calls, caller) if edge[1].startswith(target)
    }
    assert into_implementer, "fixture must reach the sole implementer's method"

    assert _calls_from(_calls(store), caller) == _calls_from(clean_calls, caller)
    assert _calls(store) == clean_calls
    assert _snapshot(store) == clean


def test_an_unchanged_class_keeps_the_override_its_parse_chose(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    store, _clean_calls, clean = _clean_then_incremental(
        root, JAVA_OVERRIDE_ORDER, "src/demo/Caller.java"
    )
    overrides = {
        (edge[1], edge[4])
        for edge in clean[1]
        if edge[2] == cs.RelationshipType.OVERRIDES.value
    }
    assert overrides == {
        (f"{PROJECT}.src.demo.C.C.m()", f"{PROJECT}.src.demo.I.I.m()")
    }, "fixture must pin the interface, not the superclass two hops up"

    assert _snapshot(store) == clean


def test_a_second_implementer_in_the_reparsed_file_retires_the_edge(
    temp_repo: Path,
) -> None:
    # The re-parsed file adds `Lower`; the unchanged `Upper` is read back. A
    # clean index of the edited tree sees two implementers and emits no
    # companion edge, where parsing alone saw `Lower` as the only one.
    root = temp_repo / PROJECT
    _write(root, JAVA)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    caller = root / "src/demo/Caller.java"
    caller.write_text(JAVA_CALLER + "\n" + JAVA_LOWER_CLASS, encoding="utf-8")
    _touch_after_cache(caller, root, "//")
    _index(store, root, force=False)

    clean_store = _StatefulIngestor()
    _index(clean_store, root, force=True)
    clean_calls = _calls(clean_store)
    assert {edge[1] for edge in _calls_from(clean_calls, JAVA_CALLER_QN)} == {
        f"{PROJECT}.src.demo.Strategy.Strategy.translate(String)"
    }

    assert _calls(store) == clean_calls
    assert _snapshot(store) == _snapshot(clean_store)


def test_two_implementers_get_no_sole_implementer_edge(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    files = {
        **JAVA,
        "src/demo/Lower.java": "package demo;\n\n" + "public " + JAVA_LOWER_CLASS,
    }
    store, clean_calls, clean = _clean_then_incremental(
        root, files, "src/demo/Caller.java"
    )
    assert {edge[1] for edge in _calls_from(clean_calls, JAVA_CALLER_QN)} == {
        f"{PROJECT}.src.demo.Strategy.Strategy.translate(String)"
    }

    assert _calls(store) == clean_calls
    assert _snapshot(store) == clean


@pytest.mark.parametrize(
    ("files", "edited"),
    [
        (JAVA, "src/demo/Upper.java"),
        (JAVA_ENUM, "src/demo/Policy.java"),
        (CSHARP, "Upper.cs"),
        (RUST, "src/upper.rs"),
        (RUST_PRIMITIVE, "src/lexical/errors.rs"),
        (RUST_INHERENT_UNCALLED, "src/caller.rs"),
        (RUST_EMPTY_PRIMITIVE_IMPL, "src/caller.rs"),
        (TYPESCRIPT, "src/caller.ts"),
    ],
    ids=[
        "java-implementer-edited",
        "java-enum-implementer-edited",
        "csharp-implementer-edited",
        "rust-impl-edited",
        "rust-primitive-impl-edited",
        "rust-inherent-method-gains-no-override",
        "rust-trait-with-an-untraceable-second-impl",
        "typescript-interface-call-stays-unresolved",
    ],
)
def test_edges_the_sync_already_matched_are_unchanged(
    temp_repo: Path, files: dict[str, str], edited: str
) -> None:
    root = temp_repo / PROJECT
    store, clean_calls, clean = _clean_then_incremental(root, files, edited)

    assert _calls(store) == clean_calls
    assert _snapshot(store) == clean


def _updater(store: _StatefulIngestor, root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )


@pytest.mark.parametrize(
    "full_build_on_the_same_updater",
    [False, True],
    ids=["read-back-pairs", "read-back-and-parsed-pairs"],
)
def test_a_forced_build_on_a_reused_updater_matches_a_fresh_one(
    temp_repo: Path, full_build_on_the_same_updater: bool
) -> None:
    # An incremental run reads the implementer pairs back from the graph, and
    # a forced run skips that read-back. A reused updater that kept the pairs
    # of an earlier run still counted a deleted implementer, so the forced
    # build missed the sole-implementer edge a fresh one emits. With the full
    # build on the same updater its parsed pairs hold the deleted one too.
    root = temp_repo / PROJECT
    lower = root / "src/demo/Lower.java"
    _write(root, JAVA)
    _write(root, {"src/demo/Lower.java": "package demo;\n\npublic " + JAVA_LOWER_CLASS})
    store = _StatefulIngestor()
    updater = _updater(store, root)
    if full_build_on_the_same_updater:
        updater.run(force=True)
    else:
        _index(store, root, force=True)
    _touch_after_cache(root / "src/demo/Caller.java", root, "//")
    updater.run(force=False)
    lower.unlink()
    updater.run(force=True)

    fresh = _StatefulIngestor()
    _index(fresh, root, force=True)
    fresh_calls = _calls(fresh)
    assert JAVA_UPPER_QN in {
        edge[1] for edge in _calls_from(fresh_calls, JAVA_CALLER_QN)
    }, "fixture must reach the remaining implementer's method"

    assert _calls_from(_calls(store), JAVA_CALLER_QN) == _calls_from(
        fresh_calls, JAVA_CALLER_QN
    )
    assert _calls(store) == fresh_calls
    assert _snapshot(store) == _snapshot(fresh)
