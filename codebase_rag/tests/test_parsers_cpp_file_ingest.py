"""`CppFileIngest`: the C++20 module unit a file declares and the type
aliases its `typedef`s and `using`s name, recorded once the file's imports
are in."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.parsers.cpp.file_ingest import CppFileIngest
from codebase_rag.tests.collaborator_support import index
from codebase_rag.tests.conftest import get_nodes
from evals.recorded_graph import RecordingIngestor


def test_typedefs_and_usings_name_their_class(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(
        temp_repo / "cx",
        mock_ingestor,
        {
            "src/alias.h": (
                "class Derived {};\n"
                "using DerivedPtr = Derived*;\n"
                "typedef Derived DAlias;\n"
                "using Count = int;\n"
            )
        },
    )
    ingest = updater.factory.definition_processor._for(cs.SupportedLanguage.CPP)
    assert isinstance(ingest, CppFileIngest)
    aliases = updater.factory.definition_processor.facts.type_aliases
    assert aliases["DerivedPtr"] == "Derived"
    assert aliases["DAlias"] == "Derived"


def test_an_alias_two_files_spell_differently_is_dropped(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(
        temp_repo / "cx",
        mock_ingestor,
        {
            "src/a.h": "class A {};\nclass B {};\nusing Handle = A;\nusing Same = A;\n",
            "src/b.h": '#include "a.h"\nusing Handle = B;\nusing Same = A;\n',
        },
    )
    aliases = updater.factory.definition_processor.facts.type_aliases
    assert "Handle" not in aliases
    assert aliases["Same"] == "A"


def test_a_module_interface_unit_is_recorded_with_its_file(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    index(
        temp_repo / "cx",
        mock_ingestor,
        {
            "src/core.cppm": "export module shapes.core;\nexport int area() { return 1; }\n"
        },
    )
    (interface,) = get_nodes(mock_ingestor, cs.NodeLabel.MODULE_INTERFACE)
    assert interface.properties[cs.KEY_QUALIFIED_NAME] == "cx.shapes.core"
    assert interface.properties[cs.KEY_PATH] == "src/core.cppm"
