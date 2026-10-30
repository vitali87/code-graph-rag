"""`JsTsClassIngest`: the class a base reaches through its module's exports
or as a member of a namespace import, a global class named as a builtin, and
an overload signature a class body repeats."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.js_ts.class_ingest import JsTsClassIngest
from codebase_rag.tests.collaborator_support import edges, index, nodes_of, parse
from codebase_rag.types_defs import DeferredInherit
from evals.recorded_graph import RecordingIngestor

_FILES = {
    "src/base.ts": (
        "class Animal { speak(): string { return 'x'; } }\n"
        "export { Animal as AnimalBase };\n"
    ),
    "src/shapes.ts": "export class Shape {}\nclass Hidden {}\n",
    "src/dog.ts": (
        "import { AnimalBase } from './base';\n"
        "import * as shapes from './shapes';\n"
        "export class Dog extends AnimalBase {}\n"
        "export class Sq extends shapes.Shape {}\n"
        "export class MyErr extends Error {}\n"
    ),
}


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: RecordingIngestor) -> GraphUpdater:
    return index(temp_repo / "tp", mock_ingestor, _FILES)


def _ingest(updater: GraphUpdater) -> JsTsClassIngest:
    ingest = updater.factory.definition_processor.class_ingest.js_ts
    assert isinstance(ingest, JsTsClassIngest)
    return ingest


def _entry(child: str, parent: str) -> DeferredInherit:
    return DeferredInherit(
        rel_type=cs.RelationshipType.INHERITS,
        child_qn=child,
        parent_qn=parent,
        module_qn="tp.src.dog",
        base_index=0,
        language=cs.SupportedLanguage.TS,
    )


def test_bases_bind_through_a_rename_a_namespace_and_the_globals(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    assert edges(mock_ingestor, cs.RelationshipType.INHERITS) == {
        ("tp.src.dog.Dog", "tp.src.base.Animal"),
        ("tp.src.dog.Sq", "tp.src.shapes.Shape"),
        ("tp.src.dog.MyErr", "builtin.Error"),
    }


def test_a_renamed_export_names_the_class_it_publishes(updater: GraphUpdater) -> None:
    ingest = _ingest(updater)
    renamed = _entry("tp.src.dog.Dog", "tp.src.base.AnimalBase")
    assert ingest.reexport_target(renamed) == "tp.src.base.Animal"
    # A name the exports do not rename is no re-export.
    assert ingest.reexport_target(_entry("tp.src.dog.Dog", "tp.src.base.Other")) is None


def test_a_namespace_import_member_is_the_modules_exported_class(
    updater: GraphUpdater,
) -> None:
    ingest = _ingest(updater)
    entry = _entry("tp.src.dog.Sq", "tp.src.dog.shapes.Shape")
    assert ingest.namespace_member("shapes.Shape", entry) == "tp.src.shapes.Shape"
    assert ingest.namespace_member("other.Shape", entry) is None


def test_only_a_js_global_class_is_externalized(updater: GraphUpdater) -> None:
    ingest = _ingest(updater)
    assert ingest.externalized_base("Error") == "builtin.Error"
    assert ingest.externalized_base("Widget") is None
    assert JsTsClassIngest.separate_declaration_spaces is True


def test_an_overload_signature_repeats_the_members_signature(
    updater: GraphUpdater,
) -> None:
    ingest = _ingest(updater)
    root = parse(
        cs.SupportedLanguage.TS,
        "class K {\n  m(a: string): void;\n  m(a: string): void;\n  n(): void {}\n}\n",
    )
    members = nodes_of(root, "method_signature") + nodes_of(root, "method_definition")
    seen: set[str] = set()
    repeats = [ingest.repeats_member_signature(member, seen) for member in members]
    assert repeats == [False, True, False]
