"""`CSharpFunctionIngest`: the local functions it drops as parse-recovery
artifacts or pins to their host scope, and the members a `#if`-truncated type
spilled outside its body, recovered as that type's methods."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.csharp.function_ingest import CSharpFunctionIngest
from codebase_rag.tests.collaborator_support import first, index, nodes_of, parse
from codebase_rag.tests.test_csharp_preproc_split_body import _ORPHAN_MEMBER_SRC
from evals.recorded_graph import RecordingIngestor

_CS = cs.SupportedLanguage.CSHARP
_LOCAL = (
    "namespace N {\n  public class Host {\n"
    "    public int Run() {\n      int Twice(int a, int b) { return a + b; }\n"
    "      return Twice(1, 2);\n    }\n  }\n}\n"
)


def _ingest(updater: GraphUpdater) -> CSharpFunctionIngest:
    ingest = updater.factory.definition_processor.function_ingest.csharp
    assert isinstance(ingest, CSharpFunctionIngest)
    return ingest


def test_a_local_function_is_pinned_to_its_host_with_its_arity(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "cp", mock_ingestor, {"Host.cs": _LOCAL})
    facts = updater.factory.definition_processor.facts
    ((qn, (host_key, arity)),) = facts.csharp_local_functions.items()
    assert qn.endswith(".Run.Twice")
    assert arity == 2
    # Keyed by the method that hosts it: `Run` starts on line 3.
    assert host_key[0] == "cp.Host"
    assert host_key[1] == 3


def test_a_keyword_named_local_function_is_a_parse_artifact(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "cp", mock_ingestor, {"Host.cs": _LOCAL})
    ingest = _ingest(updater)
    _, queries = load_parsers()
    spec = LANGUAGE_SPECS[_CS]
    # A `#if` splitting an if/else chain recovers `else if (...)` as a local
    # function named `if`.
    split = parse(
        _CS,
        "namespace N;\npublic class C {\n  public void W(object v) {\n"
        "    if (v is int) { E(1); }\n#if X\n    else if (v is long) { E(2); }\n"
        "#endif\n    else { E(3); }\n  }\n}\n",
    )
    artifacts = [
        node
        for node in nodes_of(split, cs.TS_CSHARP_LOCAL_FUNCTION_STATEMENT)
        if node.child_by_field_name(cs.FIELD_NAME).text == b"if"
    ]
    assert artifacts
    real = first(parse(_CS, _LOCAL), cs.TS_CSHARP_LOCAL_FUNCTION_STATEMENT)

    def skips(node) -> bool:
        return ingest.skips_function(node, "cp.Host", _CS, spec, queries[_CS], None)

    assert all(skips(node) for node in artifacts)
    assert not skips(real)


def test_members_a_truncated_type_spilled_are_recovered_as_its_methods(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "cp", mock_ingestor, {"Reader.cs": _ORPHAN_MEMBER_SRC})
    ingest = _ingest(updater)
    recovered = {qn for qn in ingest.methods if ".Reader.BlockCopyChars" in qn}
    assert len(recovered) == 1
    (qn,) = recovered
    facts = updater.factory.definition_processor.facts
    assert qn in facts.csharp_call_shapes
    assert any(
        location.qualified_name == qn for location in facts.function_locations.values()
    )
