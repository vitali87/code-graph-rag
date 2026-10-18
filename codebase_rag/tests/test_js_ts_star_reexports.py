# `export * from './lib'` never reached the import map: the re-export parser
# compared the child against the Java grammar's named `asterisk` node, while
# the JavaScript and TypeScript grammars spell the token as an anonymous `*`.
# And `follow_reexports` only followed a name a module binds explicitly, so a
# member of a star barrel stayed on the barrel, where nothing is registered.
# Found reviewing #2560: `import * as r from './index'` then
# `implements r.Router<T>` was externalized for a star barrel.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.utils import follow_reexports
from codebase_rag.types_defs import NodeType, PropertyDict, PropertyValue

_PROJECT = "proj"


class _Recorder:
    def __init__(self) -> None:
        self.rels: list[tuple[str, str, str, PropertyValue]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        resolution = (properties or {}).get(cs.KEY_RESOLUTION)
        self.rels.append(
            (str(from_spec[2]), str(rel_type), str(to_spec[2]), resolution)
        )

    def flush_all(self) -> None:
        return None


def _run(root: Path, files: dict[str, str]) -> tuple[GraphUpdater, _Recorder]:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    parsers, queries = load_parsers()
    recorder = _Recorder()
    updater = GraphUpdater(
        ingestor=recorder,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=_PROJECT,
    )
    updater.run(force=True)
    return updater, recorder


@pytest.mark.parametrize("ext", [".js", ".ts", ".tsx"])
def test_star_re_export_is_recorded_for_every_js_ts_grammar(
    tmp_path: Path, ext: str
) -> None:
    updater, _ = _run(
        tmp_path,
        {
            f"lib{ext}": "export function helper() { return 1 }\n",
            f"index{ext}": "export * from './lib'\nexport { helper as h } from './lib'\n",
        },
    )
    mapping = updater.factory.import_processor.import_mapping["proj.index"]
    assert mapping == {
        f"{cs.GLOB_ALL}proj.lib": "proj.lib",
        "h": "proj.lib.helper",
    }


def test_namespace_re_export_is_not_a_star_re_export(tmp_path: Path) -> None:
    # `export * as ns from` binds one name, `ns`; the module's members are not
    # spread into the barrel.
    updater, _ = _run(
        tmp_path,
        {
            "lib.ts": "export function helper() { return 1 }\n",
            "index.ts": "export * as ns from './lib'\n",
        },
    )
    mapping = updater.factory.import_processor.import_mapping["proj.index"]
    assert not any(key.startswith(cs.GLOB_ALL) for key in mapping), mapping


STAR_LIB_TS = """\
export function helper(): number { return 1 }
export class Svc { go(): number { return 2 } }
"""

STAR_USE_TS = """\
import { Svc } from './index'
export function method() { const s = new Svc(); return s.go() }
"""


def test_method_call_on_a_class_from_a_star_barrel_resolves_exactly(
    tmp_path: Path,
) -> None:
    # The receiver's type is `index.Svc`; following it into the star source
    # binds `s.go()`, which was left unresolved (a named barrel resolved it).
    _, recorder = _run(
        tmp_path,
        {
            "src/lib.ts": STAR_LIB_TS,
            "src/index.ts": "export * from './lib'\n",
            "src/use.ts": STAR_USE_TS,
        },
    )
    calls = {
        (target, resolution)
        for source, rel, target, resolution in recorder.rels
        if source == "proj.src.use.method" and rel == cs.RelationshipType.CALLS.value
    }
    assert calls == {("proj.src.lib.Svc.go", cs.EdgeResolution.EXACT)}, calls


def _registry(*qns: str) -> FunctionRegistryTrie:
    registry = FunctionRegistryTrie()
    for qn in qns:
        registry[qn] = NodeType.CLASS
    return registry


def _star(module: str) -> str:
    return f"{cs.GLOB_ALL}{module}"


class TestFollowReexportsThroughStars:
    def test_the_one_star_source_that_declares_the_name(self) -> None:
        mapping = {"p.index": {_star("p.a"): "p.a", _star("p.b"): "p.b"}}
        registry = _registry("p.b.Router", "p.a.Other")
        assert follow_reexports("p.index.Router", mapping, registry) == "p.b.Router"

    def test_a_star_chain(self) -> None:
        mapping = {
            "p.index": {_star("p.api"): "p.api"},
            "p.api": {_star("p.router"): "p.router"},
        }
        registry = _registry("p.router.Router")
        assert (
            follow_reexports("p.index.Router", mapping, registry) == "p.router.Router"
        )

    def test_two_sources_reaching_one_declaration_agree(self) -> None:
        mapping = {
            "p.index": {_star("p.a"): "p.a", _star("p.b"): "p.b"},
            "p.a": {_star("p.core"): "p.core"},
            "p.b": {_star("p.core"): "p.core"},
        }
        registry = _registry("p.core.Router")
        assert follow_reexports("p.index.Router", mapping, registry) == "p.core.Router"

    # --- negative: nothing is guessed --------------------------------------

    def test_two_declaring_sources_are_ambiguous(self) -> None:
        mapping = {"p.index": {_star("p.a"): "p.a", _star("p.b"): "p.b"}}
        registry = _registry("p.a.Router", "p.b.Router")
        assert follow_reexports("p.index.Router", mapping, registry) == "p.index.Router"

    def test_no_declaring_source_stays_put(self) -> None:
        mapping = {"p.index": {_star("p.a"): "p.a"}}
        registry = _registry("p.a.Other")
        assert follow_reexports("p.index.Router", mapping, registry) == "p.index.Router"

    def test_an_explicit_binding_outranks_the_stars(self) -> None:
        mapping = {"p.index": {_star("p.a"): "p.a", "Router": "p.b.Router"}}
        registry = _registry("p.a.Router", "p.b.Router")
        assert follow_reexports("p.index.Router", mapping, registry) == "p.b.Router"

    def test_a_star_cycle_terminates(self) -> None:
        mapping = {
            "p.index": {_star("p.a"): "p.a"},
            "p.a": {_star("p.index"): "p.index"},
        }
        registry = _registry("p.other.Router")
        assert follow_reexports("p.index.Router", mapping, registry) == "p.index.Router"

    def test_a_registered_name_is_never_redirected(self) -> None:
        mapping = {"p.index": {_star("p.a"): "p.a"}}
        registry = _registry("p.index.Router", "p.a.Router")
        assert follow_reexports("p.index.Router", mapping, registry) == "p.index.Router"


class TestStarSourcesExposeOnlyExports:
    def test_a_private_candidate_does_not_count(self) -> None:
        mapping = {"p.index": {_star("p.a"): "p.a", _star("p.b"): "p.b"}}
        registry = _registry("p.a.Router", "p.b.Router")
        registry.mark_module_private("p.a.Router")
        assert follow_reexports("p.index.Router", mapping, registry) == "p.b.Router"

    def test_a_private_candidate_alone_is_not_followed(self) -> None:
        mapping = {"p.index": {_star("p.a"): "p.a"}}
        registry = _registry("p.a.Router")
        registry.mark_module_private("p.a.Router")
        assert follow_reexports("p.index.Router", mapping, registry) == "p.index.Router"

    def test_an_explicit_binding_to_a_private_name_is_still_followed(self) -> None:
        # `export { Router } from './a'` names it outright; only a star source
        # is limited to what its module exports.
        mapping = {"p.index": {"Router": "p.a.Router"}}
        registry = _registry("p.a.Router")
        registry.mark_module_private("p.a.Router")
        assert follow_reexports("p.index.Router", mapping, registry) == "p.a.Router"
