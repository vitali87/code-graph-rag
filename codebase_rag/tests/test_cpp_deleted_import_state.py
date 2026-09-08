"""Deleting a C/C++ file retracts the import state recorded by its parse."""

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import _MockIngestor

INCLUDES = "#include <widget>\n#include <widget.h>\n"


def _updater(root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    for language in (cs.SupportedLanguage.C, cs.SupportedLanguage.CPP):
        if language not in parsers:
            pytest.skip(f"{language} parser not available")
    updater = GraphUpdater(
        ingestor=_MockIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run()
    return updater


def _module_qn(updater: GraphUpdater, path: Path) -> str:
    owners = updater.factory.definition_processor.module_qn_to_file_path
    return next(qn for qn, owner in owners.items() if owner == path)


@pytest.mark.parametrize("suffix", (*cs.C_EXTENSIONS, *cs.CPP_EXTENSIONS))
def test_deleted_c_cpp_file_retracts_imports(tmp_path: Path, suffix: str) -> None:
    gone = tmp_path / f"gone{suffix}"
    kept = tmp_path / f"kept{suffix}"
    gone.write_text(INCLUDES, encoding="utf-8")
    kept.write_text(INCLUDES, encoding="utf-8")
    updater = _updater(tmp_path)
    processor = updater.factory.import_processor
    gone_qn = _module_qn(updater, gone)
    kept_qn = _module_qn(updater, kept)
    assert processor.import_mapping[gone_qn]
    assert (gone_qn, "std.widget") in processor._cpp_shadowed_include_targets
    kept_mapping = processor.import_mapping[kept_qn].copy()
    kept_shadowed = {
        entry
        for entry in processor._cpp_shadowed_include_targets
        if entry[0] == kept_qn
    }
    assert kept_shadowed

    gone.unlink()
    updater.remove_file_from_state(gone)
    updater.remove_file_from_state(gone)

    assert gone_qn not in processor.import_mapping
    assert processor._cpp_shadowed_include_targets == kept_shadowed
    assert processor.import_mapping[kept_qn] == kept_mapping


def test_deleted_module_declaration_retracts_its_exemption(tmp_path: Path) -> None:
    gone = tmp_path / "gone.cppm"
    kept = tmp_path / "kept.cppm"
    gone.write_text("export module removed;\n", encoding="utf-8")
    kept.write_text("export module retained;\n", encoding="utf-8")
    updater = _updater(tmp_path)
    processor = updater.factory.import_processor
    gone_qn = _module_qn(updater, gone)
    kept_qn = _module_qn(updater, kept)
    assert (gone_qn, "proj.removed") in processor._cpp_declaration_mappings
    assert (kept_qn, "proj.retained") in processor._cpp_declaration_mappings

    gone.unlink()
    updater.remove_file_from_state(gone)

    assert gone_qn not in processor.import_mapping
    assert processor._cpp_declaration_mappings == {(kept_qn, "proj.retained")}
    assert processor.import_mapping[kept_qn]


def test_deleted_same_stem_file_uses_its_recorded_module(tmp_path: Path) -> None:
    source = tmp_path / "unit.cpp"
    header = tmp_path / "unit.hpp"
    source.write_text(INCLUDES, encoding="utf-8")
    header.write_text(INCLUDES, encoding="utf-8")
    updater = _updater(tmp_path)
    processor = updater.factory.import_processor
    modules = {_module_qn(updater, path): path for path in (source, header)}
    assert len(modules) == 2
    assert "proj.unit" in modules
    gone_qn = next(qn for qn in modules if qn != "proj.unit")
    gone = modules[gone_qn]
    kept_mapping = processor.import_mapping["proj.unit"].copy()
    assert processor.import_mapping[gone_qn]

    gone.unlink()
    updater.remove_file_from_state(gone)

    assert gone_qn not in processor.import_mapping
    assert processor.import_mapping["proj.unit"] == kept_mapping
    assert all(
        entry[0] == "proj.unit" for entry in processor._cpp_shadowed_include_targets
    )
