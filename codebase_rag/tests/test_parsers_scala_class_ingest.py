"""`ScalaClassIngest` binds a held-back Scala base through the imports of the
package block around its class, ahead of the class the written name found at
parse time."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.parsers.scala.class_ingest import ScalaClassIngest
from codebase_rag.tests.collaborator_support import edges, index
from codebase_rag.types_defs import DeferredInherit
from evals.recorded_graph import RecordingIngestor

_SRC = "proj.src.main.scala"
_FILES = {
    "src/main/scala/shop/Cart.scala": "package shop\nclass Cart\n",
    # Block `one` imports shop's Cart; block `two` declares a Cart of its own,
    # which is what the bare name `Cart` found while the file was parsed.
    "src/main/scala/app/Blocks.scala": (
        "package app\n\n"
        "package one {\n  import shop.Cart\n  class Special extends Cart\n}\n\n"
        "package two {\n  class Cart\n  class Local extends Cart\n}\n"
    ),
}


def _indexed(temp_repo: Path, mock_ingestor: RecordingIngestor) -> ScalaClassIngest:
    updater = index(temp_repo / "proj", mock_ingestor, _FILES)
    ingest = updater.factory.definition_processor.class_ingest.scala
    assert isinstance(ingest, ScalaClassIngest)
    return ingest


def test_a_base_a_block_imports_binds_through_that_import(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    _indexed(temp_repo, mock_ingestor)
    inherits = edges(mock_ingestor, cs.RelationshipType.INHERITS)
    assert (f"{_SRC}.app.Blocks.Special", f"{_SRC}.shop.Cart.Cart") in inherits
    assert (f"{_SRC}.app.Blocks.Special", f"{_SRC}.app.Blocks.Cart") not in inherits


def test_a_base_no_block_imports_keeps_the_class_the_file_declares(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    _indexed(temp_repo, mock_ingestor)
    inherits = edges(mock_ingestor, cs.RelationshipType.INHERITS)
    assert (f"{_SRC}.app.Blocks.Local", f"{_SRC}.app.Blocks.Cart") in inherits


def test_imported_base_answers_from_the_import_around_the_class(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    ingest = _indexed(temp_repo, mock_ingestor)
    module_qn = f"{_SRC}.app.Blocks"

    def entry(child: str) -> DeferredInherit:
        return DeferredInherit(
            rel_type=cs.RelationshipType.INHERITS,
            child_qn=f"{module_qn}.{child}",
            parent_qn=f"{module_qn}.Cart",
            module_qn=module_qn,
            base_index=0,
            language=cs.SupportedLanguage.SCALA,
        )

    assert ingest.imported_base(entry("Special")) == f"{_SRC}.shop.Cart.Cart"
    assert ingest.imported_base(entry("Local")) is None
