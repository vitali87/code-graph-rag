"""`JsTsFileIngest`: the specifiers a file's imports resolved nowhere, stored
on its Module node, TypeScript's overload signatures folded into their
implementation, and the named JS/TS passes run once the file's functions and
classes are in."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.js_ts.file_ingest import JsTsFileIngest
from codebase_rag.tests.collaborator_support import index, nodes_of, parse
from codebase_rag.tests.conftest import get_nodes
from evals.recorded_graph import RecordingIngestor

_OVERLOADS = (
    "export function f(a: string): void;\n"
    "export function f(a: number): void;\n"
    "export function f(a: any) { return a; }\n"
)


def _ingest(updater: GraphUpdater, language: cs.SupportedLanguage) -> JsTsFileIngest:
    ingest = updater.factory.definition_processor._for(language)
    assert isinstance(ingest, JsTsFileIngest)
    return ingest


def test_unresolved_specifiers_are_stored_on_the_module(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    index(
        temp_repo / "tp",
        mock_ingestor,
        {
            "src/a.ts": "import missing from './missing';\nimport { b } from './b';\n",
            "src/b.ts": "export const b = 1;\n",
        },
    )
    specifiers = {
        n.properties[cs.KEY_QUALIFIED_NAME]: n.properties[cs.KEY_UNRESOLVED_SPECIFIERS]
        for n in get_nodes(mock_ingestor, cs.NodeLabel.MODULE)
        if cs.KEY_UNRESOLVED_SPECIFIERS in n.properties
    }
    assert specifiers == {"tp.src.a": ["./missing"], "tp.src.b": []}


def test_typescript_overload_signatures_fold_into_the_implementation(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "tp", mock_ingestor, {"src/f.ts": _OVERLOADS})
    functions = [
        n.properties[cs.KEY_START_LINE]
        for n in get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
        if n.properties[cs.KEY_QUALIFIED_NAME] == "tp.src.f.f"
    ]
    assert functions == [3]

    _, queries = load_parsers()
    root = parse(cs.SupportedLanguage.TS, _OVERLOADS)
    folded = _ingest(updater, cs.SupportedLanguage.TS).folded_overloads(
        root, cs.SupportedLanguage.TS, queries, None
    )
    signatures = nodes_of(root, "function_signature")
    assert folded is not None
    assert {s.start_byte for s in signatures} <= folded
    implementation = nodes_of(root, "function_declaration")[0]
    assert implementation.start_byte not in folded


def test_javascript_folds_no_overloads(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "tp", mock_ingestor, {"src/a.js": "function f() {}\n"})
    _, queries = load_parsers()
    root = parse(cs.SupportedLanguage.JS, "function f() {}\n")
    ingest = _ingest(updater, cs.SupportedLanguage.JS)
    assert ingest.folded_overloads(root, cs.SupportedLanguage.JS, queries, None) is None


def test_the_named_passes_register_what_the_queries_miss(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    index(
        temp_repo / "tp",
        mock_ingestor,
        {
            "src/m.js": (
                "const api = { get() { return 1; } };\n"
                "module.exports.helper = () => 2;\n"
            )
        },
    )
    functions = {
        str(n.properties[cs.KEY_QUALIFIED_NAME])
        for n in get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
    }
    # An object literal's method and a CommonJS export's arrow, both named.
    assert functions == {"tp.src.m.get", "tp.src.m.helper"}
