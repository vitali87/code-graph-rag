"""An incremental sync fans a call out to same-named definitions as a clean index does (#2403).

A definition whose qualified name another one already holds registers as a
`@line` variant, and `FunctionRegistryTrie.variants` answers the natural qn
together with every variant, so a call into the name fans out to all of them
as `overload`. That list is recorded by `register_unique_qn` while PARSING.
An incremental run parses only the changed files and reads every other
definition back from the graph, which restored each variant as a plain entry
and never rejoined it to its natural qn. A re-parsed caller then saw one
target: a Java call into an enum whose constant bodies override the method
bound the natural qn alone as `exact` where a clean index emits one
`overload` edge per body, so the graph depended on which files happened to
be re-parsed.
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import NodeType
from evals.cgr_graph import _StatefulIngestor

PROJECT = "proj"

# The gson `FieldNamingPolicy.translateName` shape: every enum constant body
# overrides the abstract method, so each body is a real dispatch target and
# the natural qn plus its `@line` variants are all legitimate.
JAVA_POLICY = (
    "package demo;\n"
    "\n"
    "public enum Policy {\n"
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
    "  };\n"
    "\n"
    "  public abstract String translate(String name);\n"
    "}\n"
)
JAVA_CALLER = (
    "package demo;\n"
    "\n"
    "public class Caller {\n"
    "  public String run(Policy p) {\n"
    '    return p.translate("x");\n'
    "  }\n"
    "}\n"
)
JAVA_ENUM: dict[str, str] = {
    "src/demo/Policy.java": JAVA_POLICY,
    "src/demo/Caller.java": JAVA_CALLER,
}
JAVA_CALLER_QN = f"{PROJECT}.src.demo.Caller.Caller.run(Policy)"
JAVA_TARGET_QN = f"{PROJECT}.src.demo.Policy.Policy.translate(String)"

# Which `dumps` exists is decided at import time, so a call may reach either.
PY_UTIL = (
    "import sys\n"
    "\n"
    "if sys.version_info >= (3, 11):\n"
    "    def dumps(x):\n"
    "        return repr(x)\n"
    "else:\n"
    "    def dumps(x):\n"
    "        return str(x)\n"
)
PY_CONDITIONAL: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": PY_UTIL,
    "pkg/app.py": "from pkg.util import dumps\n\n\ndef run():\n    return dumps(1)\n",
}
PY_CALLER_QN = f"{PROJECT}.pkg.app.run"
PY_TARGET_QN = f"{PROJECT}.pkg.util.dumps"

JAVA_UNIQUE: dict[str, str] = {
    "src/demo/Greeter.java": (
        "package demo;\n\npublic class Greeter {\n"
        "  public String greet(String name) {\n    return name;\n  }\n}\n"
    ),
    "src/demo/Caller.java": (
        "package demo;\n\npublic class Caller {\n"
        "  public String run(Greeter g) {\n"
        '    return g.greet("x");\n  }\n}\n'
    ),
}
JAVA_UNIQUE_TARGET_QN = f"{PROJECT}.src.demo.Greeter.Greeter.greet(String)"

PY_UNIQUE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def dumps(x):\n    return repr(x)\n",
    "pkg/app.py": "from pkg.util import dumps\n\n\ndef run():\n    return dumps(1)\n",
}

# Caller and every variant in ONE file, so re-parsing it registers the
# variants by parsing and nothing is read back from the graph.
PY_SAME_FILE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": PY_UTIL + "\n\ndef run():\n    return dumps(1)\n",
    "pkg/other.py": "def unrelated():\n    return 0\n",
}

# serde_json's `Limb`: one alias per pointer width, so the second registers as
# `Limb@5`. A clean index cannot tell which one `Vec<Limb>` names and emits no
# RETURNS edge for it.
RUST_LIMB: dict[str, str] = {
    "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
    "src/lib.rs": "mod lexical;\n",
    "src/lexical/mod.rs": "mod algorithm;\nmod bignum;\nmod math;\n",
    "src/lexical/math.rs": (
        '#[cfg(target_pointer_width = "32")]\n'
        "pub type Limb = u32;\n"
        "\n"
        '#[cfg(target_pointer_width = "64")]\n'
        "pub type Limb = u64;\n"
    ),
    "src/lexical/bignum.rs": (
        "use super::math::*;\n"
        "\n"
        "pub(crate) struct Bigint {\n"
        "    pub(crate) data: Vec<Limb>,\n"
        "}\n"
        "\n"
        "impl Bigint {\n"
        "    pub(crate) fn data(&self) -> &Vec<Limb> {\n"
        "        &self.data\n"
        "    }\n"
        "}\n"
    ),
    "src/lexical/algorithm.rs": "pub(crate) fn run() -> u32 {\n    0\n}\n",
}
LIMB_QN = f"{PROJECT}.src.lexical.math.Limb"

CallEdge = tuple[str, str, str]
Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _index(store: _StatefulIngestor, root: Path, force: bool) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=force)
    return updater


def _touch_after_cache(path: Path, root: Path, comment: str) -> None:
    # A trailing comment changes the hash but not the AST. The mtime is set
    # past the cache's so a coarse-timestamp filesystem cannot skip the file.
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(path.read_text(encoding="utf-8") + f"{comment} touched\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


def _calls(store: _StatefulIngestor) -> set[CallEdge]:
    return {
        (
            str(edge[1]),
            str(edge[4]),
            str(store.edge_props.get(edge, {}).get(cs.KEY_RESOLUTION)),
        )
        for edge in store.keyed_edges
        if edge[2] == cs.RelationshipType.CALLS.value
    }


def _snapshot(store: _StatefulIngestor) -> Snapshot:
    nodes = frozenset((label, str(uid)) for (label, uid) in store.nodes)
    edges = frozenset(tuple(str(part) for part in edge) for edge in store.edges)
    return nodes, edges


def _calls_from(calls: set[CallEdge], caller: str) -> set[CallEdge]:
    return {edge for edge in calls if edge[0] == caller}


def _clean_then_incremental(
    root: Path, files: dict[str, str], edited: str, comment: str
) -> tuple[_StatefulIngestor, set[CallEdge], Snapshot, GraphUpdater]:
    _write(root, files)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    clean_calls, clean = _calls(store), _snapshot(store)
    _touch_after_cache(root / edited, root, comment)
    updater = _index(store, root, force=False)
    return store, clean_calls, clean, updater


@pytest.mark.parametrize(
    ("files", "edited", "comment", "caller", "target"),
    [
        (JAVA_ENUM, "src/demo/Caller.java", "//", JAVA_CALLER_QN, JAVA_TARGET_QN),
        (PY_CONDITIONAL, "pkg/app.py", "#", PY_CALLER_QN, PY_TARGET_QN),
    ],
    ids=["java-enum-constant-bodies", "python-conditional-definitions"],
)
def test_reparsing_only_the_caller_keeps_the_overload_fan_out(
    temp_repo: Path,
    files: dict[str, str],
    edited: str,
    comment: str,
    caller: str,
    target: str,
) -> None:
    root = temp_repo / PROJECT
    store, clean_calls, clean, updater = _clean_then_incremental(
        root, files, edited, comment
    )
    fan_out = _calls_from(clean_calls, caller)
    assert len(fan_out) > 1, "fixture must fan out to same-named definitions"
    assert {edge[2] for edge in fan_out} == {cs.EdgeResolution.OVERLOAD.value}
    assert all(edge[1].startswith(target) for edge in fan_out), fan_out

    assert _calls_from(_calls(store), caller) == fan_out
    assert _calls(store) == clean_calls
    assert _snapshot(store) == clean
    # The registry itself, not only the edges: every definition read back
    # from the graph is one of the target's variants again.
    assert sorted(updater.function_registry.variants(target)) == sorted(
        edge[1] for edge in fan_out
    )


def test_variants_read_back_from_the_graph_list_in_line_order(
    temp_repo: Path,
) -> None:
    # The graph returns rows in no fixed order; the natural qn must stay
    # first (Go's variant-span matching reads index 0 as the natural) and
    # the rest follow the lines they were declared on, whichever file ran.
    root = temp_repo / PROJECT
    _store, _clean_calls, _clean, updater = _clean_then_incremental(
        root, JAVA_ENUM, "src/demo/Caller.java", "//"
    )
    assert updater.function_registry.variants(JAVA_TARGET_QN) == [
        JAVA_TARGET_QN,
        f"{JAVA_TARGET_QN}@11",
        f"{JAVA_TARGET_QN}@17",
    ]


def test_a_type_named_twice_stays_ambiguous_after_a_sync(temp_repo: Path) -> None:
    # The type-edge resolver matches a bare name against the declared-name
    # index. Read back from the graph, `Limb@5` was indexed only as `Limb@5`,
    # so `Limb` looked unique and the unchanged `Bigint.data` gained a RETURNS
    # edge a clean index never emits.
    root = temp_repo / PROJECT
    store, _clean_calls, clean, updater = _clean_then_incremental(
        root, RUST_LIMB, "src/lexical/algorithm.rs", "//"
    )
    returns = {
        (edge[1], edge[4])
        for edge in clean[1]
        if edge[2] == cs.RelationshipType.RETURNS.value
    }
    assert not any(target.startswith(LIMB_QN) for _source, target in returns)
    assert {(label, qn) for label, qn in clean[0] if qn.startswith(LIMB_QN)} == {
        ("Type", LIMB_QN),
        ("Type", f"{LIMB_QN}@5"),
    }

    assert _snapshot(store) == clean
    assert sorted(updater.function_registry.find_ending_with("Limb")) == [
        LIMB_QN,
        f"{LIMB_QN}@5",
    ]


class TestIndexDeclaredName:
    def test_a_variant_is_found_by_the_name_it_was_written_with(self) -> None:
        registry = FunctionRegistryTrie(simple_name_lookup=defaultdict(set))
        registry["p.Limb"] = NodeType.TYPE
        registry["p.Limb@6"] = NodeType.TYPE
        # Warm the cache first: the index change must invalidate it.
        assert registry.find_ending_with("Limb") == ["p.Limb"]
        registry.index_declared_name("p.Limb@6", "Limb")
        assert registry.find_ending_with("Limb") == ["p.Limb", "p.Limb@6"]

    def test_a_signatured_method_is_found_by_its_bare_name(self) -> None:
        registry = FunctionRegistryTrie(simple_name_lookup=defaultdict(set))
        registry["p.C.m(String)"] = NodeType.METHOD
        registry.index_declared_name("p.C.m(String)", "m")
        assert registry.find_ending_with("m") == ["p.C.m(String)"]
        assert registry.find_ending_with("m(String)") == ["p.C.m(String)"]

    def test_an_empty_name_indexes_nothing(self) -> None:
        lookup: defaultdict[str, set[str]] = defaultdict(set)
        registry = FunctionRegistryTrie(simple_name_lookup=lookup)
        registry["p.f"] = NodeType.FUNCTION
        registry.index_declared_name("p.f", "")
        assert "" not in lookup


class TestRestoreVariant:
    def test_rejoins_variants_to_their_natural_qn_in_line_order(self) -> None:
        registry = FunctionRegistryTrie()
        for qn in ("p.f@30", "p.f@12_8", "p.f", "p.f@12"):
            registry[qn] = NodeType.FUNCTION
            registry.restore_variant(qn)
        assert registry.variants("p.f") == ["p.f", "p.f@12", "p.f@12_8", "p.f@30"]

    def test_is_idempotent(self) -> None:
        registry = FunctionRegistryTrie()
        registry["p.f"] = NodeType.FUNCTION
        registry["p.f@9"] = NodeType.FUNCTION
        registry.restore_variant("p.f@9")
        registry.restore_variant("p.f@9")
        assert registry.variants("p.f") == ["p.f", "p.f@9"]

    def test_keeps_a_variant_minted_by_parsing(self) -> None:
        # A re-parsed file's variants were bucketed by register_unique_qn;
        # one read back from another file joins them rather than replacing.
        registry = FunctionRegistryTrie()
        registry["p.f"] = NodeType.FUNCTION
        parsed = registry.register_unique_qn("p.f", 20)
        registry[parsed] = NodeType.FUNCTION
        registry["p.f@5"] = NodeType.FUNCTION
        registry.restore_variant("p.f@5")
        assert registry.variants("p.f") == ["p.f", "p.f@5", "p.f@20"]

    @pytest.mark.parametrize(
        "qn",
        ["p.f", "p.Outer@7.run", "p.@event", "p.C.m(java.lang.String)"],
        ids=["plain", "marker-on-an-outer-segment", "csharp-verbatim", "signature"],
    )
    def test_a_name_without_a_trailing_marker_stays_alone(self, qn: str) -> None:
        registry = FunctionRegistryTrie()
        registry[qn] = NodeType.FUNCTION
        registry.restore_variant(qn)
        assert registry.variants(qn) == [qn]

    def test_a_csharp_verbatim_variant_joins_the_verbatim_name(self) -> None:
        registry = FunctionRegistryTrie()
        registry.restore_variant("p.@event@12")
        assert registry.variants("p.@event") == ["p.@event", "p.@event@12"]


@pytest.mark.parametrize(
    ("files", "edited", "comment", "target"),
    [
        (JAVA_UNIQUE, "src/demo/Caller.java", "//", JAVA_UNIQUE_TARGET_QN),
        (PY_UNIQUE, "pkg/app.py", "#", PY_TARGET_QN),
    ],
    ids=["java-unique-method", "python-unique-function"],
)
def test_a_unique_target_keeps_its_single_edge(
    temp_repo: Path, files: dict[str, str], edited: str, comment: str, target: str
) -> None:
    root = temp_repo / PROJECT
    store, clean_calls, clean, updater = _clean_then_incremental(
        root, files, edited, comment
    )
    into_target = {edge for edge in clean_calls if edge[1] == target}
    assert len(into_target) == 1, clean_calls
    assert next(iter(into_target))[2] != cs.EdgeResolution.OVERLOAD.value

    assert _calls(store) == clean_calls
    assert _snapshot(store) == clean
    assert updater.function_registry.variants(target) == [target]


@pytest.mark.parametrize(
    ("files", "edited", "comment", "caller"),
    [
        (JAVA_ENUM, "src/demo/Policy.java", "//", JAVA_CALLER_QN),
        (PY_CONDITIONAL, "pkg/util.py", "#", PY_CALLER_QN),
        (PY_SAME_FILE, "pkg/util.py", "#", f"{PROJECT}.pkg.util.run"),
    ],
    ids=[
        "java-defining-file-edited",
        "python-defining-file-edited",
        "python-caller-beside-its-variants",
    ],
)
def test_variants_in_a_reparsed_file_are_unchanged(
    temp_repo: Path, files: dict[str, str], edited: str, comment: str, caller: str
) -> None:
    root = temp_repo / PROJECT
    store, clean_calls, clean, _updater = _clean_then_incremental(
        root, files, edited, comment
    )
    fan_out = _calls_from(clean_calls, caller)
    assert len(fan_out) > 1, clean_calls
    assert {edge[2] for edge in fan_out} == {cs.EdgeResolution.OVERLOAD.value}

    assert _calls(store) == clean_calls
    assert _snapshot(store) == clean


def test_a_forced_build_on_a_reused_updater_drops_variants_it_read_back(
    temp_repo: Path,
) -> None:
    # The sync before the forced build reads the `@line` variants back from
    # the graph. The forced build skips that read-back, so a variant whose
    # body the edit removed must leave with the re-parsed file's state, or a
    # call into the name still fans out to it.
    root = temp_repo / PROJECT
    store, _clean_calls, _clean, updater = _clean_then_incremental(
        root, JAVA_ENUM, "src/demo/Caller.java", "//"
    )
    assert len(updater.function_registry.variants(JAVA_TARGET_QN)) == 3
    policy = root / "src/demo/Policy.java"
    policy.write_text(
        JAVA_POLICY.replace(
            "  UPPER {\n"
            "    @Override\n"
            "    public String translate(String name) {\n"
            "      return name.toUpperCase();\n"
            "    }\n"
            "  };\n",
            "  UPPER;\n",
        ),
        encoding="utf-8",
    )
    updater.run(force=True)

    fresh = _StatefulIngestor()
    fresh_updater = _index(fresh, root, force=True)
    assert len(fresh_updater.function_registry.variants(JAVA_TARGET_QN)) == 2

    assert _calls(store) == _calls(fresh)
    assert _snapshot(store) == _snapshot(fresh)
    assert updater.function_registry.variants(
        JAVA_TARGET_QN
    ) == fresh_updater.function_registry.variants(JAVA_TARGET_QN)
